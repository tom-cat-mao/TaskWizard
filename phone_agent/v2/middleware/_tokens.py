"""Shared token estimation for the budget + compaction middleware (A4).

Two consumers need a cheap, dependency-free token gauge:

* :mod:`phone_agent.v2.middleware.budget` — the **cost** budget. It prefers the
  real ``AIMessage.usage_metadata`` (input + output tokens the gateway billed)
  and only falls back to an estimate (full request input + output) when a call
  reports no usage.
* :mod:`phone_agent.v2.middleware.compact` — the **context-size** gauge. It has
  no per-call usage to read (it must decide *before* the call), so it always
  estimates the current transcript against the model's context window.

The estimate is deliberately crude but script-aware: CJK characters count ~1
token each (``len // 4`` badly undercounts Chinese — a 312-token estimate
measured 1000 under cl100k_base), other text keeps ``len // 4``, and a flat
:data:`IMAGE_TOKEN_COST` is charged per image block. ``AIMessage.tool_calls``
args — the dominant agent output (update_task_doc routes, write_document HTML,
type_text) — are counted from the json-serialized name+args. It is never exact;
it only needs to be monotone and stable so thresholds fire.
"""

from __future__ import annotations

import json
from typing import Any

# Flat per-image token cost. A phone screenshot at gateway tiling lands in the
# ~1-2k token range; 1500 is a middle estimate (design: "len//4 + 图1500").
IMAGE_TOKEN_COST = 1500

# CJK codepoint ranges counted at ~1 token per character (sorted ascending).
# Covers CJK Unified Ideographs + Extension A, CJK punctuation (，「」…), and
# fullwidth forms (，！？ＡＢ).
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xFF00, 0xFFEF),
)


def _is_image_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("type") in {"image_url", "image"}:
        return True
    return "image_url" in block


def _is_cjk_codepoint(codepoint: int) -> bool:
    """Single-probe check against the ascending ``_CJK_RANGES`` table."""

    for low, high in _CJK_RANGES:
        if codepoint < low:
            return False
        if codepoint <= high:
            return True
    return False


def _tool_calls_of(message: Any) -> list[dict]:
    """Normalize ``tool_calls`` from a message object or dict (or ``[]``)."""

    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not calls:
        return []
    return [call for call in calls if isinstance(call, dict)]


def _tool_call_payload(call: dict) -> str:
    """Json-serialize one tool call's name+args (OpenAI-shape tolerated)."""

    name = call.get("name")
    args = call.get("args")
    if name is None or args is None:
        function = call.get("function")
        if isinstance(function, dict):
            if name is None:
                name = function.get("name")
            if args is None:
                args = function.get("arguments")
    try:
        return json.dumps(
            {"name": name, "args": args},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return f"{name!r}:{args!r}"


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a plain text string (script-aware).

    CJK characters count ~1 token each; everything else keeps the cheap
    ``len // 4`` heuristic. Single pass, no per-call regex compile; pure-ASCII
    text short-circuits to ``len // 4``.
    """

    if not text:
        return 0
    if text.isascii():
        return len(text) // 4
    cjk_chars = 0
    other_chars = 0
    for char in text:
        if _is_cjk_codepoint(ord(char)):
            cjk_chars += 1
        else:
            other_chars += 1
    return cjk_chars + other_chars // 4


def estimate_message_tokens(message: Any) -> int:
    """Estimate tokens for one message (text + images + ``tool_calls`` args).

    Accepts either a message object (``.content``) or a raw content value
    (``str`` | ``list[dict]``). Non-text, non-image blocks contribute nothing;
    every ``tool_calls`` entry adds :func:`estimate_text_tokens` over its
    json-serialized name+args.
    """

    content = getattr(message, "content", message)
    if isinstance(content, str):
        total = estimate_text_tokens(content)
    else:
        total = 0
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total += estimate_text_tokens(str(block.get("text", "")))
                    elif _is_image_block(block):
                        total += IMAGE_TOKEN_COST
                elif isinstance(block, str):
                    total += estimate_text_tokens(block)
    for call in _tool_calls_of(message):
        total += estimate_text_tokens(_tool_call_payload(call))
    return total


def estimate_context_tokens(messages: Any) -> int:
    """Estimate total tokens for a list of messages (sum of per-message)."""

    if not messages:
        return 0
    return sum(estimate_message_tokens(m) for m in messages)


def usage_tokens(message: Any) -> int | None:
    """Return ``input + output`` tokens from ``usage_metadata`` or ``None``.

    ``None`` means the message carries no usable usage (a scripted / cached
    response, or a provider that omits usage) — the caller falls back to an
    estimate. A present-but-zero usage returns ``0`` (a real reported value).
    """

    um = getattr(message, "usage_metadata", None)
    if not isinstance(um, dict):
        return None
    if "input_tokens" not in um and "output_tokens" not in um:
        return None
    try:
        return int(um.get("input_tokens", 0) or 0) + int(um.get("output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None


__all__ = [
    "IMAGE_TOKEN_COST",
    "estimate_text_tokens",
    "estimate_message_tokens",
    "estimate_context_tokens",
    "usage_tokens",
]
