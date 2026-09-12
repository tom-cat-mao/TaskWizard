"""Protocol locks and model-owned context support, using only synthetic data."""

from copy import deepcopy
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
import pytest

from phone_agent.v2.providers import (
    ModelContextProfile,
    ModelInputEstimate,
    PreparedModelMessages,
    bind_context_support,
    build_model_from_resolved,
    estimate_model_input,
    get_context_support,
    model_context_profile,
    model_protected_message_ids,
    normalize_model_usage,
    prepare_model_messages,
    register_api_builder,
    unregister_api_builder,
)
from phone_agent.v2.providers.builders import effective_compat
from phone_agent.v2.providers.loader import (
    ModelsFileError,
    build_provider_registry,
    parse_models_json,
)
from phone_agent.v2.providers.registry import ProviderRegistryError
from phone_agent.v2.providers.types import (
    ModelSpec,
    ProviderCompat,
    ProviderSpec,
    ResolvedModel,
)


def config(**overrides):
    return SimpleNamespace(
        **{
            "model_timeout": 1,
            "model_max_retries": 0,
            "sampling": {},
            "thinking": "off",
            "streaming": "off",
            "parallel_tool_calls": False,
            "base_url": "https://example.invalid/v1",
            "api_key": "synthetic-key",
            "model_name": "example-model",
            "models_file": None,
            **overrides,
        }
    )


def build(
    api="openai-completions",
    model_id="example-model",
    *,
    compat=None,
    model_compat=None,
    sampling=None,
):
    spec = ModelSpec(
        id=model_id,
        context_window=48000,
        max_tokens=800,
        sampling_params=sampling or {},
        compat=model_compat,
    )
    provider = ProviderSpec(
        id="synthetic",
        api=api,
        api_key="synthetic-key",
        base_url="https://example.invalid/v1",
        compat=compat,
        models={model_id: spec},
    )
    return build_model_from_resolved(
        ResolvedModel(provider, spec, "synthetic:" + model_id), config()
    )


@tool
def example_tool(target_mark_id: str, intent: str) -> str:
    """A schema-only example; no device access."""
    raise AssertionError("Never execute this tool")


@pytest.mark.parametrize("model_id", ["codex/example-model", "example-model"])
@pytest.mark.parametrize("choice", [None, "auto", "chat", "responses"])
def test_protocol_matrix(model_id, choice):
    model = build(model_id=model_id, compat=ProviderCompat(request_api=choice))
    payload = model._get_request_payload([HumanMessage(content="synthetic")])
    expected = (
        "responses"
        if choice == "responses" or (choice in {None, "auto"} and "codex" in model_id)
        else "chat/completions"
    )
    assert ("responses" if "input" in payload else "chat/completions") == expected
    assert model_context_profile(model).request_api == expected


def test_inheritance_and_explicit_auto_reset():
    spec = ModelSpec(
        id="model", compat=ProviderCompat(request_api="auto", cache_policy="off")
    )
    provider = ProviderSpec(
        id="p",
        api="openai-completions",
        compat=ProviderCompat(request_api="responses", cache_policy="stable-prefix"),
    )
    resolved = effective_compat(provider, spec)
    assert resolved.request_api == "auto" and resolved.request_api_declared == "auto"
    assert resolved.cache_policy == "off"
    inherited = effective_compat(provider, ModelSpec(id="model"))
    assert inherited.request_api == "responses"
    assert inherited.cache_policy == "stable-prefix"


@pytest.mark.parametrize(
    "key,value",
    [("requestApi", "response"), ("requestApi", True), ("cachePolicy", "magic")],
)
def test_invalid_typed_choices_reject_strict_parse(key, value):
    with pytest.raises(ModelsFileError, match="compat"):
        parse_models_json(
            {"providers": {"p": {"api": "openai-completions", "compat": {key: value}}}}
        )


@pytest.mark.parametrize(
    "choice,legacy", [("chat", True), ("responses", False), ("auto", True)]
)
def test_conflicting_protocol_escape_hatch_rejected(choice, legacy):
    with pytest.raises(ValueError, match="conflicts"):
        build(
            compat=ProviderCompat(request_api=choice),
            sampling={"use_responses_api": legacy},
        )


def test_equal_legacy_override_and_explicit_lock_are_compatible():
    model = build(
        compat=ProviderCompat(request_api="chat"), sampling={"use_responses_api": False}
    )
    assert model.use_responses_api is False


