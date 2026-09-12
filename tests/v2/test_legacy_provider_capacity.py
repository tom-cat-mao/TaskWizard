"""Declared limits follow old custom builders through actual dispatch."""

from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import pytest

from phone_agent.v2.agent import _WrapModelBridgeMiddleware
from phone_agent.v2.events import EventBus
from phone_agent.v2.middleware.context_request import ContextCapacityError, ContextRequestObserver
from phone_agent.v2.providers import (
    ModelContextProfile, ModelInputEstimate, ModelSpec, ProviderSpec, ResolvedModel,
    bind_context_support, build_model_from_resolved, estimate_model_input,
    get_context_support, model_context_profile, prepare_model_messages,
    register_api_builder, unregister_api_builder,
)

API = "legacy-capacity-test"


class LegacyModel(BaseChatModel):
    calls: int = 0
    fail: bool = False

    @property
    def _llm_type(self):
        return "synthetic-legacy-capacity"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic primary failure")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def settings(**kwargs):
    return SimpleNamespace(
        model_name="legacy-primary", sampling={}, thinking="", streaming="off",
        context_window=kwargs.get("window"), compact_schema_reserve=0,
        compact_output_reserve=0,
    )


@pytest.fixture
def factory():
    products = {}

    # No new support or streaming parameter is required from this old builder.
    def builder(provider, model, config, *, sampling, headers, level, compat):
        return products.setdefault(model.id, LegacyModel())

    register_api_builder(API, builder)

    def build(name="model", window=1000):
        spec = ModelSpec(id=name, context_window=window, max_tokens=99999)
        provider = ProviderSpec(id="custom", api=API, models={name: spec})
        return build_model_from_resolved(ResolvedModel(provider, spec, f"custom:{name}"), settings())

    yield build
    unregister_api_builder(API)


def test_legacy_declaration_survives_copy_binding_without_claiming_output_cap(factory):
    model = factory()
    for target in (model, model.model_copy(), model.bind_tools([])):
        profile = model_context_profile(target)
        assert profile.context_window == 1000
        assert profile.source == "model_declaration"
        assert profile.request_api == "unknown"
        assert profile.max_output_tokens is None
        assert get_context_support(target) is None
    assert "declared_context" not in str(model.model_dump())
    message = HumanMessage(content=[{
        "type": "text", "text": "legacy manual cache",
        "cache_control": {"type": "ephemeral"},
    }])
    assert prepare_model_messages(model, [message]).messages == [message]


def test_legacy_primary_known_capacity_is_not_treated_as_unknown(factory):
    model = factory()
    observer = ContextRequestObserver(settings(window=50000))
    with pytest.raises(ContextCapacityError):
        observer.prepare(ModelRequest(model=model, messages=[HumanMessage(content="字" * 2500)]))
    assert model.calls == 0


def test_legacy_small_fallback_is_rejected_before_its_model_call(factory):
    primary = factory("primary", 10000)
    primary.fail = True
    backup = factory("backup", 1000)
    bridge = _WrapModelBridgeMiddleware(
        EventBus(), config=settings(window=50000),
        fallback_provider=lambda: (backup, "custom:backup"),
    )
    with pytest.raises(RuntimeError, match="primary failure") as caught:
        bridge.wrap_model_call(
            ModelRequest(model=primary, messages=[HumanMessage(content="字" * 2500)]),
            lambda request: request.model.invoke(request.messages),
        )
    assert isinstance(caught.value.__cause__, ContextCapacityError)
    assert primary.calls == 1
    assert backup.calls == 0


@pytest.mark.parametrize("profile_behavior", ["absent", "missing_window", "failure", "stricter"])
def test_declaration_does_not_replace_partial_plugin_support(factory, profile_behavior):
    class Support:
        def estimate(self, model, messages, tools=()):
            return ModelInputEstimate(17, includes_tools=True, complete=True)

    support = Support()
    if profile_behavior == "missing_window":
        support.profile = lambda model, **kwargs: ModelContextProfile(request_api="custom")
    elif profile_behavior == "failure":
        def broken(*args, **kwargs):
            raise RuntimeError("optional profile unavailable")
        support.profile = broken
    elif profile_behavior == "stricter":
        support.profile = lambda model, **kwargs: ModelContextProfile(context_window=500, source="plugin")
    model = bind_context_support(factory(), support)
    assert get_context_support(model) is support
    assert estimate_model_input(model, []).tokens == 17
    assert model_context_profile(model).context_window == (500 if profile_behavior == "stricter" else 1000)


def test_undeclared_legacy_capacity_remains_unknown(factory):
    model = factory(window=None)
    assert model_context_profile(model).context_window is None
    observer = ContextRequestObserver(settings())
    observer.prepare(ModelRequest(model=model, messages=[HumanMessage(content="字" * 2500)]))


@pytest.mark.parametrize("compact", [False, True])
def test_real_thin_agent_retains_legacy_window_at_final_admission(tmp_path, monkeypatch, compact):
    """Exercise bootstrap, real capabilities and final dispatch, not just helpers."""
    from phone_agent.v2.agent import ThinPhoneAgent
    from phone_agent.v2.capabilities import CapabilitySpec
    from phone_agent.v2.config import V2Config
    from phone_agent.v2.providers import register_provider
    from phone_agent.v2.session import Observation, PhoneSession
    from tests.v2._doubles import FakeDeviceFactory

    device = FakeDeviceFactory()
    monkeypatch.setattr("phone_agent.v2.session.get_device_factory", lambda: device)

    def observe(session, **kwargs):
        session.screen_seq += 1
        session.epoch += 1
        return Observation(
            "U1lOVEhFVElD", 800, 1200, "com.synthetic.app", [],
            session.screen_seq, epoch=session.epoch,
        )

    monkeypatch.setattr(PhoneSession, "observe", observe)
    model = LegacyModel()

    def builder(provider, spec, config, *, sampling, headers, level, compat):
        return model

    def apply(ctx):
        register_api_builder(API, builder)
        ctx.on_dispose(lambda: unregister_api_builder(API))
        register_provider(ctx, ProviderSpec(
            id="legacy", api=API, models={"actor": ModelSpec(id="actor", context_window=1000)},
        ))

    declaration = tmp_path / "models.json"
    declaration.write_text('{"providers": {}}')
    config = V2Config(
        base_url="https://synthetic.invalid/v1", model_name="legacy:actor", models_file=str(declaration),
        memory_dir=str(tmp_path / "memory"), trace_dir=str(tmp_path / "trace"),
        experience_enabled=False, memory_rag="off", app_kb_enabled=False,
        resolver_embed=False, deliverable_enabled=False, compact_enabled=compact,
        context_window=None, safety_mode="off", observe_settle_ms=0,
    )
    try:
        agent = ThinPhoneAgent(config, extra_capabilities=[CapabilitySpec(
            "legacy_provider", "Synthetic legacy provider", "on", deps=("providers",), apply=apply,
        )])
        with pytest.raises(ContextCapacityError, match="capacity=1000"):
            agent.run("Read the synthetic result")
        assert model.calls == 0
    finally:
        unregister_api_builder(API)
