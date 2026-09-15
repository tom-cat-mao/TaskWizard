"""Machine gate: ``PHONE_AGENT_*`` keys agree across code, docs and the template.

Three surfaces describe one operator contract, and the gate holds them together:

* the **v2 config plane** — ``phone_agent/v2/config.py`` plus the provider/role
  modules — is where keys are really read from the environment;
* **``pages/configuration.md``** is the user manual: it may only name keys that
  exist, and (bar an explicitly whitelisted few) it has to name every key the
  plane reads, so a new knob cannot ship without a catalogue entry;
* **``.env.example``** is the copy-paste template: every key it lists must be
  read by some module, so an operator never sets a key nothing consumes.  The
  reverse is deliberately not enforced — the plane keeps a few dead compat
  switches out of the template on purpose (see the "保留但无读取方" note in the
  configuration page).

Sources are parsed, never imported: the ci.yml docs job installs pytest and
nothing else, so ``phone_agent`` must stay unimportable here.  A key counts as
read when it appears as a ``PHONE_AGENT_*`` string literal outside comments and
docstrings — that covers both ``os.getenv``/``_env_*`` helper calls and the
``(env_key, param)`` tables the module iterates.  A key that only lives in a
docstring is prose about a key, not a reader of it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# tests/docs/test_config_keys_sync.py -> parents[2] is the repository root (worktree).
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFIG_PAGE = "pages/configuration.md"
_ENV_TEMPLATE = ".env.example"

# Directories never walked: private runtime state, build output, tool caches and
# the test suite itself (a key exercised only by tests is not a shipped key).
_WALK_SKIP_DIRS = frozenset(
    {".git", ".venv", "site", "node_modules", "memory", "outputs", "tests"}
)

_KEY_RE = re.compile(r"PHONE_AGENT_[A-Z0-9_]+")


def _config_plane_files() -> list[str]:
    """``config.py`` plus every provider/role module, repo-relative and sorted."""

    providers = _REPO_ROOT / "phone_agent" / "v2" / "providers"
    return ["phone_agent/v2/config.py"] + [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in sorted(providers.glob("*.py"))
    ]


# Keys the user manual is allowed to leave out.  Entries are a claim that the key
# is not part of the operator contract; every one needs a one-line reason, and
# `test_whitelist_entries_are_argumented_and_still_needed` refuses stale entries.
UNDOCUMENTED_OK: dict[str, str] = {}


def keys_read_in(path: Path) -> set[str]:
    """``PHONE_AGENT_*`` literals the module reads (comments/docstrings excluded)."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _KEY_RE.fullmatch(node.value)
    }


def config_plane_keys() -> set[str]:
    """Keys the v2 configuration plane reads from the environment."""

    keys: set[str] = set()
    for relative in _config_plane_files():
        keys |= keys_read_in(_REPO_ROOT / relative)
    return keys


def repository_keys() -> set[str]:
    """Keys read anywhere in shipped sources (the whole-tree superset)."""

    keys: set[str] = set()
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(_REPO_ROOT).parts
        if parts and parts[0] in _WALK_SKIP_DIRS:
            continue
        keys |= keys_read_in(path)
    return keys


def documented_keys() -> set[str]:
    """Keys named anywhere on the configuration page (table row or prose note)."""

    return set(_KEY_RE.findall((_REPO_ROOT / _CONFIG_PAGE).read_text(encoding="utf-8")))


def template_keys(text: str) -> set[str]:
    """Keys listed in a ``.env`` template, commented-out lines included."""

    return set(_KEY_RE.findall(text))


def _page_keys(page: Path) -> set[str]:
    return set(_KEY_RE.findall(page.read_text(encoding="utf-8")))


def _pages() -> list[Path]:
    pages_dir = _REPO_ROOT / "pages"
    if not pages_dir.is_dir():
        return []
    return sorted(path for path in pages_dir.glob("*.md") if path.is_file())


_PAGES = _pages()
_PLANE_KEYS = config_plane_keys()
_DOCUMENTED = documented_keys()


