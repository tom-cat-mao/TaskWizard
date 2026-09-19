"""Work item D: fused evidence receipts for non-final observation siblings.

One model turn may carry several observation tools (``home``, ``tap``, …), and
each success would ship its own fresh screenshot + marks digest. The later
sibling's ``observe()`` re-mints the batch anyway (P0 #2) and the image-keep
window (``PHONE_AGENT_IMAGE_KEEP``, P0 #3) then prunes the earlier pictures
unseen — pure waste. These tests pin the presentation-only fix:

* a non-final observation sibling returns a compact text receipt (screen_seq,
  foreground, marks count, one-line structural diff) — no image, no digest;
* the turn's final observation sibling and single-action turns keep today's
  full ``[OBS]`` + image shape;
* ``PHONE_AGENT_SIBLING_RECEIPTS=off`` never sets the hint, so the transcript
  is the legacy shape byte-for-byte;
* a failed observation keeps its explicit failure text — never a receipt;
* receipts add no image blocks, so the keep window is not consumed by them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from phone_agent.v2.agent import (
    _ExecutionAdmissionListener,
    _non_final_observation_sibling,
    _SiblingReceiptListener,
    _ToolExecuteBridgeMiddleware,
)
from phone_agent.v2.events import TOOL_EXECUTE, EventBus
from phone_agent.v2.middleware.images import ContextPrunerService
from phone_agent.v2.tools._obs import (
    OBSERVATION_TOOLS,
    SIBLING_RECEIPT_HINT,
    sibling_receipt_requested,
)
from phone_agent.v2.tools.actuation import build_actuation_tools
from tests.v2._doubles import FakeDeviceFactory, FakePhoneSession, make_mark


class _GrowingSession(FakePhoneSession):
    """Session double whose batch grows by one mark per committed observation.

    Two observe() calls in the same turn therefore differ by exactly one mark,
    which is what the receipt's structural diff line reports.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._marks_added = 0

    def observe(self):  # noqa: ANN201 - test double mirrors FakePhoneSession
        observation = super().observe()
        self._marks_added += 1
        mark_id = f"ax_{self._marks_added}"
        self.marks[mark_id] = make_mark(mark_id, text=f"按钮{self._marks_added}")
        observation.marks = dict(self.marks)
        return observation


