"""Value resolution for models.json fields (S4).

pi-style three-state values: a literal string, ``$ENV`` / ``${ENV}`` env
interpolation.  ``!command`` execution is deliberately NOT implemented in this
phase (documented non-goal; key material comes from the environment instead).

Resolution is single-pass and non-recursive: an env value that itself contains
``$`` is kept verbatim. Missing env names raise :class:`ValueError`; the loader
reports them as explicit models-file configuration errors.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_ENV_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def resolve_value(value: object, *, env: Mapping[str, str] | None = None) -> object:
    """Resolve ``$ENV`` / ``${ENV}`` references inside a string value.

    Non-string values pass through untouched.  A missing env variable raises
    ``ValueError`` (fail-visible at parse time).
    """

    if not isinstance(value, str) or "$" not in value:
        return value
    lookup = os.environ if env is None else env

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in lookup:
            raise ValueError(f"models.json references undefined env var: {name}")
        return lookup[name]

    return _ENV_PATTERN.sub(_sub, value)


def resolve_str(value: object, *, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve and require a string result; ``None`` passes through."""

    if value is None:
        return None
    resolved = resolve_value(value, env=env)
    if not isinstance(resolved, str):
        raise ValueError(f"expected a string value, got {resolved!r}")
    return resolved


def resolve_headers(
    headers: Mapping[str, object] | None, *, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Resolve every header value; keys stay literal."""

    if not headers:
        return {}
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        text = resolve_str(value, env=env)
        if text is None:
            raise ValueError(f"header {key!r} must resolve to a string")
        resolved[str(key)] = text
    return resolved
