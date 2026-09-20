"""Unified per-run usage ledger tests (WP-G).

All model and session objects are fakes; no network or real device is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from phone_agent.v2.middleware.budget import BudgetMiddleware
from phone_agent.v2.middleware.compact import CompactMiddleware
from phone_agent.v2.middleware.safety import build_safety_reviewer
from phone_agent.v2.usage import UsageLedger
from phone_agent.v2.verify import verify_finish


def _response(text: str, reported: int | None) -> AIMessage:
    if reported is None:
        return AIMessage(content=text)
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": reported - 2,
            "output_tokens": 2,
            "total_tokens": reported,
        },
    )


class _FakeModel:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.calls: list = []

    def invoke(self, messages):  # noqa: ANN001
        self.calls.append(messages)
        return self.response


def test_ledger_record_total_by_role_and_reset():
    ledger = UsageLedger()

    assert ledger.record("actor", estimate_tokens=10) == 10
    assert ledger.record("compact", estimate_tokens=20) == 20
    assert ledger.record("actor", estimate_tokens=5) == 5
    assert ledger.total == 35
    assert ledger.by_role() == {"actor": 15, "compact": 20}

    snapshot = ledger.by_role()
    snapshot["actor"] = 999
    assert ledger.by_role()["actor"] == 15

    ledger.reset()
    assert ledger.total == 0
    assert ledger.by_role() == {}


def test_ledger_prefers_usage_metadata_over_estimate():
    ledger = UsageLedger()
    message = _response("x" * 400, reported=17)

    assert ledger.record("verifier", message, estimate_tokens=999) == 17
    assert ledger.total == 17


@pytest.mark.parametrize(
    ("cache_metadata", "expected"),
    [
        ({"prompt_tokens_details": {"cached_tokens": 11}}, 11),
        ({"prompt_token_details": {"cached_tokens": 11}}, 11),
        ({"input_token_details": {"cached_tokens": 12}}, 12),
        ({"input_tokens_details": {"cached_tokens": 12}}, 12),
        ({"input_token_details": {"cache_read": 13}}, 13),
        ({"input_token_details": {"cache_read_input_tokens": 14}}, 14),
        ({"cached_tokens": 15}, 15),
        ({"cached_input_tokens": 16}, 16),
        ({"cache_read_input_tokens": 17}, 17),
        ({"cache_read_tokens": 17}, 17),
        ({"cacheReadInputTokens": 17}, 17),
        ({"prompt_cache_hit_tokens": 18}, 18),
        ({"cached_content_token_count": 19}, 19),
        ({"cachedContentTokenCount": 19}, 19),
        ({}, 0),
    ],
)
def test_ledger_records_provider_cached_token_variants(cache_metadata, expected):
    ledger = UsageLedger()
    message = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 20,
            "output_tokens": 2,
            "total_tokens": 22,
            **cache_metadata,
        },
    )

    assert ledger.record("actor", message) == 22
    assert ledger.cached_total == expected
    assert ledger.cached_by_role() == {"actor": expected}
    assert ledger.by_role() == {"actor": 22}


def test_ledger_cached_tokens_accumulate_by_role_and_reset():
    ledger = UsageLedger()
    actor = AIMessage(
        content="actor",
        usage_metadata={
            "input_tokens": 20,
            "output_tokens": 2,
            "total_tokens": 22,
            "prompt_tokens_details": {"cached_tokens": 9},
        },
    )
    compact = AIMessage(
        content="compact",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 1,
            "total_tokens": 11,
            "input_token_details": {"cache_read": 4},
        },
    )

    ledger.record("actor", actor)
    ledger.record("actor", actor)
    ledger.record("compact", compact)

    assert ledger.cached_total == 22
    assert ledger.cached_by_role() == {"actor": 18, "compact": 4}
    snapshot = ledger.cached_by_role()
    snapshot["actor"] = 999
    assert ledger.cached_by_role()["actor"] == 18

    ledger.reset()
    assert ledger.cached_total == 0
    assert ledger.cached_by_role() == {}


def test_ledger_does_not_double_count_raw_and_normalized_cache_aliases():
    ledger = UsageLedger()
    message = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 20,
            "output_tokens": 2,
            "total_tokens": 22,
            "prompt_tokens_details": {"cached_tokens": 8},
            "input_token_details": {"cache_read": 8},
        },
    )

    ledger.record("actor", message)

    assert ledger.cached_total == 8


def test_ledger_uses_explicit_then_message_estimate_fallback():
    ledger = UsageLedger()

    assert (
        ledger.record(
            "reviewer", AIMessage(content="x" * 400), estimate_tokens=77
        )
        == 77
    )
    assert ledger.record("distill", AIMessage(content="x" * 40)) == 10
    assert ledger.by_role() == {"reviewer": 77, "distill": 10}


def test_ledger_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown usage role"):
        UsageLedger().record("other", estimate_tokens=1)


def test_budget_with_ledger_sees_side_calls_at_warn_and_exhaustion_once():
    ledger = UsageLedger()
    budget = BudgetMiddleware(
        token_budget=1000, warn_remaining=200, ledger=ledger
    )

    ledger.record("compact", estimate_tokens=850)
    warned = budget.before_model({"messages": []}, runtime=None)
    assert warned is not None
    assert "已用约 850/1000" in warned["messages"][0].content
    assert budget.before_model({"messages": []}, runtime=None) is None

    ledger.record("verifier", estimate_tokens=175)
    stopped = budget.before_model({"messages": []}, runtime=None)
    assert stopped is not None
    assert stopped["jump_to"] == "end"
    assert "[TOKEN_BUDGET_EXHAUSTED]" in stopped["messages"][0].content
    assert budget.used_tokens == 1025
    assert budget.exhausted is True
    assert budget.before_model({"messages": []}, runtime=None) == {"jump_to": "end"}


def test_budget_with_ledger_records_actor_and_reset_clears_grand_total():
    ledger = UsageLedger()
    budget = BudgetMiddleware(token_budget=1000, warn_remaining=100, ledger=ledger)
    actor = _response("actor", reported=42)
    actor.id = "actor-1"

    budget.after_model({"messages": [actor]}, runtime=None)
    ledger.record("reviewer", estimate_tokens=8)
    assert budget.used_tokens == 50
    assert ledger.by_role() == {"actor": 42, "reviewer": 8}

    budget.reset()
    assert budget.used_tokens == 0
    assert ledger.by_role() == {}


@pytest.mark.parametrize("reported", [31, None])
def test_compact_records_shared_ledger_with_reported_or_estimated_usage(reported):
    ledger = UsageLedger()
    session = SimpleNamespace(task_doc=None, usage_ledger=ledger)
    config = SimpleNamespace(
        model_name="actor-model", context_window=10_000, memory_model=None
    )
    model = _FakeModel(_response("summary with finished facts", reported))
    middleware = CompactMiddleware(session, config, model=model)

    assert middleware._summarise([HumanMessage(content="old turn")], None)
    counted = ledger.by_role()["compact"]
    if reported is None:
        assert counted > 0
    else:
        assert counted == reported


class _VerifierSession:
    def __init__(self, ledger: UsageLedger) -> None:
        self.task_doc = None
        self.usage_ledger = ledger

    def observe(self):  # noqa: ANN201
        raise RuntimeError("no device in unit test")


@pytest.mark.parametrize("reported", [37, None])
def test_verifier_records_shared_ledger_with_reported_or_estimated_usage(reported):
    ledger = UsageLedger()
    session = _VerifierSession(ledger)
    config = SimpleNamespace(verifier_model=None, model_name="actor-model")
    model = _FakeModel(_response("APPROVE evidence matches", reported))

    verdict = verify_finish(session, config, model=model)

    assert verdict.approve is True
    counted = ledger.by_role()["verifier"]
    if reported is None:
        assert counted > 0
    else:
        assert counted == reported


@dataclass
class _ReviewerConfig:
    model_name: str = "actor-model"
    safety_reviewer_model: str | None = "reviewer-model"
    verifier_model: str | None = None


@pytest.mark.parametrize("reported", [41, None])
def test_reviewer_records_shared_ledger_with_reported_or_estimated_usage(
    monkeypatch, reported
):
    ledger = UsageLedger()
    session = SimpleNamespace(usage_ledger=ledger)
    model = _FakeModel(_response("REVERSIBLE", reported))
    monkeypatch.setattr(
        "phone_agent.v2.model.build_role_model", lambda config, **kwargs: model
    )

    reviewer = build_safety_reviewer(_ReviewerConfig(), session=session)

    assert reviewer is not None
    assert reviewer("tap", "支付方式") is True
    counted = ledger.by_role()["reviewer"]
    if reported is None:
        assert counted > 0
    else:
        assert counted == reported


def test_run_resets_shared_ledger_between_reused_agent_runs():
    from phone_agent.v2.agent import ThinPhoneAgent

    ledger = UsageLedger()
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(max_hitl_resumes=0, max_model_calls=100)
    agent.run_id = "usage-reset"
    agent.trace_path = None
    agent._trace = SimpleNamespace(_step=0)
    agent._compact = None
    agent._hitl_exhausted = False
    agent.usage_ledger = ledger
    agent.session = SimpleNamespace(
        usage_ledger=ledger,
        finished=False,
        finish_summary=None,
        takeover_reason=None,
    )

    class _Budget:
        exhausted = False

        def reset(self) -> None:
            pass

    class _Graph:
        def invoke(self, payload, config):  # noqa: ANN001
            ledger.record("compact", estimate_tokens=23)
            return {}

    agent._budget = _Budget()
    agent.agent = _Graph()
    agent._seed_task_doc = lambda task: None
    agent._prepare_app_knowledge = lambda: None
    agent._initial_messages = lambda task: []

    agent.run("first")
    assert ledger.total == 23
    ledger.record("verifier", estimate_tokens=100)
    assert ledger.total == 123

    agent.run("second")
    assert ledger.total == 23
    assert ledger.by_role() == {"compact": 23}
