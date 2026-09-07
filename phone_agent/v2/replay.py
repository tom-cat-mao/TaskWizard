"""Offline replay metric for the exemplar channel.

Replays history in timestamp order inside an in-memory vector index and measures
how often the top-1 recalled successful precedent is (a) available, (b) relevant
(same task key), and (c) more efficient than the current run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from phone_agent.v2.evolution import _task_key
from phone_agent.v2.recall import (
    MlxEmbedder,
    VecIndex,
    episode_is_indexable,
    read_episode_events,
)


def replay_exemplar_metrics(
    experience_dir: str = "memory/experience",
    *,
    embedder: Any | None = None,
    min_steps: int = 2,
    min_score: float = 0.50,
    top_k: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """Replay episodes chronologically and score the exemplar channel.

    The replay simulates each run's start-of-run recall window: a run may only
    see episodes that ended before it.  An in-memory ``VecIndex`` is rebuilt from
    the experience log so the metric never mutates production indexes.
    """

    events_path = Path(experience_dir) / "events.jsonl"
    events = read_episode_events(events_path)
    events.sort(key=lambda item: (float(item.get("ts_end", 0.0)), str(item.get("run_id", ""))))

    run_by_id = {str(item["run_id"]): item for item in events}

    active_embedder = embedder if embedder is not None else MlxEmbedder()

    runs_total = len(events)
    runs_with_candidates = 0
    covered = 0
    relevant = 0
    deltas: list[int] = []
    details: list[dict[str, Any]] = []

    with VecIndex(":memory:", embedder=active_embedder) as index:
        for event in events:
            run_id = str(event["run_id"])
            goal = str(event.get("goal_text", ""))
            device_scope = str(event.get("device_scope", ""))
            run_steps = int(event.get("steps", 0) or 0)

            top: dict[str, Any] | None = None
            if episode_is_indexable(event, min_steps=min_steps):
                candidates = index.recall(
                    goal,
                    device_scope=device_scope,
                    namespaces=("episode",),
                    top_k=top_k,
                    min_score=min_score,
                    now=now,
                )
                if candidates:
                    top = candidates[0]
                    runs_with_candidates += 1

            hit_event = run_by_id.get(str(top["ref_id"])) if top else None

            qualified = False
            is_relevant = False
            delta: int | None = None
            hit_run_id: str | None = None
            hit_steps: int | None = None

            if hit_event is not None:
                hit_run_id = str(hit_event["run_id"])
                hit_steps = int(hit_event.get("steps", 0) or 0)
                qualified = (
                    hit_event.get("success") is True
                    and hit_steps >= min_steps
                )
                is_relevant = qualified and _task_key(hit_event) == _task_key(event)
                if is_relevant:
                    delta = run_steps - hit_steps
                    deltas.append(delta)

            if qualified:
                covered += 1
            if is_relevant:
                relevant += 1

            details.append(
                {
                    "run_id": run_id,
                    "goal": goal[:60],
                    "hit_run_id": hit_run_id,
                    "qualified": qualified,
                    "relevant": is_relevant,
                    "delta": delta,
                    "steps_run": run_steps,
                    "steps_hit": hit_steps,
                }
            )

            # Production behavior: always attempt to index, even if the run is
            # below the quality gate (index_episode will no-op in that case).
            index.index_episode(event, min_steps=min_steps)

    coverage_rate = round(covered / runs_total, 6) if runs_total else 0.0
    relevance_rate = round(relevant / covered, 6) if covered else 0.0

    if deltas:
        steps_delta = {
            "min": min(deltas),
            "max": max(deltas),
            "mean": round(statistics.mean(deltas), 6),
            "median": round(statistics.median(deltas), 6),
            "positive_share": round(sum(1 for value in deltas if value > 0) / len(deltas), 6),
        }
    else:
        steps_delta = {
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "positive_share": None,
        }

    coverage_ok = coverage_rate >= 0.30
    relevance_ok = relevance_rate >= 0.70
    median_delta = steps_delta["median"]
    delta_ok = median_delta is not None and median_delta > 0

    return {
        "runs_total": runs_total,
        "runs_with_candidates": runs_with_candidates,
        "covered": covered,
        "relevant": relevant,
        "coverage_rate": coverage_rate,
        "relevance_rate": relevance_rate,
        "steps_delta": steps_delta,
        "gates": {
            "coverage_ok": coverage_ok,
            "relevance_ok": relevance_ok,
            "delta_ok": delta_ok,
            "channel_recommended": coverage_ok and relevance_ok and delta_ok,
        },
        "details": details,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay exemplar-channel metrics for historical episodes."
    )
    parser.add_argument(
        "--experience-dir",
        default="memory/experience",
        help="Directory containing events.jsonl (default: memory/experience)",
    )
    parser.add_argument(
        "--min-steps",
        type=int,
        default=2,
        help="Minimum step count for a usable episode (default: 2)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.50,
        help="Minimum recall score for a candidate (default: 0.50)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=1,
        help="Episode recall quota per run (default: 1)",
    )
    args = parser.parse_args()

    result = replay_exemplar_metrics(
        experience_dir=args.experience_dir,
        min_steps=args.min_steps,
        min_score=args.min_score,
        top_k=args.top_k,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    _main()
