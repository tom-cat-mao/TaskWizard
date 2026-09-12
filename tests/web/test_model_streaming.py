"""Incremental model-text streaming: observe-only projection, offline evidence.

Every test in this module is offline: fake httpx ``MockTransport`` clients stand
in for the three transports, tool functions are local stubs, and no real device,
gateway, MLX model or private memory store is touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk

from phone_agent.v2.events import EventBus
from phone_agent.v2.middleware.streaming import (
    ModelStreamObserver,
    extract_display_text,
    model_with_stream_observer,
)
from phone_agent.v2.providers import ModelSpec, ProviderSpec
from phone_agent.v2.run_events import WebEventMiddleware
from phone_agent.web.app import _stream_selection, _stream_tail
from phone_agent.web.bridge import WebRunBridge


# --------------------------------------------------------------------- helpers


class _Sink:
    """Minimal event sink: a list with the ``put`` contract."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def put(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def kinds(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event") == name]


def _sse(chunks: list[dict[str, Any]]) -> str:
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def _openai_chunk(delta: dict[str, Any], *, finish: str | None = None, usage=None):
    payload: dict[str, Any] = {
        "id": "1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fake-model",
        "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
    return payload


def _openai_transport(
    *,
    text: str = "我先点击",
    tool_name: str = "tap",
    tool_args: str = '{"target":"ok"}',
    final_text: str = "完成",
    usage: dict[str, int] | None = None,
    fail_after: int | None = None,
    calls: list[dict[str, Any]] | None = None,
):
    """A MockTransport handler serving one streaming chat completion.

    The first request returns an optional tool call; a request that already
    carries a tool result returns ``final_text`` (mirrors a real turn).
    """

    usage = usage or {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if calls is not None:
            calls.append(body)
        has_tool_result = any(
            message.get("role") == "tool" for message in body.get("messages", [])
        )
        if has_tool_result:
            chunks = [
                _openai_chunk({"role": "assistant", "content": final_text}),
                _openai_chunk({}, finish="stop"),
                _openai_chunk({}, usage=usage),
            ]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=_sse(chunks)
            )
        chunks: list[dict[str, Any]] = [
            _openai_chunk({"role": "assistant", "content": text[:1]}),
            _openai_chunk({"content": text[1:]}),
        ]
        if tool_name:
            half = len(tool_args) // 2
            chunks.append(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": tool_args[:half],
                                },
                            }
                        ]
                    }
                )
            )
            chunks.append(
                _openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": tool_args[half:]},
                            }
                        ]
                    }
                )
            )
        chunks.append(_openai_chunk({}, finish="tool_calls" if tool_name else "stop"))
        chunks.append(_openai_chunk({}, usage=usage))
        if fail_after is None:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=_sse(chunks)
            )

        def broken():
            for chunk in chunks[:fail_after]:
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            raise httpx.ReadError("connection lost mid-stream")

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=broken()
        )

    return handler


def _openai_model(handler, **kwargs):
    from langchain_openai import ChatOpenAI

    kwargs.setdefault("stream_usage", True)
    kwargs.setdefault("streaming", True)
    return ChatOpenAI(
        model="fake-model",
        api_key="x",
        base_url="http://fake.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
        **kwargs,
    )


# ------------------------------------------------------- extraction primitives


def test_extract_display_text_keeps_only_sdk_text_and_reasoning():
    text, reasoning = extract_display_text(
        AIMessageChunk(
            content=[
                {"type": "text", "text": "正文"},
                {"type": "thinking", "thinking": "推理"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + "A" * 400},
                },
                {"type": "tool_use", "id": "t1", "name": "tap", "input": {"x": 1}},
            ]
        )
    )
    assert text == "正文"
    assert reasoning == "推理"


def test_extract_display_text_reads_reasoning_content_extra():
    text, reasoning = extract_display_text(
        AIMessageChunk(content="答案", additional_kwargs={"reasoning_content": "思考"})
    )
    assert (text, reasoning) == ("答案", "思考")


# --------------------------------------------------------------- observer unit


def test_observer_emits_start_delta_end_and_redacts():
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 3,
        flush_chars=4,
        flush_seconds=0.0,
    )
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="sk-live0000secret"))
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="答案"))
    observer.on_llm_end(None)
    names = [name for name, _ in events]
    assert names[0] == "model_stream_start"
    assert names[-1] == "model_stream_end"
    assert events[0][1] == {"attempt": 3}
    assert events[-1][1] == {"attempt": 3, "ok": True}
    joined = "".join(payload.get("text", "") for name, payload in events if name.endswith("delta"))
    assert "sk-live0000secret" not in joined
    assert "<redacted>" in joined
    assert "答案" in joined


