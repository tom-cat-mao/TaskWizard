"""Conservative context implementations for the built-in SDK adapters.

Only opt-in ``cachePolicy=stable-prefix`` decorates content. OpenAI requires
explicit Responses selection; native Anthropic uses legal ephemeral text
breakpoints. Google remains an implicit-cache/no-decoration adapter. Cache
preparation has no network operations or persistent canonical state changes.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
from threading import Lock
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from phone_agent.v2.native_content import has_native_metadata
from phone_agent.v2.pins import TASKDOC_ID_PREFIX
from phone_agent.v2.providers.context import (
    ModelContextProfile,
    ModelInputEstimate,
    PreparedModelMessages,
    unwrap_model,
)
from phone_agent.v2.providers.types import (
    API_ANTHROPIC,
    API_GOOGLE,
    API_OPENAI,
    ModelSpec,
)

_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_KNOWN_BLOCKS = _TEXT_TYPES | {"image", "image_url", "tool_use", "tool_result"}
_DYNAMIC_MARKERS = ("[TASK_DOC]", "[COMPACT_WARN]", "[COMPACT_DONE]", "[TOKEN_BUDGET]")


def _positive_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _json(value: Any) -> str:
    def encode(item: Any) -> Any:
        method = getattr(item, "model_dump", None)
        if callable(method):
            return method(exclude_none=True)
        raise TypeError("unsupported request structure")

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode
    )


def _bound_settings(model: Any, tools: Any) -> tuple[Any, dict[str, Any]]:
    base, settings = unwrap_model(model)
    if tools and "tools" not in settings:
        _, tool_settings = unwrap_model(base.bind_tools(list(tools)))
        settings = {**tool_settings, **settings}
    return base, settings


def _request_protocol_and_cap(
    base: Any, api: str, settings: dict[str, Any]
) -> tuple[str, int | None]:
    """Use the SDK's own local alias/merge rules, then its HTTP extra-body cap.

    Only a synthetic text message is serialized: no request is sent, no user
    content is inspected, and no token-count endpoint is called. Guessing the
    first apparent token key is unsafe because SDK aliases can overwrite it.
    """
    sample = [HumanMessage(content="context-profile")]
    if api == API_GOOGLE:
        request = base._prepare_request(sample, **settings)
        config = request["config"]
        cap = _positive_int(config.max_output_tokens)
        extra = getattr(getattr(config, "http_options", None), "extra_body", None)
        if isinstance(extra, dict) and "generationConfig" in extra:
            generation = extra["generationConfig"]
            if not isinstance(generation, dict):
                cap = None
            elif "maxOutputTokens" in generation:
                cap = _positive_int(generation["maxOutputTokens"])
        return api, cap

    payload = base._get_request_payload(sample, **settings)
    request_api = api
    if api == API_OPENAI:
        request_api = "responses" if "input" in payload else "chat/completions"
    extra = payload.get("extra_body")
    if isinstance(extra, dict):
        payload = {**payload, **extra}
    key = (
        "max_output_tokens"
        if request_api == "responses"
        else "max_completion_tokens"
        if request_api == "chat/completions"
        else "max_tokens"
    )
    if request_api == "chat/completions" and "max_tokens" in payload:
        # An HTTP extra_body can reintroduce the deprecated Chat cap alongside
        # its replacement. Server precedence is not a client-side guarantee.
        if "max_completion_tokens" in payload:
            return request_api, None
        key = "max_tokens"
    return request_api, _positive_int(payload.get(key))


def _text_blocks(message: Any) -> list[Any]:
    content = getattr(message, "content", None)
    return (
        [{"type": "text", "text": content}]
        if isinstance(content, str)
        else list(content or [])
    )


def _dynamic(message: Any) -> bool:
    if str(getattr(message, "id", "") or "").startswith(TASKDOC_ID_PREFIX):
        return True
    return any(
        isinstance(block, dict)
        and str(block.get("text", "")).startswith(_DYNAMIC_MARKERS)
        for block in _text_blocks(message)
    )


def _stable_candidates(
    messages: list[Any], *, anthropic: bool
) -> list[tuple[int, int, str]]:
    """Identify stable text endpoints before the first mutable/native block.

    Anthropic lifts all system messages. A tail system pin means history is
    preceded by that mutable pin on the wire, so only the initial stable system
    segment is eligible. Hashes remain private and include the complete prefix;
    content changes or compaction automatically invalidate remembered endpoints.
    """
    first_non_system = next(
        (i for i, m in enumerate(messages) if not isinstance(m, SystemMessage)),
        len(messages),
    )
    lifted_tail = anthropic and any(
        isinstance(m, SystemMessage) for m in messages[first_non_system:]
    )
    hasher = hashlib.sha256()
    candidates: list[tuple[int, int, str]] = []
    for index, message in enumerate(messages):
        if _dynamic(message) or (lifted_tail and index >= first_non_system):
            break
        if index >= first_non_system and isinstance(message, SystemMessage):
            break
        if has_native_metadata(getattr(message, "additional_kwargs", None)):
            break
        hasher.update(
            _json(
                {
                    "type": getattr(message, "type", ""),
                    "tool_calls": getattr(message, "tool_calls", None),
                    "tool_call_id": getattr(message, "tool_call_id", None),
                    "additional_kwargs": getattr(message, "additional_kwargs", None),
                }
            ).encode()
        )
        for block_index, block in enumerate(_text_blocks(message)):
            if (
                not isinstance(block, dict)
                or block.get("type") not in _TEXT_TYPES
                or has_native_metadata(block)
            ):
                return candidates
            text = block.get("text")
            if not isinstance(text, str) or "\nmarks (" in text:
                return candidates
            hasher.update(_json(block).encode())
            if text and isinstance(message, (SystemMessage, HumanMessage, ToolMessage)):
                candidates.append((index, block_index, hasher.hexdigest()))
    return candidates


@dataclass
class BuiltinContextSupport:
    api: str
    spec: ModelSpec
    cache_policy: str = "off"
    # Private bounded hint history, shared by invocation copies of this model.
    # Only hashes are retained. No provider results or claimed cache hits live here.
    _previous: OrderedDict[str, str] = field(
        default_factory=OrderedDict, repr=False, compare=False
    )
    _lock: Any = field(default_factory=Lock, repr=False, compare=False)

    def __deepcopy__(self, memo: dict) -> "BuiltinContextSupport":
        return self

    def profile(self, model: Any, tools: Any = ()) -> ModelContextProfile:
        base, settings = _bound_settings(model, tools)
        request_api, cap = _request_protocol_and_cap(base, self.api, settings)
        return ModelContextProfile(
            context_window=self.spec.context_window,
            max_output_tokens=cap,
            request_api=request_api,
            source="model_declaration+sdk_payload",
            cache_mode=self.cache_policy,
        )

    def estimate(
        self, model: Any, messages: list[Any], tools: Any = ()
    ) -> ModelInputEstimate:
        from phone_agent.v2.middleware._tokens import (
            estimate_context_tokens,
            estimate_text_tokens,
        )

        _, settings = _bound_settings(model, tools)
        serialized_tools = settings.get("tools")
        tokens = estimate_context_tokens(messages)
        if serialized_tools:
            tokens += estimate_text_tokens(_json(serialized_tools))
        return ModelInputEstimate(
            tokens=tokens,
            includes_tools=bool(serialized_tools),
            complete=False,
            source="message_heuristic+sdk_tool_schema",
        )

    def prepare(
        self, model: Any, messages: list[Any], tools: Any = ()
    ) -> PreparedModelMessages:
        if self.cache_policy == "off":
            return PreparedModelMessages(messages)
        profile = self.profile(model, tools)
        if self.api == API_OPENAI and profile.request_api != "responses":
            return PreparedModelMessages(messages)
        candidates = _stable_candidates(messages, anthropic=self.api == API_ANTHROPIC)
        if not candidates:
            params = (
                {"prompt_cache_options": {"mode": "explicit"}}
                if self.api == API_OPENAI
                else {}
            )
            return PreparedModelMessages(messages, params)
        # Retain the last endpoint written on the preceding compatible request,
        # plus a static endpoint and the newest stable endpoint (at most 3 writes).
        family = candidates[0][2]
        with self._lock:
            previous = self._previous.get(family)
            self._previous[family] = candidates[-1][2]
            self._previous.move_to_end(family)
            while len(self._previous) > 16:
                self._previous.popitem(last=False)
        selected = {candidates[0], candidates[-1]}
        selected.update(
            candidate for candidate in candidates if candidate[2] == previous
        )
        marker = (
            "prompt_cache_breakpoint" if self.api == API_OPENAI else "cache_control"
        )
        value = (
            {"mode": "explicit"} if self.api == API_OPENAI else {"type": "ephemeral"}
        )
        for message_index, block_index, _ in selected:
            message = messages[message_index]
            if isinstance(message.content, str):
                message.content = [{"type": "text", "text": message.content}]
            message.content[block_index][marker] = dict(value)
        params = (
            {"prompt_cache_options": {"mode": "explicit"}}
            if self.api == API_OPENAI
            else {}
        )
        return PreparedModelMessages(messages, params)

    def protected_message_ids(self, model: Any, messages: list[Any]) -> frozenset[str]:
        return frozenset(
            str(message.id)
            for message in messages
            if getattr(message, "id", None)
            and (
                has_native_metadata(getattr(message, "additional_kwargs", None))
                or any(
                    isinstance(block, dict)
                    and (
                        block.get("type") not in _KNOWN_BLOCKS
                        or has_native_metadata(block)
                    )
                    for block in _text_blocks(message)
                )
            )
        )
