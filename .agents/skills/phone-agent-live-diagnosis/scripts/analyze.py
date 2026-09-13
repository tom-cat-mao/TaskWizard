"""Analyze a diagnostic run into the v2 ``summary.json`` structure.

Two evidence planes feed the analysis and are deliberately kept distinct:

* the **runner IPC** stream (``events.jsonl`` / ``control.jsonl`` / ``run.json`` /
  ``spec.json``) is the authority for *what the harness did* — terminal status,
  requested/actual model identity, reported token usage, control/stop, HITL;
* the **diagnostic evidence** stream (``<run_id>.evidence.jsonl``) is the
  authority for the *model-visible replay* — request context hygiene, tool args
  and receipts, TaskDoc snapshots, screenshots on disk.

The summary keeps three judgments apart, never collapsing them:

1. ``harness_terminal`` — harness fact (finished / takeover / stopped / budget /
   fuse / error);
2. ``case_acceptance`` — the Case's own checkpoints, evaluated against recorded
   evidence only (missing evidence -> ``unknown``; ``finished`` never implies
   Case ``pass``);
3. ``diagnosis`` — source-mapped **inferences** for a human to confirm, not
   proven root causes.

A single tool error is a tool-health fact, not a whole-run failure: the run
verdict follows the harness terminal, not any one tool return.

Analysis never touches the device or re-runs a tool. Optional inputs (trace,
memory ledgers, resolver/capability artifacts) are read best-effort and are
fail-open: absent/malformed/partially-written files produce empty blocks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from case import (
    AcceptanceEvidence,
    Case,
    evaluate_acceptance,
    rollup_acceptance,
)
from evidence import EvidenceView, parse_obs_windows, result_text_of
from events import RunnerEventsView
from sourcemap import V2_SOURCE_RULES, add_line_numbers
from taxonomy import (
    classify_result,
    category_of,
)

_OPEN_STATUSES = ("pending", "in_progress")
_RESOLVER_ROUTES = ("exact", "lexical", "pinyin", "embedding")
_RESOLVER_MATCH_TYPES = (
    "exact_alias",
    "exact_label",
    "exact_package",
    "exact_package_segment",
    "registered_containment",
    "token_prefix",
    "containment",
    "fuzzy",
    "pinyin_full",
    "pinyin_initials",
    "embedding",
)
_LAUNCHED_PACKAGE_RE = re.compile(r"\blaunched\s+.+?\s+\(([A-Za-z][A-Za-z0-9_.]+)\)")
_RUN_ID_IN_NOTE_RE = re.compile(r"run<([^<>]+)>")
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (``pct`` in 0..100). Empty -> 0.0."""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(1, int(round(pct / 100.0 * len(ordered))))
    rank = min(rank, len(ordered))
    return float(ordered[rank - 1])


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


# ---------------------------------------------------------------------------
# verdict (harness terminal only — never a single tool error)
# ---------------------------------------------------------------------------
def classify_verdict(
    outcome: dict[str, Any],
    view: EvidenceView,
    harness: dict[str, Any] | None = None,
) -> str:
    """Map the *harness terminal* to a run verdict.

    The run verdict answers "how did the harness end?", not "did every tool
    succeed?" and not "did the Case pass?". A tool error is recorded under
    ``tool_health`` and never flips this verdict on its own.

    Values: ``success | takeover | stopped | budget_exhausted | loop_fuse |
    error | failed | uncertain``. Non-terminal harness states (running /
    stopping / unknown_terminated / unknown) map to ``uncertain`` — the
    diagnostic stream's own ``finished`` is never allowed to promote them.
    """

    if harness:
        state = str(harness.get("state") or "")
        mapping = {
            "succeeded": "success",
            "takeover": "takeover",
            "stopped": "stopped",
            "token_budget_exhausted": "budget_exhausted",
            "loop_fuse": "loop_fuse",
            "error": "error",
            "failed": "failed",
        }
        if state in mapping:
            return mapping[state]
        if harness.get("source") == "runner_ipc":
            # A runner IPC stream exists but shows no terminal event -> not a
            # success, regardless of what the diagnostic stream recorded.
            return "uncertain"

    takeover_reason = str(outcome.get("takeover_reason") or "")
    terminal = (view.run_end or {}).get("terminal", {}) if view.run_end else {}
    if not takeover_reason:
        takeover_reason = str(terminal.get("takeover_reason") or "")
    if takeover_reason:
        from case import STOP_TAKEOVER_REASON

        return "stopped" if takeover_reason == STOP_TAKEOVER_REASON else "takeover"
    if bool(outcome.get("finished")) or bool(terminal.get("finished")):
        return "success"
    reason = str(outcome.get("reason") or terminal.get("reason") or "")
    if reason == "token_budget_exhausted":
        return "budget_exhausted"
    if reason == "loop_fuse":
        return "loop_fuse"
    if reason.startswith("error:"):
        return "error"
    return "uncertain"


# ---------------------------------------------------------------------------
# finish_gate
# ---------------------------------------------------------------------------
def build_finish_gate(view: EvidenceView) -> dict[str, Any]:
    finish_calls = view.finish_calls()
    rejections: list[dict[str, Any]] = []
    accepted = False
    blocked_open = False
    open_items_at_finish: list[str] = []
    for call in finish_calls:
        cls = classify_result(call["result_text"])
        if cls == "finish_ok":
            accepted = True
        elif cls in {"finish_no_evidence", "finish_blocked_open_items"}:
            rejections.append(
                {
                    "step": call.get("step"),
                    "class": cls,
                    "message": call["result_text"],
                }
            )
            if cls == "finish_blocked_open_items":
                blocked_open = True
    # open items at the moment of a blocked finish: use the latest snapshot.
    if blocked_open:
        snap = view.latest_taskdoc()
        if snap:
            open_items_at_finish = [
                f"{it.get('id')}:{it.get('content')}"
                for it in snap.get("items", [])
                if it.get("status") in _OPEN_STATUSES
            ]
    return {
        "attempted": bool(finish_calls),
        "accepted": accepted,
        "blocked_by_open_items": blocked_open,
        "open_items_at_finish": open_items_at_finish,
        "rejections": rejections,
    }


# ---------------------------------------------------------------------------
# taskdoc_final
# ---------------------------------------------------------------------------
def build_taskdoc_final(view: EvidenceView) -> dict[str, Any]:
    snap = view.latest_taskdoc()
    if not snap:
        return {
            "goal_base": None,
            "amendments": [],
            "items": [],
            "facts": [],
            "counts": {
                "total": 0,
                "completed": 0,
                "in_progress": 0,
                "pending": 0,
                "blocked": 0,
            },
            "open_item_count": 0,
            "terminal_state": "no_board",
        }
    items = snap.get("items", []) or []
    counts = {
        "total": len(items),
        "completed": 0,
        "in_progress": 0,
        "pending": 0,
        "blocked": 0,
    }
    for it in items:
        status = it.get("status")
        if status in counts:
            counts[status] += 1
    open_count = counts["pending"] + counts["in_progress"]
    if not items:
        terminal_state = "no_board"
    elif counts["blocked"]:
        terminal_state = "blocked_present"
    elif open_count:
        terminal_state = "has_open"
    else:
        terminal_state = "all_completed"
    return {
        "goal_base": snap.get("goal_base"),
        "amendments": snap.get("amendments", []),
        "items": items,
        "facts": snap.get("facts", []),
        "counts": counts,
        "open_item_count": open_count,
        "terminal_state": terminal_state,
    }