class _FailOnCall(FakePhoneSession):
    """Session double whose observe() raises on the Nth call of the run.

    The failure happens before the double advances anything, mirroring the real
    contract: a failed observation keeps ``screen_seq``/epoch frozen.
    """

    def __init__(self, *args: Any, fail_at: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fail_at = fail_at

    def observe(self):  # noqa: ANN201 - test double mirrors FakePhoneSession
        if self.observe_count + 1 == self._fail_at:
            self.observe_count += 1
            raise RuntimeError("synthetic observation failure")
        return super().observe()


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    i: int = 0
    seen: list[list[Any]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        self.seen.append(list(messages))
        response = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-sibling-receipts"


def _call(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _turn(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


@tool
def read_notes() -> str:
    """Read a synthetic note; no device action, no observation."""

    return "OK. notes"


def _home_tools(session) -> list[Any]:
    return [
        tool
        for tool in build_actuation_tools(session, session.config)
        if tool.name == "home"
    ]


def _run(session, responses: list[AIMessage], *, enabled: bool = True) -> dict[str, Any]:
    """Run one synthetic graph invocation with the sibling-receipt fence wired in."""

    bus = EventBus()
    bus.on(TOOL_EXECUTE, _ExecutionAdmissionListener(session))
    bus.on(TOOL_EXECUTE, _SiblingReceiptListener(session, enabled=enabled))
    model = _ScriptedModel(responses=responses)
    graph = create_agent(
        model,
        tools=[*_home_tools(session), read_notes],
        middleware=[_ToolExecuteBridgeMiddleware(bus)],
        checkpointer=InMemorySaver(),
    )
    result = graph.invoke(
        {"messages": [HumanMessage("synthetic task")]},
        {"configurable": {"thread_id": "synthetic"}, "max_concurrency": 1},
    )
    return {"result": result, "model": model}


def _tool_message(result: dict[str, Any], call_id: str) -> ToolMessage:
    return next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id
    )


def _blocks(message: Any) -> list[dict[str, Any]]:
    content = getattr(message, "content", message)
    return content if isinstance(content, list) else []


def _is_receipt(message: Any) -> bool:
    text = "".join(
        block.get("text", "")
        for block in _blocks(message)
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return "同轮中间步骤回执" in text


def _image_seqs(message: Any) -> list[int]:
    return [
        block["screen_seq"]
        for block in _blocks(message)
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]


def _text_of(message: Any) -> str:
    return "\n".join(
        block.get("text", "")
        for block in _blocks(message)
        if isinstance(block, dict) and block.get("type") == "text"
    )


# -- the exact seam: which call is a non-final observation sibling ------------


def _request(calls: list[dict[str, Any]], call_id: str) -> SimpleNamespace:
    name = next(call["name"] for call in calls if call["id"] == call_id)
    return SimpleNamespace(
        tool_call={"name": name, "id": call_id, "args": {}},
        state={
            "messages": [
                HumanMessage("task"),
                AIMessage(content="", tool_calls=list(calls)),
            ]
        },
    )


def test_detection_matrix() -> None:
    three_home = [_call("home", "a"), _call("home", "b"), _call("home", "c")]
    assert _non_final_observation_sibling(_request(three_home, "a")) is True
    assert _non_final_observation_sibling(_request(three_home, "b")) is True
    assert _non_final_observation_sibling(_request(three_home, "c")) is False

    # A trailing non-observation sibling does not give the previous one a
    # receipt: the turn's last observation still ships in full.
    doc_last = [_call("home", "a"), _call("read_notes", "b")]
    assert _non_final_observation_sibling(_request(doc_last, "a")) is False

    # read_screen is an observation tool; locate is deliberately not (its image
    # is the same frame the locate model ran on, never a fresh batch).
    perception = [_call("read_screen", "a"), _call("tap", "b", target_mark_id="ax_1")]
    assert _non_final_observation_sibling(_request(perception, "a")) is True
    locate_first = [_call("locate", "a"), _call("tap", "b", target_mark_id="ax_1")]
    assert _non_final_observation_sibling(_request(locate_first, "a")) is False

    # Fail-open: unreadable state or an unknown call id keeps today's behavior.
    unknown = SimpleNamespace(
        tool_call={"name": "home", "id": "zzz", "args": {}},
        state={"messages": [AIMessage(content="", tool_calls=list(three_home))]},
    )
    assert _non_final_observation_sibling(unknown) is False
    assert _non_final_observation_sibling(
        SimpleNamespace(tool_call={"name": "home", "id": "a", "args": {}}, state={})
    ) is False
    assert _non_final_observation_sibling(
        SimpleNamespace(tool_call={"name": "home", "id": "a", "args": {}})
    ) is False


def test_listener_sets_hint_around_the_body_and_always_clears_it() -> None:
    session = FakePhoneSession({})
    listener = _SiblingReceiptListener(session, enabled=True)
    request = _request(
        [_call("home", "a"), _call("home", "b")], "a"
    )
    observed: list[bool] = []

    def terminal(_request):  # noqa: ANN001
        observed.append(sibling_receipt_requested(session))
        return "done"

    assert listener(request, terminal) == "done"
    assert observed == [True]
    assert sibling_receipt_requested(session) is False

    def raising_terminal(_request):  # noqa: ANN001
        observed.append(sibling_receipt_requested(session))
        raise RuntimeError("tool blew up")

    try:
        listener(request, raising_terminal)
    except RuntimeError:
        pass
    assert observed == [True, True]
    assert sibling_receipt_requested(session) is False

    # The final sibling never even sees the hint.
    final_request = _request([_call("home", "a"), _call("home", "b")], "b")
    observed.clear()
    listener(final_request, terminal)
    assert observed == [False]


def test_disabled_listener_never_sets_the_hint() -> None:
    session = FakePhoneSession({})
    listener = _SiblingReceiptListener(session, enabled=False)
    request = _request([_call("home", "a"), _call("home", "b")], "a")

    def terminal(_request):  # noqa: ANN001
        assert sibling_receipt_requested(session) is False
        return "done"

    assert listener(request, terminal) == "done"
    assert getattr(session, SIBLING_RECEIPT_HINT, False) is False


# -- the rendered transcript --------------------------------------------------


def test_middle_siblings_get_receipts_and_the_last_gets_the_full_observation() -> None:
    session = _GrowingSession({})
    run = _run(
        session,
        [
            _turn(_call("home", "t1")),
            _turn(_call("home", "m1"), _call("home", "m2"), _call("home", "m3")),
            AIMessage(content="done"),
        ],
    )
    result = run["result"]

    first, middle, last = (_tool_message(result, call_id) for call_id in ("m1", "m2", "m3"))

    for message in (first, middle):
        assert _is_receipt(message) is True
        assert _image_seqs(message) == []
        assert _text_of(message).startswith("OK. home")
        # `t1`'s committed frame is the baseline for `m1`; each receipt names its
        # own screen and the structural delta against the previous observation.
        assert "marks (2)" in _text_of(first)
        assert "较上次观测：marks 1→2" in _text_of(first)
        assert "marks (3)" in _text_of(middle)
        assert "较上次观测：marks 2→3" in _text_of(middle)

    # The final observation sibling is untouched: image + digest.
    assert _is_receipt(last) is False
    assert _image_seqs(last) == [4]
    assert "marks (4)" in _text_of(last)
    assert "同轮中间步骤回执" not in _text_of(last)
    # Sampling ran for every action (P0 #15): four homes, four observations.
    assert session.observe_count == 4
    assert session.device_factory.calls.count(("home",)) == 4
    # The one-shot hint never leaks past the turn.
    assert sibling_receipt_requested(session) is False


def test_single_action_turn_is_unchanged() -> None:
    session = _GrowingSession({})
    run = _run(
        session,
        [_turn(_call("home", "only")), AIMessage(content="done")],
    )
    only = _tool_message(run["result"], "only")

    assert _is_receipt(only) is False
    assert _image_seqs(only) == [1]
    assert "marks (1)" in _text_of(only)
    assert "较上次观测" not in _text_of(only)


def test_off_flag_keeps_the_legacy_image_shape_for_every_sibling() -> None:
    session = _GrowingSession({})
    run = _run(
        session,
        [
            _turn(_call("home", "m1"), _call("home", "m2"), _call("home", "m3")),
            AIMessage(content="done"),
        ],
        enabled=False,
    )
    result = run["result"]

    for index, call_id in enumerate(("m1", "m2", "m3"), start=1):
        message = _tool_message(result, call_id)
        assert _is_receipt(message) is False
        assert _image_seqs(message) == [index]
        assert "marks (" in _text_of(message)
    assert session.observe_count == 3


def test_failed_observation_middle_sibling_keeps_its_failure_text() -> None:
    session = _FailOnCall({}, fail_at=1)
    run = _run(
        session,
        [
            _turn(_call("home", "m1"), _call("home", "m2")),
            AIMessage(content="done"),
        ],
    )
    result = run["result"]
    failed = _tool_message(result, "m1")

    assert _is_receipt(failed) is False
    assert _image_seqs(failed) == []
    assert "re-observation failed" in _text_of(failed)
    # The later sibling still gets its full observation: the failure of an
    # earlier receipt never demotes the turn's final evidence.
    assert _image_seqs(_tool_message(result, "m2")) == [1]


def test_receipt_then_failed_final_sibling_still_names_its_batch() -> None:
    """Contract item 6: a failed final sibling leaves the receipt as evidence."""

    session = _FailOnCall({}, fail_at=2)
    run = _run(
        session,
        [
            _turn(_call("home", "m1"), _call("home", "m2")),
            AIMessage(content="done"),
        ],
    )
    result = run["result"]
    receipt = _tool_message(result, "m1")
    failed = _tool_message(result, "m2")

    assert _is_receipt(receipt) is True
    # The receipt references the live batch and tells the model how to refresh.
    assert "screen#1" in _text_of(receipt)
    assert "需要 target_mark_id 寻址时先 read_screen" in _text_of(receipt)
    assert _is_receipt(failed) is False
    assert "re-observation failed" in _text_of(failed)
    # No new observation path was invented: marks stay on the last committed
    # batch, and the failure text says what actually happened.
    assert session.observe_count == 2


def test_receipts_do_not_consume_the_image_keep_window() -> None:
    def transcript(middle_has_image: bool) -> list[ToolMessage]:
        middle_content: list[dict[str, Any]] = [
            {"type": "text", "text": "OK. home"},
            (
                {
                    "type": "text",
                    "text": "[OBS] app=com.example screen#2 marks (3)",
                }
                if not middle_has_image
                else {
                    "type": "text",
                    "text": "[OBS] app=com.example screen#2\nmarks (3): ax_1|button|(1,2)",
                }
            ),
        ]
        if middle_has_image:
            middle_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,MID"},
                    "screen_seq": 2,
                }
            )
        return [
            ToolMessage(
                content=[
                    {"type": "text", "text": "OK. tap"},
                    {"type": "text", "text": "[OBS] app=com.example screen#1\nmarks (2): ax_1|a|(1,2)"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64:PRE"},
                        "screen_seq": 1,
                    },
                ],
                tool_call_id="pre",
            ),
            ToolMessage(content=middle_content, tool_call_id="middle"),
            ToolMessage(
                content=[
                    {"type": "text", "text": "OK. home"},
                    {"type": "text", "text": "[OBS] app=com.example screen#3\nmarks (4): ax_1|a|(1,2)"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64:LAST"},
                        "screen_seq": 3,
                    },
                ],
                tool_call_id="last",
            ),
        ]

    pruner = ContextPrunerService(keep_images=2, keep_marks=2)

    legacy = transcript(middle_has_image=True)
    assert pruner.prune(legacy) != []
    # Legacy shape: three image messages compete, the pre-action frame (whose
    # marks the model addressed) is the one evicted.
    assert any(
        "[screen#1 已剪除]" in block.get("text", "") for block in legacy[0].content
    )

    with_receipt = transcript(middle_has_image=False)
    assert pruner.prune(with_receipt) == []
    # The pre-action frame survives next to the final observation; the receipt
    # itself is already a compact text block and carries no image slot.
    assert not any(
        "已剪除" in block.get("text", "") for block in with_receipt[0].content
    )
    assert with_receipt[1].content[1]["text"].startswith("[OBS] app=com.example screen#2")


def test_observation_tool_roster_covers_the_auto_observation_family() -> None:
    session = _GrowingSession({"ax_1": make_mark("ax_1", text="按钮")})
    names = {tool.name for tool in build_actuation_tools(session, session.config)}
    assert OBSERVATION_TOOLS <= names | {"read_screen"}
    assert "locate" not in OBSERVATION_TOOLS
    assert "update_task_doc" not in OBSERVATION_TOOLS


def test_hint_write_failure_never_breaks_the_tool_body() -> None:
    """A session that rejects the hint write still executes the action normally."""

    class _Immutable(FakePhoneSession):
        def __setattr__(self, name, value):  # noqa: ANN001
            if name.startswith("sibling_receipt"):
                raise AttributeError("read-only session double")
            super().__setattr__(name, value)

    session = _Immutable({})
    listener = _SiblingReceiptListener(session, enabled=True)
    request = _request([_call("home", "a"), _call("home", "b")], "a")
    home = _home_tools(session)[0]

    def terminal(_request):  # noqa: ANN001
        return home.invoke({})

    result = listener(request, terminal)
    # The hint never landed, so the legacy shape is rendered — and the device
    # action plus its observation still happened.
    assert _is_receipt(result) is False
    assert _image_seqs(result) == [1]
    assert session.device_factory.calls == [("home",)]


# -- production wiring: ThinPhoneAgent registers the fence innermost ----------


@dataclass
class _AgentSession:
    """Minimal duck-typed session for the real-agent path (thin-loop wiring)."""

    config: Any = None
    marks: dict = field(default_factory=dict)
    screen_seq: int = 0
    epoch: int = 1
    finished: bool = False
    finish_summary: str | None = None
    takeover_reason: str | None = None
    finish_reviewed: bool = False
    finish_review_seq: int = -1
    finish_dispute_count: int = 0
    finish_hard_doubts: list[str] = field(default_factory=list)
    last_tool_ok: bool | None = None
    task_doc: Any = None
    run_goal: str = "synthetic task"
    device_factory: Any = field(default_factory=FakeDeviceFactory)

    def observe(self):  # noqa: ANN201 - mirrors the production Observation shape
        self.screen_seq += 1
        return SimpleNamespace(
            screenshot_b64=f"QUJD{self.screen_seq}",
            width=1080,
            height=2400,
            current_app="com.example",
            marks=dict(self.marks),
            screen_seq=self.screen_seq,
            mime_type="image/png",
        )


def _agent_config(tmp_path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "lang": "cn",
        "max_model_calls": 20,
        "max_hitl_resumes": 5,
        "trace_dir": str(tmp_path),
        "trace_enabled": True,
        "safety_mode": "off",
        "compact_enabled": False,
        "taskdoc_enabled": False,
        "app_kb_enabled": False,
        "deliverable_enabled": False,
        "experience_enabled": False,
        "finish_verify": "auto",
        "memory_rag": "off",
        "sibling_receipts_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_agent_modules(monkeypatch, session, model, tools_builder) -> None:
    import sys
    import types

    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config, *args, **kwargs: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")
    tools_mod.build_tools = tools_builder
    prompts_mod = types.ModuleType("phone_agent.v2.prompts")
    prompts_mod.get_system_prompt = lambda lang="cn": "system"
    for name, module in (
        ("phone_agent.v2.model", model_mod),
        ("phone_agent.v2.session", session_mod),
        ("phone_agent.v2.tools", tools_mod),
        ("phone_agent.v2.prompts", prompts_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize("enabled", [True, False])
def test_real_agent_wires_the_receipt_fence_innermost(
    tmp_path, monkeypatch, enabled
) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    session = _AgentSession()
    model = _ScriptedModel(
        responses=[
            _turn(_call("home", "m1"), _call("home", "m2")),
            AIMessage(content="done"),
        ]
    )
    _install_agent_modules(
        monkeypatch,
        session,
        model,
        lambda sess, config: [*_home_tools(sess), read_notes],
    )
    agent = ThinPhoneAgent(_agent_config(tmp_path, sibling_receipts_enabled=enabled))
    agent.run("synthetic task")

    # ``model_stopped`` is expected: the scripted model never calls finish. What
    # matters is the transcript the second (and last) model call actually saw.
    assert len(model.seen) == 2
    # The fence is the innermost tool/execute listener: the terminal is its next.
    listeners = agent.event_bus._listeners[TOOL_EXECUTE]
    assert listeners[-1] is agent._sibling_receipts
    assert agent._sibling_receipts.enabled is enabled

    transcript = model.seen[-1]
    tool_messages = {
        message.tool_call_id: message
        for message in transcript
        if isinstance(message, ToolMessage)
    }
    # The run-start observation already consumed screen#1, so the batch frames
    # are screen#2 and screen#3.
    if enabled:
        assert _is_receipt(tool_messages["m1"]) is True
        assert _image_seqs(tool_messages["m1"]) == []
        assert "screen#2" in _text_of(tool_messages["m1"])
    else:
        assert _is_receipt(tool_messages["m1"]) is False
        assert _image_seqs(tool_messages["m1"]) == [2]
    # The turn's final observation always ships in full.
    assert _image_seqs(tool_messages["m2"]) == [3]
    # The hint never leaks past the turn.
    assert sibling_receipt_requested(session) is False
