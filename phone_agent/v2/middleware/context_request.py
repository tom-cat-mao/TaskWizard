"""Prepare and observe each actual model attempt without rewriting its history.

The harness owns semantic compaction and its full-list bridge. This boundary
only decorates a private request copy through the selected model's support,
checks that model's capacity, and emits numeric/hash-only diagnostics. Auxiliary
calls can use the same preparation without acquiring actor history or a new
budget policy. SDK-internal retries remain opaque, not invented HTTP attempts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import time
from typing import Any, Callable, Mapping

from langchain_core.messages import BaseMessage

from phone_agent.v2.middleware.context_admission import check_context_admission
from phone_agent.v2.providers.context import (
    model_context_profile,
    prepare_model_messages,
)
from phone_agent.v2.usage import usage_details


class ContextCapacityError(ValueError):
    """The selected model cannot admit this request without semantic changes."""

    def __init__(self, admission: Any) -> None:
        self.admission = admission
        super().__init__(
            f"{admission.reason}: required={admission.required_tokens}, "
            f"capacity={admission.context_window}, source={admission.estimate_source}"
        )


def _emit(recorder: Any, event: str, **payload: Any) -> None:
    if callable(recorder):
        try:
            recorder(event, **payload)
        except Exception:  # noqa: BLE001 - diagnostics must not control a call
            pass


def _effective_model(model: Any, settings: Mapping[str, Any]) -> Any:
    bind = getattr(model, "bind", None)
    return bind(**settings) if settings and callable(bind) else model


def _message_value(message: Any) -> Any:
    if isinstance(message, BaseMessage):
        return {
            "role": message.type,
            "name": getattr(message, "name", None),
            "content": message.content,
            "tool_calls": getattr(message, "tool_calls", None),
            "tool_call_id": getattr(message, "tool_call_id", None),
            "additional_kwargs": getattr(message, "additional_kwargs", None),
        }
    return message


def _digest(value: Any) -> str:
    # Plaintext (including images) is hashed only in memory. Neither the input
    # nor arbitrary reprs become trace data; the digest stays process-local.
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tool_values(tools: Any) -> list[Any]:
    values = []
    for tool in tools or ():
        if isinstance(tool, Mapping):
            values.append(dict(tool))
        else:
            values.append({
                "name": getattr(tool, "name", None),
                "description": getattr(tool, "description", None),
                "args": getattr(tool, "args", None),
            })
    return values


@dataclass
class _PreparedCall:
    messages: list[Any]
    settings: dict[str, Any]
    metadata: dict[str, Any]


def _prepare_call(
    model: Any,
    messages: list[Any],
    *,
    tools: Any = (),
    settings: Mapping[str, Any] | None = None,
    config: Any = None,
    auxiliary: bool = False,
) -> _PreparedCall:
    settings = dict(settings or {})
    selected = _effective_model(model, settings)
    prepared = prepare_model_messages(selected, messages, tools=tools)
    for key, value in prepared.model_kwargs.items():
        if key in settings and settings[key] != value:
            raise ValueError(f"context preparation conflicts with invocation setting {key!r}")
        settings[key] = value
    selected = _effective_model(model, settings)
    profile = model_context_profile(selected, tools=tools)
    admission = check_context_admission(
        selected,
        prepared.messages,
        tools=tools,
        config=None if auxiliary else config,
        **({"schema_reserve": 0} if auxiliary else {}),
    )
    metadata = {
        **asdict(admission),
        "request_api": profile.request_api,
        "cache_mode": profile.cache_mode,
    }
    if not admission.allowed:
        raise ContextCapacityError(admission)
    return _PreparedCall(prepared.messages, settings, metadata)


class ContextRequestObserver:
    """Per-run actor attempt preparation; fallback is prepared independently."""

    def __init__(self, config: Any, recorder: Any = None) -> None:
        self.config = config
        self.recorder = recorder
        self.reset()

    def reset(self) -> None:
        self._attempt = 0
        self._previous: tuple[str, str, tuple[str, ...]] | None = None

    def prepare(self, request: Any) -> tuple[Any, int]:
        self._attempt += 1
        attempt = self._attempt
        system = getattr(request, "system_message", None)
        messages = list(getattr(request, "messages", None) or [])
        if system is not None:
            messages.insert(0, system)
        tools = getattr(request, "tools", None) or []
        model = request.model
        settings = getattr(request, "model_settings", None) or {}
        try:
            prepared = _prepare_call(
                model, messages, tools=tools, settings=settings, config=self.config
            )
        except Exception as exc:
            details = asdict(exc.admission) if isinstance(exc, ContextCapacityError) else {"allowed": False}
            _emit(
                self.recorder, "context_admission",
                attempt=attempt, role="actor", error_type=type(exc).__name__,
                **details,
            )
            raise

        try:
            self._observe_input(model, tools, prepared, attempt)
        except Exception:  # noqa: BLE001 - a fingerprint is only telemetry
            _emit(self.recorder, "context_request", attempt=attempt, role="actor",
                  fingerprint_unavailable=True, **prepared.metadata)
        # Cache preparation preserves canonical order. Retain the separate
        # LangChain system field to avoid sending it twice via the factory.
        overrides: dict[str, Any] = {
            "messages": prepared.messages[1:] if system is not None else prepared.messages,
            "model_settings": prepared.settings,
        }
        if system is not None:
            overrides["system_message"] = prepared.messages[0]
        return request.override(**overrides), attempt

    def _observe_input(self, model: Any, tools: Any, prepared: _PreparedCall, attempt: int) -> None:
        # Local ordered-message evidence is explicitly not a server token LCP:
        # a provider may further merge/promote system blocks during serialization.
        identity = _digest({
            "model": getattr(model, "model_name", getattr(model, "model", None)),
            "api": prepared.metadata["request_api"],
            "settings": prepared.settings,
        })
        tools_hash = _digest(_tool_values(tools))
        hashes = tuple(_digest(_message_value(item)) for item in prepared.messages)
        current = (identity, tools_hash, hashes)
        first_changed: str | None = None
        common_messages = 0
        if self._previous is not None:
            previous = self._previous
            if previous[0] != identity:
                first_changed = "model_or_settings"
            elif previous[1] != tools_hash:
                first_changed = "tools"
            else:
                for old, new in zip(previous[2], hashes):
                    if old != new:
                        break
                    common_messages += 1
                if common_messages < max(len(previous[2]), len(hashes)):
                    first_changed = f"messages[{common_messages}]"
        self._previous = current
        _emit(
            self.recorder, "context_request", attempt=attempt, role="actor",
            fingerprint_basis="ordered_client_messages", first_changed=first_changed,
            common_messages=common_messages, message_count=len(hashes),
            **prepared.metadata,
        )
    def record_result(self, request: Any, attempt: int, response: Any, error: BaseException | None) -> None:
        _emit(
            self.recorder, "model_attempt_usage", role="actor", attempt=attempt,
            attempt_scope="handler_invoke", ok=error is None,
            error_type=type(error).__name__ if error else None,
            **usage_details(response, model=request.model),
        )


def invoke_with_context(
    model: Any,
    messages: Any,
    *,
    config: Any = None,
    role: str = "auxiliary",
    trace_recorder: Callable[..., Any] | None = None,
    model_settings: Mapping[str, Any] | None = None,
) -> Any:
    """Prepare an auxiliary model while leaving its caller's usage policy intact.

    No actor transcript, actor capacity override, new retry or spending policy
    is introduced. Each caller retains its existing ledger and failure rules.
    """

    prepared = _prepare_call(
        model, list(messages), settings=model_settings, config=config, auxiliary=True
    )
    _emit(trace_recorder, "context_request", role=role, **prepared.metadata)
    started = time.perf_counter()
    try:
        response = model.invoke(prepared.messages, **prepared.settings)
    except Exception as exc:
        _emit(
            trace_recorder, "model_attempt_usage", role=role, ok=False,
            attempt_scope="handler_invoke", error_type=type(exc).__name__,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    _emit(
        trace_recorder, "model_attempt_usage", role=role, ok=True,
        attempt_scope="handler_invoke", error_type=None,
        latency_ms=int((time.perf_counter() - started) * 1000),
        **usage_details(response, model=model),
    )
    return response


__all__ = ["ContextCapacityError", "ContextRequestObserver", "invoke_with_context"]
