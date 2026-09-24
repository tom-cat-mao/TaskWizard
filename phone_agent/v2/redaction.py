"""Declared sensitive literals for the v2 egress redaction boundary (P0 #6/#18).

The built-in redaction regex (``phone_agent/config/redact.py``) covers the
patterns the harness knows about: phone numbers, emails, order/verification
codes, api keys, JWTs and long base64 runs.  A capability that handles its own
kind of secret (an account id of a particular service, a device-specific token
prefix) declares the literal at assembly time::

    ctx.register_redaction("ACME-ACCOUNT-")

Every v2 egress path that goes through :func:`phone_agent.v2.middleware._redact.redact_text`
(trace, diagnostic evidence stream, Web run events, streaming preview) then
replaces that literal with ``<redacted>`` before the regex runs.  This is
egress-only armour: the safety classifier and prompt-side sanitization keep
reading the built-in pattern, so a declaration can never change what is
classified or what the model sees (declarations classify, they never allow).

Validation is fail-visible: a literal must be a single line and at least
:data:`MIN_REDACTION_LITERAL` characters long.  A one- or two-character
"secret" would redact unrelated text everywhere and destroy the trace it is
supposed to make safe, so it is refused at assembly time instead.

Unlike a pin prefix or a tool name (identity: exactly one owner), a literal is
a policy *value*: two capabilities protecting the same secret is ordinary, so
the registry keeps a set of owners per literal and withdrawal only removes the
owner that is going away.  The registry is process-level, like the redaction
function itself, and no literal is ever written to disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

MIN_REDACTION_LITERAL = 4
"""Shortest literal that may be declared (a shorter one nukes unrelated text)."""

REDACTED_PLACEHOLDER = "<redacted>"
"""Replacement text, shared with the built-in regex redaction."""


class RedactionRegistry:
    """Declared sensitive literals, mapped literal -> set of declaring owners.

    :meth:`declare` is the single writer (called by
    :meth:`phone_agent.v2.capabilities.CapabilityAssemblyContext.register_redaction`);
    consumers read through :meth:`literals` (longest first, so a literal that
    contains another one is replaced before its prefix can split it).
    """

    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._owners: dict[str, set[str]] = {}
        for literal in literals:
            self.declare(literal, owner="__seeded__")

    @staticmethod
    def _validate(literal: object) -> str:
        clean = str(literal).strip()
        if len(clean) < MIN_REDACTION_LITERAL:
            raise ValueError(
                f"redaction literal must be at least {MIN_REDACTION_LITERAL} "
                f"characters long (a shorter one would redact unrelated text): "
                f"{literal!r}"
            )
        if "\n" in clean or "\r" in clean:
            raise ValueError(f"redaction literal must be one line: {literal!r}")
        return clean

    def declare(self, literal: object, *, owner: str) -> str:
        """Declare ``literal`` for ``owner``; returns the normalized literal."""

        clean = self._validate(literal)
        self._owners.setdefault(clean, set()).add(str(owner))
        return clean

    def withdraw(self, owner: str) -> tuple[str, ...]:
        """Drop ``owner``'s claims; returns the literals no owner holds now."""

        dropped: list[str] = []
        for literal in list(self._owners):
            holders = self._owners[literal]
            holders.discard(str(owner))
            if not holders:
                self._owners.pop(literal, None)
                dropped.append(literal)
        return tuple(dropped)

    def literals(self) -> tuple[str, ...]:
        """Active literals, longest first (replacement order)."""

        return tuple(sorted(self._owners, key=len, reverse=True))

    def owners_of(self, literal: str) -> frozenset[str]:
        """Every owner currently holding ``literal``."""

        return frozenset(self._owners.get(str(literal).strip(), ()))

    def is_declared(self, literal: str) -> bool:
        return str(literal).strip() in self._owners

    def __iter__(self) -> Iterator[str]:
        return iter(self.literals())

    def __len__(self) -> int:
        return len(self._owners)


REDACTION_REGISTRY = RedactionRegistry()
"""The process-wide registry every v2 egress redaction path consults."""

__all__ = [
    "MIN_REDACTION_LITERAL",
    "REDACTED_PLACEHOLDER",
    "REDACTION_REGISTRY",
    "RedactionRegistry",
]
