"""Round-1 declaration plane (P0 #18): tool risk, modes, ownership, pin prefixes.

These are the fail-visible declaration points the plugin plane added: a tool's
risk declaration (and its fail-closed default for undeclared tools), the
``off``/``shadow``/``on`` mount-mode validation, harness-owned service protection,
and declared pin prefixes.  Two properties matter most and are pinned here:

* the built-in declarations reproduce the pre-declaration classification
  **exactly** — same gate, same level, same reason for every built-in tool;
* every declaration point fails visibly (duplicate tool name, invalid risk,
  invalid mode, replacing a harness-owned service, undeclared pin prefix).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CAPABILITY_MODES,
    HARNESS_OWNER,
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    ToolRiskRegistry,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.middleware.safety import (
    LEGACY_ACTUATION_GATED_TOOLS,
    build_hitl_middleware,
    build_safety_hard_hitl_listener,
    build_safety_warning_listener,
    classify_tool_call,
)
from phone_agent.v2.pins import (
    COMPACT_ID_PREFIX,
    HARNESS_PIN_OWNER,
    HARNESS_PIN_PREFIXES,
    TASKDOC_ID_PREFIX,
    PinPrefixRegistry,
)
from phone_agent.v2.tool_risk import BUILTIN_TOOL_RISKS, declared_risk


def _request(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name, "args": args})


def _config(**overrides) -> SimpleNamespace:
    values = {
        "taskdoc_enabled": True,
        "safety_mode": "wary",
        "compact_enabled": True,
        "finish_verify": "auto",
        "app_kb_enabled": True,
        "dream_mode": "manual",
        "experience_enabled": True,
        "memory_rag": "shadow",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _declared_registry() -> ToolRiskRegistry:
    """The registry the harness builds for its own tools (declaration only)."""

    registry = ToolRiskRegistry()
    for name, risk in BUILTIN_TOOL_RISKS.items():
        registry.declare(name, risk, owner=HARNESS_OWNER)
    return registry


def _cap(cap_id: str, apply=None) -> CapabilitySpec:
    return CapabilitySpec(cap_id, cap_id, "on", apply=apply)


def _mount(
    ctx: CapabilityAssemblyContext, *specs: CapabilitySpec
) -> CapabilityAssemblyContext:
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    return assemble_capabilities(registry, ctx)


def _apply(ctx: CapabilityAssemblyContext, cap_id: str, apply) -> None:
    _mount(ctx, _cap(cap_id, apply))


# ---------------------------------------------------------------------------
# tool risk: declarations, fail-closed default, 1:1 built-in behaviour
# ---------------------------------------------------------------------------
def test_builtin_actuation_set_is_the_predeclaration_gate_set() -> None:
    actuation = {
        name for name, risk in BUILTIN_TOOL_RISKS.items() if risk == "actuation"
    }
    assert actuation == set(LEGACY_ACTUATION_GATED_TOOLS)


def test_every_builtin_tool_self_declares() -> None:
    from phone_agent.v2.tools import build_base_tools, build_tools
    from phone_agent.v2.tools.control import make_finish_tool
    from phone_agent.v2.tools.deliverable import make_deliverable_tools
    from phone_agent.v2.tools.obs_archive import make_obs_archive_tools
    from phone_agent.v2.tools.taskdoc import make_update_task_doc_tool
    from tests.v2.doubles.config import FakeConfig
    from tests.v2.doubles.session import FakePhoneSession

    session, config = FakePhoneSession(), FakeConfig()
    archive = SimpleNamespace(close=lambda: None)
    tools = [
        *build_base_tools(session, config),
        *build_tools(session, config),
        make_finish_tool(session, config),
        make_update_task_doc_tool(session, "cn"),
        *make_deliverable_tools("run-declare", "/tmp/unused"),
        *make_obs_archive_tools(session, archive),
    ]
    built_in = {tool.name for tool in tools}
    assert built_in <= set(BUILTIN_TOOL_RISKS)
    assert all(declared_risk(tool) is not None for tool in tools)


def test_builtin_declarations_reproduce_legacy_classification_exactly() -> None:
    """No built-in tool changes gate, level or reason once declarations exist."""

    risks = _declared_registry()
    config = SimpleNamespace(safety_mode="hard")
    for name in sorted(BUILTIN_TOOL_RISKS):
        args = {
            "target_description": "确认支付",
            "text": "验证码 887766",
            "app_name": "招商银行",
        }
        legacy = classify_tool_call(_request(name, args), None, config)
        declared = classify_tool_call(_request(name, args), None, config, risks=risks)
        assert (legacy.should_gate, legacy.level, legacy.reason) == (
            declared.should_gate,
            declared.level,
            declared.reason,
        ), name


def test_declared_risk_lands_in_the_registry_service() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(
            SimpleNamespace(name="plugin_read"), risk="readonly"
        )
        context.register_tool(SimpleNamespace(name="plugin_act"), risk="actuation")

    _apply(ctx, "plug", apply)
    registry = ctx.service("tool_risk_registry")

    assert isinstance(registry, ToolRiskRegistry)
    assert registry.risk_for("plugin_read") == "readonly"
    assert registry.risk_for("plugin_act") == "actuation"


def test_tool_metadata_stamp_declares_without_an_explicit_argument() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(
            SimpleNamespace(name="plugin_read", metadata={"risk": "readonly"})
        )

    _apply(ctx, "plug", apply)

    assert ctx.service("tool_risk_registry").risk_for("plugin_read") == "readonly"


def test_undeclared_tool_fails_closed_to_actuation() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(SimpleNamespace(name="plugin_unmarked"))

    _apply(ctx, "plug", apply)
    risks = ctx.service("tool_risk_registry")
    verdict = classify_tool_call(
        _request("plugin_unmarked", {"target_description": "确认支付"}),
        None,
        SimpleNamespace(safety_mode="wary"),
        risks=risks,
    )

    assert verdict.should_gate is True
    assert verdict.level == "hard"


def test_declared_readonly_tool_never_enters_the_classifier() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(SimpleNamespace(name="plugin_read"), risk="readonly")

    _apply(ctx, "plug", apply)
    verdict = classify_tool_call(
        _request("plugin_read", {"target_description": "确认支付"}),
        None,
        SimpleNamespace(safety_mode="wary"),
        risks=ctx.service("tool_risk_registry"),
    )

    assert verdict.should_gate is False
    assert verdict.reason == "not_actuation"


def test_warning_listener_classifies_through_the_live_registry() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(SimpleNamespace(name="plugin_read"), risk="readonly")
        context.register_tool(SimpleNamespace(name="plugin_act"))

    _apply(ctx, "plug", apply)
    listener = build_safety_warning_listener(
        None, SimpleNamespace(safety_mode="wary"), risks=ctx.service("tool_risk_registry")
    )

    def next_(request):  # noqa: ANN001 - terminal double
        return "executed"

    blocked = listener(
        _request("plugin_act", {"target_description": "确认支付"}), next_
    )
    passed = listener(_request("plugin_read", {"target_description": "确认支付"}), next_)

    assert blocked.status == "error"
    assert "已拦截" in blocked.content
    assert passed == "executed"


def test_hard_mode_interrupt_table_follows_declared_actuation() -> None:
    risks = _declared_registry()
    listener = build_hitl_middleware(
        session=None, config=SimpleNamespace(safety_mode="hard"), risks=risks
    )

    assert set(listener.interrupt_on) == {
        *LEGACY_ACTUATION_GATED_TOOLS,
        "ask_user",
        "take_over",
    }


def test_invalid_risk_value_fails_visible() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(SimpleNamespace(name="plugin_bad"), risk="sensitive")

    with pytest.raises(ValueError, match="invalid tool risk"):
        _apply(ctx, "plug", apply)


def test_tool_without_a_name_fails_visible() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="non-empty name"):
        _apply(ctx, "plug", lambda context: context.register_tool(SimpleNamespace()))


# ---------------------------------------------------------------------------
# duplicate tool names
# ---------------------------------------------------------------------------
def test_same_capability_cannot_register_a_name_twice() -> None:
    ctx = CapabilityAssemblyContext()

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_tool(SimpleNamespace(name="twice"))
        context.register_tool(SimpleNamespace(name="twice"))

    with pytest.raises(ValueError, match="already registered by this capability"):
        _apply(ctx, "plug", apply)


def test_second_capability_cannot_reuse_a_mounted_tool_name() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="already registered by capability"):
        _mount(
            ctx,
            _cap("first", lambda context: context.register_tool(
                SimpleNamespace(name="shared_tool")
            )),
            _cap("second", lambda context: context.register_tool(
                SimpleNamespace(name="shared_tool")
            )),
        )


def test_same_named_core_tool_replacement_still_works_and_restores() -> None:
    ctx = CapabilityAssemblyContext(
        {"event_bus": None}
    )
    ctx.register_core_tool(
        SimpleNamespace(name="tap", origin="core"), order=0, risk="readonly"
    )
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "plug",
            "Plug",
            "on",
            apply=lambda context: context.register_tool(
                SimpleNamespace(name="tap", origin="plugin"), risk="actuation"
            ),
            release=lambda context: None,
        )
    )

    assemble_capabilities(registry, ctx)
    assert ctx.service("tool_risk_registry").risk_for("tap") == "actuation"
    assert [(tool.name, tool.origin) for tool in ctx.tools] == [("tap", "plugin")]

    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off"))
    assemble_capabilities(disabled, ctx)

    # The replacing declaration is gone; the core declaration is visible again.
    assert ctx.service("tool_risk_registry").risk_for("tap") == "readonly"


def test_undeclared_replacement_does_not_inherit_the_core_declaration() -> None:
    """Silence is not an inheritance channel: a replacing tool declares or fails closed."""

    ctx = CapabilityAssemblyContext()
    ctx.register_core_tool(
        SimpleNamespace(name="finish", origin="core"), order=0, risk="readonly"
    )
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "plug",
            "Plug",
            "on",
            apply=lambda context: context.register_tool(
                SimpleNamespace(name="finish", origin="plugin")
            ),
        )
    )

    assemble_capabilities(registry, ctx)

    assert ctx.service("tool_risk_registry").risk_for("finish") is None
    assert classify_tool_call(
        _request("finish", {"target_description": "确认支付"}),
        None,
        SimpleNamespace(safety_mode="wary"),
        risks=ctx.service("tool_risk_registry"),
    ).should_gate is True


# ---------------------------------------------------------------------------
# undeclared tools are announced at assembly end (H2.4)
# ---------------------------------------------------------------------------
def _announcement_context(capsys) -> tuple[CapabilityAssemblyContext, list]:
    from phone_agent.v2.events import CAPABILITY_TOOLS_UNDECLARED, EventBus

    bus = EventBus()
    seen: list[tuple[str, dict]] = []
    bus.on(CAPABILITY_TOOLS_UNDECLARED, lambda payload: seen.append(("bus", payload)))
    session = SimpleNamespace(
        resolution_trace_recorder=lambda event, **payload: seen.append(
            (event, payload)
        )
    )
    capsys.readouterr()  # drop anything printed before this point
    return CapabilityAssemblyContext({"event_bus": bus, "session": session}), seen


def test_undeclared_tools_are_announced_at_assembly_end(capsys) -> None:
    from phone_agent.v2.events import CAPABILITY_TOOLS_UNDECLARED

    ctx, seen = _announcement_context(capsys)

    _mount(
        ctx,
        _cap("plug", lambda context: (
            context.register_tool(SimpleNamespace(name="mystery")),
            context.register_tool(SimpleNamespace(name="known"), risk="readonly"),
        )),
    )

    err = capsys.readouterr().err
    assert "mystery [plug]" in err
    assert "known" not in err
    # One trace record and one bus event, both naming only the undeclared tool.
    assert [kind for kind, _payload in seen] == [
        CAPABILITY_TOOLS_UNDECLARED,
        "bus",
    ]
    for _kind, payload in seen:
        assert payload["tools"] == ["mystery"]
        assert payload["owners"] == {"mystery": "plug"}


def test_declared_tools_are_never_announced(capsys) -> None:
    ctx, seen = _announcement_context(capsys)

    _mount(
        ctx,
        _cap(
            "plug",
            lambda context: context.register_tool(
                SimpleNamespace(name="known"), risk="actuation"
            ),
        ),
    )

    assert capsys.readouterr().err == ""
    assert seen == []


def test_announcement_is_recomputed_per_assembly(capsys) -> None:
    """Releasing the capability clears the warning; nothing is sticky."""

    ctx, seen = _announcement_context(capsys)
    _mount(ctx, _cap("plug", lambda context: context.register_tool(
        SimpleNamespace(name="mystery")
    )))
    assert "mystery" in capsys.readouterr().err

    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off"))
    assemble_capabilities(disabled, ctx)

    assert capsys.readouterr().err == ""
    assert len(seen) == 2  # one trace + one bus event, from the first pass only


def test_announcement_survives_a_missing_bus_and_session(capsys) -> None:
    ctx = CapabilityAssemblyContext()
    capsys.readouterr()

    _mount(ctx, _cap("plug", lambda context: context.register_tool(
        SimpleNamespace(name="mystery")
    )))

    assert "mystery" in capsys.readouterr().err


def test_builtin_only_assembly_announces_nothing(capsys) -> None:
    """A real harness assembly is silent: every built-in tool self-declares."""

    from phone_agent.v2.tools import build_base_tools
    from tests.v2.doubles.config import FakeConfig
    from tests.v2.doubles.session import FakePhoneSession

    ctx, seen = _announcement_context(capsys)
    for index, tool in enumerate(build_base_tools(FakePhoneSession(), FakeConfig())):
        ctx.register_core_tool(tool, order=index)
    assemble_capabilities(CapabilityRegistry(), ctx)

    assert capsys.readouterr().err == ""
    assert seen == []


def test_hard_mode_interrupt_table_does_not_cover_undeclared_tools(capsys) -> None:
    """Locks today's behaviour, including the known gap.

    The ``hard`` interrupt table is built from *declared* actuation tools, so an
    undeclared plugin tool is not interrupted there — the gap is scheduled for
    Round 3 (closing it needs the assembled tool list, not just declarations).
    The default ``wary``/``reviewer`` listener still fail-closes the same call
    through :func:`classify_tool_call`, which this test pins alongside it.
    """

    ctx = CapabilityAssemblyContext()
    capsys.readouterr()
    _mount(
        ctx,
        _cap(
            "plug",
            lambda context: (
                context.register_tool(
                    SimpleNamespace(name="declared_act"), risk="actuation"
                ),
                context.register_tool(SimpleNamespace(name="undeclared_act")),
            ),
        ),
    )
    risks = ctx.service("tool_risk_registry")
    listener = build_safety_hard_hitl_listener(
        session=None, config=SimpleNamespace(safety_mode="hard"), risks=risks
    )

    # Only the declared actuation tool (ask_user/take_over ride the core control
    # listener, not this one).
    assert set(listener.interrupt_on) == {"declared_act"}

    # The warning flow classifies the undeclared tool fail-closed all the same.
    warn = build_safety_warning_listener(
        None, SimpleNamespace(safety_mode="wary"), risks=risks
    )
    blocked = warn(
        _request("undeclared_act", {"target_description": "确认支付"}),
        lambda request: "executed",
    )
    assert blocked.status == "error"


def test_core_tool_names_must_be_unique() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="core tool 'read_screen' is already"):
        ctx.register_core_tool(SimpleNamespace(name="read_screen"), order=0)
        ctx.register_core_tool(SimpleNamespace(name="read_screen"), order=1)


# ---------------------------------------------------------------------------
# mount-mode validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", sorted(CAPABILITY_MODES))
def test_legal_mount_modes_are_accepted(mode: str) -> None:
    assert CapabilitySpec("cap", "Cap", mode).mode == mode


@pytest.mark.parametrize("mode", ["wary", "hard", "auto", "manual", "custom", ""])
def test_illegal_mount_modes_fail_visible(mode: str) -> None:
    with pytest.raises(ValueError, match="invalid capability mode"):
        CapabilitySpec("cap", "Cap", mode)


def test_mode_validation_is_case_insensitive() -> None:
    assert CapabilitySpec("cap", "Cap", "ON").mode == "ON"


def test_builtin_registry_translates_domain_modes_without_state_change() -> None:
    rows = {
        row["cap_id"]: row
        for row in build_capability_registry(_config()).status()
    }

    # Domain modes mount at full strength, exactly as they did before the modes
    # were validated; off stays off, shadow stays shadow.
    assert rows["safety"]["mode"] == "on"
    assert rows["safety"]["state"] == "active"
    assert rows["finish_verify"]["mode"] == "on"
    assert rows["dream"]["mode"] == "on"
    assert rows["recall"]["mode"] == "shadow"
    assert rows["obs_archive"]["mode"] == "off"

    off_rows = {
        row["cap_id"]: row
        for row in build_capability_registry(
            _config(safety_mode="off", dream_mode="off", finish_verify="off")
        ).status()
    }
    assert off_rows["safety"]["state"] == "off"
    assert off_rows["dream"]["state"] == "off"
    assert off_rows["finish_verify"]["state"] == "off"


# ---------------------------------------------------------------------------
# service ownership (G6)
# ---------------------------------------------------------------------------
def test_capability_cannot_replace_a_harness_owned_service() -> None:
    ctx = CapabilityAssemblyContext({"event_bus": object()})

    def apply(context: CapabilityAssemblyContext) -> None:
        context.register_service("event_bus", object())

    with pytest.raises(ValueError, match="harness-owned"):
        _apply(ctx, "plug", apply)


def test_capability_set_service_cannot_replace_a_harness_owned_service() -> None:
    ctx = CapabilityAssemblyContext({"config": object()})

    def apply(context: CapabilityAssemblyContext) -> None:
        context.set_service("config", object())

    with pytest.raises(ValueError, match="harness-owned"):
        _apply(ctx, "plug", apply)


def test_capability_owns_its_own_services() -> None:
    ctx = CapabilityAssemblyContext({"config": object()})
    first, second = object(), object()

    def apply(context: CapabilityAssemblyContext) -> None:
        # A capability may register and freely overwrite its own services.
        context.register_service("plugin_state", first)
        context.set_service("plugin_state", second)

    _apply(ctx, "plug", apply)

    assert ctx.service("plugin_state") is second


def test_capability_service_conflict_between_two_capabilities_fails() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="already registered by capability"):
        _mount(
            ctx,
            _cap("first", lambda context: context.register_service("shared", object())),
            _cap("second", lambda context: context.register_service("shared", object())),
        )


def test_release_keeps_harness_services_and_drops_capability_ones() -> None:
    bus = object()
    ctx = CapabilityAssemblyContext({"event_bus": bus})
    _apply(ctx, "plug", lambda context: context.register_service("plugin_state", 1))

    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off"))
    assemble_capabilities(disabled, ctx)

    assert ctx.service("event_bus") is bus
    assert ctx.service("plugin_state") is None


def test_release_frees_a_capability_service_name_for_the_next_owner() -> None:
    """Ownership is a lease: releasing the owner frees the key for a new claim."""

    ctx = CapabilityAssemblyContext()
    _mount(
        ctx,
        _cap("first", lambda context: context.register_service("shared", "first")),
    )

    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("first", "First", "off"))
    assemble_capabilities(disabled, ctx)

    _mount(
        ctx,
        _cap("second", lambda context: context.register_service("shared", "second")),
    )

    assert ctx.service("shared") == "second"

    released = CapabilityRegistry()
    released.register(CapabilitySpec("second", "Second", "off"))
    assemble_capabilities(released, ctx)

    assert ctx.service("shared") is None


def test_harness_may_replace_its_own_services_outside_apply() -> None:
    ctx = CapabilityAssemblyContext()
    ctx.set_service("tool_risk_registry", ToolRiskRegistry())

    assert isinstance(ctx.service("tool_risk_registry"), ToolRiskRegistry)


# ---------------------------------------------------------------------------
# pin prefixes (G4)
# ---------------------------------------------------------------------------
def test_harness_pin_prefixes_are_declared_at_construction() -> None:
    registry = PinPrefixRegistry()

    assert set(registry) == set(HARNESS_PIN_PREFIXES)
    assert registry.owner_of(TASKDOC_ID_PREFIX) == HARNESS_PIN_OWNER
    assert registry.pin_id(TASKDOC_ID_PREFIX, "old") == f"{TASKDOC_ID_PREFIX}old"
    assert registry.pin_id(COMPACT_ID_PREFIX) == COMPACT_ID_PREFIX


def test_capability_declares_and_pins_its_own_prefix() -> None:
    ctx = CapabilityAssemblyContext()
    _apply(ctx, "plug", lambda context: context.register_pin_prefix("__plug_pin__"))
    registry = ctx.service("pin_prefix_registry")

    assert registry.owner_of("__plug_pin__") == "plug"
    assert registry.pin_id("__plug_pin__", "0") == "__plug_pin__0"


def test_undeclared_pin_prefix_fails_visible() -> None:
    registry = PinPrefixRegistry()

    with pytest.raises(ValueError, match="was never declared"):
        registry.pin_id("__never_declared__", "0")


@pytest.mark.parametrize("prefix", ["plug_pin", "__Plug__", "__pi n__", "", "__1bad__"])
def test_malformed_pin_prefix_fails_visible(prefix: str) -> None:
    with pytest.raises(ValueError, match="invalid pin prefix"):
        PinPrefixRegistry().declare(prefix, owner="plug")


def test_another_capability_cannot_take_a_declared_prefix() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="already declared by capability"):
        _mount(
            ctx,
            _cap("first", lambda context: context.register_pin_prefix("__shared_pin__")),
            _cap(
                "second", lambda context: context.register_pin_prefix("__shared_pin__")
            ),
        )


def test_capability_cannot_take_a_harness_prefix() -> None:
    ctx = CapabilityAssemblyContext()

    with pytest.raises(ValueError, match="already declared by the harness"):
        _apply(
            ctx,
            "plug",
            lambda context: context.register_pin_prefix(TASKDOC_ID_PREFIX),
        )


def test_release_withdraws_a_capability_pin_prefix() -> None:
    ctx = CapabilityAssemblyContext()
    _apply(ctx, "plug", lambda context: context.register_pin_prefix("__plug_pin__"))

    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off"))
    assemble_capabilities(disabled, ctx)

    registry = ctx.service("pin_prefix_registry")
    assert registry.owner_of("__plug_pin__") is None
    assert registry.is_declared(TASKDOC_ID_PREFIX)
