"""Tests for the WP-WF2b procedure replay channel (plus exemplar regression)."""

from __future__ import annotations

import json
from pathlib import Path

from phone_agent.v2.evolution import LessonCandidate
from phone_agent.v2.recall import HashEmbedder
from phone_agent.v2.replay import (
    _pool_at,
    _procedure_pool_timeline,
    replay_exemplar_metrics,
    replay_procedure_metrics,
    sweep_procedure_thresholds,
    _main,
)

BILI = "tv.danmaku.bili"
MEITUAN = "com.sankuai.meituan"
FAILED = "com.failed.app"


def _bili_card(lesson_id: str = "les_0000000000000001", **kwargs) -> dict:
    """A procedure card scoped to B站 (card text = title + steps)."""

    return _procedure(
        lesson_id,
        "B站搜索视频并播放",
        BILI,
        steps=["打开B站", "搜索框输入关键词"],
        **kwargs,
    )


def _general_card(lesson_id: str = "les_0000000000000004", **kwargs) -> dict:
    """A cross-app card served only to episodes without a launch receipt."""

    return _procedure(
        lesson_id,
        "通用搜索并等待结果加载",
        "general",
        steps=["点开搜索框", "输入关键词", "等待结果列表稳定"],
        **kwargs,
    )


def _episode(
    *,
    run_id: str,
    ts_start: float,
    goal_text: str,
    steps: int = 6,
    ts_end: float | None = None,
    device_scope: str = "device:test",
    success: bool = True,
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": ts_start,
        "ts_end": ts_start + 10.0 if ts_end is None else ts_end,
        "time_of_day": "afternoon",
        "day_of_week": 1,
        "device_scope": device_scope,
        "goal_text": goal_text,
        "apps": [],
        "success": success,
        "reason": "",
        "steps": steps,
        "tokens_total": 100,
        "tokens_by_role": {"actor": 100},
        "warnings": 0,
        "takeover": None,
        "verifier": "skipped",
        "capabilities": {},
        "injected_lessons": [],
        "deliverable_path": None,
    }


def _launch(
    *, run_id: str, step: int, ts: float, package: str | None, result_class: str = "ok"
) -> dict:
    return {
        "type": "experience_event",
        "schema_v": 1,
        "run_id": run_id,
        "step": step,
        "ts": ts,
        "tool": "launch_app",
        "result_class": result_class,
        "app_package": package,
        "device_scope": "device:test",
    }


def _procedure(
    lesson_id: str,
    title: str,
    app_scope: str,
    *,
    status: str = "auto_approved",
    steps: list[str] | None = None,
    device: str | None = None,
) -> dict:
    payload = {
        "lesson_id": lesson_id,
        "schema_v": 1,
        "version": 1,
        "status": status,
        "text": title,
        "scope": {"device": device, "app": None, "app_version": None},
        "evidence": [{"run_id": "run_evidence", "note": "outcome pattern"}],
        "support_count": 1,
        "task_keys": ["demo"],
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
        "kind": "procedure",
        "steps": ["打开应用", "搜索框输入关键词"] if steps is None else steps,
        "pitfalls": None,
        "app_scope": app_scope,
    }
    return LessonCandidate.from_dict(payload).to_dict()


def _proposed(lesson: dict, ts: float) -> dict:
    return {"type": "lesson_proposed", "schema_v": 1, "ts": ts, "lesson": lesson}


def _lifecycle(event_type: str, lesson_id: str, ts: float, *, version: int = 1) -> dict:
    return {
        "type": event_type,
        "schema_v": 1,
        "ts": ts,
        "lesson_id": lesson_id,
        "version": version,
        "reason": "test",
    }


