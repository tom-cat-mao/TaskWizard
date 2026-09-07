"""Offline replay metrics for the two workflow-memory channels (observe-only).

Both channels replay history in timestamp order and never mutate production
state; the exemplar channel rebuilds an in-memory ``VecIndex``, the procedure
channel rebuilds the lesson card pool from the lesson event log.

``exemplar``
    Whole-episode precedent recall: how often the top-1 recalled successful
    precedent is (a) available, (b) relevant (same task key), and (c) more
    efficient than the current run.  Already scored ``channel_recommended:
    False`` on 20 real episodes.

``procedure`` (WP-WF2b)
    Procedure-card recall: for every episode, rebuild the set of cards that
    would have been injectable when that run started, then apply the design's
    hard filter (card ``app_scope`` must be a package the episode itself
    launched successfully; episodes without an extractable receipt fall back to
    the cross-app ``general`` pool) and its soft ranking (cosine between the
    goal vector and the card ``title + steps`` vector), and take the top-1.

Relevance is a proxy, not a measurement (procedure channel)
    Offline replay can only score a *proxy* for semantic relevance:

    * the app half is free — the hard filter already guarantees that a hit
      card's ``app_scope`` is a package the episode really launched, so an
      app-scoped hit is relevant by construction and can never be a false
      positive on that axis;
    * the semantic half degenerates into "the top-1 cosine cleared
      ``min_score``", i.e. vector similarity to the goal text, which is *not*
      evidence that the card's steps actually applied to that goal.

    So ``relevance_rate`` (relevant / covered) really measures "of the covered
    episodes, how many were app-grounded rather than served by the cross-app
    ``general`` pool" — episodes with no launch receipt can never score
    relevant, which biases the rate down.  It is a floor, not an estimate, of
    true semantic alignment.  Whether an injected card actually helps is an
    online question: design §6 cuts the control group, so only injection ids
    land in the trace/episode for later analysis.

steps-delta is N/A (procedure channel)
    A card is distilled *from* the episodes that would have to form its
    counterfactual, so there is no step count to subtract and no way to replay
    an alternative history.  The gate is reported as ``N/A`` and is excluded
    from the verdict: the procedure channel passes or fails on coverage and
    relevance alone.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from phone_agent.v2.evolution import (
    GENERAL_APP_SCOPE,
    LESSON_EVENT_TYPES,
    LessonCandidate,
    _APPROVABLE_STATUSES,
    _DEMOTABLE_STATUSES,
    _PROPOSAL_STATUSES,
    _demoted_status,
    _task_key,
    _tool_ledgers,
    lesson_injectable,
)
from phone_agent.v2.recall import (
    MlxEmbedder,
    VecIndex,
    episode_is_indexable,
    read_episode_events,
)

PROCEDURE_SWEEP_START = 0.30
PROCEDURE_SWEEP_STOP = 0.70
PROCEDURE_SWEEP_STEP = 0.05
PROCEDURE_COVERAGE_TARGET = 0.30
PROCEDURE_RELEVANCE_TARGET = 0.60

_STEPS_DELTA_NA: dict[str, Any] = {
    "applicable": False,
    "reason": (
        "not measurable offline: a procedure card is distilled from the very "
        "episodes that would have to form its counterfactual, so there is no "
        "step count to subtract; the gate stays N/A and does not decide the "
        "channel verdict"
    ),
}



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


def _event_ts(event: Mapping[str, Any], *, floor: float) -> float:
    """Read a lesson event timestamp, never going back before ``floor``."""

    value = event.get("ts")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return max(number, floor)
    return floor


def _injectable_procedure_cards(
    current: Mapping[str, LessonCandidate],
) -> tuple[LessonCandidate, ...]:
    """Return the currently injectable procedure cards, stably ordered."""

    cards = [
        card
        for card in current.values()
        if card.kind == "procedure" and lesson_injectable(card)
    ]
    return tuple(sorted(cards, key=lambda card: card.lesson_id))


def _procedure_pool_timeline(
    events_path: Path,
) -> list[tuple[float, tuple[LessonCandidate, ...]]]:
    """Replay the lesson log into ``(ts, injectable procedure pool)`` snapshots.

    The timeline holds one entry per *change* of the injectable pool, so a run
    only ever sees cards that were injectable when it started — a card approved
    afterwards, or one that was later revoked or demoted, is invisible to every
    earlier episode.  Timestamps are clamped to be non-decreasing.
    """

    current: dict[str, LessonCandidate] = {}
    timeline: list[tuple[float, tuple[LessonCandidate, ...]]] = []
    if not events_path.exists():
        return timeline

    last_ts = 0.0
    last_pool: tuple[LessonCandidate, ...] = ()
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                kind = event["type"]
                if kind not in LESSON_EVENT_TYPES or event.get("schema_v") != 1:
                    continue
                timestamp = _event_ts(event, floor=last_ts)
                last_ts = timestamp
                changed = False
                if kind in {"lesson_proposed", "lesson_superseded"}:
                    candidate = LessonCandidate.from_dict(event["lesson"])
                    if candidate.status not in _PROPOSAL_STATUSES:
                        continue
                    if kind == "lesson_proposed":
                        if candidate.lesson_id in current or candidate.version != 1:
                            continue
                    else:
                        prior = current.get(candidate.lesson_id)
                        if (
                            prior is None
                            or event.get("lesson_id") != candidate.lesson_id
                            or event.get("from_version") != prior.version
                            or candidate.version != prior.version + 1
                        ):
                            continue
                    current[candidate.lesson_id] = candidate
                    changed = True
                else:
                    lesson_id = str(event["lesson_id"])
                    candidate = current.get(lesson_id)
                    if candidate is None or event.get("version") != candidate.version:
                        continue
                    if (
                        kind == "lesson_approved"
                        and candidate.status in _APPROVABLE_STATUSES
                    ):
                        current[lesson_id] = replace(candidate, status="approved")
                        changed = True
                    elif kind == "lesson_revoked" and candidate.status != "revoked":
                        current[lesson_id] = replace(candidate, status="revoked")
                        changed = True
                    elif (
                        kind == "lesson_demoted"
                        and candidate.status in _DEMOTABLE_STATUSES
                    ):
                        current[lesson_id] = replace(
                            candidate, status=_demoted_status(candidate)
                        )
                        changed = True
                if changed:
                    pool = _injectable_procedure_cards(current)
                    if pool != last_pool:
                        timeline.append((timestamp, pool))
                        last_pool = pool
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return timeline


def _pool_at(
    timeline: Sequence[tuple[float, tuple[LessonCandidate, ...]]], timestamp: float
) -> tuple[LessonCandidate, ...]:
    """Return the pool in force at ``timestamp`` (empty before the first event)."""

    pool: tuple[LessonCandidate, ...] = ()
    for event_ts, snapshot in timeline:
        if event_ts > timestamp:
            break
        pool = snapshot
    return pool


def _procedure_card_text(lesson: LessonCandidate) -> str:
    """Return the soft-ranking document: card title plus its semantic steps."""

    return "\n".join([str(lesson.text), *lesson.steps])


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Return the cosine of two embedder-normalized vectors."""

    return sum(value * other for value, other in zip(left, right))


