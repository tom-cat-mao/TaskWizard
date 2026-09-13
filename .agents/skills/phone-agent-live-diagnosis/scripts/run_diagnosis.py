#!/usr/bin/env python3
"""Runner-backed live diagnosis for the TaskWizard thin-loop (v2) agent.

This driver **reuses the formal runtime** instead of reimplementing it:

* a real run is launched as ``python -m phone_agent.runner <spec.json>`` built
  from :mod:`phone_agent.v2.run_ipc` — the same assembly, safety policy,
  ``V2Config`` precedence, ``models.json``/roles and plugin authorization the
  console uses;
* ``V2Config.from_env`` applies the normal precedence (CLI flag > shell env >
  project ``.env`` > default), and the fully resolved values go into the spec;
* the diagnostic evidence stream is enabled only through the spec overrides
  (``diagnostic_evidence`` / ``diagnostic_evidence_dir`` / ``diagnostic_unredacted``),
  never by editing the runtime;
* human interaction and stop reuse the runner control channel
  (``control.jsonl``): ``hitl`` answers a *currently pending* prompt, ``stop``
  requests a soft stop. **A requested stop is not an ended run**, and a control
  answer is not execution fact — only the runner's clearing event proves
  consumption.

Subcommands::

    start <case.json> [run flags]    # detached: return immediately, do not wait
    wait  <run_dir> [--timeout S]    # poll to a real run_end, then finalize
    case  <case.json> [run flags]    # start + wait + finalize (foreground)
    run   "<target>" [run flags]     # ad-hoc Case from a bare target
    dry-run [--output-dir D]         # offline synthetic smoke (unique new dir)
    monitor <run_dir> [--follow]     # live state from real events
    hitl <run_dir> <answer>          # answer the current pending HITL prompt
    stop <run_dir>                   # append a stop request
    analyze <run_dir>                # re-derive summary.json from artifacts
    report  <run_dir|summary.json>   # re-render report.html from a saved summary
    status  <run_dir>                # print status.json

Deleted from the previous skill: ``--share`` (regex redaction is not a security
boundary), ``--reset-app`` (destructive ``pm clear``), ``--nudge-steps`` (the
runtime nudge was removed), and the v1 flags. The runner's process exit code is
**never** treated as Case or harness success.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Sibling modules import by bare name (as the tests do).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from sourcemap import resolve_repo_root  # noqa: E402

ROOT = resolve_repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze import build_summary  # noqa: E402
from case import Case, case_from_target, load_case, synthetic_case  # noqa: E402
from evidence import EvidenceView, read_evidence, read_evidence_with_issues  # noqa: E402
from events import RunnerEventsView, read_json_object  # noqa: E402
from report import render_html  # noqa: E402
from synthetic import write_synthetic_run  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "live-diagnosis"

_ARTIFACT_GLOBS = ("summary.json", "report.html", "evidence.jsonl", "status.json", "case.json", "launch.json", "*.evidence.jsonl")

# Terminal harness states that mean "the run ended" (real run_end event).
_TERMINAL_STATES = {
    "succeeded",
    "takeover",
    "stopped",
    "token_budget_exhausted",
    "loop_fuse",
    "error",
    "failed",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def slugify(value: str) -> str:
    import re

    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", (value or "").strip()).strip("-")
    return text[:60] or uuid.uuid4().hex[:8]


def build_run_id(target: str) -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + slugify(target)[:30] + "-" + uuid.uuid4().hex[:6]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    _chmod_600(path)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _chmod_600(path: Path) -> None:
    _chmod(path, 0o600)


# ---------------------------------------------------------------------------
# config + spec (reuse the formal IPC/config stack)
# ---------------------------------------------------------------------------
def _overrides_from_args(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    """Map run flags to V2Config fields; unset flags are dropped (None)."""

    overrides: dict[str, Any] = {
        "device_id": args.device_id,
        "max_model_calls": args.max_steps,
        "base_url": args.base_url,
        "model_name": args.model,
        "api_key": args.apikey,
        "model_timeout": args.model_timeout,
        "model_max_retries": args.model_max_retries,
        "grounding_provider": args.grounding_provider,
        "accessibility_timeout": args.accessibility_timeout,
        "accessibility_max_marks": args.accessibility_max_marks,
        "locateanything_model": args.locateanything_model,
        "locateanything_max_size": args.locateanything_max_size,
        "lang": args.lang,
        "token_budget": args.token_budget,
        # Diagnosis evidence is enabled through the resolved config (spec
        # overrides only) — the production default stays OFF.
        "diagnostic_evidence": True,
        "diagnostic_evidence_dir": str(run_dir),
        "diagnostic_unredacted": True,
        "trace_dir": str(run_dir / "traces"),
    }
    if args.no_taskdoc:
        overrides["taskdoc_enabled"] = False
    return {
        key: value
        for key, value in overrides.items()
        if value is not None or key.startswith("diagnostic_") or key == "trace_dir"
    }


def _load_project_env() -> None:
    """Load project ``.env`` defaults (shell env wins). Real runs only.

    ``V2Config.from_env`` itself does not call this; the formal CLI loads the
    project env first. We keep the call behind a small wrapper so offline
    commands never touch local config and tests can patch it.
    """

    from phone_agent.v2.config import load_project_env

    load_project_env()


def _resolve_config(args: argparse.Namespace, run_dir: Path) -> Any:
    """Real-run config resolution: project ``.env`` -> env -> CLI overrides.

    This is only used on the real launch path. Offline commands (`dry-run`,
    `analyze`, `report`, `monitor`) never call it.
    """

    from phone_agent.v2.config import V2Config

    _load_project_env()
    config = V2Config.from_env(_overrides_from_args(args, run_dir))
    if getattr(args, "device_id", None) is not None and not str(args.device_id).strip():
        config.device_id = None
    return config


def _build_spec(config: Any, run_id: str, task: str, run_dir: Path) -> dict[str, Any]:
    """Build a RunSpec through the formal IPC helpers (fingerprint-safe)."""

    from phone_agent.v2.run_ipc import (
        RunPaths,
        RunSpec,
        app_kb_generation,
        capability_snapshot,
        config_fingerprint,
        resolved_config_dict,
        write_run_spec,
    )

    paths = RunPaths.for_run(run_dir.parent, run_id)
    config_values = resolved_config_dict(config)
    snapshot = {
        "config_fingerprint": config_fingerprint(config_values),
        "memory_generation": app_kb_generation(config),
        "capabilities": capability_snapshot(config),
        "ts": time.time(),
    }
    spec = RunSpec(
        run_id=run_id,
        task=task,
        overrides=config_values,
        snapshot=snapshot,
        events_path=str(paths.events.resolve()),
        control_path=str(paths.control.resolve()),
    )
    write_run_spec(paths.spec, spec)
    return {"spec_path": str(paths.spec), "paths": paths}


# ---------------------------------------------------------------------------
# launching (detached worker; monkeypatchable in tests)
# ---------------------------------------------------------------------------
def _runner_command(spec_path: Path) -> list[str]:
    """The formal runner command. Overridable for an offline fake worker."""

    return [sys.executable, "-m", "phone_agent.runner", str(spec_path)]


def _spawn_runner(spec_path: Path, run_dir: Path) -> dict[str, Any]:
    """Launch the formal runner as a detached process; return its pid.

    The runtime owns ``runner.pid`` (it writes it at start and removes it on
    terminal), so the launcher does **not** write it — that avoids a stale-PID
    race when a very fast runner already exited. Kept narrow so tests can
    replace it with a fake.
    """

    log_path = run_dir / "runner.log"
    log = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _runner_command(spec_path),
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log.close()
    _chmod_600(log_path)
    return {"pid": process.pid, "process": process}


# ---------------------------------------------------------------------------
# outcome + finalization
# ---------------------------------------------------------------------------
class EvidenceAmbiguity(RuntimeError):
    """Multiple producer evidence streams exist and none was named explicitly."""


def _find_evidence(
    run_dir: Path, run_id: str | None = None, explicit: Path | None = None
) -> Path | None:
    """Resolve the diagnostic stream to read.

    An explicitly supplied path wins exactly. Otherwise the producer
    ``<run_id>.evidence.jsonl`` is preferred; if several producers exist and no
    ``run_id`` disambiguates them, that is an error (never a blind first pick).
    ``evidence.jsonl`` is only a derived copy and is used only when no producer
    exists.
    """

    if explicit is not None:
        return explicit
    if run_id:
        producer = run_dir / f"{run_id}.evidence.jsonl"
        if producer.exists():
            return producer
    producers = sorted(run_dir.glob("*.evidence.jsonl"))
    if len(producers) > 1:
        names = ", ".join(p.name for p in producers)
        raise EvidenceAmbiguity(f"multiple producer evidence streams: {names}")
    if len(producers) == 1:
        return producers[0]
    stable = run_dir / "evidence.jsonl"
    if stable.exists():
        return stable
    return None


def _pid_alive(run_dir: Path) -> bool | None:
    """Process liveness, falling back to the launcher PID during startup.

    The runtime owns ``runner.pid`` but writes it a moment after exec; if the
    child dies before that, the persisted ``launch.json`` pid is the only
    liveness anchor. Returns ``None`` only when neither pid is available.
    """

    try:
        from phone_agent.v2.run_ipc import pid_is_alive, read_pid

        pid = read_pid(run_dir / "runner.pid")
        if pid is None:
            launch = read_json_object(run_dir / "launch.json") or {}
            try:
                pid = int(launch.get("pid"))
            except (TypeError, ValueError):
                pid = None
        if pid is None:
            return None
        return pid_is_alive(pid)
    except Exception:  # noqa: BLE001 - liveness unknown -> None (never faked)
        return None


def _load_case_for_run(run_dir: Path) -> Case | None:
    payload = read_json_object(run_dir / "case.json")
    if not payload:
        return None
    try:
        return Case.from_dict(payload)
    except Exception:  # noqa: BLE001 - a corrupt case.json must not gate the report
        return None


def _duration_from_events(events: RunnerEventsView) -> float | None:
    start, end = events.event_time_span()
    if start is None or end is None:
        return None
    return round(max(0.0, end - start), 2)


def _status_from_harness(summary: dict[str, Any]) -> dict[str, Any]:
    """Honest status: derived from the harness terminal, not a process exit code."""

    harness = summary.get("harness_terminal") or {}
    state = str(harness.get("state") or "unknown")
    if state == "succeeded":
        state_class = "completed"
    elif state in _TERMINAL_STATES:
        state_class = "failed"
    elif state == "stopping":
        state_class = "stopping"
    elif state == "running":
        state_class = "running"
    else:
        state_class = "incomplete"
    return {
        "state": state_class,
        "harness_state": state,
        "run_end_seen": bool(harness.get("run_end_seen")),
        "run_summary_present": bool(harness.get("run_summary_present")),
        "stop_requested": bool(harness.get("stop_requested")),
        "process_alive": harness.get("process_alive"),
        "status_source": "derived_cache",
    }


def live_status(run_dir: Path) -> dict[str, Any]:
    """Re-derive status from the current IPC files (not the cached status.json).

    Shared by ``status`` and ``monitor`` so an ended run never keeps reporting
    ``running`` from a stale startup snapshot.
    """

    events = RunnerEventsView.load(run_dir)
    alive = _pid_alive(run_dir)
    terminal = events.harness_terminal()
    state = terminal["state"]
    if alive is False and state in {"running", "stopping"}:
        state = "unknown_terminated"
    harness = {**terminal, "state": state, "process_alive": alive}
    status = _status_from_harness({"harness_terminal": harness})
    status["status_source"] = "live_events"
    status["run_id"] = (events.run_json or {}).get("run_id") or run_dir.name
    status["pid"] = _read_pid_safe(run_dir)
    status["steps"] = terminal.get("steps")
    status["last_event"] = events.events[-1].get("event") if events.events else None
    hitl = events.hitl_state()
    status["unresolved_hitl"] = hitl["unresolved_prompts"]
    status["hitl_submitted"] = hitl["submitted_count"]
    status["hitl_consumed"] = hitl["consumed_count"]
    return status


def _read_pid_safe(run_dir: Path) -> int | None:
    try:
        from phone_agent.v2.run_ipc import read_pid

        return read_pid(run_dir / "runner.pid")
    except Exception:  # noqa: BLE001
        return None


def _exit_code_from_summary(summary: dict[str, Any]) -> int:
    """Command exit-code convention (never conflates process exit 0 with success).

    * 0 harness succeeded
    * 2 harness ended non-success (failed / takeover / stopped / budget / fuse / error)
    * 4 process died without a run_end (unknown_terminated)
    * 5 still running / stopping (no terminal)
    """

    state = str((summary.get("harness_terminal") or {}).get("state") or "unknown")
    if state == "succeeded":
        return 0
    if state in _TERMINAL_STATES:
        return 2
    if state == "unknown_terminated":
        return 4
    return 5


def finalize(
    *,
    run_dir: Path,
    run_id: str,
    case: Case | None,
    target: str,
    command: list[str] | None = None,
    duration_sec: float | None = None,
    notes: list[str] | None = None,
    evidence_path_override: Path | None = None,
    process_alive: bool | None = None,
) -> dict[str, Any]:
    """Analyze the run's two evidence planes into summary + report + status.

    Re-analysis preserves previously saved run metadata (``created_at``,
    ``command``) and recomputes ``duration_sec`` from event timestamps whenever
    they exist, so an intermediate analyze does not freeze the final duration.
    """

    events = RunnerEventsView.load(run_dir)
    if case is not None:
        write_json(run_dir / "case.json", case.to_dict())
    else:
        case = _load_case_for_run(run_dir)
    target = target or (case.goal if case else "")

    evidence_path = _find_evidence(run_dir, run_id, explicit=evidence_path_override)
    evidence_events: list[dict[str, Any]] = []
    diagnostic_issues: list[dict[str, Any]] = []
    if evidence_path is not None:
        evidence_events, diagnostic_issues = read_evidence_with_issues(evidence_path)
        # Maintain the stable copy as a derived artifact (never the source).
        if evidence_path.name != "evidence.jsonl":
            try:
                (run_dir / "evidence.jsonl").write_text(
                    evidence_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
                _chmod_600(run_dir / "evidence.jsonl")
            except OSError:
                pass
    view = EvidenceView.from_events(evidence_events)

    previous = read_json_object(run_dir / "summary.json") or {}
    launch = read_json_object(run_dir / "launch.json") or {}
    created_at = previous.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S")
    # Recompute duration from events whenever a span exists (an earlier analyze
    # must not freeze a shorter duration).
    event_duration = _duration_from_events(events)
    if event_duration is not None:
        duration_sec = event_duration
    elif duration_sec is None:
        duration_sec = previous.get("duration_sec")
    # The persisted launch descriptor is the real startup command; never let a
    # re-analysis ('wait'/'analyze') command overwrite it.
    effective_command = (
        launch.get("command")
        or previous.get("command")
        or command
        or []
    )
    if process_alive is None:
        process_alive = _pid_alive(run_dir)

    summary = build_summary(
        {},
        view,
        run_id=run_id,
        created_at=created_at,
        target=target,
        case=case,
        events=events,
        run_dir=str(run_dir),
        command=effective_command,
        duration_sec=duration_sec,
        evidence_stream=str(run_dir / "evidence.jsonl"),
        trace=str(run_dir / "traces"),
        artifacts={
            "summary": str(run_dir / "summary.json"),
            "report": str(run_dir / "report.html"),
            "evidence": str(run_dir / "evidence.jsonl"),
        },
        process_alive=process_alive,
        extra_data_issues=diagnostic_issues,
    )
    for note in previous.get("notes", []) or []:
        summary.setdefault("notes", []).append(note)
    for note in notes or []:
        summary.setdefault("notes", []).append(note)
    write_json(run_dir / "summary.json", summary)
    (run_dir / "report.html").write_text(
        render_html(summary, evidence_events), encoding="utf-8"
    )
    _chmod_600(run_dir / "report.html")

    status = _status_from_harness(summary)
    status.update(
        {
            "run_id": run_id,
            "verdict": summary["verdict"],
            "case_acceptance": (summary.get("case_acceptance") or {}).get("overall"),
            "finished": (summary.get("harness_terminal") or {}).get("finished"),
            "steps": summary.get("steps"),
            "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "report_path": str(run_dir / "report.html"),
            "summary_path": str(run_dir / "summary.json"),
            "evidence_path": str(run_dir / "evidence.jsonl"),
        }
    )
    write_json(run_dir / "status.json", status)
    _lock_down(run_dir)
    return summary


def _lock_down(run_dir: Path) -> None:
    _chmod(run_dir, 0o700)
    for pattern in _ARTIFACT_GLOBS:
        for path in run_dir.glob(pattern):
            if path.is_file():
                _chmod_600(path)
    for sub in ("screenshots", "traces"):
        directory = run_dir / sub
        if directory.is_dir():
            for path in directory.glob("*"):
                if path.is_file():
                    _chmod_600(path)
    for name in ("runner.log", "runner.pid"):
        if (run_dir / name).exists():
            _chmod_600(run_dir / name)


# ---------------------------------------------------------------------------
# run commands
# ---------------------------------------------------------------------------
def _run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device-id", default=None, help="ADB device serial (blank = auto)")
    parser.add_argument("--max-steps", type=int, default=None, help="max model calls (loop fuse)")
    parser.add_argument("--token-budget", type=int, default=None, help="token cost ceiling")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--apikey", default=None)
    parser.add_argument("--model-timeout", type=float, default=None)
    parser.add_argument("--model-max-retries", type=int, default=None)
    parser.add_argument("--grounding-provider", default=None)
    parser.add_argument("--accessibility-timeout", type=float, default=None)
    parser.add_argument("--accessibility-max-marks", type=int, default=None)
    parser.add_argument("--locateanything-model", default=None)
    parser.add_argument("--locateanything-max-size", type=int, default=None)
    parser.add_argument("--lang", choices=["cn", "en"], default=None)
    parser.add_argument("--no-taskdoc", action="store_true", help="disable the TaskDoc board")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--quiet", action="store_true")


def _prepare_case(args: argparse.Namespace) -> Case:
    if getattr(args, "case", None):
        return load_case(args.case)
    target = getattr(args, "target", None)
    if not target:
        raise ValueError("provide a case file or a target")
    return case_from_target(target)


def _launch_case(args: argparse.Namespace, *, command: list[str]) -> dict[str, Any]:
    """Persist case+spec+launch metadata and spawn the runner detached."""

    case = _prepare_case(args)
    run_id = build_run_id(case.id or case.goal)
    run_dir = Path(args.output_dir).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _chmod(run_dir, 0o700)
    # Persist the Case, spec and launch metadata BEFORE launching, so an
    # interruption still leaves an analyzable run dir with its real command.
    write_json(run_dir / "case.json", case.to_dict())
    write_json(
        run_dir / "launch.json",
        {"run_id": run_id, "task": case.goal, "case_id": case.id, "command": command},
    )

    config = _resolve_config(args, run_dir)
    built = _build_spec(config, run_id, case.task_text(), run_dir)
    try:
        spawned = _spawn_runner(Path(built["spec_path"]), run_dir)
    except Exception as exc:  # noqa: BLE001 - preserve case/spec + record the failure
        write_json(
            run_dir / "status.json",
            {
                "state": "error",
                "run_id": run_id,
                "phase": "spawn",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "status_source": "derived_cache",
            },
        )
        raise
    launch = read_json_object(run_dir / "launch.json") or {}
    launch["pid"] = spawned.get("pid")
    write_json(run_dir / "launch.json", launch)
    events = RunnerEventsView.load(run_dir)
    terminal = events.harness_terminal()
    write_json(
        run_dir / "status.json",
        {
            **_status_from_harness({"harness_terminal": terminal}),
            "run_id": run_id,
            "pid": spawned.get("pid"),
            "report_path": str(run_dir / "report.html"),
            "summary_path": str(run_dir / "summary.json"),
        },
    )
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "pid": spawned.get("pid"),
        "task": case.goal,
        "case_id": case.id,
        "initial_state": terminal.get("state"),
        "report_finalize": "run `wait`/`analyze` after run_end to produce summary.json + report.html",
        # Internal handle for the foreground caller to poll/reap; never JSON'd.
        "_process": spawned.get("process"),
    }


def _launch_command(args: argparse.Namespace, sub: str) -> list[str]:
    """The real startup descriptor (no keys/headers, not full sensitive argv).

    ``sub`` is the actual subcommand used (``start`` / ``case`` / ``run``), so a
    ``start`` invocation is never mislabelled as ``case``.
    """

    case_arg = getattr(args, "case", None) or getattr(args, "target", "")
    return ["run_diagnosis.py", sub, case_arg]


def cmd_start(args: argparse.Namespace) -> int:
    try:
        info = _launch_case(args, command=_launch_command(args, "start"))
    except Exception as exc:  # noqa: BLE001 - clean, no side-effect error
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    process = info.pop("_process", None)
    if process is not None:
        # Detached path: reap the child when it exits so it never lingers as a
        # zombie whose pid still answers os.kill(pid, 0). The daemon thread does
        # not block the CLI from returning.
        import threading

        threading.Thread(target=process.wait, daemon=True).start()
    if not args.quiet:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _wait_loop(run_dir: Path, timeout: float, interval: float, process: Any | None = None) -> str:
    """Poll to a real run_end. Returns ``done`` / ``timeout`` / ``dead``.

    A foreground caller passes its ``Popen`` handle so a child that exited
    before writing ``runner.pid`` is detected immediately (and reaped) rather
    than leaving a zombie that ``os.kill(pid, 0)`` still reports as alive.
    """

    def _exited() -> bool:
        if process is not None and process.poll() is not None:
            return True
        return _pid_alive(run_dir) is False

    deadline = time.monotonic() + float(timeout)
    if _exited() and not RunnerEventsView.load(run_dir).has_run_end:
        return "dead"
    while True:
        events = RunnerEventsView.load(run_dir)
        if events.has_run_end:
            return "done"
        if _exited():
            return "dead"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(interval)


def cmd_wait(args: argparse.Namespace) -> int:
    run_dir = Path(args.path)
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    outcome = _wait_loop(run_dir, args.timeout, args.interval)
    # No command descriptor: finalize must keep the persisted launch.command.
    summary = _finalize_dir(run_dir, quiet=args.quiet)
    if outcome == "timeout":
        print(json.dumps({"run_id": run_dir.name, "state": "timeout", "run_end_seen": False}, ensure_ascii=False))
        return 3
    if outcome == "dead":
        print(json.dumps({"run_id": run_dir.name, "state": "unknown_terminated", "run_end_seen": False}, ensure_ascii=False))
        return 4
    return _exit_code_from_summary(summary)


def cmd_case(args: argparse.Namespace) -> int:
    """start + wait + finalize (foreground)."""
    sub = "run" if getattr(args, "target", None) else "case"
    try:
        info = _launch_case(args, command=_launch_command(args, sub))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    run_dir = Path(info["run_dir"])
    process = info.get("_process")
    if not getattr(args, "quiet", False):
        # Flush launch info immediately so another terminal can answer HITL.
        print(json.dumps({k: v for k, v in info.items() if k != "_process"}, ensure_ascii=False), flush=True)
    outcome = _wait_loop(run_dir, getattr(args, "timeout", 1800.0), 0.5, process=process)
    if process is not None:
        try:
            process.poll()  # reap if already exited (avoid a zombie)
        except Exception:  # noqa: BLE001
            pass
    summary = _finalize_dir(run_dir, quiet=True)
    if not getattr(args, "quiet", False):
        print(json.dumps(_payload(info["run_id"], run_dir, summary), ensure_ascii=False, indent=2))
    if outcome == "timeout":
        return 3
    if outcome == "dead":
        return 4
    return _exit_code_from_summary(summary)


def _finalize_dir(
    run_dir: Path,
    *,
    quiet: bool,
    command: list[str] | None = None,
    evidence_path_override: Path | None = None,
) -> dict[str, Any]:
    events = RunnerEventsView.load(run_dir)
    case = _load_case_for_run(run_dir)
    launch = read_json_object(run_dir / "launch.json") or {}
    target = launch.get("task") or ((events.spec or {}).get("task") if isinstance(events.spec, dict) else None)
    if not target and case:
        target = case.goal
    summary = finalize(
        run_dir=run_dir,
        run_id=(launch.get("run_id") or run_dir.name),
        case=case,
        target=target or "",
        command=command,
        evidence_path_override=evidence_path_override,
    )
    if not quiet:
        print(json.dumps(_payload(summary["run_id"], run_dir, summary), ensure_ascii=False, indent=2))
    return summary


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Offline synthetic smoke: real analyze + report over synthetic IPC files.

    Always uses the built-in synthetic Case and a **unique new** run directory;
    it never deletes existing data and never reads local config or global memory.
    """

    case = synthetic_case()
    run_id = "synthetic-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = Path(args.output_dir).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _chmod(run_dir, 0o700)
    write_json(run_dir / "case.json", case.to_dict())
    write_synthetic_run(run_dir, run_id=run_id)
    summary = finalize(
        run_dir=run_dir,
        run_id=run_id,
        case=case,
        target=case.goal,
        command=["run_diagnosis.py", "dry-run"],
        duration_sec=0.0,
        notes=["dry-run：合成事件 + 合成截图，仅验证 analyze→report 管线；不代表真机能力。"],
    )
    if not args.quiet:
        print(json.dumps(_payload(run_id, run_dir, summary), ensure_ascii=False, indent=2))
    return 0


