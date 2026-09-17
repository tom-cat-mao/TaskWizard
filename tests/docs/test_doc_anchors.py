"""Doc anchor contract: every ``*.md#anchor`` link lands on a declared anchor.

Anchor ids are an external contract.  The P0 table in ``AGENTS.md`` links each
rule to a contract body in ``pages/`` through an explicit attr_list anchor
(``### 标题 {#coords}``), and the published site's deep links resolve through
the same ids, so retitling a section is free while renaming its id breaks
readers that already hold the link.  ``test_doc_references.py`` checks the
*target file* of every reference but skips tokens containing ``#``, leaving
anchor existence to memory; this gate closes that hole.

Only explicit ``{#anchor-id}`` anchors count.  MkDocs also derives slugs from
headings, but those are an implementation detail of the renderer (they move
when the heading is translated), so the contract is the attr_list literal and
nothing else.  A reference resolves like the renderer resolves it: relative to
the directory of the document that carries the link (``#coords`` is the same
file, ``architecture.md#coords`` from another page, ``../../pages/*.md#id``
from a two-level subtree, ``pages/*.md#id`` from the repository root).

Fenced code blocks are stripped first — an anchor shown inside a fence is an
example, not a declaration or a link — and so are HTML comments, which render
nowhere and so contract nothing (``<!-- allow:不再 -->`` exemptions are
comments, not text).  ``EXTERNAL_ANCHOR_OK`` below covers the rare pointer
whose target deliberately lives outside the repository; the set is empty today
and every entry needs a one-line reason.

Only the standard library and pytest may be imported here.  The ci.yml docs
job installs nothing beyond ``pytest`` (no ``requirements.txt``), so importing
``phone_agent`` or any third-party helper would break the gate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

import pytest

# tests/docs/test_doc_anchors.py -> parents[2] is the repository root (worktree).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Entry points and subtree guides, plus every published handbook page.
_DOCUMENTS = (
    "AGENTS.md",
    "README.md",
    "docs/future-roadmap.md",
    "pages/AGENTS.md",
    "phone_agent/v2/AGENTS.md",
    "phone_agent/web/AGENTS.md",
    "tests/AGENTS.md",
)

# A reference is a markdown link or a backticked token; only these carry the
# ``target#anchor`` / ``#anchor`` shape.  Anchor ids in this repository are
# lowercase kebab (``{#windowed-marks}``), so the pattern stays narrow enough to
# leave ``#L10``-style line anchors and ``[P0 #23]`` link *text* alone.
_FRAGMENT_RE = re.compile(r"^(?P<target>[^#\s]*\.md)?#(?P<anchor>[a-z][a-z0-9-]*)$")

# ``[text](target)`` / ``[text](target "title")``; the URL is captured without
# the optional whitespace-separated title.
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s[^)]*)?\)")
_BACKTICK_RE = re.compile(r"`(?P<token>[^`\n]+)`")

# Fenced blocks are examples; they declare no anchors and carry no links.
_FENCED_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<fence>```+|~~~+)[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# HTML comments render nowhere, so neither their links nor their `{#id}`s count
# (same stripping as test_doc_budgets.py).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_ANCHOR_DECLARATION_RE = re.compile(r"\{#(?P<anchor>[a-z][a-z0-9-]*)\}")

# Anchor links whose target a fresh clone deliberately does not contain — an
# external document, or a file the repository points at but does not own.  Each
# entry is a claim that the anchor cannot be machine-checked here; the reason is
# mandatory and `test_whitelist_entries_are_argumented_and_still_needed` refuses
# stale entries.  Empty is the healthy state.
EXTERNAL_ANCHOR_OK: dict[str, str] = {}


def _body(markdown: str) -> str:
    """The reading view of *markdown*: comments and fenced blocks removed."""

    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    return _FENCED_BLOCK_RE.sub(" ", _HTML_COMMENT_RE.sub(" ", text))


def _anchor_fragment(token: str) -> tuple[str, str] | None:
    """``(target, anchor)`` for a reference token, or ``None`` when it is not one.

    ``target`` is ``""`` for a same-file reference (``#coords``).
    """

    token = token.strip().strip("`").rstrip(".,;:，。；、）)")
    if token.startswith(("http://", "https://", "mailto:")):
        return None
    match = _FRAGMENT_RE.match(token)
    if match is None:
        return None
    return match.group("target") or "", match.group("anchor")


def anchors_in(markdown: str) -> list[tuple[str, str]]:
    """Every ``(target, anchor)`` reference in *markdown*, in reading order."""

    body = _body(markdown)
    hits: list[tuple[int, tuple[str, str]]] = []
    for match in _BACKTICK_RE.finditer(body):
        fragment = _anchor_fragment(match.group("token"))
        if fragment is not None:
            hits.append((match.start(), fragment))
    for match in _MARKDOWN_LINK_RE.finditer(body):
        fragment = _anchor_fragment(match.group("target"))
        if fragment is not None:
            hits.append((match.start(), fragment))
    found: list[tuple[str, str]] = []
    for _, fragment in sorted(hits, key=lambda hit: hit[0]):
        if fragment not in found:
            found.append(fragment)
    return found


def resolve_target(document: str, target: str, *, root: Path = REPO_ROOT) -> Path:
    """The file a reference in *document* points at (relative, like the renderer)."""

    if not target:
        return root / document
    # ``..`` segments are collapsed lexically: the target may not exist yet.
    return Path(os.path.normpath((root / document).parent / PurePosixPath(target)))


def declared_anchors(path: Path) -> set[str]:
    """Anchor ids declared by the document at *path* (``{#id}`` literals)."""

    return set(_ANCHOR_DECLARATION_RE.findall(_body(path.read_text(encoding="utf-8"))))


def violations(
    document: str, markdown: str, *, root: Path = REPO_ROOT
) -> list[tuple[str, str]]:
    """``(reference, reason)`` for every anchor reference in *document* that fails."""

    found: list[tuple[str, str]] = []
    for target, anchor in anchors_in(markdown):
        written = f"{target}#{anchor}"
        if written in EXTERNAL_ANCHOR_OK:
            continue
        path = resolve_target(document, target, root=root)
        if not path.is_relative_to(root):
            found.append((written, "target lives outside the repository"))
            continue
        if not path.is_file():
            found.append((written, f"target file not found ({path.relative_to(root).as_posix()})"))
            continue
        if anchor not in declared_anchors(path):
            found.append(
                (written, f"anchor not declared in {path.relative_to(root).as_posix()}")
            )
    return found


def _document_paths() -> list[str]:
    """The checked documents: the fixed entry points plus every ``pages/*.md``."""

    pages = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "pages").glob("*.md")
        if path.is_file()
    )
    ordered = [name for name in _DOCUMENTS if (REPO_ROOT / name).is_file()]
    return ordered + [name for name in pages if name not in ordered]


def _document_text(document: str) -> str:
    return (REPO_ROOT / document).read_text(encoding="utf-8")


_DOCUMENT_PATHS = _document_paths()
_ALL_REFERENCES = [
    (document, f"{target}#{anchor}")
    for document in _DOCUMENT_PATHS
    for target, anchor in anchors_in(_document_text(document))
]


def test_the_anchor_scan_covers_the_document_set() -> None:
    """Guard against the check silently passing because it scans nothing."""

    assert _DOCUMENT_PATHS, "no documents to scan"
    assert len(_ALL_REFERENCES) >= 20, (
        f"only {len(_ALL_REFERENCES)} anchor references found across {len(_DOCUMENT_PATHS)} documents\n"
        "  fix : point this gate at the documents that carry the links (P0 table, pages/, subtree AGENTS.md)"
    )
    assert len({document for document, _ in _ALL_REFERENCES}) >= 3, (
        "anchor references come from fewer than three documents; the scan surface looks wrong"
    )


@pytest.mark.parametrize("document", _DOCUMENT_PATHS)
def test_document_anchor_references_resolve(document: str) -> None:
    """Every ``#anchor`` written in *document* is declared by its target file."""

    found = violations(document, _document_text(document))
    assert found == [], (
        f"{document} links to anchors no document declares:\n"
        + "\n".join(f"  {written} -> {reason}" for written, reason in found)
        + "\n  fix : declare the id as `### 标题 {#anchor}` in the target page, or point"
        "\n        the link at an existing id (anchor ids are an external contract)"
    )


def test_whitelist_entries_are_argumented_and_still_needed() -> None:
    """EXTERNAL_ANCHOR_OK stays honest: a reference that really is written, with a reason."""

    written = {reference for _, reference in _ALL_REFERENCES}
    for reference, reason in EXTERNAL_ANCHOR_OK.items():
        assert _anchor_fragment(reference) is not None, (
            f"external-anchor whitelist entry is not a `target#anchor` reference: {reference!r}"
        )
        assert reason.strip(), f"external-anchor whitelist entry {reference} needs a one-line reason"
        assert reference in written, (
            f"external-anchor whitelist entry {reference} is not referenced any more — drop it"
        )


@pytest.mark.parametrize(
    "token",
    [
        "pages/architecture.md",  # no fragment at all
        "[P0 #23](AGENTS.md)",  # the `#` is link text, not a fragment
        "https://tom-cat-mao.github.io/TaskWizard/architecture/#coords",  # external URL
        "mailto:someone@example.com",  # not a document
        "#L10",  # a line anchor's fragment, not a lowercase document anchor
        "text",  # prose token
        "v2/coords.py#L10",  # line anchor on a source file
    ],
)
def test_tokens_that_are_not_document_anchors_are_skipped(token: str) -> None:
    assert anchors_in(f"见 `{token}`。") == []


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("#coords", ("", "coords")),
        ("architecture.md#coords", ("architecture.md", "coords")),
        ("../../pages/console.md#web-projection", ("../../pages/console.md", "web-projection")),
        ("pages/configuration.md#token-budget", ("pages/configuration.md", "token-budget")),
    ],
)
def test_document_anchor_tokens_are_extracted(token: str, expected: tuple[str, str]) -> None:
    assert anchors_in(f"见 `{token}`。") == [expected]


