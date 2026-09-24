"""Declared configuration keys (P0 #8 / #18): precedence, validation, manifest pass-through.

Round 2 opens the configuration plane to capabilities: ``ctx.register_setting``
declares a key, which then resolves through the same chain as the built-in
``V2Config`` fields — harness CLI override > shell env / ``.env`` > the plugin's
manifest ``[plugin.config]`` value > the declared default — and is mirrored on
the read-only ``V2Config.plugin_settings`` mapping.  These tests pin the chain,
the fail-visible validation (``PHONE_AGENT_`` prefix, scalar default, duplicate
keys) and the manifest pass-through into the plugin's own context.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    CapabilitySpec,
    assemble_capabilities,
)
from phone_agent.v2.config import V2Config
from phone_agent.v2.plugins import PluginEntry, load_plugin_spec
from phone_agent.v2.settings import SettingRegistry


def _config() -> V2Config:
    """A real V2Config; constructed directly so no ambient env leaks in."""

    return V2Config(base_url="http://gateway.invalid/v1", model_name="test-model")


def _ctx(**services) -> CapabilityAssemblyContext:
    return CapabilityAssemblyContext(services)


def _mount(ctx: CapabilityAssemblyContext, *specs: CapabilitySpec) -> None:
    registry = CapabilityRegistry()
    for spec in specs:
        registry.register(spec)
    assemble_capabilities(registry, ctx)


def _cap(cap_id: str, apply) -> CapabilitySpec:
    return CapabilitySpec(cap_id, cap_id, "on", apply=apply)


# ---------------------------------------------------------------------------
# The precedence chain
# ---------------------------------------------------------------------------
def test_declared_setting_falls_back_to_its_default_and_mirrors_onto_config():
    config = _config()
    ctx = _ctx(config=config)
    seen: list[float] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_threshold",
                env_var="PHONE_AGENT_DEMO_THRESHOLD",
                default=0.75,
                description="demo threshold",
            )
        )

    _mount(ctx, _cap("demo", apply))

    assert seen == [0.75]
    assert dict(config.plugin_settings) == {"demo_threshold": 0.75}
    registry = ctx.service("setting_registry")
    assert isinstance(registry, SettingRegistry)
    assert registry["demo_threshold"].owner == "demo"
    assert registry["demo_threshold"].description == "demo threshold"


def test_declared_setting_prefers_environment_over_default(monkeypatch):
    config = _config()
    ctx = _ctx(config=config)
    monkeypatch.setenv("PHONE_AGENT_DEMO_THRESHOLD", "0.25")
    seen: list[float] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_threshold",
                env_var="PHONE_AGENT_DEMO_THRESHOLD",
                default=0.75,
                description="demo threshold",
            )
        )

    _mount(ctx, _cap("demo", apply))

    assert seen == [0.25]
    assert dict(config.plugin_settings) == {"demo_threshold": 0.25}


def test_declared_setting_prefers_cli_override_over_environment(monkeypatch):
    config = _config()
    ctx = _ctx(config=config, setting_overrides={"demo_threshold": 0.9})
    monkeypatch.setenv("PHONE_AGENT_DEMO_THRESHOLD", "0.25")
    seen: list[float] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_threshold",
                env_var="PHONE_AGENT_DEMO_THRESHOLD",
                default=0.75,
                description="demo threshold",
            )
        )

    _mount(ctx, _cap("demo", apply))

    assert seen == [0.9]


@pytest.mark.parametrize("raw,expected", [("1", True), ("off", False), ("yes", True)])
def test_declared_boolean_setting_reads_env_tokens(monkeypatch, raw, expected):
    config = _config()
    ctx = _ctx(config=config)
    monkeypatch.setenv("PHONE_AGENT_DEMO_FLAG", raw)
    seen: list[bool] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_flag",
                env_var="PHONE_AGENT_DEMO_FLAG",
                default=True,
                description="demo flag",
            )
        )

    _mount(ctx, _cap("demo", apply))

    assert seen == [expected]


def test_unreadable_setting_value_fails_visibly(monkeypatch):
    ctx = _ctx(config=_config())
    monkeypatch.setenv("PHONE_AGENT_DEMO_COUNT", "not-a-number")

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            "demo_count",
            env_var="PHONE_AGENT_DEMO_COUNT",
            default=3,
            description="demo count",
        )

    with pytest.raises(ValueError, match="PHONE_AGENT_DEMO_COUNT"):
        _mount(ctx, _cap("demo", apply))


# ---------------------------------------------------------------------------
# Fail-visible declaration validation
# ---------------------------------------------------------------------------
def test_declared_setting_env_var_must_carry_the_phone_agent_prefix():
    ctx = _ctx(config=_config())

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            "demo_threshold",
            env_var="DEMO_THRESHOLD",
            default=1.0,
            description="demo threshold",
        )

    with pytest.raises(ValueError, match="PHONE_AGENT_"):
        _mount(ctx, _cap("demo", apply))


@pytest.mark.parametrize(
    "key,env_var,default,description",
    [
        ("Demo", "PHONE_AGENT_DEMO_X", 1, "x"),
        ("demo", "PHONE_AGENT_demo_x", 1, "x"),
        ("demo_x", "PHONE_AGENT_DEMO_X", ["a"], "x"),
        ("demo_x", "PHONE_AGENT_DEMO_X", 1, "  "),
    ],
)
def test_declared_setting_rejects_bad_declarations(key, env_var, default, description):
    ctx = _ctx(config=_config())

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            key, env_var=env_var, default=default, description=description
        )

    with pytest.raises(ValueError):
        _mount(ctx, _cap("demo", apply))


def test_a_second_capability_cannot_declare_the_same_key():
    ctx = _ctx(config=_config())

    def declare(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            "demo_shared",
            env_var="PHONE_AGENT_DEMO_SHARED",
            default=1,
            description="shared",
        )

    _mount(ctx, _cap("first", declare))
    with pytest.raises(ValueError, match="already declared"):
        _mount(
            ctx,
            _cap("first", declare),
            _cap("second", declare),
        )


def test_release_withdraws_the_declared_setting_and_its_mirror():
    config = _config()
    ctx = _ctx(config=config)

    def declare(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            "demo_shared",
            env_var="PHONE_AGENT_DEMO_SHARED",
            default=7,
            description="shared",
        )

    _mount(ctx, _cap("first", declare))
    assert dict(config.plugin_settings) == {"demo_shared": 7}

    ctx.release_capability("first")

    assert dict(config.plugin_settings) == {}
    assert "demo_shared" not in ctx.service("setting_registry")

    # The key is free again: another capability may declare it.
    _mount(ctx, _cap("second", declare))
    assert dict(config.plugin_settings) == {"demo_shared": 7}


# ---------------------------------------------------------------------------
# Manifest [plugin.config] pass-through
# ---------------------------------------------------------------------------
def test_manifest_config_is_visible_inside_the_apply_hook():
    ctx = _ctx(config=_config())
    seen: list[dict] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(dict(cap_ctx.plugin_config()))

    spec = CapabilitySpec(
        "demo", "Demo", "on", apply=apply, manifest_config={"mode_hint": "fast"}
    )
    _mount(ctx, spec)

    assert seen == [{"mode_hint": "fast"}]


def test_plugin_config_is_empty_outside_an_apply_hook():
    assert dict(_ctx(config=_config()).plugin_config()) == {}


@pytest.mark.parametrize(
    "env_value,expected",
    [(None, 0.6), ("0.2", 0.2)],
)
def test_manifest_config_sits_below_the_environment(monkeypatch, env_value, expected):
    config = _config()
    ctx = _ctx(config=config)
    if env_value is not None:
        monkeypatch.setenv("PHONE_AGENT_DEMO_THRESHOLD", env_value)
    seen: list[float] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_threshold",
                env_var="PHONE_AGENT_DEMO_THRESHOLD",
                default=0.9,
                description="demo threshold",
            )
        )

    _mount(
        ctx,
        CapabilitySpec(
            "demo",
            "Demo",
            "on",
            apply=apply,
            manifest_config={"demo_threshold": 0.6},
        ),
    )

    assert seen == [expected]


def test_load_plugin_spec_attaches_the_manifest_config(tmp_path):
    directory = tmp_path / "plug"
    directory.mkdir()
    (directory / "plugin.py").write_text(
        "from phone_agent.v2.capabilities import CapabilitySpec\n"
        "CAPABILITY = CapabilitySpec('plug_cfg', 'Plug', 'on')\n",
        encoding="utf-8",
    )
    entry = PluginEntry(name="plug", path=str(directory), config={"speed": "fast"})

    spec = load_plugin_spec(entry)

    assert dict(spec.manifest_config) == {"speed": "fast"}


def test_load_plugin_spec_without_config_keeps_the_loaded_spec(tmp_path):
    directory = tmp_path / "plug"
    directory.mkdir()
    (directory / "plugin.py").write_text(
        "from phone_agent.v2.capabilities import CapabilitySpec\n"
        "CAPABILITY = CapabilitySpec('plug_plain', 'Plug', 'on')\n",
        encoding="utf-8",
    )
    entry = PluginEntry(name="plug", path=str(directory))

    spec = load_plugin_spec(entry)

    assert dict(spec.manifest_config) == {}


def test_manifest_config_must_be_a_mapping():
    with pytest.raises(TypeError, match="manifest_config"):
        CapabilitySpec("demo", "Demo", "on", manifest_config=["fast"])  # type: ignore[arg-type]


def test_declared_settings_are_readable_through_the_registry_service():
    ctx = _ctx(config=_config())

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_setting(
            "demo_a",
            env_var="PHONE_AGENT_DEMO_A",
            default="x",
            description="first",
        )
        cap_ctx.register_setting(
            "demo_b",
            env_var="PHONE_AGENT_DEMO_B",
            default=2,
            description="second",
        )

    _mount(ctx, _cap("demo", apply))

    registry = ctx.service("setting_registry")
    assert sorted(registry) == ["demo_a", "demo_b"]
    assert registry["demo_b"].value == 2
    assert registry.overrides == {}


def test_setting_registry_cannot_be_replaced_by_a_capability():
    ctx = _ctx(config=_config())

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        cap_ctx.register_service("setting_registry", SimpleNamespace())

    with pytest.raises(ValueError, match="harness-owned"):
        _mount(ctx, _cap("demo", apply))


def test_declaration_without_a_config_service_still_resolves():
    """The CLI maintenance assembly publishes no config; only the mirror is skipped."""

    ctx = _ctx()
    seen: list[int] = []

    def apply(cap_ctx: CapabilityAssemblyContext) -> None:
        seen.append(
            cap_ctx.register_setting(
                "demo_count",
                env_var="PHONE_AGENT_DEMO_COUNT",
                default=4,
                description="demo count",
            )
        )

    _mount(ctx, _cap("demo", apply))

    assert seen == [4]
    assert ctx.service("setting_registry")["demo_count"].value == 4
