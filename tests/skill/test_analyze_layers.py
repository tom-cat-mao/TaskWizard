"""Analyzer contract: three independent layers, honest evidence scope."""

from __future__ import annotations

import json
from pathlib import Path

from analyze import build_summary
from case import Acceptance, Case
from evidence import EvidenceView, read_evidence
from events import RunnerEventsView
from synthetic import write_synthetic_run


def _views(run_dir: Path):
    events = RunnerEventsView.load(run_dir)
    evidence_path = run_dir / "evidence.jsonl"
    evidence = read_evidence(evidence_path)
    return events, EvidenceView.from_events(evidence)


def _summary(events, view, case=None, **kw):
    return build_summary({}, view, run_id="t1", created_at="t", target="g", case=case, events=events, **kw)


def _append(evidence_path: Path, event: dict) -> None:
    with evidence_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def test_finished_does_not_imply_case_pass(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="从未出现的证据", match=("NEVER-OBSERVED",)),),
    )
    summary = _summary(events, view, case)
    assert summary["harness_terminal"]["finished"] is True
    assert summary["case_acceptance"]["overall"] == "unknown"
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_goal_only_does_not_self_prove():
    """A goal that literally contains the match word must not pass A1."""
    view = EvidenceView(events=[])
    events = RunnerEventsView(
        events=[{"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}}],
        run_json={"status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}},
    )
    case = Case(
        id="c1",
        title="t",
        goal="确认 Wi-Fi 已开启",
        acceptance=(Acceptance(id="A1", description="Wi-Fi 已开启", match=("Wi-Fi 已开启",)),),
    )
    summary = _summary(events, view, case)
    assert summary["case_acceptance"]["overall"] == "unknown"
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_intent_and_note_do_not_self_prove(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    evidence_path = run_dir / "evidence.jsonl"
    _append(
        evidence_path,
        {
            "event": "tool_observation",
            "step": 9,
            "tool": "tap",
            "latency_ms": 1,
            "result_text": "OK. tapped",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
    )
    _append(
        evidence_path,
        {
            "event": "tool_invoke",
            "step": 9,
            "tool": "tap",
            "args": {"intent": "Wi-Fi 已开启", "note": "Wi-Fi 已开启"},
        },
    )
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("Wi-Fi 已开启",)),),
    )
    summary = _summary(events, view, case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_failure_receipt_with_match_word_is_not_objective(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    evidence_path = run_dir / "evidence.jsonl"
    _append(
        evidence_path,
        {
            "event": "tool_observation",
            "step": 9,
            "tool": "locate",
            "latency_ms": 1,
            "result_text": "未定位: Wi-Fi 已开启（未找到）",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
    )
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("Wi-Fi 已开启",)),),
    )
    summary = _summary(events, view, case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_taskdoc_note_is_candidate_only(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    evidence_path = run_dir / "evidence.jsonl"
    _append(
        evidence_path,
        {
            "event": "taskdoc_snapshot",
            "step": 9,
            "goal_base": "g",
            "amendments": [],
            "items": [
                {"id": "s1", "content": "做某事", "status": "completed", "reason": None, "evidence_note": "Wi-Fi 已开启"}
            ],
            "facts": [],
            "open_item_count": 0,
        },
    )
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("Wi-Fi 已开启",)),),
    )
    summary = _summary(events, view, case)
    cp = summary["case_acceptance"]["checkpoints"][0]
    assert cp["status"] == "unknown"
    assert cp.get("note")


def test_objective_observation_can_pass(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="settings", match=("com.android.settings",)),),
    )
    summary = _summary(events, view, case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "met"


def test_match_and_contradiction_conflict_is_unknown(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(
            Acceptance(id="A1", description="d", match=("com.android.settings",), contradict=("设置",)),
        ),
    )
    summary = _summary(events, view, case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_counter_evidence_marks_checkpoint_unmet(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(
            Acceptance(id="A1", description="d", match=("NEVER",), contradict=("com.android.settings",)),
        ),
    )
    summary = _summary(events, view, case)
    cp = summary["case_acceptance"]["checkpoints"][0]
    assert cp["status"] == "unmet"
    assert summary["case_acceptance"]["overall"] == "fail"


def test_single_tool_error_is_not_whole_run_failure(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    _append(
        run_dir / "evidence.jsonl",
        {
            "event": "tool_observation",
            "step": 5,
            "tool": "tap",
            "latency_ms": 5,
            "result_text": "未定位: 找不到目标",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
    )
    events, view = _views(run_dir)
    summary = _summary(events, view)
    assert summary["verdict"] == "success"
    assert summary["tool_health"]["total_errors"] == 1
    assert summary["harness_terminal"]["finished"] is True


def test_missing_usage_is_null_not_zero():
    view = RunnerEventsView(
        events=[
            {"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}},
            {"event": "model_call", "step": 1, "requested_model": "m", "actual_model": None},
        ],
    )
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    usage = summary["model"]["token_usage"]
    assert usage["reported"] is False
    assert usage["input_tokens"] is None
    assert usage["total_tokens"] is None
    assert summary["model"]["actual_models"] == []


def test_partial_usage_keeps_null_total():
    view = RunnerEventsView(
        events=[{"event": "model_call", "step": 1, "input_tokens": 10, "output_tokens": None}],
    )
    usage = view.usage_totals()
    assert usage["reported"] is True
    assert usage["partial"] is True
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None


def test_stop_request_is_separate_from_terminal(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    events.control = [{"type": "stop"}]
    summary = _summary(events, view)
    assert summary["harness_terminal"]["stop_requested"] is True
    assert summary["harness_terminal"]["run_end_seen"] is True
    assert summary["harness_terminal"]["state"] == "succeeded"


def test_stopped_run_maps_to_stopped_state():
    view = RunnerEventsView(
        events=[
            {
                "event": "run_end",
                "status": "takeover",
                "result": {"success": False, "reason": "用户从 Web 控制台停止", "steps": 2},
            }
        ],
    )
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    assert summary["harness_terminal"]["state"] == "stopped"
    assert summary["verdict"] == "stopped"


def test_run_json_without_run_end_is_not_terminal():
    view = RunnerEventsView(
        events=[{"event": "model_call", "step": 3}],
        run_json={"status": "succeeded", "result": {"success": True, "reason": "done", "steps": 3}},
    )
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    ht = summary["harness_terminal"]
    assert ht["run_end_seen"] is False
    assert ht["run_summary_present"] is True
    assert ht["state"] == "unknown_terminated"
    assert summary["verdict"] == "uncertain"


def test_explicit_failed_state_is_failed():
    view = RunnerEventsView(
        events=[{"event": "run_end", "status": "failed", "result": {"success": False, "reason": "model_stopped", "steps": 2}}],
    )
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    assert summary["harness_terminal"]["state"] == "failed"
    assert summary["verdict"] == "failed"


def test_running_has_steps_from_events():
    view = RunnerEventsView(events=[{"event": "model_call", "step": 4}])
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    assert summary["harness_terminal"]["state"] == "running"
    assert summary["harness_terminal"]["steps"] == 4


def test_unresolved_hitl_not_auto_approved():
    view = RunnerEventsView(
        events=[
            {"event": "run_end", "status": "failed", "result": {"success": False, "reason": "model_stopped", "steps": 1}},
            {"event": "pending_hitl", "step": 1, "prompt": "是否继续？"},
        ],
    )
    assert view.unresolved_hitl() == ["是否继续？"]
    summary = build_summary({}, EvidenceView(events=[]), run_id="t1", created_at="t", target="g", events=view)
    assert summary["hitl"]["unresolved_prompts"] == ["是否继续？"]


def test_unresolved_hitl_only_cleared_by_null():
    """first -> null -> second with one submitted answer must keep second open."""
    events = [
        {"event": "pending_hitl", "step": 1, "prompt": "first?"},
        {"event": "pending_hitl", "step": 1, "prompt": None},
        {"event": "pending_hitl", "step": 2, "prompt": "second?"},
    ]
    view = RunnerEventsView(events=events, control=[{"type": "hitl", "answer": "reply-to-first"}])
    assert view.unresolved_hitl() == ["second?"]
    state = view.hitl_state()
    assert state["consumed_count"] == 1
    assert state["submitted_count"] == 1
    assert state["unconsumed_count"] == 0


def test_submitted_but_unconsumed_hitl():
    view = RunnerEventsView(
        events=[{"event": "pending_hitl", "step": 1, "prompt": "q?"}],
        control=[{"type": "hitl", "answer": "a"}],
    )
    state = view.hitl_state()
    assert state["unresolved_prompts"] == ["q?"]
    assert state["unconsumed_count"] == 1


def test_verifier_unknown_without_authoritative_audit(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    summary = _summary(events, view)
    fv = summary["finish_verifier"]
    assert fv["review_packets"] == 1 and fv["confirmed"] == 1
    assert fv["verifier_status"] == "unknown"
    assert fv["verifier_status_source"] == "no_authoritative_audit"


def test_verifier_fail_from_in_band_rejection(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    _append(
        run_dir / "evidence.jsonl",
        {
            "event": "tool_observation",
            "step": 6,
            "tool": "finish",
            "latency_ms": 1,
            "result_text": "验收未通过：缺少屏幕证据",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
    )
    events, view = _views(run_dir)
    summary = _summary(events, view)
    fv = summary["finish_verifier"]
    assert fv["verifier_status"] == "fail"
    assert fv["verifier_status_source"] == "in_band_rejection"


def test_verifier_persisted_status_surface():
    from analyze import build_finish_verifier

    events = RunnerEventsView(
        events=[{"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}}],
        run_json={"status": "succeeded", "result": {"success": True}, "finish_verifier": "skipped"},
    )
    fv = build_finish_verifier(EvidenceView(events=[]), events)
    assert fv["verifier_status"] == "skipped"
    assert fv["verifier_status_source"] == "persisted"


def test_budget_distinguishes_visible_usage_from_ledger(tmp_path):
    run_dir = write_synthetic_run(tmp_path)
    events, view = _views(run_dir)
    summary = _summary(events, view)
    budget = summary["budget"]
    assert budget["visible_used_tokens"] == 800
    assert budget["visible_usage_reported"] is True
    assert budget["ledger_available"] is False
    assert budget["ledger_used_tokens"] is None


def test_reference_and_failed_obs_are_not_objective():
    events = [
        {
            "event": "tool_observation",
            "step": 1,
            "tool": "read_screen",
            "latency_ms": 1,
            "result_text": "[OBS] (re-observation failed: REF-TOKEN present)",
            "obs": None,
            "image": {"present": True, "reference": "3", "screen_seq": None, "bytes": 10},
            "error": None,
        }
    ]
    view = EvidenceView.from_events(events)
    case = Case(
        id="c1",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("REF-TOKEN",)),),
    )
    summary = build_summary({}, view, run_id="t1", created_at="t", target="g", case=case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_usage_coverage_is_per_call_not_union():
    view = RunnerEventsView(
        events=[
            {"event": "model_call", "step": 1, "input_tokens": 10, "output_tokens": None},
            {"event": "model_call", "step": 2, "input_tokens": None, "output_tokens": 5},
        ],
    )
    usage = view.usage_totals()
    assert usage["partial"] is True
    assert usage["total_tokens"] is None
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["coverage"]["input_tokens_calls"] == 1
    assert usage["coverage"]["output_tokens_calls"] == 1
    assert usage["coverage"]["calls"] == 2


def test_usage_cache_unknown_never_reads_as_complete():
    view = RunnerEventsView(
        events=[
            {"event": "model_call", "step": 1, "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 3},
            {"event": "model_call", "step": 2, "input_tokens": 1, "output_tokens": 1},
        ],
    )
    usage = view.usage_totals()
    assert usage["cache_read_tokens"] is None  # not complete across calls
    assert usage["coverage"]["cache_read_tokens_calls"] == 1


def test_action_prefix_with_target_name_is_not_objective():
    # The success prefix echoes the target, but the real [OBS] segment does not.
    events = [
        {
            "event": "tool_observation",
            "step": 1,
            "tool": "tap",
            "latency_ms": 1,
            "result_text": "OK. tapped 设置\n[OBS] app=com.other screen#1\nmarks (0):",
            "obs": {"current_app": "com.other", "screen_seq": 1, "mark_count": 0},
            "image": {"present": True, "screen_seq": 1, "bytes": 10},
            "error": None,
        }
    ]
    view = EvidenceView.from_events(events)
    case = Case(id="c", title="t", goal="g", acceptance=(Acceptance(id="A1", description="d", match=("设置",)),))
    summary = build_summary({}, view, run_id="t1", created_at="t", target="g", case=case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"


def test_reference_frame_action_receipt_is_not_objective():
    events = [
        {
            "event": "tool_observation",
            "step": 1,
            "tool": "tap",
            "latency_ms": 1,
            "result_text": "OK. tapped 设置",
            "obs": None,
            "image": {"present": True, "reference": "7", "screen_seq": None, "bytes": 10},
            "error": None,
        }
    ]
    view = EvidenceView.from_events(events)
    case = Case(id="c", title="t", goal="g", acceptance=(Acceptance(id="A1", description="d", match=("设置",)),))
    summary = build_summary({}, view, run_id="t1", created_at="t", target="g", case=case)
    assert summary["case_acceptance"]["checkpoints"][0]["status"] == "unknown"
