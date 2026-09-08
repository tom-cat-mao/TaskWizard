"""WP-WF1: procedure-card artifact + two-call distill with self-grading.

Scope: lesson schema ``kind``/``steps``/``pitfalls``/``app_scope``, the
harness-computed fact sheet, the distiller's ``auto_approved`` / ``needs_review``
verdict, the widened status machine, and the injection gate matrix.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pytest

import main_v2
from phone_agent.v2.evolution import (
    LessonCandidate,
    LessonStore,
    distill_lessons,
    lesson_injectable,
    make_lesson_id,
    select_lessons_for_injection,
)

APP = "com.example.travel"
STEPS = ["搜索框输入目标", "选店进入", "到结算页停手问人"]


def _episode(
    run_id: str,
    *,
    success: bool,
    goal: str,
    reason: str = "finished",
    ts: float,
    apps: tuple[str, ...] = (APP,),
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_end": ts,
        "device_scope": "device:serial-1",
        "goal_text": goal,
        "apps": list(apps),
        "success": success,
        "reason": reason,
    }


def _write_episodes(path: Path, episodes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes),
        encoding="utf-8",
    )


def _rule_payload(
    run_ids: list[str],
    task_keys: list[str],
    *,
    text: str = "查询前应等待搜索页加载完成",
) -> dict:
    return {
        "lesson_id": "les_123456789abc",
        "schema_v": 1,
        "version": 1,
        "status": "proposed",
        "text": text,
        "scope": {"device": None, "app": None, "app_version": None},
        "evidence": [{"run_id": rid, "note": "outcome pattern"} for rid in run_ids],
        "support_count": len(run_ids),
        "task_keys": task_keys,
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
    }


def _procedure_payload(
    run_ids: list[str],
    task_keys: list[str],
    *,
    title: str = "旅行应用下单到结算",
    app_scope: str | None = APP,
    steps: list[str] | None = None,
    pitfalls: str | None = "开屏广告点右上角跳过",
    status: str = "proposed",
) -> dict:
    return {
        "lesson_id": "les_123456789abc",
        "schema_v": 1,
        "version": 1,
        "status": status,
        "text": title,
        "scope": {"device": None, "app": None, "app_version": None},
        "evidence": [{"run_id": rid, "note": "outcome pattern"} for rid in run_ids],
        "support_count": len(run_ids),
        "task_keys": task_keys,
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
        "kind": "procedure",
        "steps": STEPS if steps is None else steps,
        "pitfalls": pitfalls,
        "app_scope": app_scope,
    }


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


def _grading_reply(grade: str, basis: str = "三次一致成功且包名已验证"):
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


def _human_payload(messages) -> dict:  # noqa: ANN001
    return json.loads(str(messages[-1].content).split("\n", 1)[1])


def _default_batch() -> list[dict]:
    """Two task buckets, one early failure — enough for rule and procedure."""

    return [
        _episode("run-0", success=False, goal="查询机票", reason="timeout", ts=1),
        _episode("run-1", success=True, goal="查询机票", ts=2),
        _episode("run-2", success=True, goal="预订酒店", ts=3),
    ]


# --- schema ---------------------------------------------------------------


def test_old_lesson_payload_validates_with_rule_defaults():
    candidate = LessonCandidate.from_dict(_rule_payload(["run-0"], ["open_app"]))

    assert candidate.kind == "rule"
    assert candidate.steps == []
    assert candidate.pitfalls is None
    assert candidate.app_scope is None
    assert candidate.to_dict()["kind"] == "rule"
    assert set(candidate.to_dict()) >= {"kind", "steps", "pitfalls", "app_scope"}


def test_procedure_schema_roundtrip_is_exact():
    candidate = LessonCandidate.from_dict(
        _procedure_payload(["run-0", "run-1"], ["search_flight", "search_hotel"])
    )

    assert candidate.kind == "procedure"
    assert candidate.steps == STEPS
    assert candidate.app_scope == APP
    assert LessonCandidate.from_dict(candidate.to_dict()) == candidate


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            _procedure_payload(["run-0"], ["search_flight"], steps=[]),
            "steps must be a non-empty array",
        ),
        (
            _procedure_payload(["run-0"], ["search_flight"], app_scope=None),
            "app_scope must be a non-empty string",
        ),
        (
            _procedure_payload(["run-0"], ["search_flight"], app_scope="  "),
            "app_scope must be a non-empty string",
        ),
        (
            {
                **_rule_payload(["run-0"], ["search_flight"]),
                "steps": STEPS,
            },
            "rule lessons must not carry steps",
        ),
        (
            {**_rule_payload(["run-0"], ["search_flight"]), "app_scope": APP},
            "rule lessons must not carry app_scope",
        ),
        (
            {**_rule_payload(["run-0"], ["search_flight"]), "kind": "workflow"},
            "kind must be one of",
        ),
        (
            {**_rule_payload(["run-0"], ["search_flight"]), "screen_hash": "x"},
            "fields mismatch",
        ),
    ],
)
def test_procedure_schema_rejects_invalid_shapes(payload, message):
    with pytest.raises(ValueError, match=message):
        LessonCandidate.from_dict(payload)


def test_general_app_scope_is_accepted_for_cross_app_cards():
    candidate = LessonCandidate.from_dict(
        _procedure_payload(["run-0"], ["search_flight"], app_scope="general")
    )

    assert candidate.app_scope == "general"


def test_lessons_json_rebuild_carries_procedure_fields(tmp_path):
    store = LessonStore(tmp_path)
    saved = store.propose(
        LessonCandidate.from_dict(
            _procedure_payload(["run-0", "run-1"], ["search_flight", "search_hotel"])
        )
    )

    view = json.loads(store.lessons_path.read_text(encoding="utf-8"))
    assert view[0]["kind"] == "procedure"
    assert view[0]["steps"] == STEPS
    assert view[0]["pitfalls"] == "开屏广告点右上角跳过"
    assert view[0]["app_scope"] == APP

    rebuilt = LessonStore(tmp_path)
    assert rebuilt.get(saved.lesson_id) == saved


# --- two-call distill -----------------------------------------------------


def test_distill_two_calls_emit_rule_and_auto_approved_procedure(tmp_path):
    episodes = _default_batch()
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)
    first_call = {
        "rules": [_rule_payload(["run-0", "run-1", "run-2"], ["search_flight"])],
        "procedures": [
            _procedure_payload(["run-0", "run-1", "run-2"], ["search_flight", "search_hotel"])
        ],
    }
    model = _ScriptedModel(
        json.dumps(first_call, ensure_ascii=False), _grading_reply("auto_approved")
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert len(model.calls) == 2
    assert result.groups_rejected == 0
    # Every candidate is self-graded now: the rule wears the grader's verdict,
    # not a harness default of `proposed`.
    statuses = {item.kind: item.status for item in result.proposed}
    assert statuses == {"rule": "auto_approved", "procedure": "auto_approved"}
    # Both calls are charged to the distill role.
    assert result.tokens_by_role == {"distill": 36}

    events = (tmp_path / "lessons/events.jsonl").read_text(encoding="utf-8")
    assert '"fact_sheet"' in events
    assert "三次一致成功且包名已验证" in events


def test_distill_grading_prompt_carries_candidates_and_fact_sheets(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())
    model = _ScriptedModel(
        json.dumps(
            {
                "rules": [],
                "procedures": [
                    _procedure_payload(
                        ["run-1", "run-2"], ["search_flight", "search_hotel"]
                    )
                ],
            },
            ensure_ascii=False,
        ),
        _grading_reply("needs_review"),
    )

    distill_lessons(events_path, tmp_path / "lessons", model=model)

    payload = _human_payload(model.calls[1])
    procedure = next(
        item for item in payload["candidates"] if item["kind"] == "procedure"
    )
    sheet = payload["fact_sheets"][procedure["lesson_id"]]
    assert sheet["run_ids"] == ["run-1", "run-2"]
    assert sheet["task_count"] == 2
    assert sheet["outcome"] == "success"
    assert sheet["app_scope"] == APP


def test_distill_grades_a_rule_only_batch_with_the_same_second_call(tmp_path):
    """Self-grading is unified: a rule gets call 2 just like a procedure card."""

    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())
    model = _ScriptedModel(
        json.dumps(
            {"rules": [_rule_payload(["run-0", "run-1"], ["search_flight"])], "procedures": []},
            ensure_ascii=False,
        ),
        _grading_reply("needs_review", "只在单任务里出现"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert len(model.calls) == 2
    assert [(item.kind, item.status) for item in result.proposed] == [
        ("rule", "needs_review")
    ]
    grading_payload = _human_payload(model.calls[1])
    assert [item["kind"] for item in grading_payload["candidates"]] == ["rule"]
    sheet = next(iter(grading_payload["fact_sheets"].values()))
    assert sheet["kind"] == "rule"
    assert sheet["support_count_verified"] == 2


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (lambda: _procedure_payload(["run-ghost", "run-1"], ["search_flight", "search_hotel"]), "fabricated run id"),
        (lambda: _procedure_payload(["run-1", "run-2"], ["search_flight", "search_hotel"], steps=["tap(ax_3@e7)", "选店进入"]), "mark id step"),
        (lambda: _procedure_payload(["run-1", "run-2"], ["search_flight", "search_hotel"], steps=["在坐标 500,800 点击", "选店进入"]), "coordinate step"),
    ],
)
def test_distill_skips_procedure_failing_the_evidence_gate(tmp_path, payload, reason):
    """Fabricated citations or steps mechanically bound to one screen."""

    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())
    model = _ScriptedModel(
        json.dumps({"rules": [], "procedures": [payload()]}, ensure_ascii=False),
        _grading_reply("auto_approved"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert result.proposed == (), reason
    assert len(model.calls) == 1


def test_distill_keeps_single_task_procedure_for_the_grader(tmp_path):
    """Cross-task recurrence is a grader fact now, never a harness gate."""

    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())
    model = _ScriptedModel(
        json.dumps(
            {
                "rules": [],
                "procedures": [_procedure_payload(["run-0", "run-1"], ["search_flight"])],
            },
            ensure_ascii=False,
        ),
        _grading_reply("needs_review", "只在单一任务里出现"),
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert len(model.calls) == 2
    assert [(item.kind, item.status) for item in result.proposed] == [
        ("procedure", "needs_review")
    ]
    sheet = next(iter(_human_payload(model.calls[1])["fact_sheets"].values()))
    assert sheet["recurs_in_tasks"] == 1
    assert sheet["task_count"] == 1


def test_grading_prompt_states_the_asymmetric_risk_of_auto_approval():
    from phone_agent.v2.evolution import _build_grading_messages

    system = _build_grading_messages([], {})[0].content
    # The grader must know a wrong injection costs more than a slow review.
    assert "风险并不对称" in system
    assert "needs_review" in system
    assert "conflicts_with_approved" in system


# --- fact sheet -----------------------------------------------------------


def test_fact_sheet_reports_outcomes_appkb_and_prior_proposals():
    from phone_agent.v2.evolution import _procedure_fact_sheet, _verified_appkb_packages

    candidate = LessonCandidate.from_dict(
        _procedure_payload(["run-0", "run-1"], ["search_flight", "search_hotel"])
    )
    episodes = [
        _episode("run-0", success=True, goal="查询机票", ts=1),
        _episode("run-1", success=True, goal="预订酒店", ts=2),
    ]
    sheet = _procedure_fact_sheet(
        candidate,
        episodes,
        verified_packages=frozenset({APP}),
        prior_counts={},
    )
    assert sheet["outcome"] == "success"
    assert sheet["outcome_consistent"] is True
    assert sheet["outcome_success"] == 2 and sheet["outcome_failure"] == 0
    assert sheet["app_scope_verified"] is True
    assert sheet["previously_proposed"] is False

    mixed = _procedure_fact_sheet(
        candidate,
        [
            _episode("run-0", success=False, goal="查询机票", reason="timeout", ts=1),
            _episode("run-1", success=True, goal="预订酒店", ts=2),
        ],
        verified_packages=frozenset(),
        prior_counts={("旅行应用下单到结算", APP): 2},
    )
    assert mixed["outcome"] == "mixed"
    assert mixed["outcome_consistent"] is False
    assert mixed["app_scope_verified"] is False
    assert mixed["prior_proposals"] == 2
    assert mixed["previously_proposed"] is True


def test_verified_appkb_packages_reads_kb_view_and_fails_open(tmp_path):
    from phone_agent.v2.evolution import _verified_appkb_packages

    assert _verified_appkb_packages(tmp_path) == frozenset()

    root = tmp_path / "app_kb"
    root.mkdir(parents=True)
    (root / "kb.json").write_text(
        json.dumps(
            [
                {"package": APP, "success_count": 1, "stale": False},
                {"package": "com.example.dead", "success_count": 3, "stale": True},
                {"package": "com.example.unverified", "success_count": 0},
            ]
        ),
        encoding="utf-8",
    )
    assert _verified_appkb_packages(tmp_path) == frozenset({APP})


def test_second_batch_detects_previously_proposed_procedure(tmp_path):
    """Cross-batch repetition is visible in the second fact sheet."""

    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    _write_episodes(
        events_path,
        [
            _episode("run-0", success=True, goal="查询机票", ts=1),
            _episode("run-1", success=True, goal="预订酒店", ts=2),
        ],
    )
    distill_lessons(
        events_path,
        lessons_dir,
        model=_ScriptedModel(
            json.dumps(
                {
                    "rules": [],
                    "procedures": [
                        _procedure_payload(["run-0", "run-1"], ["search_flight", "search_hotel"])
                    ],
                },
                ensure_ascii=False,
            ),
            _grading_reply("needs_review"),
        ),
    )
    # A later batch (everything after the watermark) repeats the sub-process.
    _write_episodes(
        events_path,
        [
            _episode("run-0", success=True, goal="查询机票", ts=1),
            _episode("run-1", success=True, goal="预订酒店", ts=2),
            _episode("run-2", success=True, goal="查询机票", ts=3),
            _episode("run-3", success=True, goal="预订酒店", ts=4),
        ],
    )
    distill_lessons(
        events_path,
        lessons_dir,
        model=_ScriptedModel(
            json.dumps(
                {
                    "rules": [],
                    "procedures": [
                        _procedure_payload(
                            ["run-2", "run-3"],
                            ["search_flight", "search_hotel"],
                        )
                    ],
                },
                ensure_ascii=False,
            ),
            _grading_reply("auto_approved", "跨批次复现"),
        ),
    )

    events = [
        json.loads(line)
        for line in (lessons_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    graded = [item for item in events if "fact_sheet" in item]
    assert [item["fact_sheet"]["prior_proposals"] for item in graded] == [0, 1]
    assert graded[-1]["grade"] == "auto_approved"
    assert graded[-1]["fact_sheet"]["previously_proposed"] is True


# --- grading verdicts -----------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (_grading_reply("auto_approved"), "auto_approved"),
        (_grading_reply("needs_review"), "needs_review"),
        (_grading_reply("confident"), "needs_review"),
        (json.dumps({"grades": []}), "needs_review"),
        ("not json at all", "needs_review"),
    ],
)
def test_grade_mapping_including_illegal_and_unparsable(tmp_path, reply, expected):
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())
    model = _ScriptedModel(
        json.dumps(
            {
                "rules": [],
                "procedures": [
                    _procedure_payload(["run-1", "run-2"], ["search_flight", "search_hotel"])
                ],
            },
            ensure_ascii=False,
        ),
        reply,
    )

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert [item.status for item in result.proposed] == [expected]


def test_grading_call_failure_keeps_candidates_at_needs_review(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, _default_batch())

    class _FailingModel:
        def __init__(self) -> None:
            self.calls = []

        def invoke(self, messages):  # noqa: ANN001
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(
                    content=json.dumps(
                        {
                            "rules": [],
                            "procedures": [
                                _procedure_payload(
                                    ["run-1", "run-2"], ["search_flight", "search_hotel"]
                                )
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            raise RuntimeError("grading transport down")

    result = distill_lessons(events_path, tmp_path / "lessons", model=_FailingModel())

    assert [item.status for item in result.proposed] == ["needs_review"]
    assert result.groups_rejected == 0


def test_grading_is_skipped_when_budget_is_exhausted(tmp_path):
    """No grading call once the budget is gone; candidates stay needs_review."""

    from phone_agent.v2.evolution import _apply_self_grading
    from phone_agent.v2.usage import UsageLedger

    store = LessonStore(tmp_path)
    candidate = LessonCandidate.from_dict(
        _procedure_payload(["run-1", "run-2"], ["search_flight", "search_hotel"])
    )
    model = _ScriptedModel(_grading_reply("auto_approved"))
    ledger = UsageLedger()
    ledger.record("distill", estimate_tokens=5000)

    records = _apply_self_grading(
        [candidate],
        _default_batch(),
        model=model,
        ledger=ledger,
        store=store,
        token_budget=10,
        appkb_dir=None,
    )

    assert model.calls == []
    assert records[candidate.lesson_id]["grade"] == "needs_review"
    assert records[candidate.lesson_id]["fact_sheet"]["run_count"] == 2


# --- status machine + injection gate --------------------------------------


def _stored_lesson(
    store: LessonStore, *, kind: str, status: str, title: str = "旅行应用下单到结算"
) -> LessonCandidate:
    """Propose one lesson with a unique semantic id for the given title."""

    payload = (
        _procedure_payload(
            ["run-0", "run-1", "run-2"], ["search_flight", "search_hotel"], status=status, title=title
        )
        if kind == "procedure"
        else {
            **_rule_payload(["run-0", "run-1", "run-2"], ["search_flight"], text=title),
            "status": status,
        }
    )
    candidate = LessonCandidate.from_dict(payload)
    identity = candidate.scope
    if kind == "procedure":
        identity = {**identity, "app_scope": candidate.app_scope}
    return store.propose(
        replace(candidate, lesson_id=make_lesson_id(candidate.text, identity))
    )


def test_injection_gate_matrix(tmp_path):
    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    approved_rule = _stored_lesson(store, kind="rule", status="proposed", title="规则一")
    store.approve(approved_rule.lesson_id)
    auto_rule = _stored_lesson(store, kind="rule", status="auto_approved", title="规则二")
    auto_procedure = _stored_lesson(
        store, kind="procedure", status="auto_approved", title="过程卡一"
    )
    review_procedure = _stored_lesson(
        store, kind="procedure", status="needs_review", title="过程卡二"
    )

    lessons = store.lessons()
    expected = {
        approved_rule.lesson_id: True,
        # auto_approved crosses the gate for any kind now; a wrong card is
        # corrected by human CLI / dream demotion, not by a prior restraint.
        auto_rule.lesson_id: True,
        auto_procedure.lesson_id: True,
        review_procedure.lesson_id: False,
    }
    # lesson_injectable 是谓词（approved 任意 kind 放行，auto_approved 也不限 kind）；
    # rule 注入通道仍只选 kind=rule，过程卡不泄漏为单行 rule（WF3 修复）。
    assert {item.lesson_id: lesson_injectable(item) for item in lessons} == expected

    selected = select_lessons_for_injection(
        lessons_dir, device_scope="device:serial-1", max_items=10, max_tokens=800
    )
    assert {item.lesson_id for item in selected} == {
        approved_rule.lesson_id,
        auto_rule.lesson_id,
    }


def test_demote_lands_procedure_in_needs_review_and_rule_in_proposed(tmp_path):
    store = LessonStore(tmp_path)
    procedure = _stored_lesson(store, kind="procedure", status="auto_approved")
    rule = _stored_lesson(store, kind="rule", status="proposed")
    store.approve(rule.lesson_id)

    assert store.demote(procedure.lesson_id, "evidence archived").status == "needs_review"
    assert store.demote(rule.lesson_id, "evidence archived").status == "proposed"
    assert LessonStore(tmp_path).get(procedure.lesson_id).status == "needs_review"


def test_approve_rejects_auto_approved_and_accepts_needs_review(tmp_path):
    store = LessonStore(tmp_path)
    auto_procedure = _stored_lesson(
        store, kind="procedure", status="auto_approved", title="已自批的过程卡"
    )
    with pytest.raises(ValueError, match="proposed or needs_review"):
        store.approve(auto_procedure.lesson_id)

    pending = _stored_lesson(
        store, kind="procedure", status="needs_review", title="待审的过程卡"
    )
    assert store.approve(pending.lesson_id).status == "approved"


# --- CLI ------------------------------------------------------------------


def _cli_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        lessons_dir=str(tmp_path / "lessons"),
        experience_dir=str(tmp_path / "experience"),
        memory_dir=str(tmp_path / "memory"),
        evolution_mode="manual",
    )


def test_cli_approve_promotes_needs_review_procedure(tmp_path, monkeypatch):
    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    candidate = _stored_lesson(store, kind="procedure", status="needs_review")
    _write_episodes(
        tmp_path / "experience/events.jsonl",
        [
            _episode("run-0", success=True, goal="查询机票", ts=1),
            _episode("run-1", success=True, goal="查询机票", ts=2),
            _episode("run-2", success=True, goal="预订酒店", ts=3),
        ],
    )
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: _cli_config(tmp_path))

    assert main_v2.main(["--approve-lesson", candidate.lesson_id]) == 0
    assert LessonStore(lessons_dir).get(candidate.lesson_id).status == "approved"


def test_review_cli_shows_kind_steps_app_scope_and_grade_basis(
    tmp_path, monkeypatch, capsys
):
    lessons_dir = tmp_path / "lessons"
    store = LessonStore(lessons_dir)
    store.propose(
        LessonCandidate.from_dict(
            _procedure_payload(
                ["run-0", "run-1", "run-2"],
                ["search_flight", "search_hotel"],
                status="needs_review",
            )
        ),
        grade="needs_review",
        grade_basis="事实单显示结局不一致",
        fact_sheet={"run_count": 2, "outcome": "mixed"},
    )
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: _cli_config(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt: "s")

    assert main_v2.main(["--review-lessons"]) == 0

    printed = capsys.readouterr().out
    assert '"kind": "procedure"' in printed
    assert "搜索框输入目标" in printed
    assert APP in printed
    assert "事实单显示结局不一致" in printed
    assert "needs_review" in printed


def test_distill_parser_accepts_bare_run_id_evidence_and_list_pitfalls():
    """Real gateway output: models emit evidence as bare run_id strings and
    pitfalls as a list. Both must be tolerated instead of dropping the card."""
    from phone_agent.v2.evolution import LessonCandidate

    payload = {
        "lesson_id": "les_" + "a" * 16,
        "schema_v": 1,
        "version": 1,
        "status": "proposed",
        "text": "B站按UP主找最新视频并播放/记录",
        "scope": {"device": None, "app": "tv.danmaku.bili", "app_version": None},
        "evidence": ["run_a", "run_b"],
        "support_count": 2,
        "task_keys": ["open_app", "search"],
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
        "kind": "procedure",
        "app_scope": "tv.danmaku.bili",
        "steps": ["启动应用进入首页", "搜索 UP 主"],
        "pitfalls": ["坑一", "坑二"],
    }
    candidate = LessonCandidate.from_dict(payload)
    assert [e["run_id"] for e in candidate.evidence] == ["run_a", "run_b"]
    assert candidate.pitfalls == "坑一；坑二"