def test_observer_drops_unshowable_chunks_and_envelope_fields():
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    observer.on_llm_new_token(
        "",
        chunk=AIMessageChunk(
            content=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "B" * 500}}
            ]
        ),
    )
    observer.on_llm_new_token(
        "",
        chunk=AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "tap", "args": '{"target"', "id": "c1", "index": 0}
            ],
        ),
    )
    assert events == []


def test_failed_attempt_keeps_partial_text_and_its_own_identity():
    events: list[tuple[str, dict]] = []
    attempts = iter([1, 2])
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: next(attempts),
        flush_chars=1,
        flush_seconds=0.0,
    )
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="半截"))
    observer.on_llm_error(httpx.ReadError("boom"))
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="备用答案"))
    observer.on_llm_end(None)
    deltas = [payload for name, payload in events if name == "model_stream_delta"]
    assert [payload["attempt"] for payload in deltas] == [1, 2]
    assert deltas[0]["text"] == "半截"
    assert deltas[1]["text"] == "备用答案"
    ends = [payload for name, payload in events if name == "model_stream_end"]
    assert ends == [
        {"attempt": 1, "ok": False, "error": "ReadError"},
        {"attempt": 2, "ok": True},
    ]


def test_observer_failures_never_raise_into_the_model_call():
    def broken_emit(_name, _payload):
        raise RuntimeError("sink down")

    observer = ModelStreamObserver(broken_emit, lambda: 1, flush_chars=1, flush_seconds=0)
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="x"))
    observer.on_llm_end(None)  # must not raise


def test_model_with_stream_observer_copies_without_mutating_the_original():
    model = _openai_model(_openai_transport())
    observer = ModelStreamObserver(lambda *_a: None, lambda: 1)
    instrumented = model_with_stream_observer(model, observer)
    assert instrumented is not None
    assert instrumented is not model
    assert model.callbacks in (None, [])
    assert observer in instrumented.callbacks


# ------------------------------------------------------------- middleware (off)


def test_middleware_off_keeps_the_plain_call_and_emits_no_stream_events():
    sink = _Sink()
    middleware = WebEventMiddleware(sink)
    model = _openai_model(_openai_transport(), streaming=False)

    class _Request:
        pass

    request = _Request()
    request.model = model

    def handler(_request):
        return SimpleNamespace(result=[SimpleNamespace(type="ai", content="ok")])

    middleware.wrap_model_call(request, handler)
    assert sink.kinds("model_stream_start") == []
    assert sink.kinds("model_stream_delta") == []
    assert middleware.streaming is False
    assert model.callbacks in (None, [])

    middleware.set_streaming(True)
    assert middleware.streaming is True
    middleware.set_streaming(False)
    assert middleware.streaming is False


def test_stream_events_use_the_documented_envelope(tmp_path):
    events, _result, _order, _calls = _run_agent_with_streaming(tmp_path)
    for event in events:
        if not str(event.get("event", "")).startswith("model_stream"):
            continue
        assert set(event) <= {"event", "step", "attempt", "text", "reasoning", "ok", "error", "ts"}


# ------------------------------------------------------ create_agent end to end


def _tool_recorder(order: list[tuple[str, str]]):
    from langchain_core.tools import tool

    @tool
    def tap(target: str) -> str:
        """Tap one target on the screen."""

        order.append(("tool", target))
        return f"已点击 {target}"

    return tap


def _run_agent_with_streaming(tmp_path, **kwargs):
    """Run one fake actor turn with the web middleware in streaming mode."""

    order: list[tuple[str, str]] = []
    calls: list[dict[str, Any]] = []
    sink = _Sink()

    class _OrderedObserver(WebEventMiddleware):
        """Records delta arrivals into the same order list as the tool."""

        def _on_stream_event(self, name, payload):  # noqa: ANN001
            if name == "model_stream_delta":
                order.append(("delta", str(payload.get("text") or "")))
            super()._on_stream_event(name, payload)

    middleware = _OrderedObserver(sink, streaming=True)
    model = _openai_model(_openai_transport(calls=calls, **kwargs))
    agent = create_agent(
        model, tools=[_tool_recorder(order)], middleware=[middleware]
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "go"}]})
    return sink.events, result, order, calls


