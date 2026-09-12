"""S1 pre-request protocol tests: full-list waterfall + single bridge update.

Contract under test (S1 hardening): every ``model/pre_request`` listener is a
**full message list in, full message list out** transform that never emits a
``RemoveMessage``; the pre-request bridge alone mints the single legal LangGraph
update — ``[RemoveMessage(REMOVE_ALL_MESSAGES), *transformed_list]``.

Why this kills real bugs (all verified against installed langgraph/langchain):

* langgraph's ``add_messages`` executes ``REMOVE_ALL_MESSAGES`` as
  ``return right[remove_all_idx + 1:]`` **verbatim** — any further
  ``RemoveMessage`` inside the payload survives into state and the next model
  call dies in ``langchain_openai..._convert_message_to_dict`` with
  ``TypeError: Got unknown type`` (the old compact+taskdoc reducer-delta mix).
* A listener that *replaces* the transformed transcript with a one-message
  addition (the old budget warn path) silently discards a same-turn T2 fold.

These tests run the REAL reducer (``langgraph.graph.message.add_messages``) and
the REAL wire converter (``langchain_openai.chat_models.base.
_convert_message_to_dict``) — no shortcuts.

All fakes: no device, no network, no MLX.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from phone_agent.v2.agent import _ModelPreRequestBridgeMiddleware
from phone_agent.v2.events import EventBus, MODEL_PRE_REQUEST
from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.middleware.compact import CompactMiddleware
from phone_agent.v2.middleware.taskdoc import TaskDocInjector
from phone_agent.v2.pins import TASKDOC_ID_PREFIX
from phone_agent.v2.usage import UsageLedger


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeSummariser:
    """Text-only stand-in for the compact summariser model."""

    def __init__(self, text: str = "## 目标\n续接原任务\n## 下一步\n连接 WLAN") -> None:
        self.text = text
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content=self.text)


class _FakeTaskDoc:
    def __init__(self, goal: str = "打开设置并连上 WLAN") -> None:
        self.goal = goal

    def render(self, lang: str = "cn") -> str:  # noqa: ARG002
        return f"## 目标\nbase: {self.goal}"


def _make_session(task_doc: Any = None) -> Any:
    """Minimal compact/taskdoc session surface (no usage ledger -> skip)."""

    return type("S", (), {"task_doc": task_doc, "usage_ledger": None})()


def _ai_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _tool_receipt(call_id: str, text: str, name: str = "tap") -> ToolMessage:
    return ToolMessage(content=text, id=f"m-{call_id}", tool_call_id=call_id, name=name)


def _wire_clean(messages: list[Any]) -> None:
    """Every state message must survive the real OpenAI wire converter."""

    from langchain_openai.chat_models.base import _convert_message_to_dict

    for message in messages:
        assert isinstance(message, BaseMessage), f"non-message in state: {message!r}"
        # A RemoveMessage residue raises TypeError: Got unknown type here.
        _convert_message_to_dict(message)


def _count_prefix(messages: list[Any], prefix: str) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m, SystemMessage)
        and str(m.content).startswith(prefix)
    )


def _build_chain(
    *,
    compact: CompactMiddleware | None = None,
    injector: TaskDocInjector | None = None,
    budget: BudgetMiddleware | None = None,
) -> tuple[_ModelPreRequestBridgeMiddleware, EventBus]:
    """Register the given listeners in production nesting order on a real bus."""

    bus = EventBus()
    if compact is not None:
        # Compact is prepended: outermost listener, exactly like production.
        bus.on(MODEL_PRE_REQUEST, compact.on_pre_request, prepend=True)
    if injector is not None:
        bus.on(MODEL_PRE_REQUEST, injector)
    if budget is not None:
        bus.on(MODEL_PRE_REQUEST, budget.on_pre_request)
    return _ModelPreRequestBridgeMiddleware(bus), bus


def _make_compact(
    *,
    summariser: _FakeSummariser | None = None,
    window: int = 20_000,
    warn_ratio: float = 0.75,
    trigger_ratio: float = 0.92,
    keep_ratio: float = 0.5,
    schema_reserve: int = 0,
    output_reserve: int = 0,
    task_doc: Any = None,
) -> CompactMiddleware:
    return CompactMiddleware(
        _make_session(task_doc=task_doc),
        type("C", (), {"model_name": "test-model", "context_window": window})(),
        model=summariser or _FakeSummariser(),
        warn_ratio=warn_ratio,
        trigger_ratio=trigger_ratio,
        keep_ratio=keep_ratio,
        schema_reserve=schema_reserve,
        output_reserve=output_reserve,
        lang="cn",
    )


# ---------------------------------------------------------------------------
# T2 fold + stale TaskDoc through the REAL bridge -> REAL reducer -> REAL wire
# ---------------------------------------------------------------------------
def test_t2_fold_plus_stale_taskdoc_wires_clean():
    """The BUG 1 scenario: a T2 fold lands on a transcript that already carries
    an injected ``[TASK_DOC]`` block. The old reducer-delta mix left a raw
    ``RemoveMessage`` + two TaskDoc blocks in state and killed the next model
    call at the wire converter. Through the full-list protocol the final state
    has exactly one TaskDoc, one summary, zero RemoveMessage residue, and every
    message converts for the OpenAI wire.
    """

    summariser = _FakeSummariser()
    taskdoc_old_id = f"{TASKDOC_ID_PREFIX}old"
    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    injector._injected_id = taskdoc_old_id

    history: list[Any] = [
        SystemMessage(content="系统提示词", id="sys-1"),
        HumanMessage(content="打开设置并连上 WLAN"),
    ]
    for index in range(8):
        history.append(
            _ai_call("tap", {"target_mark_id": f"ax_{index}"}, f"c{index}")
        )
        history.append(_tool_receipt(f"c{index}", "x" * 12_000))
    history.append(
        SystemMessage(content="[TASK_DOC]\n## 目标\nbase: 旧任务板", id=taskdoc_old_id)
    )

    compact = _make_compact(summariser=summariser)
    bridge, _bus = _build_chain(compact=compact, injector=injector)

    update = bridge.before_model({"messages": history}, None)
    assert update is not None
    # Exactly one legal LangGraph update, minted by the bridge alone.
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES

    state = add_messages(history, update["messages"])

    assert not any(isinstance(m, RemoveMessage) for m in state)
    assert _count_prefix(state, "[TASK_DOC]") == 1
    assert _count_prefix(state, "[COMPACT_SUMMARY]") == 1
    # The stale pinned copy is gone (replaced, not duplicated).
    assert all(getattr(m, "id", None) != taskdoc_old_id for m in state)
    # Head system prompt survived the fold.
    assert state[0].content == "系统提示词"
    # Every ToolMessage kept its AIMessage pair before it.
    _assert_pairs_intact(state)
    _wire_clean(state)
    # The summariser actually ran (the fold is real, not a skipped fold).
    assert summariser.calls


def test_t2_fold_and_budget_warn_same_turn():
    """BUG 2 scenario: a same-turn budget warn must extend the folded
    transcript, not replace it. Final state carries BOTH the hand-off summary
    and the warn message.
    """

    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    history: list[Any] = [
        SystemMessage(content="系统提示词", id="sys-1"),
        HumanMessage(content="任务"),
    ]
    for index in range(8):
        history.append(
            _ai_call("tap", {"target_mark_id": f"ax_{index}"}, f"c{index}")
        )
        history.append(_tool_receipt(f"c{index}", "x" * 12_000))

    ledger = UsageLedger()
    ledger.record("compact", estimate_tokens=850)
    budget = BudgetMiddleware(token_budget=1000, warn_remaining=200, ledger=ledger)

    compact = _make_compact()
    bridge, _bus = _build_chain(compact=compact, injector=injector, budget=budget)

    update = bridge.before_model({"messages": history}, None)
    state = add_messages(history, update["messages"])

    assert _count_prefix(state, "[COMPACT_SUMMARY]") == 1
    assert _count_prefix(state, "[TASK_DOC]") == 1
    warns = [
        m
        for m in state
        if isinstance(m, SystemMessage) and "Token 预算余量" in str(m.content)
    ]
    assert len(warns) == 1
    # The warn is the newest addition, after the refreshed TaskDoc block.
    assert state[-1] is warns[0]
    assert not any(isinstance(m, RemoveMessage) for m in state)
    _wire_clean(state)


def test_t1_warn_keeps_full_transcript_and_flow_line():
    """A T1 warn turn must not truncate the transcript: the TaskDoc block the
    downstream injector pins still derives ``## 流程线`` from the FULL
    transcript (tool_calls + receipts), and the history stays verbatim.
    """

    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    history: list[Any] = [
        SystemMessage(content="系统提示词", id="sys-1"),
        HumanMessage(content="任务"),
    ]
    for index in range(16):
        history.append(
            _ai_call(
                "tap",
                {"target_mark_id": f"ax_{index}", "intent": f"点击目标{index}"},
                f"c{index}",
            )
        )
        history.append(_tool_receipt(f"c{index}", "x" * 4_000))

    budget = BudgetMiddleware(token_budget=1_000_000, warn_remaining=100_000)
    compact = _make_compact(warn_ratio=0.75, trigger_ratio=0.92)
    bridge, _bus = _build_chain(compact=compact, injector=injector, budget=budget)

    update = bridge.before_model({"messages": history}, None)
    assert update is not None
    state = add_messages(history, update["messages"])

    # No fold happened (T1 only): the transcript survived verbatim.
    assert _count_prefix(state, "[COMPACT_SUMMARY]") == 0
    assert (
        sum(
            1
            for m in state
            if isinstance(m, ToolMessage) and m.content == "x" * 4_000
        )
        == 16
    )
    # One warn injected.
    assert _count_prefix(state, "[COMPACT_WARN]") == 1
    # ...and the TaskDoc block still carries the flow line from the full
    # transcript (last 8 entries; intent #15 is the newest).
    taskdoc_blocks = [m for m in state if _count_prefix([m], "[TASK_DOC]")]
    assert len(taskdoc_blocks) == 1
    block_text = taskdoc_blocks[0].content
    assert "## 流程线" in block_text
    assert "点击目标15" in block_text
    assert "（未声明）" not in block_text.split("## 流程线")[1]
    _wire_clean(state)


def test_two_consecutive_t2_folds_stay_stable_and_wire_clean():
    """Fold, keep working, fold again: the iterative compaction must stay a
    fixed point of the protocol — one summary (prior superseded), one TaskDoc,
    no residue, wire-clean — with the prior summary fed back to the
    summariser.
    """

    summariser = _FakeSummariser()
    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    compact = _make_compact(summariser=summariser)

    history: list[Any] = [
        SystemMessage(content="系统提示词", id="sys-1"),
        HumanMessage(content="任务"),
    ]
    for index in range(8):
        history.append(
            _ai_call("tap", {"target_mark_id": f"ax_{index}"}, f"c{index}")
        )
        history.append(_tool_receipt(f"c{index}", "x" * 12_000))

    bridge, _bus = _build_chain(compact=compact, injector=injector)

    update1 = bridge.before_model({"messages": history}, None)
    state1 = add_messages(history, update1["messages"])
    assert _count_prefix(state1, "[COMPACT_SUMMARY]") == 1
    folds_after_first = len(summariser.calls)

    # The agent keeps working: more heavy turns push the context over T2 again.
    history2 = list(state1)
    for index in range(8, 16):
        history2.append(
            _ai_call("tap", {"target_mark_id": f"ax_{index}"}, f"c{index}")
        )
        history2.append(_tool_receipt(f"c{index}", "x" * 12_000))

    update2 = bridge.before_model({"messages": history2}, None)
    state2 = add_messages(state1, update2["messages"])

    assert len(summariser.calls) > folds_after_first  # a second fold ran
    # The prior summary was fed back into the second summariser call.
    second_input_text = "\n".join(
        str(getattr(m, "content", "")) for m in summariser.calls[-1]
    )
    assert "续接原任务" in second_input_text
    assert _count_prefix(state2, "[COMPACT_SUMMARY]") == 1
    assert _count_prefix(state2, "[TASK_DOC]") == 1
    assert not any(isinstance(m, RemoveMessage) for m in state2)
    _assert_pairs_intact(state2)
    _wire_clean(state2)


def test_pair_straddling_fold_cut_stays_paired():
    """The fold cut must never split an AIMessage/tool_result pair: heavy
    tool_calls args (the dominant output of this agent) make the raw keep-budget
    cut land on a ToolMessage; ``_safe_tail_start`` must push it so the tail
    never begins with a dangling receipt and no kept ToolMessage loses its
    AIMessage. Also proves tool_calls args are *counted* (with the old
    content-only estimate these AIMessages weigh ~0 and the cut moves).
    """

    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    big_args = {"html": "y" * 4_000, "intent": "写入文档"}
    history: list[Any] = [
        SystemMessage(content="系统提示词", id="sys-1"),
        HumanMessage(content="任务"),
    ]
    for index in range(12):
        history.append(_ai_call("write_document", big_args, f"c{index}"))
        history.append(_tool_receipt(f"c{index}", "OK"))

    compact = _make_compact(window=10_000)
    bridge, _bus = _build_chain(compact=compact, injector=injector)

    update = bridge.before_model({"messages": history}, None)
    state = add_messages(history, update["messages"])

    assert _count_prefix(state, "[COMPACT_SUMMARY]") == 1
    # No dangling receipts: every ToolMessage in state has its AIMessage pair
    # before it, and the tail after the summary starts on a non-ToolMessage.
    _assert_pairs_intact(state)
    summary_index = next(
        i for i, m in enumerate(state) if _count_prefix([m], "[COMPACT_SUMMARY]")
    )
    assert not isinstance(state[summary_index + 1], ToolMessage)
    _wire_clean(state)


def _assert_pairs_intact(messages: list[Any]) -> None:
    """Every ToolMessage must have its AIMessage pair earlier in the list."""

    seen_call_ids: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                seen_call_ids.add(str(call.get("id")))
        elif isinstance(message, ToolMessage):
            assert str(message.tool_call_id) in seen_call_ids, (
                "dangling tool_result without its tool_use in state"
            )


# ---------------------------------------------------------------------------
# budget adapter contract
# ---------------------------------------------------------------------------
def test_budget_warn_alone_appends_exactly_one_warn_message():
    ledger = UsageLedger()
    ledger.record("compact", estimate_tokens=900)
    budget = BudgetMiddleware(token_budget=1000, warn_remaining=200, ledger=ledger)
    bridge, _bus = _build_chain(budget=budget)

    history = [HumanMessage(content="任务"), AIMessage(content="好的", id="a1")]
    update = bridge.before_model({"messages": history}, None)
    assert update is not None

    state = add_messages(history, update["messages"])
    warns = [
        m
        for m in state
        if isinstance(m, SystemMessage) and "Token 预算余量" in str(m.content)
    ]
    assert len(warns) == 1
    assert len(state) == len(history) + 1
    assert state[:-1] == history
    _wire_clean(state)


def test_budget_hard_ceiling_returns_jump_to_end():
    ledger = UsageLedger()
    ledger.record("compact", estimate_tokens=1500)
    budget = BudgetMiddleware(token_budget=1000, warn_remaining=200, ledger=ledger)
    bridge, _bus = _build_chain(budget=budget)

    history = [HumanMessage(content="任务")]
    result = bridge.before_model({"messages": history}, None)

    assert result is not None
    assert result["jump_to"] == "end"
    assert any(
        "[TOKEN_BUDGET_EXHAUSTED]" in str(getattr(m, "content", ""))
        for m in result["messages"]
    )


# ---------------------------------------------------------------------------
# compact reserve math (BUG 4)
# ---------------------------------------------------------------------------
def test_compact_reserves_advance_t2_trigger():
    """effective = estimated_context + schema_reserve + output_reserve must be
    what the T1/T2 thresholds compare against: identical history stays silent
    without reserves and folds once the reserves push it over T2.
    """

    def _history() -> list[Any]:
        messages: list[Any] = [
            SystemMessage(content="系统提示词", id="sys-1"),
            HumanMessage(content="任务"),
        ]
        for index in range(7):
            messages.append(
                _ai_call("tap", {"target_mark_id": f"ax_{index}"}, f"c{index}")
            )
            messages.append(_tool_receipt(f"c{index}", "x" * 8_000))  # ~2000 tok
        return messages  # ≈ 14_150 estimated tokens

    # window 20_000: T1 at 15_000, T2 at 18_400. 14_150 sits between the
    # no-reserve silence line (15_000) and 18_400 - 5_000 = 13_400.
    no_reserve = _make_compact(window=20_000, schema_reserve=0, output_reserve=0)
    assert no_reserve.before_model({"messages": _history()}, None) is None

    with_reserve = _make_compact(
        window=20_000, schema_reserve=3_000, output_reserve=2_000
    )
    update = with_reserve.before_model({"messages": _history()}, None)
    assert update is not None
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES


def test_compact_build_reads_reserves_from_config():
    from phone_agent.v2.middleware.compact import build_compact_middleware

    config = type(
        "C",
        (),
        {
            "model_name": "test-model",
            "context_window": 1_000,
            "compact_warn_ratio": 0.75,
            "compact_trigger_ratio": 0.92,
            "compact_schema_reserve": 1234,
            "compact_output_reserve": 4321,
            "lang": "cn",
        },
    )()
    compact = build_compact_middleware(_make_session(), config)
    assert compact.schema_reserve == 1234
    assert compact.output_reserve == 4321

    # Defaults hold when the config omits the fields.
    compact_default = build_compact_middleware(
        _make_session(),
        type("C", (), {"model_name": "m", "context_window": 1_000})(),
    )
    assert compact_default.schema_reserve == 3000
    assert compact_default.output_reserve == 2000


def test_compact_reserves_read_from_env(monkeypatch):
    from phone_agent.v2.config import V2Config

    monkeypatch.setenv("PHONE_AGENT_COMPACT_SCHEMA_RESERVE", "1500")
    monkeypatch.setenv("PHONE_AGENT_COMPACT_OUTPUT_RESERVE", "2500")
    config = V2Config.from_env()
    assert config.compact_schema_reserve == 1500
    assert config.compact_output_reserve == 2500


# ---------------------------------------------------------------------------
# taskdoc listener contract
# ---------------------------------------------------------------------------
def test_taskdoc_listener_transforms_full_list_without_remove_message():
    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    old_id = f"{TASKDOC_ID_PREFIX}old"
    injector._injected_id = old_id
    messages = [
        SystemMessage(content="系统提示词", id="sys-1"),
        SystemMessage(content="[TASK_DOC]\n旧", id=old_id),
        HumanMessage(content="任务"),
    ]

    out = injector(messages, lambda payload: payload)

    assert not any(isinstance(m, RemoveMessage) for m in out)
    assert [m for m in out if getattr(m, "id", None) == old_id] == []
    blocks = [m for m in out if _count_prefix([m], "[TASK_DOC]")]
    assert len(blocks) == 1
    assert blocks[0] is out[-1]
    assert "打开设置并连上 WLAN" in blocks[0].content


def test_taskdoc_make_delta_legacy_path_keeps_remove_message():
    injector = TaskDocInjector(_make_session(task_doc=_FakeTaskDoc()))
    # First turn: fresh block, nothing to remove yet.
    first = injector.make_delta([HumanMessage(content="任务")])
    assert first is not None and len(first) == 1
    assert not any(isinstance(m, RemoveMessage) for m in first)

    second = injector.make_delta([HumanMessage(content="任务"), first[0]])
    assert second is not None
    assert isinstance(second[0], RemoveMessage)
    assert second[0].id == first[0].id
    assert isinstance(second[1], SystemMessage)


# ---------------------------------------------------------------------------
# token accounting units (BUG 3)
# ---------------------------------------------------------------------------
def test_estimate_text_tokens_is_script_aware():
    from phone_agent.v2.middleware._tokens import estimate_text_tokens

    # Pure ASCII keeps len//4.
    assert estimate_text_tokens("x" * 400) == 100
    assert estimate_text_tokens("") == 0
    # CJK ideographs count ~1 token each (was len//4: 4 -> 1).
    assert estimate_text_tokens("你好世界") == 4
    # CJK punctuation and fullwidth forms count as CJK.
    assert estimate_text_tokens("「」，！") == 4
    # Mixed: CJK chars + other//4.
    assert estimate_text_tokens("你好world!!") == 2 + 7 // 4
    assert estimate_text_tokens("ab你好cd") == 2 + 4 // 4


def test_estimate_message_tokens_counts_tool_call_args():
    from phone_agent.v2.middleware._tokens import (
        estimate_message_tokens,
        estimate_text_tokens,
    )

    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "update_task_doc",
                "args": {"items_text": "x" * 400},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    expected = estimate_text_tokens(
        json.dumps(
            {"name": "update_task_doc", "args": {"items_text": "x" * 400}},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )
    assert expected > 100  # the payload is real, not noise
    assert estimate_message_tokens(message) == expected

    # Two calls stack.
    two = AIMessage(
        content="hi",
        tool_calls=[
            {"name": "a", "args": {"k": "v"}, "id": "c1", "type": "tool_call"},
            {"name": "b", "args": {}, "id": "c2", "type": "tool_call"},
        ],
    )
    per_call = estimate_text_tokens(
        json.dumps({"name": "a", "args": {"k": "v"}}, ensure_ascii=False, sort_keys=True, default=str)
    ) + estimate_text_tokens(
        json.dumps({"name": "b", "args": {}}, ensure_ascii=False, sort_keys=True, default=str)
    )
    assert estimate_message_tokens(two) == estimate_text_tokens("hi") + per_call

    # Dict-shaped messages with OpenAI-style function calls are tolerated.
    raw = {
        "tool_calls": [
            {"function": {"name": "f", "arguments": "{}"}, "id": "c1"},
        ]
    }
    assert estimate_message_tokens(raw) > 0


def test_budget_fallback_counts_input_plus_output_without_usage():
    """No usage_metadata -> the turn costs the full request input + the output.

    Private-counter path: the accumulated total must reflect the whole
    request, not just the newest AIMessage's content.
    """

    budget = BudgetMiddleware(token_budget=1_000_000, warn_remaining=1)
    from phone_agent.v2.middleware._tokens import estimate_context_tokens

    older_ai = AIMessage(content="old", id="old-1")
    newest_ai = AIMessage(content="z" * 400, id="new-1")
    messages = [
        SystemMessage(content="s" * 400),
        HumanMessage(content="x" * 400),
        older_ai,
        HumanMessage(content="y" * 400),
        newest_ai,
    ]

    budget.after_model({"messages": messages}, runtime=None)

    expected_input = estimate_context_tokens(messages[:4])
    expected_output = estimate_context_tokens([newest_ai])
    assert budget.used_tokens == expected_input + expected_output

    # Same newest id is not double-counted.
    budget.after_model({"messages": messages}, runtime=None)
    assert budget.used_tokens == expected_input + expected_output


def test_budget_ledger_fallback_records_input_plus_output_estimate():
    from phone_agent.v2.middleware._tokens import estimate_context_tokens

    ledger = UsageLedger()
    budget = BudgetMiddleware(token_budget=1_000_000, warn_remaining=1, ledger=ledger)
    newest_ai = AIMessage(content="z" * 400, id="new-1")
    messages = [HumanMessage(content="x" * 400), newest_ai]

    budget.after_model({"messages": messages}, runtime=None)

    expected = estimate_context_tokens(messages[:1]) + estimate_context_tokens(
        [newest_ai]
    )
    assert ledger.by_role()["actor"] == expected

    # Real usage stays authoritative when present.
    ledger.reset()
    budget.reset()
    reported = AIMessage(
        content="z" * 400,
        id="new-2",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    )
    budget.after_model({"messages": [HumanMessage(content="x" * 400), reported]}, None)
    assert ledger.by_role()["actor"] == 150