# ---------------------------------------------------------------------------
# harness terminal (runner IPC authority)
# ---------------------------------------------------------------------------
def build_harness_terminal(
    outcome: dict[str, Any],
    events: RunnerEventsView | None,
    process_alive: bool | None = None,
) -> dict[str, Any]:
    """Harness fact layer: how the run ended.

    IPC ``run_end`` is the authority. When an events view is present but carries
    no ``run_end``, the harness state is a non-terminal state (running /
    stopping / unknown_terminated) and the diagnostic stream's own terminal is
    **not** allowed to override it. ``process_alive=False`` turns a would-be
    running/stopping state into ``unknown_terminated`` (a dead process without a
    terminal event). Only when there is no IPC event stream at all (an
    evidence-only re-analyze) do we fall back to the diagnostic terminal,
    labelled ``source="diagnostic_fallback"``.
    """

    if events is not None and events.has_events:
        terminal = events.harness_terminal()
        state = terminal["state"]
        if process_alive is False and state in {"running", "stopping"}:
            state = "unknown_terminated"
        merged = {
            "source": "runner_ipc",
            "state": state,
            "status": terminal.get("status"),
            "finished": bool(terminal.get("finished")),
            "finish_summary": terminal.get("finish_summary"),
            "takeover_reason": terminal.get("takeover_reason"),
            "reason": terminal.get("reason"),
            "returncode": outcome.get("returncode"),
            "steps": terminal.get("steps"),
            "stop_requested": bool(terminal.get("stop_requested")),
            "run_end_seen": bool(terminal.get("run_end_seen")),
            "run_summary_present": bool(terminal.get("run_summary_present")),
            "process_alive": process_alive,
            "trace_path": terminal.get("trace_path"),
            "tokens_total": terminal.get("tokens_total"),
        }
        return merged

    # Evidence-only fallback: no IPC events were available at all.
    return {
        "source": "diagnostic_fallback",
        "state": "unknown",
        "status": None,
        "finished": bool(outcome.get("finished")),
        "finish_summary": outcome.get("finish_summary"),
        "takeover_reason": outcome.get("takeover_reason"),
        "reason": outcome.get("reason"),
        "returncode": outcome.get("returncode"),
        "steps": outcome.get("steps"),
        "stop_requested": False,
        "run_end_seen": False,
        "run_summary_present": False,
        "process_alive": process_alive,
        "trace_path": None,
        "tokens_total": None,
    }


# ---------------------------------------------------------------------------
# case acceptance (evidence-only; missing -> unknown)
# ---------------------------------------------------------------------------
def build_case_block(case: Case | None) -> dict[str, Any] | None:
    if case is None:
        return None
    return {
        "id": case.id,
        "title": case.title,
        "goal": case.goal,
        "preconditions": list(case.preconditions),
        "preconditions_confirmed": case.preconditions_confirmed,
        "safety_boundaries": list(case.safety_boundaries),
        "acceptance": [
            {"id": a.id, "description": a.description} for a in case.acceptance
        ],
        "notes": case.notes,
    }


def build_case_acceptance(
    case: Case | None,
    view: EvidenceView,
    harness_finished: bool,
) -> dict[str, Any]:
    """Evaluate the Case checkpoints against recorded evidence only."""

    if case is None or not case.acceptance:
        return {
            "overall": "unknown",
            "checkpoints": [],
            "note": "未提供 Case 验收检查点；无法判定验收（harness 终局 ≠ Case 通过）。",
        }
    evidence = build_acceptance_evidence(view, harness_finished=harness_finished)
    checkpoints = evaluate_acceptance(case, evidence)
    overall = rollup_acceptance(checkpoints, harness_finished=harness_finished)
    return {
        "overall": overall,
        "checkpoints": checkpoints,
        "note": (
            "验收只依据客观设备观测（[OBS]/感知工具成功返回）判定 met/unmet；"
            "TaskDoc evidence_note/facts 与 finish 自述仅作候选证据，永不自动判通过；"
            "目标/intent/note/失败回执/未验证参考图不作为验收证据；"
            "无客观证据的检查点为 unknown，绝不因 harness finished 记为 met。"
        ),
    }


# The only objective observation anchor: a real ``[OBS] app=`` segment.
_OBS_FAILED_MARKER = "[OBS] (re-observation failed:"
_OBJECTIVE_OBS_HEADER = "[OBS] app="


def _objective_observation_text(call: dict[str, Any]) -> str | None:
    """Return the objective ``[OBS] app=…`` segment, or ``None``.

    Only the real observation segment is evidence. A successful action receipt's
    leading text (which may contain the target name) is **not** included — it is
    at most candidate evidence. Unverified reference frames (``image.reference``
    or no committed ``screen_seq``) are excluded even if their text looks right.
    """

    if call.get("error"):
        return None
    observation = call.get("observation") or {}
    image = observation.get("image") if isinstance(observation, dict) else None
    image = image if isinstance(image, dict) else {}
    if image.get("present") and (image.get("reference") or image.get("screen_seq") is None):
        return None
    text = str(call.get("result_text") or "")
    if not text or _OBS_FAILED_MARKER in text:
        return None
    if classify_result(text) in _ERROR_CLASSES:
        return None
    idx = text.find(_OBJECTIVE_OBS_HEADER)
    if idx == -1:
        return None
    return text[idx:]


def build_acceptance_evidence(
    view: EvidenceView, *, harness_finished: bool
) -> AcceptanceEvidence:
    """Collect the typed acceptance corpus.

    Objective entries come only from real ``[OBS] app=`` observation segments.
    Successful action receipts, TaskDoc evidence notes / facts, and the actor's
    finish self-claim are *candidate* entries and can never auto-pass. The goal,
    per-step ``intent`` / ``note`` / ``target_description``, and failed receipts
    are excluded entirely.
    """

    from case import KIND_CANDIDATE, KIND_OBJECTIVE

    texts: list[tuple[str, str, str]] = []
    for call in view.tool_calls:
        text = call.get("result_text") or ""
        if not text:
            continue
        objective = _objective_observation_text(call)
        if objective:
            texts.append((f"step {call.get('step')} {call.get('tool')} 观测", objective, KIND_OBJECTIVE))
        elif classify_result(text) == "success":
            # A successful action receipt is a world-action fact, but its text
            # may echo the target; keep it candidate-only.
            texts.append((f"step {call.get('step')} {call.get('tool')} 回执", text, KIND_CANDIDATE))
    snap = view.latest_taskdoc()
    if snap:
        for item in snap.get("items", []) or []:
            if item.get("evidence_note"):
                texts.append(
                    (f"TaskDoc {item.get('id')} 证据", str(item["evidence_note"]), KIND_CANDIDATE)
                )
        for fact in snap.get("facts", []) or []:
            texts.append(("TaskDoc fact", str(fact), KIND_CANDIDATE))
    # The actor's own finish claim is candidate-only (never objective).
    for call in view.tool_calls:
        if call.get("tool") != "finish":
            continue
        invoke = call.get("invoke") or {}
        args = invoke.get("args") if isinstance(invoke, dict) else None
        if isinstance(args, dict):
            summary = args.get("summary")
            if summary:
                texts.append((f"step {call.get('step')} finish 自述", str(summary), KIND_CANDIDATE))
            for i, ev in enumerate(args.get("evidence") or []):
                if ev:
                    texts.append(
                        (f"step {call.get('step')} finish evidence[{i}]", str(ev), KIND_CANDIDATE)
                    )
    return AcceptanceEvidence(texts, harness_finished=harness_finished)


# ---------------------------------------------------------------------------
# safety / budget / context errors (real run facts)
# ---------------------------------------------------------------------------
def build_safety(view: EvidenceView, events: RunnerEventsView | None) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    if events is not None:
        for event in events.safety_warnings:
            warnings.append(
                {
                    "step": event.get("step"),
                    "tool": event.get("tool"),
                    "text": event.get("text"),
                }
            )
    if not warnings:
        for call in view.tool_calls:
            if classify_result(call.get("result_text") or "") == "safety_warning":
                warnings.append(
                    {
                        "step": call.get("step"),
                        "tool": call.get("tool"),
                        "text": call.get("result_text"),
                    }
                )
    return {
        "warnings": warnings,
        "count": len(warnings),
        "mode": (events.config.get("safety_mode") if events else None),
        "note": (
            "wary（默认）：风险执行不执行、不叫人工，只回预警；模型带 "
            "confirm_irreversible=true 重发才执行。ask_user/take_over 仍 interrupt。"
        ),
    }


