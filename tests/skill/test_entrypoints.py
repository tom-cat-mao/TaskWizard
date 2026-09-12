"""Cross-agent entry points must be live symlinks to the canonical skill.

CodeBuddy / Claude Code may not read ``.agents/skills``. The repo keeps thin
compat entry points under ``.claude/skills`` and ``.codebuddy/skills`` that are
**relative symlinks** to the canonical ``.agents/skills/phone-agent-live-diagnosis``
directory — never copies, which can silently go stale.

These are offline checks: they resolve the links on disk and compare content,
and (when a git work tree is present) assert the links are not gitignored so they
can ship in the PR. They do not claim any CLI auto-discovers the skill.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _ROOT / ".agents" / "skills" / "phone-agent-live-diagnosis"
_ENTRYPOINTS = (
    ".claude/skills/phone-agent-live-diagnosis",
    ".codebuddy/skills/phone-agent-live-diagnosis",
)


@pytest.mark.parametrize("rel", _ENTRYPOINTS)
def test_entrypoint_is_symlink_to_canonical(rel: str) -> None:
    path = _ROOT / rel
    assert path.is_symlink(), f"{rel} must be a symlink, not a copy"
    assert path.resolve() == _CANONICAL.resolve()


@pytest.mark.parametrize("rel", _ENTRYPOINTS)
def test_entrypoint_content_is_not_stale(rel: str) -> None:
    entry = _ROOT / rel
    # The link resolves to the canonical tree, so the SKILL and references bytes
    # must match exactly (a stale copy would drift).
    assert (entry / "SKILL.md").read_bytes() == (_CANONICAL / "SKILL.md").read_bytes()
    entry_refs = entry / "references"
    canon_refs = _CANONICAL / "references"
    for canon_file in canon_refs.rglob("*"):
        if canon_file.is_file():
            rel_file = canon_file.relative_to(canon_refs)
            assert (entry_refs / rel_file).read_bytes() == canon_file.read_bytes()


def test_canonical_skill_is_present() -> None:
    assert (_CANONICAL / "SKILL.md").is_file()
    assert (_CANONICAL / "scripts" / "run_diagnosis.py").is_file()
    assert (_CANONICAL / "references" / "case-format.md").is_file()


@pytest.mark.parametrize("rel", _ENTRYPOINTS)
def test_entrypoint_is_trackable(rel: str) -> None:
    """The symlink must not be gitignored (so it can ship in the PR)."""

    if not shutil.which("git") or not (_ROOT / ".git").exists():
        pytest.skip("no git work tree")
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=str(_ROOT),
        capture_output=True,
    )
    # check-ignore exits 0 when ignored, 1 when not ignored.
    assert result.returncode == 1, f"{rel} is gitignored and would not ship"
