"""Root test defaults: keep the real memory/ store untouched by test runs."""

import os
from pathlib import Path

import pytest

# Experience recording is exercised by dedicated tests with explicit tmp dirs;
# every other test (skill smoke pipelines etc.) must not write into the repo's
# real memory/experience store.
os.environ.setdefault("PHONE_AGENT_EXPERIENCE", "off")

_REPO_MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory"


def _memory_snapshot():
    """File inventory of the repo's real ``memory/`` dir (``None`` if absent)."""

    if not _REPO_MEMORY_DIR.exists():
        return None
    return {
        str(path.relative_to(_REPO_MEMORY_DIR)): path.stat().st_mtime_ns
        for path in _REPO_MEMORY_DIR.rglob("*")
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def guard_real_memory_dir():
    """No test may write into the repo's real ``memory/`` directory (S3).

    Capability-mount tests and any fixture that forgets tmp isolation would
    otherwise silently create or mutate ``./memory`` in the worktree.  The
    guard snapshots the directory before the test and fails when a file was
    added or changed.  Read-only access stays allowed.
    """

    before = _memory_snapshot()
    yield
    after = _memory_snapshot()
    if before == after:
        return
    created = sorted(set(after or {}) - set(before or {}))
    changed = sorted(
        key
        for key in set(before or {}) & set(after or {})
        if before[key] != after[key]  # type: ignore[index]
    )
    details = []
    if created:
        details.append(f"created={created}")
    if changed:
        details.append(f"modified={changed}")
    if before is None:
        details.append("the directory did not exist before the test")
    raise AssertionError(
        "test wrote into the repo's real memory/ directory ("
        + "; ".join(details)
        + "); isolate the test with tmp_path"
    )


@pytest.fixture(autouse=True)
def isolate_user_plugin_manifest(monkeypatch, tmp_path):
    """Never discover or execute plugins from the developer's real profile."""

    from phone_agent.v2 import plugins

    real_project_manifest_path = plugins.project_manifest_path
    monkeypatch.setattr(
        plugins, "user_manifest_path", lambda: tmp_path / "user-profile.toml"
    )
    monkeypatch.setattr(
        plugins,
        "project_manifest_path",
        lambda config=None: (
            real_project_manifest_path(config)
            if getattr(config, "plugin_manifest", None)
            else tmp_path / "project-profile.toml"
        ),
    )
