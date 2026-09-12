"""WP-PROVIDER2 (P1): DSH-style api-builder transport registry.

Covers registration/disposal semantics (register / unregister / override /
restore), api_type shape validation, fail-closed dispatch for unregistered
api types (with the registered-type listing), an end-to-end custom transport
through models.json -> ProviderRegistry -> build_model_from_resolved, the
plugin face via the providers capability ctx, and simple thread-safety.
"""

from __future__ import annotations

import json
import threading

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.config import V2Config
from phone_agent.v2.providers import (
    ModelSpec,
    ProviderSpec,
    ResolvedModel,
    build_model_from_resolved,
    build_provider_registry,
    get_api_builder,
    register_api_builder,
    registered_api_types,
    unregister_api_builder,
)
from phone_agent.v2.providers import builders as builders_mod

BUILTIN_TYPES = (
    "openai-completions",
    "anthropic-messages",
    "google-generative-ai",
)


class FakeChatModel(BaseChatModel):
    """Minimal BaseChatModel stand-in so the build path is exercised for real."""

    marker: str = "fake"

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.marker))]
        )


def _builder_factory(calls: list | None = None):
    def builder(provider, model, config, *, sampling, headers, level, compat):
        if calls is not None:
            calls.append(
                {
                    "provider_id": provider.id,
                    "model_id": model.id,
                    "sampling": dict(sampling),
                    "headers": dict(headers),
                    "level": level,
                    "compat": compat,
                }
            )
        return FakeChatModel(marker=f"{provider.id}:{model.id}")

    return builder


@pytest.fixture
def registry_guard():
    """Snapshot/restore the process-global builder table around each test."""

    snapshot = dict(builders_mod._API_BUILDERS)
    yield
    builders_mod._API_BUILDERS.clear()
    builders_mod._API_BUILDERS.update(snapshot)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _config(tmp_path, **overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        trace_dir=str(tmp_path / "traces"),
        trace_enabled=False,
        experience_enabled=False,
        thinking="",
        sampling=None,
    )
    defaults.update(overrides)
    return V2Config(**defaults)


def _ctx(config, **services):
    payload = {"config": config}
    payload.update(services)
    return CapabilityAssemblyContext(payload)


def _provider(api: str, model_id: str = "m1") -> ProviderSpec:
    return ProviderSpec(
        id="acme",
        api=api,
        base_url="https://acme.example",
        api_key="k",
        models={model_id: ModelSpec(id=model_id)},
    )


def _resolve_build(provider: ProviderSpec, config, model_id: str = "m1"):
    model = provider.get_model(model_id) or ModelSpec(id=model_id)
    resolved = ResolvedModel(
        provider=provider, model=model, ref=f"{provider.id}:{model_id}"
    )
    return build_model_from_resolved(resolved, config)


# -- built-in defaults ------------------------------------------------------


def test_builtin_transports_registered_at_import(registry_guard):
    assert set(BUILTIN_TYPES).issubset(set(registered_api_types()))
    for api in BUILTIN_TYPES:
        assert get_api_builder(api) is builders_mod._BUILTIN_API_BUILDERS[api]


# -- shape validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "OpenAI", "a_b", "a b", "-lead", "trail-", None, 42],
)
def test_register_rejects_invalid_api_type_shape(registry_guard, bad):
    with pytest.raises(ValueError, match="invalid api type"):
        register_api_builder(bad, _builder_factory())


@pytest.mark.parametrize("good", ["acme-transport", "acme2", "a", "a-b-c-42"])
def test_register_accepts_valid_api_type_shape(registry_guard, good):
    register_api_builder(good, _builder_factory())
    assert get_api_builder(good) is not None
    assert unregister_api_builder(good) is True


def test_register_rejects_non_callable(registry_guard):
    with pytest.raises(TypeError, match="callable"):
        register_api_builder("acme-transport", "not-callable")


# -- registration / replace / dispose semantics -----------------------------


def test_duplicate_registration_requires_override(registry_guard):
    first, second = _builder_factory(), _builder_factory()
    register_api_builder("acme-transport", first)
    with pytest.raises(ValueError, match="override=True"):
        register_api_builder("acme-transport", second)
    # override=True is the atomic replace (DSH registerAdapter semantics).
    register_api_builder("acme-transport", second, override=True)
    assert get_api_builder("acme-transport") is second


def test_override_builtin_requires_explicit_flag(registry_guard):
    fake = _builder_factory()
    with pytest.raises(ValueError, match="override=True"):
        register_api_builder("openai-completions", fake)
    assert get_api_builder("openai-completions") is not fake  # untouched


def test_unregister_semantics_noop_idempotent(registry_guard):
    # Unknown registration: no-op, returns False, never raises.
    assert unregister_api_builder("acme-transport") is False
    # Untouched built-in: nothing to remove, default stays live.
    builtin = get_api_builder("openai-completions")
    assert unregister_api_builder("openai-completions") is False
    assert get_api_builder("openai-completions") is builtin
    # Custom registration: removed; second call is a no-op again.
    register_api_builder("acme-transport", _builder_factory())
    assert unregister_api_builder("acme-transport") is True
    assert unregister_api_builder("acme-transport") is False
    assert get_api_builder("acme-transport") is None


def test_unregister_overridden_builtin_restores_default(registry_guard):
    fake = _builder_factory()
    register_api_builder("openai-completions", fake, override=True)
    assert get_api_builder("openai-completions") is fake
    assert unregister_api_builder("openai-completions") is True
    assert get_api_builder("openai-completions") is (
        builders_mod._BUILTIN_API_BUILDERS["openai-completions"]
    )


