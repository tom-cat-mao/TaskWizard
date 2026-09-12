"""Shared detection of native replay metadata, independent of any SDK imports."""

from __future__ import annotations

from typing import Any

_NATIVE_METADATA_KEYS = frozenset(
    {
        "signature",
        "thought_signature",
        "thoughtSignature",
        "encrypted_content",
        "encryptedContent",
        "compaction",
    }
)
CONTEXT_BOOKKEEPING_KEYS = frozenset({"__openai_function_call_ids__"})


def native_metadata(value: Any) -> Any:
    """Extract replay metadata without ordinary text/image bodies.

    This tree is for detection/counting only. A signature remains attached to
    the original part it authenticates; it cannot be transplanted into a digest.
    Known SDK bookkeeping is neither replay state nor a reason to pin a group.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in CONTEXT_BOOKKEEPING_KEYS:
                continue
            if key in _NATIVE_METADATA_KEYS and item:
                result[key] = item
            elif isinstance(item, (dict, list, tuple)):
                nested = native_metadata(item)
                if nested:
                    result[key] = nested
        return result
    if isinstance(value, (list, tuple)):
        return [part for item in value if (part := native_metadata(item))]
    return {}


def has_native_metadata(value: Any) -> bool:
    """Whether a nested content block/envelope has nonempty native replay state."""
    return bool(native_metadata(value))


__all__ = ["CONTEXT_BOOKKEEPING_KEYS", "native_metadata", "has_native_metadata"]
