"""Optional, model-owned context support; no universal wire protocol.

Builders still return ``BaseChatModel``. A support object may implement any of
``profile(model, tools=())``, ``estimate(model, messages, tools=())``,
``prepare(model, messages, tools=())``, ``normalize_usage(message)``, and
``protected_message_ids(model, messages)``. Helpers tolerate absent optional
operations. Preparation receives private copies and must never semantically
delete history. Only an adapter interprets protocol-specific cache fields.

No support registry, credentials, raw request logging, remote token counting,
server-side continuation, or cache-resource provisioning lives here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

_SUPPORT_ATTRIBUTE = "_taskwizard_context_support"
_CACHE_FIELDS = frozenset({"cache_control", "prompt_cache_breakpoint"})
_CACHE_PARAMETERS = frozenset(
    {
        "prompt_cache_options",
        "prompt_cache_key",
        "prompt_cache_retention",
        "cache_control",
        "cached_content",
    }
)
_DISPATCH_FIELDS = frozenset(
    {
        "model",
        "messages",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "use_responses_api",
        "previous_response_id",
        "use_previous_response_id",
        "context_management",
        "stream",
        "streaming",
        "system",
        "system_instruction",
        "contents",
        "history",
        "conversation",
        "truncation",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "response_format",
        "text",
        "generation_config",
        "stop",
        "stop_sequences",
        "temperature",
        "top_p",
        "reasoning",
        "reasoning_effort",
        "extra_headers",
        "extra_query",
        "model_kwargs",
        "top_k",
        "n",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "output_config",
        "thinking",
        "timeout",
        "max_retries",
        "headers",
        "base_url",
        "api_key",
        "prompt",
        "prompt_template",
        "system_prompt",
        "response_schema",
        "response_mime_type",
        "automatic_function_calling",
        "include",
        "store",
        "background",
        "service_tier",
        "verbosity",
        "model_name",
        "model_id",
    }
)
_SEMANTIC_PARAMETER_NAMES = frozenset(
    "".join(character for character in name.casefold() if character.isalnum())
    for name in _DISPATCH_FIELDS
)


@dataclass(frozen=True)
class ModelInputEstimate:
    tokens: int
    includes_tools: bool = False
    includes_images: bool = True
    complete: bool = False
    source: str = "heuristic"

    def __post_init__(self) -> None:
        if (
            isinstance(self.tokens, bool)
            or not isinstance(self.tokens, int)
            or self.tokens < 0
        ):
            raise ValueError("input token estimate must be a nonnegative integer")


@dataclass(frozen=True)
class ModelContextProfile:
    context_window: int | None = None
    max_output_tokens: int | None = None
    request_api: str = "unknown"
    source: str = "unknown"
    cache_mode: str = "off"

    def __post_init__(self) -> None:
        for value in (self.context_window, self.max_output_tokens):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(
                    "context/output limits must be positive integers or None"
                )


@dataclass
class PreparedModelMessages:
    messages: list[Any]
    model_kwargs: dict[str, Any] = field(default_factory=dict)


def unwrap_model(model: Any) -> tuple[Any, dict[str, Any]]:
    """Unwrap RunnableBinding without losing its invocation settings."""
    seen: set[int] = set()
    settings: dict[str, Any] = {}
    current = model
    while id(current) not in seen:
        seen.add(id(current))
        bound = getattr(current, "bound", None)
        if bound is None:
            break
        inner_settings = getattr(current, "kwargs", None)
        if isinstance(inner_settings, dict):
            settings = {**inner_settings, **settings}
        current = bound
    return current, settings


def bind_context_support(model: Any, support: Any) -> Any:
    """Bind a support object privately to the model; return the same model.

    BaseChatModel's normal ``model_copy`` carries this private attribute. Tool
    bindings are resolved by ``get_context_support``. Nothing is added to public
    metadata/model_kwargs and no process-global ownership table is required.
    """
    base, _ = unwrap_model(model)
    if hasattr(base, "__pydantic_private__"):
        private = dict(getattr(base, "__pydantic_private__", None) or {})
        private[_SUPPORT_ATTRIBUTE] = support
        object.__setattr__(base, "__pydantic_private__", private)
    else:
        setattr(base, _SUPPORT_ATTRIBUTE, support)
    return model


def get_context_support(model: Any) -> Any | None:
    base, _ = unwrap_model(model)
    private = getattr(base, "__pydantic_private__", None)
    if isinstance(private, dict) and _SUPPORT_ATTRIBUTE in private:
        return private[_SUPPORT_ATTRIBUTE]
    return getattr(base, _SUPPORT_ATTRIBUTE, None)


def model_context_profile(model: Any, tools: Any = ()) -> ModelContextProfile:
    method = getattr(get_context_support(model), "profile", None)
    if callable(method):
        try:
            result = method(model, tools=tools)
            if isinstance(result, ModelContextProfile):
                return result
        except Exception:  # noqa: BLE001 - optional diagnostics fail open
            pass
    return ModelContextProfile()


def estimate_model_input(
    model: Any, messages: Any, tools: Any = ()
) -> ModelInputEstimate:
    method = getattr(get_context_support(model), "estimate", None)
    if callable(method):
        try:
            result = method(model, deepcopy(list(messages or [])), tools=tools)
            if isinstance(result, ModelInputEstimate):
                return result
        except Exception:  # noqa: BLE001 - retain the generic estimate
            pass
    from phone_agent.v2.middleware._tokens import estimate_context_tokens

    return ModelInputEstimate(tokens=estimate_context_tokens(messages))


def _clean_content(content: Any) -> Any:
    """Remove cache metadata only, never tool argument dictionaries or text."""
    if not isinstance(content, list):
        return content
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in _CACHE_FIELDS:
            block.pop(key, None)
        extras = block.get("extras")
        if isinstance(extras, dict):
            for key in _CACHE_FIELDS:
                extras.pop(key, None)
            if not extras:
                block.pop("extras", None)
        for key in ("content", "output"):
            if isinstance(block.get(key), list):
                _clean_content(block[key])
    return content


def _without_cache(messages: Any) -> list[Any]:
    result = deepcopy(list(messages))
    for message in result:
        if hasattr(message, "content"):
            content = _clean_content(message.content)
            message.content = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else content
            )
    return result


def _cache_only_overrides(settings: dict[str, Any], support: Any) -> bool:
    # A plugin may name additional cache parameters, but cannot reclassify
    # semantic dispatch/history/output settings as cache metadata.
    declared = getattr(support, "cache_parameter_names", ())
    if not isinstance(declared, (tuple, list, set, frozenset)) or any(
        not isinstance(name, str) for name in declared
    ):
        return False

    def semantic_name(name: str) -> bool:
        return (
            "".join(c for c in name.casefold() if c.isalnum())
            in _SEMANTIC_PARAMETER_NAMES
        )

    def contains_semantic_fields(value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                (isinstance(name, str) and semantic_name(name))
                or contains_semantic_fields(item)
                for name, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(contains_semantic_fields(item) for item in value)
        return False

    allowed = frozenset(
        name
        for name in _CACHE_PARAMETERS | frozenset(declared)
        if not semantic_name(name)
    )

    def cache_parameters(values: dict[str, Any]) -> bool:
        for name, value in values.items():
            if name == "extra_body":
                if (
                    not isinstance(value, dict)
                    or "extra_body" in value
                    or not cache_parameters(value)
                ):
                    return False
            elif name not in allowed or contains_semantic_fields(value):
                return False
        return True

    return cache_parameters(settings)


def prepare_model_messages(
    model: Any, messages: Any, tools: Any = ()
) -> PreparedModelMessages:
    """Prepare a fresh attempt, stripping earlier protocol cache decorations.

    The fallback baseline is itself a copy. An optional implementation that
    raises after editing its input cannot corrupt that baseline or canonical
    graph messages. Runtime cache failure never turns into semantic compaction.
    """
    baseline = deepcopy(list(messages or []))
    support = get_context_support(model)
    method = getattr(support, "prepare", None)
    if not callable(method):
        # Legacy models and estimate-only plugins retain their own legal wire
        # metadata. New attempt decorations are never canonical in the harness.
        return PreparedModelMessages(baseline)
    for message in baseline:
        if hasattr(message, "content"):
            message.content = _clean_content(message.content)
    try:
        result = method(model, deepcopy(baseline), tools=tools)
        if (
            isinstance(result, PreparedModelMessages)
            and isinstance(result.model_kwargs, dict)
            and _cache_only_overrides(result.model_kwargs, support)
            and _without_cache(result.messages) == _without_cache(baseline)
        ):
            return result
    except Exception:  # noqa: BLE001 - optional cache preparation fails open
        pass
    return PreparedModelMessages(baseline)


def model_protected_message_ids(model: Any, messages: Any) -> frozenset[str]:
    method = getattr(get_context_support(model), "protected_message_ids", None)
    if callable(method):
        try:
            return frozenset(
                str(item) for item in method(model, deepcopy(list(messages or [])))
            )
        except Exception:  # noqa: BLE001 - generic callers also protect native blocks
            pass
    return frozenset()


def normalize_model_usage(model: Any, message: Any) -> Any | None:
    """Optional interpretation: nullable input/output/cache_read/cache_write tokens.

    Mapping keys are ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    and ``cache_write_tokens``. Consumers validate this numeric allowlist; no
    other raw provider fields should be persisted. None selects standard usage.
    """
    method = getattr(get_context_support(model), "normalize_usage", None)
    if callable(method):
        try:
            return method(deepcopy(message))
        except Exception:  # noqa: BLE001 - absent interpretation is not zero usage
            pass
    return None


__all__ = [
    "ModelContextProfile",
    "ModelInputEstimate",
    "PreparedModelMessages",
    "bind_context_support",
    "get_context_support",
    "estimate_model_input",
    "model_context_profile",
    "prepare_model_messages",
    "model_protected_message_ids",
    "normalize_model_usage",
]
