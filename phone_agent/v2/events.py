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
    context. Listeners may return a replacement payload or ``REJECT`` to
    short-circuit execution.

``MODEL_PRE_REQUEST`` / ``"model/pre_request"``
    Applied with :meth:`EventBus.waterfall`. Payload is a copy of the model
    request messages list. Listeners may return a replacement list.
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
        self, event: str, listener: Listener, *, prepend: bool = False
    ) -> Disposer:
        """Register ``listener`` and return an idempotent disposer."""

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

    def waterfall(self, event: str, payload: Any) -> Any:
        """Pass ``payload`` through listeners; ``REJECT`` short-circuits."""

        event = validate_event_name(event)
        current = payload
        for listener in tuple(self._listeners.get(event, ())):
            current = listener(current)
            if current is REJECT:
                return REJECT
        return current

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
    "validate_event_name",
]
