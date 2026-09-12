"""Synthetic context policy/admission tests; no SDK network or device calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage

from phone_agent.v2.config import V2Config
from phone_agent.v2.middleware._tokens import estimate_context_tokens, estimate_message_tokens
from phone_agent.v2.middleware.compact import CompactMiddleware, build_compact_middleware
from phone_agent.v2.middleware.context_admission import check_context_admission
from phone_agent.v2.middleware.images import ContextPrunerService, _message_has_image
from phone_agent.v2.providers.context import ModelContextProfile, ModelInputEstimate, bind_context_support
from phone_agent.v2.usage import UsageLedger


class Model:
    def __init__(self, text="history preserved", on_call=None):
        self.text = text
        self.requests = []
        self.on_call = on_call

    def invoke(self, messages, **kwargs):
        self.requests.append(messages)
        if self.on_call:
            return self.on_call(messages)
        return AIMessage(content=self.text)


class Support:
    def __init__(self, window=100_000, protected=()):
        self.window = window
        self.protected = protected

    def profile(self, model, tools=()):
        return ModelContextProfile(context_window=self.window, max_output_tokens=500, source="synthetic")

    def estimate(self, model, messages, tools=()):
        return ModelInputEstimate(
            estimate_context_tokens(messages) + len(tools) * 111,
            includes_tools=True,
            complete=True,
            source="synthetic",
        )

    def protected_message_ids(self, model, messages):
        return self.protected


def history(pairs=12, size=3000, images=False):
    messages = [SystemMessage(content="rules"), HumanMessage(content="original goal", id="goal")]
    for i in range(pairs):
        messages.append(AIMessage(content="", id=f"a{i}", tool_calls=[{
            "name": "tap", "args": {"target_mark_id": f"ax_{i}", "intent": f"step {i}"},
            "id": f"c{i}", "type": "tool_call",
        }]))
        content = "x" * (size * 4)
        if images:
            content = [
                {"type": "text", "text": content},
                {"type": "text", "text": f"[OBS] app=test screen#{i}\nmarks (1): ax_{i}@e{i}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,SYNTHETIC{i}"}, "screen_seq": i},
            ]
        messages.append(ToolMessage(content=content, tool_call_id=f"c{i}", id=f"t{i}", name="tap"))
    return messages


def middleware(model=None, **kwargs):
    model = model or Model()
    bind_context_support(model, Support())
    config = SimpleNamespace(model_name="synthetic", context_window=None, memory_model=None, token_budget=1_000_000)
    session = SimpleNamespace(task_doc=None, run_goal="original goal", usage_ledger=UsageLedger())
    return CompactMiddleware(
        session, config, model=model, schema_reserve=0, output_reserve=500,
        work_target=32_000, **kwargs,
    )


def folded(update):
    return bool(update and any(isinstance(m, RemoveMessage) for m in update["messages"]))


def test_production_soft_target_folds_below_model_capacity_and_zero_disables():
    model = Model()
    bind_context_support(model, Support())
    config = V2Config(base_url="https://synthetic.invalid/v1", model_name="synthetic", context_window=100_000)
    session = SimpleNamespace(task_doc=None, run_goal="original goal")
    compact = build_compact_middleware(session, config, model=model, tools_provider=lambda: ["tool"])
    update = compact.before_model({"messages": history()}, None)
    assert folded(update)
    assert compact.last_result["after_tokens"] <= 32_000 * config.compact_target_ratio
    assert compact.window == 100_000
    config.context_work_target = 0
    legacy = build_compact_middleware(session, config, model=Model())
    assert legacy.before_model({"messages": history()}, None) is None


def test_oversized_summary_rolls_back_after_micro_and_keeps_exact_k_images():
    model = Model(text="x" * 160_000)
    compact = middleware(model, pruner=ContextPrunerService(2, 2))
    messages = history(images=True)
    out = compact.on_pre_request(messages, lambda value: value)
    assert not any(isinstance(m, RemoveMessage) for m in out)
    assert sum(_message_has_image(m) for m in out) == 2
    assert _message_has_image(messages[-1]) and _message_has_image(messages[-3])
    assert compact.last_result["status"] == "skipped"
    assert compact.last_result["reason"] == "summary_output_exceeds_target"
    assert all(not str(m.content).startswith("[COMPACT_SUMMARY]") for m in out)


def test_postcount_uses_provider_estimate_and_rejects_growth():
    class InflatedSummary(Support):
        def estimate(self, model, messages, tools=()):
            tokens = estimate_context_tokens(messages)
            if any(str(m.content).startswith("[COMPACT_SUMMARY]") for m in messages):
                tokens += 60_000
            return ModelInputEstimate(tokens, includes_tools=True, complete=True)

    compact = middleware()
    bind_context_support(compact._main_model, InflatedSummary())
    assert not folded(compact.before_model({"messages": history()}, None))
    assert compact.last_result["reason"] == "summary_not_useful_or_oversize"
    assert compact.last_result["after_tokens"] > compact.last_result["before_tokens"]


def test_newest_large_multicall_group_is_never_split_or_rewritten():
    compact = middleware()
    messages = history(pairs=16, size=1800)
    html = "<html>" + "a" * 28_000 + "</html>"
    newest = AIMessage(content="", tool_calls=[
        {"name": "write_document", "args": {"html": html}, "id": "write", "type": "tool_call"},
        {"name": "read_screen", "args": {}, "id": "read", "type": "tool_call"},
    ])
    results = [ToolMessage(content="saved", tool_call_id="write"), ToolMessage(content="skipped", tool_call_id="read", status="error")]
    messages.extend([newest, *results])
    update = compact.before_model({"messages": messages}, None)
    assert folded(update)
    assert newest in update["messages"]
    assert all(result in update["messages"] for result in results)
    assert newest.tool_calls[0]["args"]["html"] == html


def test_older_error_group_enters_summary_source_with_complete_failure_facts():
    compact = middleware()
    messages = history()
    failure = "error: device command was dispatched; outcome unknown; user text might have been entered"
    messages[3].content = failure
    messages[3].status = "error"
    assert folded(compact.before_model({"messages": messages}, None))
    source = "\n".join(str(m.content) for req in compact._main_model.requests for m in req)
    assert failure in source
    assert "'ax_0'" in source  # the failing call and its result travel together
    assert "结果未知或可能已经执行" in source


def test_newest_group_too_large_for_soft_target_stays_intact():
    compact = middleware()
    messages = history()
    messages[-2].tool_calls[0]["args"]["html"] = "body" * 24_000
    newest = messages[-2:]
    update = compact.before_model({"messages": messages}, None)
    assert not folded(update)
    assert messages[-2:] == newest
    assert compact._main_model.requests == []


def test_current_taskdoc_preview_is_counted_instead_of_small_stale_pin():
    compact = middleware()
    compact.session.task_doc = SimpleNamespace(render=lambda lang: "current goal " + "g" * 140_000)
    messages = history() + [SystemMessage(content="[TASK_DOC]\nold", id="__taskdoc__old")]
    assert not folded(compact.before_model({"messages": messages}, None))
    assert compact._main_model.requests == []
    assert compact.last_result["reason"] == "protected_context_exceeds_work_target"


@pytest.mark.parametrize("kind", ["opaque", "pending", "adapter"])
def test_protected_dependency_group_is_not_summarized(kind):
    compact = middleware()
    messages = history()
    if kind == "opaque":
        messages[2].content = [{"type": "reasoning", "encrypted_content": "native-state"}]
    elif kind == "pending":
        messages.pop(3)  # no receipt for c0
    else:
        bind_context_support(compact._main_model, Support(protected={"a0"}))
    assert not folded(compact.before_model({"messages": messages}, None))
    assert compact._main_model.requests == []
    assert compact.last_result["reason"] == "protected_or_recent_context"


def test_protected_goal_soft_overflow_does_not_cut_goal_or_pay_summary():
    compact = middleware()
    messages = history()
    messages[1].content = "g" * 140_000
    original_goal = messages[1].content
    assert not folded(compact.before_model({"messages": messages}, None))
    assert messages[1].content == original_goal
    assert compact._main_model.requests == []
    assert compact.last_result["reason"] == "protected_context_exceeds_work_target"


def test_exhausted_budget_never_pays_summary_but_still_prunes():
    compact = middleware(pruner=ContextPrunerService(2, 2))
    compact.config.token_budget = 100
    compact.session.usage_ledger.record("actor", estimate_tokens=100)
    messages = history(images=True)
    out = compact.on_pre_request(messages, lambda value: value)
    assert compact._main_model.requests == []
    assert sum(_message_has_image(m) for m in out) == 2
    assert compact.last_result["reason"] == "token_budget_exhausted"


def test_smaller_summary_model_chunks_complete_sources_then_merges():
    compact = middleware()
    summary = Model()
    bind_context_support(summary, Support(window=8000))
    compact._memory_model = summary
    compact._memory_model_built = True
    update = compact.before_model({"messages": history()}, None)
    assert folded(update)
    assert 2 < len(summary.requests) <= 8
    assert all(estimate_context_tokens(request) + 500 <= 8000 for request in summary.requests)
    # Every folded call appears in one source chunk. Tail calls stay verbatim.
    source_text = "\n".join(str(m.content) for req in summary.requests[:-1] for m in req)
    remaining_calls = {call["id"] for m in update["messages"] if isinstance(m, AIMessage) for call in m.tool_calls}
    for i in range(12):
        assert f"c{i}" in remaining_calls or f"'ax_{i}'" in source_text


def test_budget_exhausted_mid_chunks_rolls_back_without_partial_summary():
    compact = middleware()
    compact.config.token_budget = 100
    summary = Model(on_call=lambda messages: AIMessage(
        content="chunk", usage_metadata={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
    ))
    bind_context_support(summary, Support(window=8000))
    compact._memory_model, compact._memory_model_built = summary, True
    messages = history()
    assert not folded(compact.before_model({"messages": messages}, None))
    assert len(summary.requests) == 1
    assert compact.session.usage_ledger.total == 100
    assert compact.last_result["reason"] == "token_budget_exhausted"


def test_unfit_atomic_summary_source_and_failed_merge_never_drop_sources():
    compact = middleware()
    summary = Model()
    bind_context_support(summary, Support(window=2000))
    compact._memory_model, compact._memory_model_built = summary, True
    assert not folded(compact.before_model({"messages": history()}, None))
    assert not summary.requests
    assert compact.last_result["reason"] == "summary_group_exceeds_capacity"

    def reject_merge(messages):
        if "历史分段摘要" in str(messages[-1].content):
            raise RuntimeError("synthetic merge unavailable")
        return AIMessage(content="chunk")

    compact = middleware()
    summary = Model(on_call=reject_merge)
    bind_context_support(summary, Support(window=8000))
    compact._memory_model, compact._memory_model_built = summary, True
    assert not folded(compact.before_model({"messages": history()}, None))
    assert 2 < len(summary.requests) <= 8
    assert compact.last_result["status"] == "skipped"


def test_admission_known_fallback_capacity_cannot_be_enlarged_by_actor_override():
    model = bind_context_support(Model(), Support(window=5000))
    decision = check_context_admission(model, [HumanMessage(content="x" * 20_000)], config=SimpleNamespace(context_window=100_000))
    assert not decision.allowed
    assert decision.context_window == 5000
    assert decision.schema_reserve == 0  # provider already counted tools
    assert decision.estimate_complete
    assert decision.reason == "context_capacity_exceeded"


def test_admission_unknown_profile_compatible_and_opaque_estimate_nonzero():
    opaque = AIMessage(content=[{"type": "reasoning", "encrypted_content": "x" * 8000}])
    assert estimate_message_tokens(opaque) > 0
    decision = check_context_admission(Model(), [opaque], schema_reserve=0)
    assert decision.allowed and decision.context_window is None
    assert not decision.estimate_complete
    assert decision.reason == "capacity_unknown"
    native_extra = AIMessage(content="", additional_kwargs={"reasoning_content": "opaque" * 100})
    assert estimate_message_tokens(native_extra) > 0


def test_too_small_reduction_and_empty_summary_are_not_committed():
    compact = middleware(min_reduction_tokens=100_000)
    assert not folded(compact.before_model({"messages": history()}, None))
    assert compact.last_result["reason"] == "summary_not_useful_or_oversize"
    compact = middleware(Model(text=""))
    assert not folded(compact.before_model({"messages": history()}, None))
    assert compact.last_result["reason"] == "summary_failed"


def test_work_config_validation_and_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_CONTEXT_WORK_TARGET", "24000")
    monkeypatch.setenv("PHONE_AGENT_COMPACT_TARGET_RATIO", "0.6")
    configured = V2Config.from_env({"context_work_target": 0})
    assert configured.context_work_target == 0
    assert configured.compact_target_ratio == 0.6
    monkeypatch.setenv("PHONE_AGENT_COMPACT_TARGET_RATIO", "1")
    with pytest.raises(ValueError, match="COMPACT_TARGET_RATIO"):
        V2Config.from_env()
