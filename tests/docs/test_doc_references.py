"""Doc reference contract: paths named by the tracked docs must resolve.

``AGENTS.md`` / ``README.md`` / ``docs/future-roadmap.md`` are the entry points a
fresh clone is read through, and the two subtree ``AGENTS.md`` files
(``pages/``, ``phone_agent/v2/``) carry the same obligation to the readers who
enter those directories, so every repository path they name has to exist —
otherwise a reader (human or coding agent) is pointed at a file that is not
there.  The check covers backticked tokens and inline markdown links and
resolves the shorthands ``AGENTS.md`` defines at the top of the file:
``v2/...`` = ``phone_agent/v2/...``, ``middleware/...`` =
``phone_agent/v2/middleware/...``, ``adb/``/``grounding/``/``config/`` under
``phone_agent/``.  A bare module name (``native_content.py``) resolves against
the whole tree, because the docs name modules, not only paths from the root.

Tokens that are not repository references are skipped rather than rewritten:
commands ending in an executable name (``.venv/bin/python``), env vars and
config keys (``PHONE_AGENT_TOKEN_BUDGET``, ``ctx.on``,
``roles.safety_reviewer.model``), event topics (``tool/execute``), branch
names, globs, anchored symbols (``module.py::func``), line numbers, listen
addresses and the ``v2/…`` ellipsis.  Two explicit sets below cover tokens
whose targets are absent on purpose; keeping them short is the point —
everything else must be real.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path, PurePosixPath

import pytest

# ``tests/docs/test_doc_references.py`` -> repo root, independent of the cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]

_DOCUMENTS = (
    "AGENTS.md",
    "README.md",
    "docs/future-roadmap.md",
    "pages/AGENTS.md",
    "phone_agent/v2/AGENTS.md",
)

# AGENTS.md header: `v2/...` means phone_agent/v2/..., `middleware/...` means
# phone_agent/v2/middleware/..., `adb/`/`grounding/`/`config/` live under
# phone_agent/.  Trying the bases in order also resolves module paths that are
# named relative to `v2/` alone (e.g. `providers/builders.py`).
_BASE_DIRS = (
    REPO_ROOT,
    REPO_ROOT / "phone_agent",
    REPO_ROOT / "phone_agent" / "v2",
)

# Never walked when resolving a bare module name: private runtime state, build
# output and VCS metadata.
_WALK_SKIP_DIRS = frozenset(
    {".git", ".venv", "site", "node_modules", "memory", "outputs"}
)

_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_BACKTICK = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_BRACE_GROUP = re.compile(r"\{([^{}]*)\}")
_ANCHORED = re.compile(r"::|:\d+\b")
_NUMERIC_HOST = re.compile(r"\d{1,3}(?:\.\d{1,3}){1,3}")  # 127.0.0.1, not a file name

# Characters that make a token something other than a single path: whitespace,
# quoting and placeholders, globs, shell pipes, config-key equals, markdown
# syntax, and the `…` used in the shorthand notes.
_NOT_A_PATH = frozenset(" \t'\"<>|*?=()[]#…")

# A backticked token counts as a file reference when it ends in one of these,
# so dotted identifiers (`ctx.on`, `compat.supportsUsageInStreaming`) stay out.
_FILE_SUFFIXES = frozenset(
    {
        "md",
        "py",
        "json",
        "jsonl",
        "toml",
        "yaml",
        "yml",
        "txt",
        "html",
        "sh",
        "cfg",
        "ini",
        "apk",
        "csv",
    }
)

# Tokens whose target a fresh clone deliberately does not contain.  Each entry
# is a runtime artifact the docs themselves tell operators not to commit, or a
# file the operator creates from a template.
_ABSENT_BY_DESIGN = frozenset(
    {
        ".env",  # operator copy of .env.example (README quickstart)
        ".taskwizard.models.json",  # operator-provided provider registry
        "models.json",  # same registry, explicitly pointed at via env
        "events.jsonl",  # experience store under the private memory/ dir
        "episodes.json",  # rebuilt view over events.jsonl
        "kb.json",  # app-KB snapshot under the private memory/ dir
        "memory/",  # private runtime dir (.gitignore)
        "outputs/",  # private runtime dir (.gitignore)
    }
)

# AGENTS.md's Module Map states these v1 paths are deleted; requiring them to
# exist would contradict the doc it is checking.
_DELETED_V1 = frozenset(
    {"graph/", "actions/", "checkpoint/", "evals/", "agent.py", "main.py"}
)


def _document_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _expand_braces(token: str) -> list[str]:
    """``v2/{events,plugins}.py`` -> ``v2/events.py``, ``v2/plugins.py``."""

    match = _BRACE_GROUP.search(token)
    if match is None:
        return [token]
    expanded: list[str] = []
    for part in match.group(1).split(","):
        expanded.extend(
            _expand_braces(token[: match.start()] + part.strip() + token[match.end() :])
        )
    return expanded


def _is_path_like(token: str, *, from_link: bool) -> bool:
    """A directory reference, a dotfile, a suffixed file name or a link target."""

    if token.endswith("/"):
        return True
    name = token.rsplit("/", 1)[-1]
    if from_link:
        return True  # an inline markdown link is a pointer by definition
    if name.startswith("."):
        return True  # dotfile: .env, .env.example
    return "." in name and name.rsplit(".", 1)[-1] in _FILE_SUFFIXES


def _normalise(raw: str, *, from_link: bool) -> list[str]:
    """Turn one backticked/linked token into path candidates (possibly none)."""

    token = raw.strip().strip("`").rstrip(".,;:，。；、）)")
    if not token or any(char in _NOT_A_PATH for char in token):
        return []
    if token.startswith(("http://", "https://", "mailto:", "#", "/", "~", "./")):
        return []
    if _ANCHORED.search(token):
        return []
    if _NUMERIC_HOST.fullmatch(token):  # a listen address, not a repo file
        return []
    return [
        candidate
        for candidate in _expand_braces(token)
        if _is_path_like(candidate, from_link=from_link)
    ]


@lru_cache(maxsize=1)
def _bare_files() -> dict[str, str]:
    """Map bare file name -> one repo-relative path (first match wins)."""

    found: dict[str, str] = {}
    for path in REPO_ROOT.rglob("*"):
        parts = path.relative_to(REPO_ROOT).parts
        if not parts or parts[0] in _WALK_SKIP_DIRS:
            continue
        if path.is_file():
            found.setdefault(path.name, path.relative_to(REPO_ROOT).as_posix())
    return found


def _resolve(token: str) -> str | None:
    """Return the repo-relative path satisfying *token*, or ``None``."""

    if "/" in token:
        for base in _BASE_DIRS:
            candidate = base / PurePosixPath(token)
            if candidate.exists():
                return candidate.relative_to(REPO_ROOT).as_posix()
        return None
    return _bare_files().get(token)


def references(text: str) -> list[tuple[str, str | None]]:
    """``(token, resolved path or None)`` for every reference in *text*."""

    body = _FENCED_BLOCK.sub("", text)  # commands inside code fences stay commands
    tokens = [(match.group(1), False) for match in _BACKTICK.finditer(body)]
    tokens += [(match.group(1), True) for match in _MARKDOWN_LINK.finditer(body)]
    found: list[tuple[str, str | None]] = []
    for raw, from_link in tokens:
        for token in _normalise(raw, from_link=from_link):
            if token in _ABSENT_BY_DESIGN or token in _DELETED_V1:
                continue
            found.append((token, _resolve(token)))
    return found


@pytest.mark.parametrize("document", _DOCUMENTS)
def test_documented_paths_exist(document: str) -> None:
    missing = [
        (token, resolved)
        for token, resolved in references(_document_text(document))
        if resolved is None
    ]
    assert missing == [], (
        f"{document} references paths that no fresh clone contains:\n"
        + "\n".join(f"  `{token}`" for token, _ in missing)
        + "\nFix the doc to point at a tracked path (or delete the claim)."
    )


@pytest.mark.parametrize("document", _DOCUMENTS)
def test_documents_yield_references(document: str) -> None:
    """Guard against the extractor silently matching nothing."""

    assert len(references(_document_text(document))) >= 5, document


@pytest.mark.parametrize(
    "token",
    [
        "tool/execute",  # event-bus topic, not a file
        "model/pre_request",  # event-bus topic, not a file
        ".venv/bin/python",  # command interpreter, not a repo file
        "pages/*.md",  # glob
        "v2/model.py::build_default_headers",  # symbol anchor
        "phone_agent/v2/agent.py:123",  # line anchor
        "PHONE_AGENT_TOKEN_BUDGET",  # env var
        "feature/thin-loop-v2",  # branch name
        "v2/…",  # ellipsis shorthand
        "127.0.0.1",  # listen address in prose
        "ctx.on",  # API symbol, not a file
        "roles.safety_reviewer.model",  # models.json field path
        "compat.supportsUsageInStreaming",  # config field
        "taskwizard.capabilities",  # entry-point group
        "compat.requestApi=auto|chat|responses",  # config key
    ],
)
def test_non_path_tokens_are_skipped(token: str) -> None:
    assert references(f"见 `{token}`。") == []


@pytest.mark.parametrize(
    "token",
    [
        "v2/coords.py",
        "middleware/budget.py",
        "pages/",
        "native_content.py",
        ".env.example",
    ],
)
def test_path_tokens_are_extracted(token: str) -> None:
    assert [found for found, _ in references(f"见 `{token}`。")] == [token]


def test_shorthand_and_brace_expansion_resolve() -> None:
    """`v2/...`, `middleware/...` and `{a,b}.py` expand before resolving."""

    resolved = dict(
        references("`v2/coords.py`、`middleware/budget.py`、`v2/{events,plugins}.py`")
    )
    assert resolved["v2/coords.py"] == "phone_agent/v2/coords.py"
    assert resolved["middleware/budget.py"] == "phone_agent/v2/middleware/budget.py"
    assert resolved["v2/events.py"] == "phone_agent/v2/events.py"
    assert resolved["v2/plugins.py"] == "phone_agent/v2/plugins.py"


def test_markdown_links_are_checked() -> None:
    resolved = dict(
        references(
            "见 [配置参考](pages/configuration.md) 与 [文档站](https://example.com/)。"
        )
    )
    assert resolved == {"pages/configuration.md": "pages/configuration.md"}