def test_forced_chat_rejects_responses_only_options():
    with pytest.raises(ValueError, match="Responses-only"):
        build(
            compat=ProviderCompat(request_api="chat"),
            sampling={"reasoning": {"effort": "low"}},
        )


@pytest.mark.parametrize("api", ["anthropic-messages", "google-generative-ai"])
def test_openai_selection_is_not_silently_ignored_by_other_builtins(api):
    with pytest.raises(ValueError, match="OpenAI-family"):
        build(api=api, compat=ProviderCompat(request_api="responses"))


def test_cache_requires_explicit_supported_configuration():
    with pytest.raises(ValueError, match="requires explicit"):
        build(
            model_id="codex/example-model",
            compat=ProviderCompat(cache_policy="stable-prefix"),
        )
    with pytest.raises(ValueError, match="not implemented"):
        build(
            api="google-generative-ai",
            compat=ProviderCompat(cache_policy="stable-prefix"),
        )
    with pytest.raises(ValueError, match="conflicts"):
        build(
            compat=ProviderCompat(
                request_api="responses", cache_policy="stable-prefix"
            ),
            sampling={"prompt_cache_options": {"mode": "implicit"}},
        )


@pytest.mark.parametrize("scope", ["provider", "model"])
def test_lenient_loader_does_not_turn_invalid_protocol_into_gateway_auto(
    tmp_path, monkeypatch, scope
):
    monkeypatch.chdir(tmp_path)
    data = {"providers": {"gateway": {"api": "openai-completions"}}}
    provider = data["providers"]["gateway"]
    if scope == "provider":
        provider["compat"] = {"requestApi": "typo"}
    else:
        provider["models"] = [{"id": "example-model", "compat": {"requestApi": "typo"}}]
    (tmp_path / ".taskwizard.models.json").write_text(json.dumps(data))
    registry = build_provider_registry(config())
    assert registry.get("gateway") is not None
    with pytest.raises(ProviderRegistryError, match="invalid explicit context"):
        registry.resolve("example-model")
    if scope == "model":
        assert registry.resolve("another-model").synthesized


def test_higher_priority_corrected_protocol_unblocks_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".taskwizard.models.json").write_text(
        json.dumps({"providers": {"gateway": {"compat": {"requestApi": "typo"}}}})
    )
    path = tmp_path / "explicit.json"
    path.write_text(
        json.dumps({"providers": {"gateway": {"compat": {"requestApi": "chat"}}}})
    )
    registry = build_provider_registry(config(models_file=str(path)))
    assert registry.resolve("example-model").provider.compat.request_api == "chat"


def test_support_is_private_and_survives_copy_and_tool_binding():
    model = build()
    support = get_context_support(model)
    assert support is not None
    assert "_taskwizard_context_support" not in model.__dict__
    assert "_taskwizard_context_support" not in model.model_dump()
    assert get_context_support(model.model_copy()) is support
    # Invocation/stream observers use shallow model_copy; SDK HTTP clients
    # themselves do not support arbitrary deep-copying of connection locks.
    assert get_context_support(model.model_copy(update={"callbacks": []})) is support
    bound = model.bind_tools([example_tool])
    assert get_context_support(bound) is support
    profile = model_context_profile(bound)
    assert profile.context_window == 48000 and profile.max_output_tokens == 800
    estimate = estimate_model_input(bound, [HumanMessage(content="task")])
    assert (
        estimate.tokens
        > estimate_model_input(model, [HumanMessage(content="task")]).tokens
    )
    assert estimate.includes_tools and not estimate.complete
    assert not estimate_model_input(
        model, [HumanMessage(content="task")]
    ).includes_tools
    assert model.bind_tools([example_tool]).kwargs["tools"] == bound.kwargs["tools"]


def test_profile_observes_bound_protocol_features_and_output_cap():
    model = build()
    assert model_context_profile(model).request_api == "chat/completions"
    bound = model.bind(reasoning={"effort": "low"}, max_completion_tokens=123)
    profile = model_context_profile(bound)
    assert profile.request_api == "responses" and profile.max_output_tokens == 123
    assert (
        model_context_profile(model.bind(max_completion_tokens=None)).max_output_tokens
        is None
    )


def test_cache_policy_does_not_enable_server_side_history():
    with pytest.raises(ValueError, match="full client-owned"):
        build(
            compat=ProviderCompat(
                request_api="responses", cache_policy="stable-prefix"
            ),
            sampling={"use_previous_response_id": True},
        )


