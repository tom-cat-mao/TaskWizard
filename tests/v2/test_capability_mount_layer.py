"""WP-C2 capability mount equivalence, release, and reconcile contracts."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    PromptBlock,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.events import EventBus


class CoreMiddleware:
    pass


def _named_class(name: str):
    return type(name, (), {})


def _config(**overrides):
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


def _services(config=None):
    classes = {
        "taskdoc": _named_class("TaskDocMiddleware"),
        "budget": _named_class("BudgetMiddleware"),
        "compact": _named_class("CompactMiddleware"),
    }

    def tool(name: str, origin: str):
        return SimpleNamespace(name=name, origin=origin)

    def hook(name: str):
        def run(_state):
            return None

        run.__name__ = name
        return run

    return {
        "taskdoc_middleware_factory": lambda: classes["taskdoc"](),
        "budget_middleware_factory": lambda: classes["budget"](),
        "compact_middleware_factory": lambda: classes["compact"](),
        "taskdoc_tool_factory": lambda: tool("update_task_doc", "taskdoc"),
        "finish_verify_tool_factory": lambda: tool("finish", "finish_verify"),
        "taskdoc_run_start": hook("taskdoc_start"),
        "app_kb_run_start": hook("app_kb_start"),
        "app_kb_prompt_provider": lambda: PromptBlock(
            "\n\n# apps\nSettings", "system_suffix"
        ),
        "dream_run_end": hook("dream_end"),
        "experience_run_start": hook("experience_start"),
        "experience_run_end": hook("experience_end"),
        "recall_run_start": hook("recall_start"),
        "recall_run_end": hook("recall_end"),
        "recall_prompt_provider": lambda: PromptBlock("lesson"),
        "cli_handlers": {
            name: (lambda _args, command=name: command)
            for name in (
                "dream",
                "rebuild_vec",
                "distill",
                "review_lessons",
                "approve_lesson",
                "revoke_lesson",
                "supersede_lesson",
            )
        },
    }


def _context(config=None):
    ctx = CapabilityAssemblyContext(_services(config))
    ctx.register_core_middleware(
        CoreMiddleware(), order=30, replace_key="control_hitl"
    )
    ctx.register_core_tool(SimpleNamespace(name="read_screen", origin="core"), order=0)
    ctx.register_core_tool(SimpleNamespace(name="finish", origin="base"), order=1)
    return ctx


def _snapshot(ctx: CapabilityAssemblyContext):
    suffixes = []
    messages = []
    for provider in ctx.prompt_providers:
        block = provider()
        if block.placement == "system_suffix":
            suffixes.append(block.content)
        else:
            messages.append(block.content)
    return {
        "middleware": [type(item).__name__ for item in ctx.middleware],
        "tools": {item.name for item in ctx.tools},
        "tool_origins": {item.name: item.origin for item in ctx.tools},
        "prompt": "BASE" + "".join(suffixes) + "|" + "|".join(messages),
        "start_hooks": [hook.__name__ for hook in ctx.run_hooks("start")],
        "end_hooks": [hook.__name__ for hook in ctx.run_hooks("end")],
        "cli": set(ctx.cli_commands),
    }


def _legacy_expected(config):
    # Post-WP-E all policy pieces (taskdoc, safety, compact, budget) are event
    # listeners; with no event_bus in this fake harness their applies no-op, so
    # only the core placeholder middleware remains.
    middleware = ["CoreMiddleware"]
    prompt = "BASE"
    if config.app_kb_enabled:
        prompt += "\n\n# apps\nSettings"
    prompt += "|"
    if config.memory_rag != "off" and config.experience_enabled:
        prompt += "lesson"
    return {
        "middleware": middleware,
        "tools": {"read_screen", "finish"}
        | ({"update_task_doc"} if config.taskdoc_enabled else set()),
        "prompt": prompt,
    }


@pytest.mark.parametrize(
    "config",
    [
        _config(),
        _config(taskdoc_enabled=False),
        _config(safety_mode="off"),
        _config(safety_mode="hard"),
        _config(safety_mode="reviewer"),
        _config(compact_enabled=False),
        _config(finish_verify="off"),
        _config(app_kb_enabled=False),
        _config(memory_rag="on"),
    ],
)
def test_nine_typical_configs_match_legacy_products(config):
    ctx = assemble_capabilities(build_capability_registry(config), _context(config))
    actual = _snapshot(ctx)
    expected = _legacy_expected(config)
    assert actual["middleware"] == expected["middleware"]
    assert actual["tools"] == expected["tools"]
    assert actual["prompt"] == expected["prompt"]


def _registry_with_mode(config, cap_id: str, mode: str) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for spec in build_capability_registry(config).specs():
        registry.register(replace(spec, mode=mode) if spec.cap_id == cap_id else spec)
    return registry


@pytest.mark.parametrize(
    "cap_id",
    [
        "taskdoc",
        "safety",
        "budget",
        "compact",
        "finish_verify",
        "app_kb",
        "dream",
        "experience",
        "recall",
    ],
)
def test_each_capability_release_has_zero_residue(cap_id):
    config = _config()
    ctx = assemble_capabilities(build_capability_registry(config), _context())
    assemble_capabilities(_registry_with_mode(config, cap_id, "off"), ctx)

    never_applied = _context()
    assemble_capabilities(_registry_with_mode(config, cap_id, "off"), never_applied)
    assert _snapshot(ctx) == _snapshot(never_applied)


def test_end_hooks_persist_experience_before_recall_then_run_dream():
    config = _config()
    snapshot = _snapshot(
        assemble_capabilities(build_capability_registry(config), _context(config))
    )

    assert snapshot["end_hooks"] == ["experience_end", "recall_end", "dream_end"]


def test_hard_safety_release_restores_control_only_middleware_slot():
    config = _config(safety_mode="hard")
    ctx = assemble_capabilities(build_capability_registry(config), _context(config))
    assert [type(item).__name__ for item in ctx.middleware].count("CoreMiddleware") == 1

    assemble_capabilities(_registry_with_mode(config, "safety", "off"), ctx)
    never_applied = _context(config)
    assemble_capabilities(
        _registry_with_mode(config, "safety", "off"), never_applied
    )
    assert _snapshot(ctx) == _snapshot(never_applied)


def test_reconcile_applies_releases_and_reapplies_changed_mode_once():
    calls = []

    def spec(mode):
        return CapabilitySpec(
            "example",
            "Example",
            mode,
            apply=lambda ctx: (
                calls.append(f"apply:{mode}"),
                ctx.register_tool(SimpleNamespace(name="example", origin=mode)),
            ),
            release=lambda ctx: calls.append(f"release:{mode}"),
        )

    def registry(mode):
        value = CapabilityRegistry()
        value.register(spec(mode))
        return value

    ctx = _context()
    assemble_capabilities(registry("on"), ctx)
    assemble_capabilities(registry("on"), ctx)
    assemble_capabilities(registry("shadow"), ctx)
    assemble_capabilities(registry("off"), ctx)

    assert calls == ["apply:on", "release:on", "apply:shadow", "release:shadow"]
    assert {tool.name for tool in ctx.tools} == {"read_screen", "finish"}


def test_pending_dependency_never_applies():
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("base", "Base", "off"))
    registry.register(
        CapabilitySpec(
            "child",
            "Child",
            "on",
            deps=("base",),
            apply=lambda ctx: ctx.register_tool(SimpleNamespace(name="forbidden")),
        )
    )
    ctx = assemble_capabilities(registry, _context())

    assert {row["cap_id"]: row["state"] for row in registry.status()} == {
        "base": "off",
        "child": "pending",
    }
    assert "forbidden" not in {tool.name for tool in ctx.tools}


def test_dependency_applies_before_consumer_regardless_of_registration_order():
    calls: list[str] = []
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "consumer",
            "Consumer",
            "on",
            deps=("provider",),
            apply=lambda ctx: calls.append(ctx.service("shared")),
        )
    )
    registry.register(
        CapabilitySpec(
            "provider",
            "Provider",
            "on",
            apply=lambda ctx: ctx.register_service("shared", "ready"),
            provides="shared",
        )
    )

    assemble_capabilities(registry, CapabilityAssemblyContext())

    assert calls == ["ready"]


def test_dependency_cycle_is_pending_and_never_applies():
    calls: list[str] = []
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "alpha", "Alpha", "on", deps=("beta",), apply=lambda ctx: calls.append("a")
        )
    )
    registry.register(
        CapabilitySpec(
            "beta", "Beta", "on", deps=("alpha",), apply=lambda ctx: calls.append("b")
        )
    )

    assemble_capabilities(registry, CapabilityAssemblyContext())

    assert [row["state"] for row in registry.status()] == ["pending", "pending"]
    assert calls == []


def test_owned_event_listener_is_disposed_after_failed_apply():
    bus = EventBus()
    seen: list[str] = []

    def apply(ctx):
        ctx.on("observe", lambda _payload: seen.append("leaked"))
        raise RuntimeError("apply failed")

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("broken", "Broken", "on", apply=apply))
    ctx = CapabilityAssemblyContext({"event_bus": bus})

    with pytest.raises(RuntimeError, match="apply failed"):
        assemble_capabilities(registry, ctx)
    bus.emit("observe", {})

    assert seen == []


def test_event_registration_requires_an_active_capability_owner():
    ctx = CapabilityAssemblyContext({"event_bus": EventBus()})

    with pytest.raises(RuntimeError, match="active cap_id"):
        ctx.on("observe", lambda _payload: None)


def test_owned_event_listener_and_disposer_run_once_on_release():
    bus = EventBus()
    seen: list[str] = []
    disposed: list[str] = []

    def apply(ctx):
        ctx.on("observe", lambda _payload: seen.append("called"))
        ctx.on_dispose(lambda: disposed.append("disposed"))

    enabled = CapabilityRegistry()
    enabled.register(CapabilitySpec("plug", "Plug", "on", apply=apply))
    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off", apply=apply))
    ctx = assemble_capabilities(
        enabled, CapabilityAssemblyContext({"event_bus": bus})
    )
    bus.emit("observe", {})
    assemble_capabilities(disabled, ctx)
    assemble_capabilities(disabled, ctx)
    bus.emit("observe", {})

    assert seen == ["called"]
    assert disposed == ["disposed"]


def test_failing_disposer_does_not_skip_remaining_owned_cleanup():
    bus = EventBus()
    seen: list[str] = []
    disposed: list[str] = []

    def apply(ctx):
        ctx.on("observe", lambda _payload: seen.append("called"))
        ctx.on_dispose(lambda: (_ for _ in ()).throw(RuntimeError("cleanup")))
        ctx.on_dispose(lambda: disposed.append("completed"))
        ctx.register_tool(SimpleNamespace(name="owned", origin="plugin"))

    enabled = CapabilityRegistry()
    enabled.register(CapabilitySpec("plug", "Plug", "on", apply=apply))
    disabled = CapabilityRegistry()
    disabled.register(CapabilitySpec("plug", "Plug", "off", apply=apply))
    ctx = assemble_capabilities(
        enabled, CapabilityAssemblyContext({"event_bus": bus})
    )

    with pytest.raises(RuntimeError, match="cleanup"):
        assemble_capabilities(disabled, ctx)
    bus.emit("observe", {})

    assert seen == []
    assert disposed == ["completed"]
    assert all(tool.name != "owned" for tool in ctx.tools)


def test_release_error_cleans_all_owners_before_raising_and_skips_apply():
    bus = EventBus()
    seen: list[str] = []
    applied: list[str] = []

    def first_apply(ctx):
        ctx.on("observe", lambda _payload: seen.append("first"))

    def second_apply(ctx):
        ctx.on("observe", lambda _payload: seen.append("second"))
        ctx.on_dispose(
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed"))
        )

    enabled = CapabilityRegistry()
    enabled.register(CapabilitySpec("first", "First", "on", apply=first_apply))
    enabled.register(
        CapabilitySpec("second", "Second", "on", apply=second_apply)
    )
    replacement = CapabilityRegistry()
    replacement.register(
        CapabilitySpec(
            "replacement",
            "Replacement",
            "on",
            apply=lambda _ctx: applied.append("replacement"),
        )
    )
    ctx = assemble_capabilities(
        enabled, CapabilityAssemblyContext({"event_bus": bus})
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        assemble_capabilities(replacement, ctx)

    bus.emit("observe", {})
    assert seen == []
    assert applied == []
    assert ctx._mounted == {}


def test_same_named_tool_replacement_remains_supported():
    ctx = CapabilityAssemblyContext()
    ctx.register_core_tool(SimpleNamespace(name="tap", origin="core"), order=0)
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            "plug",
            "Plug",
            "on",
            apply=lambda ctx: ctx.register_tool(
                SimpleNamespace(name="tap", origin="plugin")
            ),
        )
    )

    assemble_capabilities(registry, ctx)

    assert [(tool.name, tool.origin) for tool in ctx.tools] == [("tap", "plugin")]
