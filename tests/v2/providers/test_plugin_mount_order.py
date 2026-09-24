"""Round-3 mount-order declaration (G3): ``before`` / ``after`` ordering hints.

A capability's listeners register while its ``apply`` runs, so the order two
capabilities apply in *is* their relative bus order.  Round 3 lets a capability
declare that order with ``CapabilitySpec.before`` / ``CapabilitySpec.after``:
one deterministic topological pass resolves deps and hints together, ties fall
back to registration order, and the two failure modes are fail-visible — a hint
naming an unregistered capability, and a cycle.

What the hints are *not*: a gate.  ``deps`` still decides whether a capability
may mount at all (``off`` dependency -> ``pending``); a hint at an ``off``
capability is simply vacuous, which is the whole reason the two declarations are
separate.

The last section applies this to the one real ordering dependency in the
built-in composition: ``boundary_compact`` mounts on top of ``compact``'s
instance and must therefore apply after it.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.events import EventBus
from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.session import FakePhoneSession


def _spec(cap_id: str, apply=None, **kwargs) -> CapabilitySpec:
    return CapabilitySpec(cap_id, cap_id, "on", apply=apply, **kwargs)


def _mount(ctx: CapabilityAssemblyContext, *specs: CapabilitySpec):
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    return assemble_capabilities(registry, ctx)


def _order(ctx: CapabilityAssemblyContext) -> list[str]:
    return sorted(ctx._capability_order, key=ctx._capability_order.__getitem__)  # noqa: SLF001


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def test_after_hint_orders_a_consumer_registered_before_its_target() -> None:
    calls: list[str] = []
    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec(
            "consumer",
            lambda context: calls.append(f"consumer:{context.service('provider')}"),
            after=("provider",),
        ),
        _spec(
            "provider",
            lambda context: (
                calls.append("provider"),
                context.register_service("provider", "ready"),
            ),
        ),
    )

    assert calls == ["provider", "consumer:ready"]
    assert _order(ctx) == ["provider", "consumer"]


def test_before_hint_orders_the_named_capability_later() -> None:
    calls: list[str] = []
    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec(
            "first",
            lambda _ctx: calls.append("first"),
            before=("second",),
        ),
        _spec("second", lambda _ctx: calls.append("second")),
    )

    assert calls == ["first", "second"]
    assert _order(ctx) == ["first", "second"]


def test_hint_chain_is_resolved_topologically() -> None:
    """A 3-node chain declared in reverse registration order."""

    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec("third", lambda _ctx: None, after=("second",)),
        _spec("second", lambda _ctx: None, after=("first",)),
        _spec("first", lambda _ctx: None),
    )

    assert _order(ctx) == ["first", "second", "third"]


def test_capabilities_without_hints_keep_registration_order() -> None:
    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec("alpha", lambda _ctx: None),
        _spec("beta", lambda _ctx: None),
        _spec("gamma", lambda _ctx: None),
    )

    assert _order(ctx) == ["alpha", "beta", "gamma"]


def test_hint_only_moves_the_capability_it_is_declared_on() -> None:
    """Unrelated capabilities keep their registration slots."""

    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec("beta", lambda _ctx: None, after=("alpha",)),
        _spec("alpha", lambda _ctx: None),
        _spec("gamma", lambda _ctx: None),
    )

    assert _order(ctx) == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# fail-visible validation
# ---------------------------------------------------------------------------
def test_hint_cycle_fails_visible() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="cycle among alpha, beta"):
        _mount(
            ctx,
            _spec("alpha", lambda _ctx: None, after=("beta",)),
            _spec("beta", lambda _ctx: None, after=("alpha",)),
        )


def test_mutually_contradicting_hint_directions_fail_visible() -> None:
    """``alpha before beta`` and ``beta before alpha`` cannot both hold."""

    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="cycle among alpha, beta"):
        _mount(
            ctx,
            _spec("alpha", lambda _ctx: None, before=("beta",)),
            _spec("beta", lambda _ctx: None, before=("alpha",)),
        )


def test_hint_contradicting_a_dependency_fails_visible() -> None:
    """``deps`` orders the dependency first; ``before`` says the opposite."""

    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="cycle"):
        _mount(
            ctx,
            _spec(
                "consumer",
                lambda _ctx: None,
                deps=("provider",),
                before=("provider",),
            ),
            _spec("provider", lambda _ctx: None),
        )


@pytest.mark.parametrize("keyword", ["after", "before"])
def test_unknown_hint_target_fails_visible(keyword: str) -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="no capability with that id is registered"):
        _mount(ctx, _spec("plug", lambda _ctx: None, **{keyword: ("ghost_cap",)}))


def test_hint_at_a_registered_but_unmounted_capability_is_vacuous() -> None:
    """``off`` target: nothing to order against, so the hint is skipped."""

    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec("plug", lambda _ctx: None, after=("disabled_cap",)),
        CapabilitySpec("disabled_cap", "Disabled", "off"),
    )

    assert _order(ctx) == ["plug"]


def test_hints_never_gate_the_mount() -> None:
    """A hint is ordering only: unlike ``deps`` it cannot make a spec pending."""

    registry = CapabilityRegistry()
    registry.register(_spec("plug", lambda _ctx: None, after=("disabled_cap",)))
    registry.register(CapabilitySpec("disabled_cap", "Disabled", "off"))

    assert {row["cap_id"]: row["state"] for row in registry.status()} == {
        "plug": "active",
        "disabled_cap": "off",
    }


def test_an_off_capability_does_not_validate_its_hints() -> None:
    """Turning a capability off must be able to un-break a bad hint."""

    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _spec("plug", lambda _ctx: None),
        CapabilitySpec("broken", "Broken", "off", after=("ghost_cap",)),
    )

    assert _order(ctx) == ["plug"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"after": ("Ghost Cap",)}, "invalid after ordering hint"),
        ({"before": ("bad-id",)}, "invalid before ordering hint"),
        ({"after": ("plug",)}, "cannot order itself"),
        ({"before": ("target",), "after": ("target",)}, "both before and after"),
    ],
)
def test_malformed_or_contradictory_hints_fail_at_construction(
    kwargs: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CapabilitySpec("plug", "Plug", "on", **kwargs)


# ---------------------------------------------------------------------------
# the real dependency: boundary_compact mounts on top of compact
# ---------------------------------------------------------------------------
def _boundary_context(config: FakeConfig) -> CapabilityAssemblyContext:
    bus = EventBus()
    session = FakePhoneSession()
    return CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": session,
            "config": config,
            "compact_middleware_factory": lambda **_kw: SimpleNamespace(
                on_pre_request=lambda messages, next: next(messages),
                generation=0,
                reset=lambda: None,
            ),
        }
    )


def test_boundary_compact_declares_its_compact_ordering() -> None:
    specs = {spec.cap_id: spec for spec in build_capability_registry(FakeConfig()).specs()}

    assert specs["boundary_compact"].after == ("compact",)
    # The gating dependency stays: with compact off there is nothing to fold
    # through, and the status row must keep saying so.
    assert specs["boundary_compact"].deps == ("compact",)


def test_boundary_compact_mounts_after_compact_in_the_builtin_composition() -> None:
    config = FakeConfig()
    ctx = _boundary_context(config)
    assemble_capabilities(build_capability_registry(config), ctx)

    assert ctx.service("compact_instance") is not None
    assert ctx.service("boundary_compact_listener") is not None


def test_the_ordering_hint_alone_places_compact_before_boundary_compact() -> None:
    """The declared hint is a real edge, not decoration.

    The built-in specs are re-registered in reverse order with their gating
    ``deps`` removed, so the only thing that can order them is
    ``boundary_compact``'s ``after`` hint — and the boundary listener still finds
    the compact instance it needs instead of degrading to inert.
    """

    config = FakeConfig(boundary_compact_mode="on")
    specs = {spec.cap_id: spec for spec in build_capability_registry(config).specs()}
    registry = CapabilityRegistry()
    registry.register(replace(specs["boundary_compact"], deps=()))
    registry.register(replace(specs["compact"], deps=()))
    ctx = _boundary_context(config)

    assemble_capabilities(registry, ctx)

    assert ctx.service("compact_instance") is not None
    assert ctx.service("boundary_compact_listener") is not None
