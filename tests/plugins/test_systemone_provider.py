"""Provider adapter contract: chat convention, build-time visibility, catalogs."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from phone_agent.v2.providers import ModelSpec, ProviderRegistry, ProviderSpec
from phone_agent.v2.providers.builders import get_api_builder, unregister_api_builder
from tests.plugins.systemone_harness import (
    FakeSystemOneServer,
    HarnessConfig,
    RecordingClient,
    mount_systemone,
)


def answer(client_module, kind, value, **kwargs):
    """One real ``Answer`` of the protocol client (no parallel double)."""

    return client_module.Answer(kind=kind, value=value, **kwargs)


def reply(client_module, answers, **kwargs):
    """One real ``SystemOneReply`` around real answers."""

    return client_module.SystemOneReply(answers=answers, **kwargs)


def make_model(provider_module, *, client=None, backend="kev", model="kev-4b@qwen3"):
    return provider_module.SystemOneChatModel(
        backend=backend,
        model_id=model,
        base_url="http://127.0.0.1:1",
        request_timeout=5.0,
        max_retries=0,
        client=client,
    )


# ---------------------------------------------------------------------------
# The chat-adapter convention
# ---------------------------------------------------------------------------
def test_options_marker_is_the_last_marker_line(provider_module):
    assert provider_module.parse_options_marker("do it\nOptions: yes | no") == [
        "yes",
        "no",
    ]
    assert provider_module.parse_options_marker(
        "Options: a | b\nmore prose\nOptions: c | d | e\n\n"
    ) == ["c", "d", "e"]
    # A single option is prose, not a machine-readable marker.
    assert provider_module.parse_options_marker("Options: only me") is None
    assert provider_module.parse_options_marker("no marker here") is None
    assert provider_module.parse_options_marker(None) is None


def test_final_user_text_reads_the_last_human_message(provider_module):
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="first"),
        HumanMessage(content=[{"type": "text", "text": "second"}]),
    ]
    assert provider_module.final_user_text(messages) == "second"
    assert provider_module.final_user_text([SystemMessage(content="only")]) == ""


def test_invoke_without_a_marker_asks_a_noul_question(provider_module):
    client = RecordingClient()
    model = make_model(provider_module, client=client)
    result = model.invoke([HumanMessage(content="is the button ready?")])

    assert result.content == "yes"
    state, questions = client.calls[0]
    assert state == "is the button ready?"
    assert questions["reply"]["type"] == "noul"
    assert (
        questions["reply"]["instructions"]
        == provider_module.NOUL_QUESTION_INSTRUCTIONS
    )
    assert "description" not in questions["reply"]


def test_invoke_with_a_marker_asks_a_choice_and_returns_the_chosen_option(
    provider_module, client_module
):
    client = RecordingClient(
        reply=reply(
            client_module, {"reply": answer(client_module, "choice", "B", p=0.83)}
        )
    )
    model = make_model(provider_module, client=client)
    result = model.invoke([HumanMessage(content="pick one\nOptions: A | B | C")])

    assert result.content == "B"
    _, questions = client.calls[0]
    assert questions["reply"]["type"] == "choice"
    # The official ``choice`` criteria is an option -> description map.
    assert questions["reply"]["criteria"] == {"A": None, "B": None, "C": None}
    assert questions["reply"]["instructions"] == provider_module.CHOICE_QUESTION_INSTRUCTIONS


def test_choice_answer_outside_the_offered_options_fails_visibly(
    provider_module, client_module
):
    client = RecordingClient(
        reply=reply(client_module, {"reply": answer(client_module, "choice", "D", p=0.5)})
    )
    model = make_model(provider_module, client=client)
    with pytest.raises(
        provider_module.SystemOneAdapterError, match="not one of the offered options"
    ):
        model.invoke([HumanMessage(content="pick\nOptions: A | B")])


def test_a_noul_answer_that_is_not_yes_or_no_fails_visibly(
    provider_module, client_module
):
    client = RecordingClient(
        reply=reply(client_module, {"reply": answer(client_module, "noul", "maybe", p=0.4)})
    )
    model = make_model(provider_module, client=client)
    with pytest.raises(
        provider_module.SystemOneAdapterError, match="not a yes/no value"
    ):
        model.invoke([HumanMessage(content="ready?")])


def test_invoke_without_a_user_message_fails_visibly(provider_module):
    model = make_model(provider_module, client=object())
    with pytest.raises(provider_module.SystemOneAdapterError, match="final user message"):
        model.invoke([SystemMessage(content="only system")])


def test_transport_usage_becomes_langchain_usage_metadata(
    provider_module, client_module
):
    client = RecordingClient(
        reply=reply(
            client_module,
            {"reply": answer(client_module, "noul", True, p=0.9)},
            usage={"input_tokens": 30, "output_tokens": 12},
        )
    )
    model = make_model(provider_module, client=client)
    result = model.invoke([HumanMessage(content="ready?")])

    assert result.usage_metadata == {
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
    }


def test_missing_usage_block_leaves_usage_metadata_empty(
    provider_module, client_module
):
    client = RecordingClient(
        reply=reply(
            client_module,
            {"reply": answer(client_module, "noul", False, p=0.9)},
            usage={"weird": "x"},
        )
    )
    model = make_model(provider_module, client=client)
    assert model.invoke([HumanMessage(content="ready?")]).usage_metadata is None


def test_bind_tools_is_refused_with_actionable_text(provider_module):
    model = make_model(provider_module, client=object())
    with pytest.raises(NotImplementedError, match="cannot emit tool calls"):
        model.bind_tools([])


def test_identifying_params_never_carry_the_api_key(provider_module):
    model = make_model(provider_module, client=object(), backend="jev")
    assert model._identifying_params == {
        "backend": "jev",
        "model": "kev-4b@qwen3",
        "base_url": "http://127.0.0.1:1",
    }
    assert model._llm_type == "systemone"


# ---------------------------------------------------------------------------
# Build-time failure visibility (P0 #8)
# ---------------------------------------------------------------------------
def _builder(provider_module, client_module, *, probe=True):
    def make_client(backend, model, base_url):
        return client_module.SystemOneClient(
            backend=backend, model=model, base_url=base_url, api_key="k"
        )

    return provider_module.make_api_builder(
        make_client,
        probe=(lambda url: client_module.probe_endpoint(url, timeout=0.5))
        if probe
        else None,
    )


def _spec(backend, *, base_url, api_key=None):
    return ProviderSpec(
        id=backend,
        api="systemone",
        base_url=base_url,
        api_key=api_key,
        models={"m": ModelSpec(id="m")},
    )


def test_missing_cloud_key_fails_at_build_time(provider_module, client_module):
    builder = _builder(provider_module, client_module, probe=False)
    spec = _spec("jev", base_url="https://api.typesafe.ai")
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        builder(spec, spec.get_model("m"), HarnessConfig())


def test_unreachable_local_server_fails_at_build_time(
    provider_module, client_module, no_listener_port
):
    builder = _builder(provider_module, client_module)
    spec = _spec("kev", base_url=f"http://127.0.0.1:{no_listener_port}")
    with pytest.raises(client_module.SystemOneError, match="unreachable"):
        builder(spec, spec.get_model("m"), HarnessConfig())


def test_empty_base_url_is_refused(provider_module, client_module):
    builder = _builder(provider_module, client_module, probe=False)
    spec = _spec("kev", base_url="  ")
    with pytest.raises(ValueError, match="requires a base_url"):
        builder(spec, spec.get_model("m"), HarnessConfig())


def test_builder_reads_the_declared_settings_off_the_config(
    provider_module, client_module, fake_server
):
    builder = _builder(provider_module, client_module)
    spec = _spec("kev", base_url=fake_server.url)
    config = HarnessConfig()
    config.declare_plugin_setting("systemone_timeout", 7.5)
    config.declare_plugin_setting("systemone_retries", 4)
    model = builder(spec, spec.get_model("m"), config)
    assert model.request_timeout == 7.5
    assert model.max_retries == 4


# ---------------------------------------------------------------------------
# Provider specs
# ---------------------------------------------------------------------------
def test_provider_specs_declare_both_backends_and_their_catalogs(
    provider_module, client_module
):
    specs = {
        spec.id: spec
        for spec in provider_module.build_provider_specs(
            catalogs=client_module.DEFAULT_MODELS,
            base_urls=client_module.DEFAULT_BASE_URLS,
            jev_api_key="sk-jev-test",
        )
    }
    assert set(specs) == {"jev", "kev"}
    assert specs["jev"].api == provider_module.API_FAMILY
    assert specs["jev"].base_url == "https://api.typesafe.ai"
    assert specs["jev"].api_key == "sk-jev-test"
    assert specs["kev"].base_url == "http://127.0.0.1:8787"
    assert specs["kev"].api_key is None
    assert specs["jev"].model_ids() == ("jev-latest",)
    assert set(specs["kev"].model_ids()) == {"kev-4b@qwen3", "kev-8b", "kev-9b"}


def test_provider_specs_refuse_a_backend_without_models(provider_module):
    with pytest.raises(ValueError, match="declares no models"):
        provider_module.build_provider_specs(
            catalogs={"jev": (), "kev": ("kev-8b",)},
            base_urls={"jev": "https://api.typesafe.ai", "kev": "http://127.0.0.1:8787"},
        )


# ---------------------------------------------------------------------------
# End to end through the harness's own registry path
# ---------------------------------------------------------------------------
def test_registered_transport_builds_a_working_model_through_the_registry(
    provider_module, monkeypatch
):
    from phone_agent.v2.providers import build_model_from_resolved

    server = FakeSystemOneServer(usage={"total_tokens": 7}).start()
    try:
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", server.url)
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_REVIEW", "off")
        mounted = mount_systemone(
            config=HarnessConfig(),
            session=None,
            extra_services={"provider_registry": ProviderRegistry()},
        )
        registry = mounted.service("provider_registry")
        assert set(registry.list_providers()) == {"jev", "kev"}
        resolved = registry.resolve("kev:kev-4b@qwen3")
        assert resolved.provider.id == "kev"
        model = build_model_from_resolved(resolved, mounted.config)
        reply = model.invoke([HumanMessage(content="ready?\nOptions: go | stop")])
        assert reply.content == "go"
        assert reply.usage_metadata["total_tokens"] == 7
    finally:
        server.stop()


def test_transport_registration_is_idempotent_and_disposed_on_release(
    provider_module, monkeypatch
):
    server = FakeSystemOneServer().start()
    try:
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", server.url)
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_REVIEW", "off")
        first = mount_systemone(
            config=HarnessConfig(),
            session=None,
            extra_services={"provider_registry": ProviderRegistry()},
        )
        builder = get_api_builder(provider_module.API_FAMILY)
        assert builder is not None and getattr(builder, "_taskwizard_systemone", False)

        first.ctx.release_capability("systemone")
        assert get_api_builder(provider_module.API_FAMILY) is None

        second = mount_systemone(
            config=HarnessConfig(),
            session=None,
            extra_services={"provider_registry": ProviderRegistry()},
        )
        assert get_api_builder(provider_module.API_FAMILY) is not None
        second.ctx.release_capability("systemone")
    finally:
        server.stop()


def test_a_foreign_api_family_registration_is_refused(provider_module, monkeypatch):
    from phone_agent.v2.providers import register_api_builder

    server = FakeSystemOneServer().start()
    try:
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", server.url)
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_REVIEW", "off")
        register_api_builder(provider_module.API_FAMILY, lambda *a, **k: None, override=True)
        try:
            with pytest.raises(ValueError, match="already registered"):
                mount_systemone(config=HarnessConfig(), session=None)
        finally:
            unregister_api_builder(provider_module.API_FAMILY)
    finally:
        server.stop()
