"""Machine gate: a ```bash fence is a promise that the block runs.

Blocks labelled ``bash`` in ``README.md``, ``AGENTS.md``, ``pages/*.md`` and
``.agents/notes/**/*.md`` are extracted and each one must pass ``bash -n``
(parse, without executing).  Copy-paste rot is invisible to review — a stale flag
or a missing quote only shows up when a reader runs it — so the parser is the
reviewer here.

A block that only *shows* a shape is not runnable: a ``<serial>``/``<run_dir>``
placeholder cannot be pasted into a shell, and a deliberately elided step is not
a command either.  Those blocks belong behind a ```text fence, and this gate
refuses angle-bracket placeholders inside ```bash blocks for exactly that reason
— give a concrete example instead, or drop the fence down to ```text.

``bash -n`` proves syntax, not semantics: a block that parses can still fail at
run time (a renamed flag, a missing file).  The gate catches the cheaper half of
the rot; the expensive half stays with the readers who run the commands.

Only the standard library and pytest may be imported here.  The ci.yml docs job
installs nothing beyond ``pytest`` (no ``requirements.txt``).  ``bash`` itself is
assumed present; when it is missing the gate fails instead of passing quietly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# tests/docs/test_doc_bash_blocks.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every document set that ships runnable examples.  `docs/` and `phone_agent/`
# internals are out of scope: they are operator-local, not published handbook.
_DOCUMENT_GLOBS = (
    "README.md",
    "AGENTS.md",
    "pages/*.md",
    ".agents/notes/**/*.md",
)

_FENCE_RE = re.compile(
    r"^[ \t]*(?P<fence>```+|~~~+)[ \t]*(?P<info>[^\n]*?)[ \t]*\n"
    r"(?P<body>.*?)"
    r"^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# `<serial>`, `<run_dir>`, `<目录>`: a substitutable placeholder, not a command.
_PLACEHOLDER_RE = re.compile(
    r"<[ \t]*(?:[A-Za-z_][A-Za-z0-9_\- ]*|[\u4e00-\u9fff][^<>]*?)[ \t]*>"
)


def _relative(document: Path) -> str:
    return document.relative_to(_REPO_ROOT).as_posix()


def documents() -> list[Path]:
    """Every scanned document, repo-relative paths sorted."""

    found: set[Path] = set()
    for pattern in _DOCUMENT_GLOBS:
        found.update(path for path in _REPO_ROOT.glob(pattern) if path.is_file())
    return sorted(found)


def bash_blocks(markdown: str) -> list[tuple[int, str]]:
    """``(line number of the fence, block body)`` for every ```bash fence."""

    return [
        (markdown.count("\n", 0, match.start()) + 1, match.group("body"))
        for match in _FENCE_RE.finditer(markdown.replace("\r\n", "\n"))
        if match.group("info").strip().split(" ")[0] == "bash"
    ]


def placeholders(body: str) -> list[str]:
    """Angle-bracket placeholders found in *body*, in order of appearance."""

    return _PLACEHOLDER_RE.findall(body)


def _bash() -> str:
    found = shutil.which("bash")
    if found is None:  # pragma: no cover - CI and dev machines ship bash
        pytest.fail("`bash` is not on PATH; the docs gate cannot verify code blocks")
    return found


def syntax_error(body: str, scratch: Path) -> str | None:
    """``bash -n`` complaint about *body*, or ``None`` when it parses."""

    script = scratch / "block.sh"
    script.write_text(body + "\n", encoding="utf-8")
    result = subprocess.run(
        [_bash(), "-n", str(script)], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or "bash -n failed without a message"


_DOCUMENTS = documents()


def test_the_bash_scan_covers_the_documents() -> None:
    """Guard against the check silently passing because it scans nothing."""

    names = {_relative(document) for document in _DOCUMENTS}
    assert {"README.md", "AGENTS.md"} <= names, (
        "the scanned document set looks truncated\n"
        f"  found : {sorted(names)}\n"
        "  fix   : keep README.md and AGENTS.md in _DOCUMENT_GLOBS"
    )
    total = sum(len(bash_blocks(document.read_text(encoding="utf-8"))) for document in _DOCUMENTS)
    assert total >= 8, f"only {total} bash blocks found across the documentation set"


@pytest.mark.parametrize("document", _DOCUMENTS, ids=_relative)
def test_bash_blocks_parse(document: Path, tmp_path: Path) -> None:
    """Every ```bash block is syntactically valid shell."""

    failures = []
    for line, body in bash_blocks(document.read_text(encoding="utf-8")):
        complaint = syntax_error(body, tmp_path)
        if complaint is not None:
            failures.append((line, complaint))
    if failures:
        pytest.fail(
            "documented bash block does not parse\n"
            f"  file : {_relative(document)}\n"
            + "\n".join(
                f"  line {line:>4} : {complaint.splitlines()[-1].strip()}"
                for line, complaint in failures
            )
            + "\n  fix  : repair the command, or relabel the fence as ```text"
        )


@pytest.mark.parametrize("document", _DOCUMENTS, ids=_relative)
def test_bash_blocks_contain_no_placeholders(document: Path) -> None:
    """A placeholder means the block is a shape, not a runnable command."""

    offenders = []
    for line, body in bash_blocks(document.read_text(encoding="utf-8")):
        offenders.extend((line, placeholder) for placeholder in placeholders(body))
    if offenders:
        pytest.fail(
            "documented bash block carries a placeholder\n"
            f"  file : {_relative(document)}\n"
            + "\n".join(f"  line {line:>4} : {placeholder}" for line, placeholder in offenders)
            + "\n  fix  : give a concrete example, or relabel the fence as ```text"
        )


def test_only_bash_fences_are_extracted() -> None:
    """The extractor reads the info string; other fences are left alone."""

    markdown = (
        "# doc\n\n"
        "```bash\necho ok\n```\n\n"
        "```text\n<serial>\n```\n\n"
        "```markdown\n# Agent Note: 标题\n```\n\n"
        "```python\nprint('ok')\n```\n\n"
        "```bash\n.venv/bin/pytest tests -q\n```\n"
    )
    assert bash_blocks(markdown) == [(3, "echo ok\n"), (19, ".venv/bin/pytest tests -q\n")]


def test_syntax_errors_are_reported(tmp_path: Path) -> None:
    """`bash -n` really is the judge: a broken block must not pass."""

    assert syntax_error("echo ok\n", tmp_path) is None
    assert syntax_error('if true\n  echo "unterminated\n', tmp_path) is not None


def test_placeholders_are_detected() -> None:
    assert placeholders("main_v2.py --device-id <serial>\n") == ["<serial>"]
    assert placeholders('"$S" monitor <run_dir>\n') == ["<run_dir>"]
    assert placeholders("main_v2.py --device-id <设备编号>\n") == ["<设备编号>"]
    assert placeholders(".venv/bin/python -m phone_agent.web --port 8080\n") == []
