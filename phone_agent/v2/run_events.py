"""Reusable web-console event production for in-process agent runs.

The event dictionaries in this module are the IPC contract between the runner
process and :mod:`phone_agent.web.bridge`.  Sinks only need a ``put(event)``
method, which keeps the middleware usable with both ``queue.Queue`` in tests
and the flushed JSONL writer used by the runner.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config

from phone_agent.v2.agent import RunResult
from phone_agent.v2.middleware._redact import redact_text
from phone_agent.v2.middleware._tokens import estimate_message_tokens, usage_tokens
from phone_agent.v2.middleware.streaming import (
    ModelStreamObserver,
    model_with_stream_observer,
)
from phone_agent.v2.middleware.trace import redact_args
from phone_agent.v2.usage import usage_details

OBS_RE = re.compile(r"\[OBS\]\s+app=(?P<app>.*?)\s+screen#(?P<seq>\d+)")
_SAFETY_MARKERS = ("⚠️ 已拦截（未执行）", "confirm_irreversible=true")


class EventSink(Protocol):
    """Minimal sink shared by queues and append-only event files."""

    def put(self, event: dict[str, Any]) -> None: ...


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
    ).strip()


def _image_url(block: Any) -> str:
    if not isinstance(block, dict) or block.get("type") not in {
        "image",
        "image_url",
    }:
        return ""
    payload = block.get("image_url", block.get("source", ""))
    if isinstance(payload, dict):
        return str(payload.get("url", payload.get("data", "")) or "")
    return str(payload or "")


def _response_message(response: Any) -> Any | None:
    result = getattr(response, "result", None)
    messages = result if isinstance(result, list) else [response]
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai" or getattr(
            message, "tool_calls", None
        ):
            return message
    return messages[-1] if messages and messages[-1] is not None else None


def _requested_model(request: Any) -> str | None:
    """Best-effort configured/bound model label from the outgoing request.

    This is provenance from the real request object (never fabricated): the
    bound chat model exposes ``model_name`` (OpenAI-style) or ``model``. When a
    test double or an unusual transport exposes neither, the label stays absent
    rather than being guessed.
    """

    model = getattr(request, "model", None)
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _actual_model(message: Any) -> str | None:
    """Model label actually reported by the provider, from response metadata.

    Only a provider-reported value is surfaced; the console labels this the
    *actual* model. Absence means the provider did not report one — the caller
    must not substitute the requested label for it.
    """

    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    for key in ("model_name", "model"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_result_text(content: Any, limit: int = 1200) -> str:
    text = redact_text(_message_text(content)).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _taskdoc_from_messages(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        text = _message_text(message)
        marker = "[TASK_DOC]"
        if marker in text:
            return text[text.index(marker) + len(marker) :].strip()
    return None


class WebEventMiddleware(AgentMiddleware):
    """Publish the established compact event stream to a pluggable sink.

    ``streaming`` (the actor's resolved decision) additionally projects
    incremental model text as ``model_stream_start`` / ``model_stream_delta`` /
    ``model_stream_end`` events.  The instrumentation is observe-only: the
    transport streams on its own because the providers were built with the
    resolved decision, and this middleware merely attaches a listener to a copy
    of the request's model.  Models listed in ``inactive_models`` (an
    availability fallback whose own model/global configuration keeps streaming
    off) are never instrumented, so tool calls, usage accounting, retries and
    safety policy are unchanged.
    """

    def __init__(self, events: EventSink, *, streaming: bool = False) -> None:
        super().__init__()
        self.events = events
        self._step = 0
        self._tokens = 0
        self._last_taskdoc: str | None = None
        self._last_screen_key: tuple[str, Any] | None = None
        self._session: Any | None = None
        self._stop_requested = threading.Event()
        self._streaming_enabled = bool(streaming)
        self._streaming_inactive: tuple[Any, ...] = ()
        self._stream_attempts = 0
        self._stream_lock = threading.Lock()

    def set_streaming(
        self, enabled: bool, *, inactive_models: tuple[Any, ...] = ()
    ) -> None:
        """Attach the run's streaming decision and the models it must not touch.

        ``inactive_models`` carries provider-built models whose own decision is
        off (an availability fallback built from its own model/global
        configuration); the observer is never attached to them.
        """

        self._streaming_enabled = bool(enabled)
        self._streaming_inactive = tuple(inactive_models)

    @property
    def streaming(self) -> bool:
        return self._streaming_enabled

    def _next_stream_attempt(self) -> int:
        with self._stream_lock:
            self._stream_attempts += 1
            return self._stream_attempts

    def _on_stream_event(self, name: str, payload: dict[str, Any]) -> None:
        """Project one observer payload (adds the step and a strict envelope)."""

        event: dict[str, Any] = {"event": name, "step": self._step + 1}
        for key in ("attempt", "text", "reasoning", "ok", "error"):
            if key in payload:
                event[key] = payload[key]
        self._emit(event)

    def _streamed_request(self, request: Any) -> Any:
        """Wrap this model call for incremental observation when enabled."""

        model = getattr(request, "model", None)
        enabled = bool(getattr(model, "streaming", self._streaming_enabled))
        if model is None or not enabled:
            return request
        if any(request.model is model for model in self._streaming_inactive):
            return request
        observer = ModelStreamObserver(
            self._on_stream_event, self._next_stream_attempt
        )
        instrumented = model_with_stream_observer(request.model, observer)
        if instrumented is None:
            return request
        return request.override(model=instrumented)

    def attach_session(self, session: Any) -> None:
        self._session = session

    def request_stop(self) -> None:
        self._stop_requested.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def step(self) -> int:
        return self._step

    @property
    def tokens(self) -> int:
        return self._tokens

    def emit(self, event: dict[str, Any]) -> None:
        """Publish a runner-owned lifecycle event through the same sink."""

        self._emit(event)

    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        self.events.put(event)

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        try:
            if self.stop_requested:
                if self._session is not None:
                    try:
                        self._session.takeover_reason = "用户从 Web 控制台停止"
                    except Exception:  # noqa: BLE001
                        pass
                self._emit({"event": "stopping", "step": self._step})
                return {"jump_to": "end"}
            messages = state.get("messages", []) if isinstance(state, dict) else []
            taskdoc = _taskdoc_from_messages(messages or [])
            if taskdoc is not None and taskdoc != self._last_taskdoc:
                self._last_taskdoc = taskdoc
                self._emit(
                    {
                        "event": "taskdoc_snapshot",
                        "step": self._step + 1,
                        "text": taskdoc,
                    }
                )
            for message in reversed(messages or []):
                if self._emit_screen_from_content(getattr(message, "content", None)):
                    break
        except Exception:  # noqa: BLE001
            pass
        return None

    async def abefore_model(
        self, state, runtime
    ) -> dict[str, Any] | None:  # noqa: ANN001
        return self.before_model(state, runtime)

    def _record_model(
        self,
        response: Any,
        latency_ms: int,
        error: str | None,
        *,
        requested_model: str | None = None,
    ) -> None:
        self._step += 1
        message = _response_message(response) if response is not None else None
        turn_tokens = 0
        if message is not None:
            reported_tokens = usage_tokens(message)
            turn_tokens = (
                reported_tokens
                if reported_tokens is not None
                else estimate_message_tokens(message)
            )
        self._tokens += turn_tokens
        event: dict[str, Any] = {
            "event": "model_call",
            "step": self._step,
            "latency_ms": latency_ms,
            "tokens": turn_tokens,
            "tokens_total": self._tokens,
            "error": redact_text(error) if error else None,
            **usage_details(message),
        }
        # Observe-only model-identity fields (safe labels, never credentials).
        # ``requested_model`` is the bound model reference; ``actual_model`` is
        # only ever the provider-reported label. Absent stays absent — the UI
        # renders "未上报" instead of inheriting the requested label.
        if requested_model:
            event["requested_model"] = requested_model
        actual_model = _actual_model(message) if message is not None else None
        if actual_model:
            event["actual_model"] = actual_model
        self._emit(event)

    def wrap_model_call(self, request, handler):  # noqa: ANN001
        requested_model = _requested_model(request)
        self._emit(
            {
                "event": "model_request",
                "step": self._step + 1,
                "phase": "start",
                "requested_model": requested_model,
            }
        )
        request = self._streamed_request(request)
        started = time.perf_counter()
        try:
            response = handler(request)
        except Exception as exc:  # noqa: BLE001
            self._record_model(
                None,
                int((time.perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}",
                requested_model=requested_model,
            )
            raise
        self._record_model(
            response,
            int((time.perf_counter() - started) * 1000),
            None,
            requested_model=requested_model,
        )
        return response

    async def awrap_model_call(self, request, handler):  # noqa: ANN001
        requested_model = _requested_model(request)
        self._emit(
            {
                "event": "model_request",
                "step": self._step + 1,
                "phase": "start",
                "requested_model": requested_model,
            }
        )
        request = self._streamed_request(request)
        started = time.perf_counter()
        try:
            response = await handler(request)
        except Exception as exc:  # noqa: BLE001
            self._record_model(
                None,
                int((time.perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}",
                requested_model=requested_model,
            )
            raise
        self._record_model(
            response,
            int((time.perf_counter() - started) * 1000),
            None,
            requested_model=requested_model,
        )
        return response

    def _record_tool_result(
        self, name: str, result: Any, latency_ms: int, error: str | None
    ) -> None:
        content = getattr(result, "content", None) if result is not None else None
        text = _safe_result_text(content) if content is not None else ""
        from phone_agent.v2.experience import classify_tool_result

        result_class = classify_tool_result(
            result, RuntimeError(error) if error is not None else None
        )
        ok = result_class == "ok"
        self._emit(
            {
                "event": "tool_result",
                "step": self._step,
                "tool": name,
                "text": text,
                "ok": ok,
                "latency_ms": latency_ms,
                "error": redact_text(error) if error else None,
            }
        )
        if text and any(marker in text for marker in _SAFETY_MARKERS):
            self._emit(
                {
                    "event": "safety_warning",
                    "step": self._step,
                    "tool": name,
                    "text": text,
                }
            )
        self._emit_screen_from_content(content)

    def _emit_screen_from_content(self, content: Any) -> bool:
        if not isinstance(content, list):
            return False
        text = _message_text(content)
        obs = OBS_RE.search(text)
        current_app = obs.group("app") if obs else None
        parsed_seq = int(obs.group("seq")) if obs else None
        for block in reversed(content):
            url = _image_url(block)
            if not url:
                continue
            # An unverified reference frame (a failed observation still showing
            # its last valid capture) commits no screen_seq/epoch and carries
            # ``reference``/``screen_ref`` instead. It has its own identity so a
            # run's many seq=None reference frames never collapse into one key,
            # and the UI can label it "未验证" without mistaking it for a fresh
            # observation.
            is_reference = bool(block.get("reference")) or (
                block.get("screen_ref") is not None
            )
            screen_ref = block.get("screen_ref") if is_reference else None
            screen_seq = None if is_reference else block.get("screen_seq", parsed_seq)
            screen_key = (url, screen_seq, screen_ref)
            if screen_key == self._last_screen_key:
                return True
            self._last_screen_key = screen_key
            self._emit(
                {
                    "event": "screen",
                    "step": self._step,
                    "image": url,
                    "current_app": current_app,
                    "screen_seq": screen_seq,
                    "reference": is_reference,
                    "screen_ref": (str(screen_ref) if screen_ref is not None else None),
                }
            )
            return True
        return False

    def wrap_tool_call(self, request, handler):  # noqa: ANN001
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}
        self._emit(
            {
                "event": "tool_call",
                "step": self._step,
                "tool": name,
                "args": redact_args(args),
            }
        )
        started = time.perf_counter()
        try:
            result = handler(request)
        except Exception as exc:  # noqa: BLE001
            self._record_tool_result(
                name,
                None,
                int((time.perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}",
            )
            raise
        self._record_tool_result(
            name, result, int((time.perf_counter() - started) * 1000), None
        )
        return result

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
        args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}
        self._emit(
            {
                "event": "tool_call",
                "step": self._step,
                "tool": name,
                "args": redact_args(args),
            }
        )
        started = time.perf_counter()
        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            self._record_tool_result(
                name,
                None,
                int((time.perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}",
            )
            raise
        self._record_tool_result(
            name, result, int((time.perf_counter() - started) * 1000), None
        )
        return result

    def emit_run_end(self, result: RunResult, *, status: str) -> None:
        self._emit(
            {
                "event": "run_end",
                "status": status,
                "result": asdict(result),
                "tokens_total": self._tokens,
            }
        )


def terminal_status(result: RunResult, agent: Any) -> str:
    if result.success:
        return "succeeded"
    if result.reason == "token_budget_exhausted":
        return "budget_exhausted"
    if result.reason == "loop_fuse":
        return "loop_fuse"
    if getattr(getattr(agent, "session", None), "takeover_reason", None):
        return "takeover"
    if str(result.reason).startswith("error:"):
        return "error"
    return "failed"


__all__ = ["OBS_RE", "WebEventMiddleware", "terminal_status"]
