"""LLM role resolution for the five v2 call sites (S4/P2).

Resolution order per role (owner rule: more targeted wins; env beats the
file at equal specificity):

1. the role's own env tier (``PHONE_AGENT_MEMORY_MODEL`` etc., surfaced as
   ``config.<role>_model``) — for ``actor`` this is ``PHONE_AGENT_MODEL``
   (``config.model_name``), which is always set, so ``roles.actor.model``
   cannot displace it;
2. ``roles.<role>.model`` from the models.json ``roles`` section (read off
   the registry the loader attached it to);
3. the legacy fallback chain, unchanged:

* ``actor``          <- ``PHONE_AGENT_MODEL`` (config.model_name)
* ``memory``         <- ``PHONE_AGENT_MEMORY_MODEL`` -> actor
* ``verifier``       <- ``PHONE_AGENT_VERIFIER_MODEL`` -> actor
* ``safety_reviewer``<- ``PHONE_AGENT_SAFETY_REVIEWER_MODEL`` -> verifier -> actor
* ``distill``        <- ``roles.distill.model`` -> memory -> actor (no dedicated
  env var, so the file tier outranks the generic memory fallback)

Each resolved value is a model *reference*: a bare model name (default
provider) or ``provider:model`` addressing.  ``registry=None`` (or a registry
without attached roles) reproduces the pre-P2 env/chain behavior exactly.

The same resolution feeds the optional streaming decision
(:func:`resolve_streaming_enabled`): a role's effective *model* is looked up in
the registry so a models.json streaming declaration applies to the endpoint
that will actually be called.
"""

from __future__ import annotations

from typing import Any

from phone_agent.v2.providers.types import RoleSpec, STREAMING_MODES

ROLES = ("actor", "memory", "verifier", "safety_reviewer", "distill")


def get_role_specs(registry: Any) -> dict[str, RoleSpec]:
    """Return the ``roles`` section attached by the loader (empty if none).

    Registries built elsewhere (hand-constructed in tests/plugins) carry no
    ``roles`` attribute; they resolve to an empty table instead of failing.
    """

    roles = getattr(registry, "roles", None)
    return roles if isinstance(roles, dict) else {}


def resolve_role_ref(config: Any, role: str, registry: Any = None) -> str:
    """Resolve one role to its model reference (bare name or provider:model).

    Precedence: role env > ``roles.<role>.model`` > legacy fallback chain
    (see module docstring).
    """

    model_name = str(getattr(config, "model_name", "") or "")
    spec = get_role_specs(registry).get(role)
    role_model = str(getattr(spec, "model", None) or "").strip()
    if role == "actor":
        # config.model_name IS the actor env tier and always set, so the
        # roles file tier only surfaces for a degenerate empty model_name.
        ref = model_name or role_model
    elif role == "memory":
        ref = getattr(config, "memory_model", None) or role_model or model_name
    elif role == "verifier":
        ref = getattr(config, "verifier_model", None) or role_model or model_name
    elif role == "safety_reviewer":
        ref = (
            getattr(config, "safety_reviewer_model", None)
            or role_model
            or getattr(config, "verifier_model", None)
            or model_name
        )
    elif role == "distill":
        ref = role_model or getattr(config, "memory_model", None) or model_name
    else:
        raise ValueError(f"unknown role: {role!r} (expected one of {ROLES})")
    return str(ref or "")


def _clean_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in STREAMING_MODES else ""


def streaming_mode_from_tiers(
    *, global_mode: Any, model_mode: Any, role_mode: Any
) -> str:
    """Combine the three streaming tiers (``off``/``on``)."""

    return (
        _clean_mode(role_mode)
        or _clean_mode(model_mode)
        or _clean_mode(global_mode)
        or "off"
    )


def resolve_streaming_mode(
    config: Any,
    role: str,
    registry: Any = None,
    *,
    resolved: Any = None,
) -> str:
    """Resolve one role's effective streaming mode (``off``/``on``).

    Precedence (most specific wins):

    1. ``roles.<role>.streaming`` — the per-role call decision;
    2. the resolved model entry's ``streaming`` — an endpoint capability fact
       that outranks the deployment-wide switch;
    3. ``config.streaming`` (``PHONE_AGENT_STREAMING``, default off).

    ``resolved`` may carry an already-resolved :class:`ResolvedModel` (the
    build path passes the model it actually built, including a degraded
    fallback); otherwise the role reference is resolved through the registry.
    An unresolvable reference (missing registry / unknown provider) simply
    contributes no model tier — the decision never raises during assembly.
    """

    model_mode = ""
    try:
        target = resolved
        if target is None and registry is not None:
            target = registry.resolve(resolve_role_ref(config, role, registry=registry))
        model_mode = getattr(getattr(target, "model", None), "streaming", None)
    except Exception:  # noqa: BLE001 - an unresolvable ref contributes no tier
        model_mode = ""
    spec = get_role_specs(registry).get(role)
    return streaming_mode_from_tiers(
        global_mode=getattr(config, "streaming", ""),
        model_mode=model_mode,
        role_mode=getattr(spec, "streaming", None) if spec else None,
    )


def resolve_streaming_enabled(
    config: Any,
    role: str,
    registry: Any = None,
    *,
    resolved: Any = None,
) -> bool:
    """Boolean view of :func:`resolve_streaming_mode` (default ``False``)."""

    return resolve_streaming_mode(config, role, registry, resolved=resolved) == "on"
