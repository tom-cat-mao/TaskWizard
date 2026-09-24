"""``wait_for_stable``: fold a bounded wait-for-quiet into one tool call.

The thin loop has a built-in ``wait`` tool that sleeps once and re-observes.
That is enough when the model can guess the right delay, and wrong in both
directions when it cannot: too short and the next action addresses a screen that
is still rendering, too long and the run pays for time it did not need.  This
tool replaces the guess with the fact — it polls ``session.observe()`` and stops
when the committed frame's ``screen_hash`` (G7, added in plugin-plane Round 2)
has been identical for N consecutive polls.

Discipline this tool keeps:

* **P0 #15** — ``session.observe()`` stays the single observation producer; the
  tool never reaches for a screenshot or an accessibility dump itself, it just
  asks for the next atomic observation.
* **P0 #5** — every outcome is reported as what happened: a timeout is
  ``stable=no``, an observation failure is an error string, and a session whose
  frames carry no hash says ``reason=no_screen_hash`` instead of inventing
  stability from a comparison it could not make.
* **Freshly minted batch** — each poll commits a new observation batch, so every
  mark id the model held is invalidated.  The receipt says so, because the tool
  is text-only and the model cannot see the new screenshot.

Declared risk: ``readonly`` — the tool observes the device and never touches it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

STALE_MARKS_NOTE = (
    "本工具已提交新的观测批次（此前 mark id 失效）；"
    "需要 target_mark_id 寻址时先 read_screen。"
)
DEFAULT_MAX_WAIT_S = 6.0
DEFAULT_INTERVAL_S = 0.5
DEFAULT_CONSECUTIVE = 2
#: Per-call override band for ``max_wait_s`` (the declared setting is a default,
#: the band is the hard bound).
MAX_WAIT_BAND: tuple[float, float] = (0.5, 60.0)
MIN_INTERVAL_S = 0.05


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _observation_line(obs: Any, session: Any) -> str:
    seq = getattr(obs, "screen_seq", getattr(session, "screen_seq", 0))
    app = getattr(obs, "current_app", None) or "?"
    return f"screen#{seq} app={app}"


def _failure_text(message: str, *, polls: int, elapsed_ms: int) -> str:
    return (
        "wait_for_stable: stable=no "
        f"reason=observe_failed polls={polls} elapsed_ms={elapsed_ms} "
        f"| {message[:120]}"
    )


def make_wait_for_stable_tool(
    session: Any,
    *,
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    interval_s: float = DEFAULT_INTERVAL_S,
    consecutive: int = DEFAULT_CONSECUTIVE,
    sleep: Callable[[float], Any] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StructuredTool:
    """Build the tool bound to ``session`` and the resolved declared settings.

    ``sleep``/``monotonic`` are injectable so the offline suite exercises the
    real polling loop without paying wall-clock time.
    """

    default_wait = _clamp(float(max_wait_s), *MAX_WAIT_BAND)
    step = max(MIN_INTERVAL_S, float(interval_s))
    target_streak = max(2, int(consecutive))

    def wait_for_stable(
        intent: str = "",
        note: str | None = None,
        max_wait_s: float | None = None,
        settle_ms: int | None = None,
    ) -> str:
        """Wait until the screen stops changing, then return a receipt.

        Polls the device until the same picture is seen `consecutive` times in a
        row (`screen_hash` of committed observations), or until the wait budget
        runs out. Use it after an action whose result renders asynchronously
        (search, page loads, submit) instead of guessing a sleep.

        `max_wait_s` overrides this deployment's default wait budget (clamped to
        0.5–60s); `settle_ms` replaces the global per-observation delay.
        Always pass `intent` (this step's goal). `note` optionally records what
        you discovered this step.
        """

        limit = default_wait if max_wait_s is None else _clamp(float(max_wait_s), *MAX_WAIT_BAND)
        started = monotonic()
        deadline = started + limit
        polls = 0
        streak = 0
        last_hash: str | None = None
        last_obs: Any = None

        while True:
            try:
                last_obs = (
                    session.observe()
                    if settle_ms is None
                    else session.observe(settle_ms=settle_ms)
                )
            except Exception as exc:  # noqa: BLE001 - report the fact, never fake quiet
                elapsed_ms = int((monotonic() - started) * 1000)
                return _failure_text(
                    f"{type(exc).__name__}: {exc}", polls=polls, elapsed_ms=elapsed_ms
                )
            polls += 1
            digest = str(getattr(last_obs, "screen_hash", "") or "")
            if not digest:
                elapsed_ms = int((monotonic() - started) * 1000)
                return (
                    "wait_for_stable: stable=no reason=no_screen_hash "
                    f"polls={polls} elapsed_ms={elapsed_ms} | "
                    "该 session 的观测不带 screen_hash，无法判断画面是否变化"
                )
            streak = streak + 1 if digest == last_hash else 1
            last_hash = digest
            elapsed_ms = int((monotonic() - started) * 1000)
            if streak >= target_streak:
                return (
                    f"wait_for_stable: stable=yes consecutive={streak} polls={polls} "
                    f"elapsed_ms={elapsed_ms} {_observation_line(last_obs, session)}\n"
                    f"{STALE_MARKS_NOTE}"
                )
            now = monotonic()
            if now >= deadline:
                return (
                    f"wait_for_stable: stable=no reason=timeout "
                    f"consecutive={streak}/{target_streak} polls={polls} "
                    f"elapsed_ms={elapsed_ms} {_observation_line(last_obs, session)}\n"
                    f"{STALE_MARKS_NOTE}"
                )
            sleep(min(step, max(0.0, deadline - now)))

    return StructuredTool.from_function(wait_for_stable, parse_docstring=True)


__all__ = [
    "DEFAULT_CONSECUTIVE",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_WAIT_S",
    "MAX_WAIT_BAND",
    "MIN_INTERVAL_S",
    "STALE_MARKS_NOTE",
    "make_wait_for_stable_tool",
]
