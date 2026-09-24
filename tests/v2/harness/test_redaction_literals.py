"""Declared redaction literals (P0 #6 / #18): the v2 egress boundary stays extensible.

A capability that handles its own kind of secret declares the literal through
``ctx.register_redaction`` and every v2 egress path that shares ``redact_text``
(trace, diagnostic evidence, Web run events, streaming preview) replaces it with
``<redacted>`` before the built-in regex runs.  These tests pin the effect on
the production trace file, the fail-visible validation (minimum length, single
line), the release-with-capability semantics (including a literal two
capabilities share) and the separation from the classification plane: a declared
literal changes redaction only, never what the safety classifier sees.
"""

from __future__ import annotations

import json

import pytest

from phone_agent.config.redact import SENSITIVE_PATTERN
from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    assemble_capabilities,
)
from phone_agent.v2.middleware._redact import redact_text
from phone_agent.v2.middleware.trace import TraceWriter
from phone_agent.v2.redaction import MIN_REDACTION_LITERAL, REDACTION_REGISTRY

LITERAL = "ZXQ-7741-ACME"


@pytest.fixture(autouse=True)
def _restore_redaction_registry():
    """The registry is process-wide; leave it exactly as the test found it."""

    before = {literal: REDACTION_REGISTRY.owners_of(literal) for literal in REDACTION_REGISTRY}
    yield
    for literal in tuple(REDACTION_REGISTRY):
        for owner in REDACTION_REGISTRY.owners_of(literal):
            REDACTION_REGISTRY.withdraw(owner)
    for literal, owners in before.items():
        for owner in owners:
            REDACTION_REGISTRY.declare(literal, owner=owner)


def _ctx() -> CapabilityAssemblyContext:
    return CapabilityAssemblyContext({})


def _mount(ctx: CapabilityAssemblyContext, *specs: CapabilitySpec) -> None:
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    assemble_capabilities(registry, ctx)


def _declare(literal: str = LITERAL):
    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_redaction(literal)

    return apply


def _cap(cap_id: str, literal: str = LITERAL) -> CapabilitySpec:
    return CapabilitySpec(cap_id, cap_id, "on", apply=_declare(literal))


def test_declared_literal_is_redacted_by_the_shared_egress_boundary():
    ctx = _ctx()
    _mount(ctx, _cap("protector"))

    assert redact_text(f"account {LITERAL} ok") == "account <redacted> ok"
    # The built-in regex tier still runs behind it (unchanged behaviour).
    assert redact_text("phone 13800138000") == "phone <redacted>"


def test_declared_literal_reaches_the_production_trace_file(tmp_path):
    ctx = _ctx()
    _mount(ctx, _cap("protector"))
    writer = TraceWriter("run-redact", trace_dir=str(tmp_path))

    writer.record_event("probe", text=f"account {LITERAL} ok", count=3)
    event = json.loads((tmp_path / "run-redact.jsonl").read_text(encoding="utf-8").strip())

    assert event["text"] == "account <redacted> ok"
    assert LITERAL not in (tmp_path / "run-redact.jsonl").read_text(encoding="utf-8")


def test_longest_declared_literal_is_replaced_first():
    ctx = _ctx()
    _mount(
        ctx,
        _cap("wide", LITERAL),
        _cap("narrow", "ZXQ-7741"),
    )

    assert redact_text(LITERAL) == "<redacted>"


def test_release_stops_redacting_the_declared_literal():
    ctx = _ctx()
    _mount(ctx, _cap("protector"))
    assert REDACTION_REGISTRY.is_declared(LITERAL)

    ctx.release_capability("protector")

    assert not REDACTION_REGISTRY.is_declared(LITERAL)
    assert redact_text(LITERAL) == LITERAL


def test_a_shared_literal_lives_until_the_last_owner_releases_it():
    ctx = _ctx()
    _mount(ctx, _cap("first"), _cap("second"))

    assert REDACTION_REGISTRY.owners_of(LITERAL) == {"first", "second"}

    ctx.release_capability("first")
    assert redact_text(LITERAL) == "<redacted>"

    ctx.release_capability("second")
    assert redact_text(LITERAL) == LITERAL


@pytest.mark.parametrize(
    "literal",
    ["", "abc", "ab\ncd", "   "],
)
def test_too_short_or_multiline_literals_are_refused(literal):
    ctx = _ctx()

    with pytest.raises(ValueError, match="redaction literal"):
        _mount(ctx, _cap("protector", literal))


def test_minimum_length_guard_is_documented_by_its_constant():
    assert MIN_REDACTION_LITERAL >= 4
    ctx = _ctx()

    with pytest.raises(ValueError):
        _mount(ctx, _cap("protector", "a" * (MIN_REDACTION_LITERAL - 1)))


def test_redact_text_still_tolerates_a_falsy_value():
    """The boundary never raises on an empty payload (pre-declaration behaviour)."""

    assert redact_text("") == ""
    assert redact_text(None) == ""  # type: ignore[arg-type]


def test_declared_literal_does_not_change_the_classification_plane():
    ctx = _ctx()
    _mount(ctx, _cap("protector"))

    # Redaction and classification are separate planes: the literal is scrubbed
    # on egress, and the safety classifier's pattern is untouched by that
    # declaration (a declaration never opens or closes a gate).
    assert redact_text(LITERAL) == "<redacted>"
    assert SENSITIVE_PATTERN.search(LITERAL) is None


def test_redaction_registry_is_harness_owned():
    ctx = _ctx()

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_service("redaction_registry", object())

    with pytest.raises(ValueError, match="harness-owned"):
        _mount(ctx, CapabilitySpec("claim", "Claim", "on", apply=apply))
