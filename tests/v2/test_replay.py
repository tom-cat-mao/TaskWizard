"""Tests for the exemplar-channel replay metric."""

from __future__ import annotations

import json
from pathlib import Path

from phone_agent.v2.recall import HashEmbedder
from phone_agent.v2.replay import replay_exemplar_metrics


def _episode(
    *,
    run_id: str,
    ts_end: float,
    goal_text: str,
    success: bool,
    steps: int,
    device_scope: str = "device:test",
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": ts_end - 1.0,
        "ts_end": ts_end,
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


def _write_events(experience_dir: Path, episodes: list[dict]) -> None:
    experience_dir.mkdir(parents=True, exist_ok=True)
    events_path = experience_dir / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as stream:
        for episode in episodes:
            stream.write(json.dumps(episode, ensure_ascii=False) + "\n")


def test_replay_exemplar_metrics_basic(tmp_path: Path) -> None:
    """Two same-task successes, one different-task success, one failure."""

    # Use spaced timestamps and a fixed `now` so recency tie-breaking is
    # deterministic and favors the most recent indexed episode.
    base_ts = 1_000_000.0
    day = 86_400.0
    episodes = [
        # Baseline success with more steps.
        _episode(run_id="run_a", ts_end=base_ts, goal_text="查机票", success=True, steps=8),
        # Later success on the same task, more efficient.
        _episode(run_id="run_b", ts_end=base_ts + day, goal_text="查机票", success=True, steps=5),
        # Different task key; should not recall the flight episodes.
        _episode(run_id="run_c", ts_end=base_ts + 2 * day, goal_text="查酒店", success=True, steps=6),
        # Failure on the flight task; the recalled precedent should be a success.
        _episode(run_id="run_d", ts_end=base_ts + 3 * day, goal_text="查机票", success=False, steps=7),
    ]
    experience_dir = tmp_path / "experience"
    _write_events(experience_dir, episodes)

    result = replay_exemplar_metrics(
        str(experience_dir),
        embedder=HashEmbedder(dimension=64),
        min_steps=2,
        min_score=0.50,
        top_k=1,
        now=base_ts + 3 * day,
    )

    assert result["runs_total"] == 4
    assert result["runs_with_candidates"] == 2
    assert result["covered"] == 2
    assert result["relevant"] == 2
    assert result["coverage_rate"] == 0.5
    assert result["relevance_rate"] == 1.0

    delta = result["steps_delta"]
    assert delta["min"] == -3
    assert delta["max"] == 2
    assert delta["mean"] == -0.5
    assert delta["median"] == -0.5
    assert delta["positive_share"] == 0.5

    gates = result["gates"]
    assert gates["coverage_ok"] is True
    assert gates["relevance_ok"] is True
    assert gates["delta_ok"] is False
    assert gates["channel_recommended"] is False

    details = result["details"]
    assert len(details) == 4

    # The first run sees an empty index.
    assert details[0]["run_id"] == "run_a"
    assert details[0]["hit_run_id"] is None
    assert details[0]["qualified"] is False
    assert details[0]["relevant"] is False

    # The second run recalls the first one.
    assert details[1]["run_id"] == "run_b"
    assert details[1]["hit_run_id"] == "run_a"
    assert details[1]["qualified"] is True
    assert details[1]["relevant"] is True
    assert details[1]["delta"] == -3

    # Different task key gets no candidate.
    assert details[2]["run_id"] == "run_c"
    assert details[2]["hit_run_id"] is None
    assert details[2]["qualified"] is False
    assert details[2]["relevant"] is False

    # Failure run still recalls the most recent successful precedent.
    assert details[3]["run_id"] == "run_d"
    assert details[3]["hit_run_id"] == "run_b"
    assert details[3]["qualified"] is True
    assert details[3]["relevant"] is True
    assert details[3]["delta"] == 2

    # No run should ever recall itself.
    for row in details:
        assert row["hit_run_id"] != row["run_id"]


def test_replay_empty_events(tmp_path: Path) -> None:
    """An empty experience log yields zero-valued metrics."""

    experience_dir = tmp_path / "experience"
    _write_events(experience_dir, [])

    result = replay_exemplar_metrics(
        str(experience_dir),
        embedder=HashEmbedder(dimension=64),
    )

    assert result["runs_total"] == 0
    assert result["runs_with_candidates"] == 0
    assert result["covered"] == 0
    assert result["relevant"] == 0
    assert result["coverage_rate"] == 0.0
    assert result["relevance_rate"] == 0.0
    assert result["steps_delta"] == {
        "min": None,
        "max": None,
        "mean": None,
        "median": None,
        "positive_share": None,
    }
    assert result["gates"]["channel_recommended"] is False
    assert result["details"] == []


def test_replay_unindexable_run_is_not_queried(tmp_path: Path) -> None:
    """A run below the step gate is not used as a query but still indexed."""

    episodes = [
        _episode(run_id="run_a", ts_end=1.0, goal_text="查机票", success=True, steps=8),
        _episode(run_id="run_b", ts_end=2.0, goal_text="查机票", success=True, steps=1),
    ]
    experience_dir = tmp_path / "experience"
    _write_events(experience_dir, episodes)

    result = replay_exemplar_metrics(
        str(experience_dir),
        embedder=HashEmbedder(dimension=64),
        min_steps=2,
    )

    # run_b is below the step gate, so it should not perform recall.
    assert result["runs_total"] == 2
    assert result["runs_with_candidates"] == 0
    assert result["covered"] == 0
    # But the baseline run is still in the index after the replay.
    assert result["details"][0]["hit_run_id"] is None
    assert result["details"][1]["hit_run_id"] is None
