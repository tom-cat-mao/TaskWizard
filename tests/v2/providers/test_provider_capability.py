"""S4 capability seam: providers capability, ctx service, plugin register/unregister."""

from __future__ import annotations

import json

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    build_capability_registry,
)
from phone_agent.v2.config import V2Config
from phone_agent.v2.providers import (
    ProviderRegistry,
    ProviderSpec,
    ModelSpec,
    build_provider_registry,
    register_provider,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _config(tmp_path, **overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        trace_dir=str(tmp_path / "traces"),
        trace_enabled=False,
        experience_enabled=False,
    )
    defaults.update(overrides)
    return V2Config(**defaults)


def _ctx(config, **services):
    payload = {"config": config}
    payload.update(services)
    return CapabilityAssemblyContext(payload)


def test_providers_capability_mounts_registry_service(isolated, tmp_path):
    config = _config(tmp_path)
    ctx = _ctx(config)
    assemble = __import__(
        "phone_agent.v2.capabilities", fromlist=["assemble_capabilities"]
    ).assemble_capabilities
    assemble(build_capability_registry(config), ctx)
    registry = ctx.service("provider_registry")
    assert isinstance(registry, ProviderRegistry)
    assert "gateway" in registry.list_providers()
    # The side channel lets auxiliary role builds find the same handle.
    assert config._provider_registry is registry


def test_capability_reuses_harness_built_registry(isolated, tmp_path):
    config = _config(tmp_path)
    existing = build_provider_registry(config)
    config._provider_registry = existing
    ctx = _ctx(config)
    assemble = __import__(
        "phone_agent.v2.capabilities", fromlist=["assemble_capabilities"]
    ).assemble_capabilities
    assemble(build_capability_registry(config), ctx)
    assert ctx.service("provider_registry") is existing


def test_capability_survives_broken_models_json_with_warning(isolated, tmp_path):
    (isolated / ".taskwizard.models.json").write_text("{broken", encoding="utf-8")
    config = _config(tmp_path)
    ctx = _ctx(config)
    assemble = __import__(
        "phone_agent.v2.capabilities", fromlist=["assemble_capabilities"]
    ).assemble_capabilities

    assemble(build_capability_registry(config), ctx)

    registry = ctx.service("provider_registry")
    assert "gateway" in registry.list_providers()
    assert any("malformed JSON" in w.error for w in registry.declaration_warnings)


def test_plugin_register_and_unregister_provider(isolated, tmp_path):
    config = _config(tmp_path)
    ctx = _ctx(config)
    assemble = __import__(
        "phone_agent.v2.capabilities", fromlist=["assemble_capabilities"]
    ).assemble_capabilities
    assemble(build_capability_registry(config), ctx)

    register_provider(
        ctx,
        ProviderSpec(
            id="acme",
            api="openai-completions",
            base_url="https://acme.example/v1",
            api_key="k",
            models={"fast": ModelSpec(id="fast")},
        ),
    )
    registry = ctx.service("provider_registry")
    assert "acme" in registry.list_providers()

    # A role can now route through the plugin provider via the side channel.
    from phone_agent.v2.model import build_role_model

    routed = V2Config(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        memory_model="acme:fast",
    )
    routed._provider_registry = ctx.service("provider_registry")
    model = build_role_model(routed, role="memory")
    assert model.model_name == "fast"
    assert model.openai_api_base == "https://acme.example/v1"

    assert registry.unregister("acme") is True
    from phone_agent.v2.providers.registry import UnknownProviderError

    with pytest.raises(UnknownProviderError):
        registry.resolve("acme:fast")


def test_register_provider_reuses_or_builds_registry(isolated, tmp_path):
    config = _config(tmp_path)
    ctx = _ctx(config)  # no assembly ran: no provider_registry service
    register_provider(
        ctx,
        ProviderSpec(id="solo", api="openai-completions", models={}),
    )
    registry = ctx.service("provider_registry")
    assert "solo" in registry.list_providers()
    assert "gateway" in registry.list_providers()


def test_release_removes_provider_service(isolated, tmp_path):
    from phone_agent.v2.capabilities import assemble_capabilities

    config = _config(tmp_path)
    registry = build_capability_registry(config)
    ctx = _ctx(config)
    assemble_capabilities(registry, ctx)
    assert ctx.service("provider_registry") is not None
    spec = registry.specs()[0]
    assert spec.cap_id == "providers"
    spec.release(ctx)
    assert ctx.service("provider_registry") is None


def test_models_json_provider_visible_after_assembly(isolated, tmp_path):
    """A models.json provider routes roles without any plugin involvement."""

    path = isolated / ".taskwizard.models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "api": "anthropic-messages",
                        "apiKey": "sk",
                        "models": [{"id": "claude-x"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path, verifier_model="anthropic:claude-x")
    from phone_agent.v2.model import build_role_model

    model = build_role_model(config, role="verifier")
    from langchain_anthropic import ChatAnthropic

    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-x"


def test_models_json_thinking_translation_end_to_end(isolated, tmp_path):
    """PHONE_AGENT_THINKING flows through compat/thinkingLevelMap to the client."""

    path = isolated / ".taskwizard.models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "qwen": {
                        "api": "openai-completions",
                        "apiKey": "k",
                        "compat": {"thinkingFormat": "enable_thinking"},
                        "models": [
                            {
                                "id": "q3",
                                "samplingParams": {"temperature": 0.7},
                                "thinkingLevelMap": {"medium": True},
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path, memory_model="qwen:q3", thinking="medium")
    from phone_agent.v2.model import build_role_model

    model = build_role_model(config, role="memory")
    assert model.model_name == "q3"
    assert model.temperature == 0.7
    assert model.extra_body == {"enable_thinking": True}