def test_streaming_turn_keeps_full_message_usage_and_action_order(tmp_path):
    events, result, order, calls = _run_agent_with_streaming(tmp_path)

    deltas = [event for event in events if event["event"] == "model_stream_delta"]
    by_attempt: dict[int, str] = {}
    for event in deltas:
        by_attempt[event["attempt"]] = by_attempt.get(event["attempt"], "") + event.get(
            "text", ""
        )
    assert by_attempt == {1: "我先点击", 2: "完成"}
    for event in deltas:
        assert "tap" not in (event.get("text") or "")
        assert "target" not in (event.get("text") or "")

    order_kinds = [item[0] for item in order]
    assert order_kinds.count("tool") == 1
    tool_index = order_kinds.index("tool")
    assert order[tool_index][1] == "ok"
    assert order_kinds[:tool_index] and set(order_kinds[:tool_index]) == {"delta"}
    assert "delta" in order_kinds[tool_index + 1 :]
    chat_calls = [body for body in calls if body.get("stream")]
    assert len(chat_calls) == 2

    ai_messages = [message for message in result["messages"] if message.type == "ai"]
    assert ai_messages[0].tool_calls == [
        {"name": "tap", "args": {"target": "ok"}, "id": "call_1", "type": "tool_call"}
    ]
    assert [message.content for message in result["messages"] if message.type == "tool"] == [
        "已点击 ok"
    ]
    assert ai_messages[-1].content == "完成"

    model_calls = [event for event in events if event["event"] == "model_call"]
    assert [event["tokens"] for event in model_calls] == [17, 17]
    assert model_calls[-1]["tokens_total"] == 34
    assert all("tokens" not in event for event in deltas)
    assert all("usage" not in event for event in deltas)

    assert all(body["stream"] is True for body in chat_calls)
    assert all(
        body["stream_options"] == {"include_usage": True} for body in chat_calls
    )


def test_tool_only_answer_streams_nothing_and_still_executes_once(tmp_path):
    events, result, order, _calls = _run_agent_with_streaming(
        tmp_path, text="", tool_args='{"target":"bare"}'
    )
    deltas = [event for event in events if event["event"] == "model_stream_delta"]
    assert [event.get("text") for event in deltas] == ["完成"]
    assert order[0] == ("tool", "bare")
    ai_messages = [message for message in result["messages"] if message.type == "ai"]
    assert ai_messages[0].tool_calls[0]["args"] == {"target": "bare"}


# --------------------------------------------------- fallback attempt isolation


def test_failed_primary_stream_is_isolated_from_the_fallback_answer(tmp_path):
    from phone_agent.v2.agent import _WrapModelBridgeMiddleware

    sink = _Sink()
    web = WebEventMiddleware(sink, streaming=True)
    primary = _openai_model(_openai_transport(text="半截失败", fail_after=2))
    fallback = _openai_model(
        _openai_transport(text="备用答案", tool_name="", usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
    )
    config = SimpleNamespace(
        model_name="primary-model",
        _model_fallback_recorder=None,
        _model_fallbacks=[],
    )
    bridge = _WrapModelBridgeMiddleware(
        EventBus(),
        config=config,
        fallback_provider=lambda: (fallback, "fake:fallback"),
        primary_ref="fake:primary",
    )
    agent = create_agent(primary, tools=[], middleware=[bridge, web])
    result = agent.invoke({"messages": [{"role": "user", "content": "go"}]})

    deltas = [event for event in sink.events if event["event"] == "model_stream_delta"]
    by_attempt: dict[int, str] = {}
    for event in deltas:
        by_attempt[event["attempt"]] = by_attempt.get(event["attempt"], "") + event.get("text", "")
    assert by_attempt == {1: "半截失败", 2: "备用答案"}

    ends = [event for event in sink.events if event["event"] == "model_stream_end"]
    assert [(event["attempt"], event["ok"]) for event in ends] == [(1, False), (2, True)]
    assert ends[0]["error"] == "ReadError"

    final = [message for message in result["messages"] if message.type == "ai"][-1]
    assert final.content == "备用答案"
    assert "半截失败" not in final.content

    calls = [event for event in sink.events if event["event"] == "model_call"]
    assert calls[0]["error"] is not None and calls[0]["tokens"] == 0
    assert calls[-1]["tokens"] == 7


# --------------------------------------------------------- per-protocol evidence


def _anthropic_events() -> list[tuple[str, dict]]:
    def block(index: int, kind: str, payload: dict) -> tuple[str, dict]:
        return f"content_block_{kind}", {
            "type": f"content_block_{kind}",
            "index": index,
            **payload,
        }

    return [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-fake",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                },
            },
        ),
        block(0, "start", {"content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "想一下"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        block(1, "start", {"content_block": {"type": "text", "text": ""}}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "你"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "好"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        block(
            2,
            "start",
            {"content_block": {"type": "tool_use", "id": "tu1", "name": "tap", "input": {}}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": "1}"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def test_anthropic_stream_wire_and_aggregated_message(monkeypatch):
    from phone_agent.v2.providers import build_model_from_resolved
    from phone_agent.v2.providers.types import ModelSpec, ProviderSpec

    seen: dict[str, Any] = {}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        body = "".join(
            f"event: {name}\ndata: {json.dumps(payload)}\n\n"
            for name, payload in _anthropic_events()
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )

    import langchain_anthropic.chat_models as chat_models

    def fake_client(base_url=None, timeout=None, **_kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(anthropic_handler), timeout=timeout
        )

    monkeypatch.setattr(chat_models, "_get_default_httpx_client", fake_client)
    model_spec = ModelSpec(id="claude-fake")
    provider = ProviderSpec(
        id="acme",
        api="anthropic-messages",
        base_url="http://anthropic.test",
        api_key="k",
        models={"claude-fake": model_spec},
    )
    model = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model_spec, ref="acme:claude-fake"),
        SimpleNamespace(
            model_timeout=5.0,
            model_max_retries=0,
            sampling=None,
            thinking="",
            parallel_tool_calls=False,
            streaming="on",
        ),
    )
    assert model.stream_usage is True
    assert model.streaming is True

    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    message = model_with_stream_observer(model, observer).invoke("hi")

    assert seen["body"]["stream"] is True
    assert message.tool_calls == [
        {"name": "tap", "args": {"x": 1}, "id": "tu1", "type": "tool_call"}
    ]
    assert message.usage_metadata["output_tokens"] == 9
    deltas = [payload for name, payload in events if name == "model_stream_delta"]
    assert "".join(payload.get("text", "") for payload in deltas) == "你好"
    assert "".join(payload.get("reasoning", "") for payload in deltas) == "想一下"
    assert [payload["attempt"] for payload in deltas] == [1] * len(deltas)


