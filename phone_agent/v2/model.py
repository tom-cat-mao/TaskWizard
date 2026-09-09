"""v2 model layer: ChatOpenAI factory + default request headers.

Mirrors the verified ``scripts/spike_langchain_compat.py`` setup: an
OpenAI-compatible gateway reached through ``langchain_openai.ChatOpenAI`` with
browser-style UA + optional CF Access headers and sampling params forwarded from
:class:`~phone_agent.v2.config.V2Config`.

S4 adds a role-based path on top: ``build_chat_model(config, *, role=...,
registry=...)`` resolves the role's model reference through a
:class:`~phone_agent.v2.providers.ProviderRegistry` (``provider:model``
addressing, models.json catalogs, thinking levels) and builds the matching
LangChain chat model.  ``registry=None`` reproduces the legacy single-gateway
behavior byte-for-byte, and every registry failure fails open to that same
legacy path — the provider layer never crashes a run.

See ``AGENTS.md`` §5 for the binding contract.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from phone_agent.v2.config import V2Config
    from phone_agent.v2.providers import ProviderRegistry

logger = logging.getLogger(__name__)

DEFAULT_MODEL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 TaskWizard/0.1"
)


def build_default_headers(config: "V2Config") -> dict[str, str]:
    """Build gateway request headers: UA + custom headers + CF Access pair.

    Custom ``http_headers`` are applied first; ``User-Agent`` falls back to the
    configured value or the browser-style default constant. The CF Access pair is
    only emitted when both id and secret are present (config validated the pairing).
    """

    headers: dict[str, str] = {}
    if config.http_headers:
        headers.update(config.http_headers)
    headers.setdefault("User-Agent", config.user_agent or DEFAULT_MODEL_USER_AGENT)
    if config.cf_access_client_id and config.cf_access_client_secret:
        headers["CF-Access-Client-Id"] = config.cf_access_client_id
        headers["CF-Access-Client-Secret"] = config.cf_access_client_secret
    return headers


def _build_legacy(config: "V2Config") -> "BaseChatModel":
    """Legacy single-gateway build (the pre-S4 ``build_chat_model`` body)."""

    from langchain_openai import ChatOpenAI

    sampling = dict(config.sampling or {})
    # U1: disable server-side parallel tool calls. create_agent re-binds tools
    # each turn via ``model.bind_tools(...)`` without passing this flag, so we set
    # it as a model default (model_kwargs) that survives every re-bind. The thin
    # loop is strictly one-observation-one-action; a parallel batch would address
    # marks the first action already invalidated (batch-badge freshness gate).
    model_kwargs: dict[str, object] = {}
    if not getattr(config, "parallel_tool_calls", False):
        model_kwargs["parallel_tool_calls"] = False
    return ChatOpenAI(
        base_url=config.base_url,
        model=config.model_name,
        api_key=config.api_key,
        timeout=config.model_timeout,
        max_retries=config.model_max_retries,
        default_headers=build_default_headers(config),
        model_kwargs=model_kwargs,
        **sampling,
    )


def _legacy_role_build(config: "V2Config", role: str) -> "BaseChatModel":
    """Legacy build with the role's model name substituted in.

    Routed through the module-level ``build_chat_model`` global so historical
    monkeypatches (tests, third parties) keep intercepting role builds exactly
    as they did before S4.
    """

    from phone_agent.v2.providers.roles import resolve_role_ref

    ref = resolve_role_ref(config, role)
    active = (
        config
        if ref == getattr(config, "model_name", None)
        else replace(config, model_name=ref)
    )
    return build_chat_model(active)


class _ConfigThinkingView:
    """Read-only config view with only ``thinking`` overridden (P2 roles).

    Lets the registry build path apply ``roles.<role>.thinking`` without
    mutating or requiring a dataclass copy of the caller's config: every
    other attribute (sampling, transport defaults, ...) passes through to
    the inner config untouched.
    """

    def __init__(self, inner: "V2Config", thinking: str) -> None:
        self._inner = inner
        self._thinking = thinking

    @property
    def thinking(self) -> str:
        return self._thinking

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _build_via_registry(config: "V2Config", *, role: str, registry: "ProviderRegistry") -> "BaseChatModel":
    """Registry build path shared by build_chat_model/build_role_model.

    Applies the models.json ``roles`` section (P2): ``roles.<role>.model``
    feeds reference resolution (via ``resolve_role_ref``), the role's
    ``sampling_params`` becomes the highest-precedence ``role_sampling`` tier,
    and its ``thinking`` overrides the global level for this role only.
    """

    from phone_agent.v2.providers import build_model_from_resolved, resolve_role_ref
    from phone_agent.v2.providers.roles import get_role_specs

    ref = resolve_role_ref(config, role, registry=registry)
    resolved = registry.resolve(ref)
    spec = get_role_specs(registry).get(role)
    active = config
    if spec is not None:
        if spec.thinking:
            active = _ConfigThinkingView(config, spec.thinking)
        return build_model_from_resolved(
            resolved,
            active,
            role_sampling=spec.sampling_params or None,
        )
    return build_model_from_resolved(resolved, active)


def build_role_model(
    config: "V2Config",
    *,
    role: str,
    registry: "ProviderRegistry | None" = None,
) -> "BaseChatModel":
    """Build the chat model for one LLM role (actor/memory/verifier/...).

    Registry resolution order: the explicit ``registry`` argument, then the
    ``_provider_registry`` side channel the harness leaves on config after
    assembling the actor model, then a lazily assembled registry from config
    (covers CLI paths such as ``--distill`` that never construct an agent).
    When the registry carries a models.json ``roles`` section (P2), the
    role's file entry applies: model reference (env tier still wins),
    sampling params (highest tier), and per-role thinking override.
    Every registry/models.json failure fails open to the legacy
    single-gateway path with the role's model name substituted.
    """

    if registry is None:
        registry = getattr(config, "_provider_registry", None)
    if registry is None:
        from phone_agent.v2.providers import build_provider_registry

        registry = build_provider_registry(config)
    if registry is not None:
        try:
            return _build_via_registry(config, role=role, registry=registry)
        except Exception as exc:  # noqa: BLE001 - provider layer fails open
            logger.warning(
                "provider registry path failed for role %s (%s); "
                "falling back to the legacy gateway build",
                role,
                exc,
            )
    return _legacy_role_build(config, role)


def build_chat_model(
    config: "V2Config",
    *,
    role: str = "actor",
    registry: "ProviderRegistry | None" = None,
) -> "BaseChatModel":
    """Construct the chat client for the configured gateway.

    Signature compatibility (S4): ``build_chat_model(config)`` — the historical
    call shape — still reproduces today's exact ChatOpenAI output (UA/CF
    headers, sampling forwarding, ``parallel_tool_calls=False`` default).
    Passing a ``registry`` switches to the role-based provider path: the role's
    reference (bare model name or ``provider:model``) is resolved against the
    registry and built through the matching api builder; the models.json
    ``roles`` section (P2) applies its per-role sampling/thinking overrides;
    any failure degrades to the legacy path with the role's model name
    substituted.

    Sampling params (temperature/top_p/frequency_penalty) are forwarded as-is;
    the gateway + tool_calls + image_url content blocks are all verified compatible
    by the langchain compat spike.
    """

    if registry is None:
        return _build_legacy(config)
    try:
        return _build_via_registry(config, role=role, registry=registry)
    except Exception as exc:  # noqa: BLE001 - provider layer fails open
        logger.warning(
            "provider registry path failed for role %s (%s); "
            "falling back to the legacy gateway build",
            role,
            exc,
        )
        return _legacy_role_build(config, role)
