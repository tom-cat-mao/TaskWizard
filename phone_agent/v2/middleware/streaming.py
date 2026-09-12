"""Observe-only incremental model text for the web console (WP-STREAM).

The console may show the actor model's answer as it is produced.  Streaming at
the transport level is enabled by the provider builders from the effective
global/model/role decision; this module only *listens*: a v1 streaming callback
handler is attached to the model instance the caller passes, the SDK fires
``on_llm_new_token`` per chunk and still aggregates the chunks into the same
complete ``AIMessage`` (text, tool calls, usage).  Nothing downstream changes:
tool calls execute only from the aggregated message, the action sequence and
safety policy are untouched, and a failed attempt is never replayed.

Contracts kept here:

* observe-only: every emission is best-effort and never raises into the model
  call; the original model instance is copied, never mutated.
* attempt isolation: each LLM run gets its own ``attempt`` id, deltas are
  flushed before the terminal event, and a failed attempt keeps its partial
  text plus an explicit ``ok: false`` end.
* only displayable SDK text: string text blocks and provider-reported reasoning
  (``thinking`` blocks, ``reasoning_content``, Responses ``summary`` items).
  No reasoning is fabricated, and image/base64 blocks plus tool-argument
  deltas are dropped by construction.
* cross-chunk privacy: sensitive-looking spans are never split across an
  emission.  Held text is settled only up to the start of a still-open
  token-like region (keyword/email/phone/base64-run signatures), so an
  unclosed secret can never leave the process in pieces; the held region is
  bounded (a very long run is replaced by ``<redacted>``).
* bounded output: deltas coalesce and every emitted piece is redacted through
  the shared egress primitive and length-capped.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

from phone_agent.config.redact import SENSITIVE_PATTERN
from phone_agent.v2.middleware._redact import redact_text

STREAM_FLUSH_CHARS = 240
STREAM_FLUSH_SECONDS = 0.15
STREAM_TEXT_LIMIT = 4000
STREAM_REASONING_LIMIT = 2000
STREAM_SETTLE_HOLDBACK = 32
STREAM_OPEN_RUN_MAX = 160
STREAM_REDACTED = "<redacted>"

_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_REASONING_BLOCK_TYPES = frozenset({"thinking", "reasoning", "reasoning_content"})
_REASONING_KEYS = ("reasoning_content", "reasoning")

_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_.\-:@]+$")
_RUN_CONTINUATION = re.compile(r"[A-Za-z0-9+/=_.\-:@]+")
_OPEN_SIGNATURES = (
    re.compile(r"[\w.+\-]+@[\w.\-]*$"),
    re.compile(
        r"(?:pass(?:w(?:o(?:r(?:d)?)?)?)?|bear(?:e(?:r)?)?|sk-|eyJ|api[_-]?k(?:e(?:y)?)?|"
        r"tok(?:e(?:n)?)?|secr(?:e(?:t)?)?|orde(?:r)?|cod(?:e)?|订单|验证(?:码)?)"
        r"[\s:#=：\-]*[A-Za-z0-9+/=_.\-:]*$",
        re.IGNORECASE,
    ),
)


def _summary_text(block: dict) -> str:
    """Provider-reported reasoning summary (Responses ``summary_text`` items)."""

    parts: list[str] = []
    summary = block.get("summary")
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") not in (None, "summary_text"):
                continue
            item_text = item.get("text")
            if isinstance(item_text, str) and item_text:
                parts.append(item_text)
    return "".join(parts)


def _block_text(block: Any) -> tuple[str, str]:
    """``(text, reasoning)`` carried by one content block (else empty)."""

    if not isinstance(block, dict):
        return "", ""
    kind = block.get("type")
    if kind in _TEXT_BLOCK_TYPES:
        value = block.get("text")
        return (value if isinstance(value, str) else ""), ""
    if kind in _REASONING_BLOCK_TYPES:
        parts = [_summary_text(block)]
        for key in ("thinking", "reasoning", "text"):
            value = block.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        return "", "".join(parts)
    return "", ""


def extract_display_text(chunk: Any) -> tuple[str, str]:
    """Extract displayable ``(text, reasoning)`` from one streamed chunk.

    Only SDK-provided string content is surfaced.  Any other block type (images
    with base64 payloads, tool-call argument deltas, encrypted reasoning
    content, signatures, ...) contributes nothing.
    """

    message = getattr(chunk, "message", None)
    if message is None:
        message = chunk
    content = getattr(message, "content", None)
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            text, reasoning = _block_text(block)
            if text:
                text_parts.append(text)
            if reasoning:
                reasoning_parts.append(reasoning)
    extra = getattr(message, "additional_kwargs", None)
    if isinstance(extra, dict):
        for key in _REASONING_KEYS:
            value = extra.get(key)
            if isinstance(value, str) and value:
                reasoning_parts.append(value)
    return "".join(text_parts), "".join(reasoning_parts)


def _open_start(text: str) -> int | None:
    """Start index of a trailing region that may still be an open secret, if any."""

    start: int | None = None
    match = _TOKEN_RUN.search(text)
    if match:
        start = match.start()
    for pattern in _OPEN_SIGNATURES:
        match = pattern.search(text)
        if match and (start is None or match.start() < start):
            start = match.start()
    return start


def _settle(buffer: str, *, long_run: bool, final: bool) -> tuple[str, str, bool]:
    """Split held text into a redacted prefix and a still-open suffix.

    ``final`` releases everything (the stream ended, nothing can join later).
    Otherwise the suffix from an open region is kept so a secret that spans
    chunks and flush boundaries is redacted as a whole; an open region longer
    than :data:`STREAM_OPEN_RUN_MAX` is replaced by one ``<redacted>`` marker
    so the hold stays bounded, and the remainder of that run is dropped rather
    than shown in pieces.
    """

    if long_run:
        continuation = _RUN_CONTINUATION.match(buffer)
        end = continuation.end() if continuation else 0
        if end == len(buffer):
            return ("", "", False) if final else ("", buffer[-STREAM_SETTLE_HOLDBACK:], True)
        buffer = buffer[end:]
    if final:
        return redact_text(buffer), "", False
    start = _open_start(buffer)
    cut = start if start is not None else max(0, len(buffer) - STREAM_SETTLE_HOLDBACK)
    for match in SENSITIVE_PATTERN.finditer(buffer):
        if match.start() < cut < match.end():
            cut = match.start()
            break
    prefix, held = redact_text(buffer[:cut]), buffer[cut:]
    if start is not None and len(held) > STREAM_OPEN_RUN_MAX:
        return prefix + STREAM_REDACTED, held[-STREAM_SETTLE_HOLDBACK:], True
    return prefix, held, False


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class _RunState:
    """Per-LLM-run buffers; ``attempt`` is allocated on first content."""

    __slots__ = (
        "attempt",
        "pending_text",
        "pending_reasoning",
        "pending_chars",
        "last_flush",
        "started",
        "long_run_text",
        "long_run_reasoning",
    )

    def __init__(self) -> None:
        self.attempt: int | None = None
        self.pending_text = ""
        self.pending_reasoning = ""
        self.pending_chars = 0
        self.last_flush = 0.0
        self.started = False
        self.long_run_text = False
        self.long_run_reasoning = False


class ModelStreamObserver(BaseCallbackHandler):
    """v1 streaming callback handler that projects deltas through ``emit``.

    ``emit(name, payload)`` receives ``model_stream_start`` /
    ``model_stream_delta`` / ``model_stream_end`` payloads (the owning
    middleware adds the ``step`` and the event envelope).  ``next_attempt``
    allocates the per-attempt identity.  Callback failures are swallowed;
    ``run_inline`` keeps delivery synchronous so a delta can never surface
    after the tool call that the same message triggered.
    """

    run_inline = True

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
        next_attempt: Callable[[], int],
        *,
        flush_chars: int = STREAM_FLUSH_CHARS,
        flush_seconds: float = STREAM_FLUSH_SECONDS,
    ) -> None:
        super().__init__()
        self._emit = emit
        self._next_attempt = next_attempt
        self._flush_chars = max(1, int(flush_chars))
        self._flush_seconds = max(0.0, float(flush_seconds))
        self._runs: dict[Any, _RunState] = {}
        self._lock = threading.Lock()

    def tap_output_iter(self, run_id: Any, output: Any) -> Any:
        """Passthrough: the handler consumes v1 tokens, not v2 stream events."""

        return output

    def tap_output_aiter(self, run_id: Any, output: Any) -> Any:
        """Passthrough: the handler consumes v1 tokens, not v2 stream events."""

        return output

    def on_llm_new_token(
        self, token: str, *, chunk: Any = None, run_id: Any = None, **kwargs: Any
    ) -> None:
        try:
            source = chunk if chunk is not None else token
            text, reasoning = extract_display_text(source)
            if not text and not reasoning:
                return
            with self._lock:
                state = self._runs.setdefault(run_id, _RunState())
                if not state.started:
                    state.started = True
                    state.attempt = self._next_attempt()
                    self._emit("model_stream_start", {"attempt": state.attempt})
                state.pending_text += text
                state.pending_reasoning += reasoning
                state.pending_chars += len(text) + len(reasoning)
                now = time.monotonic()
                due = (
                    state.pending_chars >= self._flush_chars
                    or now - state.last_flush >= self._flush_seconds
                )
            if due:
                self._flush(run_id, final=False)
        except Exception:  # noqa: BLE001 - observation must never break the call
            return

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._finish(run_id, ok=True, error=None)

    def on_llm_error(
        self, error: BaseException, *, run_id: Any = None, **kwargs: Any
    ) -> None:
        self._finish(run_id, ok=False, error=type(error).__name__)

    def _flush(self, run_id: Any, *, final: bool) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or not state.started:
                return
            emit_text, state.pending_text, state.long_run_text = _settle(
                state.pending_text, long_run=state.long_run_text, final=final
            )
            emit_reasoning, state.pending_reasoning, state.long_run_reasoning = _settle(
                state.pending_reasoning,
                long_run=state.long_run_reasoning,
                final=final,
            )
            state.pending_chars = len(state.pending_text) + len(state.pending_reasoning)
            state.last_flush = time.monotonic()
            attempt = state.attempt
        if not emit_text and not emit_reasoning:
            return
        payload: dict[str, Any] = {"attempt": attempt}
        if emit_text:
            payload["text"] = _bounded(emit_text, STREAM_TEXT_LIMIT)
        if emit_reasoning:
            payload["reasoning"] = _bounded(emit_reasoning, STREAM_REASONING_LIMIT)
        self._emit("model_stream_delta", payload)

    def _finish(self, run_id: Any, *, ok: bool, error: str | None) -> None:
        try:
            self._flush(run_id, final=True)
            with self._lock:
                state = self._runs.pop(run_id, None)
                started = bool(state and state.started)
                attempt = state.attempt if state else None
            if not started:
                return
            payload: dict[str, Any] = {"attempt": attempt, "ok": bool(ok)}
            if error:
                payload["error"] = redact_text(str(error))
            self._emit("model_stream_end", payload)
        except Exception:  # noqa: BLE001 - observation must never break the call
            return


def model_with_stream_observer(model: Any, observer: ModelStreamObserver) -> Any | None:
    """Return a copy of ``model`` whose callbacks include ``observer``.

    ``None`` when the transport cannot carry callbacks: the caller then
    performs the plain call so instrumentation is always optional.  The copy
    shares the underlying client and configuration; the original instance is
    never mutated.
    """

    try:
        existing = list(getattr(model, "callbacks", None) or [])
    except Exception:  # noqa: BLE001 - an unusual transport simply stays plain
        return None
    try:
        return model.model_copy(update={"callbacks": [*existing, observer]})
    except Exception:  # noqa: BLE001 - a copy failure means plain call
        return None


__all__ = [
    "ModelStreamObserver",
    "STREAM_FLUSH_CHARS",
    "STREAM_FLUSH_SECONDS",
    "STREAM_OPEN_RUN_MAX",
    "STREAM_REASONING_LIMIT",
    "STREAM_SETTLE_HOLDBACK",
    "STREAM_TEXT_LIMIT",
    "extract_display_text",
    "model_with_stream_observer",
]
