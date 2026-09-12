"""Deterministic observation hygiene and bounded semantic history compaction.

Where ``middleware/images.py`` does *fine-grained* context hygiene (roll off old
screenshots + fold old marks digests), this middleware does the *coarse* fold: as
the transcript approaches the model's context window it summarises the **ancient
segment** into one structured hand-off block, keeping only a recent verbatim tail
plus the compression-immune anchors (the system prompt + the pinned ``[TASK_DOC]``
block). The listener runs first and prunes images/marks before planning a fold.

Two thresholds, both measured as an estimate of the current context tokens
(script-aware text estimate + 1500 per image, :mod:`._tokens`, padded with the
``schema_reserve`` + ``output_reserve`` request overhead) against the inferred
context window:

* **T1 warn** (``compact_warn_ratio``, default 0.75) — inject a single
  ``SystemMessage`` (once per run) telling the model to write anything important
  into the TaskDoc (compaction-immune) and wind down loose exploration. Pure
  information, no context change.
* **T2 force** (``compact_trigger_ratio``, default 0.92) — call a text-only LLM
  (no tools; ``config.memory_model`` falling back to the main model) to produce a
  phone-oriented structured hand-off summary (目标/路线进度/关键事实/动作史/
  错误与修复/用户补充/当前屏幕/下一步), then rebuild the transcript as
  ``[system prompt] + [COMPACT_SUMMARY] + [recent tail] + [fresh-observation hint]
  + [pinned TaskDoc]``. The cut point never splits a ``tool_use``/``tool_result``
  pair and never folds the pinned blocks. Compaction is **iterative**: a prior
  ``[COMPACT_SUMMARY]`` is fed back in as input and superseded. A too-long input
  is handled in complete groups when the summary model has a smaller capacity.
  Retries never silently discard source groups. Failed, oversized or useless
  summaries leave the already-pruned baseline intact.

The production full-input work target is independent of physical model capacity.
Every summary invoke checks the existing run token budget. Final admission also
belongs at dispatch after pins and at each actual fallback model; this listener
never rewrites only a provider-local request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from phone_agent.v2.middleware._tokens import (
    estimate_context_tokens,
    estimate_message_tokens,
)
from phone_agent.v2.middleware.images import ContextPrunerService
from phone_agent.v2.middleware.context_admission import check_context_admission
from phone_agent.v2.middleware.images import (
    _message_has_image,
    _message_has_obs_marks,
)
from phone_agent.v2.middleware.taskdoc import TaskDocInjector
from phone_agent.v2.pins import COMPACT_ID_PREFIX, TASKDOC_ID_PREFIX
from phone_agent.v2.providers.context import (
    model_context_profile,
    model_protected_message_ids,
    prepare_model_messages,
)

MAX_SUMMARY_INVOKES = 8

# Default context window when the model name carries no size hint (design: 256k).
_DEFAULT_WINDOW = 256_000

# Model-name size hints (checked as lowercase substrings, largest-first so
# "1000k" wins over "100k" etc.).
_WINDOW_HINTS: tuple[tuple[str, int], ...] = (
    ("2m", 2_000_000),
    ("1m", 1_000_000),
    ("1000k", 1_000_000),
    ("512k", 512_000),
    ("256k", 256_000),
    ("200k", 200_000),
    ("128k", 128_000),
    ("100k", 100_000),
    ("64k", 64_000),
    ("32k", 32_000),
    ("16k", 16_000),
    ("8k", 8_000),
)

_WARN_TEXT = {
    "cn": (
        "[COMPACT_WARN] 上下文已达到工作目标或接近窗口阈值。请把要紧事实（已选值/价格/关键 id/"
        "下一步计划）用 update_task_doc 写入任务板（压缩免疫），并收尾松散探索、"
        "聚焦关键路线项。"
    ),
    "en": (
        "[COMPACT_WARN] Context reached the work target or is nearing the window threshold. Record key facts "
        "(chosen values / prices / key ids / next steps) into the TaskDoc via "
        "update_task_doc (compaction-immune), and wind down loose exploration to "
        "focus on the critical route items."
    ),
}

_FRESH_OBS_TEXT = {
    "cn": (
        "[COMPACT_DONE] 已将较早的历史压缩为上方交接摘要，当前保留的观测仍以其批次为准。"
        "不要使用摘要中的旧 mark；若缺少有效观测，请 read_screen 后再操作。"
    ),
    "en": (
        "[COMPACT_DONE] Earlier history was compacted into the hand-off summary "
        "above. Retained observations keep their own batch identity. Do not use "
        "old marks from the summary; call read_screen if a valid observation is missing."
    ),
}

_SUMMARY_SYSTEM = {
    "cn": (
        "你是手机自动化任务的上下文压缩器。把给定历史压缩成结构化交接摘要，"
        "供同一智能体无缝继续执行。只输出摘要本身，不要执行任何操作、不要调用工具、"
        "不要编造未发生的结果。务必保留：已选择的值、价格/金额、关键 mark/id、"
        "已完成与未完成的步骤、遇到的错误与如何修复、结果未知或可能已经执行的状态、用户的额外要求。"
    ),
    "en": (
        "You compress the context of a phone-automation task into a structured "
        "hand-off summary so the same agent can continue seamlessly. Output only the "
        "summary; do not act, call tools, or invent results. Preserve chosen values, "
        "prices/amounts, key marks/ids, done vs pending steps, errors and how they "
        "were fixed, unknown outcomes or possibly executed actions, and any extra user requirements."
    ),
}

_SUMMARY_SECTIONS = {
    "cn": (
        "请输出交接摘要，按这些小节组织：\n"
        "## 目标\n## 路线进度\n## 关键事实\n## 动作史(digest)\n"
        "## 错误与修复\n## 用户补充\n## 当前屏幕状态\n## 下一步建议"
    ),
    "en": (
        "Produce the hand-off summary with these sections:\n"
        "## Goal\n## Route progress\n## Key facts\n## Action history (digest)\n"
        "## Errors and fixes\n## User additions\n## Current screen\n## Next steps"
    ),
}


def infer_context_window(model_name: str | None, override: int | None) -> int:
    """Resolve the context window: explicit override > model-name hint > default."""

    if override and int(override) > 0:
        return int(override)
    name = (model_name or "").lower()
    for hint, value in _WINDOW_HINTS:
        if hint in name:
            return value
    return _DEFAULT_WINDOW


def _is_cn(lang: str) -> bool:
    return (lang or "").strip().lower() in {"cn", "zh", "zh-cn", "zh_cn", "chinese"}


def _actual_actor_ref(config: Any) -> str | None:
    """Actor reference the built model came from, tolerating stub model modules.

    ``actual_role_ref`` lives in the real model module; stubs carry only
    ``build_chat_model``.  Falls back to plain role resolution and returns
    ``None`` when no reference can be produced.
    """

    try:
        from phone_agent.v2.model import actual_role_ref
    except ImportError:
        actual_role_ref = None
    if callable(actual_role_ref):
        try:
            ref = actual_role_ref(config, "actor")
        except Exception:  # noqa: BLE001 - fall through to role resolution
            ref = None
        if ref:
            return str(ref)
    try:
        from phone_agent.v2.providers import resolve_role_ref

        ref = resolve_role_ref(
            config, "actor", registry=getattr(config, "_provider_registry", None)
        )
    except Exception:  # noqa: BLE001 - no resolvable actor reference
        return None
    return str(ref) if ref else None


def _pinned_id(message: Any) -> bool:
    mid = getattr(message, "id", None) or ""
    return mid.startswith(TASKDOC_ID_PREFIX) or mid.startswith(COMPACT_ID_PREFIX)


def _text_of(message: Any) -> str:
    """Flatten a message's content to text; images -> ``[截图]`` placeholder."""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype in {"image_url", "image"} or "image_url" in block:
                    parts.append("[截图]")
            elif isinstance(block, str):
                parts.append(block)
    return " ".join(p for p in parts if p)


