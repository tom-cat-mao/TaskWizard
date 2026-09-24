"""Plugin-level contract: path loading, declarations, modes, config, residue."""

from __future__ import annotations

import json

import pytest

from tests.plugins.systemone_harness import (
    PLUGIN_DIR,
    REPO_ROOT,
    HarnessConfig,
    ScriptedPhoneSession,
    load_plugin_module,
)

MANIFEST = REPO_ROOT / ".taskwizard.toml"
INDEX = REPO_ROOT / "plugins" / "index.json"


# ---------------------------------------------------------------------------
# Path-loading contract
# ---------------------------------------------------------------------------
def test_the_manifest_entry_loads_the_plugin_through_the_real_loader():
    from phone_agent.v2.plugins import PluginEntry, load_plugin_spec, satisfies_api

    # An absolute path: the suite's autouse fixture isolates the project
    # manifest into tmp_path, so a relative path would resolve against it.
    spec = load_plugin_spec(
        PluginEntry(name="systemone", path=str(PLUGIN_DIR))
    )
    assert spec.cap_id == "systemone"
    assert spec.mode == "on"
    assert spec.deps == ("providers",)
    assert spec.after == ("providers",)
    assert spec.apply is not None and spec.release is not None
    module = load_plugin_module()
    assert satisfies_api(1, module.REQUIRES_API)


def test_the_committed_manifest_keeps_the_plugin_disabled_by_default():
    from phone_agent.v2.plugins import read_manifest

    entries = {entry.name: entry for entry in read_manifest(MANIFEST)}
    assert "systemone" in entries, "the first-party plugin must be enable-able"
    entry = entries["systemone"]
    assert entry.enabled is False
    assert entry.path == "plugins/systemone"


def test_the_plugin_index_lists_the_first_party_plugin():
    from phone_agent.v2 import plugins

    rows = plugins.cmd_search("systemone", config=HarnessConfig(plugin_index=str(INDEX)))
    assert rows and rows[0]["name"] == "systemone"
    assert rows[0]["path"] == "plugins/systemone"

    # And the index stays a valid json array of objects.
    payload = json.loads(INDEX.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and all(isinstance(item, dict) for item in payload)


def test_enabling_the_plugin_in_a_manifest_yields_its_spec(tmp_path):
    from phone_agent.v2.plugins import PluginEntry, discover_external_specs, write_manifest

    manifest = tmp_path / "project.toml"
    write_manifest(
        manifest,
        [PluginEntry(name="systemone", enabled=True, path=str(PLUGIN_DIR))],
    )
    config = HarnessConfig(plugin_manifest=str(manifest))
    specs = discover_external_specs(config)
    assert [spec.cap_id for spec in specs] == ["systemone"]


# ---------------------------------------------------------------------------
# Declarations the plane is supposed to dogfood
# ---------------------------------------------------------------------------
def test_declared_settings_roles_redaction_and_tool_risk(monkeypatch, mount):
    secret = "sk-jev-abcdef123456"
    monkeypatch.setenv("TYPESAFE_API_KEY", secret)
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_STABLE_POLLS", "3")
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(config=HarnessConfig(), session=session)

    declarations = mounted.service("setting_registry")
    assert set(declarations) >= {
        "systemone_backend",
        "systemone_review",
        "systemone_confidence",
        "systemone_timeout",
        "systemone_retries",
        "systemone_stable_wait",
        "systemone_stable_interval",
        "systemone_stable_polls",
        "systemone_governor",
        "systemone_governor_steps",
    }
    assert declarations["systemone_stable_polls"].value == 3
    assert mounted.config.plugin_settings["systemone_stable_polls"] == 3

    roles = mounted.service("usage_role_registry")
    # The API always reports usage, so the account is per token (P0 #13's unit).
    assert roles.unit_of("systemone") == "tokens"

    redactions = mounted.service("redaction_registry")
    assert redactions.is_declared(secret)
    from phone_agent.v2.middleware._redact import redact_text

    assert secret not in redact_text(f"header X-API-Key: {secret}")

    assert mounted.service("tool_risk_registry").risk_for("wait_for_stable") == "readonly"
    mounted.ctx.release_capability("systemone")


def test_manifest_config_overrides_the_declared_default(monkeypatch, mount):
    from phone_agent.v2.plugins import PluginEntry, load_plugin_spec
    from phone_agent.v2.capabilities import (
        CapabilityAssemblyContext,
        CapabilityRegistry,
        CapabilitySpec,
        assemble_capabilities,
    )
    from phone_agent.v2.events import EventBus

    monkeypatch.delenv("PHONE_AGENT_SYSTEMONE_STABLE_WAIT", raising=False)
    spec = load_plugin_spec(
        PluginEntry(
            name="systemone",
            path=str(PLUGIN_DIR),
            config={"systemone_stable_wait": "9.0"},
        )
    )
    ctx = CapabilityAssemblyContext(
        {"event_bus": EventBus(), "session": ScriptedPhoneSession(["h1"])}
    )
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("providers", "Model providers", "on"))
    registry.register(spec)
    assemble_capabilities(registry, ctx)

    assert ctx.service("setting_registry")["systemone_stable_wait"].value == 9.0
    ctx.release_capability("systemone")


def test_release_leaves_zero_residue(monkeypatch, fake_server, mount):
    from phone_agent.v2.providers import ProviderRegistry
    from phone_agent.v2.providers.builders import get_api_builder

    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", fake_server.url)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-jev-abcdef123456")
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(
        config=HarnessConfig(),
        session=session,
        extra_services={"provider_registry": ProviderRegistry()},
    )
    assert get_api_builder("systemone") is not None

    mounted.ctx.release_capability("systemone")

    assert get_api_builder("systemone") is None
    assert mounted.service("setting_registry") == {}
    assert mounted.service("tool_risk_registry").risk_for("wait_for_stable") is None
    assert mounted.service("usage_role_registry").unit_of("systemone") is None
    assert not mounted.service("redaction_registry").is_declared("sk-jev-abcdef123456")
    assert mounted.ctx.tools == []
    assert mounted.listeners("tool/execute") == []
    assert mounted.listeners("model/pre_request") == []


