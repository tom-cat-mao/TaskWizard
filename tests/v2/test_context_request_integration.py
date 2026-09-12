"""Actual-attempt context support across actor fallback and auxiliary calls."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from phone_agent.v2.agent import _WrapModelBridgeMiddleware
from phone_agent.v2.events import EventBus
from phone_agent.v2.middleware.context_request import (
    ContextCapacityError,
    ContextRequestObserver,
    invoke_with_context,
)
from phone_agent.v2.providers.context import (
    ModelContextProfile,
    ModelInputEstimate,
    PreparedModelMessages,
    bind_context_support,
)
from phone_agent.v2.usage import usage_details


class _Model(BaseChatModel):
    seen: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "synthetic-context"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(deepcopy(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)


class _Support:
    def __init__(self, api, *, window=20000, tokens=1000):
        self.api = api
        self.window = window
        self.tokens = tokens

    def profile(self, model, tools=()):
        return ModelContextProfile(
            request_api=self.api, context_window=self.window, source="synthetic-declaration"
        )

    def estimate(self, model, messages, tools=()):
        return ModelInputEstimate(self.tokens, includes_tools=True, complete=True, source="synthetic-exact")

    def prepare(self, model, messages, tools=()):
        key = "cache_control" if self.api == "anthropic-messages" else "prompt_cache_breakpoint"
        value = {"type": "ephemeral"} if key == "cache_control" else {"mode": "explicit"}
        content = messages[0].content
        messages[0].content = [{"type": "text", "text": content, key: value}]
        return PreparedModelMessages(messages)


def _config(**kwargs):
    return SimpleNamespace(
        model_name="synthetic-primary", context_window=kwargs.get("context_window"),
        compact_schema_reserve=100, compact_output_reserve=100,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_each_fallback_attempt_gets_its_own_transient_protocol(asynchronous):
    primary = bind_context_support(_Model(), _Support("anthropic-messages"))
    backup = bind_context_support(_Model(), _Support("responses"))
    messages = [SystemMessage(content="stable policy"), HumanMessage(content="synthetic task")]
    events = []
    bridge = _WrapModelBridgeMiddleware(
        EventBus(), config=_config(), fallback_provider=lambda: (backup, "synthetic-backup"),
        trace_recorder=lambda event, **payload: events.append((event, payload)),
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.model is primary:
            raise RuntimeError("synthetic unavailable")
        return AIMessage(content="ok")

    request = ModelRequest(model=primary, messages=messages, tools=[])
    if asynchronous:
        async def async_handler(request):
            return handler(request)

        result = asyncio.run(bridge.awrap_model_call(request, async_handler))
    else:
        result = bridge.wrap_model_call(request, handler)
    assert result.content == "ok"
    assert len(requests) == 2
    assert "cache_control" in requests[0].messages[0].content[0]
    assert "prompt_cache_breakpoint" in requests[1].messages[0].content[0]
    assert "cache_control" not in requests[1].messages[0].content[0]
    assert messages[0].content == "stable policy"
    prepared = [data for kind, data in events if kind == "context_request"]
    assert [data["request_api"] for data in prepared] == ["anthropic-messages", "responses"]
    assert [data["attempt"] for data in prepared] == [1, 2]
    usage = [data for kind, data in events if kind == "model_attempt_usage"]
    assert [data["ok"] for data in usage] == [False, True]
    assert all(data["attempt_scope"] == "handler_invoke" for data in usage)


def test_small_fallback_is_not_called_and_canonical_history_is_preserved():
    primary = bind_context_support(_Model(), _Support("responses", window=20000, tokens=3000))
    backup = bind_context_support(_Model(), _Support("responses", window=2000, tokens=3000))
    messages = [SystemMessage(content="protected policy"), HumanMessage(content="protected facts")]
    seen = []
    events = []
    bridge = _WrapModelBridgeMiddleware(
        EventBus(), config=_config(context_window=10000),
        fallback_provider=lambda: (backup, "small-backup"),
        trace_recorder=lambda event, **data: events.append((event, data)),
    )

    def handler(request):
        seen.append(request.model)
        raise RuntimeError("primary unavailable")

    with pytest.raises(RuntimeError, match="primary unavailable") as caught:
        bridge.wrap_model_call(ModelRequest(model=primary, messages=messages), handler)
    assert isinstance(caught.value.__cause__, ContextCapacityError)
    assert seen == [primary]
    assert [message.content for message in messages] == ["protected policy", "protected facts"]
    admission = [data for kind, data in events if kind == "context_admission"]
    assert admission[0]["context_window"] == 2000
    assert admission[0]["allowed"] is False


def test_separate_system_message_is_decorated_once():
    model = bind_context_support(_Model(), _Support("responses"))
    observer = ContextRequestObserver(_config())
    system = SystemMessage(content="rules")
    human = HumanMessage(content="task")
    prepared, _ = observer.prepare(
        ModelRequest(model=model, system_message=system, messages=[human])
    )
    assert prepared.system_message.content[0]["text"] == "rules"
    assert len(prepared.messages) == 1
    assert prepared.messages[0].content == "task"
    assert system.content == "rules"


def test_auxiliary_calls_apply_support_without_actor_transcript_or_window():
    model = bind_context_support(_Model(), _Support("responses", window=10000))
    messages = [SystemMessage(content="independent verifier"), HumanMessage(content="goal and evidence")]
    response = invoke_with_context(
        model, messages, config=_config(context_window=1), role="verifier"
    )
    assert response.content == "done"
    assert model.seen[0][0].content[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert model.seen[0][1].content == "goal and evidence"
    assert messages[0].content == "independent verifier"


def test_custom_usage_normalizer_is_numeric_allowlist_only():
    class UsageSupport(_Support):
        def normalize_usage(self, message):
            return {"input_tokens": 15, "output_tokens": 2, "cache_read_tokens": 0,
                    "cache_write_tokens": None, "secret": "never expose"}

    model = bind_context_support(_Model(), UsageSupport("custom"))
    events = []
    invoke_with_context(
        model, [SystemMessage(content="context")], role="reviewer",
        trace_recorder=lambda event, **data: events.append((event, data)),
    )
    usage = [data for event, data in events if event == "model_attempt_usage"][0]
    assert usage["input_tokens"] == 15
    assert usage["cache_read_tokens"] == 0
    assert usage["cache_write_tokens"] is None
    assert "secret" not in str(events)


def test_custom_normalizer_receives_message_even_without_standard_usage():
    class RawUsageSupport(_Support):
        def normalize_usage(self, message):
            return {"input_tokens": message.response_metadata["custom_input"]}

    model = bind_context_support(_Model(), RawUsageSupport("custom"))
    message = AIMessage(content="done", response_metadata={"custom_input": 7})
    details = usage_details(SimpleNamespace(result=[message]), model=model)
    assert details["input_tokens"] == 7
    assert details["cache_read_tokens"] is None


def test_observer_reports_ordered_client_scope_and_resets_between_runs():
    events = []
    observer = ContextRequestObserver(_config(), lambda event, **data: events.append((event, data)))
    model = _Model()
    for content in ("first", "second"):
        observer.prepare(ModelRequest(model=model, messages=[HumanMessage(content=content)]))
    assert events[-1][1]["first_changed"] == "messages[0]"
    assert events[-1][1]["fingerprint_basis"] == "ordered_client_messages"
    assert "first" not in str({k: v for k, v in events[-1][1].items() if k != "first_changed"})
    observer.reset()
    observer.prepare(ModelRequest(model=model, messages=[HumanMessage(content="third")]))
    assert events[-1][1]["attempt"] == 1
    assert events[-1][1]["first_changed"] is None


def test_explicit_invocation_settings_conflict_is_visible():
    class SettingsSupport(_Support):
        def prepare(self, model, messages, tools=()):
            return PreparedModelMessages(messages, {"prompt_cache_options": {"mode": "explicit"}})

    model = bind_context_support(_Model(), SettingsSupport("responses"))
    observer = ContextRequestObserver(_config())
    request = ModelRequest(
        model=model, messages=[HumanMessage(content="task")],
        model_settings={"prompt_cache_options": {"mode": "implicit"}},
    )
    with pytest.raises(ValueError, match="conflicts"):
        observer.prepare(request)
    assert model.seen == []


def test_broken_trace_sink_does_not_fail_a_model_call():
    def broken_trace(*args, **kwargs):
        raise RuntimeError("synthetic trace unavailable")

    model = _Model()
    response = invoke_with_context(
        model, [HumanMessage(content="task")], trace_recorder=broken_trace
    )
    assert response.content == "done"
