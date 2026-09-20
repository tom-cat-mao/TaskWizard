"""S3 regressions for provider configuration reaching model calls.

All payload checks use the installed LangChain client converters directly; no
HTTP request, device access, memory store, or embedder is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from phone_agent.v2.capabilities import CapabilitySpec
from phone_agent.v2.config import V2Config
from phone_agent.v2.middleware.compact import build_compact_middleware
from phone_agent.v2.middleware.safety import build_safety_reviewer
from phone_agent.v2.model import build_chat_model
from phone_agent.v2.providers import (
    ModelSpec,
    ProviderSpec,
    UnknownProviderError,
    build_model_from_resolved,
    build_provider_registry,
    register_api_builder,
    unregister_api_builder,
)
from phone_agent.v2.providers.types import ResolvedModel


class _FakeChatModel(BaseChatModel):
    marker: str = "fake"

    @property
    def _llm_type(self) -> str:
        return "s3-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.marker))]
        )


def _config(**overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="test-key",
        model_timeout=3.0,
        model_max_retries=2,
        memory_model=None,
        verifier_model=None,
        safety_reviewer_model=None,
        models_file=None,
        context_window=None,
        sampling=None,
        parallel_tool_calls=False,
        thinking="",
    )
    defaults.update(overrides)
    return V2Config(**defaults)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_models(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_openai_model_max_tokens_reaches_real_wire_payload():
    model = ModelSpec(id="gpt-test", max_tokens=777)
    provider = ProviderSpec(
        id="openai",
        api="openai-completions",
        api_key="test-key",
        models={model.id: model},
    )
    client = build_model_from_resolved(
        ResolvedModel(provider, model, "openai:gpt-test"), _config()
    )

    payload = client._get_request_payload([HumanMessage(content="hello")])

    assert payload["max_completion_tokens"] == 777
    assert client.max_tokens == 777


def test_openai_sampling_token_limit_overrides_model_declaration():
    model = ModelSpec(id="gpt-test", max_tokens=777)
    provider = ProviderSpec(
        id="openai",
        api="openai-completions",
        api_key="test-key",
        models={model.id: model},
    )
    client = build_model_from_resolved(
        ResolvedModel(provider, model, "openai:gpt-test"),
        _config(sampling={"max_completion_tokens": 888}),
    )

    payload = client._get_request_payload([HumanMessage(content="hello")])
    assert payload["max_completion_tokens"] == 888


def test_effective_actor_context_window_drives_compact_unless_explicit(isolated):
    _write_models(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "gateway": {
                    "models": [{"id": "main-model", "contextWindow": 32768}]
                }
            }
        },
    )
    cfg = _config()
    cfg._provider_registry = build_provider_registry(cfg)

    compact = build_compact_middleware(SimpleNamespace(), cfg)
    assert compact.window == 32768

    explicit = _config(context_window=64000)
    explicit._provider_registry = build_provider_registry(explicit)
    assert build_compact_middleware(SimpleNamespace(), explicit).window == 64000


def test_roles_safety_reviewer_model_is_checked_at_reviewer_call_site(
    isolated, monkeypatch
):
    _write_models(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "review": {
                    "api": "openai-completions",
                    "apiKey": "test-key",
                    "models": [{"id": "guard"}],
                }
            },
            "roles": {"safety_reviewer": {"model": "review:guard"}},
        },
    )
    cfg = _config()
    cfg._provider_registry = build_provider_registry(cfg)
    calls = []

    class Reviewer:
        def invoke(self, messages):
            calls.append(messages)
            return AIMessage(content="REVERSIBLE")

    import phone_agent.v2.model as model_module

    monkeypatch.setattr(
        model_module,
        "build_role_model",
        lambda config, *, role, registry=None: (
            calls.append((role, registry)) or Reviewer()
        ),
    )
    reviewer = build_safety_reviewer(cfg)

    assert reviewer is not None
    assert reviewer("tap", "支付方式") is True
    assert calls[0] == ("safety_reviewer", cfg._provider_registry)


def test_native_anthropic_payload_lifts_tail_system_messages_in_order():
    model = ModelSpec(id="claude-test", max_tokens=512)
    provider = ProviderSpec(
        id="anthropic",
        api="anthropic-messages",
        api_key="test-key",
        models={model.id: model},
    )
    client = build_model_from_resolved(
        ResolvedModel(provider, model, "anthropic:claude-test"), _config()
    )
    messages = [
        SystemMessage(content="root"),
        HumanMessage(content="step one"),
        AIMessage(content="done"),
        SystemMessage(content="[TASK_DOC] tail"),
        SystemMessage(content="[RECALL] tail"),
        SystemMessage(content="[TOKEN_BUDGET] tail"),
        SystemMessage(content="[COMPACT_SUMMARY] tail"),
        HumanMessage(content="continue"),
    ]

    payload = client._get_request_payload(messages)

    assert [block["text"] for block in payload["system"]] == [
        "root",
        "[TASK_DOC] tail",
        "[RECALL] tail",
        "[TOKEN_BUDGET] tail",
        "[COMPACT_SUMMARY] tail",
    ]
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]


def test_unknown_provider_without_fallback_still_fails_visibly(isolated):
    cfg = _config(model_name="missing:actor")
    registry = build_provider_registry(cfg)

    with pytest.raises(UnknownProviderError, match="missing"):
        build_chat_model(cfg, role="actor", registry=registry)

    recovered = build_chat_model(
        _config(model_name="missing:actor", fallback_model="gateway:main-model"),
        role="actor",
        registry=registry,
    )
    assert recovered.model_name == "main-model"


def test_invalid_explicit_models_file_degrades_with_warning(isolated):
    path = isolated / "models.json"
    path.write_text("{broken", encoding="utf-8")

    registry = build_provider_registry(_config(models_file=str(path)))

    assert registry.list_providers() == ("gateway",)
    assert any("malformed JSON" in w.error for w in registry.declaration_warnings)


def test_missing_explicit_models_file_warns_but_missing_default_is_valid(
    isolated,
):
    missing = isolated / "missing-models.json"

    registry = build_provider_registry(_config(models_file=str(missing)))
    assert registry.list_providers() == ("gateway",)
    assert any("does not exist" in w.error for w in registry.declaration_warnings)

    registry = build_provider_registry(_config())
    assert registry.list_providers() == ("gateway",)
    assert registry.declaration_warnings == []


def test_declared_transport_without_live_builder_does_not_fallback(isolated):
    api = "s3-disposed-transport"
    register_api_builder(api, lambda *args, **kwargs: _FakeChatModel())
    try:
        provider = ProviderSpec(
            id="custom",
            api=api,
            models={"actor": ModelSpec(id="actor")},
        )
    finally:
        assert unregister_api_builder(api) is True

    cfg = _config(model_name="custom:actor")
    registry = build_provider_registry(cfg)
    registry.register(provider)

    with pytest.raises(ValueError, match="no api builder registered"):
        build_chat_model(cfg, role="actor", registry=registry)


def test_external_provider_transport_applies_before_actor_construction(
    isolated, monkeypatch
):
    api = "s3-bootstrap-transport"
    built = []
    applied = []

    def transport(provider, model, config, **kwargs):
        built.append((provider.id, model.id))
        return _FakeChatModel(marker=f"{provider.id}:{model.id}")

    _write_models(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "plugin": {
                    "api": api,
                    "models": [
                        {"id": "actor", "contextWindow": 48000}
                    ],
                }
            }
        },
    )

    def apply(ctx):
        applied.append(ctx)
        register_api_builder(api, transport)

    plugin = CapabilitySpec(
        "provider_plugin",
        "Provider plugin",
        "on",
        deps=("providers",),
        apply=apply,
    )

    import phone_agent.v2.session as session_module
    import phone_agent.v2.tools as tools_module

    fake_session = SimpleNamespace(event_bus=None, usage_ledger=None)
    monkeypatch.setattr(session_module, "PhoneSession", lambda config: fake_session)
    monkeypatch.setattr(tools_module, "build_base_tools", lambda session, config: [])

    cfg = _config(
        model_name="plugin:actor",
        safety_mode="off",
        compact_enabled=True,
        taskdoc_enabled=False,
        memory_rag="off",
        experience_enabled=False,
        trace_enabled=False,
        deliverable_enabled=False,
    )
    try:
        from phone_agent.v2.agent import ThinPhoneAgent

        agent = ThinPhoneAgent(cfg, extra_capabilities=[plugin])
        assert isinstance(agent.model, _FakeChatModel)
        assert built == [("plugin", "actor")]
        assert len(applied) == 1
        assert agent._compact.window == 48000
    finally:
        unregister_api_builder(api)


def test_provider_bootstrap_applies_required_helper_before_contributor(
    isolated, monkeypatch
):
    api = "s3-helper-transport"
    order = []

    def helper_apply(ctx):
        order.append("helper")
        register_api_builder(
            api,
            lambda provider, model, config, **kwargs: _FakeChatModel(
                marker=f"{provider.id}:{model.id}"
            ),
        )

    def provider_apply(ctx):
        order.append("provider")

    _write_models(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "plugin": {
                    "api": api,
                    "models": [{"id": "actor"}],
                }
            }
        },
    )
    contributor = CapabilitySpec(
        "provider_contributor",
        "Provider contributor",
        "on",
        deps=("providers", "transport_helper"),
        apply=provider_apply,
    )
    helper = CapabilitySpec(
        "transport_helper", "Transport helper", "on", apply=helper_apply
    )

    import phone_agent.v2.session as session_module
    import phone_agent.v2.tools as tools_module

    fake_session = SimpleNamespace(event_bus=None, usage_ledger=None)
    monkeypatch.setattr(session_module, "PhoneSession", lambda config: fake_session)
    monkeypatch.setattr(tools_module, "build_base_tools", lambda session, config: [])
    cfg = _config(
        model_name="plugin:actor",
        safety_mode="off",
        compact_enabled=False,
        taskdoc_enabled=False,
        memory_rag="off",
        experience_enabled=False,
        trace_enabled=False,
        deliverable_enabled=False,
    )
    try:
        from phone_agent.v2.agent import ThinPhoneAgent

        agent = ThinPhoneAgent(
            cfg, extra_capabilities=[contributor, helper]
        )
        assert isinstance(agent.model, _FakeChatModel)
        assert order == ["helper", "provider"]
    finally:
        unregister_api_builder(api)


def test_provider_bootstrap_missing_helper_fails_before_actor(isolated):
    contributor = CapabilitySpec(
        "provider_contributor",
        "Provider contributor",
        "on",
        deps=("providers", "missing_helper"),
    )

    from phone_agent.v2.agent import _build_provider_bootstrap
    from phone_agent.v2.capabilities import build_capability_registry

    registry = build_capability_registry(_config())
    registry.register(contributor)
    with pytest.raises(ValueError, match="missing dependency 'missing_helper'"):
        _build_provider_bootstrap(registry)


@pytest.mark.parametrize(
    "helper",
    [
        None,
        CapabilitySpec(
            "cycle_helper",
            "Cycle helper",
            "on",
            deps=("off_provider",),
        ),
    ],
)
def test_disabled_provider_contributor_is_inert_with_bad_dependencies(helper):
    from phone_agent.v2.agent import _build_provider_bootstrap
    from phone_agent.v2.capabilities import build_capability_registry

    registry = build_capability_registry(_config())
    deps = ("providers", "missing_helper") if helper is None else (
        "providers",
        helper.cap_id,
    )
    registry.register(
        CapabilitySpec(
            "off_provider",
            "Disabled provider",
            "off",
            deps=deps,
        )
    )
    if helper is not None:
        registry.register(helper)

    bootstrap = _build_provider_bootstrap(
        registry,
        runtime_cap_ids=frozenset(
            spec.cap_id for spec in build_capability_registry(_config()).specs()
        ),
    )
    assert [spec.cap_id for spec in bootstrap.specs()] == ["providers"]


def test_active_provider_bootstrap_cycle_is_visible():
    from phone_agent.v2.agent import _build_provider_bootstrap
    from phone_agent.v2.capabilities import CapabilityRegistry

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("providers", "Providers", "on"))
    registry.register(
        CapabilitySpec(
            "provider_contributor",
            "Provider contributor",
            "on",
            deps=("providers", "helper_a"),
        )
    )
    registry.register(
        CapabilitySpec("helper_a", "Helper A", "on", deps=("helper_b",))
    )
    registry.register(
        CapabilitySpec("helper_b", "Helper B", "on", deps=("helper_a",))
    )

    with pytest.raises(ValueError, match="dependency cycle"):
        _build_provider_bootstrap(registry)


def test_agent_rejects_runtime_builtin_provider_dependency_before_any_apply(
    isolated, monkeypatch
):
    applied = []
    contributor = CapabilitySpec(
        "provider_contributor",
        "Provider contributor",
        "on",
        deps=("providers", "budget"),
        apply=lambda ctx: applied.append(ctx),
    )
    import phone_agent.v2.session as session_module
    import phone_agent.v2.tools as tools_module

    fake_session = SimpleNamespace(event_bus=None, usage_ledger=None)
    monkeypatch.setattr(session_module, "PhoneSession", lambda config: fake_session)
    monkeypatch.setattr(tools_module, "build_base_tools", lambda session, config: [])
    from phone_agent.v2.agent import ThinPhoneAgent

    with pytest.raises(ValueError, match="cannot require runtime capability 'budget'"):
        ThinPhoneAgent(_config(), extra_capabilities=[contributor])
    assert applied == []
