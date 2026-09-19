"""Read the runner IPC event stream and derive *real* run state.

The live-diagnosis driver drives the formal runner
(``python -m phone_agent.runner <spec.json>``). The runner publishes three
append-only / atomically-replaced files under ``<runs_dir>/<run_id>/``
(:mod:`phone_agent.v2.run_ipc`):

* ``events.jsonl`` — :class:`~phone_agent.v2.run_events.WebEventMiddleware`
  output: ``model_request`` / ``model_call`` / ``tool_call`` / ``tool_result`` /
  ``screen`` / ``taskdoc_snapshot`` / ``pending_hitl`` / ``safety_warning`` /
  ``stopping`` / ``memory_generation_drift`` / ``capability_snapshot`` /
  ``model_stream_start`` / ``model_stream_delta`` / ``model_stream_end``
  (observe-only streaming projection, P0 #22) / ``run_end``;
* ``control.jsonl`` — the human/stop channel the driver appends to
  (``{"type": "stop"}`` / ``{"type": "hitl", "answer": ...}``);
* ``run.json`` — the atomic terminal *summary* (a recorded artifact).

**State is derived from these real events, never from intent.** In particular:

* the **``run_end`` event is the sole terminal authority**. A ``run.json`` is a
  recorded summary and is surfaced separately; its mere presence is not a
  terminal event and is never reported as ``run_end_seen``;
* a requested stop is not an ended run — ``stop_requested`` is separate from the
  terminal state;
* a control HITL answer only records that a human *submitted* a reply; only the
  runner's ``pending_hitl`` clearing event proves it was *consumed*. An
  unanswered prompt is surfaced as unresolved and never turned into approval.

Malformed / partially-flushed lines are skipped and reported as bounded
``parse_issues`` (the last torn line is dropped, never repaired).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from case import STOP_TAKEOVER_REASON

# Terminal status vocabulary the runner writes (``run_events.terminal_status``).
TERMINAL_SUCCEEDED = "succeeded"
TERMINAL_BUDGET = "budget_exhausted"
TERMINAL_LOOP_FUSE = "loop_fuse"
TERMINAL_TAKEOVER = "takeover"
TERMINAL_ERROR = "error"
TERMINAL_FAILED = "failed"

# Derived states that are *not* a terminal event.
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_UNKNOWN_TERMINATED = "unknown_terminated"
STATE_UNKNOWN = "unknown"

_MAX_ISSUES = 20


def read_jsonl_with_issues(
    path: str | Path | None, *, source: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a JSONL file into ``(rows, issues)``.

    A process can die mid-write; the final partial line is not valid JSON and is
    skipped, but the fact that a line was skipped (or was valid JSON yet not an
    object) is reported as a bounded issue rather than silently swallowed. Never
    raises for a missing file.
    """

    issues: list[dict[str, Any]] = []
    if not path:
        return [], issues
    p = Path(path)
    label = source or p.name
    if not p.exists():
        return [], issues
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        issues.append({"source": label, "line": None, "reason": f"read_error:{type(exc).__name__}"})
        return [], issues
    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if len(issues) < _MAX_ISSUES:
                issues.append({"source": label, "line": lineno, "reason": "invalid_json"})
            continue
        if not isinstance(payload, dict):
            if len(issues) < _MAX_ISSUES:
                issues.append({"source": label, "line": lineno, "reason": "not_an_object"})
            continue
        rows.append(payload)
    return rows, issues


def read_jsonl(path: str | Path | None) -> list[dict[str, Any]]:
    """Read a JSONL file, returning only the valid object rows."""

    rows, _ = read_jsonl_with_issues(path)
    return rows


