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
behavior byte-for-byte.

Availability degradation: when a role's preferred reference cannot be built,
the role chain's next hop is tried exactly once (memory/verifier -> actor,
safety_reviewer -> verifier -> actor, distill -> memory -> actor).  The actor
additionally honors the optional ``PHONE_AGENT_FALLBACK_MODEL`` reference
(``fallback_model`` config field) at build time and once per failed actor
call; an unset, unbuildable, or same-target fallback stays inert.  Every
degradation is recorded (stage/role/requested/actual/reason) through
:func:`record_model_fallback` and never replays a completed device action.

See ``AGENTS.md`` §5 for the binding contract.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from phone_agent.v2.config import V2Config
    from phone_agent.v2.providers import ProviderRegistry

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
    custom_headers = getattr(config, "http_headers", None)
    if custom_headers:
        headers.update(custom_headers)
    headers.setdefault(
        "User-Agent", getattr(config, "user_agent", None) or DEFAULT_MODEL_USER_AGENT
    )
    client_id = getattr(config, "cf_access_client_id", None)
    client_secret = getattr(config, "cf_access_client_secret", None)
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def _build_legacy(config: "V2Config") -> "BaseChatModel":
    """Legacy single-gateway build (the pre-S4 ``build_chat_model`` body)."""

    from langchain_openai import ChatOpenAI

    sampling = dict(config.sampling or {})
    if getattr(config, "streaming", "off") == "on":
        sampling.setdefault("streaming", True)
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


def record_model_fallback(
    config: "V2Config",
    *,
    stage: str,
    role: str,
    requested: str,
    actual: str,
    reason: str,
    outcome: str = "ok",
) -> None:
    """Record one model degradation (``stage`` build/invoke).

    Before the harness attaches ``config._model_fallback_recorder`` (the run
    trace) records are stashed on ``config._model_fallbacks`` for a later
    flush; afterwards each record goes straight to the recorder.  Only model
    references, role/stage labels, the error *type*, and the outcome are
    recorded — never keys, prompts, or request/response bodies.  A bounded log
    line makes the degradation visible before the trace exists too.
    """

    payload = {
        "stage": str(stage),
        "role": str(role),
        "requested": str(requested),
        "actual": str(actual),
        "reason": str(reason),
        "outcome": str(outcome),
    }
    recorder = getattr(config, "_model_fallback_recorder", None)
    if callable(recorder):
        try:
            recorder("model_fallback", **payload)
        except Exception:  # noqa: BLE001 - audit is best-effort
            pass
    else:
        stash = getattr(config, "_model_fallbacks", None)
        if not isinstance(stash, list):
            try:
                config._model_fallbacks = []
                stash = config._model_fallbacks
            except Exception:  # noqa: BLE001 - stash is best-effort
                stash = None
        if isinstance(stash, list):
            stash.append(payload)
    logger.warning(
        "model fallback: stage=%s role=%s requested=%s actual=%s reason=%s outcome=%s",
        payload["stage"],
        payload["role"],
        payload["requested"],
        payload["actual"],
        payload["reason"],
        payload["outcome"],
    )


def _resolved_target(registry: Any, ref: str) -> tuple[str, str] | None:
    """``(provider_id, model_id)`` identity of a reference, or None if invalid."""

    try:
        resolved = registry.resolve(ref)
    except Exception:  # noqa: BLE001 - an unresolvable ref has no target
        return None
    return (str(resolved.provider.id), str(resolved.model.id))


def _remember_role_ref(config: Any, role: str, ref: str) -> None:
    """Best-effort record of the reference a role was actually built from."""

    try:
        refs = getattr(config, "_model_role_refs", None)
        if not isinstance(refs, dict):
            refs = {}
            config._model_role_refs = refs
        refs[role] = str(ref)
    except Exception:  # noqa: BLE001 - side channel is best-effort
        pass