def test_optional_custom_support_uses_same_base_model_contract():
    class CustomSupport:
        def profile(self, model, tools=()):
            return ModelContextProfile(
                context_window=12345, request_api="custom", source="plugin"
            )

        def estimate(self, model, messages, tools=()):
            return ModelInputEstimate(
                tokens=42, includes_tools=True, complete=True, source="custom-local"
            )

        def normalize_usage(self, message):
            return {
                "input_tokens": 42,
                "output_tokens": 2,
                "cache_read_tokens": None,
                "cache_write_tokens": 0,
            }

    model = bind_context_support(build(), CustomSupport())
    assert model_context_profile(model.model_copy()).request_api == "custom"
    assert estimate_model_input(model.bind_tools([example_tool]), []).tokens == 42
    assert normalize_model_usage(model, None)["cache_read_tokens"] is None
    prepared = prepare_model_messages(model, [HumanMessage(content="kept")])
    assert prepared.messages[0].content == "kept"


def test_custom_api_builder_can_opt_in_without_changing_its_old_signature():
    class CustomModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "synthetic-custom"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="synthetic"))]
            )

        def bind_tools(self, tools, **kwargs):
            return self.bind(tools=tools, **kwargs)

    class Support:
        def estimate(self, model, messages, tools=()):
            return ModelInputEstimate(
                71, includes_tools=True, complete=True, source="custom"
            )

    # An old builder without the newer streaming keyword remains accepted.
    def factory(provider, spec, config, *, sampling, headers, level, compat):
        return bind_context_support(CustomModel(), Support())

    api = "context-test-custom"
    register_api_builder(api, factory)
    try:
        model = build(api=api)
        assert isinstance(model, CustomModel)
        assert estimate_model_input(model.bind_tools([example_tool]), []).tokens == 71
    finally:
        unregister_api_builder(api)


def test_plugin_without_support_keeps_generic_fallback():
    model = SimpleNamespace()
    assert get_context_support(model) is None
    assert model_context_profile(model).context_window is None
    estimate = estimate_model_input(
        model, [HumanMessage(content="a meaningful request")]
    )
    assert estimate.tokens > 0 and not estimate.complete and not estimate.includes_tools
    assert normalize_model_usage(model, None) is None


@pytest.mark.parametrize("mutation", ["delete", "text", "status", "raise"])
def test_bad_optional_preparation_cannot_change_semantics(mutation):
    class BadSupport:
        def prepare(self, model, messages, tools=()):
            if mutation == "delete":
                messages.pop()
            elif mutation == "text":
                messages[-1].content = "invented success"
            elif mutation == "status":
                messages[-1].status = "success"
            else:
                messages.clear()
                raise RuntimeError("synthetic")
            return PreparedModelMessages(messages)

    canonical = [ToolMessage(content="failed", status="error", tool_call_id="call")]
    before = deepcopy(canonical)
    result = prepare_model_messages(
        bind_context_support(build(), BadSupport()), canonical
    )
    assert result.messages == before and canonical == before


@pytest.mark.parametrize(
    "settings",
    [{"model": "different-model", "input": []}, {"extra_body": {"input": []}}],
)
def test_cache_preparation_cannot_override_model_or_canonical_input_via_kwargs(
    settings,
):
    class BadSupport:
        def prepare(self, model, messages, tools=()):
            return PreparedModelMessages(messages, settings)

    prepared = prepare_model_messages(
        bind_context_support(build(), BadSupport()), [HumanMessage(content="task")]
    )
    assert prepared.model_kwargs == {} and prepared.messages[0].content == "task"


def test_estimator_receives_private_messages():
    class Support:
        def estimate(self, model, messages, tools=()):
            messages[0].content = "changed"
            return ModelInputEstimate(1)

    messages = [HumanMessage(content="canonical")]
    estimate_model_input(bind_context_support(build(), Support()), messages)
    assert messages[0].content == "canonical"


def test_usage_interpreter_cannot_mutate_budget_metadata():
    class Support:
        def normalize_usage(self, message):
            message.usage_metadata["input_tokens"] = 0
            return None

    message = AIMessage(
        content="done",
        usage_metadata={"input_tokens": 50, "output_tokens": 2, "total_tokens": 52},
    )
    normalize_model_usage(bind_context_support(build(), Support()), message)
    assert message.usage_metadata["input_tokens"] == 50


def image_block(seq):
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"},
        "screen_seq": seq,
    }


def cache_model(api="openai-completions"):
    return build(
        api=api,
        compat=ProviderCompat(
            request_api="responses" if api == "openai-completions" else None,
            cache_policy="stable-prefix",
        ),
    )


