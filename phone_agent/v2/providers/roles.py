"""LLM role resolution for the five v2 call sites (S4).

The fallback chains mirror today's behavior exactly:

* ``actor``          <- ``PHONE_AGENT_MODEL`` (config.model_name)
* ``memory``         <- ``PHONE_AGENT_MEMORY_MODEL`` -> actor
* ``verifier``       <- ``PHONE_AGENT_VERIFIER_MODEL`` -> actor
* ``safety_reviewer``<- ``PHONE_AGENT_SAFETY_REVIEWER_MODEL`` -> verifier -> actor
* ``distill``        <- memory (``PHONE_AGENT_MEMORY_MODEL``) -> actor

Each resolved value is a model *reference*: a bare model name (default
provider) or ``provider:model`` addressing.
"""

from __future__ import annotations

from typing import Any

ROLES = ("actor", "memory", "verifier", "safety_reviewer", "distill")


def resolve_role_ref(config: Any, role: str) -> str:
    """Resolve one role to its model reference (bare name or provider:model)."""

    model_name = str(getattr(config, "model_name", "") or "")
    if role == "actor":
        ref = model_name
    elif role == "memory":
        ref = getattr(config, "memory_model", None) or model_name
    elif role == "verifier":
        ref = getattr(config, "verifier_model", None) or model_name
    elif role == "safety_reviewer":
        ref = (
            getattr(config, "safety_reviewer_model", None)
            or getattr(config, "verifier_model", None)
            or model_name
        )
    elif role == "distill":
        ref = getattr(config, "memory_model", None) or model_name
    else:
        raise ValueError(f"unknown role: {role!r} (expected one of {ROLES})")
    return str(ref or "")
