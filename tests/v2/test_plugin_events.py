"""WP-PLUGIN-B event bus behavior."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from langchain_core.messages import RemoveMessage, SystemMessage, ToolMessage

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    build_capability_registry,
    assemble_capabilities,
)
from phone_agent.v2.events import (
    RUN_END,
    RUN_START,
    TOOL_EXECUTE,
    EventBus,
    OBSERVE,
    REJECT,
    validate_event_name,
)
from phone_agent.v2.middleware.safety import SafetyWarningListener
from phone_agent.v2.middleware.taskdoc import TaskDocInjector
from phone_agent.v2.middleware.trace import TraceMiddleware


def test_event_name_validation() -> None:
    assert validate_event_name("tool/pre_execute") == "tool/pre_execute"
    assert validate_event_name(OBSERVE) == OBSERVE

    for event in ("tool/pre-execute", "tool/PreExecute", "tool2/x", ""):
        with pytest.raises(ValueError):
            validate_event_name(event)


def test_on_returns_disposer_and_honors_prepend() -> None:
    bus = EventBus()
    seen: list[str] = []
    dispose_late = bus.on("tool/pre_execute", lambda payload: seen.append("late"))
    bus.on("tool/pre_execute", lambda payload: seen.append("early"), prepend=True)

    bus.emit("tool/pre_execute", {})
    assert seen == ["early", "late"]

    dispose_late()
    dispose_late()
    bus.emit("tool/pre_execute", {})
    assert seen == ["early", "late", "early"]


def test_emit_is_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    seen: list[str] = []

    def fail(payload: object) -> None:
        raise RuntimeError("observer failed")

    bus.on("run/start", fail)
    bus.on("run/start", lambda payload: seen.append("ok"))

    with caplog.at_level(logging.ERROR):
        bus.emit("run/start", {"run_id": "r1"})

    assert seen == ["ok"]
    assert "event listener failed during emit: run/start" in caplog.text


def test_waterfall_replaces_payload_and_reject_short_circuits() -> None:
    bus = EventBus()
    seen: list[int] = []
    bus.on("tool/pre_execute", lambda payload, next: next({**payload, "a": 1}))

    def reject(payload: dict, next) -> object:  # noqa: A002 - next is the onion contract
        seen.append(payload["a"])
        return REJECT

    bus.on("tool/pre_execute", reject)
    bus.on("tool/pre_execute", lambda payload, next: seen.append(99))

    assert bus.waterfall("tool/pre_execute", {}, terminal=lambda x: x) is REJECT
    assert seen == [1]


def test_waterfall_raises_listener_exception() -> None:
    bus = EventBus()

    def fail(payload: object, next) -> object:  # noqa: A002 - next is the onion contract
        raise RuntimeError("policy failed")

    bus.on("tool/pre_execute", fail)
    with pytest.raises(RuntimeError, match="policy failed"):
        bus.waterfall("tool/pre_execute", {}, terminal=lambda x: x)


def test_waterfall_listener_can_wrap_terminal_result() -> None:
    """Onion semantics let a listener execute downstream and wrap the result."""

    bus = EventBus()
    bus.on(
        "tool/pre_execute",
        lambda payload, next: {"wrapped": next(payload), "meta": "post"},  # noqa: A002
    )

    result = bus.waterfall(
        "tool/pre_execute", {"x": 1}, terminal=lambda p: {"executed": p}
    )

    assert result == {"wrapped": {"executed": {"x": 1}}, "meta": "post"}


def test_serial_collects_results_and_raises() -> None:
    bus = EventBus()
    bus.on("model/pre_request", lambda payload: "a")
    bus.on("model/pre_request", lambda payload: ("b", payload))

    assert bus.serial("model/pre_request", 3) == ["a", ("b", 3)]

    bus.on("model/pre_request", lambda payload: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError, match="x"):
        bus.serial("model/pre_request", 3)


class _Cfg:
    safety_mode = "wary"


def _req(name: str, args: dict):
    from types import SimpleNamespace

    return SimpleNamespace(tool_call={"name": name, "args": args, "id": "call_1"})


def test_safety_warning_listener_short_circuits_on_tool_execute() -> None:
    bus = EventBus()
    listener = SafetyWarningListener(None, _Cfg(), notify=lambda _message: None)
    bus.on(TOOL_EXECUTE, listener)
    executed = {"n": 0}

    def handler(request):
        executed["n"] += 1
        return ToolMessage(content="executed", tool_call_id="call_1", name="tap")

    result = bus.waterfall(
        TOOL_EXECUTE,
        _req("tap", {"target_description": "确认支付"}),
        terminal=handler,
    )

    assert executed["n"] == 0
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "confirm_irreversible=true" in result.content


def test_safety_capability_registers_and_releases_event_listener() -> None:
    bus = EventBus()
    ctx = CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": None,
            "config": _Cfg(),
        }
    )
    registry = build_capability_registry(_Cfg())

    assemble_capabilities(registry, ctx)
    result = bus.waterfall(
        TOOL_EXECUTE,
        _req("tap", {"target_description": "确认支付"}),
        terminal=lambda x: x,
    )
    assert isinstance(result, ToolMessage)
    assert "confirm_irreversible=true" in result.content

    class OffCfg:
        safety_mode = "off"

    assemble_capabilities(build_capability_registry(OffCfg()), ctx)
    request = _req("tap", {"target_description": "确认支付"})
    assert bus.waterfall(TOOL_EXECUTE, request, terminal=lambda x: x) is request


# ---------------------------------------------------------------------------
# E2: tool/execute listener invariants
# ---------------------------------------------------------------------------


def test_tool_execute_listener_order_is_registration_order_outer_to_inner():
    """First registered listener is outermost (onion order)."""

    bus = EventBus()
    order: list[str] = []

    def trace_listener(request, next):  # noqa: A002 - next is the onion contract
        order.append("trace")
        return next(request)

    def safety_listener(request, next):  # noqa: A002
        order.append("safety")
        return "short-circuited"

    bus.on(TOOL_EXECUTE, trace_listener)
    bus.on(TOOL_EXECUTE, safety_listener)
    result = bus.waterfall(TOOL_EXECUTE, {}, terminal=lambda x: "terminal")

    assert order == ["trace", "safety"]
    assert result == "short-circuited"


def test_trace_pairs_rejected_call_with_tool_result(tmp_path) -> None:
    """A listener returning REJECT is still recorded as a tool_result by trace."""

    bus = EventBus()
    trace = TraceMiddleware("run-reject", trace_dir=str(tmp_path), enabled=True)
    bus.on(TOOL_EXECUTE, trace.on_tool_execute)
    bus.on(TOOL_EXECUTE, lambda request, next: REJECT)  # noqa: A002

    bus.waterfall(TOOL_EXECUTE, _req("tap", {}), terminal=lambda x: x)

    raw = (tmp_path / "run-reject.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines()]
    calls = [e for e in events if e["event"] == "tool_call"]
    results = [e for e in events if e["event"] == "tool_result"]
    assert len(calls) == 1
    assert len(results) == 1
    assert results[0]["tool"] == "tap"
    assert "已拦截" in str(results[0].get("result") or "")


def test_diagnostic_observes_warning_result(tmp_path) -> None:
    """Diagnostic's tool_observation records a safety warning ToolMessage."""

    from phone_agent.v2.middleware.diagnostic import DiagnosticEvidenceMiddleware
    from phone_agent.v2.middleware.safety import SafetyWarningListener

    bus = EventBus()
    trace = TraceMiddleware("run-diag", trace_dir=str(tmp_path), enabled=True)
    diag = DiagnosticEvidenceMiddleware(
        "run-diag", evidence_dir=str(tmp_path / "evidence"), enabled=True
    )
    safety = SafetyWarningListener(None, _Cfg(), notify=lambda _message: None)
    bus.on(TOOL_EXECUTE, trace.on_tool_execute)
    bus.on(TOOL_EXECUTE, diag.on_tool_execute)
    bus.on(TOOL_EXECUTE, safety)

    result = bus.waterfall(
        TOOL_EXECUTE,
        _req("tap", {"target_description": "确认支付"}),
        terminal=lambda x: x,
    )

    assert isinstance(result, ToolMessage)
    raw = (tmp_path / "evidence" / "run-diag.evidence.jsonl").read_text(
        encoding="utf-8"
    )
    events = [json.loads(line) for line in raw.splitlines()]
    observations = [e for e in events if e["event"] == "tool_observation"]
    assert observations
    assert "confirm_irreversible=true" in str(observations[0]["result_text"])