def _launched_packages(events_path: Path) -> dict[str, set[str]]:
    """Return packages each run really launched, from its own success receipts."""

    packages: dict[str, set[str]] = {}
    for run_id, entries in _tool_ledgers(events_path).items():
        found = {
            entry["app_package"]
            for entry in entries
            if entry["tool"] == "launch_app"
            and entry["result_class"] == "ok"
            and entry["app_package"]
        }
        if found:
            packages[run_id] = found
    return packages


def replay_procedure_metrics(
    experience_dir: str = "memory/experience",
    lessons_dir: str = "memory/lessons",
    *,
    embedder: Any | None = None,
    min_steps: int = 2,
    min_score: float = 0.50,
) -> dict[str, Any]:
    """Replay episodes chronologically and score the procedure channel.

    Every episode is scored against the card pool that existed when *it*
    started: the lesson log is replayed into a timeline first, so approval,
    revocation and demotion all take effect at their own timestamp.  Scoring is
    the design's hard filter (card ``app_scope`` must be a package this episode
    launched successfully; an episode without an extractable launch receipt is
    served by the cross-app ``general`` pool instead) followed by cosine
    top-1 ranking against the goal.  ``steps_delta`` is reported as N/A — see
    the module docstring.
    """

    events_path = Path(experience_dir) / "events.jsonl"
    events = read_episode_events(events_path)
    events.sort(key=lambda item: (float(item.get("ts_end", 0.0)), str(item.get("run_id", ""))))

    packages_by_run = _launched_packages(events_path)
    timeline = _procedure_pool_timeline(Path(lessons_dir) / "events.jsonl")

    runs_total = len(events)
    pool_ever_existed = any(pool for _ts, pool in timeline)

    rows: list[dict[str, Any]] = []
    for event in events:
        run_id = str(event["run_id"])
        packages = packages_by_run.get(run_id, set())
        device = str(event.get("device_scope", "")).removeprefix("device:")
        if device == "unknown":
            device = ""
        pool = [
            card
            for card in _pool_at(timeline, float(event.get("ts_start", 0.0) or 0.0))
            if card.scope.get("device") in {None, device or None}
        ]
        if packages:
            candidates = [card for card in pool if card.app_scope in packages]
            pool_kind = "app"
        else:
            candidates = [card for card in pool if card.app_scope == GENERAL_APP_SCOPE]
            pool_kind = "general"
        rows.append(
            {
                "run_id": run_id,
                "goal": str(event.get("goal_text", ""))[:60],
                "steps": int(event.get("steps", 0) or 0),
                "launched_packages": sorted(packages),
                "pool_kind": pool_kind,
                "pool_size": len(candidates),
                "queried": episode_is_indexable(event, min_steps=min_steps),
                "hit_lesson_id": None,
                "hit_app_scope": None,
                "hit_score": None,
                "covered": False,
                "relevant": False,
                "_goal": str(event.get("goal_text", "")),
                "_packages": packages,
                "_candidates": candidates,
            }
        )

    scoring = [row for row in rows if row["queried"] and row["_candidates"]]
    if scoring:
        active_embedder = embedder if embedder is not None else MlxEmbedder()
        card_index = {
            (card.lesson_id, card.version): card
            for row in scoring
            for card in row["_candidates"]
        }
        keys = sorted(card_index)
        vectors = active_embedder.embed(
            [str(row["_goal"]) for row in scoring]
            + [_procedure_card_text(card_index[key]) for key in keys]
        )
        goal_vectors = vectors[: len(scoring)]
        card_vectors = dict(zip(keys, vectors[len(scoring) :]))
        for row, goal_vector in zip(scoring, goal_vectors):
            hit: LessonCandidate | None = None
            hit_score = 0.0
            for card in row["_candidates"]:
                score = round(
                    _cosine(goal_vector, card_vectors[(card.lesson_id, card.version)]), 6
                )
                if (
                    hit is None
                    or score > hit_score
                    or (score == hit_score and card.lesson_id < hit.lesson_id)
                ):
                    hit, hit_score = card, score
            if hit is None:
                continue
            row["hit_lesson_id"] = hit.lesson_id
            row["hit_app_scope"] = hit.app_scope
            row["hit_score"] = hit_score
            row["covered"] = hit_score >= min_score
            row["relevant"] = row["covered"] and hit.app_scope in row["_packages"]

    details: list[dict[str, Any]] = []
    for row in rows:
        details.append(
            {
                key: value
                for key, value in row.items()
                if not key.startswith("_")
            }
        )

    runs_with_pool = sum(1 for row in rows if row["queried"] and row["pool_size"] > 0)
    covered = sum(1 for row in rows if row["covered"])
    relevant = sum(1 for row in rows if row["relevant"])
    coverage_rate = round(covered / runs_total, 6) if runs_total else 0.0
    coverage_rate_over_pool = (
        round(covered / runs_with_pool, 6) if runs_with_pool else 0.0
    )
    relevance_rate = round(relevant / covered, 6) if covered else 0.0

    coverage_ok = coverage_rate >= PROCEDURE_COVERAGE_TARGET
    relevance_ok = relevance_rate >= PROCEDURE_RELEVANCE_TARGET

    return {
        "channel": "procedure",
        "status": "ok" if pool_ever_existed else "empty_pool",
        "note": (
            None
            if pool_ever_existed
            else "candidate pool empty: the lesson log holds no injectable"
            " procedure card, so no episode can be covered"
        ),
        "min_score": min_score,
        "runs_total": runs_total,
        "runs_queried": sum(1 for row in rows if row["queried"]),
        "runs_with_pool": runs_with_pool,
        "covered": covered,
        "relevant": relevant,
        "coverage_rate": coverage_rate,
        "coverage_rate_over_pool": coverage_rate_over_pool,
        "relevance_rate": relevance_rate,
        "steps_delta": dict(_STEPS_DELTA_NA),
        "gates": {
            "coverage_ok": coverage_ok,
            "relevance_ok": relevance_ok,
            "delta_ok": None,
            "delta_note": "n/a",
            "channel_recommended": coverage_ok and relevance_ok,
        },
        "targets": {
            "coverage": PROCEDURE_COVERAGE_TARGET,
            "relevance": PROCEDURE_RELEVANCE_TARGET,
        },
        "details": details,
    }


