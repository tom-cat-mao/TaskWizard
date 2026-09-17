"""Machine gate: the root ``AGENTS.md`` Module Map names every v2 module.

The Module Map is the index a fresh clone (human or coding agent) reads to find
out what lives where; a module it never names is effectively invisible — the
reader cannot tell whether the file is missing, misplaced or simply not the
thing they want.  This already happened once: ``recall.py`` (RAG recall), the
``coords.py`` coordinate plane, ``usage.py``, ``locate_scope.py``,
``native_content.py`` and three middleware modules had no entry.

The check reads the v2 module files from disk and requires each one to be named
by the root ``AGENTS.md`` text, in the form its half of the map uses:

* a **top-level** module needs a ``name.py`` token — the map's brace shorthand
  (``v2/{events,plugins}.py``) is expanded first, so those members count too;
* a **middleware** module needs the ``middleware/`` path context: a bare stem in
  the middleware roster (``v2/middleware/（safety、images、…）``, listed without
  the extension) or a ``middleware/name.py`` token.  A top-level mention does
  not cover a middleware file, so ``v2/taskdoc.py`` being named in the task row
  does not put ``middleware/taskdoc.py`` on the map — the modules do different
  jobs and a reader looking for one must be able to find it.

``_``-prefixed modules are exempt: ``_redact.py`` and ``_tokens.py`` are
private helpers shared inside the middleware package, not entries of the map.
The exemption is a rule, not a list, and
``test_private_module_exemption_stays_narrow`` keeps it from swallowing the
package.  Sources are parsed, never imported: the ci.yml docs job installs
pytest and nothing else, so ``phone_agent`` must stay unimportable here — this
file needs no import beyond the standard library.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/docs/test_module_map.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_V2_DIR = _REPO_ROOT / "phone_agent" / "v2"
_MIDDLEWARE_DIR = _V2_DIR / "middleware"
_AGENTS_MD = "AGENTS.md"

# Fenced blocks are examples; a command sample does not put a module on the map.
_FENCED_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<fence>```+|~~~+)[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# ``name.py`` tokens, with the path prefix that decides which half of the map
# they belong to (a `middleware/` prefix marks a middleware module).
_MODULE_TOKEN_RE = re.compile(r"(?P<prefix>[A-Za-z0-9_./\-]*?)(?P<name>[a-z][a-z0-9_]*)\.py\b")
# `v2/{events,plugins,pins}.py` names events.py, plugins.py and pins.py.
_BRACED_LIST_RE = re.compile(r"(?P<prefix>[A-Za-z0-9_./\-]*)\{(?P<stems>[a-z_, \t]+)\}\.py")

# The middleware roster: the first parenthetical after the `v2/middleware/`
# entry, full-width or ASCII brackets, stems separated by `、`/`,`/whitespace.
_MIDDLEWARE_ROSTER_RE = re.compile(r"v2/middleware/[^\n（(]*[（(](?P<items>[^）)\n]*)[）)]")
_ROSTER_SPLIT_RE = re.compile(r"[、,;|\s]+")


def _strip_fences(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    return _FENCED_BLOCK_RE.sub(" ", text)


def module_paths() -> list[str]:
    """Repo-relative public module files the Module Map has to name, sorted.

    ``__init__.py`` and the ``_``-prefixed private helpers are out of scope (see
    the module docstring); everything else is a map entry.
    """

    paths: list[str] = []
    for directory in (_V2_DIR, _MIDDLEWARE_DIR):
        paths += sorted(
            path.relative_to(_REPO_ROOT).as_posix()
            for path in directory.glob("*.py")
            if path.is_file() and not path.name.startswith("_")
        )
    return paths


def named_modules(text: str) -> tuple[set[str], set[str]]:
    """``(top-level, middleware)`` module file names *text* names."""

    body = _strip_fences(text)
    top_level: set[str] = set()
    middleware: set[str] = set()

    def add(prefix: str, name: str) -> None:
        pool = middleware if prefix.rstrip("/").endswith("middleware") else top_level
        pool.add(name + ".py")

    for match in _MODULE_TOKEN_RE.finditer(body):
        add(match.group("prefix"), match.group("name"))
    for match in _BRACED_LIST_RE.finditer(body):
        for stem in match.group("stems").split(","):
            add(match.group("prefix"), stem.strip())
    for match in _MIDDLEWARE_ROSTER_RE.finditer(body):
        for item in _ROSTER_SPLIT_RE.split(match.group("items")):
            if item.strip():
                middleware.add(item.strip() + ".py")
    return top_level, middleware


def missing_modules(text: str) -> list[str]:
    """Repo-relative module files *text* never names, sorted."""

    top_level, middleware = named_modules(text)
    missing: list[str] = []
    for path in module_paths():
        pool = middleware if path.startswith("phone_agent/v2/middleware/") else top_level
        if Path(path).name not in pool:
            missing.append(path)
    return missing


def test_the_module_scan_covers_the_v2_package() -> None:
    """Guard against the check silently passing because it scans nothing."""

    found = module_paths()
    assert len(found) >= 30, f"only {len(found)} v2 modules found under {_V2_DIR}"
    assert any(path.startswith("phone_agent/v2/middleware/") for path in found), (
        "no middleware modules found; the middleware glob looks wrong"
    )


def test_every_v2_module_is_named_in_agents_md() -> None:
    """A module the index never names is invisible to a fresh clone."""

    found = missing_modules((_REPO_ROOT / _AGENTS_MD).read_text(encoding="utf-8"))
    assert found == [], (
        f"{_AGENTS_MD} does not name these v2 modules:\n"
        + "\n".join(f"  {path}" for path in found)
        + f"\n  fix : name each one in the Module Map of {_AGENTS_MD} — a `v2/name.py`"
        "\n        token (or brace shorthand) for top-level modules, a bare stem in the"
        "\n        middleware roster (`v2/middleware/（safety、images、…）`) for middleware"
    )


def test_naming_forms_are_both_read() -> None:
    """Top-level modules use `name.py` tokens, middleware a bare-stem roster."""

    text = (
        "| 总线 | `v2/{events,plugins,pins}.py` |\n"
        "| 策略 | `v2/middleware/`（safety、images） |\n"
        "| 保留 | `middleware/{budget,compact}.py` |\n"
    )
    top_level, middleware = named_modules(text)
    assert top_level == {"events.py", "plugins.py", "pins.py"}
    assert middleware == {"safety.py", "images.py", "budget.py", "compact.py"}


def test_a_top_level_mention_does_not_cover_a_middleware_module() -> None:
    """`v2/taskdoc.py` and `v2/middleware/taskdoc.py` do different jobs."""

    top_level, middleware = named_modules("| 任务 | `v2/{taskdoc,resolver}.py` |")
    assert top_level == {"taskdoc.py", "resolver.py"}
    assert middleware == set()


def test_missing_modules_are_reported_not_silently_passed() -> None:
    """The gate has teeth: a module with no mention is a violation."""

    assert missing_modules("nothing here") == module_paths()
    assert missing_modules("`v2/recall.py`") == [
        path for path in module_paths() if path != "phone_agent/v2/recall.py"
    ]


def test_a_completed_map_is_green() -> None:
    """Self-test for the parallel Module Map fix: real text plus the missing
    names passes, without touching the checked file."""

    text = (_REPO_ROOT / _AGENTS_MD).read_text(encoding="utf-8")
    patched = (
        text
        + "\n`v2/{recall,coords,usage,locate_scope,native_content}.py`\n"
        + "`v2/middleware/（safety、images、compact、budget、trace、procedure、diagnostic、"
        "streaming、taskdoc、context_request、context_admission）`\n"
    )
    assert missing_modules(patched) == []


def test_private_module_exemption_stays_narrow() -> None:
    """`_`-prefixed helpers are exempt on purpose; the rule may not grow teeth."""

    private = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for directory in (_V2_DIR, _MIDDLEWARE_DIR)
        for path in directory.glob("*.py")
        if path.is_file() and path.name.startswith("_") and path.name != "__init__.py"
    )
    assert len(private) <= 4, (
        "too many private v2 modules to leave off the Module Map:\n"
        + "\n".join(f"  {path}" for path in private)
        + "\n  fix : name them in AGENTS.md and drop this exemption"
    )
