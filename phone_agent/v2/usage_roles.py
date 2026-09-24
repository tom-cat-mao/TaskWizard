"""Declared accounting roles and their unit (P0 #13 / #18 declaration plane).

Every :meth:`phone_agent.v2.usage.UsageLedger.record` call names a role; this
module owns the registry that decides which names exist and what their numbers
mean.  The harness pre-registers its own roles (all ``unit="tokens"``); a
capability adds its own at assembly time::

    ctx.register_usage_role("systemone", unit="calls")

``unit="tokens"`` roles feed the token budget exactly as before.  ``unit="calls"``
roles are accounted as invocations (one unit per ``record`` call) and reported
separately (:attr:`~phone_agent.v2.usage.UsageLedger.calls_by_role`); they never
enter the token total, so the budget adjudication point stays harness-owned and
token-only (P0 #13).

This module exists separately from :mod:`phone_agent.v2.usage` so that the
registry stays dependency-free: the ledger, the assembly context and the
experience schema (:mod:`phone_agent.v2.experience`) all read the *same*
singleton, and no consumer has to import the token-estimation stack to validate
a role.

The registry is process-level, like the provider transport table: a run and an
offline reader in the same process see the roles the plugins declared.  A role
declared by a capability is withdrawn with that capability (fail-visible: an
undeclared role still raises at ``record`` time).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
import re

USAGE_UNITS = frozenset({"tokens", "calls"})
"""Legal accounting units: token cost, or invocation count."""

HARNESS_USAGE_OWNER = "__harness__"
"""Owner label for the harness roles; outside the cap_id grammar."""

HARNESS_USAGE_ROLES: tuple[tuple[str, str], ...] = (
    ("actor", "tokens"),
    ("compact", "tokens"),
    ("verifier", "tokens"),
    ("reviewer", "tokens"),
    ("distill", "tokens"),
)
"""Roles the harness itself records, pre-registered at registry construction."""

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class UsageRoleRegistry(Mapping[str, str]):
    """Declared accounting roles, keyed by role -> unit.

    Mapping contract only: :meth:`declare` is the single writer (called by
    :meth:`phone_agent.v2.capabilities.CapabilityAssemblyContext.register_usage_role`)
    and consumers read through :meth:`unit_of` / :meth:`roles`.  A role name is
    unique across the process: re-declaring another owner's role raises, and a
    harness role can never be re-declared (its unit is part of P0 #13's
    contract).  ``withdraw`` only drops the given owner's roles.
    """

    def __init__(
        self,
        roles: Iterable[tuple[str, str]] = HARNESS_USAGE_ROLES,
        *,
        owner: str = HARNESS_USAGE_OWNER,
    ) -> None:
        self._harness_owner = owner
        self._declared: dict[str, tuple[str, str]] = {}
        for role, unit in roles:
            self._declared[str(role).strip()] = (self._validate_unit(unit), owner)

    @staticmethod
    def _validate_role(role: object) -> str:
        clean = str(role).strip()
        if not _ROLE_RE.fullmatch(clean):
            raise ValueError(
                f"invalid usage role: {role!r} (expected a lowercase identifier "
                "such as 'systemone')"
            )
        return clean

    @staticmethod
    def _validate_unit(unit: object) -> str:
        clean = str(unit).strip().lower()
        if clean not in USAGE_UNITS:
            raise ValueError(
                f"invalid usage unit: {unit!r} (expected one of "
                f"{', '.join(sorted(USAGE_UNITS))})"
            )
        return clean

    def declare(self, role: object, *, unit: object = "tokens", owner: str) -> str:
        """Declare ``role`` with ``unit`` for ``owner``; returns the unit."""

        clean = self._validate_role(role)
        clean_unit = self._validate_unit(unit)
        existing = self._declared.get(clean)
        if existing is not None and existing[1] != owner:
            where = (
                "the harness"
                if existing[1] == self._harness_owner
                else f"capability {existing[1]!r}"
            )
            raise ValueError(f"usage role {clean!r} is already declared by {where}")
        self._declared[clean] = (clean_unit, owner)
        return clean_unit

    def withdraw(self, owner: str) -> tuple[str, ...]:
        """Drop every role declared by ``owner`` (harness roles never go)."""

        if owner == self._harness_owner:
            return ()
        removed = tuple(
            role for role, (_, holder) in self._declared.items() if holder == owner
        )
        for role in removed:
            self._declared.pop(role, None)
        return removed

    def unit_of(self, role: str) -> str | None:
        """The declared unit for ``role`` (``None`` when unregistered)."""

        entry = self._declared.get(str(role))
        return entry[0] if entry else None

    def is_registered(self, role: str) -> bool:
        return str(role) in self._declared

    def owner_of(self, role: str) -> str | None:
        entry = self._declared.get(str(role))
        return entry[1] if entry else None

    def roles(self, *, unit: str | None = None) -> frozenset[str]:
        """Snapshot of role names, optionally filtered to one unit."""

        clean_unit = self._validate_unit(unit) if unit is not None else None
        return frozenset(
            role
            for role, (role_unit, _) in self._declared.items()
            if clean_unit is None or role_unit == clean_unit
        )

    def __getitem__(self, role: str) -> str:
        entry = self._declared.get(str(role))
        if entry is None:
            raise KeyError(role)
        return entry[0]

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._declared))

    def __len__(self) -> int:
        return len(self._declared)


USAGE_ROLE_REGISTRY = UsageRoleRegistry()
"""The process-wide registry the ledger, assembler and schema all read."""


def registered_usage_roles(*, unit: str | None = None) -> frozenset[str]:
    """Live snapshot of registered roles, optionally filtered by unit."""

    return USAGE_ROLE_REGISTRY.roles(unit=unit)


__all__ = [
    "HARNESS_USAGE_OWNER",
    "HARNESS_USAGE_ROLES",
    "USAGE_ROLE_REGISTRY",
    "USAGE_UNITS",
    "UsageRoleRegistry",
    "registered_usage_roles",
]
