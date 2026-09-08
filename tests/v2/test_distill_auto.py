"""WP-DISTILL-AUTO (package A): the harness stops judging, the model does.

Scope: the removed failure anchor, the slim candidate contract with
harness-filled bookkeeping, unified self-grading for rules and procedure cards,
mechanical ``struggle_markers``, and the widened injection gate.  Every test
here is behavioral — no test reaches for a private field that could not equally
be observed through a real batch.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from langchain_core.messages import AIMessage
import pytest

from phone_agent.v2.evolution import (
    LessonStore,
    _build_distill_messages,
    _candidate_from_model_dict,
    _struggle_markers,
    distill_lessons,
    lesson_injectable,
    make_lesson_id,
    select_lessons_for_injection,
)

APP = "com.example.travel"


def _episode(
    run_id: str,
    *,
    goal: str,
    ts: float,
    success: bool = True,
    reason: str = "finished",
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_end": ts,
        "device_scope": "device:serial-1",
        "goal_text": goal,
        "apps": [APP],
        "success": success,
        "reason": reason,
    }


def _experience_event(
    run_id: str,
    step: int,
    tool: str,
    result_class: str = "ok",
) -> dict:
    return {
        "type": "experience_event",
        "schema_v": 1,
        "run_id": run_id,
        "step": step,
        "ts": float(step),
        "tool": tool,
        "result_class": result_class,
        "app_package": None,
        "device_scope": "device:serial-1",
        "intent": None,
        "note": None,
    }


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
    )


def _slim_rule(
    run_ids: list[str],
    task_keys: list[str],
    *,
    text: str = "查询前应等待搜索页加载完成",
    **overrides: object,
) -> dict:
    """The distiller's contract: semantics only, no bookkeeping fields."""

    payload: dict = {
        "text": text,
        "kind": "rule",
        "evidence": [{"run_id": run_id} for run_id in run_ids],
        "scope": {"device": "serial-1", "app": APP},
        "task_keys": task_keys,
    }
    payload.update(overrides)
    return payload


def _grading_reply(grade: str, basis: str = "事实单支持"):
    """Grade every candidate in the grading prompt with ``grade``."""

    def build(messages) -> str:  # noqa: ANN001
        payload = json.loads(str(messages[-1].content).split("\n", 1)[1])
        return json.dumps(
            {
                "grades": [
                    {"lesson_id": item["lesson_id"], "grade": grade, "basis": basis}
                    for item in payload["candidates"]
                ]
            },
            ensure_ascii=False,
        )

    return build


