"""S4 roles: fallback chains, provider:model env values, zero-config regression."""

from __future__ import annotations

from dataclasses import replace as dc_replace

import pytest

from phone_agent.v2.config import V2Config
from phone_agent.v2.model import (
    _build_legacy,
    build_chat_model,
    build_role_model,
)
from phone_agent.v2.providers import build_provider_registry, resolve_role_ref
from phone_agent.v2.providers.registry import ProviderRegistry
from phone_agent.v2.providers.types import ModelSpec, ProviderSpec

_CLIENT_FIELDS = {"client", "async_client", "root_client", "root_async_client"}


def _config(**overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="cfg-key",
        memory_model=None,
        verifier_model=None,
        safety_reviewer_model=None,
        models_file=None,
        thinking="",
    )
    defaults.update(overrides)
    return V2Config(**defaults)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _config_dict(model):
    """Semantic field view of a built chat model (client objects excluded)."""

    return {
        key: value
        for key, value in model.__dict__.items()
        if key not in _CLIENT_FIELDS
    }


def test_role_fallback_chains():
    cfg = _config(
        memory_model="mem-model",
        verifier_model="ver-model",
        safety_reviewer_model="rev-model",
    )
    assert resolve_role_ref(cfg, "actor") == "main-model"
    assert resolve_role_ref(cfg, "memory") == "mem-model"
    assert resolve_role_ref(cfg, "verifier") == "ver-model"
    assert resolve_role_ref(cfg, "safety_reviewer") == "rev-model"
    assert resolve_role_ref(cfg, "distill") == "mem-model"


def test_role_fallback_chain_defaults():
    cfg = _config(verifier_model="ver-model")
    assert resolve_role_ref(cfg, "memory") == "main-model"  # -> actor
    assert resolve_role_ref(cfg, "safety_reviewer") == "ver-model"  # -> verifier
    assert resolve_role_ref(cfg, "distill") == "main-model"  # -> memory -> actor


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="unknown role"):
        resolve_role_ref(_config(), "locate")


def test_role_env_value_with_provider_addressing(isolated):
    """Role env values may carry provider:model refs resolved via the registry."""

    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            id="acme",
            api="openai-completions",
            base_url="https://acme.example/v1",
            api_key="acme-key",
            models={"fast": ModelSpec(id="fast")},
        )
    )
    cfg = _config(memory_model="acme:fast")
    model = build_role_model(cfg, role="memory", registry=registry)
    assert model.model_name == "fast"
    assert model.openai_api_base == "https://acme.example/v1"
    assert model.openai_api_key.get_secret_value() == "acme-key"


def test_safety_reviewer_three_hop_via_registry(isolated):
    registry = build_provider_registry(_config())
    cfg = _config(safety_reviewer_model="reviewer-model")
    model = build_role_model(cfg, role="safety_reviewer", registry=registry)
    assert model.model_name == "reviewer-model"


def test_zero_config_actor_matches_legacy_byte_level(isolated):
    """The registry path must reproduce the legacy ChatOpenAI config exactly."""

    cfg = _config(
        sampling={"temperature": 1.0, "top_p": 0.95},
        http_headers={"X-Custom": "v1"},
        user_agent="UA/2",
    )
    legacy = _build_legacy(cfg)
    via_role = build_role_model(cfg, role="actor")
    assert _config_dict(via_role) == _config_dict(legacy)


def test_zero_config_explicit_registry_matches_legacy(isolated):
    cfg = _config(sampling={"frequency_penalty": 0.0})
    registry = build_provider_registry(cfg)
    via_registry = build_chat_model(cfg, role="actor", registry=registry)
    assert _config_dict(via_registry) == _config_dict(_build_legacy(cfg))


def test_zero_config_all_roles_match_legacy_substitution(isolated):
    cfg = _config(memory_model="mem-model", verifier_model="ver-model")
    registry = build_provider_registry(cfg)
    for role, ref in (
        ("memory", "mem-model"),
        ("verifier", "ver-model"),
        ("safety_reviewer", "ver-model"),
        ("distill", "mem-model"),
    ):
        built = build_role_model(cfg, role=role, registry=registry)
        expected = _build_legacy(dc_replace(cfg, model_name=ref))
        assert _config_dict(built) == _config_dict(expected), role


def test_explicit_registry_failure_is_visible(isolated):
    """An explicit registry cannot silently manufacture a gateway client."""

    registry = ProviderRegistry()  # no default provider registered
    cfg = _config(memory_model="mem-model")
    from phone_agent.v2.providers import UnknownProviderError

    with pytest.raises(UnknownProviderError, match="default provider"):
        build_role_model(cfg, role="memory", registry=registry)


def test_build_chat_model_without_registry_is_legacy(isolated):
    cfg = _config()
    model = build_chat_model(cfg)
    assert _config_dict(model) == _config_dict(_build_legacy(cfg))
    # And the actor role via the lazy registry path agrees.
    assert _config_dict(build_role_model(cfg, role="actor")) == _config_dict(model)