def _write(dir_path: Path, rows: list[dict]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "events.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_pool_timeline_tracks_approval_revocation_and_demotion(tmp_path: Path) -> None:
    """The injectable pool evolves at each lifecycle event's own timestamp."""

    card_a = _bili_card(status="proposed")
    card_b = _procedure("les_0000000000000002", "美团下单到结算", MEITUAN)
    lessons = tmp_path / "lessons"
    _write(
        lessons,
        [
            _proposed(card_a, ts=10.0),  # proposed only: not injectable yet
            _lifecycle("lesson_approved", "les_0000000000000001", ts=20.0),
            _lifecycle("lesson_revoked", "les_0000000000000001", ts=30.0),
            _proposed(card_b, ts=40.0),  # auto_approved: injectable at once
            _lifecycle("lesson_demoted", "les_0000000000000002", ts=50.0),
        ],
    )

    timeline = _procedure_pool_timeline(lessons / "events.jsonl")

    assert [lesson.lesson_id for lesson in _pool_at(timeline, 19.0)] == []
    assert [lesson.lesson_id for lesson in _pool_at(timeline, 25.0)] == [
        "les_0000000000000001"
    ]
    assert [lesson.lesson_id for lesson in _pool_at(timeline, 35.0)] == []
    assert [lesson.lesson_id for lesson in _pool_at(timeline, 45.0)] == [
        "les_0000000000000002"
    ]
    # A demoted procedure card lands in needs_review: no longer injectable.
    assert [lesson.lesson_id for lesson in _pool_at(timeline, 55.0)] == []


def test_episode_only_sees_cards_injectable_when_it_started(tmp_path: Path) -> None:
    """No time travel: an episode never sees a card approved after it started."""

    card = _bili_card(status="proposed")
    lessons = tmp_path / "lessons"
    _write(
        lessons,
        [
            _proposed(card, ts=10.0),
            _lifecycle("lesson_approved", "les_0000000000000001", ts=100.0),
        ],
    )
    experience = tmp_path / "experience"
    _write(
        experience,
        [
            _episode(run_id="run_early", ts_start=50.0, goal_text="B站搜索视频并播放"),
            _launch(run_id="run_early", step=1, ts=51.0, package=BILI),
            _episode(run_id="run_late", ts_start=200.0, goal_text="B站搜索视频并播放"),
            _launch(run_id="run_late", step=1, ts=201.0, package=BILI),
        ],
    )

    result = replay_procedure_metrics(
        str(experience), str(lessons), embedder=HashEmbedder(dimension=64)
    )

    details = {row["run_id"]: row for row in result["details"]}
    # The card became injectable at ts=100, after run_early started.
    assert details["run_early"]["pool_size"] == 0
    assert details["run_early"]["hit_lesson_id"] is None
    assert details["run_early"]["covered"] is False
    assert details["run_late"]["pool_size"] == 1
    assert details["run_late"]["hit_lesson_id"] == "les_0000000000000001"
    assert details["run_late"]["covered"] is True
    assert result["covered"] == 1


def test_hard_filter_uses_successful_launch_receipts(tmp_path: Path) -> None:
    """Only packages from the episode's own successful launches build the pool."""

    lessons = tmp_path / "lessons"
    _write(
        lessons,
        [
            _proposed(_bili_card(), ts=1.0),
            _proposed(_procedure("les_0000000000000002", "美团下单到结算", MEITUAN), ts=1.0),
            _proposed(_procedure("les_0000000000000003", "失败应用流程", FAILED), ts=1.0),
            _proposed(_general_card(), ts=1.0),
        ],
    )
    experience = tmp_path / "experience"
    _write(
        experience,
        [
            _episode(run_id="run_app", ts_start=10.0, goal_text="B站搜索视频并播放"),
            _launch(run_id="run_app", step=1, ts=11.0, package=BILI),
            _launch(run_id="run_app", step=2, ts=12.0, package=MEITUAN),
            # A package that only appears on a failed launch is not evidence.
            _launch(
                run_id="run_app", step=3, ts=13.0, package=FAILED, result_class="error"
            ),
            # No launch receipt at all (and one failed): falls back to general.
            _episode(run_id="run_general", ts_start=20.0, goal_text="搜索关键词"),
            _launch(
                run_id="run_general", step=1, ts=21.0, package=MEITUAN, result_class="error"
            ),
        ],
    )

    result = replay_procedure_metrics(
        str(experience), str(lessons), embedder=HashEmbedder(dimension=64)
    )
    details = {row["run_id"]: row for row in result["details"]}

    app_row = details["run_app"]
    assert app_row["launched_packages"] == [MEITUAN, BILI]
    assert app_row["pool_kind"] == "app"
    # The two really-launched apps, never the merely attempted one.
    assert app_row["pool_size"] == 2
    assert app_row["hit_app_scope"] == BILI

    general_row = details["run_general"]
    assert general_row["launched_packages"] == []
    assert general_row["pool_kind"] == "general"
    assert general_row["pool_size"] == 1
    assert general_row["hit_app_scope"] == "general"


def test_coverage_and_relevance_gates_with_steps_delta_na(tmp_path: Path) -> None:
    """Coverage counts every episode; relevance only counts app-grounded hits."""

    lessons = tmp_path / "lessons"
    _write(
        lessons,
        [
            _proposed(_bili_card(), ts=1.0),
            _proposed(_general_card(), ts=1.0),
        ],
    )
    experience = tmp_path / "experience"
    _write(
        experience,
        [
            _episode(run_id="run_b1", ts_start=10.0, goal_text="打开b站搜索视频"),
            _launch(run_id="run_b1", step=1, ts=11.0, package=BILI),
            _episode(run_id="run_b2", ts_start=20.0, goal_text="打开b站搜索视频"),
            _launch(run_id="run_b2", step=1, ts=21.0, package=BILI),
            # General-pool episodes: covered is possible, relevant is not.
            _episode(run_id="run_g1", ts_start=30.0, goal_text="在这个应用里搜索关键词"),
            _episode(run_id="run_g2", ts_start=40.0, goal_text="点开搜索框输入关键词"),
            # No card for this app: never covered.
            _episode(run_id="run_x", ts_start=50.0, goal_text="高德地图导航"),
            _launch(run_id="run_x", step=1, ts=51.0, package="com.autonavi.minimap"),
        ],
    )

    result = replay_procedure_metrics(
        str(experience),
        str(lessons),
        embedder=HashEmbedder(dimension=64),
        min_score=0.50,
    )

    assert result["runs_total"] == 5
    assert result["runs_queried"] == 5
    assert result["runs_with_pool"] == 4
    assert result["covered"] == 2
    assert result["relevant"] == 2
    assert result["coverage_rate"] == 0.4
    assert result["coverage_rate_over_pool"] == 0.5
    assert result["relevance_rate"] == 1.0

    # steps-delta has no counterfactual offline and does not gate the channel.
    assert result["steps_delta"]["applicable"] is False
    assert result["gates"]["delta_ok"] is None
    assert result["gates"]["delta_note"] == "n/a"
    assert result["gates"]["coverage_ok"] is True
    assert result["gates"]["relevance_ok"] is True
    assert result["gates"]["channel_recommended"] is True


def test_sweep_picks_the_knee_point(tmp_path: Path) -> None:
    """Knee = highest coverage among thresholds clearing the relevance target."""

    lessons = tmp_path / "lessons"
    _write(
        lessons,
        [
            _proposed(_bili_card(), ts=1.0),
            _proposed(_general_card(), ts=1.0),
        ],
    )
    experience = tmp_path / "experience"
    _write(
        experience,
        [
            _episode(run_id="run_b1", ts_start=10.0, goal_text="打开b站搜索视频"),
            _launch(run_id="run_b1", step=1, ts=11.0, package=BILI),
            _episode(run_id="run_b2", ts_start=20.0, goal_text="打开b站搜索视频"),
            _launch(run_id="run_b2", step=1, ts=21.0, package=BILI),
            # Weak general hits (0.307 / 0.489): they lift coverage but never
            # relevance, so the loosest thresholds fail the relevance gate.
            _episode(run_id="run_g1", ts_start=30.0, goal_text="在这个应用里搜索关键词"),
            _episode(run_id="run_g2", ts_start=40.0, goal_text="点开搜索框输入关键词"),
            _episode(run_id="run_x", ts_start=50.0, goal_text="高德地图导航"),
            _launch(run_id="run_x", step=1, ts=51.0, package="com.autonavi.minimap"),
        ],
    )

    result = sweep_procedure_thresholds(
        str(experience), str(lessons), embedder=HashEmbedder(dimension=64)
    )

    assert [row["min_score"] for row in result["thresholds"]] == [
        0.3,
        0.35,
        0.4,
        0.45,
        0.5,
        0.55,
        0.6,
        0.65,
        0.7,
    ]
    table = {row["min_score"]: row for row in result["thresholds"]}
    # Loosest threshold: 4 covered but only 2 app-grounded -> relevance 0.5.
    assert table[0.3]["coverage_rate"] == 0.8
    assert table[0.3]["relevance_rate"] == 0.5
    # Both weak general hits are gone by 0.5: relevance 1.0, coverage 0.4.
    assert table[0.5]["coverage_rate"] == 0.4
    assert table[0.5]["relevance_rate"] == 1.0
    assert table[0.7]["covered"] == 2

    # Coverage never grows as the threshold tightens.
    rates = [row["coverage_rate"] for row in result["thresholds"]]
    assert rates == sorted(rates, reverse=True)

    # 0.30 has the highest coverage (0.8) but fails relevance; the knee is the
    # loosest threshold that clears relevance: 0.35 (coverage 0.6).
    assert result["recommended"]["min_score"] == 0.35
    assert result["recommended"]["coverage_rate"] == 0.6
    assert result["recommended"]["relevance_rate"] == 0.666667
    assert "relevance >= 0.6" in result["recommended_reason"]


def test_empty_card_pool_exits_cleanly(tmp_path: Path, capsys) -> None:
    """No injectable procedure card is a visible neutral state, not an error."""

    lessons = tmp_path / "lessons"
    # A proposed-only procedure card and an approved rule never enter the pool.
    _write(
        lessons,
        [
            _proposed(
                _procedure("les_0000000000000001", "B站搜索视频并播放", BILI, status="proposed"),
                ts=1.0,
            ),
        ],
    )
    experience = tmp_path / "experience"
    _write(
        experience,
        [
            _episode(run_id="run_a", ts_start=10.0, goal_text="B站搜索视频并播放"),
            _launch(run_id="run_a", step=1, ts=11.0, package=BILI),
        ],
    )

    result = replay_procedure_metrics(
        str(experience), str(lessons), embedder=HashEmbedder(dimension=64)
    )
    assert result["status"] == "empty_pool"
    assert "candidate pool empty" in result["note"]
    assert result["runs_total"] == 1
    assert result["runs_with_pool"] == 0
    assert result["covered"] == 0
    assert result["gates"]["channel_recommended"] is False
    assert result["details"][0]["pool_size"] == 0

    sweep = sweep_procedure_thresholds(
        str(experience), str(lessons), embedder=HashEmbedder(dimension=64)
    )
    assert sweep["status"] == "empty_pool"
    assert sweep["recommended"] is None
    assert "candidate pool empty" in sweep["recommended_reason"]

    _main(
        [
            "--channel",
            "procedure",
            "--sweep",
            "--experience-dir",
            str(experience),
            "--lessons-dir",
            str(lessons),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "empty_pool"
    assert "candidate pool empty" in payload["recommended_reason"]


def test_exemplar_channel_is_untouched(tmp_path: Path, capsys) -> None:
    """The exemplar channel keeps its shape; the CLI default still routes to it."""

    experience = tmp_path / "experience"
    day = 86_400.0
    _write(
        experience,
        [
            _episode(run_id="run_a", ts_start=0.0, goal_text="查机票", steps=8),
            _episode(run_id="run_b", ts_start=day, goal_text="查机票", steps=5),
        ],
    )

    result = replay_exemplar_metrics(
        str(experience),
        embedder=HashEmbedder(dimension=64),
        min_score=0.50,
        now=day,
    )

    assert set(result) == {
        "runs_total",
        "runs_with_candidates",
        "covered",
        "relevant",
        "coverage_rate",
        "relevance_rate",
        "steps_delta",
        "gates",
        "details",
    }
    assert set(result["gates"]) == {
        "coverage_ok",
        "relevance_ok",
        "delta_ok",
        "channel_recommended",
    }
    assert isinstance(result["gates"]["delta_ok"], bool)
    assert set(result["steps_delta"]) == {
        "min",
        "max",
        "mean",
        "median",
        "positive_share",
    }
    assert result["covered"] == 1
    assert result["relevant"] == 1

    # No --channel: the CLI keeps emitting the exemplar shape.
    _main(["--experience-dir", str(experience), "--min-score", "0.50"])
    payload = json.loads(capsys.readouterr().out)
    assert "channel" not in payload
    assert payload["runs_total"] == 2
