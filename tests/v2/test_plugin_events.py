"""WP-PLUGIN-B event bus behavior."""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import ToolMessage

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    build_capability_registry,
    assemble_capabilities,
)
from phone_agent.v2.events import (
    RUN_END,
    RUN_START,
    EventBus,
    OBSERVE,
    REJECT,
    validate_event_name,
)
from phone_agent.v2.middleware.safety import SafetyWarningMiddleware


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


def test_safety_warning_middleware_uses_event_bus_listener() -> None:
    bus = EventBus()
    mw = SafetyWarningMiddleware(None, _Cfg(), notify=lambda _message: None, event_bus=bus)
    bus.on("tool/pre_execute", lambda payload, next: REJECT)  # noqa: A002
    executed = {"n": 0}

    def handler(request):
        executed["n"] += 1
        return ToolMessage(content="executed", tool_call_id="call_1", name="tap")

    result = mw.wrap_tool_call(_req("tap", {"target_description": "确认支付"}), handler)

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
            "safety_middleware_factory": lambda: SafetyWarningMiddleware(
                None, _Cfg(), notify=lambda _message: None, event_bus=bus
            ),
        }
    )
    registry = build_capability_registry(_Cfg())

    assemble_capabilities(registry, ctx)
    assert bus.waterfall(
        "tool/pre_execute",
        _req("tap", {"target_description": "确认支付"}),
        terminal=lambda x: x,
    ) is REJECT

    class OffCfg:
        safety_mode = "off"

    assemble_capabilities(build_capability_registry(OffCfg()), ctx)
    request = _req("tap", {"target_description": "确认支付"})
    assert bus.waterfall("tool/pre_execute", request, terminal=lambda x: x) is request


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
    from phone_agent.v2.agent import _ModelPreRequestBridgeMiddleware

    bus = EventBus()
    mw = _ModelPreRequestBridgeMiddleware(bus)
    messages = [{"type": "text", "text": "hello"}]
    bus.on(
        "model/pre_request",
        lambda msgs, next: [*msgs, {"type": "text", "text": "extra"}],  # noqa: A002
    )

    result = mw.before_model({"messages": messages}, None)

    assert result == {
        "messages": [*messages, {"type": "text", "text": "extra"}]
    }


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
