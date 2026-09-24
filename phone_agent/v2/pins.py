"""Pinned-block identification contract for the TaskWizard v2 thin loop.

This module exposes the exact message-id prefixes that the actor uses to mark
blocks which must never be summarised, folded, or removed by context-hygiene
middleware.  Plugins and external capability code should import these constants
rather than hard-coding the prefix strings so the contract stays byte-for-byte
consistent across the codebase.

The two harness prefixes are always declared.  An external capability claims a
prefix of its own through ``ctx.register_pin_prefix("__my_plugin__")`` (P0 #18
declaration plane); the claim is released with the capability.  Minting an id
under a prefix nobody declared raises (:meth:`PinPrefixRegistry.pin_id`), so a
typo can never produce a block that context hygiene silently treats as ordinary
fodder.

Consumer wiring is still harness-owned: ``middleware/compact.py`` protects the
two built-in prefixes, and nothing yet consults a capability-declared prefix
when it decides what to fold (see the Round-1 decision note).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping

TASKDOC_ID_PREFIX: str = "__taskdoc__"
"""Pinned id prefix for the TaskDoc board block injected before every turn."""

COMPACT_ID_PREFIX: str = "__compact__"
"""Pinned id prefix for the auto-compaction summary block (immune to folding)."""

HARNESS_PIN_PREFIXES: tuple[str, ...] = (TASKDOC_ID_PREFIX, COMPACT_ID_PREFIX)
"""Every prefix the harness itself pins; declared at registry construction."""

HARNESS_PIN_OWNER: str = "__harness__"
"""Owner label for the harness prefixes.  Outside the cap_id grammar
(``^[a-z][a-z0-9_]*$``), so it cannot collide with a capability id."""

# A declared prefix is a dunder-wrapped lowercase identifier: ``__taskdoc__``.
_PREFIX_RE = re.compile(r"^__[a-z][a-z0-9_]*__$")


class PinPrefixRegistry(Mapping[str, str]):
    """Declared pinned message-id prefixes, keyed by prefix -> owner.

    Mapping contract only: :meth:`declare` is the single writer (called by
    :meth:`phone_agent.v2.capabilities.CapabilityAssemblyContext.register_pin_prefix`
    and by the harness seeds), and consumers read + validate through
    :meth:`is_declared` / :meth:`pin_id`.  The harness prefixes are declared at
    construction; every other entry is capability-owned and removed with it.
    """

    def __init__(self, prefixes: Iterable[str] = HARNESS_PIN_PREFIXES) -> None:
        self._declared: dict[str, str] = {}
        for prefix in prefixes:
            self._validate(prefix)
            self._declared[prefix] = HARNESS_PIN_OWNER

    @staticmethod
    def _validate(prefix: str) -> str:
        clean = str(prefix).strip()
        if not _PREFIX_RE.fullmatch(clean):
            raise ValueError(
                f"invalid pin prefix: {prefix!r} (expected __lower_case_identifier__)"
            )
        return clean

    def declare(self, prefix: str, *, owner: str) -> None:
        """Declare ``prefix`` as ``owner``'s; another owner's claim raises."""

        clean = self._validate(prefix)
        existing = self._declared.get(clean)
        if existing is not None and existing != owner:
            where = (
                "the harness"
                if existing == HARNESS_PIN_OWNER
                else f"capability {existing!r}"
            )
            raise ValueError(f"pin prefix {clean!r} is already declared by {where}")
        self._declared[clean] = owner

    def withdraw(self, owner: str) -> None:
        """Drop every prefix declared by ``owner`` (harness prefixes stay)."""

        for prefix in [
            prefix for prefix, holder in self._declared.items() if holder == owner
        ]:
            self._declared.pop(prefix, None)

    def is_declared(self, prefix: str) -> bool:
        return str(prefix).strip() in self._declared

    def owner_of(self, prefix: str) -> str | None:
        return self._declared.get(str(prefix).strip())

    def pin_id(self, prefix: str, suffix: str = "") -> str:
        """Mint ``<prefix><suffix>``; an undeclared prefix raises (fail-visible)."""

        clean = str(prefix).strip()
        if clean not in self._declared:
            raise ValueError(
                f"pin prefix {prefix!r} was never declared; declare it through "
                "ctx.register_pin_prefix(...) before pinning a block"
            )
        return f"{clean}{suffix}"

    def __getitem__(self, prefix: str) -> str:
        clean = str(prefix).strip()
        if clean not in self._declared:
            raise KeyError(prefix)
        return self._declared[clean]

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._declared))

    def __len__(self) -> int:
        return len(self._declared)


__all__ = [
    "COMPACT_ID_PREFIX",
    "HARNESS_PIN_OWNER",
    "HARNESS_PIN_PREFIXES",
    "TASKDOC_ID_PREFIX",
    "PinPrefixRegistry",
]