def test_google_stream_wire_and_aggregated_message():
    import google.genai as genai
    from google.genai import types as gtypes

    from phone_agent.v2.providers import build_model_from_resolved
    from phone_agent.v2.providers.types import ModelSpec, ProviderSpec

    seen: dict[str, Any] = {}

    def google_handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)

        def response(parts, usage=None, finish=None):
            payload: dict[str, Any] = {
                "candidates": [
                    {"content": {"parts": parts, "role": "model"}, "index": 0}
                ],
                "modelVersion": "gemini-fake",
            }
            if finish:
                payload["candidates"][0]["finishReason"] = finish
            if usage:
                payload["usageMetadata"] = usage
            return payload

        chunks = [
            response([{"text": "你"}]),
            response([{"text": "好"}]),
            response([{"functionCall": {"name": "tap", "args": {"x": 1}}}]),
            response(
                [],
                usage={
                    "promptTokenCount": 7,
                    "candidatesTokenCount": 9,
                    "totalTokenCount": 16,
                },
                finish="STOP",
            ),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks),
        )

    model_spec = ModelSpec(id="gemini-fake")
    provider = ProviderSpec(
        id="acme",
        api="google-generative-ai",
        base_url="http://google.test",
        api_key="k",
        models={"gemini-fake": model_spec},
    )
    model = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=model_spec, ref="acme:gemini-fake"),
        SimpleNamespace(
            model_timeout=5.0,
            model_max_retries=0,
            sampling=None,
            thinking="",
            parallel_tool_calls=False,
            streaming="on",
        ),
    )
    assert model.streaming is True
    model.client = genai.Client(
        api_key="k",
        http_options=gtypes.HttpOptions(
            api_version="v1beta",
            base_url="http://google.test",
            httpx_client=httpx.Client(transport=httpx.MockTransport(google_handler)),
        ),
    )

    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    message = model_with_stream_observer(model, observer).invoke("hi")

    assert ":streamGenerateContent" in seen["url"]
    assert message.tool_calls[0]["name"] == "tap"
    assert message.tool_calls[0]["args"] == {"x": 1}
    assert message.usage_metadata["input_tokens"] == 7
    assert message.usage_metadata["output_tokens"] == 9
    deltas = [payload for name, payload in events if name == "model_stream_delta"]
    assert "".join(payload.get("text", "") for payload in deltas) == "你好"


# ------------------------------------------------------- split-secret privacy


@pytest.mark.parametrize(
    ("parts", "secret"),
    [
        (["sk-", "live0000secret"], "sk-live0000secret"),
        (["138", "00138000"], "13800138000"),
        (["user@exa", "mple.com"], "user@example.com"),
        (["Bearer ", "abc123"], "Bearer abc123"),
        (["api_key: ", "AKIA1234567890"], "api_key: AKIA1234567890"),
    ],
)
def test_secrets_split_across_chunks_never_leave_in_pieces(parts, secret):
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    for part in parts:
        observer.on_llm_new_token("", chunk=AIMessageChunk(content=part))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "")
        for name, payload in events
        if name == "model_stream_delta"
    )
    assert secret not in emitted
    assert parts[-1] not in emitted
    assert "<redacted>" in emitted


