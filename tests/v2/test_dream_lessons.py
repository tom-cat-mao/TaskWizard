"""Dream-side lesson maintenance: demote replay, evidence reconciliation, effectiveness."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from phone_agent.v2.dream import lesson_effectiveness, run_maintenance
from phone_agent.v2.evolution import LESSON_EVENT_TYPES, LessonCandidate, LessonStore
from phone_agent.v2.experience import ExperienceWriter, load_episodes

LESSON_A = "les_aaaaaaaaaaaa"
LESSON_B = "les_bbbbbbbbbbbb"
LESSON_C = "les_cccccccccccc"


def _lesson(
    *,
    lesson_id: str = LESSON_A,
    run_ids: tuple[str, ...] = ("run-0", "run-1", "run-2"),
) -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": lesson_id,
            "schema_v": 1,
            "version": 1,
            "status": "proposed",
            "text": "在示例应用中应该先确认页面稳定再继续",
            "scope": {
                "device": "serial-1",
                "app": "com.example.travel",
                "app_version": None,
            },
            "evidence": [
                {"run_id": run_id, "note": "episode outcome supports rule"}
                for run_id in run_ids
            ],
            "support_count": len(run_ids),
            "task_keys": ["open_app", "search_flight"],
            "conflicts": [],
            "created_ts": 1.0,
            "source": "distill",
        }
    )


def _episode(
    run_id: str,
    *,
    goal: str = "打开应用",
    success: bool = True,
    steps: int = 10,
    tokens_total: int = 1000,
    injected_lessons: list[str] | None = None,
) -> dict:
    # Recent timestamps: dream's 90-day retention would archive 1970-era runs.
    now = time.time()
    return {
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": now,
        "ts_end": now,
        "device_scope": "device:serial-1",
        "goal_text": goal,
        "apps": ["com.example.travel"],
        "success": success,
        "reason": "finished" if success else "failed",
        "steps": steps,
        "tokens_total": tokens_total,
        "injected_lessons": injected_lessons or [],
    }


def _config(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        app_kb_enabled=True,
        memory_dir=str(tmp_path / "memory"),
        alias_overwrite_enabled=False,
        experience_enabled=True,
        experience_dir=str(tmp_path / "experience"),
        episode_keep=500,
        episode_archive_days=90,
        evolution_mode="manual",
        lessons_dir=str(tmp_path / "lessons"),
        vec_db=None,
    )


def _append_raw(events_path, event: dict) -> None:
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def _demote_event(lesson_id: str, version: int) -> dict:
    return {
        "type": "lesson_demoted",
        "schema_v": 1,
        "ts": 9.0,
        "lesson_id": lesson_id,
        "version": version,
        "reason": "evidence no longer eligible: rule:verified_evidence=0<3",
    }


def _view_episodes(experience_dir: str) -> list[dict]:
    return [
        record
        for record in load_episodes(experience_dir).values()
        if record.get("type") == "episode_outcome"
    ]


def _write_evidence(writer: ExperienceWriter) -> None:
    for index, goal in enumerate(("打开应用", "打开应用", "查询机票")):
        writer.append_outcome(**_episode(f"run-{index}", goal=goal))


# --- demote ---------------------------------------------------------------


def test_demote_requires_known_approved_lesson_and_reason(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_lesson())

    with pytest.raises(ValueError, match="only an approved lesson"):
        store.demote(candidate.lesson_id, "evidence archived")
    with pytest.raises(KeyError):
        store.demote("les_ffffffffffff", "evidence archived")

    store.approve(candidate.lesson_id)
    with pytest.raises(ValueError, match="must not be empty"):
        store.demote(candidate.lesson_id, "   ")


def test_demote_replay_returns_approved_to_proposed_and_rewrites_view(tmp_path):
    store = LessonStore(tmp_path)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)

    demoted = store.demote(candidate.lesson_id, "evidence archived")

    assert demoted.status == "proposed"
    assert demoted.version == candidate.version == 1
    events = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["type"] for item in events] == [
        "lesson_proposed",
        "lesson_approved",
        "lesson_demoted",
    ]
    assert set(item["type"] for item in events) <= LESSON_EVENT_TYPES

    assert json.loads(store.lessons_path.read_text(encoding="utf-8"))[0][
        "status"
    ] == "proposed"
    store.lessons_path.write_text("{broken", encoding="utf-8")
    rebuilt = LessonStore(tmp_path)
    assert rebuilt.get(candidate.lesson_id).status == "proposed"
    assert json.loads(rebuilt.lessons_path.read_text(encoding="utf-8"))[0][
        "status"
    ] == "proposed"


def test_demote_replay_ignores_revoked_proposed_and_version_mismatch(tmp_path):
    store = LessonStore(tmp_path)

    revoked = store.propose(_lesson(lesson_id=LESSON_A))
    store.approve(revoked.lesson_id)
    store.revoke(revoked.lesson_id, "人工撤销")
    _append_raw(store.events_path, _demote_event(LESSON_A, 1))

    store.propose(_lesson(lesson_id=LESSON_B))
    _append_raw(store.events_path, _demote_event(LESSON_B, 1))

    versioned = store.propose(_lesson(lesson_id=LESSON_C))
    store.approve(versioned.lesson_id)
    superseded = store.supersede(versioned.lesson_id, "在示例应用中应该先刷新再继续")
    assert superseded.version == 2
    store.approve(versioned.lesson_id)
    _append_raw(store.events_path, _demote_event(versioned.lesson_id, 1))

    rebuilt = LessonStore(tmp_path)
    # Revoked stays revoked: there is deliberately no reinstatement path.
    assert rebuilt.get(LESSON_A).status == "revoked"
    assert rebuilt.get(LESSON_B).status == "proposed"
    assert rebuilt.get(LESSON_C).status == "approved"

    assert rebuilt.demote(LESSON_C, "evidence archived").version == 2
    assert LessonStore(tmp_path).get(LESSON_C).status == "proposed"


# --- evidence reconciliation ---------------------------------------------


def test_dream_demotes_lesson_whose_evidence_runs_were_archived(tmp_path):
    config = _config(tmp_path)
    writer = ExperienceWriter(config.experience_dir)
    _write_evidence(writer)
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)
    writer.archive(["run-0", "run-1", "run-2"], {})

    summary = run_maintenance(config, light=False)

    demoted = summary["lessons_demoted"]
    assert [item["lesson_id"] for item in demoted] == [candidate.lesson_id]
    assert "rule:verified_evidence=0<3" in demoted[0]["reasons"]
    assert LessonStore(config.lessons_dir).get(candidate.lesson_id).status == "proposed"


def test_dream_keeps_lesson_whose_evidence_survives(tmp_path):
    config = _config(tmp_path)
    _write_evidence(ExperienceWriter(config.experience_dir))
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)

    summary = run_maintenance(config, light=False)

    assert summary["lessons_demoted"] == []
    assert LessonStore(config.lessons_dir).get(candidate.lesson_id).status == "approved"


def test_dream_keeps_low_support_count_lesson_whose_evidence_survives(tmp_path):
    # The fixture above satisfies the whole old gate (support 3, two task
    # keys), which would hide a demotion triggered by anything other than
    # evidence loss.  This one isolates the trigger: support_count=1 with its
    # single cited run still in the view must survive.
    config = _config(tmp_path)
    _write_evidence(ExperienceWriter(config.experience_dir))
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson(run_ids=("run-0",)))
    store.approve(candidate.lesson_id)
    assert candidate.support_count == 1

    summary = run_maintenance(config, light=False)

    assert summary["lessons_demoted"] == []
    assert LessonStore(config.lessons_dir).get(candidate.lesson_id).status == "approved"


def test_dream_demotes_lesson_as_soon_as_one_cited_run_is_archived(tmp_path):
    config = _config(tmp_path)
    writer = ExperienceWriter(config.experience_dir)
    _write_evidence(writer)
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)
    writer.archive(["run-2"], {})

    summary = run_maintenance(config, light=False)

    demoted = summary["lessons_demoted"]
    assert [item["lesson_id"] for item in demoted] == [candidate.lesson_id]
    assert "rule:verified_evidence=2<3" in demoted[0]["reasons"]
    assert LessonStore(config.lessons_dir).get(candidate.lesson_id).status == "proposed"


def test_dream_lesson_maintenance_is_skipped_when_evolution_is_off(tmp_path):
    config = _config(tmp_path)
    config.evolution_mode = "off"
    _write_evidence(ExperienceWriter(config.experience_dir))
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)

    summary = run_maintenance(config, light=False)

    assert "lessons_demoted" not in summary
    assert store.get(candidate.lesson_id).status == "approved"


# --- injection effectiveness ----------------------------------------------


def _write_effectiveness_fixture(experience_dir: str) -> None:
    writer = ExperienceWriter(experience_dir)
    writer.append_outcome(
        **_episode(
            "run-0",
            success=False,
            steps=20,
            tokens_total=5000,
            injected_lessons=[LESSON_A],
        )
    )
    writer.append_outcome(
        **_episode(
            "run-1",
            success=False,
            steps=25,
            tokens_total=6000,
            injected_lessons=[LESSON_A],
        )
    )
    writer.append_outcome(
        **_episode("run-2", goal="查询机票", steps=10, tokens_total=2000)
    )
    writer.append_outcome(
        **_episode("run-3", goal="查询机票", steps=12, tokens_total=2400)
    )


def test_lesson_effectiveness_flags_lesson_with_worse_success_rate(tmp_path):
    experience_dir = str(tmp_path / "experience")
    _write_effectiveness_fixture(experience_dir)

    report = lesson_effectiveness(_view_episodes(experience_dir))

    assert [item["lesson_id"] for item in report] == [LESSON_A]
    entry = report[0]
    assert entry["runs_with"] == {
        "runs": 2,
        "success_rate": 0.0,
        "avg_steps": 22.5,
        "avg_tokens_total": 5500.0,
    }
    assert entry["runs_without"] == {
        "runs": 2,
        "success_rate": 1.0,
        "avg_steps": 11.0,
        "avg_tokens_total": 2200.0,
    }


def test_lesson_effectiveness_ignores_single_injection_run(tmp_path):
    experience_dir = str(tmp_path / "experience")
    _write_effectiveness_fixture(experience_dir)
    ExperienceWriter(experience_dir).append_outcome(
        **_episode(
            "run-4",
            goal="查询机票",
            success=False,
            steps=40,
            tokens_total=9000,
            injected_lessons=[LESSON_B],
        )
    )

    report = lesson_effectiveness(_view_episodes(experience_dir))

    assert [item["lesson_id"] for item in report] == [LESSON_A]


def test_dream_reports_suggested_revoke_without_revoking(tmp_path):
    config = _config(tmp_path)
    writer = ExperienceWriter(config.experience_dir)
    _write_evidence(writer)
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)
    for index, steps, tokens in ((3, 20, 5000), (4, 30, 7000)):
        writer.append_outcome(
            **_episode(
                f"run-{index}",
                goal="查询机票",
                success=False,
                steps=steps,
                tokens_total=tokens,
                injected_lessons=[candidate.lesson_id],
            )
        )

    summary = run_maintenance(config, light=False)

    assert summary["lessons_demoted"] == []
    flagged = summary["suggested_revoke"]
    assert [item["lesson_id"] for item in flagged] == [candidate.lesson_id]
    assert flagged[0]["runs_with"]["success_rate"] == 0.0
    assert flagged[0]["runs_without"]["success_rate"] == 1.0
    assert LessonStore(config.lessons_dir).get(candidate.lesson_id).status == "approved"


def test_dream_does_not_flag_lesson_injected_into_successful_runs(tmp_path):
    config = _config(tmp_path)
    writer = ExperienceWriter(config.experience_dir)
    _write_evidence(writer)
    store = LessonStore(config.lessons_dir)
    candidate = store.propose(_lesson())
    store.approve(candidate.lesson_id)
    for index in (3, 4):
        writer.append_outcome(
            **_episode(
                f"run-{index}",
                goal="查询机票",
                success=True,
                injected_lessons=[candidate.lesson_id],
            )
        )

    summary = run_maintenance(config, light=False)

    assert summary["suggested_revoke"] == []
