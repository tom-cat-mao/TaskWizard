"""Block-level first-diff telemetry for adjacent actor model calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.middleware.trace import TraceMiddleware


def _request(messages):  # noqa: ANN001
    return SimpleNamespace(messages=messages, system_message=None)


def _call(middleware, messages):  # noqa: ANN001
    request = _request(messages)
    return middleware.wrap_model_call(
        request, lambda received: AIMessage(content=str(len(received.messages)))
    )


def test_first_diff_reports_new_tail_message_index():
    events: list[tuple[str, dict]] = []
    middleware = BudgetMiddleware(
        trace_recorder=lambda event, **payload: events.append((event, payload))
    )
    base = [
        SystemMessage(content="system"),
        HumanMessage(content="goal"),
        SystemMessage(content="[TASK_DOC]\nroute", id="__taskdoc__1"),
    ]

    _call(middleware, base)
    _call(
        middleware,
        [
            *base[:-1],
            AIMessage(content="next"),
            base[-1],
        ],
    )

    assert events == [
        ("model_input_first_diff", {"first_diff_block": "messages[1]"})
    ]


def test_first_diff_reports_taskdoc_before_unchanged_messages():
    events: list[tuple[str, dict]] = []
    middleware = BudgetMiddleware(
        trace_recorder=lambda event, **payload: events.append((event, payload))
    )
    first = [
        SystemMessage(content="system"),
        HumanMessage(content="goal"),
        SystemMessage(content="[TASK_DOC]\nroute A", id="__taskdoc__1"),
    ]
    second = [
        SystemMessage(content="system"),
        HumanMessage(content="goal"),
        SystemMessage(content="[TASK_DOC]\nroute B", id="__taskdoc__2"),
    ]

    _call(middleware, first)
    _call(middleware, second)

    assert events == [
        ("model_input_first_diff", {"first_diff_block": "taskdoc"})
    ]


def test_first_diff_reset_forgets_previous_run_and_trace_errors_fail_open():
    calls = 0

    def broken_trace(event, **payload):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise RuntimeError("trace unavailable")

    middleware = BudgetMiddleware(trace_recorder=broken_trace)
    first = [SystemMessage(content="system"), HumanMessage(content="one")]
    second = [SystemMessage(content="system"), HumanMessage(content="two")]

    assert _call(middleware, first).content == "2"
    assert _call(middleware, second).content == "2"
    assert calls == 1

    middleware.reset()
    _call(middleware, second)
    assert calls == 1


def test_first_diff_uses_custom_trace_event_path(tmp_path):
    trace = TraceMiddleware("first-diff", str(tmp_path), enabled=True)
    middleware = BudgetMiddleware(trace_recorder=trace.record_event)

    _call(
        middleware,
        [SystemMessage(content="system"), HumanMessage(content="one")],
    )
    _call(
        middleware,
        [SystemMessage(content="system"), HumanMessage(content="two")],
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "first-diff.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert events[0]["event"] == "model_input_first_diff"
    assert events[0]["first_diff_block"] == "messages[0]"