def _threshold_grid(start: float, stop: float, step: float) -> list[float]:
    """Return an inclusive ``start..stop`` grid without float accumulation."""

    if step <= 0 or stop < start:
        return []
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    return [round(start + index * step, 10) for index in range(count)]


def sweep_procedure_thresholds(
    experience_dir: str = "memory/experience",
    lessons_dir: str = "memory/lessons",
    *,
    embedder: Any | None = None,
    min_steps: int = 2,
    start: float = PROCEDURE_SWEEP_START,
    stop: float = PROCEDURE_SWEEP_STOP,
    step: float = PROCEDURE_SWEEP_STEP,
    relevance_target: float = PROCEDURE_RELEVANCE_TARGET,
) -> dict[str, Any]:
    """Score the procedure channel across a ``min_score`` grid.

    The replay runs once: every row keeps its top-1 score, so each threshold
    just re-counts the same per-episode scores.  The recommended knee is the
    threshold with the **highest coverage** among those whose relevance clears
    ``relevance_target``; ties break on higher relevance, then on the loosest
    threshold (more recall at equal quality).  When no threshold reaches the
    relevance target the recommendation is ``None`` and the reason says so
    rather than silently returning a threshold that failed the gate.
    """

    result = replay_procedure_metrics(
        experience_dir,
        lessons_dir,
        embedder=embedder,
        min_steps=min_steps,
        min_score=start,
    )
    rows = result["details"]
    runs_total = result["runs_total"]

    table: list[dict[str, Any]] = []
    for threshold in _threshold_grid(start, stop, step):
        covered = [
            row
            for row in rows
            if row["hit_score"] is not None and row["hit_score"] >= threshold
        ]
        relevant = [row for row in covered if row["relevant"]]
        table.append(
            {
                "min_score": threshold,
                "covered": len(covered),
                "relevant": len(relevant),
                "coverage_rate": (
                    round(len(covered) / runs_total, 6) if runs_total else 0.0
                ),
                "relevance_rate": (
                    round(len(relevant) / len(covered), 6) if covered else 0.0
                ),
            }
        )

    eligible = [
        row for row in table if row["relevance_rate"] >= relevance_target
    ]
    if eligible:
        best = max(
            eligible,
            key=lambda row: (row["coverage_rate"], row["relevance_rate"], -row["min_score"]),
        )
        reason = (
            "highest coverage among thresholds whose relevance >= "
            f"{relevance_target}; ties break on relevance, then the loosest threshold"
        )
    elif table:
        best = max(
            table,
            key=lambda row: (row["relevance_rate"], row["coverage_rate"], -row["min_score"]),
        )
        reason = (
            f"no threshold reached relevance >= {relevance_target}; showing the "
            "best-relevance row for inspection — the channel is not recommended"
        )
    else:
        best = None
        reason = "empty threshold grid"

    if result["status"] == "empty_pool" and best is not None:
        best = None
        reason = "candidate pool empty: no injectable procedure card to score"

    return {
        "channel": "procedure",
        "status": result["status"],
        "note": result["note"],
        "runs_total": runs_total,
        "runs_with_pool": result["runs_with_pool"],
        "relevance_target": relevance_target,
        "thresholds": table,
        "recommended": None if best is None else dict(best),
        "recommended_reason": reason,
    }


