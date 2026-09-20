"""Order-independence proofs for the token budget vs. model-call limit gates (WP-D2).

Per AGENTS.md P0 #13 the token budget is a hard cost ceiling and
``PHONE_AGENT_MAX_STEPS`` is only a runaway-loop fuse.  These tests prove that
swapping the middleware order does not change which terminal branch fires.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from phone_agent.v2.middleware.budget import (
    TOKEN_BUDGET_EXHAUSTED_MARKER,
    BudgetMiddleware,
)
from phone_agent.v2.usage import UsageLedger


class _LoopModel(BaseChatModel):
    """A fake model that calls ``noop`` on every turn until the graph ends."""

    calls: int = 0
    usage_per_call: int = 30

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=self._ai_message())])

    def _ai_message(self) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "noop",
                    "args": {},
                    "id": f"c{self.calls}",
                    "type": "tool_call",
                }
            ],
            usage_metadata={
                "input_tokens": self.usage_per_call,
                "output_tokens": 0,
                "total_tokens": self.usage_per_call,
            },
        )

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return "loop-model"


@tool
def _noop() -> str:
    """No-op tool that keeps the loop alive."""
    return "ok"


def _run_with_order(
    order: list[str],
    *,
    token_budget: int,
    run_limit: int,
    usage_per_call: int,
    warn_remaining: int = 1,
) -> tuple[int, list[Any]]:
    """Build a minimal agent with the given middleware order and run it once."""

    budget = BudgetMiddleware(
        token_budget=token_budget,
        warn_remaining=warn_remaining,
        ledger=UsageLedger(),
    )
    limiter = ModelCallLimitMiddleware(run_limit=run_limit, exit_behavior="end")
    middleware = (
        [budget, limiter]
        if order == ["budget", "limit"]
        else [limiter, budget]
    )

    model = _LoopModel(usage_per_call=usage_per_call)
    agent = create_agent(
        model,
        tools=[_noop],
        middleware=middleware,
        system_prompt="loop",
    )
    result = agent.invoke({"messages": [HumanMessage("go")]})
    return model.calls, result["messages"]


def _terminal_text(messages: list[Any]) -> str:
    return " ".join(str(getattr(m, "content", "")) for m in messages)


@pytest.mark.parametrize(
    "order",
    [
        ["budget", "limit"],
        ["limit", "budget"],
    ],
)
def test_token_budget_branch_independent_of_order(order: list[str]) -> None:
    """When tokens run out first, both orderings report token-budget exhaustion."""

    calls, messages = _run_with_order(
        order,
        token_budget=50,
        run_limit=10,
        usage_per_call=30,
    )

    assert calls == 2
    assert TOKEN_BUDGET_EXHAUSTED_MARKER in _terminal_text(messages)


@pytest.mark.parametrize(
    "order",
    [
        ["budget", "limit"],
        ["limit", "budget"],
    ],
)
def test_loop_fuse_branch_independent_of_order(order: list[str]) -> None:
    """When the step fuse blows first, both orderings report loop_fuse semantics."""

    calls, messages = _run_with_order(
        order,
        token_budget=1000,
        run_limit=3,
        usage_per_call=10,
    )

    assert calls == 3
    assert "Model call limits exceeded" in _terminal_text(messages)


def test_budget_accumulation_depends_only_on_newest_ai_turn() -> None:
    """Budget accounting depends only on the newest AI turn, not on context.

    This is the lightweight, equivalent argument behind order independence:
    ``BudgetMiddleware.after_model`` extracts the newest ``AIMessage`` (the last
    one in the list, which is the LangChain convention) and adds its
    ``usage_metadata`` to the ledger.  Older messages and surrounding context do
    not affect the cumulative total, so reordering middleware around the budget
    gate cannot change the cost signal.
    """

    ledger = UsageLedger()
    mw = BudgetMiddleware(token_budget=10_000, warn_remaining=1, ledger=ledger)

    ai = AIMessage(
        content="answer",
        id="m1",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    )

    # Same newest AI turn, different surrounding context.
    mw.after_model({"messages": [HumanMessage("earlier"), ai]}, runtime=None)
    mw.after_model({"messages": [ai, HumanMessage("later")]}, runtime=None)

    # The second call has the same id as the first, so it is not double-counted.
    assert mw.used_tokens == 150

    ai2 = AIMessage(
        content="another",
        id="m2",
        usage_metadata={
            "input_tokens": 200,
            "output_tokens": 100,
            "total_tokens": 300,
        },
    )
    mw.after_model(
        {"messages": [HumanMessage("noise"), ai2, HumanMessage("more")]},
        runtime=None,
    )
    assert mw.used_tokens == 450
