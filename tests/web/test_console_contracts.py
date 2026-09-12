"""Confirmed blockers for the first manual real-console task.

Covers the read-only App-KB table projection, content-based memory generation
identity, optional-memory drift auditing, and explicit blank device overrides.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from phone_agent.v2.agent import RunResult
from phone_agent.v2.appkb import AppKnowledgeStore
from phone_agent.v2.config import V2Config
from phone_agent.v2.run_ipc import (
    JsonlReader,
    app_kb_generation,
    config_fingerprint,
    read_run_spec,
)
from phone_agent.v2.runner import run_spec
from phone_agent.v2.usage import UsageLedger
from phone_agent.web.app import _device_override
from phone_agent.web.bridge import WebRunBridge

_ENTRY = {
    "term": "设置",
    "label": "设置",
    "package": "com.android.settings",
    "kind": "device",
    "scope": "device:SERIAL-A",
    "confidence": 1.0,
}
_OTHER_ENTRY = {
    "term": "相机",
    "label": "相机",
    "package": "com.android.camera",
    "kind": "device",
    "scope": "device:SERIAL-A",
    "confidence": 1.0,
}


def _config(tmp_path: Path) -> V2Config:
    return V2Config(
        base_url="http://example.invalid/v1",
        model_name="fake",
        memory_dir=str(tmp_path / "memory"),
        runs_dir=str(tmp_path / "runs"),
        experience_enabled=False,
        memory_rag="off",
    )


def _bridge(tmp_path: Path) -> WebRunBridge:
    return WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))


def _pending_process() -> Any:
    class PendingProcess:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    return PendingProcess()


class _FakeAgent:
    def __init__(self, config, *, extra_middleware, run_id):  # noqa: ANN001
        self.session = SimpleNamespace(takeover_reason=None)
        self.trace_path = None
        self.usage_ledger = UsageLedger()

    def run(self, task, hitl_handler):  # noqa: ANN001
        return RunResult(True, "done", 0, None)


def _set_console_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHONE_AGENT_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("PHONE_AGENT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("PHONE_AGENT_EXPERIENCE", "off")
    monkeypatch.setenv("PHONE_AGENT_MEMORY_RAG", "off")


def test_kb_entries_reads_snapshot_without_storing(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    store = AppKnowledgeStore(str(memory))
    store.upsert(_ENTRY)
    before_kb = (store.kb_path.read_bytes(), store.kb_path.stat().st_mtime_ns)
    before_events = store.events_path.stat().st_mtime_ns

    from phone_agent.v2 import appkb

    class _ExplodingStore:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("kb_entries must not construct AppKnowledgeStore")

    monkeypatch.setattr(appkb, "AppKnowledgeStore", _ExplodingStore)
    bridge = _bridge(tmp_path)

    rows = bridge.kb_entries()
    assert [row["package"] for row in rows] == ["com.android.settings"]
    assert bridge.kb_entries() == rows
    assert (store.kb_path.read_bytes(), store.kb_path.stat().st_mtime_ns) == before_kb
    assert store.events_path.stat().st_mtime_ns == before_events


def test_kb_entries_fail_open_on_missing_corrupt_or_odd_rows(tmp_path):
    memory = tmp_path / "memory"
    bridge = _bridge(tmp_path)
    assert bridge.kb_entries() == []
    assert not memory.exists()

    kb_path = memory / "app_kb" / "kb.json"
    kb_path.parent.mkdir(parents=True)
    kb_path.write_text("{not json", encoding="utf-8")
    assert bridge.kb_entries() == []
    assert kb_path.read_text(encoding="utf-8") == "{not json"

    kb_path.write_text(
        json.dumps([_ENTRY, "junk", {"label": "缺包名", "kind": "user"}, 7]),
        encoding="utf-8",
    )
    rows = bridge.kb_entries()
    assert [row["package"] for row in rows] == ["com.android.settings"]
    assert rows[0]["success_count"] == 0
    assert rows[0]["stale"] is False


def test_memory_generation_is_content_stable_across_rewrite_and_touch(tmp_path):
    memory = tmp_path / "memory"
    store = AppKnowledgeStore(str(memory))
    store.upsert(_ENTRY)
    config = _config(tmp_path)
    first = app_kb_generation(config)
    assert first is not None and first["source"] == "kb.json.digest"

    AppKnowledgeStore(str(memory))
    assert app_kb_generation(config) == first

    stat = store.kb_path.stat()
    os.utime(store.kb_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert app_kb_generation(config) == first

    store.upsert(_OTHER_ENTRY)
    assert app_kb_generation(config) != first

    store.kb_path.unlink()
    assert app_kb_generation(config) is None


def test_web_refresh_between_spec_and_runner_start_does_not_reject(
    tmp_path, monkeypatch
):
    memory = tmp_path / "memory"
    store = AppKnowledgeStore(str(memory))
    store.upsert(_ENTRY)
    captured = {}

    def fake_popen(argv, **_kwargs):
        captured["spec"] = read_run_spec(argv[-1])
        return _pending_process()

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    _set_console_env(monkeypatch, tmp_path)
    bridge = WebRunBridge(
        {}, config_factory=V2Config.from_env, popen_factory=fake_popen
    )
    bridge.start("打开设置")
    spec = captured["spec"]
    assert spec.snapshot["memory_generation"] == app_kb_generation(
        V2Config(**spec.overrides)
    )

    kb_before = (store.kb_path.read_bytes(), store.kb_path.stat().st_mtime_ns)
    events_before = store.events_path.stat().st_mtime_ns
    for _ in range(3):
        rows = bridge.kb_entries()
        assert [row["package"] for row in rows] == ["com.android.settings"]
    assert (store.kb_path.read_bytes(), store.kb_path.stat().st_mtime_ns) == kb_before
    assert store.events_path.stat().st_mtime_ns == events_before

    AppKnowledgeStore(str(memory))

    assert run_spec(spec, agent_factory=_FakeAgent, poll_seconds=0.01) == 0
    events = JsonlReader(spec.events_path).read_new()
    assert [event["event"] for event in events] == [
        "capability_snapshot",
        "run_end",
    ]
    assert events[-1]["status"] == "succeeded"


def test_true_memory_drift_is_audited_not_fatal(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    store = AppKnowledgeStore(str(memory))
    store.upsert(_ENTRY)
    captured = {}

    def fake_popen(argv, **_kwargs):
        captured["spec"] = read_run_spec(argv[-1])
        return _pending_process()

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    _set_console_env(monkeypatch, tmp_path)
    bridge = WebRunBridge(
        {}, config_factory=V2Config.from_env, popen_factory=fake_popen
    )
    bridge.start("打开设置")
    spec = captured["spec"]

    store.upsert(_OTHER_ENTRY)
    actual = app_kb_generation(V2Config(**spec.overrides))
    assert actual != spec.snapshot["memory_generation"]

    assert run_spec(spec, agent_factory=_FakeAgent, poll_seconds=0.01) == 0
    events = JsonlReader(spec.events_path).read_new()
    drift = [event for event in events if event["event"] == "memory_generation_drift"]
    assert len(drift) == 1
    assert drift[0]["captured"] == spec.snapshot["memory_generation"]
    assert drift[0]["actual"] == actual
    assert events[-1]["event"] == "run_end"
    assert events[-1]["status"] == "succeeded"


def test_device_override_blank_whitespace_omitted_and_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_DEVICE_ID", "ENV-SERIAL")
    _set_console_env(monkeypatch, tmp_path)
    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)

    def spec_for(overrides: dict[str, Any]) -> Any:
        captured = {}

        def fake_popen(argv, **_kwargs):
            captured["spec"] = read_run_spec(argv[-1])
            return _pending_process()

        bridge = WebRunBridge(
            dict(overrides),
            config_factory=V2Config.from_env,
            popen_factory=fake_popen,
        )
        bridge.start("设备覆盖测试")
        spec = captured["spec"]
        Path(spec.events_path).with_name("runner.pid").unlink(missing_ok=True)
        return spec

    assert V2Config.from_env({"device_id": None}).device_id == "ENV-SERIAL"
    assert _device_override(None) == ""
    assert _device_override("   ") == ""
    assert _device_override(" UI-SERIAL ") == "UI-SERIAL"

    assert spec_for({}).overrides["device_id"] == "ENV-SERIAL"
    blank = spec_for({"device_id": ""})
    assert blank.overrides["device_id"] is None
    assert blank.snapshot["config_fingerprint"] == config_fingerprint(blank.overrides)
    assert spec_for({"device_id": "   "}).overrides["device_id"] is None
    assert spec_for({"device_id": "UI-SERIAL"}).overrides["device_id"] == "UI-SERIAL"
