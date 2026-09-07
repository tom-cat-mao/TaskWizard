"""External capability plugins: manifest, discovery, and the API-version gate.

WP-PLUGIN-C turns the capability layer into a mini-Cordis loading surface. A
plugin is an out-of-tree ``CapabilitySpec`` (see ``capabilities.py``) contributed
either as an installed distribution that advertises the
``taskwizard.capabilities`` entry-point group, or as a local development
directory containing a ``plugin.py`` module that exposes ``CAPABILITY``.

Two manifest tiers select which plugins are active:

* user level  – ``~/.taskwizard/profile.toml``
* project level – ``<repo>/.taskwizard.toml`` (or ``PHONE_AGENT_PLUGIN_MANIFEST``)

Project entries override user entries by ``name``. Reading uses stdlib
``tomllib``; writing uses the minimal, shape-restricted emitter in this module
(``[[plugin]]`` table array, string/bool scalars, one ``[plugin.config]``
subtable) so no third-party TOML writer is pulled in.

Everything here is fail-visible: a plugin declared in an enabled manifest entry
that cannot be loaded, or whose ``REQUIRES_API`` does not admit the harness
``PLUGIN_API_VERSION``, raises rather than being silently skipped. An empty or
absent manifest yields no specs and therefore zero behavior change.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from phone_agent.v2.capabilities import CapabilitySpec

try:  # Phase A owns PLUGIN_API_VERSION; tolerate its absence pre-merge.
    from phone_agent.v2.capabilities import PLUGIN_API_VERSION
except ImportError:  # pragma: no cover - removed once Phase A lands
    PLUGIN_API_VERSION = 1

ENTRY_POINT_GROUP = "taskwizard.capabilities"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PluginError(Exception):
    """Base class for plugin manifest / loading failures."""


class PluginApiError(PluginError):
    """A plugin's ``REQUIRES_API`` does not admit the harness API version."""


class PluginLoadError(PluginError):
    """A plugin declared in an enabled manifest entry could not be loaded."""


@dataclass(frozen=True)
class PluginEntry:
    """One ``[[plugin]]`` manifest record (raw, no version-range semantics)."""

    name: str
    version: str | None = None
    enabled: bool = True
    path: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manifest paths
# ---------------------------------------------------------------------------
def user_manifest_path() -> Path:
    """User-level manifest: ``~/.taskwizard/profile.toml``."""

    return Path.home() / ".taskwizard" / "profile.toml"


def project_manifest_path(config: Any | None = None) -> Path:
    """Project-level manifest, honoring ``PHONE_AGENT_PLUGIN_MANIFEST`` override."""

    override = getattr(config, "plugin_manifest", None)
    if override:
        return Path(override)
    # config.py's ROOT is the repo root; fall back to CWD when unavailable.
    try:
        from phone_agent.v2.config import ROOT

        base = Path(ROOT)
    except Exception:  # pragma: no cover - config import is always available
        base = Path.cwd()
    return base / ".taskwizard.toml"


def manifest_paths(config: Any | None = None) -> tuple[Path, Path]:
    """Return the (user, project) manifest paths in override order."""

    return user_manifest_path(), project_manifest_path(config)


# ---------------------------------------------------------------------------
# Manifest read / merge / write
# ---------------------------------------------------------------------------
def _coerce_entry(raw: Any, *, source: Path) -> PluginEntry:
    if not isinstance(raw, dict):
        raise PluginError(f"{source}: each [[plugin]] must be a table, got {raw!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PluginError(f"{source}: [[plugin]] requires a non-empty string name")
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise PluginError(f"{source}: invalid plugin name {name!r}")
    version = raw.get("version")
    if version is not None and not isinstance(version, str):
        raise PluginError(f"{source}: plugin {name!r} version must be a string")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginError(f"{source}: plugin {name!r} enabled must be a bool")
    path = raw.get("path")
    if path is not None and not isinstance(path, str):
        raise PluginError(f"{source}: plugin {name!r} path must be a string")
    cfg = raw.get("config", {})
    if not isinstance(cfg, dict):
        raise PluginError(f"{source}: plugin {name!r} [plugin.config] must be a table")
    return PluginEntry(
        name=name,
        version=version,
        enabled=enabled,
        path=path,
        config=dict(cfg),
    )


def read_manifest(path: str | Path) -> list[PluginEntry]:
    """Parse one manifest file into ordered ``PluginEntry`` records.

    A missing file yields an empty list (absent manifest = no plugins).
    """

    path = Path(path)
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"{path}: cannot read manifest: {exc}") from exc
    raw_plugins = data.get("plugin", [])
    if not isinstance(raw_plugins, list):
        raise PluginError(f"{path}: [[plugin]] must be an array of tables")
    return [_coerce_entry(item, source=path) for item in raw_plugins]


