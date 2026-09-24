"""Stuck governor: signature derivation, one advisory per signature, shadow-only."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from tests.plugins.systemone_harness import (
    HarnessConfig,
    RecordingClient,
    ScriptedPhoneSession,
)


def ai_step(tool: str, intent: str, *, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": tool, "args": {"intent": intent}, "id": call_id, "type": "tool_call"}
        ],
    )


def governor(governor_module, client_module, *, client="default", steps=3):
    events: list[tuple[str, dict]] = []
    instance = governor_module.StuckGovernor(
        ScriptedPhoneSession(["h1"]),
        record=lambda event, **payload: events.append((event, payload)),
        tokens_of=client_module.usage_tokens,
        client=RecordingClient() if client == "default" else client,
        steps=steps,
    )
    return instance, events


# ---------------------------------------------------------------------------
# Signature derivation
# ---------------------------------------------------------------------------
def test_step_signature_uses_the_declared_intent(governor_module):
    call = {"name": "tap", "args": {"intent": "打开设置"}}
    assert governor_module.step_signature(call) == "tap|打开设置"
    assert governor_module.step_signature({"name": "tap", "args": {}}) == "tap|"
    assert governor_module.step_signature({"name": "", "args": {}}) is None


def test_step_signature_falls_back_to_the_target_description(governor_module):
    call = {"name": "tap", "args": {"target_description": "设置"}}
    assert governor_module.step_signature(call) == "tap|设置"


def test_recent_signatures_keeps_the_trailing_window(governor_module):
    messages = [
        ai_step("tap", "a"),
        ai_step("tap", "b"),
        ai_step("scroll", "c", call_id="c2"),
    ]
    assert governor_module.recent_signatures(messages, 2) == ["tap|b", "scroll|c"]
    assert governor_module.recent_signatures([], 3) == []


# ---------------------------------------------------------------------------
# Advisory behaviour
# ---------------------------------------------------------------------------
def test_repeated_signature_emits_one_calibrated_advisory(governor_module, client_module):
    instance, events = governor(governor_module, client_module)
    messages = [ai_step("tap", "打开设置", call_id=f"c{i}") for i in range(3)]

    instance.inspect(messages)

    assert len(events) == 1
    event, payload = events[0]
    assert event == governor_module.GOVERNOR_EVENT
    assert payload["advisory"] is True
    assert payload["signature"] == "tap|打开设置"
    assert payload["repeat_count"] == 3
    assert payload["p"] == pytest.approx(0.97)
    assert payload["model_value"] is True
    assert payload["error"] is None
    assert instance.advisory_count == 1


def test_the_same_signature_is_advisory_only_once(governor_module, client_module):
    instance, events = governor(governor_module, client_module)
    messages = [ai_step("tap", "打开设置", call_id=f"c{i}") for i in range(4)]

    instance.inspect(messages)
    instance.inspect(messages)

    assert len(events) == 1


def test_varied_signatures_emit_nothing(governor_module, client_module):
    instance, events = governor(governor_module, client_module)
    instance.inspect([ai_step("tap", "a"), ai_step("tap", "b"), ai_step("tap", "c")])
    assert events == []
    assert instance.advisory_count == 0


def test_shorter_history_than_the_window_emits_nothing(governor_module, client_module):
    instance, events = governor(governor_module, client_module, steps=4)
    instance.inspect([ai_step("tap", "a"), ai_step("tap", "a"), ai_step("tap", "a")])
    assert events == []


def test_a_model_failure_still_leaves_the_deterministic_advisory(governor_module, client_module):
    client = RecordingClient(error=RuntimeError("endpoint down"))
    instance, events = governor(governor_module, client_module, client=client)
    instance.inspect([ai_step("tap", "a", call_id=f"c{i}") for i in range(3)])

    payload = events[0][1]
    assert payload["advisory"] is True
    assert payload["p"] is None
    assert payload["error"] == "RuntimeError"


def test_the_governor_is_transparent_in_the_waterfall(governor_module, client_module):
    instance, _ = governor(governor_module, client_module)
    messages = [ai_step("tap", "a", call_id=f"c{i}") for i in range(3)]
    payload = list(messages)

    result = instance(payload, lambda value: ("next", value))

    assert result[0] == "next"
    assert result[1] is payload  # the very same list, untouched
    assert payload == messages


def test_the_governor_books_the_reported_tokens(governor_module, client_module):
    from tests.plugins.systemone_harness import RecordingLedger

    ledger = RecordingLedger()
    session = ScriptedPhoneSession(["h1"])
    session.usage_ledger = ledger
    instance = governor_module.StuckGovernor(
        session,
        record=lambda *a, **k: None,
        tokens_of=client_module.usage_tokens,
        client=RecordingClient(
            reply=client_module.SystemOneReply(
                answers={
                    "stuck": client_module.Answer(kind="noul", value=True, p=0.8)
                },
                usage={"input_tokens": 15, "output_tokens": 6},
            )
        ),
        steps=3,
    )
    instance.inspect([ai_step("tap", "a", call_id=f"c{i}") for i in range(3)])

    assert ledger.calls == [("systemone", 21)]


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def test_the_governor_does_not_exist_when_switched_off(monkeypatch, fake_server, mount):
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_GOVERNOR", "off")
    mounted = mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))
    assert mounted.listeners("model/pre_request") == []


def test_a_prompt_injecting_mode_is_refused_rather_than_silently_downgraded(
    monkeypatch, fake_server, mount
):
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_GOVERNOR", "on")
    with pytest.raises(ValueError, match="systemone_governor must be one of off, shadow"):
        mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))


def test_the_mounted_governor_is_advisory_only(monkeypatch, fake_server, mount):
    from phone_agent.v2.events import MODEL_PRE_REQUEST

    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_GOVERNOR", "shadow")
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(config=HarnessConfig(), session=session)

    messages = [ai_step("tap", "a", call_id=f"c{i}") for i in range(3)]
    returned = mounted.bus.waterfall(MODEL_PRE_REQUEST, list(messages), lambda value: value)

    assert returned == messages
    assert len(session.events("systemone_governor")) == 1


def test_mounting_without_a_backend_keeps_the_deterministic_governor(
    monkeypatch, fake_server, mount
):
    monkeypatch.delenv("PHONE_AGENT_SYSTEMONE_BACKEND", raising=False)
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(config=HarnessConfig(), session=session)
    assert len(mounted.listeners("model/pre_request")) == 1

    mounted.listeners("model/pre_request")[0](
        [ai_step("tap", "a", call_id=f"c{i}") for i in range(3)], lambda value: value
    )
    event = session.events("systemone_governor")[0]
    assert event["advisory"] is True
    assert event["p"] is None  # no backend -> no model opinion, no invented number
