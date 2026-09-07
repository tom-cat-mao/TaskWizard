"""WP-L lesson distillation, promotion, review, and gated-injection tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pytest

import main_v2
from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.config import V2Config
from phone_agent.v2.evolution import (
    LESSON_EVENT_TYPES,
    LessonCandidate,
    LessonStore,
    approve_if_eligible,
    distill_lessons,
    evaluate_promotion,
)
from phone_agent.v2.usage import UsageLedger


def _candidate(
    *,
    status: str = "proposed",
    text: str = "在示例应用中应该先确认页面稳定再继续",
    evidence_count: int = 3,
    task_keys: list[str] | None = None,
    conflicts: list[str] | None = None,
) -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": "les_123456789abc",
            "schema_v": 1,
            "version": 1,
            "status": status,
            "text": text,
            "scope": {
                "device": "serial-1",
                "app": "com.example.travel",
                "app_version": None,
            },
            "evidence": [
                {"run_id": f"run-{index}", "note": "episode outcome supports rule"}
                for index in range(evidence_count)
            ],
            "support_count": evidence_count,
            "task_keys": task_keys or ["open_app", "search_flight"],
            "conflicts": conflicts or [],
            "created_ts": 1.0,
            "source": "distill",
        }
    )


def _episode(
    run_id: str,
    *,
    success: bool,
    goal: str,
    reason: str,
    ts: float,
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_end": ts,
        "device_scope": "device:serial-1",
        "goal_text": goal,
        "apps": ["com.example.travel"],
        "success": success,
        "reason": reason,
    }


def _write_episodes(path: Path, episodes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes),
        encoding="utf-8",
    )


def _model_candidate(episodes: list[dict], *, text: str | None = None) -> dict:
    return {
        **_candidate(
            text=text or "在示例应用中应该等待稳定结果后再继续",
            evidence_count=len(episodes),
        ).to_dict(),
        "evidence": [
            {"run_id": item["run_id"], "note": "outcome pattern"}
            for item in episodes
        ],
        "support_count": len(episodes),
    }


def _candidate_dict(
    run_ids: list[str],
    task_keys: list[str],
    *,
    text: str = "示例规则",
) -> dict:
    return {
        "lesson_id": "les_123456789abc",
        "schema_v": 1,
        "version": 1,
        "status": "proposed",
        "text": text,
        "scope": {
            "device": "serial-1",
            "app": "com.example.travel",
            "app_version": None,
        },
        "evidence": [{"run_id": rid, "note": "outcome pattern"} for rid in run_ids],
        "support_count": len(run_ids),
        "task_keys": task_keys,
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
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


class _FakeModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list = []

    def invoke(self, messages):  # noqa: ANN001
        self.calls.append(messages)
        return AIMessage(
            content=self.response,
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
            },
        )


def test_schema_roundtrip_is_exact_and_rejects_extra_fields():
    candidate = _candidate()
    assert LessonCandidate.from_dict(candidate.to_dict()) == candidate

    invalid = {**candidate.to_dict(), "unexpected": True}
    with pytest.raises(ValueError, match="fields mismatch"):
        LessonCandidate.from_dict(invalid)


def test_event_log_rebuilds_view_and_records_review_version_chain(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    assert store.approve(candidate.lesson_id).status == "approved"
    revised = store.supersede(candidate.lesson_id, "在示例应用中应该先刷新再继续")
    assert revised.version == 2
    assert revised.status == "proposed"
    assert store.revoke(candidate.lesson_id, "规则不再适用").status == "revoked"

    events = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["type"] for item in events] == [
        "lesson_proposed",
        "lesson_approved",
        "lesson_superseded",
        "lesson_revoked",
    ]
    assert set(item["type"] for item in events) <= LESSON_EVENT_TYPES

    store.lessons_path.write_text("{broken", encoding="utf-8")
    rebuilt = LessonStore(tmp_path)
    assert rebuilt.get(candidate.lesson_id) == store.get(candidate.lesson_id)
    assert json.loads(rebuilt.lessons_path.read_text(encoding="utf-8"))[0][
        "status"
    ] == "revoked"


def test_event_replay_skips_proposal_with_non_proposed_payload(tmp_path):
    invalid = replace(_candidate(), status="approved").to_dict()
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {"type": "lesson_proposed", "schema_v": 1, "ts": 1.0, "lesson": invalid}
        )
        + "\n",
        encoding="utf-8",
    )

    assert LessonStore(tmp_path).lessons() == []


def test_repeat_identical_distill_cannot_demote_reviewed_lesson(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.approve(candidate.lesson_id)

    repeated = replace(candidate, created_ts=99.0)
    saved = store.propose(repeated)

    assert saved.status == "approved"
    assert saved.version == 1
    events = store.events_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 2


def test_changed_distill_evidence_creates_reported_proposed_revision(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    first_batch = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="打开 A", reason="finished", ts=2),
    ]
    _write_episodes(events_path, first_batch)
    first_payload = _model_candidate(first_batch)
    first_payload["task_keys"] = ["open_app"]
    first = distill_lessons(
        events_path,
        lessons_dir,
        model=_FakeModel(json.dumps([first_payload], ensure_ascii=False)),
    )
    assert first.proposed[0].version == 1

    # The watermark skips run-0/run-1, so only the newer episodes are distilled.
    second_batch = [
        _episode("run-2", success=False, goal="查询机票", reason="timeout", ts=3),
        _episode("run-3", success=True, goal="查询机票", reason="finished", ts=4),
    ]
    _write_episodes(events_path, [*first_batch, *second_batch])
    second_payload = _model_candidate(second_batch)
    second_payload["task_keys"] = ["search_flight"]
    second = distill_lessons(
        events_path,
        lessons_dir,
        model=_FakeModel(json.dumps([second_payload], ensure_ascii=False)),
    )

    assert second.proposed[0].version == 2
    assert second.proposed[0].status == "proposed"
    events = LessonStore(lessons_dir).events_path.read_text(encoding="utf-8")
    assert "lesson_superseded" in events


def test_distill_valid_json_writes_only_proposed_and_charges_distill(tmp_path):
    episodes = [
        _episode(
            "run-0", success=False, goal="打开旅行应用", reason="target_missing", ts=1
        ),
        _episode(
            "run-1", success=True, goal="打开旅行应用", reason="finished", ts=2
        ),
        _episode(
            "run-2", success=True, goal="查询机票", reason="finished", ts=3
        ),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)
    model = _FakeModel(json.dumps([_model_candidate(episodes)], ensure_ascii=False))
    ledger = UsageLedger()

    result = distill_lessons(
        events_path,
        tmp_path / "lessons",
        model=model,
        ledger=ledger,
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert len(result.proposed) == 1
    assert result.proposed[0].status == "proposed"
    assert result.tokens_by_role == {"distill": 18}
    assert "打开旅行应用" in str(model.calls)
    events = (tmp_path / "lessons/events.jsonl").read_text(encoding="utf-8")
    assert '"type": "lesson_proposed"' in events
    assert "lesson_approved" not in events
    assert json.loads((tmp_path / "lessons/distill_state.json").read_text()) == {
        "last_ts_end": 3.0
    }


@pytest.mark.parametrize(
    "response",
    ["not json", "{}", "```json\n[]\n```", '[{"a": NaN}]'],
)
def test_distill_rejects_overall_invalid_model_output(tmp_path, response):
    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="打开 B", reason="finished", ts=2),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)

    result = distill_lessons(
        events_path, tmp_path / "lessons", model=_FakeModel(response)
    )

    assert result.groups_rejected == 1
    assert result.proposed == ()


@pytest.mark.parametrize("response", ["[1]", "[{}]"])
def test_distill_skips_candidate_level_invalid_response(tmp_path, response):
    """A valid JSON array containing malformed candidates is skipped, not rejected."""

    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="打开 B", reason="finished", ts=2),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)

    result = distill_lessons(
        events_path, tmp_path / "lessons", model=_FakeModel(response)
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert result.proposed == ()


def test_distill_watermark_processes_each_episode_exactly_once(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="打开 A", reason="finished", ts=2),
    ]
    _write_episodes(events_path, episodes)
    payload = _model_candidate(episodes)
    payload["task_keys"] = ["open_app"]
    first_model = _FakeModel(json.dumps([payload], ensure_ascii=False))

    first = distill_lessons(events_path, lessons_dir, model=first_model)

    assert first.groups_considered == 1
    assert first.groups_rejected == 0
    assert len(first_model.calls) == 1
    assert "run-0" in str(first_model.calls[0])
    assert "run-1" in str(first_model.calls[0])
    assert json.loads((lessons_dir / "distill_state.json").read_text()) == {
        "last_ts_end": 2.0
    }

    second_model = _FakeModel("[]")
    second = distill_lessons(events_path, lessons_dir, model=second_model)

    assert second.groups_considered == 0
    assert second.groups_rejected == 0
    assert second.proposed == ()
    assert second_model.calls == []

    _write_episodes(
        events_path,
        [
            *episodes,
            _episode("run-2", success=True, goal="打开 A", reason="finished", ts=3),
        ],
    )
    third_model = _FakeModel("[]")
    third = distill_lessons(events_path, lessons_dir, model=third_model)

    assert third.groups_considered == 1
    assert "run-2" in str(third_model.calls[0])
    assert "run-0" not in str(third_model.calls[0])
    assert json.loads((lessons_dir / "distill_state.json").read_text()) == {
        "last_ts_end": 3.0
    }


def test_distill_without_new_episodes_writes_no_state(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, [])
    model = _FakeModel("[]")

    result = distill_lessons(events_path, tmp_path / "lessons", model=model)

    assert result.groups_considered == 0
    assert result.groups_rejected == 0
    assert result.proposed == ()
    assert result.tokens_total == 0
    assert result.tokens_by_role == {}
    assert model.calls == []
    assert not (tmp_path / "lessons/distill_state.json").exists()


def test_distill_skips_candidate_without_a_proven_citation_pattern(tmp_path):
    """A candidate whose citations do not prove an eligible pattern is skipped."""

    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="查询机票", reason="finished", ts=2),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)
    candidate = _model_candidate([episodes[0]])
    candidate["task_keys"] = ["open_app"]

    result = distill_lessons(
        events_path,
        tmp_path / "lessons",
        model=_FakeModel(json.dumps([candidate], ensure_ascii=False)),
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert result.proposed == ()


def test_distill_mixed_batch_skips_success_only_and_keeps_contrastive(tmp_path):
    """Per-candidate validation drops success-only rules but keeps eligible ones."""

    episodes = [
        _episode("run-0", success=False, goal="打开旅行应用", reason="failed", ts=1),
        _episode("run-1", success=True, goal="打开旅行应用", reason="finished", ts=2),
        _episode("run-2", success=True, goal="查询机票", reason="finished", ts=3),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)

    success_only = _candidate_dict(
        ["run-1", "run-2"],
        ["open_app", "search_flight"],
        text="仅引用成功的规则",
    )
    contrastive = _candidate_dict(
        ["run-0", "run-1", "run-2"],
        ["open_app", "search_flight"],
        text="失败早于成功的规则",
    )

    result = distill_lessons(
        events_path,
        tmp_path / "lessons",
        model=_FakeModel(json.dumps([success_only, contrastive], ensure_ascii=False)),
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert len(result.proposed) == 1
    assert result.proposed[0].text == "失败早于成功的规则"


def test_distill_repeated_failure_requires_shared_tool_prefix(tmp_path):
    """Same termination code is not enough; the failure path must look similar."""

    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="loop_fuse", ts=1),
        _episode("run-1", success=False, goal="查询机票", reason="loop_fuse", ts=2),
        _episode("run-2", success=False, goal="设置 WiFi", reason="loop_fuse", ts=3),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(
        events_path,
        [
            *episodes,
            _experience_event("run-0", 1, "launch_app"),
            _experience_event("run-0", 2, "tap"),
            _experience_event("run-0", 3, "type_text"),
            _experience_event("run-1", 1, "tap"),
            _experience_event("run-1", 2, "swipe"),
            _experience_event("run-2", 1, "long_press"),
        ],
    )
    candidate = _candidate_dict(
        ["run-0", "run-1", "run-2"],
        ["open_app", "search_flight"],
        text="同 reason 不同路径的规则",
    )

    result = distill_lessons(
        events_path,
        tmp_path / "lessons",
        model=_FakeModel(json.dumps([candidate], ensure_ascii=False)),
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert result.proposed == ()


def test_distill_repeated_failure_accepts_shared_tool_prefix(tmp_path):
    """Repeated failures with the same reason and shared tool prefix are valid."""

    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="loop_fuse", ts=1),
        _episode("run-1", success=False, goal="查询机票", reason="loop_fuse", ts=2),
        _episode("run-2", success=False, goal="设置 WiFi", reason="loop_fuse", ts=3),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(
        events_path,
        [
            *episodes,
            _experience_event("run-0", 1, "launch_app"),
            _experience_event("run-0", 2, "wait"),
            _experience_event("run-0", 3, "tap"),
            _experience_event("run-0", 4, "read_screen"),
            _experience_event("run-0", 5, "type_text"),
            _experience_event("run-1", 1, "launch_app"),
            _experience_event("run-1", 2, "tap"),
            _experience_event("run-1", 3, "type_text"),
            _experience_event("run-2", 1, "launch_app"),
            _experience_event("run-2", 2, "tap"),
            _experience_event("run-2", 3, "type_text"),
        ],
    )
    candidate = _candidate_dict(
        ["run-0", "run-1", "run-2"],
        ["open_app", "search_flight"],
        text="同 reason 同路径的规则",
    )

    result = distill_lessons(
        events_path,
        tmp_path / "lessons",
        model=_FakeModel(json.dumps([candidate], ensure_ascii=False)),
    )

    assert result.groups_considered == 1
    assert result.groups_rejected == 0
    assert len(result.proposed) == 1
    assert result.proposed[0].text == "同 reason 同路径的规则"


def test_distill_system_prompt_requires_failure_anchored_evidence():
    from phone_agent.v2.evolution import _build_distill_messages

    messages = _build_distill_messages([], {})
    system = messages[0].content
    assert "候选的 evidence 必须锚定失败" in system
    assert "仅引用成功 run 的候选会被丢弃" in system


def test_distill_budget_rejects_before_model_call(tmp_path):
    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="查询机票", reason="finished", ts=2),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, episodes)
    model = _FakeModel("[]")

    result = distill_lessons(
        events_path, tmp_path / "lessons", model=model, token_budget=1
    )

    assert result.groups_rejected == 1
    assert result.tokens_total == 0
    assert model.calls == []


def test_distill_prompt_shows_full_goal_and_steps_ledger(tmp_path):
    goal = "查询十月二日上海飞桃仙的最低价机票"
    episodes = [
        _episode("run-0", success=False, goal=goal, reason="timeout", ts=1),
        _episode("run-1", success=True, goal=goal, reason="finished", ts=2),
    ]
    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(
        events_path,
        [
            *episodes,
            {
                "type": "experience_event",
                "schema_v": 1,
                "run_id": "run-0",
                "step": 2,
                "ts": 0.5,
                "tool": "launch_app",
                "result_class": "ok",
                "app_package": "com.example.travel",
                "device_scope": "device:serial-1",
                "intent": "打开旅行应用",
                "note": "首页已加载",
            },
            {
                "type": "experience_event",
                "schema_v": 1,
                "run_id": "run-0",
                "step": 1,
                "ts": 0.4,
                "tool": "read_screen",
                "result_class": "ok",
                "app_package": None,
                "device_scope": "device:serial-1",
                "intent": "确认当前页面",
                "note": None,
            },
            {
                "type": "experience_event",
                "schema_v": 1,
                "run_id": "run-1",
                "step": 1,
                "ts": 1.5,
                "tool": "tap",
                "result_class": "error",
                "app_package": None,
                "device_scope": "device:serial-1",
                "intent": "点击搜索",
                "note": None,
            },
        ],
    )
    candidate = _model_candidate(episodes, text="查询机票前应该先等待搜索页加载完成")
    candidate["task_keys"] = ["search_flight"]
    model = _FakeModel(json.dumps([candidate], ensure_ascii=False))

    result = distill_lessons(
        events_path, tmp_path / "lessons", model=model
    )

    prompt = json.loads(str(model.calls[0][-1].content).split("\n", 1)[1])
    assert result.groups_rejected == 0
    assert len(result.proposed) == 1
    assert [row["run_id"] for row in prompt] == ["run-0", "run-1"]
    assert prompt[0]["goal"] == goal
    assert prompt[0]["task_key"] == "search_flight"
    assert prompt[0]["steps_ledger"] == [
        {
            "step": 1,
            "tool": "read_screen",
            "result_class": "ok",
            "app_package": None,
            "intent": "确认当前页面",
            "note": None,
        },
        {
            "step": 2,
            "tool": "launch_app",
            "result_class": "ok",
            "app_package": "com.example.travel",
            "intent": "打开旅行应用",
            "note": "首页已加载",
        },
    ]
    assert prompt[1]["steps_ledger"] == [
        {
            "step": 1,
            "tool": "tap",
            "result_class": "error",
            "app_package": None,
            "intent": "点击搜索",
            "note": None,
        }
    ]


@pytest.mark.parametrize(
    ("evidence_count", "task_keys", "conflicts", "eligible"),
    [
        (3, ["open_app", "search_flight"], [], True),
        (2, ["open_app", "search_flight"], [], False),
        (3, ["open_app"], [], False),
        (3, ["open_app", "search_flight"], ["model_conflict"], False),
    ],
)
def test_rule_of_three_boundary_matrix(
    evidence_count, task_keys, conflicts, eligible
):
    candidate = _candidate(
        evidence_count=evidence_count,
        task_keys=task_keys,
        conflicts=conflicts,
    )
    episodes = [
        _episode(
            f"run-{index}",
            success=bool(index),
            goal="打开应用" if index != 2 else "查询机票",
            reason="failed" if index == 0 else "finished",
            ts=index + 1,
        )
        for index in range(evidence_count)
    ]

    evaluation = evaluate_promotion(candidate, episodes)

    assert evaluation.eligible is eligible
    assert evaluation.candidate.status == "proposed"
    assert bool(evaluation.candidate.conflicts) is (not eligible)


def test_same_scope_opposite_approved_lesson_blocks_promotion():
    candidate = _candidate(text="在示例应用中应该先确认页面稳定再继续")
    approved = replace(
        candidate,
        lesson_id="les_abcdef123456",
        status="approved",
        text="在示例应用中不应该先确认页面稳定再继续",
    )
    episodes = [
        _episode(
            f"run-{index}",
            success=bool(index),
            goal="打开应用" if index != 2 else "查询机票",
            reason="failed" if index == 0 else "finished",
            ts=index + 1,
        )
        for index in range(3)
    ]

    evaluation = evaluate_promotion(
        candidate, episodes, approved_lessons=[approved]
    )

    assert not evaluation.eligible
    assert evaluation.reasons == ("approved_conflict:les_abcdef123456@v1",)


def test_human_approve_and_revoke_write_events(tmp_path):
    store = LessonStore(tmp_path / "lessons")
    candidate = store.propose(_candidate())
    episodes = [
        _episode(
            f"run-{index}",
            success=bool(index),
            goal="打开应用" if index != 2 else "查询机票",
            reason="failed" if index == 0 else "finished",
            ts=index + 1,
        )
        for index in range(3)
    ]

    assert approve_if_eligible(store, candidate.lesson_id, episodes).status == "approved"
    assert store.revoke(candidate.lesson_id, "人工撤销").status == "revoked"
    raw = store.events_path.read_text(encoding="utf-8")
    assert "lesson_approved" in raw
    assert "lesson_revoked" in raw


@pytest.mark.parametrize("memory_rag", ["shadow", "off"])
def test_lessons_never_enter_actor_initial_messages_when_injection_is_off(
    tmp_path, memory_rag
):
    secret_lesson = "LESSON_MUST_NEVER_REACH_ACTOR_CONTEXT"
    store = LessonStore(tmp_path / "lessons")
    candidate = store.propose(_candidate(text=secret_lesson))
    store.approve(candidate.lesson_id)
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent._system_prompt = "base system prompt"
    agent.config = SimpleNamespace(
        memory_rag=memory_rag,
        lessons_dir=str(tmp_path / "lessons"),
        lesson_inject_max=3,
        lesson_inject_tokens=800,
    )
    agent.session = SimpleNamespace(
        observe=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    agent._trace = SimpleNamespace(record_event=lambda *_args, **_kwargs: None)
    agent._prepare_lesson_injection("device:serial-1")

    messages = agent._initial_messages("ordinary task")
    rendered = " ".join(str(message.content) for message in messages)
    assert secret_lesson not in rendered

    root = Path(__file__).resolve().parents[2]
    actor_sources = [
        root / "phone_agent/v2/agent.py",
        root / "phone_agent/v2/prompts.py",
        *sorted((root / "phone_agent/v2/middleware").glob("*.py")),
        *sorted((root / "phone_agent/v2/tools").glob("*.py")),
    ]
    forbidden = ("LessonStore", "load_lessons", "lessons.json", "lesson_proposed")
    for source in actor_sources:
        source_text = source.read_text(encoding="utf-8")
        assert not any(token in source_text for token in forbidden), source


@pytest.mark.parametrize("status", ["proposed", "revoked"])
def test_unapproved_lessons_never_enter_actor_initial_messages_when_on(
    tmp_path, status
):
    secret_lesson = "UNAPPROVED_LESSON_MUST_NEVER_REACH_ACTOR_CONTEXT"
    store = LessonStore(tmp_path / "lessons")
    candidate = store.propose(_candidate(text=secret_lesson))
    if status == "revoked":
        store.revoke(candidate.lesson_id, "not trusted")

    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent._system_prompt = "base system prompt"
    agent.config = SimpleNamespace(
        memory_rag="on",
        lessons_dir=str(tmp_path / "lessons"),
        lesson_inject_max=3,
        lesson_inject_tokens=800,
    )
    agent.session = SimpleNamespace(
        observe=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    agent._trace = SimpleNamespace(record_event=lambda *_args, **_kwargs: None)

    agent._prepare_lesson_injection("device:serial-1")
    rendered = " ".join(
        str(message.content) for message in agent._initial_messages("ordinary task")
    )

    assert secret_lesson not in rendered


def test_evolution_config_defaults_and_env(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_EVOLUTION", raising=False)
    monkeypatch.delenv("PHONE_AGENT_LESSONS_DIR", raising=False)
    monkeypatch.delenv("PHONE_AGENT_LESSON_INJECT_MAX", raising=False)
    monkeypatch.delenv("PHONE_AGENT_LESSON_INJECT_TOKENS", raising=False)
    default = V2Config.from_env()
    assert default.evolution_mode == "manual"
    assert default.lessons_dir == "memory/lessons"
    assert default.memory_rag == "shadow"
    assert default.lesson_inject_max == 3
    assert default.lesson_inject_tokens == 800

    monkeypatch.setenv("PHONE_AGENT_EVOLUTION", "off")
    monkeypatch.setenv("PHONE_AGENT_LESSONS_DIR", "/tmp/example-lessons")
    configured = V2Config.from_env()
    assert configured.evolution_mode == "off"
    assert configured.lessons_dir == "/tmp/example-lessons"


def test_distill_cli_refuses_when_evolution_is_off(monkeypatch, capsys):
    config = SimpleNamespace(evolution_mode="off")
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)

    assert main_v2.main(["--distill"]) == 1
    assert "PHONE_AGENT_EVOLUTION=off" in capsys.readouterr().err


def test_distill_cli_wires_offline_paths_model_and_budget(tmp_path, monkeypatch):
    config = SimpleNamespace(
        evolution_mode="manual",
        experience_dir=str(tmp_path / "experience"),
        lessons_dir=str(tmp_path / "lessons"),
        memory_dir=str(tmp_path / "memory"),
        token_budget=321,
    )
    fake_model = object()
    calls = []
    fake_result = SimpleNamespace(to_dict=lambda: {"proposed": []})
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)
    monkeypatch.setattr(
        "phone_agent.v2.evolution.build_distill_model", lambda value: fake_model
    )
    monkeypatch.setattr(
        "phone_agent.v2.evolution.distill_lessons",
        lambda events, lessons, **kwargs: calls.append((events, lessons, kwargs))
        or fake_result,
    )

    assert main_v2.main(["--distill"]) == 0
    assert calls == [
        (
            str(tmp_path / "experience/events.jsonl"),
            str(tmp_path / "lessons"),
            {
                "model": fake_model,
                "token_budget": 321,
                "appkb_dir": str(tmp_path / "memory"),
            },
        )
    ]


def test_noninteractive_review_cli_writes_approve_revoke_supersede(
    tmp_path, monkeypatch
):
    lessons_dir = tmp_path / "lessons"
    experience_dir = tmp_path / "experience"
    store = LessonStore(lessons_dir)
    candidate = store.propose(_candidate())
    episodes = [
        _episode(
            f"run-{index}",
            success=bool(index),
            goal="打开应用" if index != 2 else "查询机票",
            reason="failed" if index == 0 else "finished",
            ts=index + 1,
        )
        for index in range(3)
    ]
    _write_episodes(experience_dir / "events.jsonl", episodes)
    config = SimpleNamespace(
        lessons_dir=str(lessons_dir),
        experience_dir=str(experience_dir),
        evolution_mode="manual",
    )
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)

    assert main_v2.main(["--approve-lesson", candidate.lesson_id]) == 0
    assert main_v2.main(["--revoke-lesson", candidate.lesson_id, "人工撤销"]) == 0
    assert (
        main_v2.main(
            ["--supersede-lesson", candidate.lesson_id, "修订后的行为规则"]
        )
        == 0
    )
    current = LessonStore(lessons_dir).get(candidate.lesson_id)
    assert current is not None
    assert current.version == 2
    assert current.status == "proposed"


def test_interactive_review_cli_revokes_proposal(tmp_path, monkeypatch):
    lessons_dir = tmp_path / "lessons"
    candidate = LessonStore(lessons_dir).propose(_candidate(evidence_count=1))
    config = SimpleNamespace(
        lessons_dir=str(lessons_dir),
        experience_dir=str(tmp_path / "experience"),
        evolution_mode="manual",
    )
    answers = iter(["revoke", "证据不可靠"])
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert main_v2.main(["--review-lessons"]) == 0
    assert LessonStore(lessons_dir).get(candidate.lesson_id).status == "revoked"