# ---------------------------------------------------------------------------
# Unconfigured vs explicitly misconfigured
# ---------------------------------------------------------------------------
def test_without_a_backend_the_local_seams_still_mount(monkeypatch, mount, capsys):
    monkeypatch.delenv("PHONE_AGENT_SYSTEMONE_BACKEND", raising=False)
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(config=HarnessConfig(), session=session)

    assert "wait_for_stable" in mounted.tools()
    assert mounted.listeners("tool/execute") == []  # no reviewer without a backend
    assert len(mounted.listeners("model/pre_request")) == 1
    assert "no PHONE_AGENT_SYSTEMONE_BACKEND configured" in capsys.readouterr().err


def test_an_explicit_review_without_a_backend_fails_visibly(monkeypatch, mount):
    monkeypatch.delenv("PHONE_AGENT_SYSTEMONE_BACKEND", raising=False)
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_REVIEW", "on")
    with pytest.raises(ValueError, match="REVIEW=on needs a backend"):
        mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))


def test_a_cloud_backend_without_its_key_fails_visibly(monkeypatch, mount):
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))


def test_an_unreachable_local_server_fails_visibly(monkeypatch, mount, no_listener_port):
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
    monkeypatch.setenv(
        "PHONE_AGENT_SYSTEMONE_BASE_URL", f"http://127.0.0.1:{no_listener_port}"
    )
    with pytest.raises(Exception, match="unreachable"):
        mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))


@pytest.mark.parametrize(
    "env_var,value,match",
    [
        ("PHONE_AGENT_SYSTEMONE_BACKEND", "gemini", "must be one of jev, kev"),
        ("PHONE_AGENT_SYSTEMONE_REVIEW", "maybe", "systemone_review must be one of"),
        ("PHONE_AGENT_SYSTEMONE_TIMEOUT", "30", "protocol band"),
        ("PHONE_AGENT_SYSTEMONE_RETRIES", "-1", "must not be negative"),
        ("PHONE_AGENT_SYSTEMONE_CONFIDENCE", "1.5", "must be a probability"),
        ("PHONE_AGENT_SYSTEMONE_STABLE_WAIT", "0.1", "STABLE_WAIT must be within"),
        ("PHONE_AGENT_SYSTEMONE_STABLE_INTERVAL", "0.001", "STABLE_INTERVAL must be at least"),
        ("PHONE_AGENT_SYSTEMONE_STABLE_POLLS", "1", "STABLE_POLLS must be at least 2"),
        ("PHONE_AGENT_SYSTEMONE_GOVERNOR_STEPS", "1", "GOVERNOR_STEPS must be at least 2"),
    ],
)
def test_out_of_contract_settings_fail_at_assembly(monkeypatch, mount, env_var, value, match):
    monkeypatch.setenv(env_var, value)
    with pytest.raises(ValueError, match=match):
        mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))


# ---------------------------------------------------------------------------
# Assembly-only contexts
# ---------------------------------------------------------------------------
def test_a_runless_context_gets_declarations_but_no_runtime_seams(monkeypatch, mount):
    """``main_v2.py``'s maintenance context publishes no bus and no session."""

    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", "http://127.0.0.1:1")
    mounted = mount(config=HarnessConfig(), session=None, with_bus=False)

    assert mounted.ctx.tools == []
    assert mounted.service("setting_registry")["systemone_backend"].value == "kev"
    assert mounted.service("usage_role_registry").unit_of("systemone") == "tokens"


def test_the_plugin_declares_the_ordering_it_needs():
    module = load_plugin_module()
    assert module.CAPABILITY.before == ()
    assert "providers" in module.CAPABILITY.deps
    assert "providers" in module.CAPABILITY.after


def test_settings_declare_phone_agent_prefixed_env_vars():
    module = load_plugin_module()
    for key, env_var, _default, description in module.SETTINGS:
        assert env_var.startswith("PHONE_AGENT_"), key
        assert key.islower() and " " not in key, key
        assert description.strip(), key


def test_the_plugin_is_bootstrapped_with_the_providers_capability():
    """``deps=("providers",)`` is what makes its provider usable as the actor.

    The agent selects provider contributors before it builds the actor model; a
    plugin that wants to contribute the actor's own provider must be in that
    pass, and must be mounted after the built-in ``providers`` capability.
    """

    from phone_agent.v2.agent import _build_provider_bootstrap
    from phone_agent.v2.capabilities import build_capability_registry
    from phone_agent.v2.plugins import PluginEntry, load_plugin_spec

    registry = build_capability_registry(HarnessConfig())
    registry.register(load_plugin_spec(PluginEntry(name="systemone", path=str(PLUGIN_DIR))))
    bootstrap = _build_provider_bootstrap(registry)
    order = [spec.cap_id for spec in bootstrap.specs()]
    assert "systemone" in order
    assert order.index("providers") < order.index("systemone")


def test_the_committed_manifest_relative_path_resolves_against_the_repo_root():
    """A manifest ``path`` is relative to the manifest's own directory."""

    from phone_agent.v2.plugins import project_manifest_path

    config = HarnessConfig(plugin_manifest=str(MANIFEST))
    assert project_manifest_path(config) == MANIFEST
    assert MANIFEST.parent == REPO_ROOT
    assert (MANIFEST.parent / "plugins" / "systemone" / "plugin.py").is_file()