def test_split_base64_run_is_replaced_as_a_whole():
    secret = "QUJD" * 33
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    for part in (secret[:40], secret[40:90], secret[90:]):
        observer.on_llm_new_token("", chunk=AIMessageChunk(content=part))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "")
        for name, payload in events
        if name == "model_stream_delta"
    )
    assert secret not in emitted
    for start in range(0, len(secret) - 60, 30):
        assert secret[start : start + 60] not in emitted
    assert "<redacted>" in emitted


def test_unbounded_run_stays_bounded_and_loses_no_piece_to_the_stream():
    secret = "Q" * 600
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    observer.on_llm_new_token("", chunk=AIMessageChunk(content=secret))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "")
        for name, payload in events
        if name == "model_stream_delta"
    )
    assert "Q" * 20 not in emitted
    assert "<redacted>" in emitted
    assert len(emitted) < 200


def test_settled_text_is_still_emitted_around_a_secret():
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="已点击「设置」，手机号"))
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="138"))
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="00138000"))
    observer.on_llm_new_token("", chunk=AIMessageChunk(content="，继续。"))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "")
        for name, payload in events
        if name == "model_stream_delta"
    )
    assert "13800138000" not in emitted
    assert "已点击「设置」" in emitted
    assert "继续。" in emitted


def test_redaction_holdback_never_cuts_a_complete_phone_number():
    from phone_agent.v2.middleware._redact import redact_text

    parts = ["中文" * 60 + "13800138000" + "已完成。" * 6, "结束"]
    events = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0,
    )
    for part in parts:
        observer.on_llm_new_token("", chunk=AIMessageChunk(content=part))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "") for name, payload in events
        if name == "model_stream_delta"
    )
    assert emitted == redact_text("".join(parts))


def test_long_secret_does_not_discard_the_following_safe_text():
    events = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0,
    )
    for part in ("Q" * 600, " 继续显示安全结果。"):
        observer.on_llm_new_token("", chunk=AIMessageChunk(content=part))
    observer.on_llm_end(None)
    emitted = "".join(
        payload.get("text", "") for name, payload in events
        if name == "model_stream_delta"
    )
    assert emitted == "<redacted> 继续显示安全结果。"


# ----------------------------------------------------------- Responses protocol


def _responses_transport(*, calls: list[dict[str, Any]] | None = None):
    """MockTransport handler for the OpenAI Responses streaming wire."""

    def events() -> str:
        seq = [0]

        def event(kind: str, payload: dict) -> str:
            payload = {"type": kind, "sequence_number": seq[0], **payload}
            seq[0] += 1
            return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

        completed = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "fake-r",
            "output": [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "先看屏幕"}],
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "点击", "annotations": []}],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "name": "tap",
                    "arguments": '{"target":"ok"}',
                    "call_id": "call_1",
                    "status": "completed",
                },
            ],
            "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
        }
        return "".join(
            [
                event(
                    "response.created",
                    {
                        "response": {
                            "id": "resp_1",
                            "object": "response",
                            "status": "in_progress",
                            "output": [],
                            "model": "fake-r",
                        }
                    },
                ),
                event(
                    "response.output_item.added",
                    {"output_index": 0, "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
                ),
                event(
                    "response.reasoning_summary_part.added",
                    {
                        "output_index": 0,
                        "summary_index": 0,
                        "item_id": "rs_1",
                        "part": {"type": "summary_text", "text": ""},
                    },
                ),
                event(
                    "response.reasoning_summary_text.delta",
                    {
                        "output_index": 0,
                        "summary_index": 0,
                        "item_id": "rs_1",
                        "delta": "先看屏幕",
                    },
                ),
                event(
                    "response.output_item.added",
                    {
                        "output_index": 1,
                        "item": {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                ),
                event(
                    "response.output_text.delta",
                    {
                        "output_index": 1,
                        "item_id": "msg_1",
                        "content_index": 0,
                        "delta": "点击",
                    },
                ),
                event(
                    "response.output_item.added",
                    {
                        "output_index": 2,
                        "item": {
                            "id": "fc_1",
                            "type": "function_call",
                            "name": "tap",
                            "arguments": "",
                            "call_id": "call_1",
                            "status": "in_progress",
                        },
                    },
                ),
                event(
                    "response.function_call_arguments.delta",
                    {"output_index": 2, "item_id": "fc_1", "delta": '{"target":'},
                ),
                event(
                    "response.function_call_arguments.delta",
                    {"output_index": 2, "item_id": "fc_1", "delta": '"ok"}'},
                ),
                event("response.completed", {"response": completed}),
            ]
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content.decode()))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=events()
        )

    return handler


def test_responses_stream_summary_text_and_tool_arguments():
    calls: list[dict[str, Any]] = []
    model = _openai_model(
        _responses_transport(calls=calls), use_responses_api=True, streaming=True
    )
    events: list[tuple[str, dict]] = []
    observer = ModelStreamObserver(
        lambda name, payload: events.append((name, payload)),
        lambda: 1,
        flush_chars=1,
        flush_seconds=0.0,
    )
    message = model_with_stream_observer(model, observer).invoke("hi")

    assert "/responses" in str(calls[0].get("model", "")) or calls[0].get("stream") is True
    assert message.tool_calls == [
        {"name": "tap", "args": {"target": "ok"}, "id": "call_1", "type": "tool_call"}
    ]
    assert message.usage_metadata["total_tokens"] == 13

    deltas = [payload for name, payload in events if name == "model_stream_delta"]
    reasoning = "".join(payload.get("reasoning", "") for payload in deltas)
    text = "".join(payload.get("text", "") for payload in deltas)
    assert reasoning == "先看屏幕"
    assert text == "点击"
    assert "target" not in text and "target" not in reasoning
    assert "encrypted" not in json.dumps(deltas, ensure_ascii=False)


def test_responses_chunk_shapes_are_displayed_only_when_provided():
    text, reasoning = extract_display_text(
        AIMessageChunk(
            content=[
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "摘要"},
                        {"type": "other", "text": "忽略"},
                    ],
                    "encrypted_content": "gAAAAAB-secret-blob",
                },
                {"type": "output_text", "text": "正文"},
                {"type": "function_call", "arguments": '{"target":"ok"}', "index": 2},
            ]
        )
    )
    assert text == "正文"
    assert reasoning == "摘要"


