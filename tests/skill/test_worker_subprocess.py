"""Real subprocess lifecycle test with an offline fake worker.

Unlike the ``_spawn_runner`` stubs elsewhere, this launches an actual Python
child process via ``_spawn_runner`` (with an injected command) that only writes
synthetic IPC files — no device, model, network, or local config. It exercises
Popen, PID handling, process exit, and the "exit 0 but harness failed" rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import run_diagnosis as rd

_FAKE_WORKER = r'''
import json, os, sys

spec = json.load(open(sys.argv[1]))
events = spec["events_path"]
run_dir = os.path.dirname(events)

with open(os.path.join(run_dir, "runner.pid"), "w") as fh:
    fh.write(str(os.getpid()))

def put(payload):
    with open(events, "a") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

put({"event": "tool_call", "step": 1, "tool": "read_screen", "args": {"intent": "x"}})
put({"event": "tool_result", "step": 1, "tool": "read_screen", "text": "[OBS] app=x screen#1", "ok": True, "latency_ms": 1})
put({"event": "run_end", "status": "failed", "result": {"success": False, "reason": "model_stopped", "steps": 1}, "tokens_total": 0})
with open(os.path.join(run_dir, "run.json"), "w") as fh:
    json.dump({"run_id": spec["run_id"], "status": "failed", "result": {"success": False, "reason": "model_stopped", "steps": 1}}, fh)
try:
    os.remove(os.path.join(run_dir, "runner.pid"))
except FileNotFoundError:
    pass
'''


def _write_case(tmp_path: Path) -> Path:
    path = tmp_path / "case.json"
    path.write_text(
        json.dumps({"id": "c1", "title": "t", "goal": "打开设置", "acceptance": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _patch_for_fake_worker(monkeypatch, tmp_path: Path):
    script = tmp_path / "fake_runner.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(rd, "_resolve_config", lambda args, run_dir: object())

    def fake_build(config, run_id, task, run_dir):
        spec = run_dir / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "task": task,
                    "overrides": {},
                    "snapshot": {},
                    "events_path": str(run_dir / "events.jsonl"),
                    "control_path": str(run_dir / "control.jsonl"),
                }
            ),
            encoding="utf-8",
        )
        return {"spec_path": str(spec)}

    monkeypatch.setattr(rd, "_build_spec", fake_build)
    monkeypatch.setattr(rd, "_runner_command", lambda spec_path: [sys.executable, str(script), str(spec_path)])


def test_real_subprocess_exit0_is_not_harness_success(tmp_path, monkeypatch, capsys):
    _patch_for_fake_worker(monkeypatch, tmp_path)
    case = _write_case(tmp_path)
    rc = rd.main(["start", str(case), "--output-dir", str(tmp_path)])
    assert rc == 0
    info = json.loads(capsys.readouterr().out)
    run_dir = Path(info["run_dir"])
    assert info["pid"] > 0

    # wait polls the real worker process to completion.
    rc = rd.main(["wait", str(run_dir), "--timeout", "15", "--quiet"])
    assert rc == 2  # harness failed, despite the worker exiting 0

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["harness_terminal"]["state"] == "failed"
    assert summary["harness_terminal"]["run_end_seen"] is True
    assert summary["verdict"] == "failed"
    # worker deleted its own runner.pid; the launcher must not have written a stale one.
    assert not (run_dir / "runner.pid").exists()
    # real events were produced
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8").strip()


_EARLY_EXIT_WORKER = "import sys\nsys.exit(3)\n"


def _patch_for_early_exit(monkeypatch, tmp_path: Path):
    script = tmp_path / "early_exit.py"
    script.write_text(_EARLY_EXIT_WORKER, encoding="utf-8")
    monkeypatch.setattr(rd, "_resolve_config", lambda args, run_dir: object())

    def fake_build(config, run_id, task, run_dir):
        spec = run_dir / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "task": task,
                    "overrides": {},
                    "snapshot": {},
                    "events_path": str(run_dir / "events.jsonl"),
                    "control_path": str(run_dir / "control.jsonl"),
                }
            ),
            encoding="utf-8",
        )
        return {"spec_path": str(spec)}

    monkeypatch.setattr(rd, "_build_spec", fake_build)
    monkeypatch.setattr(rd, "_runner_command", lambda spec_path: [sys.executable, str(script)])


def test_detached_worker_exits_before_pid_is_rc4_fast(tmp_path, monkeypatch, capsys):
    _patch_for_early_exit(monkeypatch, tmp_path)
    case = _write_case(tmp_path)
    assert rd.main(["start", str(case), "--output-dir", str(tmp_path)]) == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    rc = rd.main(["wait", str(run_dir), "--timeout", "30", "--quiet"])
    assert rc == 4  # launch-pid fallback: dead, not a 30s spin
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    # never faked as success/failure; no terminal event
    assert summary["harness_terminal"]["state"] in {"unknown", "unknown_terminated"}
    assert summary["verdict"] == "uncertain"
    assert summary["harness_terminal"]["run_end_seen"] is False
    # artifacts preserved; no run_end fabricated
    assert (run_dir / "case.json").exists()
    assert (run_dir / "launch.json").exists()
    assert not (run_dir / "runner.pid").exists()


def test_foreground_worker_exits_before_pid_is_rc4(tmp_path, monkeypatch, capsys):
    _patch_for_early_exit(monkeypatch, tmp_path)
    case = _write_case(tmp_path)
    rc = rd.main(["case", str(case), "--output-dir", str(tmp_path), "--timeout", "30", "--quiet"])
    assert rc == 4
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    assert (run_dir / "case.json").exists()
