"""api -> BaseChatModel builders (S4).

One builder per declared provider ``api``; each translates the merged
ProviderSpec/ModelSpec data plus the V2Config transport defaults into a
configured LangChain chat model:

* ``openai-completions``  -> ``langchain_openai.ChatOpenAI``
* ``anthropic-messages``  -> ``langchain_anthropic.ChatAnthropic``
* ``google-generative-ai``-> ``langchain_google_genai.ChatGoogleGenerativeAI``

Merge orders (per the design doc):

* sampling: ``ModelSpec.sampling_params`` < ``config.sampling`` < explicit
  role override (``role_sampling`` argument).
* headers: provider -> model -> role override.  The built-in gateway provider
  synthesizes its provider headers from ``build_default_headers`` (browser UA +
  CF Access pair preserved).
* thinking: global ``PHONE_AGENT_THINKING`` level translated through
  ``ModelSpec.thinking_level_map`` + ``ProviderCompat.thinking_format``;
  unsupported/absent declarations omit the param silently.

``parallel_tool_calls=False`` stays an openai-path-only default (P0 #15): the
thin loop is one-observation-one-action, and only the ChatOpenAI transport
forwards it via ``model_kwargs`` so it survives every ``bind_tools`` re-bind.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from phone_agent.v2.providers.types import (
    API_ANTHROPIC,
    API_GOOGLE,
    API_OPENAI,
    THINKING_MAP_UNSET,
    ModelSpec,
    ProviderCompat,
    ProviderSpec,
    ResolvedCompat,
    ResolvedModel,
)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from phone_agent.v2.config import V2Config

logger = logging.getLogger(__name__)

# Anthropic thinking budgets per level when no explicit map value is given.
_ANTHROPIC_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
}

# Constructor-level sampling keys per api; everything else in the sampling
# dictionary is forwarded verbatim through ``model_kwargs`` (openai/anthropic)
# or dropped for google (its client has no passthrough for unknown generate
# params beyond model_kwargs, which we still use).
_OPENAI_KNOWN_SAMPLING = frozenset(
    {
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "n",
        "logprobs",
        "top_logprobs",
        "stop",
        "stop_sequences",
        "reasoning_effort",
        "max_completion_tokens",
    }
)
_ANTHROPIC_KNOWN_SAMPLING = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_tokens_to_sample",
        "stop",
        "stop_sequences",
        "thinking",
    }
)
_GOOGLE_KNOWN_SAMPLING = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_output_tokens",
        "max_tokens",
        "stop",
        "stop_sequences",
        "candidate_count",
        "n",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "thinking_budget",
        "reasoning_effort",
    }
)
# ChatGoogleGenerativeAI.reasoning_effort (alias thinking_level) is a Literal;
# anything else is omitted silently.
_GOOGLE_REASONING_LEVELS = frozenset({"minimal", "low", "medium", "high"})


def effective_compat(provider: ProviderSpec, model: ModelSpec) -> ResolvedCompat:
    """Merge model-level compat over the provider's, then resolve defaults."""

    base = provider.compat or ProviderCompat()
    override = model.compat
    if override is not None:
        base = ProviderCompat(
            supports_usage_in_streaming=(
                override.supports_usage_in_streaming
                if override.supports_usage_in_streaming is not None
                else base.supports_usage_in_streaming
            ),
            max_tokens_field=(
                override.max_tokens_field
                if override.max_tokens_field is not None
                else base.max_tokens_field
            ),
            thinking_format=(
                override.thinking_format
                if override.thinking_format is not None
                else base.thinking_format
            ),
            supports_parallel_tool_calls=(
                override.supports_parallel_tool_calls
                if override.supports_parallel_tool_calls is not None
                else base.supports_parallel_tool_calls
            ),
            extra_body=(
                override.extra_body if override.extra_body is not None else base.extra_body
            ),
        )
    return base.resolved()


def _mapped_thinking_value(level: str, model: ModelSpec) -> Any:
    """Resolve the thinking level through the model's three-state map.

    Returns ``None`` when thinking is unsupported (explicit null map or a null
    per-level entry); missing map keys fall back to the level itself.
    """

    mapping = model.thinking_level_map
    if mapping is None:
        return None
    if mapping is THINKING_MAP_UNSET:
        return level
    if isinstance(mapping, str):
        return mapping
    if isinstance(mapping, dict):
        value = mapping.get(level, THINKING_MAP_UNSET)
        if value is THINKING_MAP_UNSET:
            return level
        return value  # explicit null entry -> unsupported for this level
    return level


