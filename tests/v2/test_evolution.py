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
    approve_lesson,
    evidence_loss_reason,
    distill_lessons,
    evaluate_promotion,
    lesson_injectable,
    select_app_rules_for_injection,
    select_lessons_for_injection,
)
from phone_agent.v2.middleware._tokens import estimate_text_tokens
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


def test_changed_distill_evidence_creates_reported_graded_revision(tmp_path):
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
    # The self-grading verdict rides along with the new proposal version.
    assert second.proposed[0].status == "needs_review"
    events = LessonStore(lessons_dir).events_path.read_text(encoding="utf-8")
    assert "lesson_superseded" in events


def test_distill_valid_json_writes_only_proposal_states_and_charges_distill(tmp_path):
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
    # A rule's status is the self-grading verdict now, never a harness default:
    # the second call here is not a grade object, so it fails open.
    assert result.proposed[0].status == "needs_review"
    assert result.tokens_by_role == {"distill": 36}
    assert "打开旅行应用" in str(model.calls)
    events = (tmp_path / "lessons/events.jsonl").read_text(encoding="utf-8")
    assert '"type": "lesson_proposed"' in events
    assert "lesson_approved" not in events
    assert json.loads((tmp_path / "lessons/distill_state.json").read_text()) == {
        "last_ts_end": 3.0,
        "last_run_id": "run-2",
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
    # Two calls per batch now: propose, then self-grade the survivors.
    assert len(first_model.calls) == 2
    assert "run-0" in str(first_model.calls[0])
    assert "run-1" in str(first_model.calls[0])
    assert json.loads((lessons_dir / "distill_state.json").read_text()) == {
        "last_ts_end": 2.0,
        "last_run_id": "run-1",
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
        "last_ts_end": 3.0,
        "last_run_id": "run-2",
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
    ("evidence_count", "task_keys", "conflicts", "quiet"),
    [
        (3, ["open_app", "search_flight"], [], True),
        (2, ["open_app", "search_flight"], [], False),
        (3, ["open_app"], [], False),
        (3, ["open_app", "search_flight"], ["model_conflict"], False),
    ],
)
def test_promotion_reference_facts_matrix(evidence_count, task_keys, conflicts, quiet):
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

    # The evaluation is a reference-fact generator, never a gate.
    evaluation = evaluate_promotion(candidate, episodes)

    assert bool(evaluation.reasons) is (not quiet)
    assert evaluation.candidate.status == "proposed"
    assert bool(evaluation.candidate.conflicts) is (not quiet)


def test_evidence_loss_reason_only_fires_on_missing_cited_runs():
    candidate = _candidate(evidence_count=2)
    episodes = [
        _episode(
            f"run-{index}",
            success=bool(index),
            goal="打开应用" if index != 1 else "查询机票",
            reason="failed" if index == 0 else "finished",
            ts=index + 1,
        )
        for index in range(2)
    ]

    # Evidence intact: a support count below 3 is not evidence loss.
    assert evidence_loss_reason(candidate, episodes) is None
    assert evidence_loss_reason(candidate, episodes[:1]) == (
        "rule:verified_evidence=1<2"
    )
    assert evidence_loss_reason(candidate, []) == "rule:verified_evidence=0<2"


def test_same_scope_opposite_approved_lesson_is_reported_as_a_fact():
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

    # A contradiction is a fact for the reviewer, not a verdict.
    assert evaluation.reasons == ("approved_conflict:les_abcdef123456@v1",)


def test_human_approve_and_revoke_write_events(tmp_path):
    store = LessonStore(tmp_path / "lessons")
    candidate = store.propose(_candidate())

    # Human CLI is a correction channel, not a gate: approval succeeds even
    # with zero supporting episodes (no Rule-of-3 hard block).
    assert approve_lesson(store, candidate.lesson_id).status == "approved"
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


def test_evolution_config_env_overrides(monkeypatch):
    # Default values live in test_config.py::test_from_env_defaults.
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


# --- S2 FIX 1: the distill cursor must not turn a transient failure into a
# permanent skip -------------------------------------------------------------


class _ExplodingModel:
    """Model transport that is down: every invoke raises."""

    def invoke(self, messages):  # noqa: ANN001
        raise RuntimeError("transport down")


def _events(lessons_dir: Path) -> list[dict]:
    path = lessons_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_distill_transient_failure_is_retried_not_skipped(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    episodes = [
        _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
        _episode("run-1", success=True, goal="查询机票", reason="finished", ts=2),
    ]
    _write_episodes(events_path, episodes)

    first = distill_lessons(events_path, lessons_dir, model=_ExplodingModel())

    assert first.retried is True
    assert first.abandoned == 0
    assert first.groups_rejected == 1
    state = json.loads((lessons_dir / "distill_state.json").read_text())
    # The cursor did not move; the failed batch is recorded for the retry.
    assert state["last_ts_end"] == 0.0
    assert state["last_run_id"] == ""
    assert state["failed_batch"] == {
        "run_ids": ["run-0", "run-1"],
        "attempts": 1,
        "last_error": "RuntimeError",
    }
    assert not [
        item for item in _events(lessons_dir) if "distill_batch" in item["type"]
    ]

    payload = _model_candidate(episodes)
    payload["task_keys"] = ["open_app"]
    good = _FakeModel(json.dumps([payload], ensure_ascii=False))
    second = distill_lessons(events_path, lessons_dir, model=good)

    # The same batch is reprocessed by the next distill run.
    assert second.retried is False
    assert second.groups_rejected == 0
    assert len(good.calls) == 2
    assert "run-0" in str(good.calls[0])
    assert "run-1" in str(good.calls[0])
    state = json.loads((lessons_dir / "distill_state.json").read_text())
    assert state == {"last_ts_end": 2.0, "last_run_id": "run-1"}


def test_distill_three_strikes_abandons_batch_with_explicit_event(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    _write_episodes(
        events_path,
        [
            _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
            _episode("run-1", success=True, goal="查询机票", reason="finished", ts=2),
        ],
    )

    first = distill_lessons(events_path, lessons_dir, model=_ExplodingModel())
    second = distill_lessons(events_path, lessons_dir, model=_ExplodingModel())
    assert first.retried is True
    assert second.retried is True
    third = distill_lessons(events_path, lessons_dir, model=_ExplodingModel())

    # The third strike advances the cursor and makes the loss audible.
    assert third.abandoned == 1
    assert third.retried is False
    abandoned = [
        item
        for item in _events(lessons_dir)
        if item["type"] == "distill_batch_abandoned"
    ]
    assert len(abandoned) == 1
    assert abandoned[0]["schema_v"] == 1
    assert abandoned[0]["run_ids"] == ["run-0", "run-1"]
    assert abandoned[0]["attempts"] == 3
    assert abandoned[0]["last_error"] == "RuntimeError"
    assert abandoned[0]["reason"] == "repeated_model_failure"
    state = json.loads((lessons_dir / "distill_state.json").read_text())
    assert state == {"last_ts_end": 2.0, "last_run_id": "run-1"}

    # The abandoned episodes never come back: a later healthy run sees nothing.
    healthy = _FakeModel("[]")
    fourth = distill_lessons(events_path, lessons_dir, model=healthy)
    assert fourth.groups_considered == 0
    assert healthy.calls == []


def test_distill_same_timestamp_overflow_is_picked_up_next_batch(tmp_path):
    """41 episodes sharing one ts_end: the cursor's run_id picks up the 41st."""

    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    episodes = [
        _episode(
            f"run-{index:02d}", success=True, goal="打开应用", reason="finished", ts=7.0
        )
        for index in range(41)
    ]
    _write_episodes(events_path, episodes)

    first_model = _FakeModel(json.dumps({"rules": [], "procedures": []}))
    first = distill_lessons(events_path, lessons_dir, model=first_model)
    assert first.groups_considered == 1
    first_prompt = str(first_model.calls[0][-1].content)
    assert "run-00" in first_prompt and "run-39" in first_prompt
    assert "run-40" not in first_prompt
    state = json.loads((lessons_dir / "distill_state.json").read_text())
    assert state == {"last_ts_end": 7.0, "last_run_id": "run-39"}

    second_model = _FakeModel(json.dumps({"rules": [], "procedures": []}))
    second = distill_lessons(events_path, lessons_dir, model=second_model)
    assert second.groups_considered == 1
    second_prompt = str(second_model.calls[0][-1].content)
    assert "run-40" in second_prompt
    assert "run-00" not in second_prompt
    state = json.loads((lessons_dir / "distill_state.json").read_text())
    assert state == {"last_ts_end": 7.0, "last_run_id": "run-40"}


def test_distill_same_timestamp_batches_cover_every_episode(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    episodes = [
        _episode(
            f"run-{index:02d}", success=True, goal="打开应用", reason="finished", ts=7.0
        )
        for index in range(41)
    ]
    _write_episodes(events_path, episodes)
    seen: list[str] = []

    for _ in range(2):
        model = _FakeModel(json.dumps({"rules": [], "procedures": []}))
        distill_lessons(events_path, lessons_dir, model=model)
        prompt = str(model.calls[0][-1].content)
        seen.extend(
            item["run_id"] for item in json.loads(prompt.split("\n", 1)[1])
        )

    assert sorted(seen) == sorted(item["run_id"] for item in episodes)


def test_distill_budget_skip_advances_cursor_and_records_event(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    _write_episodes(
        events_path,
        [
            _episode("run-0", success=False, goal="打开 A", reason="failed", ts=1),
            _episode("run-1", success=True, goal="查询机票", reason="finished", ts=2),
        ],
    )
    model = _FakeModel("[]")

    result = distill_lessons(
        events_path, lessons_dir, model=model, token_budget=1
    )

    # Consumed-but-rejected by a deterministic rule: the cursor moves and the
    # skip is recorded, so the batch is never replayed.
    assert result.groups_rejected == 1
    assert result.retried is False
    assert model.calls == []
    assert json.loads((lessons_dir / "distill_state.json").read_text()) == {
        "last_ts_end": 2.0,
        "last_run_id": "run-1",
    }
    skipped = [
        item
        for item in _events(lessons_dir)
        if item["type"] == "distill_batch_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "token_budget"
    assert skipped[0]["run_ids"] == ["run-0", "run-1"]
    assert skipped[0]["batch_size"] == 2


def test_distill_legacy_float_only_state_file_still_applies(tmp_path):
    events_path = tmp_path / "experience/events.jsonl"
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "distill_state.json").write_text(
        json.dumps({"last_ts_end": 1.0}), encoding="utf-8"
    )
    _write_episodes(
        events_path,
        [
            _episode("run-0", success=True, goal="打开 A", reason="finished", ts=1),
            _episode("run-1", success=True, goal="打开 A", reason="finished", ts=2),
        ],
    )
    model = _FakeModel(json.dumps({"rules": [], "procedures": []}))

    result = distill_lessons(events_path, lessons_dir, model=model)

    # The missing run_id reads as "": the cursor still applies and is upgraded.
    assert result.groups_considered == 1
    assert json.loads((lessons_dir / "distill_state.json").read_text()) == {
        "last_ts_end": 2.0,
        "last_run_id": "run-1",
    }
    followup = distill_lessons(
        events_path,
        lessons_dir,
        model=_FakeModel(json.dumps({"rules": [], "procedures": []})),
    )
    assert followup.groups_considered == 0


# --- S2 FIX 2: revocation must not be auto-resurrectable --------------------


def _procedure_candidate(**overrides: object) -> LessonCandidate:
    payload = {
        **_candidate().to_dict(),
        "kind": "procedure",
        "steps": ["搜索目标", "选店进入", "结算前停手问人"],
        "app_scope": "com.example.travel",
    }
    payload.update(overrides)
    return LessonCandidate.from_dict(payload)


def test_revoked_rule_reproposed_with_changed_payload_is_demoted(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.approve(candidate.lesson_id)
    store.revoke(candidate.lesson_id, "人工撤销")

    changed = replace(
        _candidate(text="在示例应用中应该先刷新页面再继续", status="auto_approved"),
        created_ts=99.0,
    )
    saved = store.propose(changed, grade="auto_approved", grade_basis="模型自评")

    # The grader's auto_approved verdict must not override the human revocation.
    assert saved.version == 2
    assert saved.status == "proposed"
    assert not lesson_injectable(saved)
    guard_events = [
        item
        for item in _events(tmp_path)
        if item["type"] == "revoked_reproposal_demoted"
    ]
    assert len(guard_events) == 1
    assert guard_events[0]["schema_v"] == 1
    assert guard_events[0]["lesson_id"] == candidate.lesson_id
    assert guard_events[0]["version"] == 2
    assert guard_events[0]["status"] == "proposed"
    assert len(guard_events[0]["fingerprint"]) == 64


def test_revoked_procedure_reproposed_with_changed_payload_lands_needs_review(
    tmp_path,
):
    store = LessonStore(tmp_path)
    candidate = store.propose(_procedure_candidate())
    store.approve(candidate.lesson_id)
    store.revoke(candidate.lesson_id, "人工撤销")

    saved = store.propose(
        replace(
            _procedure_candidate(text="换个说法的过程卡", status="auto_approved"),
            created_ts=99.0,
        ),
        grade="auto_approved",
    )

    assert saved.version == 2
    assert saved.status == "needs_review"
    assert not lesson_injectable(saved)
    guard_events = [
        item
        for item in _events(tmp_path)
        if item["type"] == "revoked_reproposal_demoted"
    ]
    assert [item["status"] for item in guard_events] == ["needs_review"]


def test_revocation_guard_survives_restart_and_view_rebuild(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.approve(candidate.lesson_id)
    store.revoke(candidate.lesson_id, "人工撤销")
    first = store.propose(replace(_candidate(text="新证据版本一"), created_ts=99.0))
    assert first.status == "proposed"

    rebuilt = LessonStore(tmp_path)
    second = rebuilt.propose(
        replace(
            _candidate(text="新证据版本二", status="auto_approved"),
            created_ts=100.0,
        ),
        grade="auto_approved",
    )

    # Still no human approval since the revocation: still demoted, never
    # auto_approved, after a full event-log replay.
    assert second.version == 3
    assert second.status == "proposed"
    assert not lesson_injectable(second)


def test_human_approve_restores_revoked_reproposal(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.approve(candidate.lesson_id)
    store.revoke(candidate.lesson_id, "人工撤销")
    saved = store.propose(replace(_candidate(text="修订后的行为规则"), created_ts=99.0))
    assert saved.status == "proposed"
    assert not lesson_injectable(saved)

    approved = store.approve(candidate.lesson_id)

    # Only the human CLI makes it injectable again.
    assert approved.status == "approved"
    assert lesson_injectable(approved)
    # With the guard lifted, a later changed re-proposal follows the grader.
    later = store.propose(
        replace(
            _candidate(text="再修订的行为规则", status="auto_approved"),
            created_ts=100.0,
        ),
        grade="auto_approved",
    )
    assert later.version == 3
    assert later.status == "auto_approved"
    assert lesson_injectable(later)


def test_revoked_lesson_identical_repropose_stays_revoked(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.revoke(candidate.lesson_id, "人工撤销")
    before = store.events_path.read_text()

    again = store.propose(replace(_candidate(), created_ts=99.0))

    assert again.status == "revoked"
    assert again.version == 1
    assert store.events_path.read_text() == before


def test_revoke_on_already_revoked_stays_idempotent(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_candidate())
    store.revoke(candidate.lesson_id, "第一次撤销")
    snapshot = store.events_path.read_text()

    with pytest.raises(ValueError, match="already revoked"):
        store.revoke(candidate.lesson_id, "第二次撤销")

    assert store.get(candidate.lesson_id).status == "revoked"
    assert store.events_path.read_text() == snapshot


# --- S2 FIX 3: goal relevance + honest first-fit budget packing -------------


def _view_rule(
    lesson_id: str,
    text: str,
    *,
    version: int = 1,
    created_ts: float = 1.0,
    status: str = "approved",
    device: str | None = None,
    app: str | None = None,
) -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": lesson_id,
            "schema_v": 1,
            "version": version,
            "status": status,
            "text": text,
            "scope": {"device": device, "app": app, "app_version": None},
            "evidence": [{"run_id": "run-0", "note": "ok"}],
            "support_count": 1,
            "task_keys": ["open_app"],
            "conflicts": [],
            "created_ts": created_ts,
            "source": "distill",
        }
    )


def _write_lessons_view(
    lessons_dir: Path, lessons: list[LessonCandidate]
) -> Path:
    lessons_dir.mkdir(parents=True, exist_ok=True)
    (lessons_dir / "lessons.json").write_text(
        json.dumps([item.to_dict() for item in lessons], ensure_ascii=False),
        encoding="utf-8",
    )
    return lessons_dir


def test_run_start_selection_goal_relevance_beats_version_order(tmp_path):
    relevant = _view_rule(
        "les_aaaaaaaaaaaa", "查询机票时应当先等待搜索结果加载完成"
    )
    irrelevant = _view_rule(
        "les_bbbbbbbbbbbb", "笔记应用应当使用深色主题", version=9, created_ts=9.0
    )
    lessons = _write_lessons_view(tmp_path, [irrelevant, relevant])

    selected = select_lessons_for_injection(
        str(lessons),
        device_scope=None,
        max_items=3,
        max_tokens=800,
        goal_text="帮我查询十月二日上海飞桃仙的机票",
    )

    assert [item.lesson_id for item in selected] == [
        relevant.lesson_id,
        irrelevant.lesson_id,
    ]


def test_run_start_selection_zero_or_no_goal_keeps_legacy_order(tmp_path):
    first = _view_rule("les_aaaaaaaaaaaa", "规则甲内容", version=1, created_ts=1.0)
    second = _view_rule(
        "les_bbbbbbbbbbbb", "规则乙内容", version=2, created_ts=2.0
    )
    lessons = _write_lessons_view(tmp_path, [first, second])

    no_goal = select_lessons_for_injection(
        str(lessons), device_scope=None, max_items=5, max_tokens=800
    )
    zero_overlap = select_lessons_for_injection(
        str(lessons),
        device_scope=None,
        max_items=5,
        max_tokens=800,
        goal_text="完全无关的目标词",
    )

    assert [item.lesson_id for item in no_goal] == [
        second.lesson_id,
        first.lesson_id,
    ]
    assert [item.lesson_id for item in zero_overlap] == [
        second.lesson_id,
        first.lesson_id,
    ]


def test_selection_first_fit_keeps_short_rule_that_fits(tmp_path):
    long_rule = _view_rule(
        "les_aaaaaaaaaaaa", "甲" * 800, version=9, created_ts=9.0
    )
    short_rule = _view_rule("les_bbbbbbbbbbbb", "短规则内容", version=1, created_ts=1.0)
    lessons = _write_lessons_view(tmp_path, [long_rule, short_rule])

    selected = select_lessons_for_injection(
        str(lessons), device_scope=None, max_items=3, max_tokens=200
    )

    # The long high-ranked rule is skipped, not evicting the short one.
    assert [item.lesson_id for item in selected] == [short_rule.lesson_id]


def test_selection_packing_uses_the_rendered_line_cost(tmp_path):
    # Bare text fits a 200-token budget; the exact line both injectors render
    # (numbering + source label) does not — packing must weigh the latter.
    big = _view_rule("les_aaaaaaaaaaaa", "字" * 780, version=2, created_ts=2.0)
    lessons = _write_lessons_view(tmp_path, [big])

    assert estimate_text_tokens(big.text) <= 200
    rendered = f"1. {big.text}（来源 {big.lesson_id} · 全局 scope）"
    assert estimate_text_tokens(rendered) > 200

    assert (
        select_lessons_for_injection(
            str(lessons), device_scope=None, max_items=3, max_tokens=200
        )
        == []
    )


def test_selection_rendered_cost_matches_injector_rendering(tmp_path):
    first = _view_rule("les_aaaaaaaaaaaa", "第一条规则内容", version=1, created_ts=1.0)
    second = _view_rule(
        "les_bbbbbbbbbbbb", "第二条规则内容", version=1, created_ts=2.0
    )
    lessons = _write_lessons_view(tmp_path, [first, second])

    selected = select_lessons_for_injection(
        str(lessons), device_scope=None, max_items=5, max_tokens=800
    )

    assert len(selected) == 2
    rendered_total = sum(
        estimate_text_tokens(
            f"{index}. {lesson.text}（来源 {lesson.lesson_id} · 全局 scope）"
        )
        for index, lesson in enumerate(selected, start=1)
    )
    assert rendered_total <= 800
    assert rendered_total > sum(
        estimate_text_tokens(item.text) for item in selected
    )


def test_app_rule_selection_goal_relevance_beats_version_order(tmp_path):
    relevant = _view_rule(
        "les_aaaaaaaaaaaa", "点外卖前应当先确认收货地址", app="com.example.app"
    )
    irrelevant = _view_rule(
        "les_bbbbbbbbbbbb",
        "购物车里应当先收藏再比价",
        version=9,
        created_ts=9.0,
        app="com.example.app",
    )
    lessons = _write_lessons_view(tmp_path, [irrelevant, relevant])

    selected = select_app_rules_for_injection(
        str(lessons),
        app_package="com.example.app",
        device_scope=None,
        goal_text="用美团点外卖",
    )

    assert [item.lesson_id for item in selected] == [
        relevant.lesson_id,
        irrelevant.lesson_id,
    ]