def actual_role_ref(config: Any, role: str) -> str:
    """The reference the role's current model was built from.

    Falls back to the role resolution when the side channel is unavailable.
    """

    refs = getattr(config, "_model_role_refs", None)
    if isinstance(refs, dict) and refs.get(role):
        return str(refs[role])
    from phone_agent.v2.providers import resolve_role_ref

    return resolve_role_ref(config, role, registry=getattr(config, "_provider_registry", None))


def _chain_default_ref(config: Any, role: str) -> str:
    """The role chain's next hop after its own tier (actor has none)."""

    model_name = str(getattr(config, "model_name", "") or "")
    if role == "safety_reviewer":
        return str(getattr(config, "verifier_model", None) or model_name or "")
    if role == "distill":
        return str(getattr(config, "memory_model", None) or model_name or "")
    if role in {"memory", "verifier"}:
        return model_name
    return ""


def _same_target_log(explicit: str) -> None:
    logger.warning(
        "fallback_model %r resolves to the actor primary target; fallback disabled",
        explicit,
    )


def _select_build_fallback(
    config: Any, *, role: str, registry: Any, primary_ref: str
) -> tuple[str, "BaseChatModel"] | None:
    """Pick the one build-time degradation target for a failed role build.

    The actor uses the explicit ``fallback_model`` reference; other roles use
    the next hop of their legacy chain.  A missing, identical, same-target, or
    unbuildable fallback returns None so the primary error propagates.
    """

    if role == "actor":
        explicit = str(getattr(config, "fallback_model", None) or "").strip()
        if not explicit:
            return None
        if explicit == primary_ref:
            _same_target_log(explicit)
            return None
        primary_target = _resolved_target(registry, primary_ref)
        if primary_target is not None and _resolved_target(registry, explicit) == primary_target:
            _same_target_log(explicit)
            return None
        try:
            return explicit, _build_ref_via_registry(
                config,
                ref=explicit,
                registry=registry,
                role=role,
                apply_role_specs=False,
            )
        except Exception as exc:  # noqa: BLE001 - primary error propagates
            logger.warning(
                "fallback_model %r is not buildable (%s); fallback disabled",
                explicit,
                type(exc).__name__,
            )
            return None
    fallback_ref = _chain_default_ref(config, role)
    if not fallback_ref or fallback_ref == primary_ref:
        return None
    primary_target = _resolved_target(registry, primary_ref)
    if primary_target is not None and _resolved_target(registry, fallback_ref) == primary_target:
        return None
    try:
        return fallback_ref, _build_ref_via_registry(
            config,
            ref=fallback_ref,
            registry=registry,
            role=role,
            apply_role_specs=False,
        )
    except Exception:  # noqa: BLE001 - primary error propagates
        return None


def _build_ref_via_registry(
    config: "V2Config",
    *,
    ref: str,
    role: str,
    registry: "ProviderRegistry",
    apply_role_specs: bool = True,
) -> "BaseChatModel":
    """Build one explicit reference through the role's registry path.

    With ``apply_role_specs`` the models.json ``roles`` section (P2) applies:
    the role's ``sampling_params`` becomes the highest-precedence
    ``role_sampling`` tier, its ``thinking`` overrides the global level, and
    its ``streaming`` becomes the strongest streaming tier for this role only.
    Fallback builds skip those primary-specific tiers so a heterogeneous
    backup is built from its own ModelSpec metadata plus the necessary global
    config (sampling, thinking level, streaming, headers, compat).
    """

    from phone_agent.v2.providers import build_model_from_resolved
    from phone_agent.v2.providers.roles import get_role_specs

    resolved = registry.resolve(ref)
    spec = get_role_specs(registry).get(role) if apply_role_specs else None
    if spec is not None:
        active = _ConfigThinkingView(config, spec.thinking) if spec.thinking else config
        return build_model_from_resolved(
            resolved,
            active,
            role_sampling=spec.sampling_params or None,
            role_streaming=spec.streaming,
        )
    return build_model_from_resolved(resolved, config)