# ------------------------------------------------------------- decision gating


def _request_double(model: Any) -> Any:
    class _Request:
        def __init__(self, inner: Any) -> None:
            self.model = inner
            self.overridden = False

        def override(self, **kwargs: Any) -> Any:
            new = _Request(kwargs.get("model", self.model))
            new.overridden = True
            return new

    return _Request(model)


def test_middleware_never_forces_streaming_on_a_model_declared_off():
    from phone_agent.v2.providers import build_model_from_resolved

    sink = _Sink()
    middleware = WebEventMiddleware(sink, streaming=True)
    fallback = _openai_model(_openai_transport(), streaming=False)
    middleware.set_streaming(True, inactive_models=(fallback,))
    request = _request_double(fallback)
    assert middleware._streamed_request(request) is request

    streaming = _openai_model(_openai_transport())
    gated = middleware._streamed_request(_request_double(streaming))
    assert gated is not None and gated.overridden is True
    assert gated.model is not streaming

    declared_off = ModelSpec(id="m1", streaming="off")
    provider = ProviderSpec(
        id="acme",
        api="openai-completions",
        base_url="http://fake.test/v1",
        api_key="k",
        models={"m1": declared_off},
    )
    built = build_model_from_resolved(
        SimpleNamespace(provider=provider, model=declared_off, ref="acme:m1"),
        SimpleNamespace(
            base_url="http://fake.test/v1",
            model_name="m1",
            api_key="k",
            model_timeout=5.0,
            model_max_retries=0,
            sampling=None,
            thinking="",
            parallel_tool_calls=False,
            streaming="on",
        ),
    )
    assert built.streaming is False
    assert middleware._streamed_request(_request_double(built)).model is built


def test_fallback_streaming_on_is_observed_even_when_the_primary_is_off():
    sink = _Sink()
    middleware = WebEventMiddleware(sink, streaming=False)
    fallback = _openai_model(_openai_transport(), streaming=True)
    request = _request_double(fallback)
    observed = middleware._streamed_request(request)
    assert observed is not request
    assert observed.model is not fallback
    message = observed.model.invoke("go")
    assert message.tool_calls[0]["args"] == {"target": "ok"}
    assert sink.kinds("model_stream_delta")
    assert sink.kinds("model_stream_end")[-1]["ok"] is True


def test_fallback_models_declared_off_are_reported_inactive(tmp_path):
    from dataclasses import replace

    from phone_agent.v2.config import V2Config
    from phone_agent.v2.model import build_actor_fallback, streaming_inactive_models
    from phone_agent.v2.providers import build_provider_registry

    models_file = tmp_path / "models.json"
    models_file.write_text(
        json.dumps(
            {
                "providers": {
                    "gateway": {
                        "api": "openai-completions",
                        "baseUrl": "http://fake.test/v1",
                        "models": [
                            {"id": "main-model"},
                            {"id": "backup-off", "streaming": "off"},
                            {"id": "backup-on", "streaming": "on"},
                        ],
                    }
                },
                "roles": {"actor": {"streaming": "on"}},
            }
        ),
        encoding="utf-8",
    )
    config = V2Config.from_env(
        {
            "base_url": "http://fake.test/v1",
            "model_name": "main-model",
            "api_key": "k",
            "models_file": str(models_file),
            "streaming": "off",
        }
    )

    def build(fallback_ref: str):
        active = replace(config, fallback_model=fallback_ref)
        return build_actor_fallback(
            active,
            registry=build_provider_registry(active),
            primary_ref="main-model",
        )

    assert streaming_inactive_models(None) == ()
    off = build("backup-off")
    assert off is not None and streaming_inactive_models(off) == (off[0],)
    on = build("backup-on")
    assert on is not None and streaming_inactive_models(on) == ()