# ---------------------------------------------------------------------------
# K2: agent/session event-bus bridge
# ---------------------------------------------------------------------------


def test_model_pre_request_bridge_no_listener_is_identity() -> None:
    from phone_agent.v2.agent import _ModelPreRequestBridgeMiddleware

    bus = EventBus()
    mw = _ModelPreRequestBridgeMiddleware(bus)
    messages = [{"type": "text", "text": "hello"}]

    assert mw.before_model({"messages": messages}, None) is None


def test_model_pre_request_bridge_listener_replaces_messages() -> None:
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from phone_agent.v2.agent import _ModelPreRequestBridgeMiddleware

    bus = EventBus()
    mw = _ModelPreRequestBridgeMiddleware(bus)
    messages = [{"type": "text", "text": "hello"}]
    bus.on(
        "model/pre_request",
        lambda msgs, next: [*msgs, {"type": "text", "text": "extra"}],  # noqa: A002
    )

    result = mw.before_model({"messages": messages}, None)

    # Full-list contract: the bridge alone mints the single legal LangGraph
    # update — one REMOVE_ALL sentinel followed by the complete transformed list.
    assert result is not None
    update_messages = result["messages"]
    assert isinstance(update_messages[0], RemoveMessage)
    assert update_messages[0].id == REMOVE_ALL_MESSAGES
    assert update_messages[1:] == [*messages, {"type": "text", "text": "extra"}]


