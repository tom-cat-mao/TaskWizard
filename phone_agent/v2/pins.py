"""Pinned-block identification contract for the TaskWizard v2 thin loop.

This module exposes the exact message-id prefixes that the actor uses to mark
blocks which must never be summarised, folded, or removed by context-hygiene
middleware.  Plugins and external capability code should import these constants
rather than hard-coding the prefix strings so the contract stays byte-for-byte
consistent across the codebase.
"""

TASKDOC_ID_PREFIX: str = "__taskdoc__"
"""Pinned id prefix for the TaskDoc board block injected before every turn."""

COMPACT_ID_PREFIX: str = "__compact__"
"""Pinned id prefix for the auto-compaction summary block (immune to folding)."""