# ------------------------------------------------------------- web projection


def test_bridge_keeps_a_real_bounded_tail_with_totals(tmp_path):
    bridge = WebRunBridge(config_factory=lambda _overrides: _bridge_config(tmp_path))
    bridge._apply_event({"event": "model_stream_start", "step": 1, "attempt": 1})
    pieces = ["A" * 9000, "B" * 9000, "C" * 7000]
    for piece in pieces:
        bridge._apply_event(
            {"event": "model_stream_delta", "step": 1, "attempt": 1, "text": piece}
        )
    record = bridge.snapshot()["stream_attempts"][0]
    joined = "".join(pieces)
    assert record["chars_total"] == len(joined)
    assert record["revision"] == len(pieces)
    assert record["text_truncated"] is True
    assert record["text"] == joined[-20000:]
    assert record["text"].endswith("C" * 7000)
    tail = _stream_tail(record["text"])
    assert tail.startswith("…")
    assert tail.endswith("C" * 2400)


def test_stream_selection_follows_latest_until_pinned():
    attempts = [
        {"attempt": 1, "status": "failed", "text": "半截", "reasoning": ""},
        {"attempt": 2, "status": "done", "text": "备用", "reasoning": ""},
    ]
    record, attempt = _stream_selection(attempts, attempt=None, follow=True)
    assert (attempt, record["attempt"]) == (2, 2)

    record, attempt = _stream_selection(attempts, attempt=1, follow=False)
    assert (attempt, record["attempt"]) == (1, 1)

    attempts.append({"attempt": 3, "status": "streaming", "text": "新的", "reasoning": ""})
    record, attempt = _stream_selection(attempts, attempt=None, follow=True)
    assert (attempt, record["attempt"]) == (3, 3)

    record, attempt = _stream_selection(attempts, attempt=1, follow=False)
    assert (attempt, record["attempt"]) == (1, 1)

    record, attempt = _stream_selection(attempts, attempt=9, follow=False)
    assert (attempt, record["attempt"]) == (3, 3)


def test_bridge_clears_stream_state_for_a_new_run(tmp_path):
    bridge = WebRunBridge(config_factory=lambda _overrides: _bridge_config(tmp_path))
    bridge._apply_event({"event": "model_stream_start", "step": 1, "attempt": 1})
    bridge._apply_event(
        {"event": "model_stream_delta", "step": 1, "attempt": 1, "text": "旧"}
    )
    assert bridge.snapshot()["stream_attempts"] != []
    bridge._reset_state()
    assert bridge.snapshot()["stream_attempts"] == []


def test_bridge_projects_attempts_and_keeps_them_separate(tmp_path):
    bridge = WebRunBridge(config_factory=lambda _overrides: _bridge_config(tmp_path))
    bridge._apply_event({"event": "model_stream_start", "step": 3, "attempt": 1})
    bridge._apply_event(
        {"event": "model_stream_delta", "step": 3, "attempt": 1, "text": "半截"}
    )
    bridge._apply_event(
        {"event": "model_stream_end", "step": 3, "attempt": 1, "ok": False, "error": "ReadError"}
    )
    bridge._apply_event({"event": "model_stream_start", "step": 3, "attempt": 2})
    bridge._apply_event(
        {"event": "model_stream_delta", "step": 3, "attempt": 2, "text": "备用"}
    )
    bridge._apply_event({"event": "model_stream_end", "step": 3, "attempt": 2, "ok": True})
    bridge._apply_event({"event": "model_call", "step": 3, "latency_ms": 5, "tokens": 7})

    attempts = bridge.snapshot()["stream_attempts"]
    assert [record["attempt"] for record in attempts] == [1, 2]
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error"] == "ReadError"
    assert attempts[0]["text"] == "半截"
    assert attempts[1]["status"] == "done"
    assert attempts[1]["text"] == "备用"


def _bridge_config(tmp_path):
    from phone_agent.v2.config import V2Config

    return V2Config(
        base_url="http://example.invalid/v1",
        model_name="fake",
        memory_dir=str(tmp_path / "memory"),
        runs_dir=str(tmp_path / "runs"),
        trace_enabled=False,
        experience_enabled=False,
        memory_rag="off",
    )


