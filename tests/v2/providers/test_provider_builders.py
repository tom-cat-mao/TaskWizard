"""S4 builders: api -> BaseChatModel kwargs translation.

Covers headers merge, sampling precedence (modelSpec < config < role),
thinking translation with silent omission, parallel_tool_calls (openai only),
and per-api constructor mapping.
"""

from __future__ import annotations

from types import SimpleNamespace

from phone_agent.v2.providers.builders import (
    build_model_from_resolved,
    effective_compat,
    translate_thinking,
)
from phone_agent.v2.providers.registry import ProviderRegistry
from phone_agent.v2.providers.types import (
    ModelSpec,
    ProviderCompat,
    ProviderSpec,
    ResolvedModel,
    THINKING_MAP_UNSET,
)


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
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _openai_model(**model_overrides) -> ModelSpec:
    return ModelSpec(id="m1", name="M1", **model_overrides)


def _openai_provider(model: ModelSpec | None = None, **spec_overrides) -> ProviderSpec:
    defaults = dict(
        id="acme",
        api="openai-completions",
        base_url="https://acme.example/v1",
        api_key="acme-key",
        headers={"X-Provider": "p"},
        models={"m1": model or _openai_model()},
    )
    defaults.update(spec_overrides)
    return ProviderSpec(**defaults)


def _resolve(provider: ProviderSpec, model: ModelSpec | None = None) -> ResolvedModel:
    model = model or provider.get_model("m1")
    return ResolvedModel(provider=provider, model=model, ref=f"{provider.id}:m1")


def test_zero_config_matches_legacy_build():
    from phone_agent.v2.config import V2Config
    from phone_agent.v2.model import _build_legacy, build_default_headers

    config = V2Config(
        base_url="http://localhost:8000/v1",
        model_name="autoglm-phone-9b",
        api_key="EMPTY",
        sampling={"temperature": 1.0},
    )
    registry = ProviderRegistry()
    registry.register(
        ProviderSpec(
            id="gateway",
            api="openai-completions",
            base_url=config.base_url,
            api_key=config.api_key,
            headers=build_default_headers(config),
            models={config.model_name: ModelSpec(id=config.model_name)},
        )
    )
    built = build_model_from_resolved(registry.resolve(config.model_name), config)
    legacy = _build_legacy(config)
    # Every constructor-relevant field must match the legacy build.
    assert built.openai_api_base == legacy.openai_api_base
    assert built.model_name == legacy.model_name
    assert built.openai_api_key.get_secret_value() == legacy.openai_api_key.get_secret_value()
    assert built.model_kwargs == legacy.model_kwargs
    assert built.request_timeout == legacy.request_timeout
    assert built.max_retries == legacy.max_retries
    assert built.temperature == legacy.temperature
    assert built.default_headers == legacy.default_headers


def test_headers_merge_provider_model_role():
    model = ModelSpec(id="m1", headers={"X-Model": "m"})
    provider = _openai_provider(
        model, headers={"X-Provider": "p", "X-Shared": "provider"}
    )
    built = build_model_from_resolved(
        _resolve(provider, model), _config(), role_headers={"X-Shared": "role"}
    )
    headers = built.default_headers
    assert headers["X-Provider"] == "p"
    assert headers["X-Model"] == "m"
    assert headers["X-Shared"] == "role"  # role override wins


def test_sampling_precedence_model_lt_config_lt_role():
    model = _openai_model(sampling_params={"temperature": 0.1, "seed": 7})
    provider = _openai_provider(model)
    built = build_model_from_resolved(
        _resolve(provider, model),
        _config(sampling={"temperature": 0.5}),
        role_sampling={"temperature": 0.9},
    )
    assert built.temperature == 0.9  # role override highest
    assert built.seed == 7  # model-level untouched by higher tiers
    assert built.top_p is None


def test_unknown_sampling_key_goes_to_model_kwargs():
    model = _openai_model(sampling_params={"custom_body_knob": "x"})
    provider = _openai_provider(model)
    built = build_model_from_resolved(_resolve(provider, model), _config())
    assert built.model_kwargs.get("custom_body_knob") == "x"
    assert built.model_kwargs.get("parallel_tool_calls") is False


def test_parallel_tool_calls_default_only_on_openai_path():
    provider = _openai_provider()
    built = build_model_from_resolved(_resolve(provider), _config())
    assert built.model_kwargs["parallel_tool_calls"] is False

    optout = build_model_from_resolved(_resolve(provider), _config(parallel_tool_calls=True))
    assert "parallel_tool_calls" not in optout.model_kwargs

    # An endpoint declared unable to accept the flag omits it silently.
    provider_nc = _openai_provider(
        compat=ProviderCompat(supports_parallel_tool_calls=False)
    )
    built_nc = build_model_from_resolved(_resolve(provider_nc), _config())
    assert "parallel_tool_calls" not in built_nc.model_kwargs


def test_anthropic_path_has_no_parallel_tool_calls():
    provider = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        base_url="https://acme.example",
        api_key="ak",
        models={"m1": ModelSpec(id="m1")},
    )
    built = build_model_from_resolved(_resolve(provider), _config())
    from langchain_anthropic import ChatAnthropic

    assert isinstance(built, ChatAnthropic)
    assert built.model == "m1"
    assert built.thinking is None


def test_thinking_reasoning_effort_openai():
    provider = _openai_provider(compat=ProviderCompat(thinking_format="reasoning_effort"))
    built = build_model_from_resolved(_resolve(provider), _config(thinking="high"))
    assert built.reasoning_effort == "high"


