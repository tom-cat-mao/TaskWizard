"""Formal model-streaming configuration (WP-STREAM).

Covers the ``off`` default compatibility, the models.json model/role
declarations, the documented precedence (role > model entry > env), and the
``supportsUsageInStreaming`` -> transport mapping.  All probes are offline:
provider builds are constructed in-process and never touch a real endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from phone_agent.v2.config import V2Config
from phone_agent.v2.providers import (
    ModelSpec,
    ModelsFileError,
    ProviderRegistry,
    ProviderSpec,
    RoleSpec,
    build_model_from_resolved,
    build_provider_registry,
    parse_models_document,
    resolve_streaming_enabled,
    resolve_streaming_mode,
)
from phone_agent.v2.providers.types import ProviderCompat
from phone_agent.v2.providers.registry import DEFAULT_PROVIDER_ID


def _config(**overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="cfg-key",
        model_timeout=99.0,
        model_max_retries=4,
        http_headers=None,
        user_agent=None,
        cf_access_client_id=None,
        cf_access_client_secret=None,
        sampling=None,
        parallel_tool_calls=False,
        thinking="",
        streaming="off",
        models_file=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _registry(*, model: ModelSpec | None = None, role_streaming: str | None = None):
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            id=DEFAULT_PROVIDER_ID,
            api="openai-completions",
            base_url="http://localhost:8000/v1",
            api_key="k",
            models={"main-model": model or ModelSpec(id="main-model")},
        )
    )
    registry.roles = (
        {"actor": RoleSpec(streaming=role_streaming)} if role_streaming else {}
    )
    registry.declaration_warnings = []
    return registry


# ---------------------------------------------------------------- env + models.json


def test_streaming_env_defaults_to_off_and_parses_on(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_STREAMING", raising=False)
    assert V2Config.from_env().streaming == "off"
    monkeypatch.setenv("PHONE_AGENT_STREAMING", "on")
    assert V2Config.from_env().streaming == "on"
    monkeypatch.setenv("PHONE_AGENT_STREAMING", "sometimes")
    assert V2Config.from_env().streaming == "off"


def test_models_json_streaming_declarations_parse():
    providers, roles = parse_models_document(
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "models": [{"id": "m1", "streaming": "on"}],
                }
            },
            "roles": {"actor": {"streaming": "off"}},
        }
    )
    assert providers["acme"].models["m1"].streaming == "on"
    assert roles["actor"].streaming == "off"


def test_models_json_rejects_unknown_streaming_value():
    with pytest.raises(ModelsFileError):
        parse_models_document(
            {"providers": {"acme": {"api": "openai-completions", "models": [
                {"id": "m1", "streaming": "maybe"}
            ]}}}
        )
    with pytest.raises(ModelsFileError):
        parse_models_document({"roles": {"actor": {"streaming": 1}}})


def test_model_override_patch_carries_streaming():
    providers, _ = parse_models_document(
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "models": [{"id": "m1"}],
                    "modelOverrides": {"m1": {"streaming": "on"}},
                }
            }
        }
    )
    assert providers["acme"].models["m1"].streaming == "on"


def test_explicit_models_file_roles_reach_the_registry(tmp_path: Path):
    models_file = tmp_path / "models.json"
    models_file.write_text(
        json.dumps(
            {
                "providers": {
                    "acme": {
                        "api": "openai-completions",
                        "baseUrl": "https://acme.example/v1",
                        "models": [{"id": "m1", "streaming": "off"}],
                    }
                },
                "roles": {"actor": {"streaming": "on"}},
            }
        ),
        encoding="utf-8",
    )
    config = _config(models_file=str(models_file))
    registry = build_provider_registry(config)
    assert registry.roles["actor"].streaming == "on"
    assert registry.get("acme").models["m1"].streaming == "off"


# ------------------------------------------------------------------- precedence


def test_precedence_role_beats_model_entry_beats_env():
    config = _config(streaming="on")
    assert resolve_streaming_mode(config, "actor", _registry()) == "on"
    model_off = _registry(model=ModelSpec(id="main-model", streaming="off"))
    assert resolve_streaming_mode(config, "actor", model_off) == "off"
    role_on = _registry(
        model=ModelSpec(id="main-model", streaming="off"), role_streaming="on"
    )
    assert resolve_streaming_mode(config, "actor", role_on) == "on"
    assert resolve_streaming_enabled(
        _config(streaming="off"), "actor", _registry(role_streaming="on")
    )
    assert resolve_streaming_mode(_config(streaming="off"), "actor", _registry()) == "off"


def test_precedence_follows_the_actors_actual_model_reference():
    """The model tier must be read from the model the role will call."""

    registry = _registry()
    registry.override(
        ProviderSpec(
            id="acme",
            api="openai-completions",
            base_url="https://acme.example/v1",
            models={"main-model": ModelSpec(id="main-model", streaming="on")},
        )
    )
    config = _config(model_name="acme:main-model", streaming="off")
    assert resolve_streaming_mode(config, "actor", registry) == "on"


def test_unresolvable_reference_never_raises():
    config = _config(model_name="ghost:model", streaming="on")
    assert resolve_streaming_mode(config, "actor", _registry()) == "on"
    assert resolve_streaming_mode(config, "actor", None) == "on"
    assert resolve_streaming_mode(_config(streaming=""), "actor", None) == "off"


# ------------------------------------------------- supportsUsageInStreaming mapping


def _openai_resolved(model: ModelSpec, provider_kwargs: dict | None = None):
    provider = ProviderSpec(
        id="acme",
        api="openai-completions",
        base_url="https://acme.example/v1",
        api_key="k",
        models={"m1": model},
        **(provider_kwargs or {}),
    )
    return provider.models["m1"], provider


def test_openai_stream_usage_follows_compat_declaration():
    model, provider = _openai_resolved(ModelSpec(id="m1"))
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model, ref="acme:m1"), _config()
    )
    assert built.stream_usage is None
    assert built._should_stream_usage() is False

    declared_on = ModelSpec(
        id="m1", compat=ProviderCompat(supports_usage_in_streaming=True)
    )
    model, provider = _openai_resolved(declared_on)
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model, ref="acme:m1"), _config()
    )
    assert built.stream_usage is True
    assert built._should_stream_usage() is True

    declared_off = ModelSpec(
        id="m1", compat=ProviderCompat(supports_usage_in_streaming=False)
    )
    model, provider = _openai_resolved(declared_off)
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model, ref="acme:m1"), _config()
    )
    assert built.stream_usage is False
    assert built._should_stream_usage() is False


def test_anthropic_stream_usage_follows_compat_declaration():
    model = ModelSpec(id="claude-x")
    provider = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        base_url="https://acme.example",
        api_key="k",
        models={"claude-x": model},
    )
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model, ref="acme:claude-x"), _config()
    )
    assert built.stream_usage is True

    declared_off = ModelSpec(
        id="claude-x", compat=ProviderCompat(supports_usage_in_streaming=False)
    )
    provider = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        base_url="https://acme.example",
        api_key="k",
        models={"claude-x": declared_off},
    )
    built = build_model_from_resolved(
        SimpleNamespace(
            provider=provider, model=declared_off, ref="acme:claude-x"
        ),
        _config(),
    )
    assert built.stream_usage is False


def test_google_has_no_usage_in_streaming_translation():
    """The google protocol has no request-side switch; the declaration is inert."""

    model = ModelSpec(
        id="gemini-x", compat=ProviderCompat(supports_usage_in_streaming=False)
    )
    provider = ProviderSpec(
        id="acme",
        api="google-generative-ai",
        api_key="k",
        models={"gemini-x": model},
    )
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model, ref="acme:gemini-x"), _config()
    )
    assert not hasattr(built, "stream_usage")


def test_legacy_build_stays_byte_for_byte():
    """No registry -> the historical single-gateway build keeps stream_usage None."""

    from phone_agent.v2.model import build_chat_model

    config = V2Config(base_url="http://localhost:8000/v1", model_name="m", api_key="k")
    built = build_chat_model(config)
    assert built.stream_usage is None
    assert built.streaming is False
    config.streaming = "on"
    streamed = build_chat_model(config)
    assert streamed.streaming is True
    assert built.streaming is False


# --------------------------------------------- role chain -> transport streaming


def _models_file(tmp_path: Path, *, model_streaming: str | None = None) -> Path:
    entry: dict[str, Any] = {"id": "main-model"}
    if model_streaming is not None:
        entry["streaming"] = model_streaming
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    DEFAULT_PROVIDER_ID: {
                        "api": "openai-completions",
                        "baseUrl": "http://fake.test/v1",
                        "models": [entry],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _build_role(tmp_path: Path, *, global_mode: str, roles: dict | None, **extra):
    from phone_agent.v2.model import build_chat_model

    models_file = _models_file(tmp_path)
    if roles:
        payload = json.loads(models_file.read_text(encoding="utf-8"))
        payload["roles"] = roles
        models_file.write_text(json.dumps(payload), encoding="utf-8")
    config = V2Config.from_env(
        {
            "base_url": "http://fake.test/v1",
            "model_name": "main-model",
            "api_key": "k",
            "models_file": str(models_file),
            "streaming": global_mode,
            **extra,
        }
    )
    registry = build_provider_registry(config)
    model = build_chat_model(config, role="actor", registry=registry)
    return config, registry, model


def test_role_chain_build_drives_transport_streaming(tmp_path):
    _, _, model = _build_role(tmp_path, global_mode="on", roles=None)
    assert model.streaming is True

    _, _, model = _build_role(tmp_path, global_mode="off", roles=None)
    assert model.streaming is False

    _, _, model = _build_role(
        tmp_path, global_mode="on", roles={"actor": {"streaming": "off"}}
    )
    assert model.streaming is False

    _, _, model = _build_role(
        tmp_path, global_mode="off", roles={"actor": {"streaming": "on"}}
    )
    assert model.streaming is True


def test_model_entry_declaration_outranks_global_switch(tmp_path):
    from dataclasses import replace

    from phone_agent.v2.model import build_chat_model

    models_file = _models_file(tmp_path, model_streaming="off")
    config = V2Config.from_env(
        {
            "base_url": "http://fake.test/v1",
            "model_name": "main-model",
            "api_key": "k",
            "models_file": str(models_file),
            "streaming": "on",
        }
    )
    registry = build_provider_registry(config)
    model = build_chat_model(config, role="actor", registry=registry)
    assert model.streaming is False

    roles_registry = build_provider_registry(
        replace(config, streaming="off"),
    )
    memory = build_chat_model(
        replace(config, streaming="off"), role="memory", registry=roles_registry
    )
    assert memory.streaming is False


def test_fallback_build_uses_its_own_streaming_declaration(tmp_path):
    from phone_agent.v2.model import build_actor_fallback

    models_file = _models_file(tmp_path)
    payload = json.loads(models_file.read_text(encoding="utf-8"))
    payload["providers"][DEFAULT_PROVIDER_ID]["models"].extend(
        [
            {"id": "backup-off-model", "streaming": "off"},
            {"id": "backup-on-model", "streaming": "on"},
        ]
    )
    payload["roles"] = {"actor": {"streaming": "on"}}
    models_file.write_text(json.dumps(payload), encoding="utf-8")

    def _build(global_mode: str, fallback_ref: str):
        config = V2Config.from_env(
            {
                "base_url": "http://fake.test/v1",
                "model_name": "main-model",
                "api_key": "k",
                "models_file": str(models_file),
                "streaming": global_mode,
                "fallback_model": fallback_ref,
            }
        )
        registry = build_provider_registry(config)
        return config, build_actor_fallback(
            config, registry=registry, primary_ref="main-model"
        )

    _, primary_cfg = _build("off", "backup-off-model")
    fallback_model, ref = primary_cfg
    assert ref == "backup-off-model"
    assert fallback_model.streaming is False

    _, off_backup = _build("on", "backup-off-model")
    assert off_backup is not None and off_backup[0].streaming is False

    _, on_backup = _build("off", "backup-on-model")
    assert on_backup is not None and on_backup[0].streaming is True


def test_headless_invoke_streams_and_aggregates_the_full_message(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    import langchain_openai.chat_models.base as openai_base

    from tests.web.test_model_streaming import _openai_transport

    monkeypatch.setattr(
        openai_base,
        "_get_default_httpx_client",
        lambda *_args, **_kwargs: httpx.Client(
            transport=httpx.MockTransport(_openai_transport())
        ),
    )
    wire: list[dict] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content.decode()))
        return _openai_transport()(request)

    monkeypatch.setattr(
        openai_base,
        "_get_default_httpx_client",
        lambda *_args, **_kwargs: httpx.Client(
            transport=httpx.MockTransport(recording_handler)
        ),
    )
    _, _, model = _build_role(tmp_path, global_mode="on", roles=None)
    message = model.invoke("hi")
    assert isinstance(message, AIMessage)
    assert message.tool_calls == [
        {"name": "tap", "args": {"target": "ok"}, "id": "call_1", "type": "tool_call"}
    ]
    assert message.usage_metadata["total_tokens"] == 17
    assert wire[0]["stream"] is True
