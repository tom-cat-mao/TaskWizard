"""Read-only context admission for the actual model about to be invoked.

Adapters own token estimation and declared model limits. This module combines
them with harness reserves; it never edits messages or retries a model. A
rejection based on approximate counts is explicitly different from a complete
count exceeding a declared window. Passing an estimate is not a guarantee that
an opaque gateway will accept the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from phone_agent.v2.middleware._tokens import IMAGE_TOKEN_COST
from phone_agent.v2.middleware.images import _is_image_block
from phone_agent.v2.providers.context import (
    estimate_model_input,
    model_context_profile,
)


@dataclass(frozen=True)
class ContextAdmission:
    """A capacity decision with the estimation assumptions kept visible."""

    allowed: bool
    reason: str
    input_tokens: int
    required_tokens: int
    context_window: int | None
    schema_reserve: int
    output_reserve: int
    estimate_complete: bool
    estimate_source: str
    capacity_source: str

    @property
    def exceeds_capacity(self) -> bool:
        return not self.allowed


def _positive(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def check_context_admission(
    model: Any,
    messages: Sequence[Any],
    *,
    config: Any = None,
    tools: Sequence[Any] = (),
    context_window: int | None = None,
    schema_reserve: int | None = None,
    output_reserve: int | None = None,
) -> ContextAdmission:
    """Measure a complete request without silently changing its contents.

    An explicit/config limit may tighten but never enlarge this model's known
    declaration (especially when it is a smaller fallback model).
    No inferred capacity is invented here: callers doing legacy inference may
    pass it explicitly, while fallback admission can keep an unknown limit.
    ``input_tokens`` includes the schema/image estimates added here; output is
    counted only in ``required_tokens``. Auxiliary callers pass schema=0 and
    omit actor config so a memory model never inherits the actor's window.
    """

    profile = model_context_profile(model, tools=tools)
    estimate = estimate_model_input(model, messages, tools=tools)
    window = _positive(context_window)
    source = "override" if window is not None else "unknown"
    if window is None:
        window = _positive(getattr(config, "context_window", None))
        if window is not None:
            source = "config"
    declared = _positive(profile.context_window)
    if declared is not None and (window is None or declared < window):
        window = declared
        source = str(profile.source)

    schema = max(
        0,
        int(
            schema_reserve
            if schema_reserve is not None
            else getattr(config, "compact_schema_reserve", 3000)
        ),
    )
    if estimate.includes_tools:
        schema = 0
    output = max(
        0,
        int(
            output_reserve
            if output_reserve is not None
            else getattr(config, "compact_output_reserve", 2000)
        ),
        _positive(profile.max_output_tokens) or 0,
    )
    missing_images = 0
    if not estimate.includes_images:
        missing_images = IMAGE_TOKEN_COST * sum(
            1
            for message in messages
            if isinstance(getattr(message, "content", None), list)
            for block in message.content
            if _is_image_block(block)
        )
    complete = bool(estimate.complete and not schema and not missing_images)
    input_tokens = max(0, int(estimate.tokens)) + schema + missing_images
    required = input_tokens + output
    exceeds = window is not None and required > window
    if exceeds:
        reason = (
            "context_capacity_exceeded"
            if complete
            else "estimated_context_capacity_exceeded"
        )
    else:
        reason = "within_capacity" if window is not None else "capacity_unknown"
    return ContextAdmission(
        allowed=not exceeds,
        reason=reason,
        input_tokens=input_tokens,
        required_tokens=required,
        context_window=window,
        schema_reserve=schema,
        output_reserve=output,
        estimate_complete=complete,
        estimate_source=str(estimate.source),
        capacity_source=source,
    )


__all__ = ["ContextAdmission", "check_context_admission"]
