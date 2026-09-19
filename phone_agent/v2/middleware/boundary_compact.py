"""Boundary-aware online compact: fold history when a route item finishes.

Auto-compact (:mod:`.compact`) fires on *capacity* — the transcript is already
approaching the window, so the fold is paid for under pressure and its cut is
chosen by a token count. A capacity-triggered fold is a rescue, and rescues are
expensive: everything already sent was sent at full size.

This capability adds the cheap trigger. The TaskDoc board already records which
route item the model is working on, and P0 #11 guarantees a completion passes
through ``in_progress`` — so the committed ``in_progress -> completed`` transition
(``taskdoc/completed``) is a real, harness-visible subtask boundary. The model
supplies the *signal* (it owns the plan; the harness must not guess one) and this
listener owns the *decision*: mechanically, from run-local counters and the
shared token estimator, it asks whether folding the just-finished span repays the
summary call over the steps that remain. If it does not, nothing happens and the
reason is logged; the capacity trigger keeps the last word either way.

Mounting is the usual capability seam (:mod:`phone_agent.v2.capabilities`): one
observer on ``taskdoc/completed``, one listener on ``model/pre_request`` (inside
the compact listener, so a fold that capacity already paid for is never
double-folded), and one ``run/start`` hook that clears per-run counters.

Modes (``PHONE_AGENT_BOUNDARY_COMPACT``, default ``shadow``):

* ``off`` — the capability never mounts; behaviour is identical to a build
  without this file.
* ``shadow`` — decisions are computed and logged as ``boundary_compact_decision``
  trace events; the ``model/pre_request`` listener is a pure pass-through and the
  transcript is byte-for-byte what it would be without the capability.
* ``on`` — a favourable decision may request a fold through
  :meth:`.compact.CompactMiddleware.request_semantic_fold`, which runs the
  existing fold gates. A refusal from any of them leaves the transcript alone.

Tuning keys and their defaults are read in :func:`build_boundary_compact_listener`
from ``V2Config``; nothing here hardcodes an endpoint, a key, or a model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from phone_agent.v2.events import MODEL_PRE_REQUEST, TASKDOC_ITEM_COMPLETED
from phone_agent.v2.middleware._tokens import estimate_context_tokens
from phone_agent.v2.taskdoc import OPEN_STATUSES

# One thin-loop step adds an assistant tool-call message plus its receipt, so a
# span of N observations is about 2N messages.
MESSAGES_PER_STEP = 2


@dataclass(frozen=True)
class BoundaryDecision:
    """The mechanical verdict for one boundary, with every number behind it."""

    fold: bool
    reason: str
    numbers: dict[str, Any]


def open_route_items(doc: Any) -> int:
    """Count ``pending``/``in_progress`` items on a task board (duck-typed)."""

    items = getattr(doc, "items", None) or []
    return sum(1 for item in items if getattr(item, "status", "") in OPEN_STATUSES)


def steps_per_completed_item(
    *, completed_items: int, steps_total: int, prior: int, guard_items: int
) -> float:
    """Run-local average steps per finished route item, guarded when thin.

    Below ``guard_items`` completions the mean is noise (one lucky quick item
    would justify folding the rest of the run away), so the configured prior is
    used instead. The prior is deliberately small: over-estimating the horizon is
    the expensive mistake, under-estimating only defers a fold to the trigger
    that must fire anyway.
    """

    guard = max(1, int(guard_items))
    if completed_items < guard:
        return float(max(0, int(prior)))
    return max(0, int(steps_total)) / float(completed_items)


def evaluate_boundary_fold(
    *,
    messages: Sequence[Any],
    span_steps: int,
    completed_items: int,
    steps_total: int,
    open_items: int,
    summary_tokens: int,
    prior_steps_per_item: int,
    guard_items: int,
    min_horizon_steps: int,
    min_span_tokens: int,
    min_net_ratio: float,
) -> BoundaryDecision:
    """Decide whether folding the completed span repays itself. Pure.

    ``span_steps`` is how many observations the finished item covered; the rest of
    the inputs are run-local counters and resolved config values. The estimate is
    deliberately coarse — an average token-per-message gauge over a message-count
    estimate for the span — because it only has to separate an obvious win from a
    coin flip:

    * ``benefit = max(0, span_tokens - summary_tokens) * horizon_steps`` — every
      remaining step stops carrying the span and starts carrying the summary;
    * ``cost = span_tokens + summary_tokens`` — one summariser pass over the
      span, plus its bounded output;
    * fold only when ``benefit >= min_net_ratio * cost`` (clearly net-positive,
      not marginally) and both the span and the horizon clear their floors.
    """

    if not messages:
        return BoundaryDecision(False, "no_messages", {})
    if open_items <= 0:
        return BoundaryDecision(False, "no_open_route_items", {})

    per_message = estimate_context_tokens(messages) / float(len(messages))
    span_messages = max(0, int(span_steps)) * MESSAGES_PER_STEP
    span_tokens = int(per_message * span_messages)
    per_step_tokens = int(per_message * MESSAGES_PER_STEP)
    average = steps_per_completed_item(
        completed_items=completed_items,
        steps_total=steps_total,
        prior=prior_steps_per_item,
        guard_items=guard_items,
    )
    horizon = float(open_items) * average

    numbers = {
        "span_steps": int(span_steps),
        "span_tokens": span_tokens,
        "per_step_tokens": per_step_tokens,
        "completed_items": int(completed_items),
        "steps_total": int(steps_total),
        "open_items": int(open_items),
        "steps_per_item": round(average, 2),
        "horizon_steps": round(horizon, 2),
        "summary_tokens": int(summary_tokens),
    }
    if span_tokens < max(0, int(min_span_tokens)):
        numbers["cost_tokens"] = span_tokens + int(summary_tokens)
        return BoundaryDecision(False, "span_too_small", numbers)

    saving = max(0, span_tokens - int(summary_tokens)) * horizon
    cost = span_tokens + int(summary_tokens)
    numbers["benefit_tokens"] = int(saving)
    numbers["cost_tokens"] = int(cost)
    numbers["net_ratio"] = round(saving / cost, 2) if cost else 0.0
    if horizon < float(min_horizon_steps):
        return BoundaryDecision(False, "horizon_too_short", numbers)
    if saving < float(min_net_ratio) * cost:
        return BoundaryDecision(False, "not_net_positive", numbers)
    return BoundaryDecision(True, "net_positive", numbers)


class BoundaryCompactListener:
    """Record route boundaries; ask for a fold when the economics say so."""

    def __init__(
        self,
        session: Any,
        config: Any,
        *,
        compact_provider: Callable[[], Any] | None = None,
        trace_recorder: Callable[..., Any] | None = None,
        mode: str = "shadow",
    ) -> None:
        self.session = session
        self.config = config
        self.mode = str(mode or "shadow").strip().lower()
        self._compact_provider = compact_provider
        self._trace_recorder = trace_recorder
        self._pending: dict[str, Any] | None = None
        self._last_boundary_seq = 0
        self.completed_items = 0
        self.decisions = 0
        self.folds = 0
        self.reset()

    def reset(self) -> None:
        """Clear per-run counters (a reused agent must behave like a fresh one)."""

        self._pending = None
        self._last_boundary_seq = 0
        self.completed_items = 0
        self.decisions = 0
        self.folds = 0

    # -- boundary signal ---------------------------------------------------
    def on_taskdoc_completed(self, payload: Any) -> None:
        """Remember the span a finished route item covered, from its event.

        ``span_steps`` counts observations since the previous boundary (or run
        start): that is the work the item just finished. Fail-open — this is an
        observer, and a malformed payload may only cost a decision, never a step.
        """

        if not isinstance(payload, dict):
            return
        item_ids = payload.get("item_ids")
        if not item_ids:
            return
        try:
            screen_seq = int(payload.get("screen_seq") or 0)
        except (TypeError, ValueError):
            return
        span_steps = max(0, screen_seq - self._last_boundary_seq)
        self.completed_items += len(list(item_ids))
        self._last_boundary_seq = screen_seq
        self._pending = {
            "item_ids": [str(item) for item in item_ids],
            "span_steps": span_steps,
            "seq": screen_seq,
        }

    # -- decision checkpoint ----------------------------------------------
    def on_pre_request(self, messages: Any, next: Any) -> Any:  # noqa: ANN001
        """``model/pre_request`` listener: pure pass-through unless a fold is due.

        The waterfall contract is full list in, full list out and never a
        ``RemoveMessage``; in ``shadow`` the incoming object is forwarded
        untouched, which is what makes the mode behaviour-neutral. One boundary
        gets exactly one decision, in both modes, so ``shadow`` is a faithful
        counterfactual of ``on`` and neither can thrash on a stalled span.
        """

        pending = self._pending
        if pending is None:
            return next(messages)
        decision = self._decide(messages, pending)
        if decision is None:
            # No observation has committed since the boundary: the span may still
            # be growing, so stay armed and change nothing this turn.
            return next(messages)
        self._pending = None
        self.decisions += 1
        self._record(decision, pending)
        if not decision.fold or self.mode != "on":
            return next(messages)
        return self._fold(messages, pending, decision, next)

    def _fold(
        self, messages: Any, pending: dict, decision: BoundaryDecision, next: Any  # noqa: ANN001
    ) -> Any:
        """One attempt for this boundary, through the existing fold gates."""

        compact = self._compact()
        request_fold = getattr(compact, "request_semantic_fold", None)
        if not callable(request_fold):
            return next(messages)
        keep_recent = self._turns_since_boundary(pending)
        try:
            folded = request_fold(messages, span_hint=keep_recent)
        except Exception:  # noqa: BLE001 - a refused fold must not break the turn
            return next(messages)
        if not isinstance(folded, list) or not folded:
            gate = getattr(compact, "last_result", None)
            self._record(
                decision,
                pending,
                event="boundary_compact_fold",
                extra={
                    "committed": False,
                    "gate_reason": str(
                        (gate or {}).get("reason", "fold_not_committed")
                    )[:64],
                },
            )
            return next(messages)
        self.folds += 1
        self._record(
            decision, pending, event="boundary_compact_fold", extra={"committed": True}
        )
        return next(folded)

    def _turns_since_boundary(self, pending: dict) -> int:
        """Trailing turns that post-date the boundary and must stay verbatim."""

        return max(1, int(self._steps_total()) - int(pending.get("seq") or 0))

    def _decide(self, messages: Any, pending: dict) -> BoundaryDecision | None:
        """Verdict for the pending boundary, or ``None`` while it is not yet due."""

        steps_total = self._steps_total()
        if steps_total <= int(pending.get("seq") or 0):
            return None
        doc = getattr(self.session, "task_doc", None)
        return evaluate_boundary_fold(
            messages=list(messages or []),
            span_steps=int(pending.get("span_steps") or 0),
            completed_items=self.completed_items,
            steps_total=steps_total,
            open_items=open_route_items(doc),
            summary_tokens=int(getattr(self.config, "compact_summary_tokens", 2000) or 0),
            prior_steps_per_item=int(
                getattr(self.config, "boundary_compact_steps_per_item", 4) or 0
            ),
            guard_items=int(
                getattr(self.config, "boundary_compact_sample_guard_items", 2) or 0
            ),
            min_horizon_steps=int(
                getattr(self.config, "boundary_compact_min_horizon_steps", 2) or 0
            ),
            min_span_tokens=int(
                getattr(self.config, "boundary_compact_min_span_tokens", 1500) or 0
            ),
            min_net_ratio=float(
                getattr(self.config, "boundary_compact_min_net_ratio", 1.5) or 0.0
            ),
        )

    def _steps_total(self) -> int:
        return int(getattr(self.session, "screen_seq", 0) or 0)

    def _compact(self) -> Any:
        return self._compact_provider() if callable(self._compact_provider) else None

    def _record(
        self, decision: BoundaryDecision, pending: dict, *, event: str | None = None, extra: dict | None = None
    ) -> None:
        """Log one decision; the trace recorder owns redaction and truncation."""

        recorder = self._trace_recorder
        if recorder is None:
            return
        payload: dict[str, Any] = {
            "mode": self.mode,
            "fold": decision.fold,
            "reason": decision.reason,
            "item_ids": list(pending.get("item_ids") or []),
            **decision.numbers,
        }
        if extra:
            payload.update(extra)
        try:
            recorder(event or "boundary_compact_decision", **payload)
        except Exception:  # noqa: BLE001 - diagnostics never change the request
            return


def build_boundary_compact_listener(
    session: Any,
    config: Any,
    *,
    compact_provider: Callable[[], Any] | None = None,
    trace_recorder: Callable[..., Any] | None = None,
) -> BoundaryCompactListener:
    """Build a listener from resolved config values."""

    return BoundaryCompactListener(
        session,
        config,
        compact_provider=compact_provider,
        trace_recorder=trace_recorder
        or getattr(session, "resolution_trace_recorder", None),
        mode=getattr(config, "boundary_compact_mode", "shadow"),
    )


def apply_boundary_compact(ctx: Any, listener_factory=None) -> bool:
    """Mount one boundary listener on the harness seams; ``False`` when inert.

    Called by the ``boundary_compact`` capability. It needs the compact instance
    (its fold path), a session and the event bus; any of those missing means there
    is nothing to trigger or nothing to fold, so the capability registers nothing
    rather than half a policy.
    """

    session = ctx.service("session")
    config = ctx.service("config")
    if session is None or config is None:
        return False
    if str(getattr(config, "boundary_compact_mode", "shadow") or "shadow").lower() == "off":
        return False
    compact = ctx.service("compact_instance")
    if compact is None:
        return False
    bus = ctx.service("event_bus")
    if bus is None:
        return False
    factory = listener_factory or (
        lambda: build_boundary_compact_listener(
            session,
            config,
            compact_provider=lambda: ctx.service("compact_instance"),
            trace_recorder=getattr(session, "resolution_trace_recorder", None),
        )
    )
    listener = factory()
    ctx.on(TASKDOC_ITEM_COMPLETED, listener.on_taskdoc_completed)
    ctx.on(MODEL_PRE_REQUEST, listener.on_pre_request)
    ctx.add_run_hook("start", lambda _state: listener.reset())
    ctx.register_service("boundary_compact_listener", listener)
    return True


__all__ = [
    "BoundaryCompactListener",
    "BoundaryDecision",
    "apply_boundary_compact",
    "build_boundary_compact_listener",
    "evaluate_boundary_fold",
    "open_route_items",
    "steps_per_completed_item",
]
