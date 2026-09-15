"""Machine gate: the decision-note format ``.agents/notes/AGENTS.md`` prescribes.

Notes are this repository's "why" record, and their value depends on the shape
being predictable: ``YYYY-MM-DD-slug.md``, a ``Status:`` line that agrees with
the folder the note lives in (moving the file *is* the state transition), and
four sections — Problem, Decision, Alternatives considered, Consequences — in
that order.  A note filed under ``implemented/`` records what was decided and
what happened, so a heading that announces work still to come (``## Proposal``,
``## 计划``) is refused there; such a note belongs in ``proposed/``.

An ``INDEX.md`` never exists anywhere under ``.agents/notes/``: state is the
folder, and a hand-kept index is a second source of truth that drifts.

Only the standard library and pytest may be imported here.  The ci.yml docs job
installs nothing beyond ``pytest`` (no ``requirements.txt``), so importing
``phone_agent`` or any third-party helper would break the gate.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

# tests/docs/test_notes_format.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOTES_ROOT = _REPO_ROOT / ".agents" / "notes"

# The four status folders AGENTS.md defines.  The folder is authoritative; the
# ``Status:`` line inside the note has to agree with it.
_STATUS_BY_FOLDER = {
    "proposed": "proposed",
    "implemented": "implemented",
    "rejected": "rejected",
    "archived": "archived",
}

_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
_STATUS_RE = re.compile(r"^Status:[ \t]*(?P<status>\S+)[ \t]*$", re.MULTILINE)
_LEVEL2_RE = re.compile(r"^##[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{2,3}[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)

_REQUIRED_SECTIONS = ("Problem", "Decision", "Alternatives considered", "Consequences")

# Headings that promise future work.  ``implemented/`` notes are frozen to what
# happened, so these are refused there (``proposed/`` is where they belong).
_FUTURE_HEADING_RE = re.compile(
    r"(?i)\b(?:proposal|proposed|plan|plans|planned|planning|todo|todos|"
    r"next steps?|roadmap|backlog|future work)\b"
    r"|计划|提案|待办|下一步|后续安排|路线图"
)


def _note_paths() -> list[Path]:
    """Every decision note; ``AGENTS.md`` is the format spec, not a note."""

    if not _NOTES_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in _NOTES_ROOT.rglob("*.md")
        if path.is_file() and path.name != "AGENTS.md"
    )


def _relative(note: Path) -> str:
    return note.relative_to(_REPO_ROOT).as_posix()


def _folder_of(note: Path) -> str:
    """The status folder holding *note*, or ``""`` when it is not filed under one."""

    parts = note.relative_to(_NOTES_ROOT).parts
    return parts[0] if len(parts) > 1 else ""


def _status_of(text: str) -> list[str]:
    return [match.group("status") for match in _STATUS_RE.finditer(text)]


def _section_titles(text: str) -> list[str]:
    return [match.group("title") for match in _LEVEL2_RE.finditer(text)]


def _future_headings(text: str) -> list[tuple[int, str]]:
    """``(line number, title)`` for every heading promising work still to come."""

    return [
        (text.count("\n", 0, match.start()) + 1, match.group("title"))
        for match in _HEADING_RE.finditer(text)
        if _FUTURE_HEADING_RE.search(match.group("title"))
    ]


_NOTES = _note_paths()
_IMPLEMENTED_NOTES = [note for note in _NOTES if _folder_of(note) == "implemented"]


def test_the_note_scan_finds_notes() -> None:
    """Guard against the check silently passing because it scans nothing."""

    assert _NOTES_ROOT.is_dir(), f"decision notes root is missing: {_NOTES_ROOT}"
    assert _NOTES, (
        f"no decision notes found under {_NOTES_ROOT}\n"
        "  fix : restore the notes, or point this gate at their new home"
    )


@pytest.mark.parametrize("note", _NOTES, ids=_relative)
def test_note_filename_is_a_date_and_a_slug(note: Path) -> None:
    """``YYYY-MM-DD-slug.md``: a real ISO date plus a lowercase kebab slug."""

    match = _FILENAME_RE.match(note.name)
    if match is None:
        pytest.fail(
            "note file name is not `YYYY-MM-DD-slug.md`\n"
            f"  file : {_relative(note)}\n"
            f"  name : {note.name}\n"
            "  fix  : rename to the day the note was written plus a lowercase kebab slug"
        )
    try:
        datetime.date.fromisoformat(match.group("date"))
    except ValueError:
        pytest.fail(
            "note file name carries an impossible date\n"
            f"  file : {_relative(note)}\n"
            f"  date : {match.group('date')}\n"
            "  fix  : use a real calendar date (YYYY-MM-DD)"
        )


@pytest.mark.parametrize("note", _NOTES, ids=_relative)
def test_note_is_filed_under_its_status_folder(note: Path) -> None:
    """The folder decides the state and the ``Status:`` line must repeat it."""

    folder = _folder_of(note)
    expected = _STATUS_BY_FOLDER.get(folder)
    if expected is None:
        pytest.fail(
            "note does not live in one of the status folders\n"
            f"  file   : {_relative(note)}\n"
            f"  folder : {folder or '.agents/notes/ (root)'}\n"
            f"  fix    : `git mv` it into one of {', '.join(sorted(_STATUS_BY_FOLDER))}/"
        )

    statuses = _status_of(note.read_text(encoding="utf-8"))
    if len(statuses) != 1:
        pytest.fail(
            "note must carry exactly one `Status:` line\n"
            f"  file     : {_relative(note)}\n"
            f"  statuses : {statuses or 'none'}\n"
            f"  fix      : write a single `Status: {expected}` line under the title"
        )
    if statuses[0] != expected:
        pytest.fail(
            "note status disagrees with its folder (the folder wins)\n"
            f"  file   : {_relative(note)}\n"
            f"  folder : {folder}/ -> Status: {expected}\n"
            f"  status : {statuses[0]}\n"
            f"  fix    : correct the `Status:` line, or `git mv` the note to {statuses[0]}/"
        )


@pytest.mark.parametrize("note", _NOTES, ids=_relative)
def test_note_has_the_four_sections_in_order(note: Path) -> None:
    """Problem, Decision, Alternatives considered, Consequences — all present."""

    titles = _section_titles(note.read_text(encoding="utf-8"))
    missing = [section for section in _REQUIRED_SECTIONS if section not in titles]
    ordered = [title for title in titles if title in _REQUIRED_SECTIONS]
    if missing or ordered != sorted(ordered, key=_REQUIRED_SECTIONS.index):
        pytest.fail(
            "note does not follow the four-section format\n"
            f"  file    : {_relative(note)}\n"
            f"  found   : {ordered or 'no `## ` sections'}\n"
            f"  missing : {missing or 'none'}\n"
            "  fix     : use `## Problem`, `## Decision`, `## Alternatives considered`,\n"
            "            `## Consequences` in that order (.agents/notes/AGENTS.md)"
        )


@pytest.mark.parametrize("note", _IMPLEMENTED_NOTES, ids=_relative)
def test_implemented_note_states_only_what_happened(note: Path) -> None:
    """``implemented/`` records the decision, not a plan for the next one."""

    offenders = _future_headings(note.read_text(encoding="utf-8"))
    if offenders:
        pytest.fail(
            "implemented note announces future work (implemented notes record what happened)\n"
            f"  file     : {_relative(note)}\n"
            + "\n".join(f"  line {line:>4} : {title}" for line, title in offenders)
            + "\n  fix      : rewrite it as fact, or move the note to proposed/"
        )


def test_no_index_file_under_notes() -> None:
    """State is the folder; a hand-kept index is a second source of truth."""

    found = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _NOTES_ROOT.rglob("*")
        if path.is_file() and path.name.lower() == "index.md"
    )
    assert found == [], (
        "an index file exists under .agents/notes/:\n"
        + "\n".join(f"  {path}" for path in found)
        + "\n  fix : delete it — the status folder decides the state, `git mv` migrates it"
    )


def test_future_heading_detector_matches_planned_sections() -> None:
    """The `implemented/` rule has teeth: prove the detector reads headings."""

    assert _future_headings("## Proposal\n") == [(1, "Proposal")]
    assert _future_headings("### 计划\n") == [(1, "计划")]
    assert _future_headings("## TODO: wire the gate\n") == [(1, "TODO: wire the gate")]
    assert _future_headings("## Decision\n\n## Consequences\n") == []


def test_status_and_section_detectors_read_the_format_they_check() -> None:
    sample = "# Agent Note: x\n\nStatus: implemented\n\n## Problem\n\n…\n"

    assert _status_of(sample) == ["implemented"]
    assert _section_titles(sample) == ["Problem"]
    assert _status_of("# Agent Note: x\n\n## Problem\n") == []
