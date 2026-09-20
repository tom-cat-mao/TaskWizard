"""S4 registry: registration, override/unregister, and reference resolution."""

from __future__ import annotations

import pytest

from phone_agent.v2.providers.registry import (
    DEFAULT_PROVIDER_ID,
    ProviderRegistry,
    UnknownProviderError,
)
from phone_agent.v2.providers.types import ModelSpec, ProviderSpec


def _spec(provider_id="gateway", **overrides):
    defaults = dict(
        id=provider_id,
        api="openai-completions",
        base_url="https://p.example/v1",
        api_key="k",
        models={"m1": ModelSpec(id="m1", name="M1")},
    )
    defaults.update(overrides)
    return ProviderSpec(**defaults)


def test_register_and_duplicate_rejected():
    registry = ProviderRegistry()
    registry.register(_spec())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec())


def test_override_upserts_models_and_fields():
    registry = ProviderRegistry()
    registry.register(_spec())
    registry.override(
        _spec(
            base_url="https://new.example/v1",
            models={"m2": ModelSpec(id="m2")},
        )
    )
    provider = registry.get("gateway")
    assert provider.base_url == "https://new.example/v1"
    assert set(provider.model_ids()) == {"m1", "m2"}  # upserted, not replaced


def test_baseurl_only_override_keeps_models():
    registry = ProviderRegistry()
    registry.register(_spec())
    registry.override(_spec(models={}))
    assert registry.get("gateway").model_ids() == ("m1",)


def test_unregister_returns_presence():
    registry = ProviderRegistry()
    registry.register(_spec())
    assert registry.unregister("gateway") is True
    assert registry.unregister("gateway") is False


def test_resolve_bare_name_uses_default_provider():
    registry = ProviderRegistry()
    registry.register(_spec())
    resolved = registry.resolve("m1")
    assert resolved.provider.id == DEFAULT_PROVIDER_ID
    assert resolved.model.id == "m1"
    assert resolved.synthesized is False


def test_resolve_provider_model_addressing():
    registry = ProviderRegistry()
    registry.register(_spec())
    resolved = registry.resolve("gateway:m1")
    assert resolved.provider.id == "gateway"
    assert resolved.model.id == "m1"


def test_resolve_unknown_model_synthesizes_defaults():
    registry = ProviderRegistry()
    registry.register(_spec())
    resolved = registry.resolve("not-in-catalog")
    assert resolved.provider.id == "gateway"
    assert resolved.model.id == "not-in-catalog"
    assert resolved.synthesized is True
    # Synthesized spec carries pristine defaults.
    assert resolved.model.sampling_params == {}
    assert resolved.model.headers == {}


def test_resolve_unknown_provider_fails_visibly():
    registry = ProviderRegistry()
    registry.register(_spec())
    with pytest.raises(UnknownProviderError):
        registry.resolve("nosuch:model")


def test_resolve_empty_reference_rejected():
    registry = ProviderRegistry()
    with pytest.raises(Exception):
        registry.resolve("")


def test_list_providers_and_models():
    registry = ProviderRegistry()
    registry.register(_spec())
    registry.register(_spec("acme", models={"a": ModelSpec(id="a")}))
    assert registry.list_providers() == ("gateway", "acme")
    assert registry.list_models("acme") == ("a",)
    with pytest.raises(UnknownProviderError):
        registry.list_models("ghost")


def test_resolve_colon_in_model_name_after_provider_split():
    registry = ProviderRegistry()
    registry.register(_spec("ollama", models={}))
    resolved = registry.resolve("ollama:qwen3:32b")
    assert resolved.provider.id == "ollama"
    assert resolved.model.id == "qwen3:32b"  # split on the first colon only
    assert resolved.synthesized is True
