"""WP-E4: final middleware retirement end-state assertions.

After E4 the compiled LangChain stack must contain only the four core bridge
middlewares plus any extra_middleware; trace and diagnostic policy behavior must
live on the event bus.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from phone_agent.v2.events import (
    AGENT_AFTER,
    MODEL_PRE_REQUEST,
    MODEL_REQUEST,
    RUN_START,
    TOOL_EXECUTE,
)


# --------------------------------------------------------------------------
# Minimal fake chat model.
# --------------------------------------------------------------------------
class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    i: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted"


@dataclass
class _FakeObservation:
    screenshot_b64: str = "QUJD"
    current_app: str = "com.example.app"
    screen_seq: int = 0
    marks: dict = field(default_factory=dict)


@dataclass
class _FakeSession:
    config: Any = None
    marks: dict = field(default_factory=dict)
    screen_seq: int = 0
    launched_apps: list = field(default_factory=list)
    finished: bool = False

    def observe(self) -> _FakeObservation:
        self.screen_seq += 1
        return _FakeObservation(screen_seq=self.screen_seq)


@dataclass
class _FakeConfig:
    lang: str = "cn"
    max_model_calls: int = 20
    trace_dir: str = ".traces"
    trace_enabled: bool = True
    diagnostic_evidence: bool = True
    diagnostic_evidence_dir: str = "outputs/live-diagnosis/.evidence"
    diagnostic_unredacted: bool = False
    safety_mode: str = "wary"
    compact_enabled: bool = False
    taskdoc_enabled: bool = False
    app_kb_enabled: bool = False
    finish_verify: str = "off"


def _ai_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


@pytest.fixture
def captured_agent(tmp_path, monkeypatch):
    """Build a ThinPhoneAgent and capture the middleware passed to create_agent."""

    from langchain.agents import create_agent as _orig_create_agent

    captured: dict[str, Any] = {"middleware": None}

    def _create_agent(model, *, tools, middleware, checkpointer):
        captured["middleware"] = list(middleware)
        return _orig_create_agent(
            model, tools=tools, middleware=middleware, checkpointer=checkpointer
        )

    monkeypatch.setattr("langchain.agents.create_agent", _create_agent)

    session = _FakeSession()
    model = _ScriptedModel(
        responses=[
            _ai_call("finish", {"summary": "done", "evidence": ["ok"]}, "c1"),
            AIMessage(content="完成"),
        ]
    )

    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")

    def _build_tools(sess, config):
        from langchain_core.tools import tool

        @tool
        def finish(summary: str, evidence: list[str]) -> str:
            """Declare the task finished."""
            sess.finished = True
            return "已记录完成声明"

        return [finish]

    tools_mod.build_tools = _build_tools
    prompts_mod = types.ModuleType("phone_agent.v2.prompts")
    prompts_mod.get_system_prompt = lambda lang="cn": "你是手机智能体。"

    for name, mod in [
        ("phone_agent.v2.model", model_mod),
        ("phone_agent.v2.session", session_mod),
        ("phone_agent.v2.tools", tools_mod),
        ("phone_agent.v2.prompts", prompts_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    from phone_agent.v2.agent import ThinPhoneAgent

    config = _FakeConfig(trace_dir=str(tmp_path))
    agent = ThinPhoneAgent(config)
    return agent, captured


def test_create_agent_middleware_is_bridges_only(captured_agent):
    agent, captured = captured_agent
    middleware = captured["middleware"]

    from phone_agent.v2.agent import (
        _AgentAfterBridgeMiddleware,
        _ModelPreRequestBridgeMiddleware,
        _PostRequestBridgeMiddleware,
        _ToolExecuteBridgeMiddleware,
        _WrapModelBridgeMiddleware,
    )

    core_types = [
        _ToolExecuteBridgeMiddleware,
        _ModelPreRequestBridgeMiddleware,
        _WrapModelBridgeMiddleware,
        _PostRequestBridgeMiddleware,
        _AgentAfterBridgeMiddleware,
    ]
    assert len(middleware) == len(core_types)
    for expected, actual in zip(core_types, middleware):
        assert isinstance(actual, expected)


def _is_bound_method(listener, obj, name: str) -> bool:
    return (
        getattr(listener, "__self__", None) is obj
        and getattr(listener, "__func__", None) is getattr(type(obj), name, None)
    )


def test_trace_is_outermost_model_request_listener(captured_agent):
    agent, _ = captured_agent
    listeners = agent.event_bus._listeners.get(MODEL_REQUEST, [])
    assert listeners
    assert _is_bound_method(listeners[0], agent._trace, "on_model_request")


def test_tool_execute_chain_order(captured_agent):
    agent, _ = captured_agent
    listeners = agent.event_bus._listeners.get(TOOL_EXECUTE, [])
    safety = agent._capability_ctx.service("_safety_warning_listener")
    names = []
    for listener in listeners:
        if _is_bound_method(listener, agent._trace, "on_tool_execute"):
            names.append("trace")
        elif agent._diagnostic is not None and _is_bound_method(
            listener, agent._diagnostic, "on_tool_execute"
        ):
            names.append("diagnostic")
        elif listener is agent._control_hitl:
            names.append("hitl")
        elif listener is safety:
            names.append("safety")
        else:
            names.append(type(listener).__name__)

    assert names[:4] == ["trace", "diagnostic", "hitl", "safety"]


def test_diagnostic_listeners_registered(captured_agent):
    agent, _ = captured_agent
    assert agent._diagnostic is not None
    bus = agent.event_bus
    assert any(
        _is_bound_method(listener, agent._diagnostic, "on_run_start")
        for listener in bus._listeners.get(RUN_START, [])
    )
    assert any(
        _is_bound_method(listener, agent._diagnostic, "on_pre_request")
        for listener in bus._listeners.get(MODEL_PRE_REQUEST, [])
    )
    assert any(
        _is_bound_method(listener, agent._diagnostic, "on_model_request")
        for listener in bus._listeners.get(MODEL_REQUEST, [])
    )
    assert any(
        _is_bound_method(listener, agent._diagnostic, "on_agent_after")
        for listener in bus._listeners.get(AGENT_AFTER, [])
    )


def test_extra_middleware_appended_after_bridges(tmp_path, monkeypatch):
    from langchain.agents import create_agent as _orig_create_agent

    captured: dict[str, Any] = {"middleware": None}

    def _create_agent(model, *, tools, middleware, checkpointer):
        captured["middleware"] = list(middleware)
        return _orig_create_agent(
            model, tools=tools, middleware=middleware, checkpointer=checkpointer
        )

    monkeypatch.setattr("langchain.agents.create_agent", _create_agent)

    session = _FakeSession()
    model = _ScriptedModel(responses=[AIMessage(content="完成")])

    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")
    tools_mod.build_tools = lambda sess, config: []
    prompts_mod = types.ModuleType("phone_agent.v2.prompts")
    prompts_mod.get_system_prompt = lambda lang="cn": "你是手机智能体。"

    for name, mod in [
        ("phone_agent.v2.model", model_mod),
        ("phone_agent.v2.session", session_mod),
        ("phone_agent.v2.tools", tools_mod),
        ("phone_agent.v2.prompts", prompts_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    from langchain.agents.middleware import AgentMiddleware
    from phone_agent.v2.agent import ThinPhoneAgent

    class _ExtraObserver(AgentMiddleware):
        pass

    config = _FakeConfig(trace_dir=str(tmp_path))
    extra = [_ExtraObserver()]
    agent = ThinPhoneAgent(config, extra_middleware=extra)
    middleware = captured["middleware"]

    from phone_agent.v2.agent import _AgentAfterBridgeMiddleware

    assert isinstance(middleware[-1], _ExtraObserver)
    assert isinstance(middleware[-2], _AgentAfterBridgeMiddleware)
