"""Machine gate: word budgets for the repository documentation set.

``doc-budgets.json`` maps a repo-relative Markdown file to the maximum number of
words it may contain.  The handbook is written mostly in Chinese, so the metric
is CJK aware:

* every CJK character counts as one word;
* every maximal run of latin letters / digits counts as one word.

Fenced code blocks and HTML comments are stripped before counting, so sample
commands and commented-out drafts do not consume budget; inline code, tables and
link targets count as written.

Only the standard library and pytest may be imported here.  The ``pages.yml``
workflow runs this file with nothing installed beyond ``pytest`` (no
``requirements.txt``), so importing ``phone_agent`` would break the gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# tests/docs/test_doc_budgets.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = Path(__file__).resolve().parent / "doc-budgets.json"

# One word per CJK character: Han, kana and Hangul, including the extension and
# compatibility blocks.  CJK punctuation (U+3000-U+303F) stays uncounted.
_CJK_CHAR_RE = re.compile(
    "["
    "\u1100-\u11ff"  # Hangul Jamo
    "\u3040-\u30ff"  # Hiragana + Katakana
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "]"
)
# One word per maximal latin/digit run; inner _ - . ' keep identifiers in one word.
_LATIN_WORD_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-'.]*")
_FENCED_CODE_RE = re.compile(
    r"^[ \t]*(?P<fence>```+|~~~+)[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def count_words(markdown: str) -> int:
    """Number of words in ``markdown`` under the CJK-aware metric."""

    text = _HTML_COMMENT_RE.sub(" ", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    text = _FENCED_CODE_RE.sub(" ", text)
    return len(_CJK_CHAR_RE.findall(text)) + len(_LATIN_WORD_RE.findall(text))


def _load_budgets() -> dict[str, int]:
    """Read the manifest; keys starting with ``_`` document it and are ignored."""

    raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    budgets: dict[str, int] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{_MANIFEST.name}: budget for {key!r} must be a positive integer, got {value!r}")
        budgets[key] = value
    if not budgets:
        raise ValueError(f"{_MANIFEST.name}: no document budgets declared")
    return budgets


_BUDGETS = _load_budgets()


@pytest.mark.parametrize(("relative_path", "budget"), sorted(_BUDGETS.items()), ids=sorted(_BUDGETS))
def test_document_within_word_budget(relative_path: str, budget: int) -> None:
    """A document may not exceed its manifest budget (report: file/budget/actual/excess)."""

    document = _REPO_ROOT / relative_path
    if not document.is_file():
        pytest.fail(
            f"budgeted document is missing\n  file   : {relative_path}\n  looked : {document}\n"
            f"  fix    : restore the file or drop its entry from tests/docs/{_MANIFEST.name}"
        )

    actual = count_words(document.read_text(encoding="utf-8"))
    excess = actual - budget
    if excess > 0:
        pytest.fail(
            "document exceeds its word budget\n"
            f"  file   : {relative_path}\n"
            f"  budget : {budget} words\n"
            f"  actual : {actual} words\n"
            f"  excess : {excess} words ({excess / budget:.1%} over)\n"
            f"  fix    : trim the document, or raise its entry in tests/docs/{_MANIFEST.name}"
        )
