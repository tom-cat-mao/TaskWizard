"""Driver control-flow contract: detached start, fake runner, HITL/stop guards.

All offline: the runner subprocess is replaced by a fake that writes IPC files,
so no device, model, network, or local config is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import run_diagnosis as rd


def _write_live_pid(run_dir: Path) -> None:
    (run_dir / "runner.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")


def _write_case(tmp_path: Path) -> Path:
    path = tmp_path / "case.json"
    path.write_text(
        json.dumps(
            {
                "id": "c1",
                "title": "t",
                "goal": "打开设置",
                "preconditions": ["已解锁"],
                "safety_boundaries": ["不得清空数据"],
                "acceptance": [{"id": "A1", "description": "d", "match": ["设置"]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _patch_offline(monkeypatch, *, spawn=None):
    monkeypatch.setattr(rd, "_resolve_config", lambda args, run_dir: object())

    def fake_build(config, run_id, task, run_dir):
        spec = run_dir / "spec.json"
        spec.write_text(
            json.dumps({"run_id": run_id, "task": task, "overrides": {}, "snapshot": {}}),
            encoding="utf-8",
        )
        return {"spec_path": str(spec)}

    monkeypatch.setattr(rd, "_build_spec", fake_build)
    monkeypatch.setattr(rd, "_spawn_runner", spawn or _default_spawn)


def _default_spawn(spec_path: Path, run_dir: Path):
    (run_dir / "events.jsonl").write_text(
        json.dumps({"event": "model_call", "step": 1}) + "\n", encoding="utf-8"
    )
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    return {"pid": 4242, "process": None}


def _only_run_dir(tmp_path: Path) -> Path:
    dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name != "case.json"]
    assert len(dirs) == 1, dirs
    return dirs[0]


def test_start_returns_immediately_and_persists_case_spec(tmp_path, monkeypatch, capsys):
    _patch_offline(monkeypatch)
    case = _write_case(tmp_path)
    rc = rd.main(["start", str(case), "--output-dir", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pid"] == 4242
    run_dir = Path(payload["run_dir"])
    assert run_dir.is_dir()
    assert (run_dir / "case.json").exists()
    assert (run_dir / "spec.json").exists()
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["harness_state"] == "running"


def test_wait_finalizes_after_run_end(tmp_path, monkeypatch, capsys):
    _patch_offline(monkeypatch)
    case = _write_case(tmp_path)
    rd.main(["start", str(case), "--output-dir", str(tmp_path)])
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}}
            )
            + "\n"
        )
    rc = rd.main(["wait", str(run_dir), "--timeout", "1", "--quiet"])
    assert rc == 0
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "report.html").exists()


def test_spawn_failure_preserves_case_and_records_error(tmp_path, monkeypatch):
    def boom(spec_path, run_dir):
        raise RuntimeError("spawn failed")

    _patch_offline(monkeypatch, spawn=boom)
    case = _write_case(tmp_path)
    rc = rd.main(["start", str(case), "--output-dir", str(tmp_path)])
    assert rc == 1
    run_dir = _only_run_dir(tmp_path)
    # Case and spec survive for a later analysis.
    assert (run_dir / "case.json").exists()
    assert (run_dir / "spec.json").exists()
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "error"
    assert status["phase"] == "spawn"


def _run_dir_with_pending(tmp_path: Path, *, terminal: bool = False) -> Path:
    run_dir = tmp_path / "live"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_live_pid(run_dir)
    events = [{"event": "model_call", "step": 1}, {"event": "pending_hitl", "step": 1, "prompt": "继续？"}]
    if terminal:
        events.append(
            {"event": "run_end", "status": "failed", "result": {"success": False, "reason": "model_stopped", "steps": 1}}
        )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    return run_dir


def test_hitl_requires_pending(tmp_path, capsys):
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "model_call", "step": 1}) + "\n", encoding="utf-8")
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    _write_live_pid(run_dir)
    rc = rd.main(["hitl", str(run_dir), "yes"])
    assert rc == 1
    assert "no pending HITL" in capsys.readouterr().err


def test_hitl_rejects_empty_and_terminal_and_duplicate(tmp_path, capsys):
    run_dir = _run_dir_with_pending(tmp_path)
    assert rd.main(["hitl", str(run_dir), "   "]) == 1
    assert "empty answer" in capsys.readouterr().err

    assert rd.main(["hitl", str(run_dir), "yes"]) == 0
    # submitted but not consumed -> a second answer is refused.
    assert rd.main(["hitl", str(run_dir), "again"]) == 1
    assert "not yet consumed" in capsys.readouterr().err

    terminal_dir = _run_dir_with_pending(tmp_path / "t", terminal=True)
    assert rd.main(["hitl", str(terminal_dir), "yes"]) == 1
    assert "already terminal" in capsys.readouterr().err


def test_hitl_cleared_by_runner_allows_next_round(tmp_path, capsys):
    run_dir = _run_dir_with_pending(tmp_path)
    assert rd.main(["hitl", str(run_dir), "yes"]) == 0
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "pending_hitl", "step": 1, "prompt": None}) + "\n")
    # now no pending -> refused (clearing consumed the prompt)
    assert rd.main(["hitl", str(run_dir), "again"]) == 1
    assert "no pending HITL" in capsys.readouterr().err


def test_stop_refused_when_terminal(tmp_path, capsys):
    terminal_dir = _run_dir_with_pending(tmp_path, terminal=True)
    assert rd.main(["stop", str(terminal_dir)]) == 1
    assert "already terminal" in capsys.readouterr().err


def test_analyze_missing_path_fails_closed(tmp_path, capsys):
    rc = rd.main(["analyze", str(tmp_path / "missing")])
    assert rc == 1
    assert "path not found" in capsys.readouterr().err


def test_report_missing_path_fails_closed(tmp_path, capsys):
    rc = rd.main(["report", str(tmp_path / "missing")])
    assert rc == 1
    assert "path not found" in capsys.readouterr().err


def test_help_lists_subcommands(capsys):
    rc = rd.main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    for command in ("start", "wait", "case", "dry-run", "monitor", "hitl", "stop", "analyze", "report", "status"):
        assert command in out


def test_unknown_flag_has_no_side_effect(tmp_path, capsys):
    with pytest.raises(SystemExit):
        rd.main(["--definitely-not-a-flag", "--output-dir", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_monitor_reports_steps_from_events(tmp_path, capsys):
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "model_call", "step": 7}) + "\n", encoding="utf-8")
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    _write_live_pid(run_dir)
    rc = rd.main(["monitor", str(run_dir)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["steps"] == 7
    assert payload["harness_state"] == "running"
    assert payload["run_end_seen"] is False


def test_dead_process_without_run_end_is_unknown_terminated(tmp_path, capsys):
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "model_call", "step": 3}) + "\n", encoding="utf-8")
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    # pid 999999 is not alive on this host.
    (run_dir / "runner.pid").write_text("999999\n", encoding="utf-8")
    rc = rd.main(["status", str(run_dir)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["harness_state"] == "unknown_terminated"
    assert payload["status_source"] == "live_events"


def test_hitl_refused_for_dead_process(tmp_path, capsys):
    run_dir = _run_dir_with_pending(tmp_path)
    (run_dir / "runner.pid").write_text("999999\n", encoding="utf-8")
    assert rd.main(["hitl", str(run_dir), "yes"]) == 1
    assert "not confirmed alive" in capsys.readouterr().err


def test_hitl_refused_after_stop_requested(tmp_path, capsys):
    run_dir = _run_dir_with_pending(tmp_path)
    (run_dir / "control.jsonl").write_text(json.dumps({"type": "stop"}) + "\n", encoding="utf-8")
    assert rd.main(["hitl", str(run_dir), "yes"]) == 1
    assert "stop was requested" in capsys.readouterr().err


def test_wait_preserves_launch_command_descriptor(tmp_path, monkeypatch, capsys):
    _patch_offline(monkeypatch)
    case = _write_case(tmp_path)
    rd.main(["start", str(case), "--output-dir", str(tmp_path)])
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}})
            + "\n"
        )
    rc = rd.main(["wait", str(run_dir), "--timeout", "5", "--quiet"])
    assert rc == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["command"] == ["run_diagnosis.py", "start", str(case)]
    # descriptor is a short argv, not keys/headers
    assert all(isinstance(part, str) and "\n" not in part for part in summary["command"])


def test_hitl_refused_when_pid_unknown(tmp_path, capsys):
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        json.dumps({"event": "pending_hitl", "step": 1, "prompt": "q?"}) + "\n", encoding="utf-8"
    )
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    # no runner.pid and no launch.json -> liveness unknown -> fail-closed
    assert rd.main(["hitl", str(run_dir), "yes"]) == 1
    assert "not confirmed alive" in capsys.readouterr().err


def test_launch_pid_fallback_detects_early_death(tmp_path, monkeypatch, capsys):
    # runner.pid is absent, but launch.json carries a dead launcher pid.
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "model_call", "step": 1}) + "\n", encoding="utf-8")
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    (run_dir / "launch.json").write_text(json.dumps({"run_id": "live", "pid": 999999, "command": ["run_diagnosis.py", "start", "x"]}), encoding="utf-8")
    rc = rd.main(["wait", str(run_dir), "--timeout", "5", "--quiet"])
    assert rc == 4