def test_run_start_event_emitted(tmp_path, monkeypatch) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent
    from tests.v2.test_experience import _install_mini_agent_modules

    config = _install_mini_agent_modules(monkeypatch, tmp_path, enabled=False)
    config.trace_enabled = False
    config.experience_enabled = False

    agent = ThinPhoneAgent(config)
    start_payloads: list[dict] = []
    end_payloads: list[dict] = []
    agent.event_bus.on(RUN_START, lambda p: start_payloads.append(p))
    agent.event_bus.on(RUN_END, lambda p: end_payloads.append(p))

    result = agent.run("联系 13800138000")

    assert result.success is True
    assert len(start_payloads) == 1
    assert start_payloads[0]["run_id"] == agent.run_id
    assert start_payloads[0]["goal"] == "联系 13800138000"
    assert start_payloads[0]["device_scope"] == "device:unknown"
    assert len(end_payloads) == 1
    assert end_payloads[0]["run_id"] == agent.run_id
    assert end_payloads[0]["success"] is True
    assert end_payloads[0]["exception"] is False


# ---------------------------------------------------------------------------
# D6: TaskDoc pinning as a model/pre_request listener
# ---------------------------------------------------------------------------


@dataclass
class _TaskDocListenerDoc:
    goal_base: str = ""
    items: list = field(default_factory=list)
    facts: list = field(default_factory=list)

    def render(self, lang: str = "cn") -> str:  # noqa: ARG002
        if not (self.goal_base or self.items or self.facts):
            return ""
        lines = ["## 目标", f"base: {self.goal_base}"]
        if self.items:
            lines.append("## 路线")
            for item in self.items:
                lines.append(f"- [{item['status']}] {item['id']}: {item['content']}")
        if self.facts:
            lines.append("## 关键事实")
            lines.extend(f"- {f}" for f in self.facts)
        return "\n".join(lines)


def _taskdoc_registry(enabled: bool):
    return build_capability_registry(
        SimpleNamespace(
            taskdoc_enabled=enabled,
            safety_mode="off",
            compact_enabled=False,
            finish_verify="off",
            app_kb_enabled=False,
            dream_mode="manual",
            experience_enabled=False,
            memory_rag="off",
            deliverable_enabled=False,
        )
    )


def test_taskdoc_capability_injects_via_model_pre_request_listener() -> None:
    bus = EventBus()
    session = SimpleNamespace(
        task_doc=_TaskDocListenerDoc(
            goal_base="订一张票",
            items=[{"id": "s1", "content": "选出发地", "status": "in_progress"}],
        )
    )
    ctx = CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": session,
            "config": SimpleNamespace(lang="cn", taskdoc_nudge_steps=5),
            "taskdoc_tool_factory": lambda: None,
            "taskdoc_run_start": lambda _state: None,
        }
    )

    assemble_capabilities(_taskdoc_registry(True), ctx)

    messages = [SystemMessage(content="sys")]
    result = bus.waterfall("model/pre_request", messages, terminal=lambda x: x)

    # One fresh pinned block appended; content carries the rendered doc.
    assert len(result) == len(messages) + 1
    assert result[-1].content.startswith("[TASK_DOC]\n")
    assert "订一张票" in result[-1].content
    assert result[-1].id.startswith("__taskdoc__")


def test_taskdoc_listener_refreshes_one_block_per_call() -> None:
    bus = EventBus()
    session = SimpleNamespace(
        task_doc=_TaskDocListenerDoc(goal_base="目标", items=[])
    )
    injector = TaskDocInjector(session, lang="cn")
    bus.on("model/pre_request", injector)

    first = bus.waterfall("model/pre_request", [], terminal=lambda x: x)
    first_id = first[-1].id

    second = bus.waterfall("model/pre_request", first, terminal=lambda x: x)

    # Full-list contract (S1): the listener drops the stale copy by id and
    # appends exactly one fresh block — no RemoveMessage ever travels in the
    # waterfall payload (the bridge alone mints the LangGraph removal).
    assert not any(isinstance(m, RemoveMessage) for m in second)
    assert all((getattr(m, "id") or "") != first_id for m in second)
    taskdoc_ids = {
        (getattr(m, "id") or "")
        for m in second
        if (getattr(m, "id") or "").startswith("__taskdoc__")
    }
    assert taskdoc_ids == {second[-1].id}
    assert second[-1].id != first_id


def test_taskdoc_capability_release_disposes_listener() -> None:
    bus = EventBus()
    session = SimpleNamespace(
        task_doc=_TaskDocListenerDoc(goal_base="目标", items=[])
    )
    ctx = CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": session,
            "config": SimpleNamespace(lang="cn", taskdoc_nudge_steps=5),
            "taskdoc_tool_factory": lambda: None,
            "taskdoc_run_start": lambda _state: None,
        }
    )

    assemble_capabilities(_taskdoc_registry(True), ctx)
    assert bus.waterfall("model/pre_request", [], terminal=lambda x: x) != []

    assemble_capabilities(_taskdoc_registry(False), ctx)
    # After release the listener is gone and the payload passes through unchanged.
    payload = [SystemMessage(content="sys")]
    assert bus.waterfall("model/pre_request", payload, terminal=lambda x: x) is payload
