"""Built-in tool-risk declarations for the v2 assembly plane (P0 #18).

A tool's *risk* tells the safety classifier how a call to it must be treated:

* ``actuation`` — the call is routed through the actuation risk classifier, so a
  soft candidate can be reviewer-judged and a hard signal can warn (``wary`` /
  ``reviewer``) or interrupt (``hard``).
* ``readonly`` — the call never enters the classifier (perception, TaskDoc,
  document and archive tools: no side effect the classifier can judge).

The declaration travels with the tool object as ``metadata["risk"]`` and is
recorded by :meth:`phone_agent.v2.capabilities.CapabilityAssemblyContext.register_tool`
/ ``register_core_tool`` into the assembly-time ``tool_risk_registry`` service,
which the safety listener reads at classification time.  An undeclared tool
fails **closed** to ``actuation``, so silence can never open the
observation-only path; an explicit ``risk=`` argument to the registration seam
wins over the stamp.

This module is a leaf (stdlib + typing only) on purpose: the ``tools/``
submodules that build the built-in tool objects import it, and importing the
assembly module or the ``tools`` package from there would re-enter a partially
initialised package (``tools/__init__.py`` imports those submodules).

The actuation set is byte-for-byte the pre-declaration gate set
(``tap`` / ``long_press`` / ``type_text`` / ``launch_app``) so every built-in
tool keeps identical safety behaviour.  ``scroll`` / ``swipe`` / ``back`` /
``home`` are physical actuations without a target-text argument — the classifier
would have nothing to judge — so they stay ``readonly`` until a later round
gives them a textual signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

ToolRisk = Literal["actuation", "readonly"]

#: The only legal declaration values; anything else is rejected at declaration.
TOOL_RISKS: frozenset[str] = frozenset({"actuation", "readonly"})

_RISK_METADATA_KEY = "risk"

_BUILTIN_TOOL_RISKS: dict[str, ToolRisk] = {
    # perception ---------------------------------------------------------
    "read_screen": "readonly",
    "locate": "readonly",
    # actuation ----------------------------------------------------------
    "tap": "actuation",
    "long_press": "actuation",
    "type_text": "actuation",
    "launch_app": "actuation",
    "scroll": "readonly",
    "swipe": "readonly",
    "back": "readonly",
    "home": "readonly",
    "wait": "readonly",
    # control ------------------------------------------------------------
    "finish": "readonly",
    "ask_user": "readonly",
    "take_over": "readonly",
    # TaskDoc (taskdoc capability; the legacy builder mounts the same tool) --
    "update_task_doc": "readonly",
    # deliverable --------------------------------------------------------
    "write_document": "readonly",
    "update_document": "readonly",
    # observation archive ------------------------------------------------
    "recall_screen": "readonly",
    "search_screens": "readonly",
}

#: Every built-in tool name and its declared risk (read-only view).
BUILTIN_TOOL_RISKS: Mapping[str, ToolRisk] = MappingProxyType(
    dict(_BUILTIN_TOOL_RISKS)
)


def builtin_tool_risk(name: str) -> ToolRisk:
    """Return the declaration for a built-in tool name; undeclared raises.

    Built-in tools self-declare here, so a missing entry is a harness bug and
    must fail loudly at assembly instead of quietly landing in the fail-closed
    default.
    """

    try:
        return _BUILTIN_TOOL_RISKS[str(name)]
    except KeyError as exc:
        raise ValueError(
            f"built-in tool {name!r} has no risk declaration; add it to "
            "phone_agent/v2/tool_risk.py::_BUILTIN_TOOL_RISKS"
        ) from exc


def declare_risk(tool: Any, risk: ToolRisk) -> Any:
    """Stamp ``tool.metadata["risk"]`` and return the tool (builder chaining).

    The stamp is the built-in declaration plane's carrier: registration reads it
    back with :func:`declared_risk`, so the tool and its declaration travel
    together through the assembly seams and same-name replacement.
    """

    if risk not in TOOL_RISKS:
        raise ValueError(f"invalid tool risk: {risk!r} (expected actuation/readonly)")
    metadata = dict(getattr(tool, "metadata", None) or {})
    metadata[_RISK_METADATA_KEY] = risk
    try:
        tool.metadata = metadata
    except Exception as exc:  # noqa: BLE001 - a frozen tool must not stay silent
        raise TypeError(f"cannot declare risk on tool {tool!r}: {exc}") from exc
    return tool


def declare_builtin_risk(tool: Any) -> Any:
    """Stamp one built-in tool with the risk declared for its name.

    A name outside :data:`BUILTIN_TOOL_RISKS` raises (see
    :func:`builtin_tool_risk`): the built-in table must stay exhaustive, and a
    new tool without a declaration is a review-time failure, not a silent
    ``actuation`` default.
    """

    return declare_risk(tool, builtin_tool_risk(str(getattr(tool, "name", ""))))


def declare_builtin_risks(tools: list[Any]) -> list[Any]:
    """Stamp every built-in tool in ``tools`` and return the list unchanged."""

    for tool in tools:
        declare_builtin_risk(tool)
    return tools


def declared_risk(tool: Any) -> str | None:
    """Read the risk stamped on a tool object (``None`` when undeclared)."""

    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get(_RISK_METADATA_KEY)
    return str(value) if value is not None else None


__all__ = [
    "BUILTIN_TOOL_RISKS",
    "TOOL_RISKS",
    "ToolRisk",
    "builtin_tool_risk",
    "declare_builtin_risk",
    "declare_builtin_risks",
    "declare_risk",
    "declared_risk",
]
