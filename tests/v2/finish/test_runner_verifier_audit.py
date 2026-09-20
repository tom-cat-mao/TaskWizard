"""The runner persists the finish verifier's decision into ``run.json`` (WP3).

Before this, the verifier's reason was returned in-band and then dropped: it
lived in the control tool's process and vanished with the run, so the diagnosis
analysis could only ever report ``verifier_status: "unknown"`` for a finished
run (the authoritative verdict was never on disk). These tests pin the artifact
contract: the status string the analyze layer already reads, the audit record
behind it, and the honest absence of both when no verdict exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from phone_agent.v2.agent import RunResult
from phone_agent.v2.config import V2Config
from phone_agent.v2.run_ipc import (
    RunPaths,
    RunSpec,
    app_kb_generation,
    capability_snapshot,
    config_fingerprint,
    resolved_config_dict,
    write_run_spec,
)
from phone_agent.v2.runner import run_spec
from phone_agent.v2.usage import UsageLedger

_VERDICT = {
    "approve": False,
    "status": "fail",
    "reason": "未见支付成功页，缺少订单完成证据",
    "latency_ms": 1234,
    "usage": {"role": "verifier", "tokens": 30},
}


def _config(tmp_path: Path) -> V2Config:
    return V2Config(
        base_url="http://example.invalid/v1",
        model_name="fake",
        api_key="test-key",
        memory_dir=str(tmp_path / "memory"),
        runs_dir=str(tmp_path / "runs"),
        trace_dir=str(tmp_path / "traces"),
        trace_enabled=False,
        experience_enabled=False,
        memory_rag="off",
        app_kb_enabled=False,
    )


def _spec(tmp_path: Path) -> tuple[RunPaths, RunSpec]:
    config = _config(tmp_path)
    config_values = resolved_config_dict(config)
    paths = RunPaths.for_run(config.runs_dir, "run-verifier")
    paths.run_dir.mkdir(parents=True)
    spec = RunSpec(
        run_id="run-verifier",
        task="完成支付",
        overrides=config_values,
        snapshot={
            "config_fingerprint": config_fingerprint(config_values),
            "memory_generation": app_kb_generation(config),
            "capabilities": capability_snapshot(config),
            "ts": 1.0,
        },
        events_path=str(paths.events),
        control_path=str(paths.control),
    )
    write_run_spec(paths.spec, spec)
    paths.events.touch()
    paths.control.touch()
    return paths, spec


def _agent_factory(session: object):
    class FakeAgent:
        def __init__(self, config, *, extra_middleware, run_id):  # noqa: ANN001
            self.session = session
            self.trace_path = None
            self.usage_ledger = UsageLedger()

        def run(self, task, hitl_handler):  # noqa: ANN001
            return RunResult(False, "verifier reject", 3, None)

    return FakeAgent


def _run_and_read(tmp_path: Path, session: object) -> dict:
    paths, spec = _spec(tmp_path)
    assert run_spec(spec, agent_factory=_agent_factory(session), poll_seconds=0.01) == 0
    return json.loads(paths.summary.read_text(encoding="utf-8"))


def test_run_json_carries_the_verifier_status_and_audit_record(tmp_path):
    session = SimpleNamespace(
        takeover_reason=None, finish_verifier="fail", finish_verifier_verdict=_VERDICT
    )
    summary = _run_and_read(tmp_path, session)

    assert summary["status"] == "failed"
    assert summary["finish_verifier"] == "fail"
    assert summary["finish_verifier_verdict"] == _VERDICT


def test_run_json_records_no_verdict_without_claiming_one(tmp_path):
    """A run whose verifier was never triggered gets null, not a fake verdict."""

    session = SimpleNamespace(
        takeover_reason=None, finish_verifier="skipped", finish_verifier_verdict=None
    )
    summary = _run_and_read(tmp_path, session)

    assert summary["finish_verifier"] == "skipped"
    assert summary["finish_verifier_verdict"] is None


def test_run_json_omits_the_keys_when_there_is_no_session(tmp_path):
    """No session means no observation — the artifact never invents a verdict."""

    class NoSessionAgent:
        def __init__(self, config, *, extra_middleware, run_id):  # noqa: ANN001
            self.trace_path = None
            self.usage_ledger = UsageLedger()

        def run(self, task, hitl_handler):  # noqa: ANN001
            return RunResult(False, "no session", 1, None)

    paths, spec = _spec(tmp_path)
    assert run_spec(spec, agent_factory=NoSessionAgent, poll_seconds=0.01) == 0
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))

    assert "finish_verifier" not in summary
    assert "finish_verifier_verdict" not in summary