def marked_texts(messages, field):
    return [
        block["text"]
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict) and field in block
    ]


def test_openai_cache_keeps_previous_stable_endpoint_and_current_images_untouched():
    model = cache_model()
    first = [
        SystemMessage(content="policy"),
        HumanMessage(
            content=[
                {"type": "text", "text": "task"},
                {"type": "text", "text": "folded one"},
                image_block(2),
            ]
        ),
        SystemMessage(content="[TASK_DOC] current", id="__taskdoc__1"),
    ]
    before = deepcopy(first)
    prepared1 = prepare_model_messages(model, first)
    assert first == before
    assert marked_texts(prepared1.messages, "prompt_cache_breakpoint") == [
        "policy",
        "folded one",
    ]
    second = deepcopy(first)
    second[1].content[2:] = [{"type": "text", "text": "folded two"}, image_block(3)]
    prepared2 = prepare_model_messages(model, second)
    assert marked_texts(prepared2.messages, "prompt_cache_breakpoint") == [
        "policy",
        "folded one",
        "folded two",
    ]
    assert prepared2.messages[1].content[-1] == image_block(3)
    assert prepared2.messages[-1].content == "[TASK_DOC] current"
    assert prepared2.model_kwargs == {"prompt_cache_options": {"mode": "explicit"}}
    payload = model._get_request_payload(prepared2.messages, **prepared2.model_kwargs)
    assert payload["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


def test_compaction_or_content_change_invalidates_old_endpoint():
    model = cache_model()
    old = [SystemMessage(content="policy"), HumanMessage(content="old history")]
    prepare_model_messages(model, old)
    new = [
        SystemMessage(content="policy"),
        SystemMessage(content="[COMPACT_SUMMARY] new generation"),
        HumanMessage(content=[{"type": "text", "text": "new history"}, image_block(3)]),
    ]
    prepared = prepare_model_messages(model, new)
    assert "old history" not in marked_texts(
        prepared.messages, "prompt_cache_breakpoint"
    )


def test_raw_marks_are_a_mutable_cache_boundary():
    messages = [
        SystemMessage(content="policy"),
        HumanMessage(
            content=[
                {"type": "text", "text": "task"},
                {"type": "text", "text": "[OBS] screen#1\nmarks (1): ax_1@e1"},
            ]
        ),
    ]
    prepared = prepare_model_messages(cache_model(), messages)
    assert marked_texts(prepared.messages, "prompt_cache_breakpoint") == [
        "policy",
        "task",
    ]


def test_anthropic_dynamic_system_limits_cache_to_stable_system_prefix():
    model = cache_model("anthropic-messages")
    canonical = [
        SystemMessage(content="policy"),
        HumanMessage(content="task and history"),
        SystemMessage(content="[TASK_DOC] current", id="__taskdoc__1"),
    ]
    prepared = prepare_model_messages(model, canonical)
    assert marked_texts(prepared.messages, "cache_control") == ["policy"]
    payload = model._get_request_payload(prepared.messages)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["system"][1]
    assert canonical[0].content == "policy"


def test_cross_protocol_preparation_removes_other_cache_metadata_only():
    original = [HumanMessage(content="task")]
    openai = prepare_model_messages(cache_model(), original)
    anthropic = prepare_model_messages(
        cache_model("anthropic-messages"), openai.messages
    )
    assert not marked_texts(anthropic.messages, "prompt_cache_breakpoint")
    assert marked_texts(anthropic.messages, "cache_control") == ["task"]
    ordinary = prepare_model_messages(
        build(api="google-generative-ai", model_id="gemini-2.5-flash"),
        anthropic.messages,
    )
    assert not marked_texts(ordinary.messages, "cache_control")
    assert ordinary.model_kwargs == {}
    assert original[0].content == "task"


def test_tool_arguments_named_like_cache_fields_are_untouched():
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "synthetic",
                "id": "call",
                "type": "tool_call",
                "args": {
                    "cache_control": "user data",
                    "prompt_cache_breakpoint": "user data",
                },
            }
        ],
    )
    prepared = prepare_model_messages(build(), [message])
    assert prepared.messages[0].tool_calls == message.tool_calls


def test_unknown_native_blocks_protect_their_message_identity():
    message = AIMessage(
        content=[{"type": "reasoning", "encrypted_content": "synthetic"}], id="opaque"
    )
    assert model_protected_message_ids(build(), [message]) == {"opaque"}
