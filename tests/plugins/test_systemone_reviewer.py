"""Shadow safety reviewer: verdict recording, threshold veto, no loosening."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

from phone_agent.v2.events import EventBus
from tests.plugins.systemone_harness import (
    FakeSystemOneServer,
    HarnessConfig,
    RecordingClient,
    RecordingLedger,
    ScriptedPhoneSession,
    run_waterfall,
    tool_call_request,
)


def delegate(request):
    return "executed"


def reviewers(mounted):
    """The reviewer listeners the plugin registered on ``tool/execute``."""

    return mounted.listeners("tool/execute")


def wire_env(monkeypatch, url, *, mode="shadow", confidence=None):
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BACKEND", "kev")
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_BASE_URL", url)
    monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_REVIEW", mode)
    if confidence is not None:
        monkeypatch.setenv("PHONE_AGENT_SYSTEMONE_CONFIDENCE", str(confidence))


# ---------------------------------------------------------------------------
# State construction
# ---------------------------------------------------------------------------
def test_state_carries_the_tool_and_a_redacted_bounded_target(reviewer_module):
    session = ScriptedPhoneSession(["h1"])
    state = reviewer_module.build_state(
        "type_text", {"text": "call 13800138000 now"}, session
    )
    assert state.startswith("tool: type_text")
    assert "13800138000" not in state
    assert "<redacted>" in state


def test_target_text_resolves_a_mark_summary(reviewer_module):
    from phone_agent.grounding.provider import MarkCandidate

    session = ScriptedPhoneSession(
        ["h1"],
        marks={
            "ax_1": MarkCandidate(
                mark_id="ax_1",
                bbox=[400, 400, 600, 600],
                center=[500, 500],
                role="button",
                text_summary="确认支付",
            )
        },
    )
    state = reviewer_module.build_state("tap", {"target_mark_id": "ax_1"}, session)
    assert "确认支付" in state


def test_readonly_tools_are_never_reviewed(reviewer_module, client_module):
    client = RecordingClient()
    listener = reviewer_module.SystemOneSafetyReviewer(
        ScriptedPhoneSession(["h1"]),
        HarnessConfig(),
        client=client,
        record=lambda *a, **k: None,
        tokens_of=client_module.usage_tokens,
    )
    assert run_waterfall([listener], tool_call_request("read_screen"), delegate) == "executed"
    assert client.calls == []


def test_confirmed_resend_is_passed_through_without_a_review(
    reviewer_module, client_module
):
    client = RecordingClient()
    listener = reviewer_module.SystemOneSafetyReviewer(
        ScriptedPhoneSession(["h1"]),
        HarnessConfig(),
        client=client,
        record=lambda *a, **k: None,
        tokens_of=client_module.usage_tokens,
    )
    request = tool_call_request("tap", {"target_description": "支付", "confirm_irreversible": True})
    assert run_waterfall([listener], request, delegate) == "executed"
    assert client.calls == []


# ---------------------------------------------------------------------------
# Never loosens an existing gate
# ---------------------------------------------------------------------------
def test_a_call_the_builtin_gate_would_block_is_delegated_untouched(
    reviewer_module, client_module
):
    """The reviewer records the fact and leaves the decision where it was."""

    client = RecordingClient()
    events: list[tuple] = []
    listener = reviewer_module.SystemOneSafetyReviewer(
        ScriptedPhoneSession(["h1"]),
        HarnessConfig(),
        client=client,
        record=lambda event, **payload: events.append((event, payload)),
        tokens_of=client_module.usage_tokens,
        mode="on",
    )
    request = tool_call_request("type_text", {"text": "确认支付 100 元"})
    assert run_waterfall([listener], request, delegate) == "executed"
    assert client.calls == []
    assert events[0][1]["action"] == "existing_gate"
    assert events[0][1]["reason"] == "irreversible_commit"


def test_in_shadow_mode_the_call_is_always_delegated(reviewer_module, client_module):
    client = RecordingClient()
    events: list[tuple] = []
    listener = reviewer_module.SystemOneSafetyReviewer(
        ScriptedPhoneSession(["h1"]),
        HarnessConfig(),
        client=client,
        record=lambda event, **payload: events.append((event, payload)),
        tokens_of=client_module.usage_tokens,
        mode="shadow",
    )
    request = tool_call_request("tap", {"target_description": "删除"})
    assert run_waterfall([listener], request, delegate) == "executed"
    assert len(client.calls) == 1
    payload = events[0][1]
    assert payload["action"] == "shadow"
    assert payload["verdict"] == "risky"
    assert payload["p"] == pytest.approx(0.97)
    assert payload["score"] == pytest.approx(0.8)
    assert payload["tokens"] == 30  # 21 + 9 from the double's default usage
    assert payload["mode"] == "shadow"


# ---------------------------------------------------------------------------
# The additive veto (mode ``on``)
# ---------------------------------------------------------------------------
def test_on_mode_vetoes_a_risky_call_above_the_threshold(monkeypatch, fake_server, mount):
    wire_env(monkeypatch, fake_server.url, mode="on", confidence=0.9)
    session = ScriptedPhoneSession(["h1"])
    mounted = mount(config=HarnessConfig(), session=session)

    # A soft candidate: the built-in wary gate lets it through, so the
    # reviewer is the only thing that can veto it.
    request = tool_call_request("tap", {"target_description": "删除"})
    result = run_waterfall(reviewers(mounted), request, delegate)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_1"
    assert result.name == "tap"
    assert "已拦截（未执行）" in result.content
    assert "confirm_irreversible=true" in result.content
    assert "p=0.95" in result.content
    event = session.events("systemone_review")[0]
    assert event["action"] == "veto"
    assert event["threshold"] == pytest.approx(0.9)


def test_on_mode_does_not_veto_below_the_threshold(monkeypatch, fake_server, mount):
    server = FakeSystemOneServer(
        answer={
            "risky": {"type": "noul", "noul": 0.55},
            "risk": {"type": "score", "score": 0.7, "confidence": 0.6},
        }
    ).start()
    try:
        wire_env(monkeypatch, server.url, mode="on", confidence=0.9)
        session = ScriptedPhoneSession(["h1"])
        mounted = mount(config=HarnessConfig(), session=session)

        result = run_waterfall(
            reviewers(mounted), tool_call_request("tap", {"target_description": "下一步"}), delegate
        )
    finally:
        server.stop()
    assert result == "executed"
    assert session.events("systemone_review")[0]["action"] == "allow"


def test_on_mode_does_not_veto_when_the_model_answers_safe(monkeypatch, fake_server, mount):
    server = FakeSystemOneServer(
        answer={
            "risky": {"type": "noul", "noul": 0.01},
            "risk": {"type": "score", "score": 0.05, "confidence": 0.9},
        }
    ).start()
    try:
        wire_env(monkeypatch, server.url, mode="on")
        session = ScriptedPhoneSession(["h1"])
        mounted = mount(config=HarnessConfig(), session=session)
        result = run_waterfall(
            reviewers(mounted), tool_call_request("tap", {"target_description": "返回"}), delegate
        )
    finally:
        server.stop()
    assert result == "executed"
    assert session.events("systemone_review")[0]["verdict"] == "safe"


def test_a_malformed_answer_degrades_to_no_veto(monkeypatch, fake_server, mount):
    """A ``noul`` answer without its probability cannot be acted on."""

    server = FakeSystemOneServer(
        answer={"risky": {"type": "noul"}, "risk": {"type": "score", "score": 0.9}}
    ).start()
    try:
        wire_env(monkeypatch, server.url, mode="on")
        session = ScriptedPhoneSession(["h1"])
        mounted = mount(config=HarnessConfig(), session=session)
        result = run_waterfall(
            reviewers(mounted), tool_call_request("tap", {"target_description": "提交"}), delegate
        )
    finally:
        server.stop()
    assert result == "executed"
    event = session.events("systemone_review")[0]
    assert event["action"] == "error"
    assert event["error"] == "SystemOneError"


# ---------------------------------------------------------------------------
# Failure policy: an additive gate never blocks a run
# ---------------------------------------------------------------------------
def test_a_review_failure_degrades_to_no_veto(reviewer_module, client_module):
    client = RecordingClient(error=RuntimeError("endpoint down"))
    events: list[tuple] = []
    listener = reviewer_module.SystemOneSafetyReviewer(
        ScriptedPhoneSession(["h1"]),
        HarnessConfig(),
        client=client,
        record=lambda event, **payload: events.append((event, payload)),
        tokens_of=client_module.usage_tokens,
        mode="on",
    )
    request = tool_call_request("tap", {"target_description": "支付"})
    assert run_waterfall([listener], request, delegate) == "executed"
    assert events[0][1]["action"] == "error"
    assert events[0][1]["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Accounting and privacy on the review path
# ---------------------------------------------------------------------------
def test_review_books_the_reported_tokens(reviewer_module, client_module):
    reply = client_module.SystemOneReply(
        answers={
            "risky": client_module.Answer(kind="noul", value=True, p=0.95),
            "risk": client_module.Answer(kind="score", value=0.8, p=0.7),
        },
        usage={"input_tokens": 30, "output_tokens": 3},
    )
    ledger = RecordingLedger()
    session = ScriptedPhoneSession(["h1"])
    session.usage_ledger = ledger
    listener = reviewer_module.SystemOneSafetyReviewer(
        session,
        HarnessConfig(),
        client=RecordingClient(reply=reply),
        record=lambda *a, **k: None,
        tokens_of=client_module.usage_tokens,
        mode="shadow",
    )
    run_waterfall([listener], tool_call_request("tap", {"target_description": "x"}), delegate)

    # ``systemone`` is a token role (the API always reports usage).
    assert ledger.calls == [("systemone", 33)]


def test_review_without_a_usage_block_books_nothing(reviewer_module, client_module):
    ledger = RecordingLedger()
    session = ScriptedPhoneSession(["h1"])
    session.usage_ledger = ledger
    listener = reviewer_module.SystemOneSafetyReviewer(
        session,
        HarnessConfig(),
        client=RecordingClient(usage={}),
        record=lambda *a, **k: None,
        tokens_of=client_module.usage_tokens,
    )
    run_waterfall([listener], tool_call_request("tap", {"target_description": "x"}), delegate)
    assert ledger.calls == []


def test_the_state_sent_to_the_backend_carries_no_raw_pii(monkeypatch, fake_server, mount):
    wire_env(monkeypatch, fake_server.url, mode="shadow")
    mounted = mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))

    request = tool_call_request("tap", {"target_description": "订单 13800138000"})
    run_waterfall(reviewers(mounted), request, delegate)

    body = fake_server.last_body()
    assert "13800138000" not in body["state"]
    assert "<redacted>" in body["state"]


def test_the_state_sent_to_the_backend_never_carries_the_api_key(monkeypatch, fake_server, mount):
    """A declared redaction literal covers the outbound review state too."""

    secret = "sk-jev-abcdef123456"
    monkeypatch.setenv("TYPESAFE_API_KEY", secret)
    wire_env(monkeypatch, fake_server.url, mode="shadow")
    mounted = mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))

    request = tool_call_request("tap", {"target_description": f"支付 订单 {secret}"})
    run_waterfall(reviewers(mounted), request, delegate)

    body = fake_server.last_body()
    assert secret not in body["state"]
    assert "<redacted>" in body["state"]


def test_review_mode_off_mounts_no_reviewer(monkeypatch, fake_server, mount):
    wire_env(monkeypatch, fake_server.url, mode="off")
    mounted = mount(config=HarnessConfig(), session=ScriptedPhoneSession(["h1"]))
    assert mounted.listeners("tool/execute") == []


def test_the_reviewer_sits_outside_the_builtin_safety_listener(monkeypatch, fake_server, mount):
    """``prepend`` is what lets the reviewer see calls the safety gate vetoes."""

    from phone_agent.v2.events import TOOL_EXECUTE
    from phone_agent.v2.middleware.safety import build_capability_safety_listener

    wire_env(monkeypatch, fake_server.url, mode="shadow")
    session = ScriptedPhoneSession(["h1"])
    bus = EventBus()
    safety = build_capability_safety_listener(session, HarnessConfig())
    bus.on(TOOL_EXECUTE, safety)  # appended: the built-in gate is registered first
    mounted = mount(config=HarnessConfig(), session=session, bus=bus)

    order = mounted.listeners(TOOL_EXECUTE)
    assert order[0] is not safety
    assert order[1] is safety
    # ... and the reviewer really runs first.
    seen: list[str] = []
    order[0]._record = lambda event, **payload: seen.append(event)
    result = run_waterfall(order, tool_call_request("tap", {"target_description": "返回"}), delegate)
    assert result == "executed"
    assert seen == ["systemone_review"]
