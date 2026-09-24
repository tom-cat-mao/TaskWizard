"""Stuck governor: advisory-only loop detection on ``model/pre_request``.

The thin loop's anti-loop signal today is the TaskDoc flow line — the model can
read its own recent trajectory.  That is passive: nothing tells the *operator*
that a run has stopped making progress, and a deployment that wants to react
cannot see the pattern at all.  This listener derives the same kind of signal
from the transcript (the ``tool_calls`` of the recent ``AIMessage``s: tool name +
the ``intent`` the model declared for the step) and emits an advisory trace
event when the last ``K`` steps carry one identical signature.

Advisory only — and its shadow-only shape is a decision, not an oversight
----------------------------------------------------------------------------

This round the governor **never touches the prompt**.  The obvious alternative
(inject a "you appear to be looping, try something else" system message) is
defeated by two existing core behaviours: the auto-compact pass folds or
replaces history before/around the listeners, and every injected block that must
survive folding needs a core-side whitelist line (`pins.py`).  A plugin cannot
declare such a line — with one, the injection would vanish on the first fold and
the governor would *silently* stop working, which is worse than not shipping the
injection.  The trace event is the part that is honest today; the injection
belongs in a round that also opens the whitelist.

The event is emitted **once per distinct signature**, so a run that stays stuck
does not flood the trace with identical advisories.  When a System One client is
configured the governor additionally asks ONE ``noul`` question ("does this
repeated flow need a different approach?") and records its calibrated ``p`` next
to the deterministic signal; the ask is bounded by the client's own timeout and
degrades open (an endpoint failure still leaves the advisory, with
``error=<type>`` in the payload).  The tokens the backend reports for that ask
are booked under the declared ``systemone`` role (``unit="tokens"``).

Nothing here changes the message list: the listener returns ``next(messages)``
with the payload it received, so a shadow governor is byte-for-byte transparent
to the model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

GOVERNOR_EVENT = "systemone_governor"
#: Legal modes this round: the prompt-injecting mode is not implemented (see the
#: module docstring), so ``on`` is refused instead of silently acting as shadow.
GOVERNOR_MODES: tuple[str, ...] = ("off", "shadow")
DEFAULT_STEPS = 3
_INTENT_KEYS = ("intent", "target_description", "text", "app_name")
MAX_STATE_CHARS = 400


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def step_signature(tool_call: Any) -> str | None:
    """``tool|intent`` signature of one tool call (``None`` when unusable)."""

    if isinstance(tool_call, Mapping):
        name = tool_call.get("name")
        args = tool_call.get("args") or {}
    else:
        name = getattr(tool_call, "name", None)
        args = getattr(tool_call, "args", None) or {}
    clean_name = _normalize(name)
    if not clean_name:
        return None
    detail = ""
    if isinstance(args, Mapping):
        for key in _INTENT_KEYS:
            value = _normalize(args.get(key))
            if value:
                detail = value
                break
    return f"{clean_name}|{detail}"


def recent_signatures(messages: Sequence[Any], limit: int) -> list[str]:
    """The last ``limit`` step signatures, oldest first."""

    signatures: list[str] = []
    for message in messages or ():
        calls = getattr(message, "tool_calls", None)
        if not calls:
            continue
        for call in calls:
            signature = step_signature(call)
            if signature:
                signatures.append(signature)
    return signatures[-max(1, int(limit)) :]


class StuckGovernor:
    """``model/pre_request`` observer that advises on repeated step signatures."""

    def __init__(
        self,
        session: Any,
        *,
        record: Callable[..., None],
        tokens_of: Callable[[Any], int | None],
        client: Any = None,
        steps: int = DEFAULT_STEPS,
        mode: str = "shadow",
    ) -> None:
        self.session = session
        self.client = client
        self.steps = max(2, int(steps))
        self.mode = str(mode).strip().lower()
        self._record = record
        # ``usage_tokens`` from the protocol client (injected: the plugin's
        # siblings are flat modules with no package context to import from).
        self._tokens_of = tokens_of
        self._advised: set[str] = set()
        self.advisory_count = 0

    def _trace(self, **payload: Any) -> None:
        try:
            self._record(GOVERNOR_EVENT, **payload)
        except Exception:  # noqa: BLE001 - advisory tracing never fails a run
            pass

    def _ask_model(
        self, signature: str, signatures: Sequence[str]
    ) -> tuple[float | None, bool | None, str]:
        """One ``noul`` question about the repeated flow; ``(p, value, error)``."""

        state = (
            "recent steps (tool|intent):\n"
            + "\n".join(signatures)
            + f"\nrepeated signature: {signature}"
        )[:MAX_STATE_CHARS]
        reply = self.client.ask(
            state,
            {
                "stuck": {
                    "type": "noul",
                    "instructions": (
                        "Do these repeated steps show the agent is stuck in a loop "
                        "and needs a different approach?"
                    ),
                }
            },
        )
        answer = reply.answer("stuck")
        usage = getattr(reply, "usage", None)
        tokens = self._tokens_of(usage) if isinstance(usage, Mapping) else None
        ledger = getattr(self.session, "usage_ledger", None)
        if ledger is not None and tokens is not None and tokens > 0:
            try:
                ledger.record("systemone", estimate_tokens=int(tokens))
            except Exception:  # noqa: BLE001 - accounting must not change behavior
                pass
        return (
            float(answer.p) if isinstance(answer.p, (int, float)) else None,
            answer.value if isinstance(answer.value, bool) else None,
            "",
        )

    def inspect(self, messages: Sequence[Any]) -> None:
        """Emit the advisory for a repeated signature (idempotent per signature)."""

        signatures = recent_signatures(messages, self.steps)
        if len(signatures) < self.steps:
            return
        if len(set(signatures)) != 1:
            return
        signature = signatures[-1]
        if signature in self._advised:
            return
        self._advised.add(signature)
        self.advisory_count += 1
        probability: float | None = None
        model_value: bool | None = None
        error = ""
        if self.client is not None:
            try:
                probability, model_value, error = self._ask_model(signature, signatures)
            except Exception as exc:  # noqa: BLE001 - deterministic signal still stands
                error = type(exc).__name__
        self._trace(
            mode=self.mode,
            advisory=True,
            signature=signature,
            steps=list(signatures),
            repeat_count=len(signatures),
            model_value=model_value,
            p=probability,
            error=error or None,
        )

    def __call__(self, messages: Any, next: Callable[[Any], Any]) -> Any:
        try:
            self.inspect(messages)
        except Exception:  # noqa: BLE001 - a shadow governor is transparent
            pass
        return next(messages)


__all__ = [
    "DEFAULT_STEPS",
    "GOVERNOR_EVENT",
    "GOVERNOR_MODES",
    "StuckGovernor",
    "recent_signatures",
    "step_signature",
]