def build_budget(
    view: EvidenceView, events: RunnerEventsView | None, harness: dict[str, Any]
) -> dict[str, Any]:
    """Token cost ceiling vs loop fuse.

    ``visible_used_tokens`` is only the *actor's provider-reported* usage seen in
    the event stream. It is **not** the harness ``UsageLedger`` total (which also
    includes aux/verifier calls and estimates). No ledger is exported to the
    diagnosis artifacts, so the ledger total is ``unknown`` and must not be
    equated with the visible usage.
    """

    usage = events.usage_totals() if events is not None else None
    config = events.config if events is not None else {}
    token_budget = config.get("token_budget")
    visible_used = usage.get("total_tokens") if usage else None
    state = harness.get("state")
    return {
        "token_budget": token_budget,
        "visible_used_tokens": visible_used,
        "visible_usage_reported": bool(usage.get("reported")) if usage else False,
        "visible_usage_partial": bool(usage.get("partial")) if usage else False,
        "ledger_available": False,
        "ledger_used_tokens": None,
        "exhausted": state == "token_budget_exhausted",
        "warn_remaining": config.get("token_warn_remaining"),
        "max_model_calls": config.get("max_model_calls"),
        "loop_fuse_hit": state == "loop_fuse",
        "note": (
            "token 预算在调用边界达到即停；max_model_calls 是独立的 runaway-loop 保险丝。"
            "visible_used_tokens 仅为 actor 上报用量，不等于 harness UsageLedger（含 aux/估算，未导出→unknown）。"
        ),
    }


