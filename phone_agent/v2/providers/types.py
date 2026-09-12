"""Typed dataclasses for the provider/model registry (S4).

The registry's product is a configured LangChain ``BaseChatModel``; these types
are the declarative data it consumes.  ``ProviderSpec`` aggregates one API
endpoint plus its model catalog; ``ModelSpec`` carries per-model overrides
(sampling params, thinking-level map, headers, compat); ``ProviderCompat`` is
the declarative quirk table that keeps endpoint differences out of builders.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# api values supported by builders.py (one builder per api).
API_OPENAI = "openai-completions"
API_ANTHROPIC = "anthropic-messages"
API_GOOGLE = "google-generative-ai"
SUPPORTED_APIS = (API_OPENAI, API_ANTHROPIC, API_GOOGLE)

# thinking_format values understood by builders.py.  ``None`` (absent) means
# the provider never receives a thinking param (silent omission).
THINKING_FORMAT_REASONING_EFFORT = "reasoning_effort"
THINKING_FORMAT_ENABLE_THINKING = "enable_thinking"
THINKING_FORMAT_ANTHROPIC = "anthropic_thinking"
SUPPORTED_THINKING_FORMATS = (
    THINKING_FORMAT_REASONING_EFFORT,
    THINKING_FORMAT_ENABLE_THINKING,
    THINKING_FORMAT_ANTHROPIC,
)

# Sentinel for ModelSpec.thinking_level_map: distinguishes "key absent in
# models.json" (use the default mapping) from an explicit JSON ``null``
# (model does not support thinking -> omit the param silently).
THINKING_MAP_UNSET = object()

# Legal per-role thinking levels (models.json ``roles`` section).  An absent
# or null value means "not written" (inherit the global level); unlike the
# config env var there is no empty-string level here.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high")

# Legal streaming modes (global env, model entry, role entry).
STREAMING_MODES = ("off", "on")


@dataclass(frozen=True)
class ProviderCompat:
    """Declarative endpoint quirk table (pi-style, initial subset).

    All fields are optional overrides: ``None`` inherits the built-in default
    (documented per field).  A model-level compat replaces only the fields it
    sets (see :func:`providers.builders.effective_compat`).
    """

    # Usage-in-streaming declaration; see builders.effective usage mapping.
    supports_usage_in_streaming: bool | None = None
    # Which request field carries the token cap ("max_tokens" default).
    max_tokens_field: str | None = None
    # How a thinking level is expressed on this endpoint:
    # reasoning_effort | enable_thinking | anthropic_thinking; None = never send.
    thinking_format: str | None = None
    # Whether the endpoint accepts ``parallel_tool_calls`` (default True; the
    # openai path forwards ``parallel_tool_calls=False`` from config unless this
    # is explicitly False).
    supports_parallel_tool_calls: bool | None = None
    # Verbatim extra-body escape hatch merged into the request (openai path).
    extra_body: dict | None = None

    def resolved(self) -> "ResolvedCompat":
        """Fill in the built-in defaults for every ``None`` field."""

        return ResolvedCompat(
            supports_usage_in_streaming=(
                True
                if self.supports_usage_in_streaming is None
                else self.supports_usage_in_streaming
            ),
            usage_in_streaming_declared=self.supports_usage_in_streaming,
            max_tokens_field=self.max_tokens_field or "max_tokens",
            thinking_format=self.thinking_format,
            supports_parallel_tool_calls=(
                True
                if self.supports_parallel_tool_calls is None
                else self.supports_parallel_tool_calls
            ),
            extra_body=dict(self.extra_body or {}),
        )


@dataclass(frozen=True)
class ResolvedCompat:
    """ProviderCompat with built-in defaults filled in (builder-side view)."""

    supports_usage_in_streaming: bool
    max_tokens_field: str
    thinking_format: str | None
    supports_parallel_tool_calls: bool
    extra_body: dict
    usage_in_streaming_declared: bool | None = None


@dataclass(frozen=True)
class ModelSpec:
    """One addressable model inside a provider catalog.

    ``thinking_level_map`` is the pi-style three-state field: absent
    (:data:`THINKING_MAP_UNSET`) = default mapping (level passed through /
    built-in budget table), a ``str`` = send that value verbatim, ``None``
    (explicit null in JSON) = thinking unsupported (omit silently), a ``dict``
    = per-level mapping whose missing levels fall back to the default.

    ``streaming`` is an optional ``off``/``on`` endpoint declaration (absent =
    not written).  It outranks the global ``PHONE_AGENT_STREAMING`` so a model
    whose endpoint cannot stream can stay ``off`` even when the deployment
    enables streaming globally; ``roles.<role>.streaming`` outranks it.
    """

    id: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    reasoning: bool = False
    input_modalities: tuple[str, ...] = ()
    # Free-form sampling dictionary merged verbatim (lowest precedence tier).
    sampling_params: dict = field(default_factory=dict)
    # Three-state thinking map (see class docstring).
    thinking_level_map: object = THINKING_MAP_UNSET
    streaming: str | None = None
    # Per-model header additions merged over the provider's headers.
    headers: dict = field(default_factory=dict)
    # Model-level compat overrides; only fields explicitly set replace the
    # provider's values.
    compat: ProviderCompat | None = None


@dataclass(frozen=True)
class ProviderSpec:
    """One API endpoint plus its model catalog.

    ``api`` may be ``None`` only for override-only entries (models.json
    entries that patch an existing provider's baseUrl/headers/compat without
    redeclaring its api); registering a *new* provider requires a concrete api.
    """

    id: str
    api: str | None
    base_url: str | None = None
    # Already value-resolved ($ENV interpolation happens in loader/values).
    api_key: str | None = None
    headers: dict = field(default_factory=dict)
    compat: ProviderCompat | None = None
    models: dict = field(default_factory=dict)  # id -> ModelSpec

    def __post_init__(self) -> None:
        if self.api is not None and self.api not in SUPPORTED_APIS:
            raise ValueError(
                f"provider {self.id!r}: unsupported api {self.api!r} "
                f"(expected one of {SUPPORTED_APIS})"
            )

    def model_ids(self) -> tuple[str, ...]:
        return tuple(self.models)

    def get_model(self, model_id: str) -> ModelSpec | None:
        return self.models.get(model_id)


@dataclass(frozen=True)
class RoleSpec:
    """Session-level per-role call configuration (models.json ``roles`` section).

    ``model`` is a model reference (bare name or ``provider:model``) applied
    only when the role's own env var is unset (same specificity -> env wins);
    ``sampling_params`` is the highest-precedence sampling tier
    (model entry < config env < roles); ``thinking`` overrides the global
    thinking level for this role only; ``streaming`` (``off``/``on``) is the
    most specific streaming decision, outranking both the model entry and the
    global ``PHONE_AGENT_STREAMING``.  ``None``/empty fields mean "not written"
    — the legacy chain/config behavior passes through untouched.
    """

    model: str | None = None
    sampling_params: dict = field(default_factory=dict)
    thinking: str | None = None
    streaming: str | None = None


@dataclass(frozen=True)
class ResolvedModel:
    """The registry's resolution product: a model inside its provider."""

    provider: ProviderSpec
    model: ModelSpec
    ref: str
    # True when the model id was not listed in the provider catalog and a
    # default ModelSpec was synthesized (passthrough-with-defaults).
    synthesized: bool = False