def _main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay workflow-memory channel metrics for historical episodes."
    )
    parser.add_argument(
        "--channel",
        choices=("exemplar", "procedure"),
        default="exemplar",
        help="Channel to score (default: exemplar)",
    )
    parser.add_argument(
        "--experience-dir",
        default="memory/experience",
        help="Directory containing events.jsonl (default: memory/experience)",
    )
    parser.add_argument(
        "--lessons-dir",
        default="memory/lessons",
        help="Lesson event log directory, --channel procedure only (default: memory/lessons)",
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
        help="Episode recall quota per run, --channel exemplar only (default: 1)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "--channel procedure only: score min_score "
            f"{PROCEDURE_SWEEP_START}..{PROCEDURE_SWEEP_STOP}"
            f" step {PROCEDURE_SWEEP_STEP} and recommend a knee point"
        ),
    )
    args = parser.parse_args(argv)

    if args.channel == "procedure":
        if args.sweep:
            result: dict[str, Any] = sweep_procedure_thresholds(
                experience_dir=args.experience_dir,
                lessons_dir=args.lessons_dir,
                min_steps=args.min_steps,
            )
        else:
            result = replay_procedure_metrics(
                experience_dir=args.experience_dir,
                lessons_dir=args.lessons_dir,
                min_steps=args.min_steps,
                min_score=args.min_score,
            )
    else:
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
