"""Shadow-first safety reviewer backed by System One (``tool/execute``).

What it is
----------

A second opinion on the calls the built-in safety classifier lets through.  The
reviewer assembles a small, redacted state string (tool name + target text) and
asks System One **one ``noul`` question** ("is this action risky or
irreversible?") plus **one ``score`` question** (descriptive risk levels, for
audit) in a single call — the API answers both in parallel.

The decision signal is the ``noul`` answer's probability ``p``: a ``noul``
answer *is* a calibrated probability, so the confidence gate reads it directly
(no secondary number to reconcile).  The ``score`` answer's ``confidence`` and
raw ``score`` are recorded in the trace for audit; the numeric scale behind
``score`` is the model's own (its ``legend`` is not interpreted here).

Modes (declared setting ``PHONE_AGENT_SYSTEMONE_REVIEW``)
---------------------------------------------------------

``off``
    Nothing is registered; the plugin's review seam does not exist.
``shadow`` (default)
    Every eligible call is reviewed and the verdict is written to the run trace;
    the call itself is untouched.  This is how the review is calibrated before
    anyone trusts it.
``on``
    Adds a **veto** — and only under three conditions that must all hold: the
    built-in safety classifier would have *allowed* the call, the ``noul``
    answer is positive, and its calibrated ``p`` is at least the declared threshold
    (``PHONE_AGENT_SYSTEMONE_CONFIDENCE``, default 0.9).  A veto returns the same
    "⚠️ 已拦截（未执行）" warning shape the built-in warning flow uses, so the
    model clears it the same way: resend with ``confirm_irreversible=true``.

Never loosens an existing gate
------------------------------

The veto is **purely additive**, and the implementation is arranged so it
cannot become anything else:

* a call the built-in classifier would already gate is never "re-allowed" —
  the reviewer records the fact and delegates, leaving the decision where it
  was;
* a call that already carries ``confirm_irreversible=true`` (the model's reply to
  a warning) is passed through untouched, exactly like the built-in warning
  listener, so a veto cannot trap the run in a loop;
* anything that goes wrong on this path — endpoint down, timeout, malformed
  answer, missing ``p`` — degrades to *no veto* and a trace line.  An additive
  gate must never turn an outage into a blocked run; the built-in gate still
  applies.

Privacy and accounting
----------------------

The state that leaves the machine is redacted with the built-in prompt-side
pattern and bounded; the tool's raw arguments are never sent.  Every review
records the tokens the response reports under the declared ``systemone`` role
(``unit="tokens"``: the API always returns ``usage``, so the accounting is
per-token rather than per-call — and it is the same number the budget sees).
The trace event itself is written through ``session.resolution_trace_recorder``
(P0 #6: the trace writer truncates and redacts, and the API key is registered as
a redaction literal by the plugin).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.messages import ToolMessage

from phone_agent.config.redact import redact_context_text
from phone_agent.v2.middleware.safety import (
    LEGACY_ACTUATION_GATED_TOOLS,
    classify_tool_call,
)

REVIEW_EVENT = "systemone_review"
#: The review modes the declared setting admits.
REVIEW_MODES: tuple[str, ...] = ("off", "shadow", "on")
DEFAULT_CONFIDENCE = 0.9
#: Bound on the state sent to the model (protocol has no schema for it; a short
#: state keeps the call inside the timeout band and the privacy surface small).
MAX_TARGET_CHARS = 120
MAX_STATE_CHARS = 400

_TARGET_KEYS = ("target_description", "text", "app_name")
_WARNING_OPTIONS = (
    "选项：\n"
    "  1) 确认执行：带 confirm_irreversible=true 重新调用同一工具（其余参数不变）。\n"
    "  2) 放弃：改做其它操作或重新观测。\n"
    "  3) 交人工：调用 ask_user 询问，或 take_over 请求人工接管。"
)


def extract_call(request: Any) -> tuple[str, dict[str, Any]]:
    """``(tool_name, args)`` from a waterfall request (langchain or test double)."""

    tool_call = getattr(request, "tool_call", None)
    if tool_call is None and isinstance(request, dict):
        tool_call = request.get("tool_call", request)
    if tool_call is None:
        tool_call = request
    if isinstance(tool_call, dict):
        name = tool_call.get("name", "")
        args = tool_call.get("args", {}) or {}
    else:
        name = getattr(tool_call, "name", "") or ""
        args = getattr(tool_call, "args", {}) or {}
    return str(name), args if isinstance(args, dict) else {}


def is_actuation(name: str, risks: Mapping[str, str] | None) -> bool:
    """Same fail-closed rule the safety classifier uses (undeclared == actuation)."""

    if risks is None:
        return name in LEGACY_ACTUATION_GATED_TOOLS
    return str(risks.get(name, "actuation")).strip().lower() != "readonly"


def confirmed_irreversible(args: Mapping[str, Any]) -> bool:
    """True when the model re-sent the call after a warning (U2 §1)."""

    return bool(args.get("confirm_irreversible"))


def target_text(name: str, args: Mapping[str, Any], session: Any) -> str:
    """Redacted, bounded target description for the review state."""

    parts: list[str] = []
    for key in _TARGET_KEYS:
        value = args.get(key)
        if value:
            parts.append(str(value))
    mark_id = args.get("target_mark_id")
    if mark_id:
        marks = getattr(session, "marks", None)
        mark = marks.get(mark_id) if isinstance(marks, dict) else None
        summary = getattr(mark, "text_summary", None) if mark is not None else None
        if summary:
            parts.append(str(summary))
    raw = " ".join(parts).strip()
    return redact_context_text(raw)[:MAX_TARGET_CHARS]


def build_state(name: str, args: Mapping[str, Any], session: Any) -> str:
    """The state string sent to System One: tool identity + redacted target."""

    lines = [f"tool: {name}"]
    target = target_text(name, args, session)
    if target:
        lines.append(f"target: {target}")
    return "\n".join(lines)[:MAX_STATE_CHARS]


#: Descriptive risk levels for the audit-side ``score`` question.  The API's
#: score question carries an ordered criteria list (no min/max), and the numeric
#: scale behind the returned ``score`` is the model's own — the plugin reads the
#: ``noul`` probability for the decision and keeps the score for the trace.
RISK_LEVELS: tuple[str, ...] = (
    "no meaningful risk: reading or ordinary navigation",
    "low risk: reversible with one obvious undo",
    "moderate risk: reversible but needs deliberate recovery",
    "high risk: hard or impossible to undo",
    "critical risk: money moves, credentials are entered, or data is lost",
)
RISK_NOUL_INSTRUCTIONS = (
    "Is this action risky or irreversible (money transfer, payment, deletion, "
    "credential entry, data loss)?"
)
RISK_SCORE_INSTRUCTIONS = (
    "How risky is this action overall? Pick the level whose description matches."
)


def _is_positive(value: Any) -> bool:
    """Read a ``noul`` value as a decision (the client returns a bool)."""

    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "y"}


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_warning(name: str, target: str, *, p: float, score: float | None) -> str:
    """The veto message: same world-fact + option-space shape as the built-in warning.

    Only the calibrated ``p`` is stated as a probability; the score is reported
    without a scale claim because the API defines none (its ``legend`` is the
    model's own explanation).
    """

    head = f"⚠️ 已拦截（未执行）：{name}"
    if target:
        head += f" → 「{target}」"
    confidence = f"p={p:.2f}"
    if score is not None:
        confidence += f"，风险评分 {score:g}"
    return (
        f"{head}\n"
        f"世界事实：System One 复核判定该动作有风险（{confidence}）。\n"
        f"{_WARNING_OPTIONS}"
    )


class SystemOneSafetyReviewer:
    """``tool/execute`` onion listener: review, then veto or delegate."""

    def __init__(
        self,
        session: Any,
        config: Any,
        *,
        client: Any,
        record: Callable[..., None],
        tokens_of: Callable[[Any], int | None],
        mode: str = "shadow",
        confidence: float = DEFAULT_CONFIDENCE,
        risks: Mapping[str, str] | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.client = client
        # ``usage_tokens`` from the protocol client: the ONE definition of how a
        # usage block becomes a number.  Injected because the plugin's siblings
        # are loaded as flat modules (no package context for a relative import).
        self._tokens_of = tokens_of
        self.mode = str(mode).strip().lower()
        self.confidence = float(confidence)
        self.risks = risks
        self._record = record
        self.review_count = 0
        self.veto_count = 0

    # -- helpers -----------------------------------------------------------
    def _trace(self, **payload: Any) -> None:
        try:
            self._record(REVIEW_EVENT, **payload)
        except Exception:  # noqa: BLE001 - tracing cannot change a decision
            pass

    def _record_usage(self, reply: Any) -> int | None:
        """Book the backend-reported tokens under the ``systemone`` role.

        The API always returns ``usage``, so this is a per-token account (the
        same number the token budget adjudicates) rather than a call count.  A
        reply without a usable number books nothing instead of inventing one.
        """

        usage = getattr(reply, "usage", None)
        tokens = self._tokens_of(usage) if isinstance(usage, Mapping) else None
        if tokens is None or tokens <= 0:
            return None
        ledger = getattr(self.session, "usage_ledger", None)
        if ledger is None:
            return tokens
        try:
            ledger.record("systemone", estimate_tokens=int(tokens))
        except Exception:  # noqa: BLE001 - accounting must not change safety
            pass
        return tokens

    def _review(
        self, name: str, args: Mapping[str, Any]
    ) -> tuple[Any, bool, float | None, float | None, str]:
        """Ask both questions in ONE call; returns ``(reply, risky, p, score, target)``."""

        state = build_state(name, args, self.session)
        target = target_text(name, args, self.session)
        questions = {
            "risky": {
                "type": "noul",
                "instructions": RISK_NOUL_INSTRUCTIONS,
            },
            "risk": {
                "type": "score",
                "instructions": RISK_SCORE_INSTRUCTIONS,
                "criteria": list(RISK_LEVELS),
            },
        }
        reply = self.client.ask(state, questions)
        return (
            reply,
            _is_positive(reply.answer("risky").value),
            _numeric(reply.answer("risky").p),
            _numeric(reply.answer("risk").value),
            target,
        )

    # -- listener ----------------------------------------------------------
    def __call__(self, request: Any, next: Callable[[Any], Any]) -> Any:
        name, args = extract_call(request)
        if not is_actuation(name, self.risks):
            return next(request)
        if confirmed_irreversible(args):
            self._trace(
                tool=name, mode=self.mode, action="confirmed", verdict="skipped"
            )
            return next(request)
        existing = classify_tool_call(
            request, self.session, self.config, risks=self.risks
        )
        if existing.should_gate:
            self._trace(
                tool=name,
                mode=self.mode,
                action="existing_gate",
                verdict="skipped",
                reason=existing.reason,
            )
            return next(request)

        self.review_count += 1
        started = time.perf_counter()
        try:
            reply, risky, probability, score, target = self._review(name, args)
        except Exception as exc:  # noqa: BLE001 - additive gate never blocks a run
            self._trace(
                tool=name,
                mode=self.mode,
                action="error",
                error=type(exc).__name__,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        tokens = self._record_usage(reply)

        veto = (
            self.mode == "on"
            and risky
            and probability is not None
            and probability >= self.confidence
        )
        self._trace(
            tool=name,
            mode=self.mode,
            action="veto" if veto else "shadow" if self.mode == "shadow" else "allow",
            verdict="risky" if risky else "safe",
            p=probability,
            score=score,
            tokens=tokens,
            target=target,
            threshold=self.confidence,
            latency_ms=latency_ms,
        )
        if not veto:
            return next(request)

        self.veto_count += 1
        tool_call = getattr(request, "tool_call", {}) or {}
        call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        return ToolMessage(
            content=build_warning(name, target, p=probability, score=score),
            tool_call_id=str(call_id or ""),
            status="error",
            name=name,
        )


__all__ = [
    "DEFAULT_CONFIDENCE",
    "MAX_STATE_CHARS",
    "MAX_TARGET_CHARS",
    "REVIEW_EVENT",
    "REVIEW_MODES",
    "RISK_LEVELS",
    "RISK_NOUL_INSTRUCTIONS",
    "RISK_SCORE_INSTRUCTIONS",
    "SystemOneSafetyReviewer",
    "build_state",
    "build_warning",
    "confirmed_irreversible",
    "extract_call",
    "is_actuation",
    "target_text",
]
