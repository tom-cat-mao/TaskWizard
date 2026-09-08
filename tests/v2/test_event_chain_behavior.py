"""WP-F1 behavioural guards for the v2 event-chain nesting.

``tests/v2/test_plugin_e4.py`` pins the *mechanism* (which listener sits at which
index of ``event_bus._listeners``). This file pins the *guarantee* the mechanism
exists for: it assembles a real ``ThinPhoneAgent`` with fakes and asserts
observable behaviour, so moving a registration statement in
``ThinPhoneAgent._register_event_chain_pre_assembly`` / ``..._post_assembly``
turns these tests red even if the white-box assertions are updated to match.

Covered guarantees:
  (a) a safety-blocked call is still traced as a (tool_call, tool_result) pair
      and never reaches the device — requires trace OUTSIDE safety;
  (b) on a T2 fold the context pruner runs exactly once (inside compact, before
      the fold) and the TaskDoc board is still pinned onto the rebuilt
      transcript — requires compact OUTSIDE taskdoc on ``model/pre_request``.

All fakes: no device, no network, no MLX.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
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


def _ai_tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


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
    task_doc: Any = None

    def observe(self) -> _FakeObservation:
        self.screen_seq += 1
        return _FakeObservation(screen_seq=self.screen_seq)


@dataclass
class _FakeTaskItem:
    id: str
    content: str
    status: str = "pending"


@dataclass
class _FakeTaskDoc:
    goal_base: str = "打开设置并连上 WLAN"
    items: list = field(default_factory=list)
    facts: list = field(default_factory=list)

    def render(self, lang: str = "cn") -> str:  # noqa: ARG002
        if not (self.goal_base or self.items or self.facts):
            return ""
        lines = ["## 目标", f"base: {self.goal_base}"]
        if self.items:
            lines.append("## 路线")
            lines.extend(
                f"- [{item.status}] {item.id}: {item.content}" for item in self.items
            )
        if self.facts:
            lines.append("## 关键事实")
            lines.extend(f"- {fact}" for fact in self.facts)
        return "\n".join(lines)


def _install_fake_modules(monkeypatch, session: _FakeSession, model, tools_builder):
    """Inject the duck-typed v2 modules the agent imports lazily."""

    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config, *args, **kwargs: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")
    tools_mod.build_tools = tools_builder
    prompts_mod = types.ModuleType("phone_agent.v2.prompts")
    prompts_mod.get_system_prompt = lambda lang="cn": "你是手机智能体。"
    for name, mod in (
        ("phone_agent.v2.model", model_mod),
        ("phone_agent.v2.session", session_mod),
        ("phone_agent.v2.tools", tools_mod),
        ("phone_agent.v2.prompts", prompts_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


# --------------------------------------------------------------------------
# (a) blocked-call trace pairing
# --------------------------------------------------------------------------
@pytest.fixture
def wary_agent(tmp_path, monkeypatch):
    """A ``wary``-mode agent whose first tool call is safety-blocked."""

    session = _FakeSession()
    model = _ScriptedModel(
        responses=[
            # Commit term + irreversible object -> hard gate -> warning flow.
            _ai_tool_call(
                "tap",
                {"target_description": "确认支付", "intent": "完成支付"},
                "c1",
            ),
            _ai_tool_call(
                "finish",
                {"summary": "已拦截支付", "evidence": ["安全警告已记录"]},
                "c2",
            ),
            AIMessage(content="任务结束"),
        ]
    )

    def _build_tools(sess: _FakeSession, config):  # noqa: ANN001
        @tool
        def tap(
            target_mark_id: str | None = None,
            target_description: str | None = None,
        ) -> str:
            """Tap a UI element by mark id or natural-language description."""
            sess.taps.append(target_mark_id or target_description)
            return "OK. tapped"

        @tool
        def finish(summary: str, evidence: list[str]) -> str:
            """Declare the task finished."""
            if not evidence:
                return "error: evidence must be non-empty"
            sess.finished = True
            sess.finish_summary = summary
            return "已记录完成声明"

        return [tap, finish]

    _install_fake_modules(monkeypatch, session, model, _build_tools)

    from phone_agent.v2.agent import ThinPhoneAgent

    config = types.SimpleNamespace(
        lang="cn",
        max_model_calls=20,
        trace_dir=str(tmp_path),
        trace_enabled=True,
        safety_mode="wary",
        compact_enabled=False,
        taskdoc_enabled=False,
        app_kb_enabled=False,
        deliverable_enabled=False,
        experience_enabled=False,
        finish_verify="off",
        memory_rag="off",
    )
    return ThinPhoneAgent(config), session


def _trace_events(agent) -> list[dict]:
    path = Path(agent.trace_path)
    assert path.exists()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_blocked_call_is_traced_as_a_pair(wary_agent):
    """(a) A safety-blocked tap: traced tool_call + warning tool_result, no tap.

    WHY THIS TURNS RED WHEN THE NESTING IS FLIPPED
    ----------------------------------------------
    The safety listener short-circuits: it returns the warning ``ToolMessage``
    *without* calling ``next``. Trace is the outermost ``tool/execute`` listener,
    so it has already written the ``tool_call`` record when the warning comes
    back and it writes the warning as the paired ``tool_result``. With safety
    registered outermost (i.e. before trace), safety returns before trace is ever
    entered: neither record is written, ``tap_calls``/``tap_results`` are both
    empty and this test fails — which is exactly the regression the white-box
    assertions in ``tests/v2/test_plugin_e4.py::test_tool_execute_chain_order``
    cannot catch, because they are updated together with the registration order.
    (Same invariant, coarser: ``tests/v2/test_trace_invariant.py``.)
    """

    agent, session = wary_agent
    result = agent.run("支付", hitl_handler=lambda prompt: "approve")

    assert result.trace_path is not None
    events = _trace_events(agent)

    tap_calls = [e for e in events if e["event"] == "tool_call" and e["tool"] == "tap"]
    tap_results = [
        e for e in events if e["event"] == "tool_result" and e["tool"] == "tap"
    ]

    # One call, one paired result — the blocked call is not silently dropped.
    assert len(tap_calls) == 1
    assert len(tap_results) == 1
    assert tap_results[0]["step"] == tap_calls[0]["step"]

    # The recorded result is the warning, not a real execution receipt.
    recorded = str(tap_results[0].get("result") or "")
    assert "已拦截" in recorded
    assert "tapped" not in recorded
    assert tap_results[0].get("error") is None

    # ...and the device was never touched.
    assert session.taps == []
    assert agent._safety_warning.warning_count == 1


# --------------------------------------------------------------------------
# (b) model/pre_request: compact -> taskdoc on a T2 fold
# --------------------------------------------------------------------------
def test_t2_fold_prunes_once_and_taskdoc_stays_pinned(tmp_path, monkeypatch):
    """(b) On a T2 fold the pruner runs exactly once and TaskDoc is re-pinned.

    Compact is prepended onto ``model/pre_request``, so it is the outermost
    listener: it prunes images/OBS marks (C1) *before* summarising, then hands
    the rebuilt transcript to the inner listeners — the TaskDoc injector still
    pins a fresh board onto it. The same invariant at unit level is covered by
    ``tests/v2/test_compact.py::test_t2_fold_preserves_pinned_taskdoc_and_listener_refreshes_it``
    (and the compact-OFF variant in ``tests/v2/test_context_pruner.py``);
    this test asserts it through the assembled chain.
    """

    session = _FakeSession(
        task_doc=_FakeTaskDoc(
            goal_base="打开设置",
            items=[_FakeTaskItem("1", "打开设置", "in_progress")],
        )
    )
    # The summariser: one canned hand-off summary.
    model = _ScriptedModel(
        responses=[AIMessage(content="## 目标\n打开设置\n## 下一步\n连接 WLAN")]
    )
    _install_fake_modules(monkeypatch, session, model, lambda sess, config: [])

    from phone_agent.v2.agent import ThinPhoneAgent
    from phone_agent.v2.events import MODEL_PRE_REQUEST

    config = types.SimpleNamespace(
        lang="cn",
        max_model_calls=20,
        trace_dir=str(tmp_path),
        trace_enabled=False,
        safety_mode="off",
        compact_enabled=True,
        context_window=20_000,
        taskdoc_enabled=True,
        app_kb_enabled=False,
        deliverable_enabled=False,
        experience_enabled=False,
        finish_verify="off",
        memory_rag="off",
    )
    agent = ThinPhoneAgent(config)

    pruner = agent._capability_ctx.service("context_pruner")
    prune_calls = {"n": 0}
    original_prune = pruner.prune

    def counted_prune(messages):  # noqa: ANN001
        prune_calls["n"] += 1
        return original_prune(messages)

    pruner.prune = counted_prune

    def _big_text(tokens: int) -> str:
        return "x" * (tokens * 4)

    conversation: list[Any] = [SystemMessage(content="任务：打开设置", id="sys")]
    for index in range(8):
        conversation.append(
            AIMessage(
                content="",
                id=f"a{index}",
                tool_calls=[
                    {
                        "name": "tap",
                        "args": {"target_mark_id": f"ax_{index}"},
                        "id": f"c{index}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        conversation.append(
            ToolMessage(
                content=_big_text(3000),
                id=f"t{index}",
                tool_call_id=f"c{index}",
                name="tap",
            )
        )

    result = agent.event_bus.waterfall(
        MODEL_PRE_REQUEST, conversation, terminal=lambda x: x
    )

    # Pruner ran exactly once, driven by compact (not by a second listener).
    assert prune_calls["n"] == 1

    # T2 actually folded: a hand-off summary replaced the ancient turns.
    summaries = [
        m
        for m in result
        if str(getattr(m, "content", "")).startswith("[COMPACT_SUMMARY]")
    ]
    assert len(summaries) == 1

    # ...and the TaskDoc board is pinned onto the rebuilt transcript (exactly
    # one fresh block, carrying the current goal).
    pinned = [
        m for m in result if str(getattr(m, "id", "") or "").startswith("__taskdoc__")
    ]
    assert len(pinned) == 1
    assert pinned[0].content.startswith("[TASK_DOC]")
    assert "打开设置" in pinned[0].content
