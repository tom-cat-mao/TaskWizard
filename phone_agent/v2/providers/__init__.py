"""Provider/Model registry package (S4).

Public surface:

* :class:`ProviderRegistry` / :func:`build_provider_registry` — the registry
  and its config-driven assembly (built-in gateway + models.json).
* :func:`build_model_from_resolved` — api -> BaseChatModel builders.
* :func:`resolve_role_ref` — the five LLM roles' fallback chains.
* :func:`register_provider` — the plugin-facing helper for adding a provider
  through the capability context.

Zero-config guarantee: with no models.json present, the synthesized gateway
provider reproduces the legacy ``build_chat_model`` output byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from phone_agent.v2.providers.builders import (
    build_model_from_resolved,
    effective_compat,
    translate_thinking,
)
from phone_agent.v2.providers.loader import (
    ModelsFileError,
    build_provider_registry,
    candidate_paths,
    load_raw_file,
    parse_models_json,
)
from phone_agent.v2.providers.registry import (
    DEFAULT_PROVIDER_ID,
    ProviderRegistry,
    ProviderRegistryError,
    UnknownProviderError,
)
from phone_agent.v2.providers.roles import ROLES, resolve_role_ref
from phone_agent.v2.providers.types import (
    ModelSpec,
    ProviderCompat,
    ProviderSpec,
    ResolvedModel,
)


def register_provider(ctx: Any, spec: ProviderSpec) -> None:
    """Plugin-facing helper: register an extra provider into the run's registry.

    Intended to be called from a plugin's capability ``apply`` hook with the
    assembly context::

        # plugin.py
        from phone_agent.v2.providers import ProviderSpec, register_provider

        def apply(ctx):
            register_provider(ctx, ProviderSpec(id="acme", api="openai-completions", ...))

    Trust level: loading a plugin (entry point ``taskwizard.capabilities`` or a
    ``profile.toml`` ``plugin add`` entry) IS execution authorization — the
    same pip-install trust level as the rest of the WP-PLUGIN system.  Any
    code that can call this helper can rewrite model routing for every role
    built afterwards, so only install plugins you trust.

    Semantics: the registry is resolved from the ``provider_registry`` ctx
    service (mounted by the built-in ``providers`` capability); a missing
    service is built lazily from the ctx ``config`` and registered under the
    calling capability so release removes it.  Registration is an upsert:
    re-registering an existing provider id overrides its fields while keeping
    the merged model catalog.  Models are resolved lazily at build time, but
    note the actor model is constructed before plugins apply — route the
    actor via models.json, plugin providers serve the auxiliary roles
    (memory/verifier/safety_reviewer/distill) built after assembly.
    """

    registry = ctx.service("provider_registry")
    if registry is None:
        from phone_agent.v2.providers.loader import build_provider_registry

        registry = build_provider_registry(ctx.service("config"))
        if registry is None:
            raise RuntimeError(
                "register_provider: no provider registry available "
                "(config-driven assembly failed; check models.json)"
            )
        try:
            ctx.register_service("provider_registry", registry)
        except RuntimeError:
            # Called outside an active capability apply hook (e.g. tests or
            # console tooling): publish without ownership tracking.
            ctx.set_service("provider_registry", registry)
    registry.override(spec)


__all__ = [
    "DEFAULT_PROVIDER_ID",
    "ModelsFileError",
    "ModelSpec",
    "ProviderCompat",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderSpec",
    "ROLES",
    "ResolvedModel",
    "UnknownProviderError",
    "build_model_from_resolved",
    "build_provider_registry",
    "candidate_paths",
    "effective_compat",
    "load_raw_file",
    "parse_models_json",
    "register_provider",
    "resolve_role_ref",
    "translate_thinking",
]
