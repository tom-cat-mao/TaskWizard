"""Provider/Model registry package (S4).

Public surface:

* :class:`ProviderRegistry` / :func:`build_provider_registry` — the registry
  and its config-driven assembly (built-in gateway + models.json).
* :func:`build_model_from_resolved` — api -> BaseChatModel builders.
* :func:`register_api_builder` / :func:`unregister_api_builder` /
  :func:`registered_api_types` — the DSH-style transport registry: custom
  ``api`` -> builder mappings (or atomic built-in replacement), callable from
  a plugin ``apply`` hook just like :func:`register_provider`.
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
    get_api_builder,
    register_api_builder,
    registered_api_types,
    translate_thinking,
    unregister_api_builder,
)
from phone_agent.v2.providers.loader import (
    DeclarationWarning,
    ModelsFileError,
    apply_provider_declarations,
    build_provider_registry,
    candidate_paths,
    load_raw_document,
    load_raw_document_lenient,
    load_raw_file,
    parse_models_document,
    parse_models_document_lenient,
    parse_models_json,
)
from phone_agent.v2.providers.registry import (
    DEFAULT_PROVIDER_ID,
    ProviderRegistry,
    ProviderRegistryError,
    UnknownProviderError,
)
from phone_agent.v2.providers.roles import (
    ROLES,
    get_role_specs,
    resolve_role_ref,
    resolve_streaming_enabled,
    resolve_streaming_mode,
)
from phone_agent.v2.providers.types import (
    STREAMING_MODES,
    THINKING_LEVELS,
    ModelSpec,
    ProviderCompat,
    ProviderSpec,
    ResolvedModel,
    RoleSpec,
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
    the merged model catalog. Models are resolved at build time; a plugin can
    contribute the actor provider as well as auxiliary-role providers when
    its capability declares
    ``deps=("providers",)``; the agent bootstraps those provider contributors
    before constructing the actor.
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
    "DeclarationWarning",
    "ModelsFileError",
    "ModelSpec",
    "ProviderCompat",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderSpec",
    "ROLES",
    "STREAMING_MODES",
    "ResolvedModel",
    "UnknownProviderError",
    "apply_provider_declarations",
    "build_model_from_resolved",
    "build_provider_registry",
    "candidate_paths",
    "effective_compat",
    "get_api_builder",
    "get_role_specs",
    "load_raw_document",
    "load_raw_document_lenient",
    "load_raw_file",
    "parse_models_document",
    "parse_models_document_lenient",
    "parse_models_json",
    "register_api_builder",
    "register_provider",
    "registered_api_types",
    "resolve_role_ref",
    "resolve_streaming_enabled",
    "resolve_streaming_mode",
    "RoleSpec",
    "THINKING_LEVELS",
    "translate_thinking",
    "unregister_api_builder",
]
