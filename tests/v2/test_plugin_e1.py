"""WP-E1: event surface foundation bridges.

Pure additions: model/request|post_request, tool/execute, agent/after bridges
plus the JUMP_END sentinel. Existing middleware behavior is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from phone_agent.v2.events import (
    AGENT_AFTER,
    JUMP_END,
    MODEL_POST_REQUEST,
    MODEL_REQUEST,
    REJECT,
    TOOL_EXECUTE,
    EventBus,
)


def _request(name: str = "tap", args: dict | None = None, call_id: str = "call_1"):
    return SimpleNamespace(
        tool_call={"name": name, "args": args or {}, "id": call_id}
    )


def test_tool_execute_bridge_no_listener_is_identity() -> None:
    from phone_agent.v2.agent import _ToolExecuteBridgeMiddleware

    bus = EventBus()
    mw = _ToolExecuteBridgeMiddleware(bus)
    req = _request()
    calls: list[object] = []

    result = mw.wrap_tool_call(req, handler=lambda r: calls.append(r) or "executed")

    assert result == "executed"
    assert calls == [req]


def test_model_request_bridge_no_listener_is_identity() -> None:
    from phone_agent.v2.agent import _WrapModelBridgeMiddleware

    bus = EventBus()
    mw = _WrapModelBridgeMiddleware(bus)
    req = SimpleNamespace(messages=[{"type": "text", "text": "hi"}])
    calls: list[object] = []

    result = mw.wrap_model_call(req, handler=lambda r: calls.append(r) or "model-response")

    assert result == "model-response"
    assert calls == [req]


def test_post_request_bridge_no_listener_is_noop() -> None:
    from phone_agent.v2.agent import _PostRequestBridgeMiddleware

    bus = EventBus()
    mw = _PostRequestBridgeMiddleware(bus, "run-1")
    state = {"messages": [{"type": "text", "text": "hi"}]}

    assert mw.after_model(state, None) is None


def test_agent_after_bridge_no_listener_is_noop() -> None:
    from phone_agent.v2.agent import _AgentAfterBridgeMiddleware

    bus = EventBus()
    mw = _AgentAfterBridgeMiddleware(bus, "run-1")
    state = {"messages": [{"type": "text", "text": "hi"}]}

    assert mw.after_agent(state, None) is None


def test_tool_execute_bridge_emits_event() -> None:
    from phone_agent.v2.agent import _ToolExecuteBridgeMiddleware

    bus = EventBus()
    mw = _ToolExecuteBridgeMiddleware(bus)
    req = _request()
    seen: list[object] = []
    bus.on(TOOL_EXECUTE, lambda payload, next: seen.append(payload) or next(payload))  # noqa: A002

    result = mw.wrap_tool_call(req, handler=lambda _r: "ok")

    assert seen == [req]
    assert result == "ok"


def test_model_request_bridge_emits_event() -> None:
    from phone_agent.v2.agent import _WrapModelBridgeMiddleware

    bus = EventBus()
    mw = _WrapModelBridgeMiddleware(bus)
    req = SimpleNamespace(messages=[{"type": "text", "text": "hi"}])
    seen: list[object] = []
    bus.on(MODEL_REQUEST, lambda payload, next: seen.append(payload) or next(payload))  # noqa: A002

    result = mw.wrap_model_call(req, handler=lambda _r: "ok")

    assert seen == [req]
    assert result == "ok"


def test_post_request_bridge_emits_state_summary() -> None:
    from phone_agent.v2.agent import _PostRequestBridgeMiddleware

    bus = EventBus()
    mw = _PostRequestBridgeMiddleware(bus, "run-1")
    state = {"messages": [{"role": "ai", "content": "hello"}]}
    payloads: list[dict] = []
    bus.on(MODEL_POST_REQUEST, lambda p: payloads.append(p))

    mw.after_model(state, None)

    assert len(payloads) == 1
    assert payloads[0]["run_id"] == "run-1"
    assert payloads[0]["messages"] is state["messages"]


def test_agent_after_bridge_emits_state_summary() -> None:
    from phone_agent.v2.agent import _AgentAfterBridgeMiddleware

    bus = EventBus()
    mw = _AgentAfterBridgeMiddleware(bus, "run-2")
    state = {"messages": [{"role": "ai", "content": "done"}]}
    payloads: list[dict] = []
    bus.on(AGENT_AFTER, lambda p: payloads.append(p))

    mw.after_agent(state, None)

    assert len(payloads) == 1
    assert payloads[0]["run_id"] == "run-2"
    assert payloads[0]["messages"] is state["messages"]


def test_jump_end_sentinel_is_singleton_and_distinct_from_reject() -> None:
    from phone_agent.v2.events import JUMP_END as exported_jump_end

    assert repr(JUMP_END) == "JUMP_END"
    assert JUMP_END is exported_jump_end
    assert JUMP_END is JUMP_END
    assert JUMP_END is not REJECT
    assert JUMP_END != REJECT


def test_tool_execute_bridge_reject_becomes_tool_message() -> None:
    from phone_agent.v2.agent import _ToolExecuteBridgeMiddleware

    bus = EventBus()
    mw = _ToolExecuteBridgeMiddleware(bus)
    req = _request(name="tap", call_id="call_42")
    bus.on(TOOL_EXECUTE, lambda _payload, _next: REJECT)

    result = mw.wrap_tool_call(req, handler=lambda _r: "should-not-run")

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_42"
    assert result.name == "tap"
    assert "已拦截（未执行）" in result.content
    assert "工具调用被策略事件拒绝" in result.content
