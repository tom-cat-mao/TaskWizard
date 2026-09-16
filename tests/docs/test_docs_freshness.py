"""Machine gate: ``pages/`` describes the present, never the change history.

A user manual that narrates "this used to be X, now it is Y" rots twice over:
the comparison is useless to a reader who never saw X, and it dates every page
it touches.  So the change-narration vocabulary is refused in ``pages/*.md``:
``以前`` / ``不再`` / ``改名为`` / ``现已`` / ``旧版`` / ``新版`` / ``原本`` /
``曾经``.

Behaviour sentences use some of these words legitimately — "耗尽后不再重试" is a
rule, not a history lesson — so a page may clear *that word on that line* with an
HTML comment on the same line or the line above:

    <!-- allow:不再 -->

The exemption is scoped to one line and stays visible in the source, so it cannot
quietly cover the rest of the page, and the rendered site does not show it.
Whether a cleared line is a rule or a rewrite waiting to happen is a judgement
call: the gate only refuses *unexplained* change narration.

Only the standard library and pytest may be imported here.  The ci.yml docs job
installs nothing beyond ``pytest`` (no ``requirements.txt``), so importing
``phone_agent`` or any third-party helper would break the gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/docs/test_docs_freshness.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAGES_DIR = _REPO_ROOT / "pages"

_BANNED_WORDS = ("以前", "不再", "改名为", "现已", "旧版", "新版", "原本", "曾经")

# `<!-- allow:不再 -->`, optionally clearing several words: `<!-- allow:不再,新版 -->`.
_ALLOW_RE = re.compile(r"<!--\s*allow:[ \t]*(?P<words>[^>]*?)[ \t]*-->")
_WORD_SPLIT_RE = re.compile(r"[,\s、]+")


def _relative(page: Path) -> str:
    return page.relative_to(_REPO_ROOT).as_posix()


def _page_paths() -> list[Path]:
    if not _PAGES_DIR.is_dir():
        return []
    return sorted(path for path in _PAGES_DIR.glob("*.md") if path.is_file())


def _allowed_words(line: str) -> set[str]:
    """Words cleared by an ``allow:`` comment on *line*."""

    allowed: set[str] = set()
    for match in _ALLOW_RE.finditer(line):
        allowed.update(word for word in _WORD_SPLIT_RE.split(match.group("words")) if word)
    return allowed


def violations(markdown: str) -> list[tuple[int, str, str]]:
    """``(line number, banned word, stripped line)`` for unexempted hits."""

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found: list[tuple[int, str, str]] = []
    previous = ""
    for number, line in enumerate(lines, start=1):
        allowed = _allowed_words(line) | _allowed_words(previous)
        found.extend(
            (number, word, line.strip())
            for word in _BANNED_WORDS
            if word in line and word not in allowed
        )
        previous = line
    return found


_PAGES = _page_paths()


def test_the_freshness_scan_covers_the_page_set() -> None:
    """Guard against the check silently passing because it scans nothing."""

    names = {page.name for page in _PAGES}
    assert _PAGES_DIR.is_dir(), f"documentation pages directory is missing: {_PAGES_DIR}"
    assert {"architecture.md", "configuration.md"} <= names, (
        "the page set looks truncated\n"
        f"  found : {sorted(names)}\n"
        "  fix   : point this gate at the rendered handbook (pages/*.md)"
    )


@pytest.mark.parametrize("page", _PAGES, ids=_relative)
def test_pages_avoid_change_narration(page: Path) -> None:
    """A page states the current contract; it does not compare against old ones."""

    hits = violations(page.read_text(encoding="utf-8"))
    if hits:
        pytest.fail(
            "page narrates a change instead of the current state\n"
            f"  file : {_relative(page)}\n"
            + "\n".join(
                f"  line {number:>4} : 「{word}」 in: {text}"
                for number, word, text in hits
            )
            + "\n  fix  : state the present rule, or clear a behaviour word with"
            "\n         `<!-- allow:<word> -->` on that line (or the line above)"
        )


def test_change_narration_is_detected() -> None:
    """The gate has teeth: prove it reads the words it refuses."""

    assert violations("旧版仍可用。") == [(1, "旧版", "旧版仍可用。")]
    assert violations("这个键以前叫别的名字。") == [(1, "以前", "这个键以前叫别的名字。")]
    assert violations("默认值不变，行为不变。") == []


def test_allow_comment_clears_that_word_on_that_line_or_the_line_above() -> None:
    assert violations("不再重试。<!-- allow:不再 -->") == []
    assert violations("<!-- allow:不再 -->\n不再重试。") == []
    assert violations("<!-- allow:不再,新版 -->\n不再重试，也不看新版。") == []


def test_allow_comment_reaches_no_further_than_one_line_and_one_word() -> None:
    # an exemption for another word on the same line clears nothing
    assert violations("旧版仍可用。<!-- allow:新版 -->") == [
        (1, "旧版", "旧版仍可用。<!-- allow:新版 -->")
    ]
    # the comment sits below the hit, so it clears nothing
    assert violations("不再重试。\n<!-- allow:不再 -->") == [(1, "不再", "不再重试。")]
    # the exemption covers its own line and the next, never the one after that
    assert violations("<!-- allow:不再 -->\n不再重试。\n曾经可用。") == [
        (3, "曾经", "曾经可用。")
    ]