def _render_line(message: Any) -> str:
    """Render one conversation message as a single ``role: text`` line."""

    text = _text_of(message).strip()
    if isinstance(message, HumanMessage):
        return f"用户: {text}"
    if isinstance(message, ToolMessage):
        name = getattr(message, "name", None) or "?"
        return f"工具[{name}]: {text}"
    if isinstance(message, AIMessage):
        calls = getattr(message, "tool_calls", None) or []
        call_text = "; ".join(
            f"{c.get('name', '?')}({c.get('args', {})})" for c in calls if isinstance(c, dict)
        )
        head = f"智能体: {text}" if text else "智能体:"
        return f"{head} → 调用 {call_text}" if call_text else head
    if isinstance(message, SystemMessage):
        return f"系统: {text}"
    return f"{type(message).__name__}: {text}"


def _group_turns(conversation: list[Any]) -> list[list[Any]]:
    """Keep an AI message and every sibling tool result in one unit.

    A turn starts at a ``HumanMessage`` or an ``AIMessage``; ``ToolMessage``s (and
    stray system messages) attach to the current turn. These are the atomic
    boundaries for tail selection and summary chunks. Incomplete units are
    protected by ``_closed_group`` rather than split or silently repaired.
    """

    groups: list[list[Any]] = []
    for msg in conversation:
        if isinstance(msg, (HumanMessage, AIMessage)) or not groups:
            groups.append([msg])
        else:
            groups[-1].append(msg)
    return groups


