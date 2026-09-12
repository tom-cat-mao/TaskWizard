"""Tests for the two-threshold auto-compact middleware (A4 §3).

All fakes — no real device, MLX, or network. A tiny scripted summariser stands in
for the memory/main model so the T2 fold path is exercised deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from phone_agent.v2.events import EventBus
from phone_agent.v2.middleware.compact import (
    CompactMiddleware,
    build_compact_middleware,
    infer_context_window,
)
from phone_agent.v2.middleware.taskdoc import TaskDocInjector


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
@dataclass
class FakeTaskItem:
    id: str
    content: str
    status: str = "pending"
    reason: str | None = None
    evidence_note: str | None = None


@dataclass
class FakeTaskDoc:
    goal_base: str = "打开设置并连上 WLAN"
    amendments: list[str] = field(default_factory=list)
    items: list[FakeTaskItem] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)


@dataclass
class FakeSession:
    task_doc: Any = None


@dataclass
class FakeConfig:
    model_name: str = "autoglm-phone-9b"
    context_window: int | None = None
    memory_model: str | None = None
    compact_warn_ratio: float = 0.75
    compact_trigger_ratio: float = 0.92
    lang: str = "cn"


class ScriptModel:
    """A minimal chat model double: returns a canned summary, counts calls."""

    def __init__(self, text: str = "## 目标\n连 WLAN\n## 下一步\n继续", fail_times: int = 0) -> None:
        self.text = text
        self.fail_times = fail_times
        self.calls = 0
        self.last_messages: list[Any] | None = None
        self.requests: list[list[Any]] = []

    def invoke(self, messages):  # noqa: ANN001
        self.calls += 1
        self.last_messages = messages
        self.requests.append(messages)
        if self.calls <= self.fail_times:
            raise RuntimeError("input too long")
        return AIMessage(content=self.text)


def _mw(session, config, model, **kw) -> CompactMiddleware:
    return CompactMiddleware(session, config, model=model, **kw)


def _big_text(n_tokens: int) -> str:
    # len//4 tokens -> repeat 4 chars per desired token.
    return "x" * (n_tokens * 4)


def _convo(n_pairs: int, tokens_each: int = 2000) -> list[Any]:
    """Build n AI(tool_call)->Tool(result) turn pairs after a Human task."""

    msgs: list[Any] = [HumanMessage(content="task", id="h0")]
    for i in range(n_pairs):
        msgs.append(
            AIMessage(
                content="",
                id=f"a{i}",
                tool_calls=[{"name": "tap", "args": {"target_mark_id": f"ax_{i}"}, "id": f"c{i}", "type": "tool_call"}],
            )
        )
        msgs.append(
            ToolMessage(content=_big_text(tokens_each), id=f"t{i}", tool_call_id=f"c{i}", name="tap")
        )
    return msgs


# --------------------------------------------------------------------------
# window inference
# --------------------------------------------------------------------------
def test_infer_window_default_and_hints():
    assert infer_context_window("autoglm-phone-9b", None) == 256_000
    assert infer_context_window("some-128k-model", None) == 128_000
    assert infer_context_window("x", 42) == 42
    assert infer_context_window(None, None) == 256_000


# --------------------------------------------------------------------------
# T1 warn
# --------------------------------------------------------------------------
def test_t1_warn_fires_once_and_no_context_change():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=10_000)
    mw = _mw(session, config, ScriptModel())  # warn at 7500, trigger at 9200
    # ~8000 tokens across a few messages: over warn, under trigger.
    msgs = [HumanMessage(content=_big_text(8000), id="h0")]
    result = mw.before_model({"messages": msgs}, runtime=None)
    assert result is not None
    assert len(result["messages"]) == 1
    assert result["messages"][0].content.startswith("[COMPACT_WARN]")
    # One-shot: a second turn under trigger does not warn again.
    assert mw.before_model({"messages": msgs}, runtime=None) is None


def test_below_warn_is_noop():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=100_000)
    mw = _mw(session, config, ScriptModel())
    msgs = [HumanMessage(content=_big_text(1000), id="h0")]
    assert mw.before_model({"messages": msgs}, runtime=None) is None


# --------------------------------------------------------------------------
# T2 forced compaction
# --------------------------------------------------------------------------
def test_t2_fold_rebuilds_with_summary_and_pinned():
    session = FakeSession(
        task_doc=FakeTaskDoc(items=[FakeTaskItem("1", "打开设置", status="in_progress")])
    )
    config = FakeConfig(context_window=20_000)
    model = ScriptModel()
    mw = _mw(session, config, model, keep_ratio=0.2)

    head = SystemMessage(content="系统提示", id="s0")
    taskdoc = SystemMessage(content="[TASK_DOC]\n## 目标", id="__taskdoc__abc")
    convo = _convo(8, tokens_each=3000)  # ~24k tokens -> over trigger (18400)
    msgs = [head, *convo, taskdoc]

    result = mw.before_model({"messages": msgs}, runtime=None)
    assert result is not None
    out = result["messages"]
    # Rebuild uses REMOVE_ALL_MESSAGES then the new list.
    assert isinstance(out[0], RemoveMessage)
    assert out[0].id == REMOVE_ALL_MESSAGES
    rebuilt = out[1:]
    # head preserved first.
    assert rebuilt[0] is head
    # summary block present and marked.
    summaries = [m for m in rebuilt if isinstance(m, SystemMessage) and m.content.startswith("[COMPACT_SUMMARY]")]
    assert len(summaries) == 1
    assert summaries[0].id.startswith("__compact__")
    # fresh-observation hint present.
    assert any(
        isinstance(m, SystemMessage) and m.content.startswith("[COMPACT_DONE]") for m in rebuilt
    )
    # pinned TaskDoc preserved (kept verbatim, at the tail).
    assert taskdoc is rebuilt[-1]
    # The source also has to fit this 20k summary model: two complete-group
    # chunks and one merge, without deleting the oldest source group.
    assert model.calls == 3


def test_t2_appends_memory_capability_state_without_sending_it_to_llm():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(text="LLM FACTS ONLY")

    class FakeRegistry:
        def status(self):
            return [
                {"cap_id": "recall", "state": "shadow"},
                {"cap_id": "compact", "state": "active"},
            ]

    registry = FakeRegistry()

    def memory_state():
        return {
            "capabilities": {
                row["cap_id"]: row["state"] for row in registry.status()
            },
            "memory_generation": {"source": "kb.json.version", "value": 7},
            "shadow_candidate_ids": ["episode-2", "episode-9"],
        }

    mw = CompactMiddleware(
        session,
        config,
        model=model,
        memory_state_provider=memory_state,
        keep_ratio=0.2,
    )
    result = mw.before_model(
        {"messages": [SystemMessage(content="sys"), *_convo(8, 3000)]},
        runtime=None,
    )

    summary = next(
        message.content
        for message in result["messages"]
        if isinstance(message, SystemMessage)
        and message.content.startswith("[COMPACT_SUMMARY]")
    )
    assert summary == (
        "[COMPACT_SUMMARY]\nLLM FACTS ONLY\n\n## 记忆与能力\n"
        '- 能力状态：{"compact":"active","recall":"shadow"}\n'
        '- 记忆代际：{"source":"kb.json.version","value":7}\n'
        '- Shadow recall 候选 ID：["episode-2","episode-9"]'
    )
    prompt = "\n".join(str(message.content) for message in model.last_messages)
    assert "## 记忆与能力" not in prompt
    assert "episode-2" not in prompt


def test_t2_omits_memory_section_when_agent_snapshot_unavailable():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(text="plain summary")
    mw = CompactMiddleware(
        session,
        config,
        model=model,
        memory_state_provider=lambda: None,
        keep_ratio=0.2,
    )

    result = mw.before_model(
        {"messages": [SystemMessage(content="sys"), *_convo(8, 3000)]},
        runtime=None,
    )
    summary = next(
        message.content
        for message in result["messages"]
        if isinstance(message, SystemMessage)
        and message.content.startswith("[COMPACT_SUMMARY]")
    )
    assert summary == "[COMPACT_SUMMARY]\nplain summary"


def test_t2_tail_never_starts_on_toolmessage():
    # The recent verbatim tail must not begin with a tool_result whose tool_use
    # was folded into the summary (would dangle at the gateway).
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    mw = _mw(session, config, ScriptModel(), keep_ratio=0.2)
    convo = _convo(8, tokens_each=3000)
    msgs = [SystemMessage(content="sys", id="s0"), *convo]

    result = mw.before_model({"messages": msgs}, runtime=None)
    rebuilt = result["messages"][1:]
    # Find the tail (messages after the summary + before the fresh hint).
    non_meta = [
        m
        for m in rebuilt
        if not (isinstance(m, SystemMessage) and (m.content.startswith("[COMPACT_") or m.content == "sys"))
    ]
    assert non_meta, "expected a verbatim tail"
    assert not isinstance(non_meta[0], ToolMessage)


def test_t2_iterative_feeds_prior_summary_and_supersedes():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(text="new summary body")
    mw = _mw(session, config, model, keep_ratio=0.2)

    prior = SystemMessage(content="[COMPACT_SUMMARY]\nOLD BODY", id="__compact__old")
    convo = _convo(8, tokens_each=3000)
    msgs = [SystemMessage(content="sys", id="s0"), prior, *convo]

    result = mw.before_model({"messages": msgs}, runtime=None)
    rebuilt = result["messages"][1:]
    summaries = [m for m in rebuilt if isinstance(m, SystemMessage) and m.content.startswith("[COMPACT_SUMMARY]")]
    # Exactly one summary (the old one is superseded, not duplicated).
    assert len(summaries) == 1
    assert "new summary body" in summaries[0].content
    assert summaries[0].id != "__compact__old"
    # The prior summary body was fed into the summariser input.
    joined = "\n".join(
        b if isinstance(b, str) else getattr(b, "content", "")
        for b in (model.last_messages or [])
    )
    assert "OLD BODY" in joined


def test_t2_iterative_does_not_feed_deterministic_memory_section_to_llm():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(text="new summary")
    mw = CompactMiddleware(
        session,
        config,
        model=model,
        memory_state_provider=lambda: {
            "capabilities": {"compact": "active"},
            "memory_generation": None,
            "shadow_candidate_ids": ["new-candidate"],
        },
        keep_ratio=0.2,
    )
    prior = SystemMessage(
        content=(
            "[COMPACT_SUMMARY]\nOLD BODY\n\n## 记忆与能力\n"
            "- 能力状态：{}\n- 记忆代际：null\n"
            '- Shadow recall 候选 ID：["old-candidate"]'
        ),
        id="__compact__old",
    )

    mw.before_model(
        {
            "messages": [
                SystemMessage(content="sys", id="s0"),
                prior,
                *_convo(8, tokens_each=3000),
            ]
        },
        runtime=None,
    )

    prompt = "\n".join(str(message.content) for message in model.last_messages)
    assert "OLD BODY" in prompt
    assert "## 记忆与能力" not in prompt
    assert "old-candidate" not in prompt


def test_t2_retry_preserves_source_then_success():
    # A retry keeps exactly the same source, including the oldest group.
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(text="ok summary", fail_times=1)
    mw = _mw(session, config, model, keep_ratio=0.2)
    convo = _convo(8, tokens_each=3000)
    msgs = [SystemMessage(content="sys", id="s0"), *convo]

    result = mw.before_model({"messages": msgs}, runtime=None)
    assert result is not None
    assert model.calls == 4  # two chunks, one merge, and one identical retry
    assert model.requests[0] == model.requests[1]
    summaries = [
        m for m in result["messages"] if isinstance(m, SystemMessage) and m.content.startswith("[COMPACT_SUMMARY]")
    ]
    assert summaries and "ok summary" in summaries[0].content


def test_t2_fail_open_when_summariser_always_fails():
    # Summariser fails every retry -> fail-open: no fold, fall through to T1 warn.
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(fail_times=99)
    mw = _mw(session, config, model, keep_ratio=0.2)
    convo = _convo(8, tokens_each=3000)
    msgs = [SystemMessage(content="sys", id="s0"), *convo]

    result = mw.before_model({"messages": msgs}, runtime=None)
    # No REMOVE_ALL rebuild; instead the T1 warn (context is also over warn).
    assert result is not None
    assert not any(isinstance(m, RemoveMessage) for m in result["messages"])
    assert result["messages"][0].content.startswith("[COMPACT_WARN]")
    assert model.calls == 3  # exhausted max_ptl_retries


def test_t2_summariser_failure_does_not_call_memory_state_provider():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    model = ScriptModel(fail_times=99)
    provider_calls = 0

    def memory_state():
        nonlocal provider_calls
        provider_calls += 1
        return {"capabilities": {"compact": "active"}}

    mw = CompactMiddleware(
        session,
        config,
        model=model,
        memory_state_provider=memory_state,
        keep_ratio=0.2,
    )
    result = mw.before_model(
        {"messages": [SystemMessage(content="sys"), *_convo(8, 3000)]},
        runtime=None,
    )

    assert result["messages"][0].content.startswith("[COMPACT_WARN]")
    assert provider_calls == 0


def test_t2_skips_fold_when_too_few_ancient_messages():
    # Over trigger but almost everything is in the keep-tail -> nothing to fold.
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=10_000)
    model = ScriptModel()
    mw = _mw(session, config, model, keep_ratio=0.9, min_fold_messages=4)
    # One giant message over trigger, but only 1 message -> can't fold 4.
    msgs = [HumanMessage(content=_big_text(9500), id="h0")]
    result = mw.before_model({"messages": msgs}, runtime=None)
    # No fold; T1 warn fires instead (and summariser never called).
    assert model.calls == 0
    assert result["messages"][0].content.startswith("[COMPACT_WARN]")


def test_t2_no_model_available_fail_open():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=20_000)
    mw = _mw(session, config, model=None, keep_ratio=0.2)  # no summariser
    convo = _convo(8, tokens_each=3000)
    msgs = [SystemMessage(content="sys", id="s0"), *convo]
    result = mw.before_model({"messages": msgs}, runtime=None)
    # Fail-open: no rebuild; T1 warn only.
    assert not any(isinstance(m, RemoveMessage) for m in result["messages"])


# --------------------------------------------------------------------------
# reset + builder
# --------------------------------------------------------------------------
def test_reset_rearms_warn():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=10_000)
    mw = _mw(session, config, ScriptModel())
    msgs = [HumanMessage(content=_big_text(8000), id="h0")]
    assert mw.before_model({"messages": msgs}, runtime=None) is not None
    assert mw.before_model({"messages": msgs}, runtime=None) is None
    mw.reset()
    assert mw.before_model({"messages": msgs}, runtime=None) is not None


def test_builder_reads_config():
    session = FakeSession(task_doc=FakeTaskDoc())
    config = FakeConfig(context_window=50_000, compact_warn_ratio=0.7, compact_trigger_ratio=0.9)
    mw = build_compact_middleware(session, config, model=ScriptModel())
    assert mw.window == 50_000
    assert mw.warn_ratio == 0.7
    assert mw.trigger_ratio == 0.9


# --------------------------------------------------------------------------
# D6: compact must preserve the pinned TaskDoc block by id prefix; the
# model/pre_request listener refreshes it after the fold.
# --------------------------------------------------------------------------


@dataclass
class _PinnedDoc:
    goal_base: str = ""
    items: list = field(default_factory=list)

    def render(self, lang: str = "cn") -> str:  # noqa: ARG002
        if not (self.goal_base or self.items):
            return ""
        lines = ["## 目标", f"base: {self.goal_base}"]
        if self.items:
            lines.append("## 路线")
            for it in self.items:
                lines.append(f"- [{it.status}] {it.id}: {it.content}")
        return "\n".join(lines)


def test_t2_fold_preserves_pinned_taskdoc_and_listener_refreshes_it():
    from dataclasses import replace

    session = FakeSession(task_doc=_PinnedDoc(goal_base="打开设置"))
    injector = TaskDocInjector(session, lang="cn")
    bus = EventBus()
    bus.on("model/pre_request", injector)

    head = SystemMessage(content="系统提示", id="s0")
    convo = _convo(8, tokens_each=3000)
    initial = [head, *convo]

    # First model call: listener pins v1.
    after_first = bus.waterfall("model/pre_request", initial, terminal=lambda x: x)
    pinned_v1 = [
        m
        for m in after_first
        if (getattr(m, "id") or "").startswith("__taskdoc__")
    ]
    assert len(pinned_v1) == 1
    v1_id = pinned_v1[0].id
    assert "打开设置" in pinned_v1[0].content

    # A tool turn passes; the board is updated.
    ai = AIMessage(
        content="",
        id="a1",
        tool_calls=[
            {
                "name": "tap",
                "args": {"target_mark_id": "ax_1"},
                "id": "call1",
                "type": "tool_call",
            }
        ],
    )
    tool = ToolMessage(content="OK. 已点击", id="t1", tool_call_id="call1", name="tap")
    session.task_doc = replace(
        session.task_doc,
        items=[
            FakeTaskItem("1", "打开设置", "completed"),
            FakeTaskItem("2", "连接 WLAN", "in_progress"),
        ],
    )

    # Compact folds before the listener runs.  It must keep the pinned v1 block
    # (identified by id prefix) so the listener can remove it and append v2.
    compact = CompactMiddleware(
        session,
        FakeConfig(context_window=20_000),
        model=ScriptModel(),
        keep_ratio=0.2,
    )
    folded = compact.before_model({"messages": after_first + [ai, tool]}, runtime=None)
    assert folded is not None
    rebuilt = folded["messages"][1:]  # skip REMOVE_ALL_MESSAGES
    assert any(getattr(m, "id", None) == v1_id for m in rebuilt)

    # Second model call: listener refreshes the pinned block.
    after_second = bus.waterfall("model/pre_request", rebuilt, terminal=lambda x: x)
    # Full-list contract (S1): the listener drops the stale pinned copy by id
    # and appends exactly one fresh block — no RemoveMessage travels in the
    # waterfall payload (the bridge alone mints the LangGraph removal).
    assert not any(isinstance(m, RemoveMessage) for m in after_second)
    assert all((getattr(m, "id") or "") != v1_id for m in after_second)
    fresh = next(
        m
        for m in after_second
        if isinstance(m, SystemMessage)
        and (getattr(m, "id") or "").startswith("__taskdoc__")
    )
    assert "连接 WLAN" in fresh.content
    assert (
        sum(
            1
            for m in after_second
            if isinstance(m, SystemMessage)
            and (getattr(m, "id") or "").startswith("__taskdoc__")
        )
        == 1
    )