def _payload(run_id: str, run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "verdict": summary.get("verdict"),
        "harness_state": (summary.get("harness_terminal") or {}).get("state"),
        "case_acceptance": (summary.get("case_acceptance") or {}).get("overall"),
        "steps": summary.get("steps"),
        "run_dir": str(run_dir),
        "report_path": str(run_dir / "report.html"),
        "summary_path": str(run_dir / "summary.json"),
        "top_recommendations": [r.get("title") for r in (summary.get("recommendations") or [])[:3]],
    }


# ---------------------------------------------------------------------------
# re-analysis / report / status / monitor / control
# ---------------------------------------------------------------------------
def cmd_analyze(args: argparse.Namespace) -> int:
    raw = Path(args.path)
    explicit: Path | None = None
    if raw.is_file():
        run_dir = raw.parent
        # An explicitly named evidence file is used exactly; do not let
        # _find_evidence pick a different stream.
        if raw.name == "evidence.jsonl" or raw.name.endswith(".evidence.jsonl"):
            explicit = raw
    elif raw.is_dir():
        run_dir = raw
    else:
        print(json.dumps({"error": f"path not found: {raw}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        _finalize_dir(run_dir, quiet=args.quiet, evidence_path_override=explicit)
    except EvidenceAmbiguity as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Render-only: re-render report.html from a saved summary without re-analyzing."""

    raw = Path(args.path)
    if raw.is_dir():
        summary_path = raw / "summary.json"
        run_dir = raw
    elif raw.is_file():
        summary_path = raw
        run_dir = raw.parent
    else:
        print(json.dumps({"error": f"path not found: {raw}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    summary = read_json_object(summary_path)
    if summary is None:
        print(json.dumps({"error": f"summary.json not found or invalid: {summary_path}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    evidence_path = None
    if args.evidence:
        evidence_path = Path(args.evidence)
        if not evidence_path.exists():
            print(json.dumps({"error": f"evidence file not found: {evidence_path}"}, ensure_ascii=False), file=sys.stderr)
            return 1
    else:
        run_id = str(summary.get("run_id") or run_dir.name)
        try:
            evidence_path = _find_evidence(run_dir, run_id)
        except EvidenceAmbiguity as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    events = read_evidence(evidence_path) if evidence_path else []
    out = Path(args.output) if args.output else run_dir / "report.html"
    out.write_text(render_html(summary, events), encoding="utf-8")
    _chmod_600(out)
    print(json.dumps({"report_path": str(out), "summary_path": str(summary_path)}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print live status re-derived from IPC (not the cached startup snapshot)."""

    run_dir = Path(args.path)
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(live_status(run_dir), ensure_ascii=False, indent=2))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Print live state derived from real events (never from intent)."""

    run_dir = Path(args.path)
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        while True:
            payload = live_status(run_dir)
            print(json.dumps(payload, ensure_ascii=False))
            if not args.follow or payload["run_end_seen"]:
                return 0
            if payload.get("process_alive") is False:
                # Dead process without a terminal event: stop polling.
                return 4
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


def cmd_hitl(args: argparse.Namespace) -> int:
    from phone_agent.v2.run_ipc import append_control

    run_dir = Path(args.path)
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    answer = str(args.answer or "").strip()
    if not answer:
        print(json.dumps({"error": "empty answer refused"}, ensure_ascii=False), file=sys.stderr)
        return 1
    events = RunnerEventsView.load(run_dir)
    if events.has_run_end:
        print(json.dumps({"error": "run already terminal; refusing to queue a HITL answer"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if _pid_alive(run_dir) is not True:
        print(json.dumps({"error": "runner process is not confirmed alive; refusing to queue a HITL answer"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if events.has_run_summary and not events.has_run_end:
        print(json.dumps({"error": "run has a summary but no terminal event (unknown_terminated); refusing to queue a HITL answer"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if events.stop_requested:
        print(json.dumps({"error": "a stop was requested; refusing to queue a HITL answer"}, ensure_ascii=False), file=sys.stderr)
        return 1
    hitl = events.hitl_state()
    if hitl["unconsumed_count"] > 0:
        print(json.dumps({"error": "a previous HITL answer is submitted but not yet consumed; wait for the runner to clear it"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if not hitl["unresolved_prompts"]:
        print(json.dumps({"error": "no pending HITL prompt; refusing to pre-queue an answer"}, ensure_ascii=False), file=sys.stderr)
        return 1
    append_control(run_dir / "control.jsonl", {"type": "hitl", "answer": answer})
    print(json.dumps({"appended": "hitl", "run_dir": str(run_dir), "prompt": hitl["unresolved_prompts"][-1]}))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    from phone_agent.v2.run_ipc import append_control

    run_dir = Path(args.path)
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    events = RunnerEventsView.load(run_dir)
    if events.has_run_end:
        print(json.dumps({"error": "run already terminal; nothing to stop"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if _pid_alive(run_dir) is False:
        print(json.dumps({"error": "runner process is not alive; nothing to stop"}, ensure_ascii=False), file=sys.stderr)
        return 1
    append_control(run_dir / "control.jsonl", {"type": "stop"})
    print(json.dumps({"appended": "stop", "note": "请求停止 ≠ 已结束；用 monitor 观察 run_end。"}))
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_diagnosis.py",
        description="TaskWizard thin-loop (v2) runner-backed live diagnosis",
        epilog="Subcommands: start, wait, case, run, dry-run, monitor, hitl, stop, analyze, report, status",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    start_p = sub.add_parser("start", help="run a Case detached; return immediately")
    start_p.add_argument("case", help="path to a Case JSON file")
    _run_flags(start_p)

    wait_p = sub.add_parser("wait", help="poll a run to a real run_end, then finalize")
    wait_p.add_argument("path")
    wait_p.add_argument("--timeout", type=float, default=1800.0)
    wait_p.add_argument("--interval", type=float, default=0.5)
    wait_p.add_argument("--quiet", action="store_true")

    case_p = sub.add_parser("case", help="run a Case foreground (start+wait+finalize)")
    case_p.add_argument("case", help="path to a Case JSON file")
    case_p.add_argument("--timeout", type=float, default=1800.0)
    _run_flags(case_p)

    run_p = sub.add_parser("run", help="run an ad-hoc target foreground")
    run_p.add_argument("target")
    run_p.add_argument("--timeout", type=float, default=1800.0)
    _run_flags(run_p)

    dry_p = sub.add_parser("dry-run", help="offline synthetic smoke (unique new dir)")
    dry_p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    dry_p.add_argument("--quiet", action="store_true")

    monitor_p = sub.add_parser("monitor", help="print live state from real events")
    monitor_p.add_argument("path")
    monitor_p.add_argument("--follow", action="store_true")
    monitor_p.add_argument("--interval", type=float, default=1.0)

    hitl_p = sub.add_parser("hitl", help="answer the current pending HITL prompt")
    hitl_p.add_argument("path")
    hitl_p.add_argument("answer")

    stop_p = sub.add_parser("stop", help="append a stop request")
    stop_p.add_argument("path")

    analyze_p = sub.add_parser("analyze", help="re-derive summary.json from artifacts")
    analyze_p.add_argument("path")
    analyze_p.add_argument("--quiet", action="store_true")

    report_p = sub.add_parser("report", help="re-render report.html from a saved summary")
    report_p.add_argument("path")
    report_p.add_argument("--evidence", default=None, help="explicit evidence JSONL for the raw tab")
    report_p.add_argument("--output", default=None, help="report.html output path")

    status_p = sub.add_parser("status", help="print status.json")
    status_p.add_argument("path")
    return parser


_SUBCOMMANDS = {
    "start": cmd_start,
    "wait": cmd_wait,
    "case": cmd_case,
    "run": cmd_case,
    "dry-run": cmd_dry_run,
    "monitor": cmd_monitor,
    "hitl": cmd_hitl,
    "stop": cmd_stop,
    "analyze": cmd_analyze,
    "report": cmd_report,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not argv:
        parser.print_help()
        return 2
    if argv[0] in {"-h", "--help", "help"}:
        parser.print_help()
        return 0
    if argv[0] in _SUBCOMMANDS:
        args = parser.parse_args(argv)
        return _SUBCOMMANDS[args.command](args)
    if argv[0].startswith("-"):
        parser.error(f"unrecognized argument: {argv[0]}")
        return 2  # unreachable; parser.error exits
    # Backward-compatible bare target -> ad-hoc foreground run.
    run_p = argparse.ArgumentParser(prog="run_diagnosis.py run")
    run_p.add_argument("target")
    run_p.add_argument("--timeout", type=float, default=1800.0)
    _run_flags(run_p)
    return cmd_case(run_p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