def build_context_errors(view: EvidenceView, events: RunnerEventsView | None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    for call in view.tool_calls:
        text = str(call.get("result_text") or "")
        if "native_context_pruning_conflict" in text:
            errors.append(
                {
                    "step": call.get("step"),
                    "kind": "native_context_pruning_conflict",
                    "message": text[:300],
                }
            )
    for call in view.tool_calls:
        text = str(call.get("result_text") or "")
        if text.startswith("[OBS] (re-observation failed:"):
            errors.append(
                {"step": call.get("step"), "kind": "obs_capture_failed", "message": text[:300]}
            )
    return {"errors": errors, "count": len(errors)}


def build_fallback(trace_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model fallback events from the production trace (real events only)."""

    rows: list[dict[str, Any]] = []
    for event in trace_events:
        if event.get("event") != "model_fallback":
            continue
        rows.append(
            {
                "stage": event.get("stage"),
                "role": event.get("role"),
                "requested": event.get("requested"),
                "actual": event.get("actual"),
                "reason": event.get("reason"),
                "outcome": event.get("outcome"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# finish verifier (independent L2 acceptance)
# ---------------------------------------------------------------------------
def build_finish_verifier(
    view: EvidenceView, events: RunnerEventsView | None = None
) -> dict[str, Any]:
    """Finish two-step receipts + independent verifier status.

    Only a **persisted, authoritative verifier verdict** can report
    ``pass`` / ``fail`` / ``skipped``. The finish receipts prove the two-step
    (review packet / confirm) happened; they say nothing about whether the
    verifier ran, passed, or was skipped fail-open. With no authoritative audit
    the status is ``unknown`` — never inferred from a finish receipt.

    ``events.run_json`` may carry a persisted ``result``-adjacent verifier field
    if one exists; we only surface it when explicitly present.
    """

    packets = 0
    confirmed = 0
    rejections: list[dict[str, Any]] = []
    dispute_takeover = False
    for call in view.finish_calls():
        text = call.get("result_text") or ""
        cls = classify_result(text)
        if cls == "finish_review_packet":
            packets += 1
        elif cls in {"finish_confirmed", "finish_ok"}:
            confirmed += 1
        elif cls == "verifier_reject":
            rejections.append({"step": call.get("step"), "message": text[:300]})
        elif cls == "verifier_dispute_takeover":
            dispute_takeover = True
            rejections.append({"step": call.get("step"), "message": text[:300]})

    # A rejected finish IS an in-band verifier verdict (the rejection text came
    # from the verifier), so `fail` is grounded; pass/skipped are not.
    persisted = None
    if events is not None and isinstance(events.run_json, dict):
        candidate = events.run_json.get("finish_verifier")
        if candidate in {"pass", "fail", "skipped"}:
            persisted = candidate

    if persisted is not None:
        verifier_status = persisted
        source = "persisted"
    elif rejections:
        verifier_status = "fail"
        source = "in_band_rejection"
    else:
        verifier_status = "unknown"
        source = "no_authoritative_audit"

    return {
        "review_packets": packets,
        "confirmed": confirmed,
        "rejections": rejections,
        "rejection_count": len(rejections),
        "dispute_takeover": dispute_takeover,
        "verifier_status": verifier_status,
        "verifier_status_source": source,
        "note": (
            "只有权威审计（本 run 持久化的 verdict 或 in-band 驳回回执）才显示"
            " pass/fail/skipped；验收器故障是 fail-open skipped，绝不显示为 pass；"
            "没有审计时按 unknown，不从 finish 回执推断未触发。"
        ),
    }


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
def build_context(view: EvidenceView) -> dict[str, Any]:
    reqs = view.model_requests
    peak_msg = max((r.get("message_count", 0) for r in reqs), default=0)
    peak_img = max((r.get("image_message_count", 0) for r in reqs), default=0)
    pruned_total = sum(r.get("pruned_screen_count", 0) for r in reqs)
    pinned_every = bool(reqs) and all(r.get("taskdoc_present") for r in reqs)
    avg_chars = _avg([float(r.get("context_chars", 0)) for r in reqs])
    return {
        "peak_message_count": peak_msg,
        "peak_image_messages": peak_img,
        "pruned_screen_total": pruned_total,
        "taskdoc_pinned_every_step": pinned_every,
        "avg_context_chars": avg_chars,
    }


# ---------------------------------------------------------------------------
# hitl
# ---------------------------------------------------------------------------
def build_hitl(
    view: EvidenceView, events: RunnerEventsView | None = None
) -> dict[str, Any]:
    decisions = view.hitl_decisions
    approvals = sum(1 for d in decisions if d.get("decision") == "approve")
    rejections = sum(1 for d in decisions if d.get("decision") == "reject")
    responds = sum(1 for d in decisions if d.get("decision") == "respond")
    ask_user = sum(
        1 for c in view.tool_calls if classify_result(c["result_text"]) == "ask_user"
    )
    take_over = sum(
        1
        for c in view.tool_calls
        if classify_result(c["result_text"]) == "takeover_requested"
    )
    unresolved: list[str] = []
    answers: list[str] = []
    submitted = consumed = unconsumed = 0
    if events is not None:
        state = events.hitl_state()
        unresolved = state["unresolved_prompts"]
        answers = state["answers"]
        submitted = state["submitted_count"]
        consumed = state["consumed_count"]
        unconsumed = state["unconsumed_count"]
    return {
        "interrupts": len(decisions),
        "decisions": [
            {
                "step": d.get("step"),
                "tool": d.get("tool"),
                "decision": d.get("decision"),
            }
            for d in decisions
        ],
        "approvals": approvals,
        "rejections": rejections,
        "responds": responds,
        "ask_user_count": ask_user,
        "take_over_count": take_over,
        "unresolved_prompts": unresolved,
        "answers": answers,
        "submitted_count": submitted,
        "consumed_count": consumed,
        "unconsumed_count": unconsumed,
        "note": (
            "control.jsonl 的答复只表示人工已提交，不代表已消费；只有 runner 的 "
            "pending_hitl:null 清除事件证明已消费。未决 HITL 绝不自动批准。"
        ),
    }


# ---------------------------------------------------------------------------
# tool_health
# ---------------------------------------------------------------------------
def build_tool_health(view: EvidenceView) -> dict[str, Any]:
    by_tool: dict[str, dict[str, Any]] = {}
    total_calls = 0
    total_errors = 0
    for call in view.tool_calls:
        tool = call.get("tool") or "?"
        cls = classify_result(call["result_text"])
        is_error = bool(call.get("error")) or cls in _ERROR_CLASSES
        total_calls += 1
        if is_error:
            total_errors += 1
        stats = by_tool.setdefault(
            tool,
            {"calls": 0, "ok": 0, "error": 0, "error_classes": {}, "_latencies": []},
        )
        stats["calls"] += 1
        if is_error:
            stats["error"] += 1
            stats["error_classes"][cls] = stats["error_classes"].get(cls, 0) + 1
        else:
            stats["ok"] += 1
        latency = call.get("latency_ms")
        if isinstance(latency, (int, float)):
            stats["_latencies"].append(float(latency))
    for tool, stats in by_tool.items():
        lat = stats.pop("_latencies")
        stats["avg_latency_ms"] = _avg(lat)
        stats["p95_latency_ms"] = round(_percentile(lat, 95), 2)
    return {
        "total_calls": total_calls,
        "total_errors": total_errors,
        "error_rate": round(total_errors / total_calls, 3) if total_calls else 0.0,
        "by_tool": by_tool,
    }


# Classes that count as a tool error for health purposes. A single one of these
# is a tool-health fact — it never becomes a whole-run failure by itself.
_ERROR_CLASSES = {
    "obs_capture_failed",
    "addressing_conflict",
    "addressing_missing",
    "stale_mark",
    "ambiguous_resolve",
    "locate_no_match",
    "locate_provider_error",
    "bad_coords",
    "bad_direction",
    "ambiguous_app",
    "launch_denied",
    "app_not_installed",
    "launch_failed",
    "unknown_app",
    "taskdoc_input_invalid",
    "taskdoc_validation_failed",
    "finish_no_evidence",
    "finish_blocked_open_items",
}

# Notable (not necessarily tool errors) classes that still deserve a finding.
_NOTABLE_CLASSES = {
    "safety_warning",
    "verifier_reject",
    "verifier_dispute_takeover",
}


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------
def build_grounding(view: EvidenceView) -> dict[str, Any]:
    by_mark_id = 0
    by_description = 0
    ambiguous = 0
    stale = 0
    no_match = 0
    locate_calls = 0
    locate_success = 0
    locate_no_match = 0
    locate_provider_error = 0
    launch = {
        "resolved": 0,
        "denied": 0,
        "unknown": 0,
        "not_installed": 0,
        "ambiguous": 0,
        "failed": 0,
    }

    for call in view.tool_calls:
        tool = call.get("tool")
        invoke = call.get("invoke") or {}
        args = invoke.get("args") if isinstance(invoke, dict) else None
        args = args if isinstance(args, dict) else {}
        cls = classify_result(call["result_text"])

        if tool in {"tap", "long_press", "type_text"}:
            if args.get("target_mark_id"):
                by_mark_id += 1
            elif args.get("target_description"):
                by_description += 1

        if cls == "ambiguous_resolve":
            ambiguous += 1
        elif cls == "stale_mark":
            stale += 1
        elif cls == "locate_no_match":
            no_match += 1

        if tool == "locate":
            locate_calls += 1
            if cls == "locate_no_match":
                locate_no_match += 1
            elif cls == "locate_provider_error":
                locate_provider_error += 1
            else:
                locate_success += 1

        if tool == "launch_app":
            if cls == "success":
                launch["resolved"] += 1
            elif cls == "launch_denied":
                launch["denied"] += 1
            elif cls == "app_not_installed":
                launch["not_installed"] += 1
            elif cls == "launch_failed":
                launch["failed"] += 1
            elif cls == "ambiguous_app":
                launch["ambiguous"] += 1
            elif cls == "unknown_app":
                launch["unknown"] += 1

    return {
        "mark_addressing": {"by_mark_id": by_mark_id, "by_description": by_description},
        "resolve_failures": {
            "ambiguous": ambiguous,
            "stale": stale,
            "no_match": no_match,
        },
        "locate": {
            "calls": locate_calls,
            "success": locate_success,
            "no_match": locate_no_match,
            "provider_error": locate_provider_error,
        },
        "launch": launch,
    }


# ---------------------------------------------------------------------------
# visual (D2)
# ---------------------------------------------------------------------------
def build_visual(view: EvidenceView) -> dict[str, Any]:
    with_image = 0
    total_bytes = 0
    first_step: int | None = None
    last_step: int | None = None
    for obs in view.observations:
        image = obs.get("image") or {}
        if image.get("present"):
            with_image += 1
            total_bytes += int(image.get("bytes", 0) or 0)
            step = obs.get("step")
            if first_step is None:
                first_step = step
            last_step = step
    return {
        "tool_results_with_image": with_image,
        "total_image_bytes": total_bytes,
        "first_image_step": first_step,
        "last_image_step": last_step,
    }


# ---------------------------------------------------------------------------
# windowing (WP-S3): window-grouped marks structure + op distribution
# ---------------------------------------------------------------------------
_WINDOW_OP_LEVELS = ("confirmed", "likely", "blocked", "unknown", "unspecified")


def build_windowing(view: EvidenceView) -> dict[str, Any]:
    """Aggregate the WP-G2a windowed marks format across the run (fail-open).

    Re-parses every ``tool_observation``'s ``[OBS]`` text with
    :func:`parse_obs_windows`. When at least one observation is windowed the
    block reports ``present=True`` plus the peak window count, aggregate op-tier
    counts, window-type tally, and per-observation summaries. When no
    observation is windowed (legacy flat runs) the block is inert
    (``present=False`` with zeroed tallies) so an old run analyzes unchanged.

    ``blocked_taps`` correlates the model's actual actuation with the windowed
    marks: a ``tap`` / ``long_press`` / ``type_text`` whose ``target_mark_id``
    was tagged ``op=blocked`` in the most recent windowed observation is a
    finding (the model addressed a mark the window layer had marked blocked).
    """

    observations: list[dict[str, Any]] = []
    peak_windows = 0
    total_op_counts = {level: 0 for level in _WINDOW_OP_LEVELS}
    type_counts: dict[str, int] = {}
    present = False
    # mark_id -> op, from the most recent windowed observation seen so far, so a
    # later actuation is judged against the window structure it acted on.
    blocked_by_mark: dict[str, str] = {}
    blocked_taps: list[dict[str, Any]] = []

    for call in view.tool_calls:
        tool = call.get("tool")
        obs_event = call.get("observation") or {}
        text = result_text_of(obs_event)
        parsed = None
        try:
            parsed = parse_obs_windows(text)
        except Exception:  # noqa: BLE001 - a malformed OBS must never gate analysis
            parsed = None

        # Before recording this observation's own structure, judge whether this
        # step's actuation targeted a mark the *prior* windowed frame blocked.
        if tool in {"tap", "long_press", "type_text"} and blocked_by_mark:
            invoke = call.get("invoke") or {}
            args = invoke.get("args") if isinstance(invoke, dict) else None
            args = args if isinstance(args, dict) else {}
            mark_id = args.get("target_mark_id")
            if mark_id and blocked_by_mark.get(str(mark_id)) == "blocked":
                blocked_taps.append(
                    {
                        "step": call.get("step"),
                        "tool": tool,
                        "target_mark_id": str(mark_id),
                    }
                )

        if not parsed or not parsed.get("present"):
            continue
        present = True
        window_count = int(parsed.get("window_count", 0) or 0)
        peak_windows = max(peak_windows, window_count)
        op_counts = parsed.get("op_counts") or {}
        for level in _WINDOW_OP_LEVELS:
            total_op_counts[level] += int(op_counts.get(level, 0) or 0)
        windows = parsed.get("windows") or []
        for window in windows:
            win_type = window.get("type") or "?"
            type_counts[win_type] = type_counts.get(win_type, 0) + 1
        observations.append(
            {
                "step": call.get("step"),
                "tool": tool,
                "schema": parsed.get("schema"),
                "source": parsed.get("source"),
                "window_count": window_count,
                "op_counts": {
                    level: int(op_counts.get(level, 0) or 0)
                    for level in _WINDOW_OP_LEVELS
                },
                "windows": [
                    {
                        "id": window.get("id"),
                        "type": window.get("type"),
                        "package": window.get("package"),
                        "layer": window.get("layer"),
                        "covered_by": window.get("covered_by"),
                        "flags": list(window.get("flags", []) or []),
                        "mark_count": int(window.get("mark_count", 0) or 0),
                        "op_counts": dict(window.get("op_counts", {}) or {}),
                    }
                    for window in windows
                ],
                "blocked_mark_ids": list(parsed.get("blocked_mark_ids", []) or []),
            }
        )
        # Refresh the blocked-mark index to this frame's structure.
        blocked_by_mark = {}
        for window in windows:
            for mark in window.get("marks", []) or []:
                mid = mark.get("mark_id")
                if mid:
                    blocked_by_mark[str(mid)] = mark.get("op")

    return {
        "present": present,
        "windowed_observations": len(observations),
        "peak_window_count": peak_windows,
        "op_counts": total_op_counts,
        "window_types": type_counts,
        "blocked_taps": blocked_taps,
        "observations": observations,
    }


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
def build_model(
    view: EvidenceView, events: RunnerEventsView | None = None
) -> dict[str, Any]:
    """Model calls + usage. Missing usage/cache is ``None``, never zero.

    The runner event stream is authoritative for identity (requested vs actual)
    and usage; the diagnostic stream is the fallback when no runner events exist
    (e.g. an evidence-only re-analyze).
    """

    identity: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    calls = len(view.model_requests)
    if events is not None and events.model_calls:
        identity = events.model_identity()
        usage = events.usage_totals()
        calls = len(events.model_calls)
    else:
        prompt_tokens = 0
        output_tokens = 0
        input_calls = 0
        output_calls = 0
        resp_calls = 0
        for resp in view.model_responses:
            raw = resp.get("usage")
            if not isinstance(raw, dict):
                continue
            resp_calls += 1
            if raw.get("input_tokens") is not None:
                input_calls += 1
                prompt_tokens += int(raw.get("input_tokens") or 0)
            if raw.get("output_tokens") is not None:
                output_calls += 1
                output_tokens += int(raw.get("output_tokens") or 0)
        if input_calls or output_calls:
            complete = resp_calls > 0 and input_calls == resp_calls and output_calls == resp_calls
            usage = {
                "reported": True,
                "partial": not complete,
                "calls": resp_calls,
                "input_tokens": prompt_tokens if input_calls else None,
                "output_tokens": output_tokens if output_calls else None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "total_tokens": (prompt_tokens + output_tokens) if complete else None,
                "coverage": {
                    "calls": resp_calls,
                    "input_tokens_calls": input_calls,
                    "output_tokens_calls": output_calls,
                    "cache_read_tokens_calls": 0,
                    "cache_write_tokens_calls": 0,
                },
            }
    requested = sorted({row.get("requested_model") for row in identity if row.get("requested_model")})
    actual = sorted({row.get("actual_model") for row in identity if row.get("actual_model")})
    return {
        "calls": calls,
        "avg_latency_ms": None,
        "p95_latency_ms": None,
        "errors": 0,
        "requested_models": requested,
        "actual_models": actual,
        "identity": identity,
        "token_usage": usage,
    }


# ---------------------------------------------------------------------------
# resolver / memory / capabilities (WP-S2, optional production artifacts)
# ---------------------------------------------------------------------------
def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    """Best-effort JSONL reader for partially-written observability files."""

    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _read_jsonl_tail(
    path: Path | None, *, max_lines: int = 5000
) -> list[dict[str, Any]]:
    """Read a bounded JSONL tail while tolerating missing and bad lines."""

    if path is None:
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            lines = deque(stream, maxlen=max_lines)
    except (OSError, UnicodeError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact_root(run_dir: str | None, evidence_stream: str | None) -> Path | None:
    if run_dir:
        return Path(run_dir).expanduser()
    if evidence_stream:
        return Path(evidence_stream).expanduser().parent
    return None


def _trace_file(
    trace: str | None, root: Path | None, source_run_id: str
) -> Path | None:
    candidates: list[Path] = []
    if trace:
        candidates.append(Path(trace).expanduser())
    if root is not None:
        candidates.append(root / "traces")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
            if not candidate.is_dir():
                continue
            preferred = candidate / f"{source_run_id}.jsonl"
            if preferred.is_file():
                return preferred
            matches = sorted(candidate.glob("*.jsonl"))
        except OSError:
            matches = []
        if len(matches) == 1:
            return matches[0]
    return None


def _text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                texts.append(item)
        return texts
    if isinstance(value, dict):
        return [str(value.get("text", ""))] if "text" in value else []
    return []


def _launched_package(value: Any) -> str | None:
    for text in _text_fragments(value):
        if not text.lstrip().startswith("OK."):
            continue
        match = _LAUNCHED_PACKAGE_RE.search(text)
        if match:
            return match.group(1)
    return None


def _launch_succeeded(value: Any) -> bool:
    return any(
        text.lstrip().startswith("OK.") and "launched" in text
        for text in _text_fragments(value)
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_resolver(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract app-name resolution attempts and route quality from trace."""

    attempts: list[dict[str, Any]] = []
    pending: int | None = None
    launch_step: Any = None
    for event in trace_events:
        event_name = event.get("event")
        if event_name == "tool_call" and event.get("tool") == "launch_app":
            launch_step = event.get("step")
            pending = None
            continue
        if event_name == "resolution_attempt":
            raw_candidates = event.get("candidates")
            candidates = [
                dict(item)
                for item in (raw_candidates if isinstance(raw_candidates, list) else [])
                if isinstance(item, dict)
            ]
            top1 = candidates[0] if candidates else {}
            first_score = _number(top1.get("rank_score", top1.get("score")))
            second_score = (
                _number(candidates[1].get("rank_score", candidates[1].get("score")))
                if len(candidates) > 1
                else None
            )
            margin = (
                round(first_score - second_score, 6)
                if first_score is not None and second_score is not None
                else None
            )
            attempt = {
                "launch_index": len(attempts) + 1,
                "step": event.get("step", launch_step),
                "mention": str(event.get("mention", "")),
                "route": top1.get("source_route"),
                "match_type": event.get("match_type") or top1.get("match_type"),
                "authority": event.get("authority") or top1.get("authority"),
                "decision_basis": event.get("decision_basis"),
                "reason": event.get("reason"),
                "top1_package": top1.get("package"),
                "top1_score": first_score,
                "margin": margin,
                "decision": str(event.get("decision", "unknown")),
                "winner": event.get("winner"),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "launch_succeeded": False,
                "launched_package": None,
            }
            attempts.append(attempt)
            pending = len(attempts) - 1
            continue
        if (
            event_name == "tool_result"
            and event.get("tool") == "launch_app"
            and pending is not None
        ):
            package = _launched_package(event.get("result"))
            succeeded = _launch_succeeded(event.get("result"))
            if succeeded and package is None:
                package = attempts[pending].get("winner")
            attempts[pending]["step"] = event.get("step", attempts[pending]["step"])
            attempts[pending]["launch_succeeded"] = succeeded
            attempts[pending]["launched_package"] = package
            pending = None

    route_stats = {
        route: {
            "attempts": 0,
            "resolved": 0,
            "successful_launches": 0,
            "resolution_rate": 0.0,
            "launch_success_rate": 0.0,
        }
        for route in _RESOLVER_ROUTES
    }
    match_type_stats = {
        match_type: {
            "attempts": 0,
            "resolved": 0,
            "successful_launches": 0,
            "resolution_rate": 0.0,
            "launch_success_rate": 0.0,
        }
        for match_type in _RESOLVER_MATCH_TYPES
    }
    decision_counts = {"resolved": 0, "ambiguous": 0, "unknown": 0}
    for attempt in attempts:
        decision = attempt["decision"]
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        route = attempt.get("route")
        if route in route_stats:
            stats = route_stats[route]
            stats["attempts"] += 1
            stats["resolved"] += int(decision == "resolved")
            stats["successful_launches"] += int(attempt["launch_succeeded"])
        match_type = attempt.get("match_type")
        if match_type in match_type_stats:
            stats = match_type_stats[match_type]
            stats["attempts"] += 1
            stats["resolved"] += int(decision == "resolved")
            stats["successful_launches"] += int(attempt["launch_succeeded"])
    for stats in list(route_stats.values()) + list(match_type_stats.values()):
        count = stats["attempts"]
        if count:
            stats["resolution_rate"] = round(stats["resolved"] / count, 3)
            stats["launch_success_rate"] = round(
                stats["successful_launches"] / count, 3
            )

    recoveries: list[dict[str, Any]] = []
    for index, attempt in enumerate(attempts):
        if attempt["decision"] != "ambiguous":
            continue
        candidate_packages = {
            str(item.get("package"))
            for item in attempt["candidates"]
            if item.get("package")
        }
        recovery = next(
            (
                later
                for later in attempts[index + 1 :]
                if later["decision"] == "resolved"
                and later["launch_succeeded"]
                and later.get("winner") in candidate_packages
            ),
            None,
        )
        if recovery is not None:
            recoveries.append(
                {
                    "ambiguous_launch_index": attempt["launch_index"],
                    "ambiguous_mention": attempt["mention"],
                    "recovery_launch_index": recovery["launch_index"],
                    "recovery_mention": recovery["mention"],
                    "winner": recovery["winner"],
                }
            )

    return {
        "attempts": attempts,
        "total_attempts": len(attempts),
        "decision_counts": decision_counts,
        "route_stats": route_stats,
        "match_type_stats": match_type_stats,
        "ambiguous_count": decision_counts.get("ambiguous", 0),
        "embedding_launch_hits": [
            attempt
            for attempt in attempts
            if attempt.get("route") == "embedding" and attempt.get("launch_succeeded")
        ],
        "ambiguous_recoveries": recoveries,
    }


def _configured_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else _REPO_ROOT / candidate


def _memory_roots(root: Path | None, explicit: str | None) -> list[Path]:
    """Explicit-only memory roots for offline analysis.

    The MVP does **not** probe the repo ``memory/`` tree, the run dir, or the
    ``PHONE_AGENT_MEMORY_DIR`` env by default — those are global/private and must
    not become an implicit dependency of an offline analyze. Only an explicitly
    supplied ``memory_dir`` is searched.
    """

    if not explicit:
        return []
    candidate = _configured_path(explicit)
    return [candidate]


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _episode_for_run(roots: list[Path], run_id: str) -> dict[str, Any]:
    """Find this run's episode outcome under the *explicit* roots only.

    No env / repo / global fallback: an empty ``roots`` list yields ``{}``.
    """

    event_paths: list[Path] = []
    json_paths: list[Path] = []
    for root in roots:
        event_paths.extend(
            (root / "experience/events.jsonl", root / "memory/experience/events.jsonl")
        )
        json_paths.extend(
            (
                root / "experience/episodes.json",
                root / "memory/experience/episodes.json",
            )
        )
    for event in reversed(_read_jsonl(_first_existing(event_paths))):
        if (
            event.get("type") == "episode_outcome"
            and str(event.get("run_id")) == run_id
        ):
            return event
    materialized = _read_json_object(_first_existing(json_paths))
    value = materialized.get(run_id)
    return dict(value) if isinstance(value, dict) else {}


def _alias_ref_id(entry: dict[str, Any]) -> str:
    identity = "\0".join(
        str(entry.get(key, "")) for key in ("term", "package", "kind", "scope")
    )
    return "alias:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _candidate_packages(
    candidate: dict[str, Any],
    *,
    episodes: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> list[str]:
    packages: list[str] = []
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    values = [
        candidate.get("package"),
        candidate.get("app_package"),
        metadata.get("app_package"),
    ]
    for value in values:
        if value:
            packages.append(str(value))
    for source in (candidate.get("apps"), metadata.get("apps")):
        if isinstance(source, list):
            packages.extend(str(value) for value in source if value)
    ref_id = str(candidate.get("ref_id", ""))
    if candidate.get("namespace") == "episode" and ref_id in episodes:
        packages.extend(
            str(value) for value in episodes[ref_id].get("apps", []) if value
        )
    if candidate.get("namespace") == "app_alias" and ref_id in aliases:
        packages.append(aliases[ref_id])
    if ref_id.startswith("registry:"):
        try:
            from phone_agent.config.apps import DEFAULT_APP_REGISTRY

            canonical_id = ref_id.removeprefix("registry:")
            identity = next(
                (
                    item
                    for item in DEFAULT_APP_REGISTRY.identities
                    if item.canonical_id == canonical_id
                ),
                None,
            )
            if identity is not None:
                packages.append(identity.primary_package)
        except Exception:  # noqa: BLE001 - optional package enrichment
            pass
    if ref_id.startswith("package:"):
        packages.append(ref_id.removeprefix("package:"))
    return list(dict.fromkeys(package for package in packages if package))


def _event_matches_run(
    event: dict[str, Any],
    run_ids: set[str],
    *,
    episode: dict[str, Any],
) -> bool:
    direct = {str(event.get(key, "")) for key in ("run_id", "evidence_run_id")}
    if direct & run_ids:
        return True
    note = str(event.get("evidence_note", ""))
    match = _RUN_ID_IN_NOTE_RE.search(note)
    if match and match.group(1) in run_ids:
        return True
    # Several App-KB writes predate an explicit run-id field. They can still be
    # attributed when their timestamp falls inside this episode's persisted
    # start/end window; no window means no attribution.
    try:
        stamp = datetime.fromisoformat(
            str(event["ts"]).replace("Z", "+00:00")
        ).timestamp()
        return float(episode["ts_start"]) <= stamp <= float(episode["ts_end"])
    except (KeyError, TypeError, ValueError):
        return False


def _clean_alias_event(event: dict[str, Any]) -> dict[str, Any]:
    entry = event.get("entry") if isinstance(event.get("entry"), dict) else {}
    return {
        "op": event.get("op"),
        "kind": entry.get("kind"),
        "term": entry.get("term", event.get("term")),
        "package": entry.get("package", event.get("package")),
        "old_package": event.get("old_package"),
        "new_package": event.get("new_package"),
        "changed": event.get("changed"),
        "ts": event.get("ts"),
    }


def build_memory(
    trace_events: list[dict[str, Any]],
    view: EvidenceView,
    *,
    source_run_id: str,
    summary_run_id: str,
    root: Path | None,
    memory_dir: str | None = None,
) -> dict[str, Any]:
    """Join run-start recall, confirmed launches, App-KB events, and episode."""

    roots = _memory_roots(root, memory_dir)
    episode = _episode_for_run(roots, source_run_id)

    appkb_paths: list[Path] = []
    for memory_root in roots:
        appkb_paths.extend(
            (
                memory_root / "app_kb/events.jsonl",
                memory_root / "memory/app_kb/events.jsonl",
            )
        )
    appkb_events = _read_jsonl_tail(_first_existing(appkb_paths))
    alias_packages: dict[str, str] = {}
    for event in appkb_events:
        entry = event.get("entry")
        if isinstance(entry, dict) and entry.get("package"):
            alias_packages[_alias_ref_id(entry)] = str(entry["package"])

    episode_events: dict[str, dict[str, Any]] = {}
    experience_paths: list[Path] = []
    configured_experience = os.getenv("PHONE_AGENT_EXPERIENCE_DIR")
    if configured_experience:
        experience_paths.append(
            _configured_path(configured_experience) / "events.jsonl"
        )
    for memory_root in roots:
        experience_paths.extend(
            (
                memory_root / "experience/events.jsonl",
                memory_root / "memory/experience/events.jsonl",
            )
        )
    for event in _read_jsonl(_first_existing(experience_paths)):
        if event.get("type") == "episode_outcome" and event.get("run_id"):
            episode_events[str(event["run_id"])] = event
    episode_json_paths: list[Path] = []
    if configured_experience:
        episode_json_paths.append(
            _configured_path(configured_experience) / "episodes.json"
        )
    for memory_root in roots:
        episode_json_paths.extend(
            (
                memory_root / "experience/episodes.json",
                memory_root / "memory/experience/episodes.json",
            )
        )
    for key, value in _read_json_object(_first_existing(episode_json_paths)).items():
        if isinstance(value, dict):
            episode_events.setdefault(str(key), value)

    actual_packages: list[str] = []
    for event in trace_events:
        if event.get("event") == "tool_result" and event.get("tool") == "launch_app":
            package = _launched_package(event.get("result"))
            if package:
                actual_packages.append(package)
    recall_evaluation: dict[str, Any] = {}
    for event in trace_events:
        if event.get("event") == "recall_evaluation" and isinstance(
            event.get("evaluation"), dict
        ):
            recall_evaluation = dict(event["evaluation"])
    actual_packages.extend(
        str(value) for value in recall_evaluation.get("actual_apps", []) if value
    )
    for call in view.tool_calls:
        if call.get("tool") == "launch_app":
            package = _launched_package(call.get("result_text"))
            if package:
                actual_packages.append(package)
    actual_packages.extend(str(value) for value in episode.get("apps", []) if value)
    actual_packages = list(dict.fromkeys(actual_packages))
    actual_set = set(actual_packages)

    recall_payload: dict[str, Any] = {}
    for event in trace_events:
        if event.get("event") == "run_start" and isinstance(
            event.get("memory_rag"), dict
        ):
            recall_payload = dict(event["memory_rag"])
            break
    candidates: list[dict[str, Any]] = []
    raw_candidates = recall_payload.get("candidates")
    for position, raw in enumerate(
        raw_candidates if isinstance(raw_candidates, list) else [], start=1
    ):
        if not isinstance(raw, dict):
            continue
        packages = _candidate_packages(
            raw, episodes=episode_events, aliases=alias_packages
        )
        matched = sorted(set(packages) & actual_set)
        candidates.append(
            {
                "rank": position,
                "namespace": raw.get("namespace"),
                "ref_id": raw.get("ref_id"),
                "score": _number(raw.get("score")),
                "packages": packages,
                "matched_packages": matched,
                "hit": bool(matched),
            }
        )
    matched_packages = sorted(
        {
            package
            for candidate in candidates
            for package in candidate["matched_packages"]
        }
    )
    matched_packages = sorted(
        set(matched_packages)
        | {str(value) for value in recall_evaluation.get("matched_apps", []) if value}
    )

    run_ids = {source_run_id, summary_run_id}
    alias_events = [
        _clean_alias_event(event)
        for event in appkb_events
        if _event_matches_run(event, run_ids, episode=episode)
        and (
            event.get("op") in {"alias_overwritten", "alias_user_set"}
            or (
                event.get("op") == "upsert"
                and isinstance(event.get("entry"), dict)
                and event["entry"].get("kind") in {"learned", "user"}
            )
        )
    ]

    return {
        "source_run_id": source_run_id,
        "memory_rag": {
            "mode": recall_payload.get("mode"),
            "status": recall_payload.get("status"),
            "candidates": candidates,
            "candidate_count": len(candidates),
            "actual_launch_packages": actual_packages,
            "matched_packages": matched_packages,
            "hit": bool(matched_packages),
        },
        "alias_events": alias_events,
        "episode": {
            "found": bool(episode),
            "injected_lessons": list(episode.get("injected_lessons", []) or []),
            "deliverable_path": episode.get("deliverable_path"),
        },
    }


def build_capabilities(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the latest run-start capability snapshot, or an empty block."""

    snapshot = next(
        (
            event
            for event in reversed(trace_events)
            if event.get("event") == "capability_snapshot"
        ),
        {},
    )
    raw_items = snapshot.get("capabilities") if isinstance(snapshot, dict) else None
    items: list[dict[str, Any]] = []
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict) or not item.get("cap_id"):
            continue
        raw_missing = item.get("missing_deps")
        items.append(
            {
                "cap_id": item.get("cap_id"),
                "title": item.get("title"),
                "mode": item.get("mode"),
                "state": item.get("state"),
                "missing_deps": (
                    list(raw_missing) if isinstance(raw_missing, (list, tuple)) else []
                ),
            }
        )
    counts: dict[str, int] = {}
    for item in items:
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "items": items,
        "by_id": {
            str(item["cap_id"]): {
                "mode": item.get("mode"),
                "state": item.get("state"),
            }
            for item in items
        },
        "counts": counts,
        "memory_generation": snapshot.get("memory_generation") if snapshot else None,
    }


# ---------------------------------------------------------------------------
# replay (A5 §3): per-step model turn + tool observations for the step report
# ---------------------------------------------------------------------------
def build_replay(view: EvidenceView) -> list[dict[str, Any]]:
    """Flatten the evidence into an ordered per-step replay for the report.

    Each entry carries the model's thinking + tool calls + token usage, the tool
    observations that followed (result text, latency, screenshot ``path``, parsed
    OBS, error), and the request context stats. This is the data behind the
    step-by-step replay — the report renders the real screenshot from
    ``image.path`` next to the model's full reasoning and each tool result.
    """

    replay: list[dict[str, Any]] = []
    for slot in view.replay_steps():
        request = slot.get("request") or {}
        response = slot.get("response") or {}
        calls = []
        for call in slot.get("tool_calls", []):
            obs = call.get("observation") or {}
            invoke = call.get("invoke") or {}
            image = obs.get("image") or {}
            # WP-S3: attach a compact windowed-marks summary when the OBS text is
            # in the grouped format; None for legacy flat observations.
            windows = None
            try:
                parsed = parse_obs_windows(result_text_of(obs))
            except Exception:  # noqa: BLE001 - never let a bad OBS gate replay
                parsed = None
            if parsed and parsed.get("present"):
                windows = {
                    "window_count": parsed.get("window_count", 0),
                    "source": parsed.get("source"),
                    "op_counts": parsed.get("op_counts") or {},
                    "windows": [
                        {
                            "id": window.get("id"),
                            "type": window.get("type"),
                            "package": window.get("package"),
                            "covered_by": window.get("covered_by"),
                            "mark_count": window.get("mark_count", 0),
                        }
                        for window in (parsed.get("windows") or [])
                    ],
                    "blocked_mark_ids": list(parsed.get("blocked_mark_ids", []) or []),
                }
            calls.append(
                {
                    "tool": call.get("tool"),
                    "args": invoke.get("args"),
                    "result_text": result_text_of(obs),
                    "result_truncated": isinstance(obs.get("result_text"), dict),
                    "latency_ms": call.get("latency_ms"),
                    "error": call.get("error"),
                    "class": classify_result(call.get("result_text") or ""),
                    "obs": obs.get("obs"),
                    "windows": windows,
                    "image": {
                        "present": bool(image.get("present")),
                        "screen_seq": image.get("screen_seq"),
                        "bytes": image.get("bytes", 0),
                        "path": image.get("path"),
                    },
                }
            )
        replay.append(
            {
                "step": slot.get("step"),
                # The middleware records the assistant message's *visible* text
                # (what the provider actually returned), never hidden reasoning.
                "model_text": response.get("thinking", ""),
                "model_tool_calls": response.get("tool_calls", []),
                "usage": response.get("usage"),
                "context": {
                    "message_count": request.get("message_count"),
                    "image_message_count": request.get("image_message_count"),
                    "pruned_screen_count": request.get("pruned_screen_count"),
                    "taskdoc_present": request.get("taskdoc_present"),
                    "context_chars": request.get("context_chars"),
                },
                "tool_calls": calls,
                "hitl": slot.get("hitl", []),
            }
        )
    return replay


# ---------------------------------------------------------------------------
# findings + recommendations
# ---------------------------------------------------------------------------
def build_findings(
    view: EvidenceView,
    resolver: dict[str, Any] | None = None,
    windowing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One finding per report category that fired an error/notable class."""

    cat_counts: dict[str, int] = {}
    cat_examples: dict[str, list[str]] = {}
    for call in view.tool_calls:
        cls = classify_result(call["result_text"])
        if cls in _ERROR_CLASSES or cls in _NOTABLE_CLASSES:
            cat = category_of(cls)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            cat_examples.setdefault(cat, [])
            if len(cat_examples[cat]) < 3:
                cat_examples[cat].append(call["result_text"][:200])

    findings: list[dict[str, Any]] = []
    for cat, count in sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        rule = V2_SOURCE_RULES.get(cat)
        if not rule:
            continue
        findings.append(
            {
                "category": cat,
                "layer": rule["layer"],
                "severity": rule["severity"],
                "title": rule["title"],
                "count": count,
                "examples": cat_examples.get(cat, []),
                "files": add_line_numbers(rule["files"]),
                "suggestion": rule["suggestion"],
                "verify": rule["verify"],
            }
        )

    resolver = resolver or {}
    embedding_hits = resolver.get("embedding_launch_hits", []) or []
    if embedding_hits:
        mentions = [str(item.get("mention", "")) for item in embedding_hits]
        findings.append(
            {
                "category": "resolver_embedding_hit",
                "layer": "resolver",
                "severity": "P2",
                "title": "应用名经 embedding 路才解析并启动",
                "count": len(embedding_hits),
                "examples": mentions[:3],
                "files": add_line_numbers(["phone_agent/v2/names.py"]),
                "suggestion": (
                    "embedding 命中说明静态 registry / App-KB 尚未覆盖该叫法；"
                    "核对高频 mention，必要时补充可信别名，减少向量兜底成本。"
                ),
                "verify": "用同一 mention 再跑 launch_app，确认 exact/lexical 可稳定命中且不引入歧义。",
            }
        )
    recoveries = resolver.get("ambiguous_recoveries", []) or []
    if recoveries:
        findings.append(
            {
                "category": "resolver_ambiguous_recovered",
                "layer": "resolver",
                "severity": "Info",
                "title": "歧义候选后模型细化名称并恢复启动",
                "count": len(recoveries),
                "examples": [
                    f"{item.get('ambiguous_mention')} → {item.get('recovery_mention')} → {item.get('winner')}"
                    for item in recoveries[:3]
                ],
                "files": add_line_numbers(
                    ["phone_agent/v2/names.py", "phone_agent/v2/tools/actuation.py"]
                ),
                "suggestion": "正向记录：排序候选给出了可用恢复线索，保持 fail-closed 歧义回执。",
                "verify": "复跑歧义叫法，确认候选排序稳定且细化名称后只启动唯一包。",
            }
        )

    windowing = windowing or {}
    blocked_taps = windowing.get("blocked_taps", []) or []
    if blocked_taps:
        findings.append(
            {
                "category": "windowed_blocked_tap",
                "layer": "grounding",
                "severity": "P1",
                "title": "点击了窗口层标记为 op=blocked 的 mark",
                "count": len(blocked_taps),
                "examples": [
                    f"step {tap.get('step')}: {tap.get('tool')} → {tap.get('target_mark_id')}"
                    for tap in blocked_taps[:3]
                ],
                "files": add_line_numbers(
                    [
                        "phone_agent/v2/session.py",
                        "phone_agent/v2/tools/actuation.py",
                    ]
                ),
                "suggestion": (
                    "窗口分组 marks 把该 mark 标记为 op=blocked（被上层窗口遮挡/不可交互），"
                    "模型仍以 target_mark_id 对其执行动作。核对窗口层遮挡计算与 marks 摘要中"
                    " op 档位的呈现，确认被遮挡 mark 不被优先建议。"
                ),
                "verify": (
                    "构造多窗口（W1 覆盖 W2）观测，确认被 covered_by 遮挡窗口内的 mark 标 op=blocked，"
                    "且对其寻址时给出可感知的降级提示。"
                ),
            }
        )
    return findings


def build_summary(
    outcome: dict[str, Any],
    view: EvidenceView,
    *,
    run_id: str,
    created_at: str,
    target: str,
    case: Case | None = None,
    events: RunnerEventsView | None = None,
    run_dir: str | None = None,
    command: list[str] | None = None,
    duration_sec: float | None = None,
    evidence_stream: str | None = None,
    trace: str | None = None,
    artifacts: dict[str, Any] | None = None,
    memory_dir: str | None = None,
    process_alive: bool | None = None,
    extra_data_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full ``summary.json`` from the two evidence planes.

    ``outcome`` is the live driver's terminal snapshot (or an empty dict when
    re-analyzing); ``events`` is the runner IPC view (present for runner-backed
    and re-analyzed runs).
    """

    root = _artifact_root(run_dir, evidence_stream)
    source_run_id = str((view.run_start or {}).get("run_id") or run_id)
    trace_events = _read_jsonl(_trace_file(trace, root, source_run_id))
    try:
        resolver = build_resolver(trace_events)
    except Exception:  # noqa: BLE001 - optional analysis must never gate a report
        resolver = build_resolver([])
    try:
        memory = build_memory(
            trace_events,
            view,
            source_run_id=source_run_id,
            summary_run_id=run_id,
            root=root,
            memory_dir=memory_dir,
        )
    except Exception:  # noqa: BLE001 - malformed optional ledgers fail open
        memory = {
            "source_run_id": source_run_id,
            "memory_rag": {
                "mode": None,
                "status": None,
                "candidates": [],
                "candidate_count": 0,
                "actual_launch_packages": [],
                "matched_packages": [],
                "hit": False,
            },
            "alias_events": [],
            "episode": {
                "found": False,
                "injected_lessons": [],
                "deliverable_path": None,
            },
        }
    try:
        capabilities = build_capabilities(trace_events)
    except Exception:  # noqa: BLE001 - malformed optional snapshots fail open
        capabilities = build_capabilities([])
    try:
        windowing = build_windowing(view)
    except Exception:  # noqa: BLE001 - windowed parsing must never gate a report
        windowing = {
            "present": False,
            "windowed_observations": 0,
            "peak_window_count": 0,
            "op_counts": {level: 0 for level in _WINDOW_OP_LEVELS},
            "window_types": {},
            "blocked_taps": [],
            "observations": [],
        }
    harness = build_harness_terminal(outcome, events, process_alive=process_alive)
    verdict = classify_verdict(outcome, view, harness)
    case_block = build_case_block(case)
    case_acceptance = build_case_acceptance(
        case, view, harness_finished=bool(harness.get("finished"))
    )
    findings = build_findings(view, resolver, windowing)
    recommendations = build_recommendations(findings, view, verdict)
    steps = harness.get("steps")
    if steps is None and view.run_end:
        steps = view.run_end.get("steps")
    redacted_target = (view.run_start or {}).get("task_goal_base") or target
    return {
        "run_id": run_id,
        "created_at": created_at,
        "target": redacted_target,
        "verdict": verdict,
        "run_dir": run_dir,
        "command": command or [],
        "duration_sec": duration_sec,
        "steps": steps,
        # -- the three independent judgments -----------------------------------
        "harness_terminal": harness,
        "case": case_block,
        "case_acceptance": case_acceptance,
        "diagnosis": {
            "inference": True,
            "caveat": (
                "findings/recommendations 是依据真实步骤证据的候选归因，"
                "源码 path:line 只是符号位置，不是已证根因；请对照步骤证据确认。"
            ),
        },
        # -- dimensions --------------------------------------------------------
        "run_summary": events.run_summary_block() if events is not None else None,
        "data_issues": (list(events.parse_issues) if events is not None else [])
        + list(extra_data_issues or []),
        "finish_gate": build_finish_gate(view),
        "finish_verifier": build_finish_verifier(view, events),
        "taskdoc_final": build_taskdoc_final(view),
        "context": build_context(view),
        "context_errors": build_context_errors(view, events),
        "hitl": build_hitl(view, events),
        "safety": build_safety(view, events),
        "budget": build_budget(view, events, harness),
        "tool_health": build_tool_health(view),
        "grounding": build_grounding(view),
        "visual": build_visual(view),
        "windowing": windowing,
        "model": build_model(view, events),
        "fallback": build_fallback(trace_events),
        "resolver": resolver,
        "memory": memory,
        "capabilities": capabilities,
        "replay": build_replay(view),
        "findings": findings,
        "recommendations": recommendations,
        "evidence_stream": evidence_stream,
        "trace": trace,
        "artifacts": artifacts or {},
    }


def build_recommendations(
    findings: list[dict[str, Any]], view: EvidenceView, verdict: str
) -> list[dict[str, Any]]:
    """80/20 recommendations: the top findings + verdict-driven guidance."""

    recs: list[dict[str, Any]] = []
    for idx, f in enumerate(findings[:5], start=1):
        recs.append(
            {
                "id": f"R{idx}",
                "priority": f["severity"],
                "title": f["title"],
                "recommendation": f["suggestion"],
                "target_files": f["files"],
                "verification": f["verify"],
            }
        )
    # verdict-specific top-of-list guidance.
    visual = build_visual(view)
    if visual["tool_results_with_image"] == 0 and view.observations:
        recs.insert(
            0,
            {
                "id": "R0",
                "priority": "P0",
                "title": "视觉回流断供：工具返回未携带截图",
                "recommendation": (
                    "tool_results_with_image=0——模型在纯文本 marks 摘要上盲操作。核对 "
                    "_obs.py / actuation.py 是否把截图 image 块随工具返回回流。"
                ),
                "target_files": add_line_numbers(V2_SOURCE_RULES["visual"]["files"]),
                "verification": V2_SOURCE_RULES["visual"]["verify"],
            },
        )
    return recs


__all__ = [
    "classify_verdict",
    "build_harness_terminal",
    "build_case_block",
    "build_case_acceptance",
    "build_acceptance_evidence",
    "build_safety",
    "build_budget",
    "build_context_errors",
    "build_fallback",
    "build_finish_verifier",
    "build_finish_gate",
    "build_taskdoc_final",
    "build_context",
    "build_hitl",
    "build_tool_health",
    "build_grounding",
    "build_visual",
    "build_windowing",
    "build_model",
    "build_resolver",
    "build_memory",
    "build_capabilities",
    "build_replay",
    "build_findings",
    "build_recommendations",
    "build_summary",
]
