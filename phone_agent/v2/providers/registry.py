"""ProviderRegistry: registration, override, and ``provider:model`` resolution (S4).

Addressing follows the AI-SDK convention: ``"provider:model"`` selects an
explicit provider, a bare ``"model"`` selects the default provider
(``"gateway"``).  A known provider with an unknown model id resolves via
passthrough-with-defaults (gateways accept arbitrary model names); an unknown
*provider* fails visibly (:class:`UnknownProviderError`).
"""

from __future__ import annotations

import threading

from phone_agent.v2.providers.types import ModelSpec, ProviderSpec, ResolvedModel

DEFAULT_PROVIDER_ID = "gateway"


class ProviderRegistryError(Exception):
    """Base error for registry failures."""


class UnknownProviderError(ProviderRegistryError):
    """A ``provider:model`` reference named an unregistered provider."""


class ProviderRegistry:
    """Thread-safe insertion-ordered registry of :class:`ProviderSpec`."""

    def __init__(self, *, default_provider: str = DEFAULT_PROVIDER_ID) -> None:
        self._providers: dict[str, ProviderSpec] = {}
        self._default_provider = default_provider
        self._lock = threading.Lock()

    # -- mutation ----------------------------------------------------------
    def register(self, spec: ProviderSpec) -> None:
        """Register a new provider id; duplicates raise (fail-visible)."""

        if not isinstance(spec, ProviderSpec):
            raise TypeError("spec must be a ProviderSpec")
        if spec.api is None:
            raise ValueError(f"provider {spec.id!r}: a new provider requires an api")
        with self._lock:
            if spec.id in self._providers:
                raise ValueError(f"provider already registered: {spec.id}")
            self._providers[spec.id] = spec

    def override(self, spec: ProviderSpec) -> None:
        """Upsert a provider (pi merge semantics): provider-level fields are
        replaced wholesale; models are upserted by id, so a spec carrying only
        baseUrl/headers keeps the existing model catalog intact.  An override
        entry may omit ``api`` (None) to inherit the registered one."""

        if not isinstance(spec, ProviderSpec):
            raise TypeError("spec must be a ProviderSpec")
        with self._lock:
            existing = self._providers.get(spec.id)
            if existing is None:
                if spec.api is None:
                    raise ValueError(
                        f"provider {spec.id!r}: a new provider requires an api"
                    )
                self._providers[spec.id] = spec
                return
            api = spec.api if spec.api is not None else existing.api
            models = dict(existing.models)
            models.update(spec.models)
            self._providers[spec.id] = ProviderSpec(
                id=spec.id,
                api=api,
                base_url=spec.base_url if spec.base_url is not None else existing.base_url,
                api_key=spec.api_key if spec.api_key is not None else existing.api_key,
                headers={**existing.headers, **spec.headers},
                compat=spec.compat if spec.compat is not None else existing.compat,
                models=models,
            )

    def unregister(self, provider_id: str) -> bool:
        """Remove a provider; returns ``True`` when it existed."""

        with self._lock:
            return self._providers.pop(provider_id, None) is not None

    # -- resolution --------------------------------------------------------
    @property
    def default_provider(self) -> str:
        return self._default_provider

    def get(self, provider_id: str) -> ProviderSpec | None:
        with self._lock:
            return self._providers.get(provider_id)

    def resolve(self, ref: str) -> ResolvedModel:
        """Resolve ``"provider:model"`` or a bare ``"model"`` reference.

        A colon-bearing ref always addresses ``<first-segment>:<rest>``, and an
        unregistered first segment raises :class:`UnknownProviderError`.  A
        bare name resolves against the default provider; an unknown model id
        there is synthesized with defaults (passthrough-with-defaults).
        """

        clean = str(ref or "").strip()
        if not clean:
            raise ProviderRegistryError("empty model reference")
        if ":" in clean:
            provider_id, model_id = clean.split(":", 1)
            provider = self.get(provider_id)
            if provider is None:
                raise UnknownProviderError(
                    f"unknown provider in reference {clean!r}: {provider_id!r}"
                )
        else:
            provider_id = self._default_provider
            model_id = clean
            provider = self.get(provider_id)
            if provider is None:
                raise UnknownProviderError(
                    f"default provider {provider_id!r} is not registered"
                )
        # Ordinary invalid declarations remain skippable. An explicitly invalid
        # new protocol/cache choice may not become an automatic gateway call.
        errors = getattr(self, "context_selection_errors", {})
        if errors.get((provider_id, model_id)) or errors.get((provider_id, None)):
            raise ProviderRegistryError(
                f"invalid explicit context configuration for {provider_id}:{model_id}; "
                "see declaration_warnings"
            )
        model = provider.get_model(model_id)
        if model is not None:
            return ResolvedModel(provider=provider, model=model, ref=clean)
        return ResolvedModel(
            provider=provider,
            model=ModelSpec(id=model_id, name=model_id),
            ref=clean,
            synthesized=True,
        )

    # -- enumeration -------------------------------------------------------
    def list_providers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._providers)

    def list_models(self, provider_id: str) -> tuple[str, ...]:
        provider = self.get(provider_id)
        if provider is None:
            raise UnknownProviderError(f"unknown provider: {provider_id!r}")
        return provider.model_ids()