def read_json_object(path: str | Path | None) -> dict[str, Any] | None:
    """Read a JSON object, returning ``None`` for missing/invalid content."""

    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class RunnerEventsView:
    """Typed index over the runner IPC stream for one run."""

    run_dir: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    control: list[dict[str, Any]] = field(default_factory=list)
    run_json: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    run_start: dict[str, Any] | None = None
    run_end: dict[str, Any] | None = None
    model_requests: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    screens: list[dict[str, Any]] = field(default_factory=list)
    taskdoc_snapshots: list[dict[str, Any]] = field(default_factory=list)
    safety_warnings: list[dict[str, Any]] = field(default_factory=list)
    pending_hitl: list[dict[str, Any]] = field(default_factory=list)
    stopping: list[dict[str, Any]] = field(default_factory=list)
    capability_snapshots: list[dict[str, Any]] = field(default_factory=list)
    memory_drifts: list[dict[str, Any]] = field(default_factory=list)
    parse_issues: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Index on construction so a directly-built view (tests) behaves like one
        # loaded from disk.
        self._index()

    def _index(self) -> None:
        self.run_start = None
        self.run_end = None
        self.model_requests = []
        self.model_calls = []
        self.tool_calls = []
        self.tool_results = []
        self.screens = []
        self.taskdoc_snapshots = []
        self.safety_warnings = []
        self.pending_hitl = []
        self.stopping = []
        self.capability_snapshots = []
        self.memory_drifts = []
        for event in self.events:
            kind = event.get("event")
            if kind == "run_start":
                self.run_start = event
            elif kind == "run_end":
                self.run_end = event
            elif kind == "model_request":
                self.model_requests.append(event)
            elif kind == "model_call":
                self.model_calls.append(event)
            elif kind == "tool_call":
                self.tool_calls.append(event)
            elif kind == "tool_result":
                self.tool_results.append(event)
            elif kind == "screen":
                self.screens.append(event)
            elif kind == "taskdoc_snapshot":
                self.taskdoc_snapshots.append(event)
            elif kind == "safety_warning":
                self.safety_warnings.append(event)
            elif kind == "pending_hitl":
                self.pending_hitl.append(event)
            elif kind == "stopping":
                self.stopping.append(event)
            elif kind == "capability_snapshot":
                self.capability_snapshots.append(event)
            elif kind == "memory_generation_drift":
                self.memory_drifts.append(event)

    @classmethod
    def load(cls, run_dir: str | Path) -> "RunnerEventsView":
        base = Path(run_dir)
        event_rows, event_issues = read_jsonl_with_issues(base / "events.jsonl", source="events.jsonl")
        control_rows, control_issues = read_jsonl_with_issues(base / "control.jsonl", source="control.jsonl")
        return cls(
            run_dir=str(base),
            events=event_rows,
            control=control_rows,
            run_json=read_json_object(base / "run.json"),
            spec=read_json_object(base / "spec.json"),
            parse_issues=event_issues + control_issues,
        )

    # -- state -------------------------------------------------------------
    @property
    def config(self) -> dict[str, Any]:
        """The resolved run-spec overrides (effective V2Config values)."""

        if not isinstance(self.spec, dict):
            return {}
        overrides = self.spec.get("overrides")
        return overrides if isinstance(overrides, dict) else {}

    @property
    def stop_requested(self) -> bool:
        """True once a control ``stop`` was appended (not the same as ended)."""

        return any(str(item.get("type")) == "stop" for item in self.control)

    @property
    def has_run_end(self) -> bool:
        """True only when a real ``run_end`` event was observed."""

        return self.run_end is not None

    @property
    def has_run_summary(self) -> bool:
        """True when a recorded ``run.json`` summary exists (not a terminal event)."""

        return self.run_json is not None

    @property
    def has_events(self) -> bool:
        return bool(self.events)

    def hitl_submitted(self) -> list[str]:
        """Answers a human wrote to ``control.jsonl`` (submitted, not consumed)."""

        return [
            str(item.get("answer", ""))
            for item in self.control
            if str(item.get("type")) == "hitl"
        ]

    def hitl_consumed(self) -> int:
        """Number of prompts the runner explicitly cleared (``prompt: null``)."""

        return sum(1 for event in self.pending_hitl if not event.get("prompt"))

    def unresolved_hitl(self) -> list[str]:
        """Prompts raised and never cleared by the runner.

        Only a ``pending_hitl`` with ``prompt: null`` clears a prompt. A control
        answer does **not** clear it here — it only records that a human replied;
        the runner clearing event is the proof of consumption. This never invents
        an answer.
        """

        open_prompts: list[str] = []
        for event in self.pending_hitl:
            prompt = event.get("prompt")
            if prompt:
                open_prompts.append(str(prompt))
            elif open_prompts:
                open_prompts.pop()
        return open_prompts

    def hitl_state(self) -> dict[str, Any]:
        """HITL bookkeeping: submitted vs consumed vs still open."""

        submitted = self.hitl_submitted()
        consumed = self.hitl_consumed()
        unresolved = self.unresolved_hitl()
        return {
            "submitted_count": len(submitted),
            "consumed_count": consumed,
            "unconsumed_count": max(0, len(submitted) - consumed),
            "unresolved_prompts": unresolved,
            "answers": submitted,
        }

    def events_step_count(self) -> int:
        """Highest ``step`` seen on any event (0 when none carry a step)."""

        best = 0
        for event in self.events:
            value = _as_int(event.get("step"))
            if value is not None:
                best = max(best, value)
        return best

    def event_time_span(self) -> tuple[float | None, float | None]:
        """First/last event ``ts`` (for a run duration estimate)."""

        stamps = [
            float(event["ts"])
            for event in self.events
            if isinstance(event.get("ts"), (int, float))
        ]
        if not stamps:
            return None, None
        return min(stamps), max(stamps)

    def run_summary_block(self) -> dict[str, Any] | None:
        """The recorded ``run.json`` summary, shown separately from the terminal."""

        if not self.has_run_summary:
            return None
        payload = self.run_json or {}
        result = payload.get("result")
        return {
            "present": True,
            "status": payload.get("status"),
            "result": result if isinstance(result, dict) else None,
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            "finished_at": payload.get("finished_at"),
            "note": "run.json 是已落盘的摘要产物，不是 run_end 终局事件本身。",
        }

    def harness_terminal(self) -> dict[str, Any]:
        """Derive the harness terminal state from real events only.

        The ``run_end`` event is authoritative. ``run.json`` is a recorded
        summary and never fabricates a terminal event. Returns
        ``{state, status, finished, reason, takeover_reason, stop_requested,
        run_end_seen, run_summary_present, steps, trace_path, tokens_total}``.
        """

        result: dict[str, Any] = {}
        status: str | None = None
        tokens_total: int | None = None
        if self.has_run_end:
            raw = self.run_end.get("result")
            result = raw if isinstance(raw, dict) else {}
            status = str(self.run_end.get("status") or "") or None
            tokens_total = _as_int(self.run_end.get("tokens_total"))

        finished = bool(result.get("success"))
        reason = result.get("reason")
        takeover_reason = None
        if self.has_run_end and not finished and status == TERMINAL_TAKEOVER:
            takeover_reason = str(reason or "takeover")

        # On success ``result.reason`` IS the finish summary; do not also report
        # it as a stop reason.
        finish_summary = str(reason) if finished and reason else None

        state: str
        if not self.has_run_end:
            if self.stop_requested:
                state = STATE_STOPPING
            elif self.has_run_summary:
                state = STATE_UNKNOWN_TERMINATED
            elif self.has_events:
                state = STATE_RUNNING
            else:
                state = STATE_UNKNOWN
        elif finished:
            state = TERMINAL_SUCCEEDED
        elif takeover_reason == STOP_TAKEOVER_REASON:
            state = "stopped"
        elif status == TERMINAL_TAKEOVER:
            state = TERMINAL_TAKEOVER
        elif status == TERMINAL_BUDGET or reason == "token_budget_exhausted":
            state = TERMINAL_BUDGET
        elif status == TERMINAL_LOOP_FUSE or reason == "loop_fuse":
            state = TERMINAL_LOOP_FUSE
        elif status == TERMINAL_ERROR:
            state = TERMINAL_ERROR
        elif status == TERMINAL_FAILED:
            state = TERMINAL_FAILED
        else:
            state = STATE_UNKNOWN

        steps = _as_int(result.get("steps"))
        if steps is None:
            steps = self.events_step_count() or None
        return {
            "state": state,
            "status": status,
            "finished": finished,
            "finish_summary": finish_summary,
            "reason": None if finished else reason,
            "takeover_reason": takeover_reason,
            "stop_requested": self.stop_requested,
            "run_end_seen": self.has_run_end,
            "run_summary_present": self.has_run_summary,
            "steps": steps,
            "trace_path": result.get("trace_path"),
            "tokens_total": tokens_total,
        }

    # -- model / usage -----------------------------------------------------
    def model_identity(self) -> list[dict[str, Any]]:
        """Per-call ``{step, requested_model, actual_model, ...}``.

        ``requested`` comes from the outgoing request binding; ``actual`` only
        from provider-reported metadata. A missing value stays ``None`` — it is
        never back-filled from the other side.
        """

        rows: list[dict[str, Any]] = []
        for event in self.model_calls:
            rows.append(
                {
                    "step": event.get("step"),
                    "requested_model": event.get("requested_model"),
                    "actual_model": event.get("actual_model"),
                    "latency_ms": event.get("latency_ms"),
                    "tokens": event.get("tokens"),
                    "tokens_total": event.get("tokens_total"),
                    "error": event.get("error"),
                }
            )
        return rows

    def usage_totals(self) -> dict[str, Any]:
        """Aggregate reported token counts with explicit per-field coverage.

        Completeness is judged **per call**, not by unioning fields across
        different calls: one call with only input plus another with only output
        is still partial. ``total_tokens`` exists only when every call reported
        both input and output; a partially-reported cache stays ``None`` rather
        than being shown as if complete.
        """

        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        calls = len(self.model_calls)
        sums = {key: 0 for key in keys}
        with_field = {key: 0 for key in keys}
        for event in self.model_calls:
            for key in keys:
                value = _as_int(event.get(key))
                if value is not None:
                    with_field[key] += 1
                    sums[key] += value
        reported = any(count > 0 for count in with_field.values())
        complete = {key: (calls > 0 and with_field[key] == calls) for key in keys}
        input_complete = complete["input_tokens"]
        output_complete = complete["output_tokens"]
        partial = not (input_complete and output_complete)
        return {
            "reported": reported,
            "partial": partial,
            "calls": calls,
            "input_tokens": sums["input_tokens"] if with_field["input_tokens"] else None,
            "output_tokens": sums["output_tokens"] if with_field["output_tokens"] else None,
            "cache_read_tokens": sums["cache_read_tokens"] if complete["cache_read_tokens"] else None,
            "cache_write_tokens": sums["cache_write_tokens"] if complete["cache_write_tokens"] else None,
            "total_tokens": (sums["input_tokens"] + sums["output_tokens"])
            if (input_complete and output_complete)
            else None,
            "coverage": {
                "calls": calls,
                **{f"{key}_calls": with_field[key] for key in keys},
            },
        }

    def tool_result_index(self) -> dict[str, dict[str, Any]]:
        """tool_call_id-less best-effort step->result map for the replay."""

        index: dict[Any, list[dict[str, Any]]] = {}
        for result in self.tool_results:
            index.setdefault(result.get("step"), []).append(result)
        return index


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


__all__ = [
    "TERMINAL_SUCCEEDED",
    "TERMINAL_BUDGET",
    "TERMINAL_LOOP_FUSE",
    "TERMINAL_TAKEOVER",
    "TERMINAL_ERROR",
    "TERMINAL_FAILED",
    "STATE_RUNNING",
    "STATE_STOPPING",
    "STATE_UNKNOWN_TERMINATED",
    "STATE_UNKNOWN",
    "RunnerEventsView",
    "read_jsonl",
    "read_jsonl_with_issues",
    "read_json_object",
]
