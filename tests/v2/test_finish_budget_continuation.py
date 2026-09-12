"""A token-boundary finish review gets one real, transaction-scoped response."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import Field

from phone_agent.v2.agent import (
    _ExecutionAdmissionListener,
    _ModelCallLimitListener,
    _ModelPreRequestBridgeMiddleware,
    _PostRequestBridgeMiddleware,
    _ToolExecuteBridgeMiddleware,
    _WrapModelBridgeMiddleware,
)
from phone_agent.v2.events import (
    MODEL_POST_REQUEST,
    MODEL_PRE_REQUEST,
    MODEL_REQUEST,
    TOOL_EXECUTE,
    EventBus,
)
from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.middleware.safety import build_control_hitl_middleware
from phone_agent.v2.review import pending_finish_review
from phone_agent.v2.taskdoc import TaskDoc, TaskItem
from phone_agent.v2.tools.control import build_control_tools
from phone_agent.v2.tools.taskdoc import make_update_task_doc_tool
from phone_agent.v2.usage import UsageLedger


@dataclass
class _Session:
    screen_seq: int = 0
    task_doc: TaskDoc | None = None
    run_goal: str = "Read the synthetic result"
    last_tool_ok: bool | None = True
    finished: bool = False
    finish_summary: str | None = None
    finish_reviewed: bool = False
    finish_review_seq: int = -1
    finish_review_ticket: Any = None
    finish_hard_doubts: list[str] = field(default_factory=list)
    finish_dispute_count: int = 0
    finish_verifier: str = "skipped"
    takeover_reason: str | None = None
    fail_observe: bool = False
    observations: int = 0
    usage_ledger: UsageLedger = field(default_factory=UsageLedger)

    def observe(self):
        self.finish_review_ticket = None
        self.observations += 1
        if self.fail_observe:
            raise RuntimeError("synthetic observation failure")
        self.screen_seq += 1
        return SimpleNamespace(
            screen_seq=self.screen_seq,
            screenshot_b64="QUJD",
            current_app="com.example.result",
            marks={},
        )


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    calls: int = 0
    requests: list[Any] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(list(messages))
        if self.calls >= len(self.responses):
            raise AssertionError("unexpected extra actor sampling")
        message = self.responses[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self):
        return "synthetic-finish-budget"


class _Verifier:
    def __init__(self, verdict="APPROVE evidence is complete", *, fail=False):
        self.verdict = verdict
        self.fail = fail
        self.requests = []

    def invoke(self, messages):
        self.requests.append(messages)
        if self.fail:
            raise RuntimeError("synthetic verifier outage")
        return AIMessage(
            content=self.verdict,
            usage_metadata={"input_tokens": 25, "output_tokens": 5, "total_tokens": 30},
        )


def _call(name, call_id, **args):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _finish(call_id, *, confirm=False, evidence=None):
    return _call(
        "finish",
        call_id,
        summary="Synthetic result recorded",
        evidence=["Synthetic result is visible"] if evidence is None else evidence,
        confirm=confirm,
        intent="Review the result",
    )


def _response(*calls, tokens=100, content=""):
    return AIMessage(
        content=content,
        tool_calls=list(calls),
        usage_metadata={
            "input_tokens": tokens,
            "output_tokens": 0,
            "total_tokens": tokens,
        },
    )


def _closed_board():
    return TaskDoc(
        goal_base="Read the synthetic result",
        items=[
            TaskItem("t1", "Read result", "completed", evidence_note="Result visible")
        ],
    )


def _review(session, *, mode="always"):
    tools = build_control_tools(session, SimpleNamespace(finish_verify=mode))
    return tools[0].invoke(_finish("issue")["args"])


def _harness(
    monkeypatch,
    responses,
    *,
    session=None,
    verifier=None,
    mode="always",
    limit=100,
    extra_tools=(),
):
    from phone_agent.v2 import verify

    session = session or _Session(task_doc=_closed_board())
    verifier = verifier or _Verifier()
    monkeypatch.setattr(verify, "_build_verifier_model", lambda config: verifier)
    config = SimpleNamespace(finish_verify=mode)
    events = []
    budget = BudgetMiddleware(
        token_budget=1000,
        warn_remaining=100,
        ledger=session.usage_ledger,
        session=session,
        trace_recorder=lambda event, **payload: events.append(
            {"event": event, **payload}
        ),
    )
    bus = EventBus()
    bus.on(MODEL_PRE_REQUEST, budget.on_pre_request)
    bus.on(MODEL_PRE_REQUEST, _ModelCallLimitListener(limit).on_pre_request)
    bus.on(MODEL_REQUEST, budget.on_model_request)
    bus.on(MODEL_POST_REQUEST, budget.on_post_request)
    admission = _ExecutionAdmissionListener(session)
    bus.on(TOOL_EXECUTE, admission)
    bus.on(TOOL_EXECUTE, build_control_hitl_middleware())
    bus.on(TOOL_EXECUTE, budget.on_tool_execute)
    operations = []

    @tool
    def synthetic_device_action() -> str:
        """Record a fake operation in memory; no device or network access."""
        operations.append("operation")
        session.observe()
        return "ok"

    model = _ScriptedModel(responses=responses)
    graph = create_agent(
        model,
        tools=[
            *build_control_tools(session, config),
            make_update_task_doc_tool(session, "cn"),
            synthetic_device_action,
            *extra_tools,
        ],
        middleware=[
            _ModelPreRequestBridgeMiddleware(bus, admission.is_terminal),
            _WrapModelBridgeMiddleware(bus),
            _PostRequestBridgeMiddleware(bus, "synthetic-finish-budget"),
            _ToolExecuteBridgeMiddleware(bus),
        ],
        checkpointer=InMemorySaver(),
    )
    return SimpleNamespace(
        graph=graph,
        model=model,
        session=session,
        verifier=verifier,
        budget=budget,
        operations=operations,
        events=events,
        config={"configurable": {"thread_id": "synthetic"}, "max_concurrency": 1},
    )


def _invoke(harness):
    return harness.graph.invoke(
        {
            "messages": [
                SystemMessage("Synthetic actor"),
                HumanMessage("Read the result"),
            ]
        },
        harness.config,
    )


def _stages(harness):
    return [
        row["stage"]
        for row in harness.events
        if row["event"] == "token_budget_finish_continuation"
    ]


def test_closed_route_review_crosses_budget_then_real_actor_confirms(monkeypatch):
    session = _Session(task_doc=TaskDoc(goal_base="Read the synthetic result"))
    harness = _harness(
        monkeypatch,
        [
            _response(
                _call(
                    "update_task_doc",
                    "start",
                    items=[
                        {"id": "t1", "content": "Read result", "status": "in_progress"}
                    ],
                ),
                tokens=200,
            ),
            _response(
                _call(
                    "update_task_doc",
                    "close",
                    items=[
                        {
                            "id": "t1",
                            "content": "Read result",
                            "status": "completed",
                            "evidence_note": "Result visible",
                        }
                    ],
                ),
                tokens=200,
            ),
            _response(_finish("review"), tokens=700),
            _response(_finish("confirm", confirm=True)),
        ],
        session=session,
    )
    result = _invoke(harness)

    assert harness.model.calls == 4
    assert any(
        "[FINISH 复核包]" in str(message.content)
        for message in harness.model.requests[-1]
    )
    assert session.finished is True
    assert session.finish_verifier == "pass"
    assert harness.budget.exhausted is True
    assert session.usage_ledger.by_role() == {"actor": 1200, "verifier": 30}
    assert len(harness.verifier.requests) == 1
    assert all(
        "Synthetic actor" not in str(message.content)
        for message in harness.verifier.requests[0]
    )
    assert [m.name for m in result["messages"] if isinstance(m, ToolMessage)][-2:] == [
        "finish",
        "finish",
    ]
    assert _stages(harness) == ["granted", "used", "confirmed"]


@pytest.mark.parametrize("ordinary_first", [False, True])
def test_continuation_never_funds_ordinary_siblings(monkeypatch, ordinary_first):
    device = _call("synthetic_device_action", "device")
    confirm = _finish("confirm", confirm=True)
    calls = (device, confirm) if ordinary_first else (confirm, device)
    harness = _harness(
        monkeypatch, [_response(_finish("review"), tokens=1100), _response(*calls)]
    )
    result = _invoke(harness)
    device_result = next(
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.tool_call_id == "device"
    )

    assert harness.session.finished is True
    assert harness.operations == []
    assert device_result.status == "error"
    assert harness.model.calls == 2
    assert len(harness.verifier.requests) == 1


def test_threshold_crossing_response_can_still_issue_first_review(monkeypatch):
    harness = _harness(
        monkeypatch,
        [
            _response(
                _call("synthetic_device_action", "normal"),
                _finish("review"),
                tokens=1100,
            ),
            _response(_finish("confirm", confirm=True)),
        ],
    )
    _invoke(harness)
    assert harness.operations == ["operation"]
    assert harness.session.finished is True
    assert harness.model.calls == 2


@pytest.mark.parametrize(
    "kind",
    [
        "no_review",
        "failed_observation",
        "stale",
        "open_route",
        "missing_evidence",
        "changed_board",
        "changed_goal",
    ],
)
def test_missing_invalid_or_changed_ticket_does_not_extend_budget(kind):
    session = _Session(task_doc=_closed_board())
    if kind == "failed_observation":
        session.fail_observe = True
    if kind != "no_review":
        _review(session)
    if kind == "no_review":
        session.finish_reviewed = (
            True  # A boolean without a minted ticket is insufficient.
        )
        session.finish_review_seq = session.screen_seq
    elif kind == "stale":
        session.observe()
    elif kind == "open_route":
        session.task_doc.items[0].status = "in_progress"
    elif kind == "missing_evidence":
        session.task_doc.items[0].evidence_note = None
    elif kind == "changed_board":
        session.task_doc.facts.append("New fact after review")
    elif kind == "changed_goal":
        session.run_goal = "A different goal"
    session.usage_ledger.record("actor", estimate_tokens=1000)
    budget = BudgetMiddleware(
        token_budget=1000, ledger=session.usage_ledger, session=session
    )

    result = budget.before_model({"messages": []}, None)
    assert result["jump_to"] == "end"
    assert budget.exhausted is True


def test_ticket_requires_nonempty_review_evidence():
    session = _Session(task_doc=_closed_board())
    finish = build_control_tools(session, SimpleNamespace(finish_verify="always"))[0]
    assert "error:" in finish.invoke(_finish("review", evidence=[])["args"])
    assert pending_finish_review(session) is None


@pytest.mark.parametrize(
    "kind", ["decline", "repeat_review", "empty_evidence", "ordinary_only"]
)
def test_one_response_cannot_refresh_or_bypass_confirmation(monkeypatch, kind):
    response = {
        "decline": _response(content="The result is not ready."),
        "repeat_review": _response(_finish("again"), _finish("later", confirm=True)),
        "empty_evidence": _response(
            _finish("empty", confirm=True, evidence=[]), _finish("later", confirm=True)
        ),
        "ordinary_only": _response(_call("synthetic_device_action", "device")),
    }[kind]
    harness = _harness(
        monkeypatch, [_response(_finish("review"), tokens=1100), response]
    )
    _invoke(harness)

    assert harness.model.calls == 2
    assert harness.session.finished is False
    assert harness.budget.exhausted is True
    assert harness.operations == []
    assert harness.verifier.requests == []
    assert _stages(harness).count("granted") == 1
    assert _stages(harness).count("exhausted") == 1
    assert harness.session.observations == 1


def test_rejected_verifier_has_no_second_sibling_attempt(monkeypatch):
    harness = _harness(
        monkeypatch,
        [
            _response(_finish("review"), tokens=1100),
            _response(_finish("confirm", confirm=True), _finish("retry", confirm=True)),
        ],
        verifier=_Verifier("REJECT missing required evidence"),
    )
    result = _invoke(harness)
    assert harness.session.finished is False
    assert harness.session.finish_verifier == "fail"
    assert harness.session.finish_dispute_count == 1
    assert len(harness.verifier.requests) == 1
    retry = next(
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.tool_call_id == "retry"
    )
    assert retry.status == "error"
    assert harness.model.calls == 2


def test_verifier_outage_retains_existing_fail_open_skipped(monkeypatch):
    harness = _harness(
        monkeypatch,
        [
            _response(_finish("review"), tokens=1100),
            _response(_finish("confirm", confirm=True)),
        ],
        verifier=_Verifier(fail=True),
    )
    _invoke(harness)
    assert harness.session.finished is True
    assert harness.session.finish_verifier == "skipped"
    assert len(harness.verifier.requests) == 1
    assert harness.session.usage_ledger.by_role() == {"actor": 1200}


def test_model_can_request_takeover_and_resume_without_new_allowance(monkeypatch):
    harness = _harness(
        monkeypatch,
        [
            _response(_finish("review"), tokens=1100),
            _response(
                _call("take_over", "handoff", reason="Human review needed"),
                _call("synthetic_device_action", "sibling"),
            ),
        ],
    )
    interrupted = _invoke(harness)
    assert interrupted["__interrupt__"]
    assert harness.session.takeover_reason is None
    resumed = harness.graph.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}), harness.config
    )

    assert harness.session.takeover_reason == "Human review needed"
    assert harness.model.calls == 2
    assert harness.operations == []
    assert _stages(harness).count("granted") == 1
    assert (
        next(
            m
            for m in resumed["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == "sibling"
        ).status
        == "error"
    )


@pytest.mark.parametrize(
    "name,decision",
    [
        ("ask_user", {"type": "respond", "message": "Not complete"}),
        ("take_over", {"type": "reject"}),
    ],
)
def test_hitl_response_or_rejection_does_not_resample_actor(
    monkeypatch, name, decision
):
    args = (
        {"question": "Is it complete?"}
        if name == "ask_user"
        else {"reason": "Human review"}
    )
    harness = _harness(
        monkeypatch,
        [
            _response(_finish("review"), tokens=1100),
            _response(_call(name, "hitl", **args)),
        ],
    )
    interrupted = _invoke(harness)
    assert interrupted["__interrupt__"]
    harness.graph.invoke(Command(resume={"decisions": [decision]}), harness.config)
    assert harness.model.calls == 2
    assert harness.session.finished is False
    assert harness.session.takeover_reason is None
    assert harness.budget.exhausted is True


def test_latch_survives_repeated_packet_and_reset_requires_new_ticket():
    session = _Session(task_doc=_closed_board())
    budget = BudgetMiddleware(
        token_budget=1000, ledger=session.usage_ledger, session=session
    )
    _review(session)
    session.usage_ledger.record("actor", estimate_tokens=1000)
    assert "jump_to" not in budget.before_model({"messages": []}, None)
    _review(session)
    assert budget.before_model({"messages": []}, None)["jump_to"] == "end"
    budget.reset()
    assert pending_finish_review(session) is None
    assert session.usage_ledger.total == 0
    _review(session)
    session.usage_ledger.record("actor", estimate_tokens=1000)
    assert "jump_to" not in budget.before_model({"messages": []}, None)


def test_verify_off_has_no_extra_actor_or_review(monkeypatch):
    harness = _harness(
        monkeypatch, [_response(_finish("finish"), tokens=1100)], mode="off"
    )
    _invoke(harness)
    assert harness.model.calls == 1
    assert harness.session.finished is True
    assert harness.session.finish_review_ticket is None
    assert harness.verifier.requests == []
    assert _stages(harness) == []


def test_loop_fuse_remains_authoritative(monkeypatch):
    harness = _harness(
        monkeypatch, [_response(_finish("review"), tokens=1100)], limit=1
    )
    _invoke(harness)
    assert harness.model.calls == 1
    assert harness.session.finished is False
    assert harness.verifier.requests == []
    assert "used" not in _stages(harness)


@pytest.mark.parametrize("change", ["screen", "board", "ticket"])
def test_changed_transaction_after_actor_grant_cannot_reach_finish(change):
    session = _Session(task_doc=_closed_board())
    budget = BudgetMiddleware(
        token_budget=1000, ledger=session.usage_ledger, session=session
    )
    _review(session)
    session.usage_ledger.record("actor", estimate_tokens=1000)
    budget.before_model({"messages": []}, None)
    budget.on_model_request(SimpleNamespace(messages=[]), lambda request: None)
    confirm = _finish("confirm", confirm=True)
    budget.after_model({"messages": [_response(confirm)]}, None)
    if change == "screen":
        session.observe()
    elif change == "board":
        session.task_doc.facts.append("Changed after actor admission")
    else:
        _review(session)
    calls = []
    result = budget.on_tool_execute(
        SimpleNamespace(tool_call=confirm), lambda request: calls.append(request)
    )
    assert result.status == "error"
    assert calls == []
    assert session.finished is False


def test_legacy_middleware_mount_has_same_resource_fence(monkeypatch):
    from phone_agent.v2 import verify

    session = _Session(task_doc=_closed_board())
    verifier = _Verifier()
    monkeypatch.setattr(verify, "_build_verifier_model", lambda config: verifier)
    budget = BudgetMiddleware(
        token_budget=1000, ledger=session.usage_ledger, session=session
    )
    model = _ScriptedModel(
        responses=[
            _response(_finish("review"), tokens=1100),
            _response(
                _call("update_task_doc", "mutate", facts=["must not write"]),
                _finish("confirm", confirm=True),
            ),
        ]
    )
    graph = create_agent(
        model,
        tools=[
            *build_control_tools(session, SimpleNamespace(finish_verify="always")),
            make_update_task_doc_tool(session, "cn"),
        ],
        middleware=[budget],
    )
    result = graph.invoke({"messages": [HumanMessage("Read the result")]})
    assert session.finished is True
    assert session.task_doc.facts == []
    assert len(verifier.requests) == 1
    mutation = next(
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.tool_call_id == "mutate"
    )
    assert mutation.status == "error"


def test_trace_never_contains_review_board_or_fingerprint(monkeypatch):
    session = _Session(task_doc=_closed_board(), run_goal="PRIVATE_SYNTHETIC_GOAL")
    session.task_doc.facts.append("PRIVATE_SYNTHETIC_FACT")
    harness = _harness(
        monkeypatch,
        [
            _response(_finish("review"), tokens=1100),
            _response(_finish("confirm", confirm=True)),
        ],
        session=session,
    )
    _invoke(harness)
    assert _stages(harness) == ["granted", "used", "confirmed"]
    assert "PRIVATE_SYNTHETIC" not in str(harness.events)
    assert "fingerprint" not in str(harness.events)


def test_real_session_failed_observation_invalidates_ticket(monkeypatch):
    from phone_agent.v2.config import V2Config
    from phone_agent.v2.session import PhoneSession, ScreenshotError

    session = PhoneSession(
        V2Config(
            base_url="https://example.invalid",
            model_name="synthetic",
            grounding_provider="fake",
            observe_settle_ms=0,
        ),
        device_factory=SimpleNamespace(),
    )
    ticket = object()
    session.finish_review_ticket = ticket

    def fail():
        raise ScreenshotError("synthetic screenshot error")

    monkeypatch.setattr(session, "_foreground_observation", fail)
    with pytest.raises(ScreenshotError):
        session.observe()
    assert session.finish_review_ticket is None
    assert session.screen_seq == 0


def _uncertain_back_session():
    """Real session/back tool with a fake command that changes world then fails."""

    from phone_agent.v2.tools.actuation import build_actuation_tools
    from tests.v2.test_observation_lifecycle import _session

    session = _session()
    session.config.observe_settle_ms = 0
    session.config.finish_verify = "auto"
    session.task_doc = _closed_board()
    session.run_goal = session.task_doc.goal_base
    session.last_tool_ok = True
    session.usage_ledger = UsageLedger()
    world = {"screen": "result", "dispatches": 0}

    def uncertain_back(*, device_id=None):
        world["screen"] = "previous screen"
        world["dispatches"] += 1
        raise RuntimeError("synthetic transport failed after dispatch")

    session.device_factory.back = uncertain_back
    back = next(
        action
        for action in build_actuation_tools(session, session.config)
        if action.name == "back"
    )
    return session, back, world


@pytest.mark.parametrize("review_before_dispatch", [True, False])
def test_unknown_real_back_dispatch_requires_a_subsequent_review(
    monkeypatch, review_before_dispatch
):
    session, back, world = _uncertain_back_session()
    calls = [_finish("review"), _call("back", "back", intent="Go back")]
    if not review_before_dispatch:
        calls.reverse()
    harness = _harness(
        monkeypatch,
        [_response(*calls, tokens=1100), _response(_finish("confirm", confirm=True))],
        session=session,
        mode="auto",
        extra_tools=[back],
    )
    result = _invoke(harness)
    receipt = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "back"
    )
    assert "设备结果无法确认" in receipt.content
    assert world == {"screen": "previous screen", "dispatches": 1}
    assert session.last_tool_ok is False
    if review_before_dispatch:
        # No observation followed the uncertain command, so seq alone is not
        # proof that the world underlying the earlier review still exists.
        assert session.screen_seq == 1
        assert session.finish_reviewed is True
        assert pending_finish_review(session) is None
        assert harness.model.calls == 1
        assert session.finished is False
        assert harness.verifier.requests == []
        assert "granted" not in _stages(harness)
    else:
        # A genuine new review after a failed action can still be confirmed.
        # Do not replace review provenance with a last_tool_ok=False shortcut.
        assert session.finished is True
        assert harness.model.calls == 2
        assert len(harness.verifier.requests) == 1
        assert session.finish_verifier == "pass"


@pytest.mark.parametrize(
    "name", ["back", "tap", "type_text", "write_document", "unknown_plugin_tool"]
)
@pytest.mark.parametrize("raises", [False, True])
def test_ordinary_delegation_supersedes_review_before_any_unknown_result(name, raises):
    session = _Session(task_doc=_closed_board())
    _review(session)
    seq = session.screen_seq
    ledger = session.usage_ledger
    budget = BudgetMiddleware(token_budget=1000, ledger=ledger, session=session)
    dispatched = []

    def dispatch(request):
        assert session.finish_review_ticket is None
        dispatched.append(name)
        if raises:
            raise RuntimeError("synthetic failure after dispatch")
        return "error: dispatched, outcome unknown"

    request = SimpleNamespace(tool_call=_call(name, "ordinary"))
    if raises:
        with pytest.raises(RuntimeError, match="after dispatch"):
            budget.on_tool_execute(request, dispatch)
    else:
        assert "outcome unknown" in budget.on_tool_execute(request, dispatch)
    assert dispatched == [name]
    assert session.screen_seq == seq
    ledger.record("actor", estimate_tokens=1000)
    assert budget.before_model({"messages": []}, None)["jump_to"] == "end"


@pytest.mark.parametrize(
    "name,decision,args",
    [
        (
            "ask_user",
            {"type": "respond", "message": "Continue"},
            {"question": "Continue?"},
        ),
        ("take_over", {"type": "reject"}, {"reason": "Human review"}),
    ],
)
def test_post_hitl_ordinary_dispatch_cannot_reuse_pre_interrupt_review(
    monkeypatch, name, decision, args
):
    session, back, world = _uncertain_back_session()
    harness = _harness(
        monkeypatch,
        [
            _response(
                _finish("review"),
                _call(name, "hitl", **args),
                _call("back", "back", intent="Go back"),
                tokens=1100,
            ),
            _response(_finish("confirm", confirm=True)),
        ],
        session=session,
        mode="auto",
        extra_tools=[back],
    )
    interrupted = _invoke(harness)
    assert interrupted["__interrupt__"]
    assert world["dispatches"] == 0
    assert pending_finish_review(session) is not None
    harness.graph.invoke(Command(resume={"decisions": [decision]}), harness.config)
    assert world["dispatches"] == 1
    assert pending_finish_review(session) is None
    assert session.finished is False
    assert harness.model.calls == 1
    assert "granted" not in _stages(harness)


def test_unexecuted_human_question_does_not_invalidate_review(monkeypatch):
    harness = _harness(
        monkeypatch,
        [
            _response(
                _finish("review"),
                _call("ask_user", "ask", question="Is the result ready?"),
                tokens=1100,
            ),
            _response(_finish("confirm", confirm=True)),
        ],
    )
    interrupted = _invoke(harness)
    assert interrupted["__interrupt__"]
    ticket = pending_finish_review(harness.session)
    assert ticket is not None
    harness.graph.invoke(
        Command(resume={"decisions": [{"type": "respond", "message": "Yes"}]}),
        harness.config,
    )
    assert harness.session.finished is True
    assert harness.model.calls == 2
    assert _stages(harness).count("granted") == 1


def test_normal_attribute_style_tool_request_keeps_delegation_compatible():
    session = _Session(task_doc=_closed_board())
    _review(session)
    budget = BudgetMiddleware(session=session)
    request = SimpleNamespace(
        tool_call=SimpleNamespace(name="unknown_plugin_tool", id="plugin", args={})
    )
    delegated = []
    result = budget.on_tool_execute(
        request, lambda call: delegated.append(call) or "plugin result"
    )
    assert result == "plugin result"
    assert delegated == [request]
    assert pending_finish_review(session) is None
