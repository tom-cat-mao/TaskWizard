"""Budget middleware: token-cost mirror + hard cost ceiling (S1 §3.1 / A4 §2).

A4 re-bases the run budget from *model-call count* to *token cost* — the gateway
bills per token, so tokens are the meaningful ceiling. This middleware owns the
**two-level token budget**:

* **L0 warn (mirror, never stops)** — once the remaining budget drops to
  ``warn_remaining`` it injects a single ``SystemMessage`` (once per run) that
  mirrors the remaining tokens and the full option space (finish / write key
  facts into the TaskDoc / converge the route / take_over). Pure information.
* **Hard cost ceiling (stops once)** — once cumulative usage reaches
  ``token_budget`` it jumps the graph to ``end`` with a ``[TOKEN_BUDGET_EXHAUSTED]``
  marker. A fresh, harness-issued finish review may receive one extra actor
  response per run, scoped to a single real confirmation of that transaction.
  Other tool operations cannot consume this allowance. Usage keeps accumulating;
  this is a call-boundary threshold, not a guarantee of zero token overshoot.
  ``PHONE_AGENT_MAX_STEPS`` remains an independent **runaway-loop fuse**.

Cumulative accounting is compaction-proof: the auto-compact middleware replaces
old ``AIMessage``s (and their ``usage_metadata``) with a summary, so re-summing
the live transcript would *undercount* the cost already billed. With a shared
``UsageLedger``, actor turns and side-model calls contribute to one per-run total;
without one, the original private actor counter remains the compatibility path.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from phone_agent.v2.middleware._tokens import (
    estimate_context_tokens,
    estimate_message_tokens,
    usage_tokens,
)
from phone_agent.v2.pins import TASKDOC_ID_PREFIX

if TYPE_CHECKING:
    from phone_agent.v2.review import FinishReviewTicket
    from phone_agent.v2.usage import UsageLedger

# Terminal marker text the hard ceiling injects; agent._build_result keys on the
# instance flag, not this string, but it keeps the transcript self-describing.
TOKEN_BUDGET_EXHAUSTED_MARKER = "[TOKEN_BUDGET_EXHAUSTED]"

_WARN_TEXT = {
    "cn": (
        "Token 预算余量：已用约 {used}/{budget}，剩约 {remaining}。"
        "若任务已达成请立即 finish（附 evidence）；否则可用 update_task_doc "
        "把要紧事实写进任务板（压缩免疫）、收敛路线并优先做关键项；"
        "若需人工介入请 take_over。"
    ),
    "en": (
        "Token budget remaining: ~{used}/{budget} used, ~{remaining} left. "
        "If the task is done call finish (with evidence); otherwise use "
        "update_task_doc to record key facts into the board (compaction-immune), "
        "converge the route and do critical items first; take_over if a human is "
        "needed."
    ),
}

_EXHAUSTED_TEXT = {
    "cn": (
        TOKEN_BUDGET_EXHAUSTED_MARKER
        + " Token 预算已耗尽（约 {used}/{budget}），本次运行结束。"
    ),
    "en": (
        TOKEN_BUDGET_EXHAUSTED_MARKER
        + " Token budget exhausted (~{used}/{budget}); ending this run."
    ),
}

_FINISH_CONTINUATION_TEXT = {
    "cn": (
        "Token 预算已耗尽；已有仍有效的 finish 复核包，本次仅提供一次真实回复机会处理该确认。"
        "确认无误可调用 finish(confirm=true)，也可拒绝完成或请求人工介入。"
        "这份续办额度不覆盖其他工具操作，不会补充总预算或再次续办。"
    ),
    "en": (
        "The token budget is exhausted. A fresh finish review is pending; this is "
        "one response opportunity to handle that confirmation. Confirm with "
        "finish(confirm=true), decline completion, or request human assistance. "
        "This allowance covers no other tool operations and cannot be renewed."
    ),
}


class BudgetMiddleware(AgentMiddleware):
    """Cumulative threshold with one bounded pending-finish continuation."""

    def __init__(
        self,
        token_budget: int = 1_000_000,
        warn_remaining: int = 100_000,
        lang: str = "cn",
        ledger: UsageLedger | None = None,
        trace_recorder: Any | None = None,
        session: Any | None = None,
    ) -> None:
        super().__init__()
        self.token_budget = max(1, int(token_budget))
        # Clamp the absolute warn line into (0, token_budget]; an out-of-range
        # value must neither disable the warn nor fire it before any usage.
        warn = int(warn_remaining)
        if warn <= 0 or warn > self.token_budget:
            warn = min(100_000, self.token_budget)
        self.warn_remaining = warn
        self.lang = lang
        self.ledger = ledger
        self._trace_recorder = trace_recorder
        self._session = session
        self._warned = False
        self._exhausted = False
        self._used_tokens = 0
        self._counted_id: str | None = None
        self._previous_input_blocks: tuple[str, str, tuple[str, ...]] | None = None
        self._finish_continuation_granted = False
        self._finish_continuation_active = False
        self._finish_continuation_ticket: FinishReviewTicket | None = None
        self._finish_response_bound = False
        self._finish_call_id: str | None = None
        self._finish_call_used = False
        self._finish_continuation_closed = False

    def reset(self) -> None:
        """Clear per-run state so a reused agent budgets the next run from zero."""

        self._warned = False
        self._exhausted = False
        self._used_tokens = 0
        self._counted_id = None
        self._previous_input_blocks = None
        self._finish_continuation_granted = False
        self._finish_continuation_active = False
        self._finish_continuation_ticket = None
        self._finish_response_bound = False
        self._finish_call_id = None
        self._finish_call_used = False
        self._finish_continuation_closed = False
        if self._session is not None:
            from phone_agent.v2.review import store_finish_review_ticket

            store_finish_review_ticket(self._session, None)
        if self.ledger is not None:
            self.ledger.reset()

    @property
    def used_tokens(self) -> int:
        """Cumulative billed tokens accounted so far (compaction-proof)."""

        if self.ledger is not None:
            return self.ledger.total
        return self._used_tokens

    @property
    def exhausted(self) -> bool:
        """True once the hard cost ceiling fired this run."""

        return self._exhausted

    # ------------------------------------------------------------------
    def _newest_ai(self, messages: list[Any]) -> AIMessage | None:
        for msg in reversed(messages or []):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _accumulate(self, messages: list[Any]) -> None:
        """Add one model turn's usage to the cumulative counter (once).

        Real ``usage_metadata`` stays authoritative. When a call reports none,
        the fallback counts the whole turn — the full request input (every
        message before the newest ``AIMessage``) plus the newest ``AIMessage``
        (output) — so a gateway that omits usage cannot make the hard cost
        ceiling meaningless.
        """

        newest = self._newest_ai(messages)
        if newest is None:
            return
        msg_id = getattr(newest, "id", None)
        # Only count a given AI message once. When ids are absent (rare), fall back
        # to always counting the newest — after_model runs once per model call.
        if msg_id is not None and msg_id == self._counted_id:
            return
        estimate = self._turn_estimate(messages, newest)
        if self.ledger is not None:
            self.ledger.record("actor", newest, estimate_tokens=estimate)
        else:
            reported = usage_tokens(newest)
            self._used_tokens += reported if reported is not None else estimate
        self._counted_id = msg_id

    def _turn_estimate(self, messages: list[Any], newest: AIMessage) -> int:
        """Estimate one model turn as request-input + output tokens.

        When the newest ``AIMessage`` carries real usage this returns the
        legacy output-only estimate (the ledger prefers the reported value
        anyway); otherwise it estimates the full request input (everything
        before the newest AI turn) plus the output message itself.
        """

        if usage_tokens(newest) is not None:
            return estimate_message_tokens(newest)
        end = len(messages) - 1
        for index in range(len(messages) - 1, -1, -1):
            if messages[index] is newest:
                end = index
                break
        return estimate_context_tokens(messages[:end]) + estimate_message_tokens(
            newest
        )

    def _warn_text(self) -> str:
        template = _WARN_TEXT.get(self.lang, _WARN_TEXT["cn"])
        used = self.used_tokens
        remaining = max(0, self.token_budget - used)
        return template.format(
            used=used, budget=self.token_budget, remaining=remaining
        )

    def _exhausted_text(self) -> str:
        template = _EXHAUSTED_TEXT.get(self.lang, _EXHAUSTED_TEXT["cn"])
        return template.format(used=self.used_tokens, budget=self.token_budget)

    def _finish_event(self, stage: str, reason: str) -> None:
        """Write only bounded world-state metadata, never review/board contents."""

        if callable(self._trace_recorder):
            try:
                self._trace_recorder(
                    "token_budget_finish_continuation",
                    stage=stage,
                    reason=reason,
                    used_tokens=self.used_tokens,
                    token_budget=self.token_budget,
                )
            except Exception:  # noqa: BLE001 - telemetry cannot grant or revoke authority
                pass

    def _close_finish_continuation(self, reason: str) -> None:
        if not self._finish_continuation_closed:
            self._finish_continuation_closed = True
            self._finish_event("exhausted", reason)

    def _grant_finish_continuation(self) -> bool:
        if self._finish_continuation_granted or self._session is None:
            return False
        from phone_agent.v2.review import pending_finish_review

        ticket = pending_finish_review(self._session)
        if ticket is None:
            return False
        self._finish_continuation_granted = True
        self._finish_continuation_ticket = ticket
        self._finish_event("granted", "fresh_pending_review")
        return True

    def _start_finish_continuation(self) -> None:
        if self._finish_continuation_granted and not self._finish_continuation_active:
            self._finish_continuation_active = True
            self._finish_event("used", "actor_request")

    def _bind_finish_response(self, messages: list[Any]) -> None:
        """Bind the exception to at most one confirm in the granted response."""

        if not self._finish_continuation_active or self._finish_response_bound:
            return
        self._finish_response_bound = True
        newest = self._newest_ai(messages)
        for call in getattr(newest, "tool_calls", None) or []:
            if not isinstance(call, dict) or call.get("name") != "finish":
                continue
            args = call.get("args") or {}
            if isinstance(args, dict) and args.get("confirm") is True and call.get("id"):
                self._finish_call_id = str(call["id"])
            # Repeated review/confirm siblings never manufacture another chance.
            break
        if self._finish_call_id is None:
            self._close_finish_continuation("no_confirmation")

    def on_tool_execute(self, request: Any, next: Any) -> Any:
        """Scope the single response allowance to its existing finish transaction.

        Mount inside core admission/control HITL and outside safety. Ordinary
        tools from the response that first crossed the threshold are untouched:
        this fence activates only when the extra actor request actually runs.
        """

        call = getattr(request, "tool_call", None) or {}
        if not isinstance(call, dict):
            call = {key: getattr(call, key, None) for key in ("name", "id", "args")}
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        if not self._finish_continuation_active:
            if name != "finish" and self._session is not None:
                from phone_agent.v2.review import store_finish_review_ticket

                # Dispatch can change the world even when the tool later fails
                # before observe() (screen_seq then stays frozen). A delegated
                # ordinary/plugin tool supersedes the old review regardless of
                # its result; only a subsequent successful review can replace it.
                store_finish_review_ticket(self._session, None)
            return next(request)
        if name in {"ask_user", "take_over"}:
            return next(request)

        from phone_agent.v2.review import pending_finish_review

        args = call.get("args") or {}
        allowed = (
            name == "finish"
            and isinstance(args, dict)
            and args.get("confirm") is True
            and call_id == self._finish_call_id
            and not self._finish_call_used
            and pending_finish_review(self._session) is self._finish_continuation_ticket
        )
        if not allowed:
            self._finish_event("tool_blocked", "outside_completion_transaction")
            return ToolMessage(
                content=(
                    "error: Token budget exhausted; this tool was not executed. "
                    "The one-shot allowance only covers the pending finish confirmation."
                ),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        self._finish_call_used = True
        try:
            result = next(request)
        except Exception as exc:
            self._finish_call_failed(exc)
            raise
        if inspect.isawaitable(result):
            async def complete():
                try:
                    resolved = await result
                except Exception as exc:
                    self._finish_call_failed(exc)
                    raise
                self._finish_call_completed()
                return resolved

            return complete()
        self._finish_call_completed()
        return result

    def _finish_call_failed(self, error: Exception) -> None:
        from langgraph.errors import GraphInterrupt

        if isinstance(error, GraphInterrupt):
            # Resume the identical interrupted tool, never another actor
            # request or a sibling confirm; core HITL retains authority.
            self._finish_call_used = False
        else:
            self._close_finish_continuation("confirmation_error")

    def _finish_call_completed(self) -> None:
        if getattr(self._session, "finished", False):
            self._finish_event("confirmed", "finish_accepted")
        else:
            self._close_finish_continuation("confirmation_not_accepted")

    def wrap_tool_call(self, request, handler):  # noqa: ANN001
        return self.on_tool_execute(request, handler)

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        result = self.on_tool_execute(request, handler)
        return await result if inspect.isawaitable(result) else result

    def _record_first_diff(self, request: Any) -> None:
        """Trace the first changed coarse input block between model calls."""

        current = _input_block_hashes(request)
        previous = self._previous_input_blocks
        self._previous_input_blocks = current
        if previous is None:
            return
        first_diff = _first_diff_block(previous, current)
        if first_diff is None or not callable(self._trace_recorder):
            return
        try:
            self._trace_recorder(
                "model_input_first_diff", first_diff_block=first_diff
            )
        except Exception:  # noqa: BLE001 - cache telemetry must never affect a call
            pass

    # ------------------------------------------------------------------
    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        remaining = self.token_budget - self.used_tokens
        # Hard cost ceiling: stop the run once the budget is fully spent.
        if remaining <= 0:
            was_exhausted = self._exhausted
            self._exhausted = True
            if self._grant_finish_continuation():
                template = _FINISH_CONTINUATION_TEXT.get(
                    self.lang, _FINISH_CONTINUATION_TEXT["cn"]
                )
                return {"messages": [SystemMessage(content=template)]}
            if self._finish_continuation_granted:
                self._close_finish_continuation("actor_opportunity_spent")
            if not was_exhausted:
                return {
                    "jump_to": "end",
                    "messages": [AIMessage(content=self._exhausted_text())],
                }
            return {"jump_to": "end"}
        # L0 warn mirror (once per run) as the remaining budget crosses the line.
        if not self._warned and remaining <= self.warn_remaining:
            self._warned = True
            return {"messages": [SystemMessage(content=self._warn_text())]}
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        return self.before_model(state, runtime)

    def after_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        messages = state.get("messages") if isinstance(state, dict) else None
        self._accumulate(messages or [])
        self._bind_finish_response(messages or [])
        return None

    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        return self.after_model(state, runtime)

    def wrap_model_call(self, request, handler):  # noqa: ANN001
        self._start_finish_continuation()
        self._record_first_diff(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        self._start_finish_continuation()
        self._record_first_diff(request)
        return await handler(request)

    # -- event-listener adapters -------------------------------------------
    def on_pre_request(self, messages: list[Any], next: Any) -> Any:
        """Adapter for the ``model/pre_request`` waterfall.

        Mirrors ``before_model`` under the full-list contract: the warn mirror
        is **appended** to the transformed transcript (a same-turn T2 fold and
        the TaskDoc refresh must survive), while the hard ceiling
        short-circuits with ``jump_to="end"``.
        """

        result = self.before_model({"messages": messages}, None)
        if result is None:
            return next(messages)
        if isinstance(result, dict) and result.get("jump_to") == "end":
            return result
        if isinstance(result, dict) and "messages" in result:
            return next(list(messages) + list(result["messages"]))
        return next(messages)

    def on_model_request(self, request: Any, next: Any) -> Any:
        """Adapter for the ``model/request`` waterfall (wraps the real call)."""

        self._start_finish_continuation()
        self._record_first_diff(request)
        return next(request)

    def on_post_request(self, payload: dict[str, Any]) -> None:
        """Adapter for the ``model/post_request`` emit."""

        self.after_model(payload, payload.get("runtime"))


def _input_block_hashes(request: Any) -> tuple[str, str, tuple[str, ...]]:
    """Hash system / pinned TaskDoc / remaining message blocks separately."""

    messages = list(getattr(request, "messages", None) or [])
    system_message = getattr(request, "system_message", None)
    system_index: int | None = None
    if system_message is None:
        for index, message in enumerate(messages):
            if isinstance(message, SystemMessage) and not _is_taskdoc(message):
                system_message = message
                system_index = index
                break

    taskdoc_messages: list[Any] = []
    tail: list[Any] = []
    for index, message in enumerate(messages):
        if index == system_index:
            continue
        if _is_taskdoc(message):
            taskdoc_messages.append(message)
        else:
            tail.append(message)

    return (
        _block_hash(system_message),
        _block_hash(taskdoc_messages),
        tuple(_block_hash(message) for message in tail),
    )


def _first_diff_block(
    previous: tuple[str, str, tuple[str, ...]],
    current: tuple[str, str, tuple[str, ...]],
) -> str | None:
    if previous[0] != current[0]:
        return "system"
    if previous[1] != current[1]:
        return "taskdoc"
    old_messages, new_messages = previous[2], current[2]
    for index, (old, new) in enumerate(zip(old_messages, new_messages)):
        if old != new:
            return f"messages[{index}]"
    if len(old_messages) != len(new_messages):
        return f"messages[{min(len(old_messages), len(new_messages))}]"
    return None


def _is_taskdoc(message: Any) -> bool:
    message_id = str(getattr(message, "id", None) or "")
    if message_id.startswith(TASKDOC_ID_PREFIX):
        return True
    content = getattr(message, "content", "")
    return isinstance(content, str) and content.startswith("[TASK_DOC]")


def _block_hash(value: Any) -> str:
    encoded = json.dumps(
        _hashable_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hashable_value(value: Any) -> Any:
    """Keep only provider-relevant message data; never persist the result."""

    if isinstance(value, BaseMessage):
        return {
            "type": value.type,
            "content": _hashable_value(value.content),
            "name": getattr(value, "name", None),
            "tool_calls": _hashable_value(getattr(value, "tool_calls", None)),
            "tool_call_id": getattr(value, "tool_call_id", None),
            "additional_kwargs": _hashable_value(
                getattr(value, "additional_kwargs", None)
            ),
        }
    if isinstance(value, dict):
        return {str(key): _hashable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hashable_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_budget_middleware(
    token_budget: int = 1_000_000,
    warn_remaining: int = 100_000,
    lang: str = "cn",
    ledger: UsageLedger | None = None,
    trace_recorder: Any | None = None,
    session: Any | None = None,
) -> BudgetMiddleware:
    """Build a :class:`BudgetMiddleware` from the resolved config values."""

    return BudgetMiddleware(
        token_budget=token_budget,
        warn_remaining=warn_remaining,
        lang=lang,
        ledger=ledger,
        trace_recorder=trace_recorder,
        session=session,
    )


__all__ = [
    "BudgetMiddleware",
    "build_budget_middleware",
    "TOKEN_BUDGET_EXHAUSTED_MARKER",
]
