"""Honest cache observability without changing the token budget policy."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.middleware.trace import TraceWriter
from phone_agent.v2.usage import UsageLedger, usage_details


def test_usage_distinguishes_missing_cache_write_from_reported_zero():
    missing = AIMessage(content="ok")
    zero = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_token_details": {"cache_read": 0, "cache_creation": 0},
        },
    )
    assert all(value is None for value in usage_details(missing).values())
    assert all(value == 0 for value in usage_details(zero).values())


@pytest.mark.parametrize("tier", ["", "priority_", "flex_"])
def test_cache_tiers_are_observed_without_discounting_run_budget(tier):
    message = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 10000,
            "output_tokens": 100,
            "total_tokens": 10100,
            "input_token_details": {
                f"{tier}cache_read": 8000,
                f"{tier}cache_creation": 1500,
            },
        },
    )
    ledger = UsageLedger()
    ledger.record("actor", message)
    assert ledger.total == 10100
    assert ledger.cached_total == 8000
    assert usage_details(SimpleNamespace(result=[message])) == {
        "input_tokens": 10000,
        "output_tokens": 100,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 1500,
    }


def test_usage_aliases_do_not_double_count_or_leak_extra_metadata(tmp_path):
    message = AIMessage(
        content="private response must not enter the numeric trace",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 10,
            "total_tokens": 1010,
            "input_token_details": {"cache_read": 800},
            "prompt_tokens_details": {"cached_tokens": 800},
            "private_note": "Bearer private-synthetic-value",
        },
    )
    trace = TraceWriter("numeric", str(tmp_path))
    trace.on_model_request(SimpleNamespace(), lambda request: message)
    text = (tmp_path / "numeric.jsonl").read_text()
    event = json.loads(text)
    assert event["cache_read_tokens"] == 800
    assert event["cache_write_tokens"] is None
    assert event["input_tokens"] == 1000
    assert "private" not in text


def test_image_change_precedes_taskdoc_even_when_both_change():
    events = []
    budget = BudgetMiddleware(
        trace_recorder=lambda event, **data: events.append((event, data))
    )
    before = [
        SystemMessage(content="stable rules"),
        HumanMessage(content=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]),
        SystemMessage(content="[TASK_DOC] old", id="__taskdoc__before"),
    ]
    after = [
        SystemMessage(content="stable rules"),
        HumanMessage(content=[{"type": "text", "text": "[screen#1 已剪除]"}]),
        SystemMessage(content="[TASK_DOC] new", id="__taskdoc__after"),
    ]
    for messages in (before, after):
        budget.wrap_model_call(
            SimpleNamespace(messages=messages, system_message=None),
            lambda request: AIMessage(content="ok"),
        )
    assert events == [("model_input_first_diff", {"first_diff_block": "messages[0]"})]


def test_reducer_only_taskdoc_id_change_is_not_prompt_change():
    events = []
    budget = BudgetMiddleware(trace_recorder=lambda *args, **kwargs: events.append(args))
    for message_id in ("__taskdoc__first", "__taskdoc__second"):
        budget.wrap_model_call(
            SimpleNamespace(
                messages=[SystemMessage(content="[TASK_DOC] same", id=message_id)],
                system_message=None,
            ),
            lambda request: AIMessage(content="ok"),
        )
    assert events == []
