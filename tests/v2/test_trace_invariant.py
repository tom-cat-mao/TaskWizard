"""C4 trace invariant: every model-visible tool result is recorded (WP-D2).

This file runs a headless thin-loop smoke test with a scripted fake model and a
safety warning that short-circuits a ``tap`` call.  It then parses the produced
JSONL trace and asserts that:

(a) every ``tool_result`` event has a matching ``tool_call`` event;
(b) the safety warning result itself appears in the trace even though the real
    tool handler was never executed.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool


def _ai_tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
        ],
    )


class _ScriptedModel(BaseChatModel):
    """Replays a fixed list of AIMessages, one per model call."""

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
        return "scripted-tool-model"


@dataclass
class _FakeObservation:
    screenshot_b64: str = "QUJD"  # "ABC"
    width: int = 1080
    height: int = 2400
    current_app: str = "com.android.settings"
    screen_seq: int = 0
    marks: dict = field(default_factory=dict)


@dataclass
class _FakeSession:
    config: Any = None
    marks: dict = field(default_factory=dict)
    screen_seq: int = 0
    finished: bool = False
    finish_summary: str | None = None
    takeover_reason: str | None = None
    taps: list = field(default_factory=list)

    def observe(self) -> _FakeObservation:
        self.screen_seq += 1
        return _FakeObservation(screen_seq=self.screen_seq)


def _build_fake_tools(session: _FakeSession) -> list:
    @tool
    def tap(
        target_mark_id: str | None = None,
        target_description: str | None = None,
    ) -> str:
        """Tap a UI element by mark id or natural-language description."""
        session.taps.append(target_mark_id or target_description)
        return "OK. tapped"

    @tool
    def finish(summary: str, evidence: list[str]) -> str:
        """Declare the task finished."""
        if not evidence:
            return "error: evidence must be non-empty"
        session.finished = True
        session.finish_summary = summary
        return "已记录完成声明"

    return [tap, finish]


@pytest.fixture
def warned_agent(tmp_path, monkeypatch):
    """Assemble a headless agent whose first tool call is blocked by safety."""

    session = _FakeSession()

    responses = [
        # A hard-gated tap: commit term + irreversible object -> warning flow.
        _ai_tool_call(
            "tap",
            {"target_description": "确认支付", "intent": "完成支付"},
            "c1",
        ),
        # The model recovers and finishes after reading the warning.
        _ai_tool_call(
            "finish",
            {"summary": "已拦截支付", "evidence": ["安全警告已记录"]},
            "c2",
        ),
        AIMessage(content="任务结束"),
    ]
    model = _ScriptedModel(responses=responses)

    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config, *args, **kwargs: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")
    tools_mod.build_tools = lambda sess, config: _build_fake_tools(sess)
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

    config = SimpleNamespace(
        lang="cn",
        max_model_calls=20,
        trace_dir=str(tmp_path),
        trace_enabled=True,
        taskdoc_enabled=False,
        app_kb_enabled=False,
        deliverable_enabled=False,
        experience_enabled=False,
        finish_verify="off",
        memory_rag="off",
        compact_enabled=False,
    )
    agent = ThinPhoneAgent(config)
    return agent, session


def test_trace_pairs_every_tool_result_with_a_tool_call(warned_agent) -> None:
    """C4(a): no tool_result appears in the trace without its tool_call."""

    agent, _session = warned_agent
    result = agent.run("支付", hitl_handler=lambda prompt: "approve")

    assert result.trace_path is not None
    trace_path = Path(result.trace_path)
    assert trace_path.exists()

    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]

    tool_calls = {
        (e["step"], e["tool"]) for e in events if e["event"] == "tool_call"
    }
    tool_results = [
        (e["step"], e["tool"]) for e in events if e["event"] == "tool_result"
    ]

    assert tool_calls, "expected at least one tool_call event"
    assert tool_results, "expected at least one tool_result event"

    for step, tool_name in tool_results:
        assert (step, tool_name) in tool_calls, (
            f"orphan tool_result: step={step} tool={tool_name}"
        )


def test_trace_records_safety_warning_result(warned_agent) -> None:
    """C4(b): a safety-short-circuited warning is still written to the trace."""

    agent, session = warned_agent
    result = agent.run("支付", hitl_handler=lambda prompt: "approve")

    # The tap was intercepted, so the real device action must not have run.
    assert session.taps == []

    trace_path = Path(result.trace_path)
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]

    warning_events = [
        e
        for e in events
        if e["event"] == "tool_result"
        and e["tool"] == "tap"
        and "已拦截" in str(e.get("result") or "")
    ]
    assert warning_events, "safety warning result missing from trace"

    # The warning must be paired with the original tool_call.
    calls = {
        (e["step"], e["tool"]) for e in events if e["event"] == "tool_call"
    }
    for warning in warning_events:
        assert (warning["step"], warning["tool"]) in calls
