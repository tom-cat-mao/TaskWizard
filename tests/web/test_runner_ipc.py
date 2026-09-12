"""Detached runner protocol, control channel, and bridge reattach tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from phone_agent.v2.agent import RunResult
from phone_agent.v2.config import V2Config
from phone_agent.v2.run_ipc import (
    JsonlReader,
    JsonlWriter,
    RunPaths,
    RunSpec,
    app_kb_generation,
    append_control,
    capability_snapshot,
    config_fingerprint,
    read_run_spec,
    resolved_config_dict,
    write_run_spec,
)
from phone_agent.v2.runner import ControlChannel, run_spec
from phone_agent.v2.usage import UsageLedger
from phone_agent.web.bridge import WebRunBridge


def _config(tmp_path: Path) -> V2Config:
    return V2Config(
        base_url="http://example.invalid/v1",
        model_name="fake",
        memory_dir=str(tmp_path / "memory"),
        runs_dir=str(tmp_path / "runs"),
        trace_enabled=False,
        experience_enabled=False,
        memory_rag="off",
    )


def _spec(tmp_path: Path, run_id: str = "run-1") -> tuple[RunPaths, RunSpec]:
    config = _config(tmp_path)
    config_values = resolved_config_dict(config)
    paths = RunPaths.for_run(config.runs_dir, run_id)
    paths.run_dir.mkdir(parents=True)
    snapshot = {
        "config_fingerprint": config_fingerprint(config_values),
        "memory_generation": app_kb_generation(config),
        "capabilities": capability_snapshot(config),
        "ts": 123.0,
    }
    spec = RunSpec(
        run_id=run_id,
        task="打开设置",
        overrides=config_values,
        snapshot=snapshot,
        events_path=str(paths.events),
        control_path=str(paths.control),
    )
    write_run_spec(paths.spec, spec)
    paths.events.touch()
    paths.control.touch()
    return paths, spec


def _event(kind: str, **values):
    return {"event": kind, **values}


def test_run_spec_roundtrip_and_explicit_snapshot(tmp_path):
    paths, spec = _spec(tmp_path)
    loaded = read_run_spec(paths.spec)

    assert loaded == spec
    assert loaded.snapshot["config_fingerprint"].startswith("sha256:")
    assert loaded.snapshot["memory_generation"] is None
    assert loaded.snapshot["capabilities"]["budget"]["state"] == "active"
    assert loaded.overrides["runs_dir"] == str(tmp_path / "runs")


def test_bridge_snapshot_public_fields_are_unchanged(tmp_path):
    bridge = WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))
    assert set(bridge.snapshot()) == {
        "run_id",
        "task",
        "status",
        "current_screen",
        "current_app",
        "screen_seq",
        "screens",
        "steps",
        "task_board",
        "pending_hitl_prompt",
        "final_result",
        "tokens",
        "usage",
        "capabilities",
        "error",
        "activity",
        "last_event_ts",
        "started_at",
        "stop_requested",
        "requested_model",
        "actual_model",
        # Observe-only incremental model text (per-attempt projection).
        "stream_attempts",
    }


def test_idle_snapshot_and_start_do_not_execute_plugin(tmp_path, monkeypatch):
    from phone_agent.v2 import plugins

    plugin_dir = tmp_path / "idle-plugin"
    plugin_dir.mkdir()
    counter = tmp_path / "imports.txt"
    (plugin_dir / "plugin.py").write_text(
        "from pathlib import Path\n"
        "from phone_agent.v2.capabilities import CapabilitySpec\n"
        f"Path({str(counter)!r}).write_text('loaded', encoding='utf-8')\n"
        "CAPABILITY = CapabilitySpec('idle_plugin', 'Idle Plugin', 'on')\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.plugin_manifest = str(tmp_path / "project.toml")
    plugins.write_manifest(
        config.plugin_manifest,
        [plugins.PluginEntry(name="idle", path=str(plugin_dir))],
    )

    class PendingProcess:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(
        config_factory=lambda _overrides: config,
        popen_factory=lambda *_args, **_kwargs: PendingProcess(),
    )

    rows = {row["cap_id"]: row for row in bridge.snapshot()["capabilities"]}
    assert "idle_plugin" not in rows
    bridge.start("test")
    assert not counter.exists()


def test_bridge_start_serializes_resolved_snapshot_before_spawn(tmp_path, monkeypatch):
    captured = {}

    class PendingProcess:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        captured["spec"] = read_run_spec(argv[-1])
        captured["kwargs"] = kwargs
        return PendingProcess()

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(
        {"max_model_calls": 12},
        config_factory=lambda overrides: V2Config(
            base_url="http://example.invalid/v1",
            model_name="fake",
            runs_dir=str(tmp_path / "runs"),
            max_model_calls=int((overrides or {}).get("max_model_calls", 100)),
        ),
        popen_factory=fake_popen,
    )

    run_id = bridge.start("打开设置", overrides={"max_model_calls": 7})
    spec = captured["spec"]
    assert run_id == spec.run_id
    assert spec.task == "打开设置"
    assert spec.overrides["max_model_calls"] == 7
    assert spec.snapshot["config_fingerprint"] == config_fingerprint(spec.overrides)
    assert spec.snapshot["capabilities"] == capability_snapshot(V2Config(**spec.overrides))
    assert captured["kwargs"]["start_new_session"] is True
    assert Path(spec.events_path).name == "events.jsonl"
    assert Path(spec.control_path).name == "control.jsonl"
    assert (Path(spec.events_path).parent.stat().st_mode & 0o777) == 0o700


def test_bridge_spawn_failure_writes_replayable_terminal_state(tmp_path):
    def fail_spawn(*_args, **_kwargs):
        raise OSError("cannot spawn")

    bridge = WebRunBridge(
        config_factory=lambda _overrides: _config(tmp_path),
        popen_factory=fail_spawn,
    )
    with pytest.raises(RuntimeError, match="runner 启动失败"):
        bridge.start("打开设置")

    state = bridge.snapshot()
    assert state["status"] == "error"
    assert state["error"] == "error: runner_start_failed:OSError"
    run_dir = Path(_config(tmp_path).runs_dir) / str(state["run_id"])
    assert JsonlReader(run_dir / "events.jsonl").read_new()[-1]["event"] == "run_end"
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))[
        "status"
    ] == "error"


def test_memory_generation_prefers_explicit_then_content_digest(tmp_path):
    config = _config(tmp_path)
    kb_path = Path(config.memory_dir) / "app_kb/kb.json"
    assert app_kb_generation(config) is None

    kb_path.parent.mkdir(parents=True)
    kb_path.write_text('{"generation": 7}', encoding="utf-8")
    assert app_kb_generation(config) == {"source": "kb.json.generation", "value": 7}

    kb_path.write_text('[{"package": "a.b", "term": "A"}]', encoding="utf-8")
    first = app_kb_generation(config)
    assert first is not None and first["source"] == "kb.json.digest"
    kb_path.write_text('  [ {"term": "A", "package": "a.b"} ]  ', encoding="utf-8")
    assert app_kb_generation(config) == first
    kb_path.write_text('[{"package": "a.b", "term": "B"}]', encoding="utf-8")
    assert app_kb_generation(config) != first


def test_jsonl_roundtrip_keeps_partial_line_for_next_poll(tmp_path):
    path = tmp_path / "events.jsonl"
    with JsonlWriter(path) as writer:
        writer.put({"event": "screen", "image": "data:image/png;base64,AAA"})
    with path.open("ab") as stream:
        stream.write(b'{"event":"model_call"')

    reader = JsonlReader(path)
    assert reader.read_new() == [
        {"event": "screen", "image": "data:image/png;base64,AAA"}
    ]
    with path.open("ab") as stream:
        stream.write(b'}\n')
    assert reader.read_new() == [{"event": "model_call"}]


def test_jsonl_writer_isolates_a_torn_record(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event":"screen"')
    with JsonlWriter(path) as writer:
        writer.put({"event": "run_end", "status": "error"})

    assert JsonlReader(path).read_new() == [{"event": "run_end", "status": "error"}]


def test_control_channel_consumes_stop_and_hitl(tmp_path):
    path = tmp_path / "control.jsonl"
    path.touch()
    stopped = []
    channel = ControlChannel(path, stop_callback=lambda: stopped.append(True), poll_seconds=0.01)
    channel.start()
    try:
        append_control(path, {"type": "hitl", "answer": "approve"})
        assert channel.wait_for_hitl() == "approve"
        append_control(path, {"type": "stop"})
        deadline = time.monotonic() + 1
        while not stopped:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert channel.wait_for_hitl() == "reject"
    finally:
        channel.close()


def test_runner_writes_events_summary_and_removes_pid(tmp_path):
    paths, spec = _spec(tmp_path)

    class FakeAgent:
        def __init__(self, config, *, extra_middleware, run_id):  # noqa: ANN001
            assert run_id == spec.run_id
            self.session = SimpleNamespace(takeover_reason=None)
            self.trace_path = None
            self.middleware = extra_middleware[0]
            self.usage_ledger = UsageLedger()

        def run(self, task, hitl_handler):  # noqa: ANN001
            assert task == spec.task
            self.middleware.emit(
                _event(
                    "model_call",
                    step=1,
                    latency_ms=2,
                    tokens=3,
                    tokens_total=3,
                    error=None,
                )
            )
            self.usage_ledger.record("actor", estimate_tokens=3)
            return RunResult(True, "done", 1, None)

    assert run_spec(spec, agent_factory=FakeAgent, poll_seconds=0.01) == 0
    events = JsonlReader(paths.events).read_new()
    assert [event["event"] for event in events] == [
        "capability_snapshot",
        "model_call",
        "run_end",
    ]
    assert events[-1]["status"] == "succeeded"
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["snapshot"] == spec.snapshot
    assert summary["usage"] == {"actor": 3}
    assert not paths.pid.exists()


def test_runner_discovers_and_passes_authorized_local_plugin(
    tmp_path, monkeypatch
):
    from phone_agent.v2 import plugins

    plugin_dir = tmp_path / "fake-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "from pathlib import Path\n"
        "from phone_agent.v2.capabilities import CapabilitySpec\n"
        f"counter = Path({str(tmp_path / 'imports.txt')!r})\n"
        "counter.write_text(counter.read_text() + 'x' if counter.exists() else 'x')\n"
        "def apply(ctx):\n"
        "    ctx.register_service('runner_effect', 'ready')\n"
        "CAPABILITY = CapabilitySpec(\n"
        "    'runner_plugin', 'Runner Plugin', 'on', apply=apply,\n"
        "    provides='runner_effect',\n"
        ")\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.plugin_manifest = str(tmp_path / "project.toml")
    plugins.write_manifest(
        config.plugin_manifest,
        [plugins.PluginEntry(name="fake", path=str(plugin_dir))],
    )
    monkeypatch.setattr(
        plugins, "user_manifest_path", lambda: tmp_path / "user.toml"
    )
    paths = RunPaths.for_run(config.runs_dir, "plugin-run")
    paths.run_dir.mkdir(parents=True)
    values = resolved_config_dict(config)
    snapshot = {
        "config_fingerprint": config_fingerprint(values),
        "memory_generation": app_kb_generation(config),
        "capabilities": capability_snapshot(config),
        "ts": 123.0,
    }
    spec = RunSpec(
        run_id="plugin-run",
        task="test",
        overrides=values,
        snapshot=snapshot,
        events_path=str(paths.events),
        control_path=str(paths.control),
    )
    paths.events.touch()
    paths.control.touch()

    class FakeAgent:
        def __init__(
            self, config, *, extra_middleware, run_id, extra_capabilities
        ):
            assert [item.cap_id for item in extra_capabilities] == ["runner_plugin"]
            from phone_agent.v2.capabilities import (
                CapabilityAssemblyContext,
                assemble_capabilities,
                build_capability_registry,
            )

            self.capability_registry = build_capability_registry(config)
            for spec in extra_capabilities:
                self.capability_registry.register(spec)
            self.capability_context = assemble_capabilities(
                self.capability_registry, CapabilityAssemblyContext()
            )
            assert self.capability_context.service("runner_effect") == "ready"
            self.session = SimpleNamespace(takeover_reason=None)
            self.trace_path = None
            self.usage_ledger = UsageLedger()

        def run(self, task, hitl_handler):
            return RunResult(True, "done", 0, None)

    assert run_spec(spec, agent_factory=FakeAgent, poll_seconds=0.01) == 0
    assert (tmp_path / "imports.txt").read_text() == "x"
    events = JsonlReader(paths.events).read_new()
    actual = next(event for event in events if event["event"] == "capability_snapshot")
    assert actual["capabilities"]["runner_plugin"]["state"] == "active"
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["snapshot"]["capabilities"] == actual["capabilities"]


def test_runner_rejects_snapshot_drift_as_terminal_event(tmp_path):
    paths, spec = _spec(tmp_path)
    drifted = RunSpec(
        **{
            **spec.to_dict(),
            "snapshot": {**spec.snapshot, "config_fingerprint": "sha256:wrong"},
        }
    )

    assert run_spec(drifted, agent_factory=lambda *_a, **_k: None, poll_seconds=0.01) == 1
    terminal = JsonlReader(paths.events).read_new()[-1]
    assert terminal["event"] == "run_end"
    assert terminal["status"] == "error"
    assert "fingerprint mismatch" in terminal["result"]["reason"]


def test_bridge_consumes_runner_actual_capability_snapshot(tmp_path, monkeypatch):
    paths, spec = _spec(tmp_path)
    paths.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with JsonlWriter(paths.events) as writer:
        writer.put(
            _event(
                "capability_snapshot",
                capabilities={
                    "runtime_plugin": {
                        "cap_id": "runtime_plugin",
                        "title": "Runtime Plugin",
                        "mode": "on",
                        "state": "active",
                        "missing_deps": [],
                    }
                },
            )
        )

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))

    rows = {row["cap_id"]: row for row in bridge.snapshot()["capabilities"]}
    assert rows["runtime_plugin"]["state"] == "active"


def test_dead_runner_repair_preserves_actual_capability_snapshot(tmp_path):
    paths, spec = _spec(tmp_path)
    actual = {
        "runtime_plugin": {
            "cap_id": "runtime_plugin",
            "title": "Runtime Plugin",
            "mode": "on",
            "state": "active",
            "missing_deps": [],
        }
    }
    events = [_event("capability_snapshot", capabilities=actual)]

    WebRunBridge._repair_dead_run(paths, spec, events)

    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["snapshot"]["capabilities"] == actual


def test_bridge_event_replay_and_incremental_tail_have_same_state(tmp_path, monkeypatch):
    paths, _ = _spec(tmp_path)
    paths.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
    first = [
        _event("model_call", step=1, latency_ms=4, tokens_total=10),
        _event(
            "tool_call",
            step=1,
            tool="tap",
            args={"intent": "打开设置", "target_mark_id": "ax_1@e1"},
        ),
    ]
    with JsonlWriter(paths.events) as writer:
        for event in first:
            writer.put(event)

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))
    assert bridge.snapshot()["steps"][0]["tool"] == "tap"

    with JsonlWriter(paths.events) as writer:
        writer.put(
            _event(
                "tool_result",
                step=1,
                tool="tap",
                text="已点击设置 [OBS] app=设置 screen#2",
                ok=True,
                latency_ms=6,
            )
        )
        writer.put(
            _event(
                "screen",
                step=1,
                image="data:image/png;base64,BBB",
                current_app="设置",
                screen_seq=2,
            )
        )
    state = bridge.snapshot()
    assert len(state["steps"]) == 1
    assert state["steps"][0]["status"] == "success"
    assert state["steps"][0]["screen_seq"] == 2
    assert state["current_screen"] == "data:image/png;base64,BBB"


def test_reattach_live_run_replays_hitl_and_refuses_second_start(tmp_path, monkeypatch):
    paths, spec = _spec(tmp_path)
    paths.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with JsonlWriter(paths.events) as writer:
        writer.put(_event("taskdoc_snapshot", step=1, text="## 路线\n- [in_progress] 1"))
        writer.put(_event("pending_hitl", prompt="是否确认？"))

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))
    state = bridge.snapshot()
    assert state["run_id"] == spec.run_id
    assert state["task_board"].startswith("## 路线")
    assert state["pending_hitl_prompt"] == "是否确认？"
    assert state["status"] == "waiting_hitl"
    with pytest.raises(RuntimeError, match="已有任务正在运行"):
        bridge.start("第二个任务")


def test_dead_pid_without_run_end_is_repaired_as_runner_died(tmp_path, monkeypatch):
    paths, spec = _spec(tmp_path)
    paths.pid.write_text("99999999\n", encoding="utf-8")
    monkeypatch.setattr("phone_agent.web.bridge.pid_is_alive", lambda _pid: False)
    with JsonlWriter(paths.events) as writer:
        writer.put(_event("model_call", step=2, latency_ms=1, tokens_total=9))

    bridge = WebRunBridge(config_factory=lambda _overrides: _config(tmp_path))
    state = bridge.snapshot()
    assert state["run_id"] == spec.run_id
    assert state["status"] == "error"
    assert state["error"] == "error: runner_died"
    assert state["final_result"]["steps"] == 2
    assert not paths.pid.exists()
    assert json.loads(paths.summary.read_text(encoding="utf-8"))["status"] == "error"
