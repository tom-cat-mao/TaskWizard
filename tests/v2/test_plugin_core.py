"""WP-PLUGIN-A service container: provides, register_service, API version.

These tests cover the Phase A seam in isolation (register/release residue,
``provides`` conflict + missing declaration error paths, ``PLUGIN_API_VERSION``)
plus the three real services mounted through a mini ``ThinPhoneAgent`` run and
its default-behavior equivalence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    PLUGIN_API_VERSION,
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    assemble_capabilities,
)


def test_plugin_api_version_is_one() -> None:
    assert PLUGIN_API_VERSION == 1
    assert isinstance(PLUGIN_API_VERSION, int)


def test_provides_key_follows_capability_grammar() -> None:
    CapabilitySpec("ok", "OK", "on", provides="my_service")
    with pytest.raises(ValueError, match="invalid provides service key"):
        CapabilitySpec("ok", "OK", "on", provides="Bad-Name")


def test_register_service_is_readable_through_service_lookup() -> None:
    handle = object()

    def apply(ctx: CapabilityAssemblyContext) -> None:
        ctx.register_service("thing", handle)

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec("cap", "Cap", "on", apply=apply, provides="thing")
    )
    ctx = assemble_capabilities(registry, CapabilityAssemblyContext())

    assert ctx.service("thing") is handle


def test_register_service_rejects_invalid_name() -> None:
    def apply(ctx: CapabilityAssemblyContext) -> None:
        ctx.register_service("Bad Name", object())

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("cap", "Cap", "on", apply=apply))
    with pytest.raises(ValueError, match="invalid service name"):
        assemble_capabilities(registry, CapabilityAssemblyContext())


def test_service_release_leaves_zero_residue() -> None:
    def apply(ctx: CapabilityAssemblyContext) -> None:
        ctx.register_service("thing", object())

    registry_on = CapabilityRegistry()
    registry_on.register(
        CapabilitySpec("cap", "Cap", "on", apply=apply, provides="thing")
    )
    ctx = assemble_capabilities(registry_on, CapabilityAssemblyContext())
    assert ctx.service("thing") is not None

    registry_off = CapabilityRegistry()
    registry_off.register(
        CapabilitySpec("cap", "Cap", "off", apply=apply, provides="thing")
    )
    assemble_capabilities(registry_off, ctx)

    assert ctx.service("thing") is None
    assert ctx.service("thing", "default") == "default"


def test_two_mounted_capabilities_registering_same_service_conflict() -> None:
    def apply(ctx: CapabilityAssemblyContext) -> None:
        ctx.register_service("shared", object())

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec("first", "First", "on", apply=apply, provides="shared")
    )
    registry.register(CapabilitySpec("second", "Second", "on", apply=apply))
    with pytest.raises(ValueError, match="already registered by capability"):
        assemble_capabilities(registry, CapabilityAssemblyContext())


def test_same_capability_may_reregister_its_own_service() -> None:
    calls: list[str] = []

    def apply(ctx: CapabilityAssemblyContext) -> None:
        ctx.register_service("thing", object())
        ctx.register_service("thing", object())  # idempotent for the owner
        calls.append("applied")

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec("cap", "Cap", "on", apply=apply, provides="thing")
    )
    ctx = assemble_capabilities(registry, CapabilityAssemblyContext())

    assert calls == ["applied"]
    assert ctx.service("thing") is not None


def test_declared_provides_without_registration_fails_visibly() -> None:
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "cap",
            "Cap",
            "on",
            apply=lambda ctx: None,  # declares provides but never registers
            provides="thing",
        )
    )
    with pytest.raises(ValueError, match="declares provides"):
        assemble_capabilities(registry, CapabilityAssemblyContext())


def test_missing_provides_registration_releases_partial_mount() -> None:
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "cap",
            "Cap",
            "on",
            apply=lambda ctx: ctx.register_tool(SimpleNamespace(name="leak")),
            provides="thing",
        )
    )
    ctx = CapabilityAssemblyContext()
    with pytest.raises(ValueError, match="declares provides"):
        assemble_capabilities(registry, ctx)

    # The failed capability must not leave its tool behind.
    assert [tool.name for tool in ctx.tools] == []


def test_conflicting_second_mount_does_not_poison_first_service() -> None:
    handle = object()

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "first",
            "First",
            "on",
            apply=lambda ctx: ctx.register_service("shared", handle),
            provides="shared",
        )
    )
    registry.register(
        CapabilitySpec(
            "second",
            "Second",
            "on",
            apply=lambda ctx: ctx.register_service("shared", object()),
        )
    )
    ctx = CapabilityAssemblyContext()
    with pytest.raises(ValueError):
        assemble_capabilities(registry, ctx)

    # The first owner's service is untouched by the rejected second mount.
    assert ctx.service("shared") is handle


def _real_registry_and_ctx(monkeypatch, tmp_path, *, memory_rag="shadow"):
    from tests.v2.test_experience import _install_mini_agent_modules

    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    config.trace_enabled = True
    config.app_kb_enabled = True
    config.memory_rag = memory_rag
    config.memory_dir = str(tmp_path / "memory")
    return config


def test_mini_agent_mounts_experience_recall_app_kb_services(
    tmp_path, monkeypatch
) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    config = _real_registry_and_ctx(monkeypatch, tmp_path)
    agent = ThinPhoneAgent(config)

    ctx = agent._capability_ctx
    assert ctx.service("experience") is not None
    assert ctx.service("recall") is not None
    assert ctx.service("app_kb") is not None
    # The recall facade drives the prompt block consumption path.
    assert hasattr(ctx.service("recall"), "lesson_prompt_block")


def test_recall_off_removes_recall_service(tmp_path, monkeypatch) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    config = _real_registry_and_ctx(monkeypatch, tmp_path, memory_rag="off")
    agent = ThinPhoneAgent(config)

    ctx = agent._capability_ctx
    # recall is off -> no recall service, but experience/app_kb remain.
    assert ctx.service("recall") is None
    assert ctx.service("experience") is not None
    assert ctx.service("app_kb") is not None


def test_default_run_behavior_unchanged_with_services(tmp_path, monkeypatch) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    config = _real_registry_and_ctx(monkeypatch, tmp_path)
    agent = ThinPhoneAgent(config)
    result = agent.run("打开设置")

    assert result.success is True
    # The recall service facade returns nothing when no lessons are injected,
    # exactly like the pre-service local renderer.
    assert agent._recall_prompt_block() is None
