"""Typed synchronous event bus for v2 capability/plugin seams.

Events use ``domain/name`` identifiers matching ``^[a-z]+/[a-z_]+$``.

Payload contracts for the first plugin events:

``RUN_START`` / ``"run/start"``
    Observed with :meth:`EventBus.emit`. Payload is a mapping containing
    ``run_id``, ``goal``, and a compact config summary.

``RUN_END`` / ``"run/end"``
    Observed with :meth:`EventBus.emit`. Payload is a mapping containing
    ``run_id`` plus the terminal result summary.

``OBSERVE`` / ``"observe"``
    Observed with :meth:`EventBus.emit` after an observation is committed.
    Payload contains ``epoch``, ``screen_seq``, ``marks_count``, and
    ``marks_failure_code``.

``TOOL_PRE_EXECUTE`` / ``"tool/pre_execute"``
    Applied with :meth:`EventBus.waterfall`. Payload is the tool-call object
    seen by middleware: tool ``name``, ``args``, and session-facing request
    context. Listeners receive ``(payload, next)``; calling ``next(payload)``
    delegates to downstream listeners and ultimately to the terminal
    execution function. Returning without calling ``next`` short-circuits the
    chain. ``REJECT`` is the policy-rejection sentinel.

``MODEL_PRE_REQUEST`` / ``"model/pre_request"``
    Applied with :meth:`EventBus.waterfall`. Payload is a copy of the model
    request messages list. Listeners receive ``(payload, next)`` and may
    return a replacement list, optionally wrapping the result of ``next``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

RUN_START = "run/start"
RUN_END = "run/end"
OBSERVE = "observe"
TOOL_PRE_EXECUTE = "tool/pre_execute"
MODEL_PRE_REQUEST = "model/pre_request"

_EVENT_RE = re.compile(r"^[a-z]+/[a-z_]+$")
_RESERVED_EVENTS = frozenset(
    {
        RUN_START,
        RUN_END,
        OBSERVE,
        TOOL_PRE_EXECUTE,
        MODEL_PRE_REQUEST,
    }
)


class _Reject:
    def __repr__(self) -> str:
        return "REJECT"


REJECT = _Reject()

Listener = Callable[[Any], Any]
WaterfallListener = Callable[[Any, Callable[[Any], Any]], Any]
Disposer = Callable[[], None]

_LOGGER = logging.getLogger(__name__)


def validate_event_name(event: str) -> str:
    """Return ``event`` if it is a valid ``domain/name`` identifier."""

    if not isinstance(event, str) or (
        event not in _RESERVED_EVENTS and not _EVENT_RE.fullmatch(event)
    ):
        raise ValueError(f"invalid event name: {event!r}")
    return event


class EventBus:
    """Small synchronous event bus for capability-owned extensions."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def on(
        self,
        event: str,
        listener: Listener | WaterfallListener,
        *,
        prepend: bool = False,
    ) -> Disposer:
        """Register ``listener`` and return an idempotent disposer.

        ``emit``/``serial`` listeners receive ``(payload)``. ``waterfall``
        listeners receive ``(payload, next)`` where ``next`` continues the
        chain and returns the value produced by the rest of the waterfall.
        """

        event = validate_event_name(event)
        if not callable(listener):
            raise TypeError("event listener must be callable")
        listeners = self._listeners.setdefault(event, [])
        if prepend:
            listeners.insert(0, listener)
        else:
            listeners.append(listener)

        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            current = self._listeners.get(event)
            if not current:
                return
            try:
                current.remove(listener)
            except ValueError:
                return
            if not current:
                self._listeners.pop(event, None)

        return dispose

    def emit(self, event: str, payload: Any) -> None:
        """Run observers in order; listener failures are logged and swallowed."""

        event = validate_event_name(event)
        for listener in tuple(self._listeners.get(event, ())):
            try:
                listener(payload)
            except Exception:  # noqa: BLE001 - observation events are fail-open
                _LOGGER.exception("event listener failed during emit: %s", event)

    def waterfall(
        self, event: str, payload: Any, terminal: Callable[[Any], Any]
    ) -> Any:
        """Pass ``payload`` through listeners; ``REJECT`` short-circuits.

        Each listener is called as ``listener(payload, next)`` where ``next``
        is a callable that continues the chain. Calling ``next(payload)``
        delegates to downstream listeners and returns the value produced by
        the rest of the chain (including ``terminal``). Returning without
        calling ``next`` short-circuits the chain. ``REJECT`` is the policy-
        rejection sentinel; callers treat it the same as a short-circuit for
        execution purposes.
        """

        event = validate_event_name(event)
        listeners = tuple(self._listeners.get(event, ()))

        def make_next(index: int) -> Callable[[Any], Any]:
            if index >= len(listeners):
                return terminal
            listener = listeners[index]

            def _next(current_payload: Any) -> Any:
                return listener(current_payload, make_next(index + 1))

            return _next

        return make_next(0)(payload)

    def serial(self, event: str, payload: Any) -> list[Any]:
        """Run listeners in order and collect their return values."""

        event = validate_event_name(event)
        return [listener(payload) for listener in tuple(self._listeners.get(event, ()))]


__all__ = [
    "Disposer",
    "EventBus",
    "Listener",
    "MODEL_PRE_REQUEST",
    "OBSERVE",
    "REJECT",
    "RUN_END",
    "RUN_START",
    "TOOL_PRE_EXECUTE",
    "WaterfallListener",
    "validate_event_name",
]
