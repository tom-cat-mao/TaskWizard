"""Shared test doubles for the v2 suite — the single source of truth.

One module per double family; a test imports the double and overrides only the
fields that differ from the defaults. Nothing here touches a real device, MLX, a
model gateway or the network.

Consolidated in the "test doubles + observation domain" refactor (batches 1+2/5);
the rationale and the rejected alternatives are in
``.agents/notes/implemented/2026-09-20-test-doubles-consolidation.md``.
"""