# -- build path: two visible gates (declarative family, dispatch) ------------


def test_never_declared_api_rejected_at_spec_level():
    # An api that was never registered is rejected when a provider declares
    # it — the declarative gate (types.SUPPORTED_APIS: built-ins + every type
    # ever registered in this process).
    with pytest.raises(ValueError, match="unsupported api"):
        _provider("never-declared-transport")


def test_declared_api_without_builder_fails_closed_listing_registered(
    isolated, registry_guard
):
    config = _config(isolated)
    # Register -> declare -> dispose: the spec still parses (append-only
    # declarative family) but the build fails closed at dispatch, listing the
    # live registered types.  No silent fallback to another transport.
    register_api_builder("disposed-transport", _builder_factory())
    provider = _provider("disposed-transport")
    assert unregister_api_builder("disposed-transport") is True
    with pytest.raises(ValueError) as excinfo:
        _resolve_build(provider, config)
    message = str(excinfo.value)
    assert "no api builder registered for 'disposed-transport'" in message
    assert "registered api types" in message
    for api in BUILTIN_TYPES:
        assert api in message  # the visible listing


def test_registered_custom_api_builds_end_to_end_from_models_json(
    isolated, registry_guard
):
    calls: list = []
    register_api_builder("acme-transport", _builder_factory(calls))
    (isolated / ".taskwizard.models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "acme": {
                        "api": "acme-transport",
                        "baseUrl": "https://acme.example",
                        "apiKey": "k",
                        "headers": {"X-Provider": "p"},
                        "models": [{"id": "m1", "name": "M1"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _config(isolated, sampling={"temperature": 0.5})
    registry = build_provider_registry(config)
    assert registry is not None
    resolved = registry.resolve("acme:m1")
    built = build_model_from_resolved(resolved, config)
    assert isinstance(built, FakeChatModel)
    assert built.marker == "acme:m1"
    # The builder received the standard merged transport inputs.
    assert len(calls) == 1
    call = calls[0]
    assert call["provider_id"] == "acme"
    assert call["model_id"] == "m1"
    assert call["sampling"]["temperature"] == 0.5
    assert call["headers"]["X-Provider"] == "p"
    assert call["compat"] is not None


def test_custom_api_unregistered_after_override_restores_builtin_build(
    isolated, registry_guard
):
    config = _config(isolated)
    provider = _provider("openai-completions")
    baseline = _resolve_build(provider, config)
    from langchain_openai import ChatOpenAI

    assert isinstance(baseline, ChatOpenAI)

    fake = _builder_factory()
    register_api_builder("openai-completions", fake, override=True)
    overridden = _resolve_build(provider, config)
    assert isinstance(overridden, FakeChatModel)

    assert unregister_api_builder("openai-completions") is True
    restored = _resolve_build(provider, config)
    assert isinstance(restored, ChatOpenAI)


# -- plugin face through the providers capability ---------------------------


def test_plugin_apply_registers_custom_transport_via_capability_ctx(
    isolated, registry_guard
):
    config = _config(isolated)
    ctx = _ctx(config)
    assemble_capabilities(build_capability_registry(config), ctx)
    registry = ctx.service("provider_registry")
    assert registry is not None

    # What a plugin.py apply(ctx) hook does.
    register_api_builder("plugin-transport", _builder_factory())
    registry.override(
        ProviderSpec(
            id="plug",
            api="plugin-transport",
            base_url="https://plug.example",
            api_key="k",
            models={"m": ModelSpec(id="m")},
        )
    )

    built = build_model_from_resolved(registry.resolve("plug:m"), config)
    assert isinstance(built, FakeChatModel)
    assert built.marker == "plug:m"

    # Dispose: the transport disappears and the build fails closed again.
    assert unregister_api_builder("plugin-transport") is True
    with pytest.raises(ValueError, match="plugin-transport"):
        build_model_from_resolved(registry.resolve("plug:m"), config)


# -- thread safety (assembly-time; simple consistency check) ----------------


def test_concurrent_register_unregister_leaves_registry_consistent(registry_guard):
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        api_type = f"thread-{index}"
        try:
            barrier.wait()
            for _ in range(50):
                register_api_builder(api_type, _builder_factory())
                unregister_api_builder(api_type)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    # Every worker's type is fully disposed; built-ins remain intact.
    for index in range(8):
        assert unregister_api_builder(f"thread-{index}") is False
    assert set(BUILTIN_TYPES).issubset(set(registered_api_types()))


def test_concurrent_override_and_restore_builtin_stays_buildable(
    isolated, registry_guard
):
    fake = _builder_factory()
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def toggler() -> None:
        try:
            barrier.wait()
            for _ in range(50):
                register_api_builder("openai-completions", fake, override=True)
                unregister_api_builder("openai-completions")
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(exc)

    def observer() -> None:
        try:
            barrier.wait()
            for _ in range(50):
                current = get_api_builder("openai-completions")
                assert current is not None  # never a hole between swap states
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(exc)

    threads = [threading.Thread(target=toggler), threading.Thread(target=observer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    # Final state is legal either way and the built-in default is restorable.
    assert unregister_api_builder("openai-completions") in {True, False}
    assert get_api_builder("openai-completions") is (
        builders_mod._BUILTIN_API_BUILDERS["openai-completions"]
    )