def test_thinking_map_str_verbatim_and_dict():
    model = _openai_model(thinking_level_map="low")
    provider = _openai_provider(
        model, compat=ProviderCompat(thinking_format="reasoning_effort")
    )
    built = build_model_from_resolved(
        _resolve(provider, model), _config(thinking="high")
    )
    assert built.reasoning_effort == "low"  # verbatim string map

    model2 = _openai_model(thinking_level_map={"high": "medium"})
    provider2 = _openai_provider(
        model2, compat=ProviderCompat(thinking_format="reasoning_effort")
    )
    built2 = build_model_from_resolved(
        _resolve(provider2, model2), _config(thinking="high")
    )
    assert built2.reasoning_effort == "medium"  # dict-mapped level


def test_thinking_silently_omitted_when_unsupported():
    # Explicit null map -> unsupported -> omitted even with a format declared.
    model = _openai_model(thinking_level_map=None)
    kwargs, extra = translate_thinking(
        "high",
        model,
        ProviderCompat(thinking_format="reasoning_effort").resolved(),
    )
    assert kwargs == {} and extra == {}

    # No thinking_format at all -> omitted.
    kwargs, extra = translate_thinking(
        "high", _openai_model(), ProviderCompat().resolved()
    )
    assert kwargs == {} and extra == {}

    # off / empty level -> omitted.
    kwargs, extra = translate_thinking(
        "off",
        _openai_model(),
        ProviderCompat(thinking_format="reasoning_effort").resolved(),
    )
    assert kwargs == {} and extra == {}


def test_thinking_enable_thinking_extra_body():
    provider = _openai_provider(
        compat=ProviderCompat(
            thinking_format="enable_thinking", extra_body={"top_k": 50}
        )
    )
    built = build_model_from_resolved(
        _resolve(provider), _config(thinking="medium")
    )
    assert built.extra_body["enable_thinking"] is True
    assert built.extra_body["top_k"] == 50  # compat extra_body preserved


def test_thinking_anthropic_budget_and_temperature_drop():
    provider = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        base_url="https://x",
        api_key="ak",
        compat=ProviderCompat(thinking_format="anthropic_thinking"),
        models={"m1": ModelSpec(id="m1")},
    )
    built = build_model_from_resolved(
        _resolve(provider),
        _config(thinking="high", sampling={"temperature": 0.3, "max_tokens": 8192}),
    )
    assert built.thinking == {"type": "enabled", "budget_tokens": 8192}
    # temperature conflicts with thinking on Anthropic -> silently dropped.
    assert built.temperature is None
    assert built.max_tokens == 8192

    # Explicit int budget via the map wins over the built-in table.
    provider2 = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        api_key="ak",
        compat=ProviderCompat(thinking_format="anthropic_thinking"),
        models={"m1": ModelSpec(id="m1", thinking_level_map={"low": 1500})},
    )
    built2 = build_model_from_resolved(
        _resolve(provider2), _config(thinking="low")
    )
    assert built2.thinking == {"type": "enabled", "budget_tokens": 1500}


def test_google_path_kwargs():
    provider = ProviderSpec(
        id="gg",
        api="google-generative-ai",
        base_url="https://gg.example",
        api_key="gkey",
        models={"m1": ModelSpec(id="m1")},
    )
    built = build_model_from_resolved(
        _resolve(provider), _config(sampling={"temperature": 0.7})
    )
    from langchain_google_genai import ChatGoogleGenerativeAI

    assert isinstance(built, ChatGoogleGenerativeAI)
    assert built.model == "m1"
    assert built.temperature == 0.7
    assert built.google_api_key.get_secret_value() == "gkey"


def test_google_thinking_reasoning_effort_literal_guard():
    provider = ProviderSpec(
        id="gg",
        api="google-generative-ai",
        api_key="gkey",
        compat=ProviderCompat(thinking_format="reasoning_effort"),
        models={"m1": ModelSpec(id="m1")},
    )
    built = build_model_from_resolved(_resolve(provider), _config(thinking="low"))
    assert built.reasoning_effort == "low"

    # A map value outside google's Literal set is omitted silently.
    provider2 = ProviderSpec(
        id="gg",
        api="google-generative-ai",
        api_key="gkey",
        compat=ProviderCompat(thinking_format="reasoning_effort"),
        models={"m1": ModelSpec(id="m1", thinking_level_map={"low": "xhigh"})},
    )
    built2 = build_model_from_resolved(_resolve(provider2), _config(thinking="low"))
    assert built2.reasoning_effort is None


def test_max_tokens_field_compat_routing():
    provider = _openai_provider(
        compat=ProviderCompat(max_tokens_field="max_completion_tokens")
    )
    built = build_model_from_resolved(
        _resolve(provider), _config(sampling={"max_tokens": 512})
    )
    # ChatOpenAI aliases max_tokens <-> max_completion_tokens onto one field.
    assert built.max_tokens == 512


def test_effective_compat_model_overrides_provider():
    provider_compat = ProviderCompat(thinking_format="enable_thinking")
    model_compat = ProviderCompat(thinking_format="reasoning_effort")
    resolved = effective_compat(
        ProviderSpec(id="p", api="openai-completions", compat=provider_compat),
        ModelSpec(id="m", compat=model_compat),
    )
    assert resolved.thinking_format == "reasoning_effort"
    # Unset model fields inherit provider values.
    resolved2 = effective_compat(
        ProviderSpec(id="p", api="openai-completions", compat=provider_compat),
        ModelSpec(id="m", compat=ProviderCompat()),
    )
    assert resolved2.thinking_format == "enable_thinking"
    assert resolved2.supports_parallel_tool_calls is True
    assert resolved2.max_tokens_field == "max_tokens"


def test_thinking_map_unset_sentinel_distinguishes_absent():
    assert _openai_model().thinking_level_map is THINKING_MAP_UNSET
