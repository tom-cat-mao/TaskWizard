"""Repository path anchors for tests — depth-sensitive ``parents[N]`` lives here.

A test that spells ``Path(__file__).resolve().parents[N]`` hard-codes its own
depth in the tree: move the file (e.g. into ``tests/v2/observation/``) and the
constant silently points somewhere else. Import :data:`REPO_ROOT` instead.
"""

from __future__ import annotations

from pathlib import Path

# tests/v2/doubles/paths.py -> parents: [0]=doubles [1]=v2 [2]=tests [3]=repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

__all__ = ["REPO_ROOT"]