def _build_role_model_with_fallback(
    config: "V2Config", *, role: str, registry: "ProviderRegistry"
) -> "BaseChatModel":
    """Registry-path role build with one recorded degradation on failure."""

    from phone_agent.v2.providers import resolve_role_ref

    primary_ref = resolve_role_ref(config, role, registry=registry)
    try:
        model = _build_ref_via_registry(
            config, ref=primary_ref, registry=registry, role=role
        )
    except Exception as exc:
        selected = _select_build_fallback(
            config, role=role, registry=registry, primary_ref=primary_ref
        )
        if selected is None:
            raise
        fallback_ref, model = selected
        _remember_role_ref(config, role, fallback_ref)
        record_model_fallback(
            config,
            stage="build",
            role=role,
            requested=primary_ref,
            actual=fallback_ref,
            reason=type(exc).__name__,
        )
        return model
    _remember_role_ref(config, role, primary_ref)
    return model


def streaming_inactive_models(
    fallback: tuple[Any, str] | None,
) -> tuple[Any, ...]:
    """Models the web observer must never instrument.

    An availability fallback built from its own model/global configuration can
    resolve streaming to ``off``; forcing a stream observer onto it would
    override that decision, so it is reported as inactive.
    """

    if not fallback:
        return ()
    model = fallback[0]
    return () if bool(getattr(model, "streaming", False)) else (model,)


def build_actor_fallback(
    config: "V2Config",
    *,
    registry: "ProviderRegistry | None" = None,
    primary_ref: str | None = None,
) -> tuple["BaseChatModel", str] | None:
    """Build the actor's one-shot call fallback, or None when not available.

    The single source is the optional ``fallback_model`` reference (legal
    ``provider:model`` or a bare model name on the existing default gateway).
    Dedup uses the actor's **actual** built reference (``primary_ref``, the
    build-degraded reference when a build fallback already happened), so the
    same target is never called twice under a primary-to-fallback label.
    Unset, equal to the actual primary reference/target, or unbuildable
    returns None — no endpoint, key, or catalog entry is ever guessed.
    """

    explicit = str(getattr(config, "fallback_model", None) or "").strip()
    if not explicit:
        return None
    if registry is None:
        registry = getattr(config, "_provider_registry", None)
    if registry is None:
        return None
    if primary_ref is None:
        primary_ref = actual_role_ref(config, "actor")
    if explicit == primary_ref:
        _same_target_log(explicit)
        return None
    primary_target = _resolved_target(registry, primary_ref)
    if primary_target is not None and _resolved_target(registry, explicit) == primary_target:
        _same_target_log(explicit)
        return None
    try:
        model = _build_ref_via_registry(
            config,
            ref=explicit,
            registry=registry,
            role="actor",
            apply_role_specs=False,
        )
    except Exception as exc:  # noqa: BLE001 - no fallback available
        logger.warning(
            "fallback_model %r is not buildable (%s); fallback disabled",
            explicit,
            type(exc).__name__,
        )
        return None
    return model, explicit


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
    A build failure degrades once to the role chain's next hop when one
    exists (recorded through :func:`record_model_fallback`); with no usable
    fallback the primary error propagates. The legacy path remains only for
    callers that supply no provider registry.
    """

    if registry is None:
        registry = getattr(config, "_provider_registry", None)
    if registry is None:
        from phone_agent.v2.providers import build_provider_registry

        registry = build_provider_registry(config)
    if registry is not None:
        return _build_role_model_with_fallback(config, role=role, registry=registry)
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
    ``roles`` section (P2) applies its per-role sampling/thinking overrides.
    A failed build degrades once to the role chain's next hop (actor: the
    configured ``fallback_model``) when one exists and is distinct; otherwise
    the primary error propagates.

    Sampling params (temperature/top_p/frequency_penalty) are forwarded as-is;
    the gateway + tool_calls + image_url content blocks are all verified compatible
    by the langchain compat spike.
    """

    if registry is None:
        return _build_legacy(config)
    return _build_role_model_with_fallback(config, role=role, registry=registry)