def merge_manifests(
    user_entries: Iterable[PluginEntry],
    project_entries: Iterable[PluginEntry],
) -> list[PluginEntry]:
    """Merge user + project entries; a project entry overrides by ``name``.

    Insertion order follows user entries first (overridden in place), with
    project-only entries appended in their own order.
    """

    merged: dict[str, PluginEntry] = {}
    for entry in user_entries:
        merged[entry.name] = entry
    for entry in project_entries:
        merged[entry.name] = entry
    return list(merged.values())


def load_manifest(config: Any | None = None) -> list[PluginEntry]:
    """Read + merge the user and project manifests for ``config``."""

    user_path, project_path = manifest_paths(config)
    return merge_manifests(read_manifest(user_path), read_manifest(project_path))


def _emit_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise PluginError(
        f"manifest emitter only supports string/bool values, got {value!r}"
    )


def emit_manifest(entries: Sequence[PluginEntry]) -> str:
    """Serialize entries with the minimal shape-restricted TOML emitter."""

    lines: list[str] = []
    for entry in entries:
        lines.append("[[plugin]]")
        lines.append(f"name = {_emit_scalar(entry.name)}")
        if entry.version is not None:
            lines.append(f"version = {_emit_scalar(entry.version)}")
        lines.append(f"enabled = {_emit_scalar(entry.enabled)}")
        if entry.path is not None:
            lines.append(f"path = {_emit_scalar(entry.path)}")
        if entry.config:
            lines.append("")
            lines.append("[plugin.config]")
            for key, value in entry.config.items():
                lines.append(f"{key} = {_emit_scalar(value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + ("\n" if entries else "")


def write_manifest(path: str | Path, entries: Sequence[PluginEntry]) -> None:
    """Write entries to ``path`` (creating parent dirs) via the minimal emitter."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_manifest(entries), encoding="utf-8")


def upsert_entry(
    path: str | Path, entry: PluginEntry
) -> list[PluginEntry]:
    """Insert or replace ``entry`` by name in the manifest at ``path``."""

    entries = read_manifest(path)
    replaced = False
    result: list[PluginEntry] = []
    for existing in entries:
        if existing.name == entry.name:
            result.append(entry)
            replaced = True
        else:
            result.append(existing)
    if not replaced:
        result.append(entry)
    write_manifest(path, result)
    return result


def remove_entry(path: str | Path, name: str) -> tuple[list[PluginEntry], bool]:
    """Drop the entry named ``name`` from the manifest; return (entries, removed)."""

    entries = read_manifest(path)
    kept = [entry for entry in entries if entry.name != name]
    removed = len(kept) != len(entries)
    if removed:
        write_manifest(path, kept)
    return kept, removed


# ---------------------------------------------------------------------------
# API-version gate
# ---------------------------------------------------------------------------
_CLAUSE_RE = re.compile(r"^\s*(>=|<=|==|=|>|<|!=)?\s*([0-9][0-9.]*)\s*$")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(text).split(".") if part != "")


def satisfies_api(version: int | str, constraint: str | None) -> bool:
    """Return whether ``version`` satisfies a comma-separated constraint string.

    Constraints are the restricted PEP-440-ish subset used by ``REQUIRES_API``:
    comma-joined ``<op><version>`` clauses (``>= <= == = > < !=``) or a bare
    version (treated as ``==``). ``None``/empty admits any version.
    """

    if constraint is None or not str(constraint).strip():
        return True
    have = _version_tuple(str(version))
    for clause in str(constraint).split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = _CLAUSE_RE.match(clause)
        if not match:
            raise PluginError(f"invalid API version constraint clause: {clause!r}")
        op, want_text = match.group(1) or "==", match.group(2)
        want = _version_tuple(want_text)
        if op in {"==", "="}:
            ok = have == want
        elif op == "!=":
            ok = have != want
        elif op == ">=":
            ok = have >= want
        elif op == "<=":
            ok = have <= want
        elif op == ">":
            ok = have > want
        else:  # "<"
            ok = have < want
        if not ok:
            return False
    return True


def _check_api_gate(name: str, requires_api: Any) -> None:
    if requires_api is None:
        return
    if not isinstance(requires_api, str):
        raise PluginApiError(
            f"plugin {name!r}: REQUIRES_API must be a string, got {requires_api!r}"
        )
    try:
        ok = satisfies_api(PLUGIN_API_VERSION, requires_api)
    except PluginError as exc:
        raise PluginApiError(f"plugin {name!r}: {exc}") from exc
    if not ok:
        raise PluginApiError(
            f"plugin {name!r} requires API {requires_api!r} but the harness "
            f"provides PLUGIN_API_VERSION={PLUGIN_API_VERSION}"
        )


# ---------------------------------------------------------------------------
# Spec resolution (entry points + local path)
# ---------------------------------------------------------------------------
def _resolve_spec(loaded: Any) -> tuple[CapabilitySpec, Any]:
    """Extract (CapabilitySpec, REQUIRES_API) from a loaded object/module."""

    requires_api = getattr(loaded, "REQUIRES_API", None)
    if isinstance(loaded, CapabilitySpec):
        return loaded, requires_api
    spec = getattr(loaded, "CAPABILITY", None)
    if isinstance(spec, CapabilitySpec):
        # A module-level REQUIRES_API takes precedence, else the spec's module.
        if requires_api is None:
            requires_api = getattr(spec, "REQUIRES_API", None)
        return spec, requires_api
    raise PluginLoadError(
        "plugin did not provide a CapabilitySpec (expected a CapabilitySpec "
        "entry point or a CAPABILITY attribute)"
    )


def _spec_from_entry_point(name: str) -> tuple[CapabilitySpec, Any]:
    eps = entry_points(group=ENTRY_POINT_GROUP)
    selected = [ep for ep in eps if ep.name == name]
    if not selected:
        raise PluginLoadError(
            f"plugin {name!r}: no entry point named {name!r} in group "
            f"{ENTRY_POINT_GROUP!r} (is the package installed?)"
        )
    try:
        loaded = selected[0].load()
    except Exception as exc:  # noqa: BLE001 - surface any loader failure
        raise PluginLoadError(f"plugin {name!r}: entry point load failed: {exc}") from exc
    return _resolve_spec(loaded)


def _spec_from_path(name: str, path: str | Path) -> tuple[CapabilitySpec, Any]:
    directory = Path(path)
    if not directory.is_absolute():
        directory = (project_manifest_path().parent / directory).resolve()
    module_file = directory / "plugin.py"
    if not module_file.is_file():
        raise PluginLoadError(
            f"plugin {name!r}: local path has no plugin.py ({module_file})"
        )
    mod_name = f"_taskwizard_plugin_{name}"
    spec_obj = importlib.util.spec_from_file_location(mod_name, module_file)
    if spec_obj is None or spec_obj.loader is None:
        raise PluginLoadError(f"plugin {name!r}: cannot import {module_file}")
    module = importlib.util.module_from_spec(spec_obj)
    try:
        spec_obj.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface any import-time failure
        raise PluginLoadError(f"plugin {name!r}: import failed: {exc}") from exc
    return _resolve_spec(module)


def load_plugin_spec(entry: PluginEntry) -> CapabilitySpec:
    """Load one enabled entry into a gated ``CapabilitySpec`` (fail-visible)."""

    if entry.path:
        spec, requires_api = _spec_from_path(entry.name, entry.path)
    else:
        spec, requires_api = _spec_from_entry_point(entry.name)
    _check_api_gate(entry.name, requires_api)
    return spec


def discover_external_specs(
    config: Any | None = None,
    *,
    entries: Sequence[PluginEntry] | None = None,
) -> list[CapabilitySpec]:
    """Return ``CapabilitySpec`` objects for every enabled manifest entry.

    Disabled entries are skipped. Any single enabled plugin that fails to load
    or fails the API gate raises (fail-visible) — plugins are never silently
    dropped. When ``PHONE_AGENT_PLUGINS`` is off, returns an empty list.
    """

    if config is not None and not getattr(config, "plugins_enabled", True):
        return []
    if entries is None:
        entries = load_manifest(config)
    specs: list[CapabilitySpec] = []
    for entry in entries:
        if not entry.enabled:
            continue
        specs.append(load_plugin_spec(entry))
    return specs


# ---------------------------------------------------------------------------
# pip / uv command construction
# ---------------------------------------------------------------------------
def pip_base_command() -> list[str]:
    """Prefer ``uv pip`` when available, else the current interpreter's pip.

    Running via ``.venv/bin/python`` makes ``sys.executable -m pip`` the venv
    pip, satisfying the spec's ``.venv/bin/pip`` fallback without hardcoding a
    machine-specific path.
    """

    if shutil.which("uv"):
        return ["uv", "pip"]
    return [sys.executable, "-m", "pip"]


def _run_pip(
    args: Sequence[str],
    *,
    run: Callable[..., Any] | None = None,
) -> None:
    """Run a pip subcommand, raising ``PluginError`` on non-zero exit / failure."""

    cmd = [*pip_base_command(), *args]
    runner = run or subprocess.run
    try:
        completed = runner(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise PluginError(f"failed to invoke pip ({cmd[0]!r}): {exc}") from exc
    returncode = getattr(completed, "returncode", 1)
    if returncode != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        raise PluginError(
            f"pip command failed ({' '.join(cmd)}): exit {returncode}"
            + (f": {stderr}" if stderr else "")
        )


# ---------------------------------------------------------------------------
# CLI command logic (argparse-free, directly testable)
# ---------------------------------------------------------------------------
def _looks_like_path(target: str) -> bool:
    return (
        target.startswith((".", "/", "~"))
        or "/" in target
        or Path(target).exists()
    )


def cmd_list(config: Any | None = None) -> list[dict[str, Any]]:
    """Rows combining manifest entries with actual assembly state.

    Each row carries the manifest fields plus ``state`` from a freshly assembled
    capability registry (external specs merged in). ``state`` is ``error`` with
    a ``reason`` when the plugin cannot be loaded/gated.
    """

    entries = load_manifest(config)
    status_by_id = _assembled_status(config, entries)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row: dict[str, Any] = {
            "name": entry.name,
            "version": entry.version,
            "enabled": entry.enabled,
            "path": entry.path,
        }
        if not entry.enabled:
            row["state"] = "disabled"
        else:
            try:
                spec = load_plugin_spec(entry)
            except PluginError as exc:
                row["state"] = "error"
                row["reason"] = str(exc)
            else:
                row["cap_id"] = spec.cap_id
                row["state"] = status_by_id.get(spec.cap_id, "unmounted")
        rows.append(row)
    return rows


def _assembled_status(
    config: Any | None, entries: Sequence[PluginEntry]
) -> dict[str, str]:
    """Best-effort assembly state per cap_id; fail-open to an empty map."""

    try:
        from phone_agent.v2.capabilities import build_capability_registry

        registry = build_capability_registry(config)
        for entry in entries:
            if not entry.enabled:
                continue
            try:
                registry.register(load_plugin_spec(entry))
            except (PluginError, ValueError):
                continue
        return {row["cap_id"]: row["state"] for row in registry.status()}
    except Exception:  # noqa: BLE001 - list stays usable without a registry
        return {}


def cmd_add(
    target: str,
    *,
    config: Any | None = None,
    scope: str = "project",
    run: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Install (pip) or register (local path) a plugin, then write the manifest.

    ``target`` is a pip package name (installed via uv/pip) or a local dev path
    (recorded as ``path=`` only). ``scope`` selects the user or project manifest.
    """

    path = _manifest_for_scope(config, scope)
    if _looks_like_path(target):
        directory = Path(target).expanduser()
        name = directory.name
        entry = PluginEntry(name=name, enabled=True, path=str(target))
        upsert_entry(path, entry)
        return {
            "action": "add",
            "kind": "path",
            "name": name,
            "path": str(target),
            "manifest": str(path),
        }
    _run_pip(["install", target], run=run)
    entry = PluginEntry(name=target, enabled=True)
    upsert_entry(path, entry)
    return {
        "action": "add",
        "kind": "package",
        "name": target,
        "manifest": str(path),
    }


def cmd_remove(
    name: str,
    *,
    config: Any | None = None,
    scope: str = "project",
    run: Callable[..., Any] | None = None,
    uninstall: bool = True,
) -> dict[str, Any]:
    """Uninstall the package (unless a local-path entry) and clean the manifest."""

    path = _manifest_for_scope(config, scope)
    existing = {entry.name: entry for entry in read_manifest(path)}
    entry = existing.get(name)
    if entry is None:
        raise PluginError(
            f"plugin {name!r} is not in the {scope} manifest ({path})"
        )
    is_package = entry.path is None
    if is_package and uninstall:
        _run_pip(["uninstall", "-y", name], run=run)
    _, removed = remove_entry(path, name)
    return {
        "action": "remove",
        "name": name,
        "uninstalled": bool(is_package and uninstall),
        "removed_from_manifest": removed,
        "manifest": str(path),
    }


def cmd_update(
    name: str | None = None,
    *,
    all_: bool = False,
    config: Any | None = None,
    run: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """``pip install -U`` one named package plugin, or every package plugin."""

    if not all_ and not name:
        raise PluginError("plugin update requires a NAME or --all")
    entries = load_manifest(config)
    package_names = [
        entry.name for entry in entries if entry.enabled and entry.path is None
    ]
    if all_:
        targets = package_names
    else:
        if name not in package_names:
            raise PluginError(
                f"plugin {name!r} is not an installed package plugin in the manifest"
            )
        targets = [name]
    for target in targets:
        _run_pip(["install", "-U", target], run=run)
    return {"action": "update", "updated": targets}


def cmd_search(term: str | None = None, *, config: Any | None = None) -> list[dict]:
    """Match entries from ``PHONE_AGENT_PLUGIN_INDEX`` (URL or local json)."""

    index = read_plugin_index(config)
    if not term:
        return index
    needle = term.lower()
    matches: list[dict] = []
    for item in index:
        haystack = " ".join(
            str(item.get(key, ""))
            for key in ("name", "summary", "description", "keywords")
        ).lower()
        if needle in haystack:
            matches.append(item)
    return matches


def read_plugin_index(config: Any | None = None) -> list[dict]:
    """Load the plugin index (local json path or http(s) URL) into a list."""

    import json

    source = getattr(config, "plugin_index", None) or "plugins/index.json"
    text: str
    if str(source).startswith(("http://", "https://")):
        try:
            import urllib.request

            with urllib.request.urlopen(str(source), timeout=10) as response:
                text = response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - surface network failure clearly
            raise PluginError(f"cannot fetch plugin index {source!r}: {exc}") from exc
    else:
        path = Path(source)
        if not path.is_absolute():
            try:
                from phone_agent.v2.config import ROOT

                candidate = Path(ROOT) / path
            except Exception:  # pragma: no cover
                candidate = path
            path = candidate if candidate.exists() else path
        if not path.exists():
            raise PluginError(f"plugin index not found: {source!r}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginError(f"cannot read plugin index {source!r}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PluginError(f"plugin index {source!r} is not valid json: {exc}") from exc
    if not isinstance(data, list):
        raise PluginError(f"plugin index {source!r} must be a json array")
    return [item for item in data if isinstance(item, dict)]


def _manifest_for_scope(config: Any | None, scope: str) -> Path:
    if scope == "user":
        return user_manifest_path()
    if scope == "project":
        return project_manifest_path(config)
    raise PluginError(f"unknown manifest scope: {scope!r} (expected user/project)")


__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_API_VERSION",
    "PluginApiError",
    "PluginEntry",
    "PluginError",
    "PluginLoadError",
    "cmd_add",
    "cmd_list",
    "cmd_remove",
    "cmd_search",
    "cmd_update",
    "discover_external_specs",
    "emit_manifest",
    "load_manifest",
    "load_plugin_spec",
    "manifest_paths",
    "merge_manifests",
    "pip_base_command",
    "project_manifest_path",
    "read_manifest",
    "read_plugin_index",
    "remove_entry",
    "satisfies_api",
    "upsert_entry",
    "user_manifest_path",
    "write_manifest",
]