def test_markdown_links_and_backticks_are_both_read() -> None:
    markdown = (
        "见 [契约](pages/architecture.md#coords)、[自链](#marks-first) 与"
        " `phone_agent/v2/AGENTS.md#whatever`，标题里的 [P0 #23](AGENTS.md) 不算。"
    )
    assert anchors_in(markdown) == [
        ("pages/architecture.md", "coords"),
        ("", "marks-first"),
        ("phone_agent/v2/AGENTS.md", "whatever"),
    ]


def test_fenced_examples_are_not_links_or_declarations() -> None:
    markdown = "```markdown\n[示例](pages/architecture.md#coords)\n### 标题 {#coords}\n```\n"
    assert anchors_in(markdown) == []
    assert _body("```\n### 标题 {#coords}\n```\n").strip() == ""


def test_html_comments_are_not_links_or_declarations() -> None:
    """Comments render nowhere: their links and ids are outside the contract."""

    markdown = "正文 <!-- [隐藏](pages/architecture.md#coords)、{#coords} --> 之后。"
    assert anchors_in(markdown) == []
    assert _body("<!-- {#coords} -->").strip() == ""


def test_an_anchor_declared_only_in_a_comment_does_not_count(tmp_path: Path) -> None:
    """A `{#id}` a reader cannot see is not a declaration the gate accepts."""

    (tmp_path / "page.md").write_text("<!-- {#hidden} -->\n", encoding="utf-8")
    assert dict(violations("page.md", "[x](page.md#hidden)", root=tmp_path)) == {
        "page.md#hidden": "anchor not declared in page.md",
    }


