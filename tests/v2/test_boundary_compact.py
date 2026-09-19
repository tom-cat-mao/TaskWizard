"""Work items B + C: boundary-aware compact and the summary quote-hit shadow metric.

All fakes — no real device, gateway, or MLX. The economics are exercised through
the pure :func:`evaluate_boundary_fold`, the trigger through a real ``EventBus``
plus a duck-typed session, and the fold through a stubbed seam *and* a real
:class:`CompactMiddleware` so the seam cannot quietly bypass a gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from phone_agent.v2.events import TASKDOC_ITEM_COMPLETED, EventBus
from phone_agent.v2.middleware.boundary_compact import (
    BoundaryCompactListener,
    evaluate_boundary_fold,
    open_route_items,
    steps_per_completed_item,
)
from phone_agent.v2.middleware.compact import CompactMiddleware
from phone_agent.v2.middleware.trace import TraceWriter
from phone_agent.v2.taskdoc import TaskDoc, TaskItem
from phone_agent.v2.tools.taskdoc import make_update_task_doc_tool

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


@dataclass
class FakeSession:
    task_doc: Any = None
    screen_seq: int = 0
    epoch: int = 0
    event_bus: Any = None
    usage_ledger: Any = None
    resolution_trace_recorder: Any = None


@dataclass
class FakeConfig:
    model_name: str = "autoglm-phone-9b"
    context_window: int | None = None
    memory_model: str | None = None
    compact_warn_ratio: float = 0.75
    compact_trigger_ratio: float = 0.92
    lang: str = "cn"
    boundary_compact_mode: str = "shadow"
    boundary_compact_steps_per_item: int = 4
    boundary_compact_sample_guard_items: int = 2
    boundary_compact_min_horizon_steps: int = 2
    boundary_compact_min_span_tokens: int = 1500
    boundary_compact_min_net_ratio: float = 1.5
    compact_summary_tokens: int = 2000


class StubCompact:
    """Records the seam call and returns whatever the test hands it."""

    def __init__(self, result: Any, *, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[dict] = []
        self.last_result = {"status": "skipped", "reason": "protected_or_recent_context"}

    def on_pre_request(self, messages, next):  # noqa: ANN001
        # Enough surface for the compact capability's bridge registration.
        return next(messages)

    def request_semantic_fold(self, messages, *, span_hint=None):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "span_hint": span_hint})
        if self.raises:
            raise RuntimeError("summariser exploded")
        return self.result


class ScriptModel:
    def __init__(self, text: str = "摘要正文") -> None:
        self.text = text
        self.calls = 0

    def invoke(self, messages):  # noqa: ANN001
        self.calls += 1
        return AIMessage(content=self.text)


def _recorder() -> tuple[list[tuple[str, dict]], Any]:
    events: list[tuple[str, dict]] = []

    def record(event: str, **payload: Any) -> None:
        events.append((event, payload))

    return events, record


def _text(n_tokens: int) -> str:
    return "x" * (n_tokens * 4)


def _turn(index: int, tokens: int = 400) -> list[Any]:
    """One closed AI(tool_call) -> Tool(result) turn, padded to ``tokens``."""

    return [
        AIMessage(
            content="",
            id=f"a{index}",
            tool_calls=[
                {
                    "name": "tap",
                    "args": {"target_mark_id": f"ax_{index}", "intent": f"第{index}步"},
                    "id": f"c{index}",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=f"OK. 已点击第 {index} 项 " + _text(tokens),
            id=f"t{index}",
            tool_call_id=f"c{index}",
            name="tap",
        ),
    ]


def _transcript(turns: int, *, tokens: int = 2000) -> list[Any]:
    messages: list[Any] = [HumanMessage(content="打开设置并连上 WLAN", id="h0")]
    for index in range(turns):
        messages.extend(_turn(index, tokens))
    return messages


def _listener(
    *, mode: str = "shadow", compact: Any = None, session: FakeSession | None = None
) -> tuple[BoundaryCompactListener, list[tuple[str, dict]]]:
    session = session or FakeSession(
        task_doc=TaskDoc(
            goal_base="打开设置",
            items=[
                TaskItem("1", "打开设置", status="completed", evidence_note="设置页可见"),
                TaskItem("2", "连接 WLAN", status="in_progress"),
            ],
        ),
        screen_seq=12,
    )
    events, record = _recorder()
    listener = BoundaryCompactListener(
        session,
        FakeConfig(boundary_compact_mode=mode),
        compact_provider=lambda: compact,
        trace_recorder=record,
        mode=mode,
    )
    return listener, events


def _arm_boundary(listener: BoundaryCompactListener, *, at_seq: int, span_from: int = 0) -> None:
    listener.on_taskdoc_completed(
        {"item_ids": ["1"], "screen_seq": at_seq, "epoch": at_seq}
    )
    listener._last_boundary_seq = span_from  # noqa: SLF001 - pin the span under test
    listener._pending["span_steps"] = at_seq - span_from  # noqa: SLF001


# --------------------------------------------------------------------------
# B1: the boundary signal is the committed route transition
# --------------------------------------------------------------------------


def test_completion_transition_emits_boundary_event():
    bus = EventBus()
    seen: list[dict] = []
    bus.on(TASKDOC_ITEM_COMPLETED, seen.append)
    session = FakeSession(
        task_doc=TaskDoc(
            goal_base="打开设置",
            items=[TaskItem("1", "打开设置", status="in_progress")],
        ),
        screen_seq=7,
        epoch=7,
        event_bus=bus,
    )
    tool = make_update_task_doc_tool(session, "cn")
    tool.invoke(
        {
            "items": [
                {
                    "id": "1",
                    "content": "打开设置",
                    "status": "completed",
                    "evidence_note": "设置页已可见",
                }
            ]
        }
    )
    assert seen == [{"item_ids": ["1"], "screen_seq": 7, "epoch": 7}]


def test_no_boundary_event_without_a_completion_transition():
    bus = EventBus()
    seen: list[dict] = []
    bus.on(TASKDOC_ITEM_COMPLETED, seen.append)
    session = FakeSession(
        task_doc=TaskDoc(
            goal_base="打开设置", items=[TaskItem("1", "打开设置", status="pending")]
        ),
        screen_seq=3,
        event_bus=bus,
    )
    tool = make_update_task_doc_tool(session, "cn")
    tool.invoke({"items": [{"id": "1", "content": "打开设置", "status": "in_progress"}]})
    tool.invoke(
        {"items": [{"id": "1", "content": "打开设置", "status": "blocked", "reason": "无网络"}]}
    )
    assert seen == []


def test_rejected_write_emits_no_boundary_and_no_bus_needed():
    # A validation failure must not announce a boundary, and a session without a
    # bus (offline commands, minimal configs) must still accept a valid write.
    session = FakeSession(
        task_doc=TaskDoc(goal_base="打开设置"), screen_seq=1, event_bus=None
    )
    tool = make_update_task_doc_tool(session, "cn")
    out = tool.invoke(
        {
            "items": [
                {
                    "id": "1",
                    "content": "打开设置",
                    "status": "completed",
                    "evidence_note": "证据",
                }
            ]
        }
    )
    assert "未写入" in out
    assert session.task_doc.items == []


def test_boundary_listener_tracks_span_and_completed_count():
    listener, _ = _listener()
    listener.on_taskdoc_completed({"item_ids": ["1"], "screen_seq": 5, "epoch": 5})
    listener.on_taskdoc_completed({"item_ids": ["2"], "screen_seq": 9, "epoch": 9})
    assert listener.completed_items == 2
    assert listener._pending == {  # noqa: SLF001 - the span under test is the contract
        "item_ids": ["2"],
        "span_steps": 4,
        "seq": 9,
    }


def test_malformed_boundary_payload_is_ignored():
    listener, _ = _listener()
    for payload in (None, "x", {}, {"item_ids": []}, {"item_ids": ["1"], "screen_seq": "n/a"}):
        listener.on_taskdoc_completed(payload)
    assert listener._pending is None  # noqa: SLF001
    assert listener.completed_items == 0


# --------------------------------------------------------------------------
# B3: the decision is mechanical, and small samples are guarded
# --------------------------------------------------------------------------


def _evaluate(**overrides) -> Any:
    args: dict[str, Any] = {
        "messages": _transcript(12),
        "span_steps": 5,
        "completed_items": 2,
        "steps_total": 12,
        "open_items": 3,
        "summary_tokens": 2000,
        "prior_steps_per_item": 4,
        "guard_items": 2,
        "min_horizon_steps": 2,
        "min_span_tokens": 1500,
        "min_net_ratio": 1.5,
    }
    args.update(overrides)
    return evaluate_boundary_fold(**args)


def test_clearly_net_positive_span_folds():
    decision = _evaluate()
    assert decision.fold is True
    assert decision.reason == "net_positive"
    assert decision.numbers["benefit_tokens"] >= 1.5 * decision.numbers["cost_tokens"]
    assert decision.numbers["horizon_steps"] == 3 * (12 / 2)


def test_finished_plan_defers():
    decision = _evaluate(open_items=0)
    assert decision.fold is False
    assert decision.reason == "no_open_route_items"


def test_small_span_defers():
    decision = _evaluate(span_steps=1, min_span_tokens=5000)
    assert decision.fold is False
    assert decision.reason == "span_too_small"
    # The span floor is checked before the ratio, so a cheap span never claims a
    # benefit it has not computed.
    assert "benefit_tokens" not in decision.numbers


def test_thin_horizon_defers():
    decision = _evaluate(open_items=1, steps_total=2, completed_items=2, span_steps=5)
    assert decision.fold is False
    assert decision.reason == "horizon_too_short"
    assert decision.numbers["horizon_steps"] < 2


def test_marginal_economics_defer():
    # Enough horizon to matter, and a real span, but the bar is set far above the
    # measured ratio: "clearly net-positive" must not mean "marginally so".
    decision = _evaluate(span_steps=6, min_net_ratio=1000.0)
    assert decision.fold is False
    assert decision.reason == "not_net_positive"
    assert 1.5 < decision.numbers["net_ratio"] < 1000.0


def test_small_sample_uses_the_conservative_prior():
    # One lucky two-step completion must not license folding the whole run.
    assert steps_per_completed_item(
        completed_items=1, steps_total=2, prior=4, guard_items=2
    ) == 4.0
    assert steps_per_completed_item(
        completed_items=4, steps_total=40, prior=4, guard_items=2
    ) == 10.0
    decision = _evaluate(completed_items=1, steps_total=2)
    assert decision.numbers["steps_per_item"] == 4.0


def test_empty_transcript_defers():
    assert _evaluate(messages=[]).reason == "no_messages"


def test_open_route_items_counts_both_open_statuses():
    doc = TaskDoc(
        items=[
            TaskItem("1", "a", status="completed", evidence_note="x"),
            TaskItem("2", "b", status="in_progress"),
            TaskItem("3", "c", status="pending"),
            TaskItem("4", "d", status="blocked", reason="y"),
        ]
    )
    assert open_route_items(doc) == 2
    assert open_route_items(None) == 0


# --------------------------------------------------------------------------
# B1/B4: shadow logs the verdict and changes nothing; on folds through the seam
# --------------------------------------------------------------------------


def test_shadow_mode_is_a_pure_passthrough_even_when_the_fold_would_win():
    compact = StubCompact(result=["folded"])
    listener, events = _listener(mode="shadow", compact=compact)
    _arm_boundary(listener, at_seq=5, span_from=0)
    messages = _transcript(12)

    out = listener.on_pre_request(messages, next=lambda m: m)

    assert out is messages, "shadow must hand the original list downstream"
    assert compact.calls == [], "shadow must never request a fold"
    decisions = [payload for event, payload in events if event == "boundary_compact_decision"]
    assert len(decisions) == 1
    assert decisions[0]["fold"] is True
    assert decisions[0]["mode"] == "shadow"
    assert decisions[0]["reason"] == "net_positive"


def test_shadow_records_the_defer_reason_too():
    listener, events = _listener(mode="shadow", compact=StubCompact(result=["folded"]))
    # A one-observation span cannot repay a summariser pass over itself.
    _arm_boundary(listener, at_seq=1, span_from=0)
    listener.on_pre_request(_transcript(12), next=lambda m: m)
    decisions = [payload for event, payload in events if event == "boundary_compact_decision"]
    assert decisions[0]["fold"] is False
    assert decisions[0]["reason"] == "not_net_positive"


def test_on_mode_requests_the_fold_with_a_boundary_aligned_hint():
    folded = [SystemMessage(content="rebuilt", id="s0")]
    compact = StubCompact(result=folded)
    listener, events = _listener(mode="on", compact=compact)
    listener.session.screen_seq = 9
    _arm_boundary(listener, at_seq=5, span_from=0)
    messages = _transcript(12)

    out = listener.on_pre_request(messages, next=lambda m: m)

    assert out == folded, "the rebuilt transcript is what must reach the model"
    assert compact.calls[0]["span_hint"] == 4  # steps committed after the boundary
    assert listener.folds == 1
    commits = [payload for event, payload in events if event == "boundary_compact_fold"]
    assert len(commits) == 1
    assert commits[0]["committed"] is True


def test_on_mode_forwards_the_original_list_when_a_gate_refuses():
    compact = StubCompact(result=None)
    listener, events = _listener(mode="on", compact=compact)
    listener.session.screen_seq = 12
    _arm_boundary(listener, at_seq=5, span_from=0)
    messages = _transcript(12)

    out = listener.on_pre_request(messages, next=lambda m: m)

    assert out is messages
    refusal = [payload for event, payload in events if event == "boundary_compact_fold"]
    assert refusal[0]["committed"] is False
    assert refusal[0]["gate_reason"] == "protected_or_recent_context"


def test_on_mode_survives_a_raising_fold_seam():
    listener, _ = _listener(mode="on", compact=StubCompact(result=None, raises=True))
    listener.session.screen_seq = 12
    _arm_boundary(listener, at_seq=5, span_from=0)
    messages = _transcript(12)
    assert listener.on_pre_request(messages, next=lambda m: m) is messages


def test_one_decision_per_boundary_and_reset_clears_counters():
    listener, events = _listener(mode="shadow", compact=StubCompact(result=["folded"]))
    listener.session.screen_seq = 12
    _arm_boundary(listener, at_seq=5, span_from=0)
    for _ in range(3):
        listener.on_pre_request(_transcript(12), next=lambda m: m)
    assert len([1 for event, _ in events if event == "boundary_compact_decision"]) == 1
    listener.reset()
    assert listener.completed_items == 0
    assert listener.folds == 0
    assert listener._pending is None  # noqa: SLF001


def test_no_boundary_means_the_waterfall_never_touches_the_transcript():
    compact = StubCompact(result=["folded"])
    listener, events = _listener(mode="on", compact=compact)
    messages = _transcript(4)
    assert listener.on_pre_request(messages, next=lambda m: m) is messages
    assert compact.calls == []
    assert events == []


def test_boundary_waits_for_the_next_observation():
    session = FakeSession(
        task_doc=TaskDoc(items=[TaskItem("2", "b", status="in_progress")]), screen_seq=5
    )
    compact = StubCompact(result=["folded"])
    listener, events = _listener(mode="on", compact=compact, session=session)
    _arm_boundary(listener, at_seq=5, span_from=0)
    messages = _transcript(12)

    # Same screen_seq as the boundary: nothing has been observed since, so the
    # span may still grow and the listener stays armed without logging.
    assert listener.on_pre_request(messages, next=lambda m: m) is messages
    assert events == []
    assert compact.calls == []
    assert listener._pending is not None  # noqa: SLF001

    session.screen_seq = 9
    listener.on_pre_request(messages, next=lambda m: m)
    assert len(compact.calls) == 1


def _mount_context(mode: str):
    from phone_agent.v2.capabilities import (
        CapabilityAssemblyContext,
        assemble_capabilities,
        build_capability_registry,
    )

    bus = EventBus()
    config = FakeConfig(boundary_compact_mode=mode)
    session = FakeSession(event_bus=bus)
    compact = StubCompact(result=None)
    ctx = CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": session,
            "config": config,
            "compact_instance": compact,
            "compact_middleware_factory": lambda **_kw: compact,
        }
    )
    registry = build_capability_registry(config)
    assemble_capabilities(registry, ctx)
    return ctx, bus, session, compact


def test_capability_mounts_listeners_and_releases_without_residue():
    from phone_agent.v2.capabilities import (
        assemble_capabilities,
        build_capability_registry,
    )

    ctx, bus, _session, _compact = _mount_context("shadow")
    assert len(bus._listeners.get(TASKDOC_ITEM_COMPLETED, ())) == 1  # noqa: SLF001
    assert ctx.service("boundary_compact_listener") is not None

    config = FakeConfig()
    config.boundary_compact_mode = "off"
    assemble_capabilities(build_capability_registry(config), ctx)
    assert bus._listeners.get(TASKDOC_ITEM_COMPLETED, ()) == ()  # noqa: SLF001
    assert ctx.service("boundary_compact_listener") is None


def test_capability_is_inert_without_a_compact_instance():
    from phone_agent.v2.capabilities import (
        CapabilityAssemblyContext,
        assemble_capabilities,
        build_capability_registry,
    )

    bus = EventBus()
    config = FakeConfig(boundary_compact_mode="on")
    ctx = CapabilityAssemblyContext(
        {
            "event_bus": bus,
            "session": FakeSession(event_bus=bus),
            "config": config,
            "compact_middleware_factory": lambda **_kw: None,
        }
    )
    assemble_capabilities(build_capability_registry(config), ctx)
    assert bus._listeners.get(TASKDOC_ITEM_COMPLETED, ()) == ()  # noqa: SLF001
    assert ctx.service("boundary_compact_listener") is None


# --------------------------------------------------------------------------
# B4: the seam is the existing fold path, gates intact
# --------------------------------------------------------------------------


def _compact_mw(*, session, config, model, **kw) -> CompactMiddleware:
    return CompactMiddleware(session, config, model=model, **kw)


def test_seam_folds_through_the_existing_gates_and_keeps_the_pin():
    session = FakeSession(
        task_doc=TaskDoc(goal_base="打开设置", items=[TaskItem("2", "连接 WLAN", "in_progress")])
    )
    config = FakeConfig(context_window=60_000)
    pin = SystemMessage(content="[TASK_DOC]\n## 路线", id="__taskdoc__pin")
    messages = [SystemMessage(content="系统提示", id="s0"), *_transcript(10, tokens=1200), pin]
    model = ScriptModel(text="## 关键事实\nOK. 已点击第 3 项")
    mw = _compact_mw(session=session, config=config, model=model, work_target=0)

    rebuilt = mw.request_semantic_fold(messages, span_hint=3)

    assert isinstance(rebuilt, list) and rebuilt
    assert not any(isinstance(m, RemoveMessage) for m in rebuilt), (
        "the seam returns a plain full list: no RemoveMessage travels downstream"
    )
    assert rebuilt[0] is messages[0], "the leading system prompt stays verbatim"
    assert rebuilt[-1] is pin, "the pinned TaskDoc survives the fold"
    summaries = [
        m for m in rebuilt if isinstance(m, SystemMessage) and m.content.startswith("[COMPACT_SUMMARY]")
    ]
    assert len(summaries) == 1
    # Cut alignment: everything newer than the boundary stays verbatim, so the
    # last two turns' receipts are still in the tail.
    tail_text = " ".join(str(getattr(m, "content", "")) for m in rebuilt)
    assert "已点击第 9 项" in tail_text
    assert mw.last_result["status"] == "completed"
    assert mw.last_result["trigger"] == "boundary"
    assert mw.generation == 1


def test_seam_never_folds_a_protected_latest_observation():
    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=60_000)
    latest = [
        AIMessage(content="", id="a99", tool_calls=[{"name": "read_screen", "args": {}, "id": "c99", "type": "tool_call"}]),
        ToolMessage(
            content=[
                {"type": "text", "text": "marks (3): ax_1 设置"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
            id="t99",
            tool_call_id="c99",
            name="read_screen",
        ),
    ]
    messages = [SystemMessage(content="系统提示", id="s0"), *_transcript(6, tokens=1200), *latest]
    mw = _compact_mw(
        session=session, config=config, model=ScriptModel(), work_target=0
    )

    rebuilt = mw.request_semantic_fold(messages, span_hint=1)

    assert rebuilt is not None
    assert latest[0] in rebuilt and latest[1] in rebuilt, (
        "the newest observation is protected: a boundary fold may not consume it"
    )


def test_seam_refuses_when_nothing_may_be_folded():
    session = FakeSession(task_doc=None)
    config = FakeConfig(context_window=60_000)
    mw = _compact_mw(session=session, config=config, model=ScriptModel(), work_target=0)
    messages = [HumanMessage(content="task", id="h0"), *_turn(0)]
    assert mw.request_semantic_fold(messages, span_hint=1) is None
    assert mw.last_result["reason"] in {"boundary_nothing_to_fold", "protected_or_recent_context"}


def test_seam_respects_the_run_token_budget():
    session = FakeSession(task_doc=None)
    session.usage_ledger = SimpleNamespace(total=5_000_000)
    config = FakeConfig(context_window=60_000)
    config.token_budget = 1_000_000
    model = ScriptModel()
    mw = _compact_mw(session=session, config=config, model=model, work_target=0)
    assert mw.request_semantic_fold(_transcript(10, tokens=1200), span_hint=2) is None
    assert mw.last_result["reason"] == "token_budget_exhausted"
    assert model.calls == 0


def test_capacity_trigger_path_is_unchanged_by_the_seam():
    # The T2 path still reports the capacity trigger and its own reasons.
    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=20_000)
    mw = _compact_mw(session=session, config=config, model=ScriptModel(), keep_ratio=0.2)
    update = mw.before_model(
        {"messages": [SystemMessage(content="sys", id="s0"), *_transcript(8, tokens=3000)]},
        runtime=None,
    )
    assert update is not None
    assert mw.last_result["trigger"] == "capacity"
    assert mw.last_result["status"] == "completed"


# --------------------------------------------------------------------------
# C: the summary quote-hit shadow metric
# --------------------------------------------------------------------------


def _fold_with_recorder(summary_text: str):
    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=60_000)
    events, record = _recorder()
    mw = CompactMiddleware(
        session,
        config,
        model=ScriptModel(text=summary_text),
        work_target=0,
        trace_recorder=record,
    )
    messages = [SystemMessage(content="系统提示", id="s0"), *_transcript(10, tokens=1200)]
    rebuilt = mw.request_semantic_fold(messages, span_hint=2)
    checks = [payload for event, payload in events if event == "compact_summary_quote_check"]
    return rebuilt, checks, mw


def test_quote_check_reports_a_verbatim_summary_as_full_hit():
    verbatim = (
        "## 关键事实\n"
        "- OK. 已点击第 3 项\n"
        "- OK. 已点击第 4 项\n"
        "- OK. 已点击第 5 项\n"
    )
    rebuilt, checks, _mw = _fold_with_recorder(verbatim)
    assert rebuilt is not None
    assert len(checks) == 1
    metric = checks[0]
    assert metric["quotes_total"] == 3
    assert metric["quotes_hit"] == 3
    assert metric["quotes_missed"] == 0
    assert metric["hit_rate"] == 1.0
    assert metric["folded_messages"] > 0
    assert metric["miss_samples"] == []


def test_quote_check_counts_invented_lines_as_misses():
    invented = "## 关键事实\n这一行是摘要自己拼出来的说法，历史里没有逐字出现过。"
    rebuilt, checks, _mw = _fold_with_recorder(invented)
    assert rebuilt is not None
    metric = checks[0]
    assert metric["quotes_total"] == 1
    assert metric["quotes_hit"] == 0
    assert metric["hit_rate"] == 0.0
    assert len(metric["miss_samples"]) == 1


def test_quote_check_skips_structure_and_stays_bounded():
    mixed = "\n".join(
        [
            "## 目标",
            "",
            "- 短",
            f"- {'长行逐字内容 ' * 40}",
            *[f"- 逐行重复的观测记录第 {i} 项内容说明" for i in range(60)],
        ]
    )
    _rebuilt, checks, _mw = _fold_with_recorder(mixed)
    metric = checks[0]
    # Headers and a one-character bullet are not quotes; the probe list is capped.
    assert metric["quotes_total"] > 1
    assert metric["quotes_total"] <= 40
    assert all(len(sample) <= 64 for sample in metric["miss_samples"])
    assert len(metric["miss_samples"]) <= 3


def test_quote_check_is_absent_when_the_fold_aborts():
    session = FakeSession(task_doc=None)
    session.usage_ledger = SimpleNamespace(total=9_000_000)
    config = FakeConfig(context_window=60_000)
    config.token_budget = 1_000_000
    events, record = _recorder()
    mw = CompactMiddleware(
        session, config, model=ScriptModel(text="## 关键事实\nOK. 已点击第 3 项"), work_target=0, trace_recorder=record
    )
    assert mw.request_semantic_fold(_transcript(10, tokens=1200), span_hint=2) is None
    assert [event for event, _ in events if event == "compact_summary_quote_check"] == []


def test_quote_check_passes_the_p0_trace_redaction_boundary(tmp_path):
    # The samples that reach the trace file are clipped and sensitive-substring
    # redacted, exactly like every other trace payload.
    writer = TraceWriter("quote-redaction", trace_dir=str(tmp_path), enabled=True)
    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=60_000)
    summary = "## 关键事实\n" + "- " + ("很长的逐字外推内容 " * 30) + "13800138000"
    mw = CompactMiddleware(
        session,
        config,
        model=ScriptModel(text=summary),
        work_target=0,
        trace_recorder=writer.record_event,
    )
    assert mw.request_semantic_fold(_transcript(10, tokens=1200), span_hint=2) is not None
    rows = [
        json.loads(line)
        for line in Path(writer.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    check = next(row for row in rows if row["event"] == "compact_summary_quote_check")
    assert check["quotes_total"] == 1
    assert len(check["miss_samples"][0]) <= 65
    assert "13800138000" not in json.dumps(check, ensure_ascii=False)


def test_quote_check_records_a_fold_from_the_capacity_trigger_too():
    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=20_000)
    events, record = _recorder()
    mw = CompactMiddleware(
        session,
        config,
        model=ScriptModel(text="## 关键事实\nOK. 已点击第 3 项"),
        keep_ratio=0.2,
        work_target=0,
        trace_recorder=record,
    )
    update = mw.before_model(
        {"messages": [SystemMessage(content="sys", id="s0"), *_transcript(8, tokens=3000)]},
        runtime=None,
    )
    assert update is not None
    checks = [payload for event, payload in events if event == "compact_summary_quote_check"]
    assert checks and checks[0]["quotes_hit"] == 1


def test_metric_never_blocks_a_fold_when_the_recorder_explodes():
    def boom(_event, **_payload):
        raise RuntimeError("sink down")

    session = FakeSession(task_doc=TaskDoc(goal_base="打开设置"))
    config = FakeConfig(context_window=60_000)
    mw = CompactMiddleware(
        session,
        config,
        model=ScriptModel(text="## 关键事实\nOK. 已点击第 3 项"),
        work_target=0,
        trace_recorder=boom,
    )
    assert mw.request_semantic_fold(_transcript(10, tokens=1200), span_hint=2) is not None
    assert mw.generation == 1