def test_drawer_hint_reports_the_effective_streaming_decision(tmp_path):
    from phone_agent.v2.config import V2Config
    from phone_agent.web.app import _streaming_hint

    models_file = tmp_path / "models.json"
    models_file.write_text(
        json.dumps(
            {
                "providers": {
                    "gateway": {
                        "api": "openai-completions",
                        "baseUrl": "http://fake.test/v1",
                        "models": [{"id": "main-model", "streaming": "on"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = V2Config.from_env(
        {
            "base_url": "http://fake.test/v1",
            "model_name": "main-model",
            "api_key": "k",
            "models_file": str(models_file),
            "streaming": "off",
        }
    )
    hint = _streaming_hint(config, switch_on=False)
    assert "on" in hint
    assert "覆盖" in hint

    assert "覆盖" not in _streaming_hint(config, switch_on=True)


# ------------------------------------------------------------------ runner seam


def test_runner_enables_streaming_only_from_the_agent_decision(tmp_path, monkeypatch):
    """Headless/default runs stay non-streaming; the agent decision is the input."""

    from phone_agent.v2 import runner as runner_module
    from phone_agent.v2.run_ipc import (
        RunPaths,
        RunSpec,
        app_kb_generation,
        capability_snapshot,
        config_fingerprint,
        resolved_config_dict,
        write_run_spec,
    )

    seen: list[tuple[bool, tuple[Any, ...]]] = []
    inactive = object()

    class _RecordingMiddleware(WebEventMiddleware):
        def set_streaming(self, enabled: bool, *, inactive_models=()) -> None:  # noqa: ANN001
            seen.append((bool(enabled), tuple(inactive_models)))
            super().set_streaming(enabled, inactive_models=inactive_models)

    class _FakeAgent:
        def __init__(self, config, **kwargs):
            self.config = config
            self.streaming_enabled = True
            self.streaming_inactive_models = (inactive,)
            self.session = SimpleNamespace(takeover_reason=None)
            self.trace_path = None
            self.usage_ledger = None

        def run(self, task, hitl_handler=None):  # noqa: ANN001
            from phone_agent.v2.agent import RunResult

            return RunResult(True, "done", 1, None)

    monkeypatch.setattr(runner_module, "WebEventMiddleware", _RecordingMiddleware)

    config = _bridge_config(tmp_path)
    config_values = resolved_config_dict(config)
    paths = RunPaths.for_run(config.runs_dir, "stream-run")
    paths.run_dir.mkdir(parents=True)
    spec = RunSpec(
        run_id="stream-run",
        task="任务",
        overrides=config_values,
        snapshot={
            "config_fingerprint": config_fingerprint(config_values),
            "memory_generation": app_kb_generation(config),
            "capabilities": capability_snapshot(config),
        },
        events_path=str(paths.events),
        control_path=str(paths.control),
    )
    write_run_spec(paths.spec, spec)

    def factory(config, **kwargs):
        agent = _FakeAgent(config, **kwargs)
        if kwargs.get("run_id") == "no-decision":
            del agent.streaming_enabled
        return agent

    assert runner_module.run_spec(spec, agent_factory=factory) == 0
    assert seen == [(True, (inactive,))]


def test_runner_default_is_off_for_an_agent_without_the_decision(tmp_path, monkeypatch):
    from phone_agent.v2 import runner as runner_module
    from phone_agent.v2.run_ipc import (
        RunPaths,
        RunSpec,
        app_kb_generation,
        capability_snapshot,
        config_fingerprint,
        resolved_config_dict,
        write_run_spec,
    )

    seen: list[bool] = []

    class _RecordingMiddleware(WebEventMiddleware):
        def set_streaming(self, enabled: bool, *, inactive_models=()) -> None:  # noqa: ANN001
            seen.append(bool(enabled))
            super().set_streaming(enabled, inactive_models=inactive_models)

    class _FakeAgent:
        def __init__(self, config, **kwargs):
            self.session = SimpleNamespace(takeover_reason=None)
            self.trace_path = None
            self.usage_ledger = None

        def run(self, task, hitl_handler=None):  # noqa: ANN001
            from phone_agent.v2.agent import RunResult

            return RunResult(True, "done", 1, None)

    monkeypatch.setattr(runner_module, "WebEventMiddleware", _RecordingMiddleware)
    config = _bridge_config(tmp_path)
    config_values = resolved_config_dict(config)
    paths = RunPaths.for_run(config.runs_dir, "plain-run")
    paths.run_dir.mkdir(parents=True)
    spec = RunSpec(
        run_id="plain-run",
        task="任务",
        overrides=config_values,
        snapshot={
            "config_fingerprint": config_fingerprint(config_values),
            "memory_generation": app_kb_generation(config),
            "capabilities": capability_snapshot(config),
        },
        events_path=str(paths.events),
        control_path=str(paths.control),
    )
    write_run_spec(paths.spec, spec)
    assert runner_module.run_spec(spec, agent_factory=_FakeAgent) == 0
    assert seen == [False]