def test_relative_targets_resolve_like_the_renderer() -> None:
    assert resolve_target("pages/architecture.md", "architecture.md") == (
        REPO_ROOT / "pages" / "architecture.md"
    )
    assert resolve_target("pages/architecture.md", "") == REPO_ROOT / "pages" / "architecture.md"
    assert resolve_target("AGENTS.md", "pages/architecture.md") == REPO_ROOT / "pages" / "architecture.md"
    assert resolve_target("phone_agent/v2/AGENTS.md", "../../AGENTS.md") == REPO_ROOT / "AGENTS.md"
    assert resolve_target("phone_agent/web/AGENTS.md", "../../pages/console.md") == (
        REPO_ROOT / "pages" / "console.md"
    )


def test_missing_anchors_are_reported_not_skipped(tmp_path: Path) -> None:
    """The gate has teeth: a dangling fragment is a violation, a declared one is not."""

    (tmp_path / "page.md").write_text("### 已声明 {#real}\n", encoding="utf-8")
    assert violations("page.md", "[x](page.md#real)", root=tmp_path) == []

    markdown = "[x](page.md#gone) 与 `missing.md#real` 与 `../../outside.md#real`"
    found = dict(violations("page.md", markdown, root=tmp_path))
    assert found["page.md#gone"] == "anchor not declared in page.md"
    assert found["missing.md#real"] == "target file not found (missing.md)"
    assert found["../../outside.md#real"] == "target lives outside the repository"
