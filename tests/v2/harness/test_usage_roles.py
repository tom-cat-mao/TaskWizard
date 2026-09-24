"""Declared accounting roles (P0 #13 / #18): tokens vs calls, budget boundary.

Round 2 unfreezes the usage-role list: the harness roles are pre-registered in
``phone_agent/v2/usage_roles.py`` and a capability adds its own through
``ctx.register_usage_role``.  The ledger still refuses an undeclared role, the
experience schema derives the token roles from the same registry (no second
copy), and the budget keeps adjudicating tokens only — a ``unit="calls"`` role
is counted and reported, never charged against the token budget.
"""

from __future__ import annotations

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    assemble_capabilities,
)
from phone_agent.v2.experience import _classify_and_clean
from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.usage import UsageLedger
from phone_agent.v2.usage_roles import (
    HARNESS_USAGE_OWNER,
    USAGE_ROLE_REGISTRY,
    registered_usage_roles,
)


@pytest.fixture(autouse=True)
def _restore_usage_roles():
    """The registry is process-wide; leave it exactly as the test found it."""

    before = {
        role: (unit, USAGE_ROLE_REGISTRY.owner_of(role))
        for role, unit in USAGE_ROLE_REGISTRY.items()
    }
    yield
    for role in tuple(USAGE_ROLE_REGISTRY):
        owner = USAGE_ROLE_REGISTRY.owner_of(role)
        if owner is not None:
            USAGE_ROLE_REGISTRY.withdraw(owner)
    for role, (unit, owner) in before.items():
        USAGE_ROLE_REGISTRY.declare(role, unit=unit, owner=owner)


def _ctx() -> CapabilityAssemblyContext:
    return CapabilityAssemblyContext({})


def _mount(ctx: CapabilityAssemblyContext, *specs: CapabilitySpec) -> None:
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    assemble_capabilities(registry, ctx)


def _declare(role: str, *, unit: str = "calls"):
    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_usage_role(role, unit=unit)

    return apply


def test_harness_roles_are_pre_registered_as_token_roles():
    assert registered_usage_roles(unit="tokens") >= {
        "actor",
        "compact",
        "verifier",
        "reviewer",
        "distill",
    }
    assert registered_usage_roles(unit="calls") == frozenset()
    assert USAGE_ROLE_REGISTRY.owner_of("actor") == HARNESS_USAGE_OWNER


def test_declared_calls_role_counts_invocations_and_stays_out_of_tokens():
    ctx = _ctx()
    _mount(ctx, CapabilitySpec("shadow", "Shadow", "on", apply=_declare("shadow_scorer")))

    ledger = UsageLedger()
    assert ledger.record("shadow_scorer") == 1
    assert ledger.record("shadow_scorer") == 1

    assert ledger.calls_total == 2
    assert ledger.calls_by_role() == {"shadow_scorer": 2}
    assert ledger.total == 0
    assert ledger.by_role() == {}

    ledger.record("actor", estimate_tokens=7)
    assert ledger.total == 7
    assert ledger.by_role() == {"actor": 7}
    assert ledger.calls_by_role() == {"shadow_scorer": 2}

    ledger.reset()
    assert ledger.calls_total == 0
    assert ledger.calls_by_role() == {}


def test_calls_role_refuses_a_token_payload():
    ctx = _ctx()
    _mount(ctx, CapabilitySpec("shadow", "Shadow", "on", apply=_declare("shadow_scorer")))
    ledger = UsageLedger()

    with pytest.raises(ValueError, match="unit='calls'"):
        ledger.record("shadow_scorer", estimate_tokens=5)


def test_declared_token_role_joins_the_token_accounting():
    ctx = _ctx()
    _mount(
        ctx,
        CapabilitySpec(
            "shadow", "Shadow", "on", apply=_declare("shadow_scorer", unit="tokens")
        ),
    )

    ledger = UsageLedger()
    assert ledger.record("shadow_scorer", estimate_tokens=11) == 11
    assert ledger.total == 11
    assert ledger.by_role() == {"shadow_scorer": 11}


def test_budget_adjudicates_tokens_only_while_calls_are_reported():
    ctx = _ctx()
    _mount(ctx, CapabilitySpec("shadow", "Shadow", "on", apply=_declare("shadow_scorer")))
    ledger = UsageLedger()
    budget = BudgetMiddleware(token_budget=1000, warn_remaining=200, ledger=ledger)

    for _ in range(5):
        ledger.record("shadow_scorer")

    assert budget.used_tokens == 0
    assert budget.before_model({"messages": []}, runtime=None) is None

    ledger.record("compact", estimate_tokens=850)
    warned = budget.before_model({"messages": []}, runtime=None)

    assert warned is not None
    assert "已用约 850/1000" in warned["messages"][0].content
    # The call-unit role is visible in the ledger, just not in the budget.
    assert ledger.calls_total == 5


def test_release_withdraws_the_declared_role():
    ctx = _ctx()
    _mount(ctx, CapabilitySpec("shadow", "Shadow", "on", apply=_declare("shadow_scorer")))
    assert USAGE_ROLE_REGISTRY.is_registered("shadow_scorer")

    ctx.release_capability("shadow")

    assert not USAGE_ROLE_REGISTRY.is_registered("shadow_scorer")
    with pytest.raises(ValueError, match="unknown usage role"):
        UsageLedger().record("shadow_scorer")


def test_undeclared_role_still_raises():
    with pytest.raises(ValueError, match="unknown usage role"):
        UsageLedger().record("never_declared", estimate_tokens=1)


def test_harness_role_cannot_be_redeclared_with_another_unit():
    with pytest.raises(ValueError, match="already declared by the harness"):
        USAGE_ROLE_REGISTRY.declare("actor", unit="calls", owner="shadow")


def test_two_capabilities_cannot_share_a_role():
    ctx = _ctx()

    with pytest.raises(ValueError, match="already declared"):
        _mount(
            ctx,
            CapabilitySpec("first", "First", "on", apply=_declare("shared_role")),
            CapabilitySpec("second", "Second", "on", apply=_declare("shared_role")),
        )


@pytest.mark.parametrize(
    "role,unit",
    [("Shadow", "calls"), ("shadow", "secrets"), ("", "tokens")],
)
def test_invalid_role_declarations_are_refused(role, unit):
    with pytest.raises(ValueError):
        USAGE_ROLE_REGISTRY.declare(role, unit=unit, owner="shadow")


def test_experience_schema_derives_token_roles_from_the_same_registry():
    ctx = _ctx()

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_usage_role("shadow_scorer", unit="tokens")
        cap_ctx.register_usage_role("shadow_calls", unit="calls")

    _mount(ctx, CapabilitySpec("shadow", "Shadow", "on", apply=apply))

    record = _classify_and_clean(
        {
            "type": "episode_outcome",
            "run_id": "run-1",
            "tokens_by_role": {"actor": 3, "shadow_scorer": 5, "shadow_calls": 9},
        }
    )

    # Token roles survive; a calls-unit role is not a token role, and an
    # unregistered name is dropped exactly as before.
    assert record["tokens_by_role"] == {"actor": 3, "shadow_scorer": 5}

    ctx.release_capability("shadow")

    replayed = _classify_and_clean(
        {
            "type": "episode_outcome",
            "run_id": "run-1",
            "tokens_by_role": {"actor": 3, "shadow_scorer": 5, "shadow_calls": 9},
        }
    )
    assert replayed["tokens_by_role"] == {"actor": 3}