class _ScriptedModel:
    """Return one response per call; a callable response sees the messages."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages):  # noqa: ANN001
        index = len(self.calls)
        self.calls.append(messages)
        if index >= len(self.responses):
            raise AssertionError("unexpected extra model call")
        response = self.responses[index]
        if callable(response):
            response = response(messages)
        return AIMessage(
            content=response,
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
            },
        )


def _grading_payload(model: _ScriptedModel) -> dict:
    return json.loads(str(model.calls[1][-1].content).split("\n", 1)[1])


def _batch_with_errors() -> list[dict]:
    """Every run succeeds, yet every run carries error receipts."""

    return [
        _episode("run-0", goal="查询机票", ts=1),
        _experience_event("run-0", 1, "launch_app"),
        _experience_event("run-0", 2, "tap", "error"),
        _experience_event("run-0", 3, "tap"),
        _episode("run-1", goal="预订酒店", ts=2),
        _experience_event("run-1", 1, "launch_app"),
        _experience_event("run-1", 2, "type_text", "error"),
        _experience_event("run-1", 3, "tap"),
    ]


# --- A1: the failure anchor is gone ---------------------------------------


def test_success_only_rule_with_error_receipts_reaches_self_grading(tmp_path):
    """No mechanical anchor: a rule cited only by successful runs survives."""

    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_slim_rule(["run-0", "run-1"], ["search_flight", "search_hotel"])]},
            ensure_ascii=False,
        ),
        _grading_reply("needs_review", "只有成功 run 支持"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert len(model.calls) == 2
    assert [(item.kind, item.status) for item in result.proposed] == [
        ("rule", "needs_review")
    ]
    sheet = next(iter(_grading_payload(model)["fact_sheets"].values()))
    # The evidence load the grader weighs instead of the removed anchor.
    assert sheet["error_receipts_per_run"] == {"run-0": 1, "run-1": 1}
    assert sheet["evidence_outcomes"] == [
        {"run_id": "run-0", "success": True, "reason": "finished"},
        {"run_id": "run-1", "success": True, "reason": "finished"},
    ]


def test_distill_prompt_drops_the_failure_anchor_and_teaches_four_lenses():
    system = _build_distill_messages([], {})[0].content

    assert "必须锚定失败" not in system
    for lens in ("错误恢复", "弯路重走", "跨任务复现子流程", "finish 驳回史"):
        assert lens in system, lens
    # Golden example + two negative archetypes with their rejection reasons.
    assert "筛选面板不收起" in system
    assert "绑死单屏与坐标" in system
    assert "空谈型" in system
    assert "宁可输出空数组也不要凑数" in system


# --- A2: slim candidate contract ------------------------------------------


def test_slim_payload_is_accepted_and_harness_fills_bookkeeping():
    candidate = _candidate_from_model_dict(
        _slim_rule(["run-0", "run-0", "run-1"], ["search_flight"])
    )

    assert candidate.scope == {"device": "serial-1", "app": APP, "app_version": None}
    assert candidate.schema_v == 1
    assert candidate.version == 1
    assert candidate.status == "proposed"
    assert candidate.source == "distill"
    # Duplicate citations count once and support_count is the harness's number.
    assert [item["run_id"] for item in candidate.evidence] == ["run-0", "run-1"]
    assert candidate.support_count == 2
    assert candidate.created_ts > 0
    assert candidate.lesson_id == make_lesson_id(
        candidate.text, {"device": "serial-1", "app": APP, "app_version": None}
    )
    # The optional note-less evidence gets a stable note.
    assert candidate.evidence[0]["note"] == "cited by distill"


def test_legacy_full_payload_is_still_tolerated():
    candidate = _candidate_from_model_dict(
        {
            **_slim_rule(["run-0"], ["search_flight"]),
            "lesson_id": "les_deadbeefdeadbeef",
            "schema_v": 9,
            "version": 7,
            "status": "approved",
            "source": "distill",
            "support_count": 99,
            "created_ts": 1.0,
            "scope": {"device": "serial-1", "app": APP, "app_version": "1.2.3"},
        }
    )

    assert candidate.support_count == 1
    assert candidate.status == "proposed"
    assert candidate.version == 1
    assert candidate.scope["app_version"] is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"evidence": [{"run_id": "run-0"}, {"run_id": "run-1"}]}, None),
        ({"scope": {"device": "serial-1", "app": "not-a-package"}}, "package"),
        ({"scope": {"device": "serial-1", "app": None, "junk": True}}, "scope"),
        ({"text": "  "}, "text"),
        ({"kind": "workflow"}, "kind"),
        ({"unknown_field": 1}, "unexpected candidate fields"),
        ({"conflicts": ["a", "a"]}, "unique"),
    ],
)
def test_candidate_validation_boundaries(overrides, message):
    payload = _slim_rule(["run-0", "run-1"], ["search_flight"], **overrides)

    if message is None:
        assert _candidate_from_model_dict(payload).support_count == 2
        return
    with pytest.raises(ValueError, match=message):
        _candidate_from_model_dict(payload)


def test_distill_still_rejects_fabricated_citations(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_slim_rule(["run-ghost", "run-1"], ["search_flight"])]},
            ensure_ascii=False,
        ),
        _grading_reply("auto_approved"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert result.proposed == ()
    # Nothing survives validation, so there is nothing to grade.
    assert len(model.calls) == 1


def test_distill_recomputes_support_count_from_deduplicated_evidence(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    candidate = _slim_rule(
        ["run-0", "run-1", "run-0"],
        ["search_flight", "search_hotel"],
        evidence=[
            {"run_id": "run-0", "note": "第一次支付失败处"},
            {"run_id": "run-1"},
            {"run_id": "run-0"},
        ],
        support_count=3,
    )
    model = _ScriptedModel(
        json.dumps({"rules": [candidate]}, ensure_ascii=False),
        _grading_reply("auto_approved"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    saved = result.proposed[0]
    assert saved.support_count == 2
    assert saved.support_count == len(saved.evidence)
    # The model's own note survives; the evidence is rebuilt from episodes.
    assert [item["run_id"] for item in saved.evidence] == ["run-0", "run-1"]
    assert saved.evidence[0]["note"] == "success:finished"


# --- A5: unified self-grading ---------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (_grading_reply("auto_approved"), "auto_approved"),
        (_grading_reply("needs_review"), "needs_review"),
        (_grading_reply("confident!!"), "needs_review"),
        (json.dumps({"grades": []}), "needs_review"),
        ("not json at all", "needs_review"),
    ],
)
def test_rule_grading_states(tmp_path, reply, expected):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_slim_rule(["run-0", "run-1"], ["search_flight", "search_hotel"])]},
            ensure_ascii=False,
        ),
        reply,
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert [item.status for item in result.proposed] == [expected]
    assert result.groups_rejected == 0


def test_rule_grading_call_failure_is_fail_open(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())

    class _TransportDown:
        def __init__(self) -> None:
            self.calls: list = []

        def invoke(self, messages):  # noqa: ANN001
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(
                    content=json.dumps(
                        {"rules": [_slim_rule(["run-0", "run-1"], ["search_flight", "search_hotel"])]},
                        ensure_ascii=False,
                    )
                )
            raise RuntimeError("grading transport down")

    result = distill_lessons(events_path, tmp_path / "lessons", model=_TransportDown())

    assert [item.status for item in result.proposed] == ["needs_review"]
    assert result.groups_rejected == 0
    events = LessonStore(tmp_path / "lessons").events_path.read_text(encoding="utf-8")
    assert '"grade": "needs_review"' in events


def test_runtime_fact_sheet_carries_every_unified_field(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_slim_rule(["run-0", "run-1"], ["search_flight", "search_hotel"])]},
            ensure_ascii=False,
        ),
        _grading_reply("auto_approved"),
    )

    distill_lessons(events_path, tmp_path / "lessons", model=model)

    sheet = next(iter(_grading_payload(model)["fact_sheets"].values()))
    assert sheet["kind"] == "rule"
    assert sheet["support_count_verified"] == 2
    assert [item["run_id"] for item in sheet["evidence_outcomes"]] == ["run-0", "run-1"]
    assert sheet["error_receipts_per_run"] == {"run-0": 1, "run-1": 1}
    assert sheet["recurs_in_tasks"] == 2
    assert sheet["recurrence_history"] == 0
    assert sheet["conflicts_with_approved"] == []


def test_conflicting_injectable_lesson_is_reported_not_blocking(tmp_path):
    """Same scope + opposite polarity shows up as a fact, never a verdict."""

    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    approved = store.propose(
        _candidate_from_model_dict(
            _slim_rule(
                ["run-x"],
                ["search_flight"],
                text="查询前不应该等待搜索页加载完成",
                evidence=[{"run_id": "run-x"}],
                scope={"device": "serial-1", "app": APP},
            )
        )
    )
    store.approve(approved.lesson_id)

    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_slim_rule(["run-0", "run-1"], ["search_flight", "search_hotel"])]},
            ensure_ascii=False,
        ),
        _grading_reply("needs_review", "与已注入经验相反，交人审"),
    )

    result = distill_lessons(events_path, lessons_dir, model=model)

    sheet = next(iter(_grading_payload(model)["fact_sheets"].values()))
    assert sheet["conflicts_with_approved"] == [f"{approved.lesson_id}@v1"]
    # Reported, still filed — the human gate decides afterwards.
    assert [item.status for item in result.proposed] == ["needs_review"]


def test_second_batch_reports_recurrence_history(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    candidate = _slim_rule(
        ["run-0", "run-1"], ["search_flight", "search_hotel"],
        evidence=[{"run_id": "run-0"}, {"run_id": "run-1"}],
    )
    _write_events(events_path, _batch_with_errors())
    first_model = _ScriptedModel(
        json.dumps({"rules": [candidate]}, ensure_ascii=False),
        _grading_reply("needs_review"),
    )
    distill_lessons(events_path, lessons_dir, model=first_model)
    assert next(iter(_grading_payload(first_model)["fact_sheets"].values()))[
        "recurrence_history"
    ] == 0

    _write_events(
        events_path,
        [
            *_batch_with_errors(),
            _episode("run-2", goal="查询机票", ts=3),
            _episode("run-3", goal="预订酒店", ts=4),
        ],
    )
    later = dict(candidate)
    later["evidence"] = [{"run_id": "run-2"}, {"run_id": "run-3"}]
    second_model = _ScriptedModel(
        json.dumps({"rules": [later]}, ensure_ascii=False),
        _grading_reply("auto_approved", "跨批次复现"),
    )
    distill_lessons(events_path, lessons_dir, model=second_model)

    sheet = next(iter(_grading_payload(second_model)["fact_sheets"].values()))
    assert sheet["recurrence_history"] == 1


# --- A4: struggle markers --------------------------------------------------


def test_struggle_markers_from_a_synthetic_ledger():
    ledger = [
        {"step": 1, "tool": "launch_app", "result_class": "ok"},
        {"step": 2, "tool": "tap", "result_class": "error"},
        {"step": 3, "tool": "type_text", "result_class": "ok"},
        {"step": 4, "tool": "tap", "result_class": "ok"},
        {"step": 5, "tool": "launch_app", "result_class": "ok"},
        {"step": 6, "tool": "back", "result_class": "ok"},
        {"step": 7, "tool": "tap", "result_class": "error"},
        {"step": 8, "tool": "type_text", "result_class": "ok"},
        {"step": 9, "tool": "tap", "result_class": "ok"},
        {"step": 10, "tool": "launch_app", "result_class": "ok"},
        {"step": 11, "tool": "finish", "result_class": "error"},
        {"step": 12, "tool": "finish", "result_class": "ok"},
    ]

    assert _struggle_markers(ledger) == {
        "error_steps": [
            {"step": 2, "tool": "tap"},
            {"step": 7, "tool": "tap"},
            # A rejected finish is an error receipt like any other — and it is
            # also counted once in finish_rejections below.
            {"step": 11, "tool": "finish"},
        ],
        "redo_loops": [
            {
                "after_step": 6,
                "repeat_tools": ["tap", "type_text", "tap", "launch_app"],
            }
        ],
        "finish_rejections": 1,
        "steps": 12,
    }


def test_struggle_markers_are_empty_without_signal():
    ledger = [
        {"step": 1, "tool": "launch_app", "result_class": "ok"},
        {"step": 2, "tool": "back", "result_class": "ok"},
        {"step": 3, "tool": "scroll", "result_class": "ok"},
    ]

    assert _struggle_markers([]) == {
        "error_steps": [],
        "redo_loops": [],
        "finish_rejections": 0,
        "steps": 0,
    }
    # A back followed by nothing previously seen is not a redo loop.
    assert _struggle_markers(ledger)["redo_loops"] == []


def test_prompt_rows_carry_one_marker_block_per_episode(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_events(events_path, _batch_with_errors())
    model = _ScriptedModel(
        json.dumps({"rules": [], "procedures": []}, ensure_ascii=False)
    )

    distill_lessons(events_path, tmp_path / "lessons", model=model)

    rows = json.loads(str(model.calls[0][-1].content).split("\n", 1)[1])
    assert [row["run_id"] for row in rows] == ["run-0", "run-1"]
    assert rows[0]["struggle_markers"] == {
        "error_steps": [{"step": 2, "tool": "tap"}],
        "redo_loops": [],
        "finish_rejections": 0,
        "steps": 3,
    }


# --- A6: injection gate ----------------------------------------------------


def test_auto_approved_rule_injects_and_needs_review_rule_does_not(tmp_path):
    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    run_start_scope = {"device": "serial-1", "app": None}
    confident_payload = _slim_rule(
        ["run-0"], ["search_flight"], text="自信的规则", scope=run_start_scope
    )
    unsure_payload = _slim_rule(
        ["run-1"], ["search_flight"], text="拿不准的规则", scope=run_start_scope
    )
    confident = store.propose(
        replace(_candidate_from_model_dict(confident_payload), status="auto_approved")
    )
    unsure = store.propose(
        replace(_candidate_from_model_dict(unsure_payload), status="needs_review")
    )

    assert lesson_injectable(confident) is True
    assert lesson_injectable(unsure) is False
    selected = select_lessons_for_injection(
        lessons_dir, device_scope="device:serial-1", max_items=10, max_tokens=800
    )
    assert [item.lesson_id for item in selected] == [confident.lesson_id]
    assert unsure.lesson_id not in {item.lesson_id for item in selected}


def test_human_approval_still_required_to_move_past_a_verdict(tmp_path):
    """auto_approved is injectable but not approvable; needs_review is the
    reverse — the human CLI stays the only path to ``approved``."""

    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    payload = _slim_rule(
        ["run-0"], ["search_flight"], scope={"device": "serial-1", "app": None}
    )
    auto = store.propose(
        replace(_candidate_from_model_dict(payload), status="auto_approved")
    )
    with pytest.raises(ValueError, match="proposed or needs_review"):
        store.approve(auto.lesson_id)

    pending = store.propose(
        replace(
            _candidate_from_model_dict(
                _slim_rule(
                    ["run-1"], ["search_flight"], text="待审规则",
                    scope={"device": "serial-1", "app": None},
                )
            ),
            status="needs_review",
        )
    )
    assert store.approve(pending.lesson_id).status == "approved"
