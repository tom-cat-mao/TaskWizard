"""Per-run token usage ledger shared by actor and side-model calls."""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
from typing import Any

from phone_agent.v2.middleware._tokens import estimate_message_tokens, usage_tokens

USAGE_ROLES = frozenset({"actor", "compact", "verifier", "reviewer", "distill"})

# Provider usage schemas have not converged on one cache-hit field.  Probe known
# raw and normalized aliases, then take the largest usable value so duplicate
# representations are not double-counted.
_CACHED_TOKEN_PATHS: tuple[tuple[str, ...], ...] = (
    ("prompt_tokens_details", "cached_tokens"),
    ("prompt_token_details", "cached_tokens"),
    ("input_token_details", "cached_tokens"),
    ("input_tokens_details", "cached_tokens"),
    ("input_token_details", "cache_read"),
    ("input_token_details", "priority_cache_read"),
    ("input_token_details", "flex_cache_read"),
    ("input_token_details", "cache_read_input_tokens"),
    ("cached_tokens",),
    ("cached_input_tokens",),
    ("cache_read_input_tokens",),
    ("cache_read_tokens",),
    ("cacheReadInputTokens",),
    ("prompt_cache_hit_tokens",),
    ("cached_content_token_count",),
    ("cachedContentTokenCount",),
)

_CACHE_WRITE_PATHS: tuple[tuple[str, ...], ...] = (
    ("input_token_details", "cache_creation"),
    ("input_token_details", "priority_cache_creation"),
    ("input_token_details", "flex_cache_creation"),
    ("input_tokens_details", "cache_write_tokens"),
    ("prompt_tokens_details", "cache_write_tokens"),
    ("cache_creation_input_tokens",),
    ("cache_write_tokens",),
)


def _reported_count(value: Any) -> int | None:
    """Keep absent/invalid counts unknown rather than manufacturing a zero."""

    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if count < 0 or isinstance(value, float) and count != value:
        return None
    return count


def _count_at_paths(metadata: Mapping, paths: tuple[tuple[str, ...], ...]) -> int | None:
    found: list[int] = []
    for path in paths:
        value: Any = metadata
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                break
            value = value[key]
        else:
            count = _reported_count(value)
            if count is not None:
                found.append(count)
    # Aliases describe the same quantity; never add raw and normalized copies.
    return max(found, default=None)


def usage_details(response: Any, *, model: Any = None) -> dict[str, int | None]:
    """Read optional numeric telemetry without making it a call failure."""
    try:
        return _usage_details(response, model=model)
    except Exception:  # noqa: BLE001 - custom telemetry must fail open
        return dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


def _usage_details(response: Any, *, model: Any = None) -> dict[str, int | None]:
    """Safe, optional input/cache counts for observability, without repricing.

    Accept an AIMessage or LangChain ModelResponse. Input/output come only from
    normalized usage: raw provider totals can have different inclusion rules.
    Cached tokens remain part of input; the legacy budget still counts I + O.
    Missing cache-write data is unknown, not evidence of a free cache write.
    """

    result = getattr(response, "result", None)
    if isinstance(result, (list, tuple)):
        response = next(
            (item for item in reversed(result) if getattr(item, "type", None) == "ai"),
            result[-1] if result else None,
        )
    if model is not None:
        from phone_agent.v2.providers.context import normalize_model_usage

        normalized = normalize_model_usage(model, response)
        if isinstance(normalized, Mapping):
            return {
                name: _reported_count(normalized.get(name))
                for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
            }
    metadata = getattr(response, "usage_metadata", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "input_tokens": _reported_count(metadata.get("input_tokens")),
        "output_tokens": _reported_count(metadata.get("output_tokens")),
        "cache_read_tokens": _count_at_paths(metadata, _CACHED_TOKEN_PATHS),
        "cache_write_tokens": _count_at_paths(metadata, _CACHE_WRITE_PATHS),
    }


def _cached_tokens(message: Any) -> int:
    """Return provider-reported cached input tokens, or zero if absent."""

    metadata = getattr(message, "usage_metadata", None)
    if not isinstance(metadata, Mapping):
        return 0
    return _count_at_paths(metadata, _CACHED_TOKEN_PATHS) or 0


class UsageLedger:
    """Thread-safe-enough token accumulator for one agent run.

    Provider-reported usage wins whenever it is available. Callers may supply a
    fuller request-plus-response estimate for providers that omit metadata; when
    they do not, the response message itself is estimated as a final fallback.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._total = 0
        self._by_role: dict[str, int] = {}
        self._cached_total = 0
        self._cached_by_role: dict[str, int] = {}

    def record(
        self,
        role: str,
        message_or_none: Any = None,
        *,
        estimate_tokens: int | None = None,
    ) -> int:
        """Record one model call and return the number of tokens counted."""

        if role not in USAGE_ROLES:
            raise ValueError(f"unknown usage role: {role!r}")

        reported = (
            usage_tokens(message_or_none) if message_or_none is not None else None
        )
        if reported is not None:
            counted = reported
        elif estimate_tokens is not None:
            counted = int(estimate_tokens)
        elif message_or_none is not None:
            counted = estimate_message_tokens(message_or_none)
        else:
            counted = 0
        counted = max(0, counted)
        cached = _cached_tokens(message_or_none) if message_or_none is not None else 0

        with self._lock:
            self._total += counted
            self._by_role[role] = self._by_role.get(role, 0) + counted
            self._cached_total += cached
            self._cached_by_role[role] = (
                self._cached_by_role.get(role, 0) + cached
            )
        return counted

    @property
    def total(self) -> int:
        """Grand total across actor and every side-model role."""

        with self._lock:
            return self._total

    def by_role(self) -> dict[str, int]:
        """Return a snapshot of cumulative usage grouped by model role."""

        with self._lock:
            return dict(self._by_role)

    @property
    def cached_total(self) -> int:
        """Grand total of provider-reported cached input tokens."""

        with self._lock:
            return self._cached_total

    def cached_by_role(self) -> dict[str, int]:
        """Return cached input-token totals grouped by model role."""

        with self._lock:
            return dict(self._cached_by_role)

    def reset(self) -> None:
        """Clear all per-run totals."""

        with self._lock:
            self._total = 0
            self._by_role.clear()
            self._cached_total = 0
            self._cached_by_role.clear()


__all__ = ["UsageLedger", "USAGE_ROLES", "usage_details"]