def _closed_group(group: list[Any]) -> bool:
    expected: set[str] = set()
    answered: set[str] = set()
    for message in group:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                cid = call.get("id")
                if not cid or cid in expected:
                    return False
                expected.add(str(cid))
        elif isinstance(message, ToolMessage):
            cid = str(message.tool_call_id)
            if cid not in expected or cid in answered:
                return False
            answered.add(cid)
    return expected == answered


def _has_native_content(message: Any) -> bool:
    """Unknown/opaque protocol blocks are never interpreted as ordinary text."""

    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and (
                block.get("type") not in {"text", "image", "image_url"}
                or any(key in block for key in ("signature", "thought_signature", "encrypted_content"))
            ):
                return True
    extra = getattr(message, "additional_kwargs", None) or {}
    return any(value for key, value in extra.items() if key not in {"tool_calls", "function_call"})


class CompactMiddleware(AgentMiddleware):
    """before_model: T1 warn + T2 forced structured hand-off compaction."""

    def __init__(
        self,
        session: Any,
        config: Any,
        *,
        model: Any | None = None,
        memory_state_provider: Any | None = None,
        pruner: ContextPrunerService | None = None,
        warn_ratio: float = 0.75,
        trigger_ratio: float = 0.92,
        keep_ratio: float = 0.5,
        max_ptl_retries: int = 3,
        min_fold_messages: int = 4,
        schema_reserve: int = 3000,
        output_reserve: int = 2000,
        lang: str = "cn",
        work_target: int = 0,
        target_ratio: float = 0.7,
        summary_tokens: int = 2000,
        min_reduction_tokens: int = 1000,
        min_reduction_ratio: float = 0.1,
        tools_provider: Callable[[], Sequence[Any]] | None = None,
        trace_recorder: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.config = config
        self._main_model = model
        self._memory_state_provider = memory_state_provider
        self._pruner = pruner
        self.work_target = max(0, int(work_target))
        self.target_ratio = _clamp_ratio(target_ratio, 0.7)
        self.summary_tokens = max(1, int(summary_tokens))
        self.min_reduction_tokens = max(0, int(min_reduction_tokens))
        self.min_reduction_ratio = max(0.0, min(1.0, float(min_reduction_ratio)))
        self._tools_provider = tools_provider
        self._trace_recorder = trace_recorder
        self.last_result: dict[str, Any] | None = None
        self.generation = 0
        self._summary_invokes = 0
        self._last_failed_signature: str | None = None
        self.warn_ratio = _clamp_ratio(warn_ratio, 0.75)
        self.trigger_ratio = _clamp_ratio(trigger_ratio, 0.92)
        # Keep-ratio: how much of the window the recent verbatim tail may occupy
        # after a fold. Kept below warn_ratio so a fold lands comfortably under T1.
        self.keep_ratio = _clamp_ratio(keep_ratio, 0.5)
        if self.keep_ratio >= self.warn_ratio:
            self.keep_ratio = self.warn_ratio * 0.6
        self.max_ptl_retries = max(1, int(max_ptl_retries))
        self.min_fold_messages = max(1, int(min_fold_messages))
        # Request-overhead reserves (S1 context hardening): the transcript
        # estimate only sees message contents — the serialized tool schemas
        # riding every request and the tokens the next reply needs are
        # invisible here. Both pad the T1/T2 comparison (see ``before_model``).
        self.schema_reserve = max(0, int(schema_reserve))
        self.output_reserve = max(0, int(output_reserve))
        self.lang = lang
        context_window = getattr(config, "context_window", None)
        model_name_hint = getattr(config, "model_name", None)
        if context_window is None:
            context_window = model_context_profile(model).context_window
        if context_window is None:
            registry = getattr(config, "_provider_registry", None)
            if registry is not None:
                actor_ref = _actual_actor_ref(config)
                if actor_ref:
                    model_name_hint = actor_ref
                    try:
                        resolved = registry.resolve(actor_ref)
                    except Exception:  # noqa: BLE001 - name hint / default
                        resolved = None
                    if resolved is not None:
                        context_window = resolved.model.context_window
        self.window = infer_context_window(model_name_hint, context_window)
        self._warned = False
        # A lazily built memory model (config.memory_model) reused across folds.
        self._memory_model: Any | None = None
        self._memory_model_built = False

    def reset(self) -> None:
        """Clear per-run state (the one-shot T1 warn guard)."""

        self._warned = False
        self.last_result = None
        self.generation = 0
        self._last_failed_signature = None

    # -- event-listener adapter --------------------------------------------
    def on_pre_request(self, messages: list[Any], next: Any) -> Any:
        """Adapter for the ``model/pre_request`` waterfall.

        The waterfall contract is **full message list in, full message list
        out**: no ``RemoveMessage`` ever travels downstream (the pre-request
        bridge alone mints the single legal LangGraph ``REMOVE_ALL`` update).
        ``before_model`` keeps returning the legacy reducer delta for the
        LangChain-middleware path, so this adapter strips the sentinel head and
        forwards the full rebuilt transcript; additive updates (T1 warn) are
        appended to the incoming list instead of replacing it.
        """

        update = self.before_model({"messages": messages}, None)
        if update is None:
            return next(messages)
        if isinstance(update, dict) and "messages" in update:
            update_messages = list(update["messages"])
            if update_messages and isinstance(update_messages[0], RemoveMessage):
                # T2 fold: the sentinel head marks a full transcript
                # replacement — forward the rebuilt transcript as-is.
                return next(update_messages[1:])
            # Additive update (T1 warn): extend the full list, never replace it
            # (a same-turn T2 fold upstream must survive).
            return next(list(messages) + update_messages)
        return next(messages)

    def _tools(self) -> Sequence[Any]:
        return self._tools_provider() if self._tools_provider is not None else ()

    def _preview_context(self, messages: list[Any]) -> list[Any]:
        """Count the current board, not the stale pin that a later listener replaces."""

        block = TaskDocInjector(self.session, lang=self.lang)._render_block(messages)
        if block is None:
            return messages
        return [
            message
            for message in messages
            if not str(getattr(message, "id", "") or "").startswith(TASKDOC_ID_PREFIX)
        ] + [SystemMessage(content=block, id=f"{TASKDOC_ID_PREFIX}preview")]

    def _measure(self, messages: list[Any]):
        profile = model_context_profile(self._main_model, tools=self._tools())
        window = getattr(self.config, "context_window", None) or profile.context_window or self.window
        return check_context_admission(
            self._main_model,
            self._preview_context(messages),
            tools=self._tools(),
            context_window=window,
            schema_reserve=self.schema_reserve,
            output_reserve=self.output_reserve,
        )

    def _record(self, status: str, reason: str, **counts: Any) -> None:
        self.last_result = {
            "status": status, "reason": reason, "generation": self.generation, **counts
        }
        if self._trace_recorder is not None:
            try:
                self._trace_recorder("context_compact", **self.last_result)
            except Exception:  # noqa: BLE001 - diagnostics must not change the request
                pass

    def _budget_exhausted(self) -> bool:
        ledger = getattr(self.session, "usage_ledger", None)
        if ledger is None:
            return False
        return ledger.total >= int(getattr(self.config, "token_budget", 1_000_000))

    def _signature(self, messages: list[Any]) -> str:
        payload = [
            (
                getattr(message, "type", type(message).__name__), message.content,
                getattr(message, "tool_calls", None), getattr(message, "tool_call_id", None),
                getattr(message, "additional_kwargs", None),
            )
            for message in self._preview_context(messages)
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()

    # -- thresholds --------------------------------------------------------
    def before_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        messages = state.get("messages") if isinstance(state, dict) else None
        messages = list(messages or [])
        if not messages:
            return None
        # C1: coarse folding runs after the fine-grained image/OBS-marks pruner.
        # The pruner mutates message content in place so the summariser sees the
        # real textual history without paying for stale screenshots.
        if self._pruner is not None:
            self._pruner.prune(messages)
        measured = self._measure(messages)
        effective = measured.required_tokens
        window = measured.context_window or self.window
        over_work = bool(self.work_target and measured.input_tokens >= self.work_target)

        if effective >= window * self.trigger_ratio or over_work:
            update = self._force_compact(messages)
            if update is not None:
                return update
            # Fold could not run (nothing to fold / summariser failed): fall
            # through so the T1 warn can still fire.

        if not self._warned and (effective >= window * self.warn_ratio or over_work):
            self._warned = True
            return {"messages": [SystemMessage(content=self._warn_text())]}
        return None

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        return self.before_model(state, runtime)

    # -- T2 forced compaction ---------------------------------------------
    def _force_compact(self, messages: list[Any]) -> dict[str, Any] | None:
        """Build the summary + rebuild the transcript; ``None`` if it can't/won't."""

        before = self._measure(messages)
        if self._budget_exhausted():
            self._record("skipped", "token_budget_exhausted", before_tokens=before.input_tokens)
            return None
        signature = self._signature(messages)
        if signature == self._last_failed_signature:
            return None
        self._last_failed_signature = signature
        self.last_result = None
        self._summary_invokes = 0
        head, pinned, conversation, prior_summary = self._partition(messages)
        fixed = self._measure([*head, *pinned, SystemMessage(content=self._fresh_obs_text())])
        physical_target = max(0, int((before.context_window or self.window) * self.warn_ratio) - before.output_reserve)
        target = min(int(self.work_target * self.target_ratio), physical_target) if self.work_target else physical_target
        tail_budget = max(0, target - fixed.input_tokens - self.summary_tokens)
        if not self.work_target:
            tail_budget = min(tail_budget, int(self.window * self.keep_ratio))
        cut = self._choose_cut(conversation, budget=tail_budget)
        ancient = conversation[:cut]
        tail = conversation[cut:]
        if len(ancient) < self.min_fold_messages:
            self._record("skipped", "protected_or_recent_context", before_tokens=before.input_tokens)
            return None  # too little to fold -> skip (avoid a pointless LLM call)

        protected = self._measure([*head, *tail, *pinned, SystemMessage(content=self._fresh_obs_text())])
        if not protected.allowed:
            self._record("skipped", "protected_context_exceeds_capacity", before_tokens=before.input_tokens)
            return None
        summary_limit = min(self.summary_tokens, max(0, target - protected.input_tokens))
        if summary_limit <= 0:
            self._record("skipped", "protected_context_exceeds_work_target", before_tokens=before.input_tokens)
            return None

        summary_text = self._summarise(ancient, prior_summary, summary_limit)
        if not summary_text:
            if self.last_result is None or self.last_result.get("status") != "skipped":
                self._record("skipped", "summary_failed", before_tokens=before.input_tokens)
            return None  # fail-open: summariser failed -> skip the fold this turn
        summary_text += self._memory_state_section()

        summary_msg = SystemMessage(
            content="[COMPACT_SUMMARY]\n" + summary_text, id=_new_compact_id()
        )
        fresh_hint = SystemMessage(content=self._fresh_obs_text())

        rebuilt: list[Any] = [
            *head,
            summary_msg,
            *tail,
            fresh_hint,
            *pinned,
        ]
        after = self._measure(rebuilt)
        reduction = before.input_tokens - after.input_tokens
        minimum = max(self.min_reduction_tokens, int(before.input_tokens * self.min_reduction_ratio))
        if not after.allowed or after.input_tokens > target or reduction < minimum:
            self._record(
                "skipped", "summary_not_useful_or_oversize",
                before_tokens=before.input_tokens, after_tokens=after.input_tokens,
            )
            return None
        self.generation += 1
        self._last_failed_signature = None
        self._record("completed", "compacted", before_tokens=before.input_tokens, after_tokens=after.input_tokens)
        # Legacy reducer delta for the LangChain-middleware path. The
        # ``model/pre_request`` adapter (``on_pre_request``) strips this
        # sentinel head and forwards the full rebuilt list downstream; the
        # pre-request bridge alone mints the LangGraph update.
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]}

    def _partition(
        self, messages: list[Any]
    ) -> tuple[list[Any], list[Any], list[Any], str | None]:
        """Split messages into (head, pinned, conversation, prior_summary_text).

        * ``head``: the leading system prompt(s) (non-pinned SystemMessages at the
          very front) — kept verbatim.
        * ``pinned``: TaskDoc / prior-summary blocks — TaskDoc kept verbatim; the
          prior ``[COMPACT_SUMMARY]`` is pulled out as ``prior_summary`` (fed back
          into the next summary, then superseded).
        * ``conversation``: everything else, in order — the fold candidates.
        """

        head: list[Any] = []
        idx = 0
        while idx < len(messages):
            msg = messages[idx]
            if isinstance(msg, SystemMessage) and not _pinned_id(msg):
                head.append(msg)
                idx += 1
            else:
                break

        pinned: list[Any] = []
        conversation: list[Any] = []
        prior_summary: str | None = None
        for msg in messages[idx:]:
            mid = getattr(msg, "id", None) or ""
            if mid.startswith(COMPACT_ID_PREFIX):
                # Iterative: feed the prior summary text back in, drop the message.
                prior_summary = _strip_memory_state_section(
                    _strip_marker(_text_of(msg))
                )
                continue
            if mid.startswith(TASKDOC_ID_PREFIX):
                pinned.append(msg)
                continue
            if isinstance(msg, HumanMessage) and not conversation and not any(isinstance(m, HumanMessage) for m in head):
                # Keep the original user goal even with TaskDoc disabled. The
                # initial screenshot has already undergone mandatory micro.
                head.append(msg)
                continue
            conversation.append(msg)
        return head, pinned, conversation, prior_summary

    def _choose_cut(self, conversation: list[Any], budget: int | None = None) -> int:
        """Index into ``conversation``: keep the newest tail under the keep budget."""

        if not conversation:
            return 0
        budget = int(self.window * self.keep_ratio) if budget is None else budget
        groups = _group_turns(conversation)
        protected_ids = model_protected_message_ids(self._main_model, conversation)
        first_protected = len(groups) - 1  # Always keep the newest whole unit.
        for index, group in enumerate(groups):
            if not _closed_group(group) or any(
                isinstance(message, HumanMessage)
                or _message_has_image(message)
                or _message_has_obs_marks(message)
                or _has_native_content(message)
                or str(getattr(message, "id", "")) in protected_ids
                for message in group
            ):
                first_protected = min(first_protected, index)
        start = len(groups)
        kept = 0
        for index in range(len(groups) - 1, -1, -1):
            size = estimate_context_tokens(groups[index])
            if kept + size > budget:
                break
            kept += size
            start = index
        start = min(start, first_protected)
        return sum(len(group) for group in groups[:start])

    # -- summariser --------------------------------------------------------
    def _summary_admission(self, model: Any, messages: list[Any]):
        return check_context_admission(
            model,
            messages,
            context_window=(
                getattr(self.config, "context_window", None)
                if model is self._main_model else None
            ),
            schema_reserve=0,
            output_reserve=self.output_reserve,
        )

    def _invoke_summary(self, model: Any, messages: list[Any], limit: int) -> str | None:
        """Retry identical sources, within a per-fold logical invoke ceiling."""

        for _ in range(self.max_ptl_retries):
            if self._budget_exhausted():
                self._record("skipped", "token_budget_exhausted")
                return None
            if self._summary_invokes >= MAX_SUMMARY_INVOKES:
                self._record("skipped", "summary_invoke_limit")
                return None
            if not self._summary_admission(model, messages).allowed:
                self._record("skipped", "summary_input_exceeds_capacity")
                return None
            self._summary_invokes += 1
            try:
                prepared = prepare_model_messages(model, messages)
                if not self._summary_admission(model, prepared.messages).allowed:
                    self._record("skipped", "prepared_summary_exceeds_capacity")
                    return None
                response = model.invoke(prepared.messages, **prepared.model_kwargs)
            except Exception:  # noqa: BLE001 - retry without deleting source history
                continue
            self._record_usage(response, messages, model=model)
            text = _content_text(response).strip()
            if text and not getattr(response, "tool_calls", None):
                if estimate_message_tokens(text) <= limit:
                    return text
                self._record("skipped", "summary_output_exceeds_target")
                return None
        return None

    def _summarise(
        self, ancient: list[Any], prior_summary: str | None, limit: int | None = None
    ) -> str | None:
        """Summarise all sources, optionally in bounded complete-group chunks."""

        model = self._get_memory_model()
        if model is None:
            return None
        limit = self.summary_tokens if limit is None else limit
        request = self._build_summary_messages(ancient, prior_summary, limit)
        if self._summary_admission(model, request).allowed:
            return self._invoke_summary(model, request, limit)

        # Only a measured capacity mismatch causes chunking. A transport error
        # never authorizes dropping the oldest user constraints or tool group.
        chunks: list[list[Any]] = []
        current: list[Any] = []
        for group in _group_turns(ancient):
            candidate = self._build_summary_messages([*current, *group], None, limit)
            if self._summary_admission(model, candidate).allowed:
                current.extend(group)
                continue
            if not current:
                self._record("skipped", "summary_group_exceeds_capacity")
                return None
            chunks.append(current)
            current = list(group)
            if not self._summary_admission(
                model, self._build_summary_messages(current, None, limit)
            ).allowed:
                self._record("skipped", "summary_group_exceeds_capacity")
                return None
        if current:
            chunks.append(current)
        if len(chunks) + 1 > MAX_SUMMARY_INVOKES:
            self._record("skipped", "summary_invoke_limit")
            return None

        partials: list[str] = []
        chunk_limit = max(1, limit // max(1, len(chunks)))
        for chunk in chunks:
            text = self._invoke_summary(
                model, self._build_summary_messages(chunk, None, chunk_limit), chunk_limit
            )
            if not text:
                return None
            partials.append(text)
        merge_source = HumanMessage(
            content="历史分段摘要（覆盖全部原子组，合并时保留错误、约束和结果未知）：\n"
            + "\n\n".join(f"片段 {i + 1}:\n{text}" for i, text in enumerate(partials))
        )
        return self._invoke_summary(
            model, self._build_summary_messages([merge_source], prior_summary, limit), limit
        )

    def _record_usage(self, response: Any, messages: list[Any], *, model: Any = None) -> None:
        """Best-effort accounting for one successful summariser call."""

        ledger = getattr(self.session, "usage_ledger", None)
        if ledger is None:
            return
        try:
            estimate = self._summary_admission(model, messages).input_tokens + estimate_message_tokens(
                response
            )
            ledger.record("compact", response, estimate_tokens=estimate)
        except Exception:  # noqa: BLE001 - accounting must never break compaction
            pass

    def _build_summary_messages(
        self, ancient: list[Any], prior_summary: str | None, limit: int | None = None
    ) -> list[Any]:
        cn = _is_cn(self.lang)
        goal_text, route_text, facts_text = self._taskdoc_sections()
        history = "\n".join(_render_line(m) for m in ancient) or "（无历史）"
        prior = prior_summary or ("（无）" if cn else "(none)")
        human = (
            f"【目标】\n{goal_text}\n\n"
            f"【路线进度】\n{route_text}\n\n"
            f"【关键事实】\n{facts_text}\n\n"
            f"【已有摘要】\n{prior}\n\n"
            f"【待压缩的历史动作与观测】\n{history}\n\n"
            f"{_SUMMARY_SECTIONS['cn' if cn else 'en']}"
        )
        if limit is not None:
            human += f"\n摘要最多约 {limit} tokens；压缩表达，不编造完成或丢失错误结论。"
        return [
            SystemMessage(content=_SUMMARY_SYSTEM["cn" if cn else "en"]),
            HumanMessage(content=human),
        ]

    def _taskdoc_sections(self) -> tuple[str, str, str]:
        """Render the TaskDoc goal / route / facts for the summary prompt."""

        doc = getattr(self.session, "task_doc", None)
        if doc is None:
            return str(getattr(self.session, "run_goal", "") or "（无任务板）"), "（无路线）", "（无事实）"
        goals: list[str] = []
        base = (getattr(doc, "goal_base", "") or "").strip()
        if base:
            goals.append(base)
        for a in getattr(doc, "amendments", []) or []:
            if str(a).strip():
                goals.append(f"补充：{a}")
        route: list[str] = []
        for it in getattr(doc, "items", []) or []:
            status = getattr(it, "status", "")
            line = f"- [{status}] {getattr(it, 'id', '?')}: {getattr(it, 'content', '')}"
            note = (getattr(it, "evidence_note", None) or "").strip()
            reason = (getattr(it, "reason", None) or "").strip()
            if status == "completed" and note:
                line += f"（证据：{note}）"
            elif status == "blocked" and reason:
                line += f"（原因：{reason}）"
            route.append(line)
        facts = [f"- {f}" for f in (getattr(doc, "facts", []) or [])]
        return (
            "\n".join(goals) or "（无目标）",
            "\n".join(route) or "（无路线）",
            "\n".join(facts) or "（无事实）",
        )

    def _memory_state_section(self) -> str:
        """Render the agent-owned memory/capability snapshot deterministically.

        This data never enters the summariser prompt.  If the owning agent cannot
        provide its run-start snapshot, omit the whole section without affecting
        the otherwise successful fold.
        """

        provider = self._memory_state_provider
        if not callable(provider):
            return ""
        try:
            snapshot = provider()
            capabilities = snapshot.get("capabilities")
            if not isinstance(capabilities, dict):
                return ""
            generation = snapshot.get("memory_generation")
            candidate_ids = snapshot.get("shadow_candidate_ids") or []
            lines = [
                "",
                "## 记忆与能力" if _is_cn(self.lang) else "## Memory and capabilities",
                "- 能力状态："
                + _stable_json(capabilities)
                if _is_cn(self.lang)
                else "- Capability states: " + _stable_json(capabilities),
                "- 记忆代际："
                + _stable_json(generation)
                if _is_cn(self.lang)
                else "- Memory generation: " + _stable_json(generation),
            ]
            if candidate_ids:
                lines.append(
                    ("- Shadow recall 候选 ID：" if _is_cn(self.lang) else "- Shadow recall candidate IDs: ")
                    + _stable_json(list(candidate_ids))
                )
            return "\n" + "\n".join(lines)
        except Exception:  # noqa: BLE001 - metadata enrichment is fail-open
            return ""

    def _get_memory_model(self) -> Any | None:
        """Lazily resolve the summariser model (memory_model or the main model)."""

        if self._memory_model_built:
            return self._memory_model
        self._memory_model_built = True
        name = getattr(self.config, "memory_model", None)
        if name:
            try:
                from phone_agent.v2.model import build_role_model

                self._memory_model = build_role_model(self.config, role="memory")
                return self._memory_model
            except Exception:  # noqa: BLE001 - fall back to the injected main model
                self._memory_model = None
        # Fall back to the main model (no tools bound when invoked directly).
        self._memory_model = self._main_model
        return self._memory_model

    # -- text --------------------------------------------------------------
    def _warn_text(self) -> str:
        return _WARN_TEXT.get(self.lang, _WARN_TEXT["cn"])

    def _fresh_obs_text(self) -> str:
        return _FRESH_OBS_TEXT.get(self.lang, _FRESH_OBS_TEXT["cn"])


def _clamp_ratio(value: float, default: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return default
    if ratio <= 0 or ratio > 1:
        return default
    return ratio


def _content_text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


def _strip_marker(text: str) -> str:
    marker = "[COMPACT_SUMMARY]"
    stripped = text.strip()
    if stripped.startswith(marker):
        return stripped[len(marker):].strip()
    return stripped


def _strip_memory_state_section(text: str) -> str:
    """Remove our deterministic suffix before an iterative LLM re-summary."""

    for heading in ("## 记忆与能力", "## Memory and capabilities"):
        marker = "\n" + heading
        if marker in text:
            return text.split(marker, 1)[0].rstrip()
        if text.startswith(heading):
            return ""
    return text


def _stable_json(value: Any) -> str:
    import json

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _new_compact_id() -> str:
    import uuid

    return f"{COMPACT_ID_PREFIX}{uuid.uuid4().hex}"


def build_compact_middleware(
    session: Any,
    config: Any,
    *,
    model: Any | None = None,
    memory_state_provider: Any | None = None,
    pruner: ContextPrunerService | None = None,
    tools_provider: Callable[[], Sequence[Any]] | None = None,
    trace_recorder: Callable[..., Any] | None = None,
) -> CompactMiddleware:
    """Build a :class:`CompactMiddleware` from resolved config values."""

    return CompactMiddleware(
        session,
        config,
        model=model,
        memory_state_provider=memory_state_provider,
        pruner=pruner,
        warn_ratio=getattr(config, "compact_warn_ratio", 0.75),
        trigger_ratio=getattr(config, "compact_trigger_ratio", 0.92),
        schema_reserve=getattr(config, "compact_schema_reserve", 3000),
        output_reserve=getattr(config, "compact_output_reserve", 2000),
        lang=getattr(config, "lang", "cn"),
        work_target=getattr(config, "context_work_target", 32_000),
        target_ratio=getattr(config, "compact_target_ratio", 0.7),
        summary_tokens=getattr(config, "compact_summary_tokens", 2000),
        min_reduction_tokens=getattr(config, "compact_min_reduction_tokens", 1000),
        min_reduction_ratio=getattr(config, "compact_min_reduction_ratio", 0.1),
        tools_provider=tools_provider,
        trace_recorder=trace_recorder,
    )


__all__ = [
    "CompactMiddleware",
    "build_compact_middleware",
    "infer_context_window",
]
