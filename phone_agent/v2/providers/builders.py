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
* streaming: the effective ``off``/``on`` decision (role > model entry >
  global env) is forwarded as the transport's own ``streaming`` parameter on
  all three supported apis, so the SDK streams and aggregates the complete
  message by itself (headless included).  ``off`` emits no parameter, keeping
  the default build unchanged.  ``supports_usage_in_streaming`` is a separate
  *usage-reporting* declaration (see below) and never a streaming switch.
* usage-in-streaming: an **explicit** ``ProviderCompat.supports_usage_in_streaming``
  (models.json ``supportsUsageInStreaming``) is forwarded as ``stream_usage`` on
  the openai and anthropic transports, where it only has a wire effect on a
  streaming request (``stream_options.include_usage`` / streaming-event usage
  capture); an absent field emits nothing so the zero-config build keeps the
  legacy/SDK default.  The google protocol has no equivalent request option, so
  the declaration is deliberately not translated there.

``parallel_tool_calls=False`` stays an openai-path-only default (P0 #15): the
thin loop is one-observation-one-action, and only the ChatOpenAI transport
forwards it via ``model_kwargs`` so it survives every ``bind_tools`` re-bind.

DSH-style transport registry (WP-PROVIDER2 / P1): the ``api`` declared by a
provider selects a *registered builder function* rather than a hardcoded
branch.  The three built-in transports register themselves at import time;
plugins may add custom transports (or atomically replace a built-in with
``override=True``) via :func:`register_api_builder` and dispose them via
:func:`unregister_api_builder`.  The registry is an assembly-time facility
(plugin mount / models.json assembly) — it is thread-safe but intentionally
not a hot-swap point for a running agent.  A successful registration also
extends the declarative api family (``types.SUPPORTED_APIS``, append-only) so
providers may *declare* the custom api; the authoritative gate stays the
dispatch lookup — an ``api`` without a live registered builder fails closed
with the list of registered types, and there is no silent fallback to another
transport.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

from phone_agent.v2.providers.roles import streaming_mode_from_tiers
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
    from typing import Protocol

    from phone_agent.v2.config import V2Config

    class ApiBuilder(Protocol):
        """A registered transport builder (DSH ``LlmAdapter`` analogue).

        The product must be a LangChain ``BaseChatModel``; the keyword-only
        transport inputs are the merged results computed by
        :func:`build_model_from_resolved` (sampling: modelSpec < config < role;
        headers: provider -> model -> role; ``level``: the global thinking
        level; ``compat``: the resolved per-model compatibility overrides).
        """

        def __call__(
            self,
            provider: ProviderSpec,
            model: ModelSpec,
            config: V2Config,
            *,
            sampling: dict[str, Any],
            headers: dict[str, str],
            level: str,
            compat: ResolvedCompat,
            streaming: bool,
        ) -> "BaseChatModel": ...

logger = logging.getLogger(__name__)

# api_type grammar: lowercase letters/digits/hyphens, starting and ending
# alphanumeric (internal hyphens allowed).
_API_TYPE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# DSH-style transport registry: ``api`` -> builder.  Guarded by a lock so
# registration from plugin ``apply`` hooks is safe, but deliberately a module
# singleton — assembly-time facility, not a per-run or hot-swap surface.
_API_BUILDERS: dict[str, "ApiBuilder"] = {}
_API_BUILDERS_LOCK = threading.Lock()

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


@cache
def _anthropic_client_type():
    """Lazily define the native client with tail-SystemMessage support."""

    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import SystemMessage

    class TailSystemChatAnthropic(ChatAnthropic):
        """Native Anthropic client accepting harness tail SystemMessages."""

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            messages = self._convert_input(input_).to_messages()
            system = [
                message for message in messages if isinstance(message, SystemMessage)
            ]
            if system and messages[: len(system)] != system:
                messages = [
                    *system,
                    *(
                        message
                        for message in messages
                        if not isinstance(message, SystemMessage)
                    ),
                ]
            return super()._get_request_payload(messages, stop=stop, **kwargs)

    return TailSystemChatAnthropic


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
            request_api=(
                override.request_api if override.request_api is not None else base.request_api
            ),
            cache_policy=(
                override.cache_policy if override.cache_policy is not None else base.cache_policy
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


def _validate_api_type(api_type: str) -> str:
    if not isinstance(api_type, str):
        raise ValueError(
            f"invalid api type: {api_type!r} "
            "(expected lowercase letters/digits/hyphens, starting alphanumeric)"
        )
    clean = api_type.strip()
    if not _API_TYPE_PATTERN.fullmatch(clean):
        raise ValueError(
            f"invalid api type: {api_type!r} "
            "(expected lowercase letters/digits/hyphens, starting alphanumeric)"
        )
    return clean


def _extend_declared_api_family(api_type: str) -> None:
    """Make the declarative layer accept ``api_type`` (types.SUPPORTED_APIS).

    ``ProviderSpec.__post_init__`` validates a provider's declared ``api``
    against the module-global ``SUPPORTED_APIS`` tuple in
    ``phone_agent.v2.providers.types``.  A custom transport must be declarable
    (models.json ``api`` field / ``ProviderSpec(api=...)``) to be usable, so a
    successful registration atomically rebinds that module attribute with the
    api type appended (append-only family; rebinding a tuple is atomic for
    concurrent readers).  The authoritative build-time gate stays the
    fail-closed dispatch lookup in :func:`build_model_from_resolved`: a
    declared api whose builder was disposed still fails visibly there, with
    the live registered-type listing.
    """

    import phone_agent.v2.providers.types as types_mod

    family = types_mod.SUPPORTED_APIS
    if api_type not in family:
        types_mod.SUPPORTED_APIS = (*family, api_type)


def register_api_builder(api_type: str, fn: "ApiBuilder", *, override: bool = False) -> None:
    """Register the transport builder for ``api_type`` (DSH ``registerAdapter``).

    This is the plugin face for custom transport protocols: the ``api`` a
    provider declares (models.json ``api`` field / ``ProviderSpec.api``) is
    dispatched to the registered builder by :func:`build_model_from_resolved`.

    Semantics:

    * ``api_type`` must match the lowercase/digit/hyphen grammar and ``fn``
      must follow the :class:`ApiBuilder` signature.
    * Registering an already-registered api type raises unless
      ``override=True`` — that flag is the atomic replace (same lock-held
      dict write as a fresh registration).  Built-in transports
      (``openai-completions`` / ``anthropic-messages`` / ``google-generative-ai``)
      therefore require an explicit ``override=True`` to be replaced.
    * The declarative api family (``types.SUPPORTED_APIS``) gains the type so
      providers may declare it; the family is append-only and never shrinks,
      while disposal removes only the builder — building then fails closed at
      dispatch (see :func:`unregister_api_builder`).
    * Assembly-time facility: call from a plugin ``apply`` hook or models.json
      assembly, never to hot-swap a transport under a running agent.

    Dispose with :func:`unregister_api_builder`.
    """

    clean = _validate_api_type(api_type)
    if not callable(fn):
        raise TypeError("api builder must be callable")
    with _API_BUILDERS_LOCK:
        if clean in _API_BUILDERS and not override:
            raise ValueError(
                f"api builder already registered for {clean!r} "
                "(pass override=True to replace it atomically)"
            )
        _API_BUILDERS[clean] = fn
        _extend_declared_api_family(clean)


def unregister_api_builder(api_type: str) -> bool:
    """Dispose a registered transport builder (DSH adapter dispose).

    Idempotent no-op semantics: removing an unknown registration (or an
    untouched built-in) returns ``False`` and changes nothing.  If the type is
    a *built-in that was overridden*, the built-in default is restored.
    Returns ``True`` only when a custom registration was removed or a
    built-in default was restored.

    The declarative api family (``types.SUPPORTED_APIS``) is append-only and
    keeps the type: providers may still *declare* the api, but every build
    attempt now fails closed at dispatch with the registered-type listing.
    """

    clean = _validate_api_type(api_type)
    with _API_BUILDERS_LOCK:
        current = _API_BUILDERS.get(clean)
        if current is None:
            return False
        builtin = _BUILTIN_API_BUILDERS.get(clean)
        if builtin is not None:
            if current is builtin:
                return False
            _API_BUILDERS[clean] = builtin
            return True
        del _API_BUILDERS[clean]
        return True


def get_api_builder(api_type: str) -> "ApiBuilder | None":
    """Return the registered builder for ``api_type`` (built-ins included)."""

    with _API_BUILDERS_LOCK:
        return _API_BUILDERS.get(api_type)


def registered_api_types() -> tuple[str, ...]:
    """Return the currently registered api types in registration order."""

    with _API_BUILDERS_LOCK:
        return tuple(_API_BUILDERS)


# Built-in transport defaults: populated by the ``_builtin_api`` decorator on
# the three ``_build_*`` definitions below.  Never removable — ``override``
# replaces them and ``unregister_api_builder`` restores them.
_BUILTIN_API_BUILDERS: dict[str, "ApiBuilder"] = {}


def build_model_from_resolved(
    resolved: ResolvedModel,
    config: "V2Config",
    *,
    role_sampling: dict | None = None,
    role_headers: dict | None = None,
    role_streaming: str | None = None,
) -> "BaseChatModel":
    """Build the configured chat model for a resolved provider:model pair.

    ``streaming`` is resolved from the three documented tiers (role > model
    entry > global env) and translated into the transport's own streaming
    parameter, so the model streams and aggregates by itself; the web observer
    only listens.  An ``off`` decision emits no parameter at all (the default
    build stays byte-for-byte identical to the legacy client).
    """

    provider = resolved.provider
    model = resolved.model
    builder = get_api_builder(provider.api)
    if builder is None:
        registered = ", ".join(registered_api_types()) or "(none)"
        raise ValueError(
            f"no api builder registered for {provider.api!r} "
            f"(provider {provider.id!r}); registered api types: {registered}"
        )
    compat = effective_compat(provider, model)
    sampling = _merge_sampling(
        model.sampling_params, getattr(config, "sampling", None), role_sampling
    )
    headers = _merge_headers(provider.headers, model.headers, role_headers)
    level = str(getattr(config, "thinking", "") or "").strip().lower()
    streaming = (
        streaming_mode_from_tiers(
            global_mode=getattr(config, "streaming", ""),
            model_mode=model.streaming,
            role_mode=role_streaming,
        )
        == "on"
    )
    built = builder(
        provider,
        model,
        config,
        sampling=sampling,
        headers=headers,
        level=level,
        compat=compat,
        **({STREAMING_KWARG: streaming} if builder_accepts_streaming(builder) else {}),
    )
    return built


def _transport_kwargs(config: "V2Config") -> dict[str, Any]:
    return {
        "timeout": getattr(config, "model_timeout", 180.0),
        "max_retries": getattr(config, "model_max_retries", 2),
    }


def _streaming_kwargs(streaming: bool) -> dict[str, Any]:
    """Transport-level streaming switch (nothing emitted when off)."""

    return {"streaming": True} if streaming else {}


def builder_accepts_streaming(builder: "ApiBuilder") -> bool:
    """Whether a registered builder takes the ``streaming`` transport input.

    External/plugin transports registered against the earlier two-argument
    shape keep working: the decision is then simply not forwarded to them.
    """

    try:
        params = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        return True
    if "streaming" in params:
        return True
    return any(param.kind is param.VAR_KEYWORD for param in params.values())


STREAMING_KWARG = "streaming"


def _builtin_api(api_type: str):
    """Mark a private builder as the immutable default for ``api_type``."""

    def deco(fn):
        _BUILTIN_API_BUILDERS[api_type] = fn
        return fn

    return deco


@_builtin_api(API_OPENAI)
def _build_openai(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    *,
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
    streaming: bool = False,
) -> "BaseChatModel":
    from langchain_openai import ChatOpenAI
    from phone_agent.v2.providers._context import BuiltinContextSupport
    from phone_agent.v2.providers.context import bind_context_support

    sampling = dict(sampling)
    request_api_kwargs: dict[str, Any] = {}
    if compat.request_api_declared is not None:
        selected = {"auto": None, "chat": False, "responses": True}[compat.request_api]
        if "use_responses_api" in sampling and sampling["use_responses_api"] is not selected:
            raise ValueError("compat.requestApi conflicts with samplingParams.use_responses_api")
        sampling.pop("use_responses_api", None)
        if selected is not None:
            request_api_kwargs["use_responses_api"] = selected
    selected_flag = request_api_kwargs.get("use_responses_api", sampling.get("use_responses_api"))
    if selected_flag is False and (
        any(sampling.get(key) is not None for key in ("context_management", "include", "reasoning", "truncation"))
        or sampling.get("use_previous_response_id")
        or sampling.get("output_version") == "responses/v1"
    ):
        raise ValueError("explicit Chat protocol conflicts with Responses-only settings")
    if compat.cache_policy == "stable-prefix" and selected_flag is not True:
        raise ValueError("OpenAI stable-prefix caching requires explicit requestApi=responses")
    if compat.cache_policy == "stable-prefix" and any(
        key in sampling or key in (compat.extra_body or {})
        for key in ("prompt_cache_options", "cache_control")
    ):
        raise ValueError("cachePolicy conflicts with explicit native cache options")
    if compat.cache_policy == "stable-prefix" and (
        sampling.get("use_previous_response_id")
        or "previous_response_id" in sampling
        or "previous_response_id" in (compat.extra_body or {})
    ):
        raise ValueError("stable-prefix cache preparation requires full client-owned history")

    thinking_kwargs, thinking_extra_body = translate_thinking(level, model, compat)
    extra_body = {**(compat.extra_body or {}), **thinking_extra_body}

    kwargs: dict[str, Any] = {
        "base_url": provider.base_url,
        "model": model.id,
        **_transport_kwargs(config),
        **_streaming_kwargs(streaming),
        **request_api_kwargs,
    }
    if compat.usage_in_streaming_declared is not None:
        kwargs["stream_usage"] = bool(compat.usage_in_streaming_declared)
    if provider.api_key is not None:
        kwargs["api_key"] = provider.api_key
    if headers:
        kwargs["default_headers"] = headers
    if extra_body:
        kwargs["extra_body"] = extra_body

    model_kwargs: dict[str, Any] = {}
    max_tokens_field = compat.max_tokens_field or "max_tokens"
    sampling_token_fields = {"max_tokens", "max_completion_tokens", max_tokens_field}
    if model.max_tokens is not None and not sampling_token_fields.intersection(sampling):
        kwargs[max_tokens_field] = model.max_tokens
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
    return bind_context_support(
        ChatOpenAI(**kwargs), BuiltinContextSupport(API_OPENAI, model, compat.cache_policy)
    )


@_builtin_api(API_ANTHROPIC)
def _build_anthropic(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    *,
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
    streaming: bool = False,
) -> "BaseChatModel":
    from phone_agent.v2.providers._context import BuiltinContextSupport
    from phone_agent.v2.providers.context import bind_context_support

    if compat.request_api_declared is not None:
        raise ValueError("compat.requestApi is only supported by the OpenAI-family builder")
    if compat.cache_policy == "stable-prefix" and (
        "cache_control" in sampling or "cache_control" in (compat.extra_body or {})
    ):
        raise ValueError("cachePolicy conflicts with explicit native cache options")
    thinking_kwargs, _ = translate_thinking(level, model, compat)
    thinking_enabled = bool(thinking_kwargs.get("thinking"))

    kwargs: dict[str, Any] = {
        "model": model.id,
        **_transport_kwargs(config),
        **_streaming_kwargs(streaming),
    }
    if compat.usage_in_streaming_declared is not None:
        kwargs["stream_usage"] = bool(compat.usage_in_streaming_declared)
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
    return bind_context_support(
        _anthropic_client_type()(**kwargs), BuiltinContextSupport(API_ANTHROPIC, model, compat.cache_policy)
    )


@_builtin_api(API_GOOGLE)
def _build_google(
    provider: ProviderSpec,
    model: ModelSpec,
    config: "V2Config",
    *,
    sampling: dict[str, Any],
    headers: dict[str, str],
    level: str,
    compat: ResolvedCompat,
    streaming: bool = False,
) -> "BaseChatModel":
    from langchain_google_genai import ChatGoogleGenerativeAI
    from phone_agent.v2.providers._context import BuiltinContextSupport
    from phone_agent.v2.providers.context import bind_context_support

    if compat.request_api_declared is not None:
        raise ValueError("compat.requestApi is only supported by the OpenAI-family builder")
    if compat.cache_policy != "off":
        raise ValueError("Google stable-prefix cache resources are not implemented")

    thinking_kwargs, _ = translate_thinking(level, model, compat)
    effort = thinking_kwargs.get("reasoning_effort")
    if effort is not None and effort not in _GOOGLE_REASONING_LEVELS:
        thinking_kwargs = {}

    kwargs: dict[str, Any] = {
        "model": model.id,
        **_transport_kwargs(config),
        **_streaming_kwargs(streaming),
    }
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
    return bind_context_support(
        ChatGoogleGenerativeAI(**kwargs), BuiltinContextSupport(API_GOOGLE, model)
    )


# Import-time registration of the three built-in transports as registry
# defaults (DSH: the core ships default adapters).
with _API_BUILDERS_LOCK:
    _API_BUILDERS.update(_BUILTIN_API_BUILDERS)