def test_extractors_are_not_silently_empty() -> None:
    """Guard: a moved file or a broken parser must fail loudly, not pass quietly."""

    for relative in _config_plane_files():
        assert (_REPO_ROOT / relative).is_file(), f"config plane file is missing: {relative}"
    assert len(_config_plane_files()) >= 2, "the provider/role glob matched nothing"
    assert len(_PLANE_KEYS) >= 50, f"only {len(_PLANE_KEYS)} keys parsed out of the config plane"
    assert len(_DOCUMENTED) >= 50, f"only {len(_DOCUMENTED)} keys parsed out of {_CONFIG_PAGE}"
    assert len(template_keys((_REPO_ROOT / _ENV_TEMPLATE).read_text(encoding="utf-8"))) >= 50
    assert _PAGES, "no documentation pages found"


def test_config_plane_is_a_subset_of_the_repository_scan() -> None:
    assert _PLANE_KEYS <= repository_keys()


@pytest.mark.parametrize("page", _PAGES, ids=lambda page: page.name)
def test_documented_keys_are_read_by_source(page: Path) -> None:
    """A page may only name keys that some module really reads."""

    unknown = sorted(_page_keys(page) - repository_keys())
    assert unknown == [], (
        f"{page.relative_to(_REPO_ROOT).as_posix()} names keys no source reads:\n"
        + "\n".join(f"  {key}" for key in unknown)
        + "\n  fix : correct the key name, or read it in the v2 config plane"
    )


def test_every_config_key_is_documented() -> None:
    """A new env knob must reach the user manual in the same change."""

    missing = sorted(_PLANE_KEYS - _DOCUMENTED - set(UNDOCUMENTED_OK))
    assert missing == [], (
        "the v2 config plane reads keys the configuration page never names:\n"
        + "\n".join(f"  {key}" for key in missing)
        + f"\n  fix : document it in {_CONFIG_PAGE}, or add a justified entry to"
        "\n         UNDOCUMENTED_OK in tests/docs/test_config_keys_sync.py"
    )


def test_whitelist_entries_are_argumented_and_still_needed() -> None:
    """UNDOCUMENTED_OK stays honest: real key, real reason, still undocumented."""

    for key, reason in UNDOCUMENTED_OK.items():
        assert _KEY_RE.fullmatch(key), f"undocumented whitelist entry is not a key: {key!r}"
        assert reason.strip(), f"undocumented whitelist entry {key} needs a one-line reason"
        assert key in _PLANE_KEYS, f"undocumented whitelist entry {key} is not read any more"
        assert key not in _DOCUMENTED, (
            f"undocumented whitelist entry {key} is documented in {_CONFIG_PAGE} — drop it"
        )


def test_env_example_keys_are_read_by_source() -> None:
    """The template never asks an operator to set a key nothing consumes."""

    listed = template_keys((_REPO_ROOT / _ENV_TEMPLATE).read_text(encoding="utf-8"))
    unknown = sorted(listed - repository_keys())
    assert unknown == [], (
        f"{_ENV_TEMPLATE} lists keys no source reads:\n"
        + "\n".join(f"  {key}" for key in unknown)
        + "\n  fix : drop the stale line, or read the key in the v2 config plane"
    )


def test_env_template_parser_reads_past_comments_and_blank_lines() -> None:
    """Commented-out keys and blank lines are part of the template contract."""

    sample = (
        "# 采样参数：仅在网关有限制时覆盖。\n"
        "# PHONE_AGENT_TEMPERATURE=\"1.0\"\n"
        "\n"
        'PHONE_AGENT_BASE_URL="http://localhost:8000/v1"  # 行尾注释\n'
        "PHONE_AGENT_API_KEY=\n"
    )
    assert template_keys(sample) == {
        "PHONE_AGENT_TEMPERATURE",
        "PHONE_AGENT_BASE_URL",
        "PHONE_AGENT_API_KEY",
    }


def test_key_extractor_reads_literals_not_prose(tmp_path: Path) -> None:
    """Docstrings and comments describe keys; only literals read them."""

    module = tmp_path / "sample.py"
    module.write_text(
        '"""PHONE_AGENT_IN_A_DOCSTRING is described here."""\n'
        "# PHONE_AGENT_IN_A_COMMENT = os.getenv(...)\n"
        "import os\n"
        'BASE = os.getenv("PHONE_AGENT_BASE_URL", "http://localhost:8000/v1")\n'
        'PAIR = ("PHONE_AGENT_TEMPERATURE", "temperature")\n',
        encoding="utf-8",
    )
    assert keys_read_in(module) == {"PHONE_AGENT_BASE_URL", "PHONE_AGENT_TEMPERATURE"}