def translate_thinking(
    level: str, model: ModelSpec, compat: ResolvedCompat
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate a thinking level into (constructor kwargs, extra-body).

    Silent omission contract: an empty level, ``off``, an absent/unsupported
    ``thinking_format``, an explicit-null map, or a value of the wrong shape
    yields ``({}, {})`` — the endpoint never sees a thinking param.
    """

    fmt = compat.thinking_format
    if not level or level == "off" or not fmt:
        return {}, {}
    mapped = _mapped_thinking_value(level, model)
    if mapped is None:
        return {}, {}
    if fmt == "reasoning_effort":
        if isinstance(mapped, str):
            return {"reasoning_effort": mapped}, {}
        return {}, {}
    if fmt == "enable_thinking":
        flag = mapped if isinstance(mapped, bool) else True
        return {}, {"enable_thinking": flag}
    if fmt == "anthropic_thinking":
        budget = mapped if isinstance(mapped, int) and not isinstance(mapped, bool) else None
        if budget is None:
            budget = _ANTHROPIC_BUDGETS.get(level)
        if not budget:
            return {}, {}
        return {"thinking": {"type": "enabled", "budget_tokens": int(budget)}}, {}
    return {}, {}


def _merge_sampling(
    model_sampling: dict | None,
    config_sampling: dict | None,
    role_sampling: dict | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    merged.update(model_sampling or {})
    merged.update(config_sampling or {})
    merged.update(role_sampling or {})
    return merged


def _merge_headers(provider_headers: dict, model_headers: dict, role_headers: dict | None):
    headers: dict[str, str] = {}
    headers.update(provider_headers or {})
    headers.update(model_headers or {})
    headers.update(role_headers or {})
    return headers


def build_model_from_resolved(
    resolved: ResolvedModel,
    config: "V2Config",
    *,
    role_sampling: dict | None = None,
    role_headers: dict | None = None,
) -> "BaseChatModel":
    """Build the configured chat model for a resolved provider:model pair."""

    provider = resolved.provider
    model = resolved.model
    compat = effective_compat(provider, model)
    sampling = _merge_sampling(
        model.sampling_params, getattr(config, "sampling", None), role_sampling
    )
    headers = _merge_headers(provider.headers, model.headers, role_headers)
    level = str(getattr(config, "thinking", "") or "").strip().lower()
    if provider.api == API_OPENAI:
        return _build_openai(provider, model, config, sampling, headers, level, compat)
    if provider.api == API_ANTHROPIC:
        return _build_anthropic(provider, model, config, sampling, headers, level, compat)
    if provider.api == API_GOOGLE:
        return _build_google(provider, model, config, sampling, headers, level, compat)
    raise ValueError(f"unsupported provider api: {provider.api!r}")


def _transport_kwargs(config: "V2Config") -> dict[str, Any]:
    return {
        "timeout": getattr(config, "model_timeout", 180.0),
        "max_retries": getattr(config, "model_max_retries", 2),
    }


def _build_openai(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
) -> "BaseChatModel":
    from langchain_openai import ChatOpenAI

    thinking_kwargs, thinking_extra_body = translate_thinking(level, model, compat)
    extra_body = {**(compat.extra_body or {}), **thinking_extra_body}

    kwargs: dict[str, Any] = {
        "base_url": provider.base_url,
        "model": model.id,
        **_transport_kwargs(config),
    }
    if provider.api_key is not None:
        kwargs["api_key"] = provider.api_key
    if headers:
        kwargs["default_headers"] = headers
    if extra_body:
        kwargs["extra_body"] = extra_body

    model_kwargs: dict[str, Any] = {}
    max_tokens_field = compat.max_tokens_field or "max_tokens"
    for key, value in sampling.items():
        if key == "max_tokens":
            kwargs[max_tokens_field] = value
        elif key in _OPENAI_KNOWN_SAMPLING:
            kwargs[key] = value
        else:
            model_kwargs[key] = value
    # P0 #15: disable server-side parallel tool calls unless the endpoint is
    # declared unable to accept the parameter (compat override) or the
    # deployment explicitly opted out via config.
    if (not getattr(config, "parallel_tool_calls", False)) and compat.supports_parallel_tool_calls:
        model_kwargs["parallel_tool_calls"] = False
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    kwargs.update(thinking_kwargs)
    return ChatOpenAI(**kwargs)


def _build_anthropic(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
) -> "BaseChatModel":
    from langchain_anthropic import ChatAnthropic

    thinking_kwargs, _ = translate_thinking(level, model, compat)
    thinking_enabled = bool(thinking_kwargs.get("thinking"))

    kwargs: dict[str, Any] = {"model": model.id, **_transport_kwargs(config)}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    if headers:
        kwargs["default_headers"] = headers
    if model.max_tokens:
        kwargs["max_tokens"] = model.max_tokens

    model_kwargs: dict[str, Any] = {}
    for key, value in sampling.items():
        if key == "temperature" and thinking_enabled and value != 1:
            # Anthropic requires temperature omitted (or 1) when thinking is on;
            # silently drop the conflicting value rather than failing the build.
            continue
        if key in _ANTHROPIC_KNOWN_SAMPLING:
            kwargs[key] = value
        else:
            model_kwargs[key] = value
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    kwargs.update(thinking_kwargs)
    return ChatAnthropic(**kwargs)


def _build_google(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
) -> "BaseChatModel":
    from langchain_google_genai import ChatGoogleGenerativeAI

    thinking_kwargs, _ = translate_thinking(level, model, compat)
    effort = thinking_kwargs.get("reasoning_effort")
    if effort is not None and effort not in _GOOGLE_REASONING_LEVELS:
        thinking_kwargs = {}

    kwargs: dict[str, Any] = {"model": model.id, **_transport_kwargs(config)}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    if headers:
        kwargs["additional_headers"] = headers
    if model.max_tokens:
        kwargs["max_output_tokens"] = model.max_tokens

    model_kwargs: dict[str, Any] = {}
    for key, value in sampling.items():
        if key == "max_tokens":
            kwargs["max_output_tokens"] = value
        elif key in _GOOGLE_KNOWN_SAMPLING:
            kwargs[key] = value
        else:
            model_kwargs[key] = value
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    kwargs.update(thinking_kwargs)
    return ChatGoogleGenerativeAI(**kwargs)
