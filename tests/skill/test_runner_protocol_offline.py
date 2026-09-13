"""Protocol contract against the REAL runner event producer.

These tests import ``phone_agent.v2.run_events`` / ``phone_agent.v2.agent`` and
feed the events they *actually* emit through the skill's reader, so the skill is
validated against the production schema rather than a self-consistent fixture.
No device, model, or network is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from events import RunnerEventsView, read_jsonl_with_issues
from phone_agent.v2.agent import RunResult
from phone_agent.v2.run_events import WebEventMiddleware, terminal_status


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def put(self, event: dict) -> None:
        self.events.append(event)


def _agent(session=None):
    return SimpleNamespace(session=session or SimpleNamespace(takeover_reason=None))


def test_terminal_status_vocabulary_matches_reader():
    cases = [
        (RunResult(True, "done", 1), None, "succeeded"),
        (RunResult(False, "token_budget_exhausted", 1), None, "budget_exhausted"),
        (RunResult(False, "loop_fuse", 1), None, "loop_fuse"),
        (RunResult(False, "error: boom", 1), None, "error"),
        (RunResult(False, "model_stopped", 1), None, "failed"),
        (
            RunResult(False, "需要验证码", 1),
            SimpleNamespace(takeover_reason="需要验证码"),
            "takeover",
        ),
    ]
    for result, session, expected_status in cases:
        assert terminal_status(result, _agent(session)) == expected_status
        sink = _Sink()
        mw = WebEventMiddleware(sink)
        mw.emit_run_end(result, status=expected_status)
        view = RunnerEventsView(events=sink.events)
        derived = view.harness_terminal()
        # reader state must round-trip the producer's status vocabulary
        expected_state = {
            "succeeded": "succeeded",
            "budget_exhausted": "budget_exhausted",
            "loop_fuse": "loop_fuse",
            "error": "error",
            "failed": "failed",
            "takeover": "takeover",
        }[expected_status]
        assert derived["state"] == expected_state, (expected_status, derived)


def test_real_run_end_event_shape_round_trips():
    sink = _Sink()
    mw = WebEventMiddleware(sink)
    result = RunResult(True, "已确认完成", 4, "/tmp/t.jsonl")
    mw.emit_run_end(result, status="succeeded")
    events_path = None
    # persist through the same append-only writer the runner uses
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        events_path = Path(d) / "events.jsonl"
        events_path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sink.events),
            encoding="utf-8",
        )
        rows, issues = read_jsonl_with_issues(events_path, source="events.jsonl")
        assert issues == []
    view = RunnerEventsView(events=rows)
    derived = view.harness_terminal()
    assert derived["run_end_seen"] is True
    assert derived["finished"] is True
    assert derived["finish_summary"] == "已确认完成"
    assert derived["steps"] == 4
    assert derived["trace_path"] == "/tmp/t.jsonl"


def test_real_model_call_event_feeds_usage_totals():
    sink = _Sink()
    mw = WebEventMiddleware(sink)

    class _Request:
        model = None
        messages: list = []

    response = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        response_metadata={"model_name": "provider-actual"},
    )
    mw.wrap_model_call(_Request(), lambda req: response)
    model_calls = [e for e in sink.events if e.get("event") == "model_call"]
    assert model_calls and model_calls[0]["actual_model"] == "provider-actual"
    view = RunnerEventsView(events=sink.events)
    usage = view.usage_totals()
    assert usage["reported"] is True
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 4
    assert usage["total_tokens"] == 14
    assert usage["cache_read_tokens"] is None


def test_real_tool_result_event_shape_round_trips():
    sink = _Sink()
    mw = WebEventMiddleware(sink)

    class _Request:
        tool_call = {"name": "read_screen", "args": {"intent": "观测"}, "id": "c1"}

    mw.wrap_tool_call(_Request(), lambda req: ToolMessage(content="[OBS] app=x screen#1", tool_call_id="c1"))
    kinds = [e.get("event") for e in sink.events]
    assert "tool_call" in kinds and "tool_result" in kinds
    result = next(e for e in sink.events if e.get("event") == "tool_result")
    assert result["tool"] == "read_screen"
    assert "screen#1" in result["text"]
