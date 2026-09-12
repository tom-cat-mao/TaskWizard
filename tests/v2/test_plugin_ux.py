"""WP-PLUGIN-C: manifest, discovery, API gate, agent merge, and CLI logic."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from phone_agent.v2 import plugins
from phone_agent.v2.capabilities import CapabilitySpec


def _config(tmp_path, **overrides):
    base = SimpleNamespace(
        plugins_enabled=True,
        plugin_manifest=str(tmp_path / ".taskwizard.toml"),
        plugin_index=str(tmp_path / "index.json"),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ---------------------------------------------------------------------------
# Manifest read / write round-trip + project overrides user
# ---------------------------------------------------------------------------
def test_manifest_round_trip(tmp_path):
    entries = [
        plugins.PluginEntry(
            name="alpha",
            version=">=1.2,<2",
            enabled=True,
            config={"key": "val", "flag": True},
        ),
        plugins.PluginEntry(name="beta", enabled=False, path="./beta"),
    ]
    path = tmp_path / "profile.toml"
    plugins.write_manifest(path, entries)
    parsed = plugins.read_manifest(path)
    assert parsed == entries


def test_emit_manifest_escapes_and_shapes(tmp_path):
    entry = plugins.PluginEntry(name="a", version='q"x', config={"s": "a\\b"})
    text = plugins.emit_manifest([entry])
    assert "[[plugin]]" in text
    assert "[plugin.config]" in text
    # Re-parse to confirm the emitter is round-trip safe with escapes.
    path = tmp_path / "m.toml"
    path.write_text(text, encoding="utf-8")
    assert plugins.read_manifest(path)[0] == entry


def test_empty_manifest_zero_behavior_change(tmp_path):
    # Folded from test_missing_manifest_is_empty + test_discover_skips_disabled:
    # every zero-entry path must be a behaviour-preserving no-op.
    config = _config(tmp_path)
    # A missing manifest reads as empty (parse layer)...
    assert plugins.read_manifest(config.plugin_manifest) == []
    # ...so discovery is a no-op and the CLI lists nothing.
    assert plugins.discover_external_specs(config) == []
    assert plugins.cmd_list(config) == []

    # A disabled entry is skipped before path resolution — even a missing path
    # must never raise.
    directory = _write_plugin_dir(tmp_path, "on_plug", cap_id="cap_on")
    entries = [
        plugins.PluginEntry(name="on_plug", enabled=True, path=str(directory)),
        plugins.PluginEntry(name="off_plug", enabled=False, path="./missing"),
    ]
    specs = plugins.discover_external_specs(entries=entries)
    assert [s.cap_id for s in specs] == ["cap_on"]


def test_project_overrides_user_by_name():
    user = [
        plugins.PluginEntry(name="shared", version="1.0"),
        plugins.PluginEntry(name="user_only"),
    ]
    project = [
        plugins.PluginEntry(name="shared", version="2.0", enabled=False),
        plugins.PluginEntry(name="project_only"),
    ]
    merged = {e.name: e for e in plugins.merge_manifests(user, project)}
    assert merged["shared"].version == "2.0"
    assert merged["shared"].enabled is False
    assert "user_only" in merged and "project_only" in merged


def test_upsert_and_remove_entry(tmp_path):
    path = tmp_path / ".taskwizard.toml"
    plugins.upsert_entry(path, plugins.PluginEntry(name="a"))
    plugins.upsert_entry(path, plugins.PluginEntry(name="a", version="9"))
    assert plugins.read_manifest(path) == [plugins.PluginEntry(name="a", version="9")]
    _, removed = plugins.remove_entry(path, "a")
    assert removed is True
    assert plugins.read_manifest(path) == []


def test_invalid_manifest_entries_fail_visible(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[[plugin]]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(plugins.PluginError):
        plugins.read_manifest(path)


# ---------------------------------------------------------------------------
# API version gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "constraint, ok",
    [
        (None, True),
        ("", True),
        (">=1,<2", True),
        ("==1", True),
        ("1", True),
        (">=2", False),
        ("<1", False),
        ("!=1", False),
    ],
)
def test_satisfies_api(constraint, ok):
    assert plugins.satisfies_api(1, constraint) is ok


def test_satisfies_api_rejects_garbage():
    with pytest.raises(plugins.PluginError):
        plugins.satisfies_api(1, "~=1.0")


# ---------------------------------------------------------------------------
# Local-path plugin loading + API gate paths
# ---------------------------------------------------------------------------
def _write_plugin_dir(tmp_path, name, *, cap_id="plug_x", requires_api=None):
    directory = tmp_path / name
    directory.mkdir()
    lines = [
        "from phone_agent.v2.capabilities import CapabilitySpec",
    ]
    if requires_api is not None:
        lines.append(f"REQUIRES_API = {requires_api!r}")
    lines.append(f'CAPABILITY = CapabilitySpec({cap_id!r}, "Plug", "on")')
    (directory / "plugin.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def test_local_path_plugin_loads(tmp_path):
    directory = _write_plugin_dir(tmp_path, "myplug", cap_id="plug_local")
    entry = plugins.PluginEntry(name="myplug", enabled=True, path=str(directory))
    spec = plugins.load_plugin_spec(entry)
    assert isinstance(spec, CapabilitySpec)
    assert spec.cap_id == "plug_local"


def test_local_path_plugin_api_gate_blocks(tmp_path):
    directory = _write_plugin_dir(tmp_path, "old", requires_api=">=2,<3")
    entry = plugins.PluginEntry(name="old", enabled=True, path=str(directory))
    with pytest.raises(plugins.PluginApiError):
        plugins.load_plugin_spec(entry)


def test_local_path_plugin_api_gate_admits(tmp_path):
    directory = _write_plugin_dir(tmp_path, "good", requires_api=">=1,<2")
    entry = plugins.PluginEntry(name="good", enabled=True, path=str(directory))
    assert plugins.load_plugin_spec(entry).cap_id == "plug_x"


def test_missing_plugin_py_fails_visible(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()
    entry = plugins.PluginEntry(name="empty", enabled=True, path=str(directory))
    with pytest.raises(plugins.PluginLoadError):
        plugins.load_plugin_spec(entry)


# ---------------------------------------------------------------------------
# Entry-point discovery (fake distribution)
# ---------------------------------------------------------------------------
def test_entry_point_plugin_loads(monkeypatch):
    spec = CapabilitySpec("plug_ep", "EP", "on")
    fake_ep = SimpleNamespace(name="plug_ep", load=lambda: spec)

    def fake_entry_points(*, group):
        assert group == plugins.ENTRY_POINT_GROUP
        return [fake_ep]

    monkeypatch.setattr(plugins, "entry_points", fake_entry_points)
    entry = plugins.PluginEntry(name="plug_ep", enabled=True)
    assert plugins.load_plugin_spec(entry) is spec


def test_entry_point_missing_fails_visible(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda *, group: [])
    entry = plugins.PluginEntry(name="ghost", enabled=True)
    with pytest.raises(plugins.PluginLoadError):
        plugins.load_plugin_spec(entry)


def test_entry_point_api_gate(monkeypatch):
    module = types.ModuleType("fake_plugin_mod")
    module.REQUIRES_API = ">=2"
    module.CAPABILITY = CapabilitySpec("plug_ep2", "EP", "on")
    fake_ep = SimpleNamespace(name="plug_ep2", load=lambda: module)
    monkeypatch.setattr(plugins, "entry_points", lambda *, group: [fake_ep])
    with pytest.raises(plugins.PluginApiError):
        plugins.load_plugin_spec(plugins.PluginEntry(name="plug_ep2", enabled=True))


# ---------------------------------------------------------------------------
# discover_external_specs
# ---------------------------------------------------------------------------
def test_discover_skips_disabled(tmp_path):
    directory = _write_plugin_dir(tmp_path, "on_plug", cap_id="cap_on")
    entries = [
        plugins.PluginEntry(name="on_plug", enabled=True, path=str(directory)),
        plugins.PluginEntry(name="off_plug", enabled=False, path="./missing"),
    ]
    specs = plugins.discover_external_specs(entries=entries)
    assert [s.cap_id for s in specs] == ["cap_on"]


def test_discover_off_switch_returns_empty(tmp_path):
    directory = _write_plugin_dir(tmp_path, "p", cap_id="cap_p")
    config = _config(tmp_path, plugins_enabled=False)
    entries = [plugins.PluginEntry(name="p", enabled=True, path=str(directory))]
    assert plugins.discover_external_specs(config, entries=entries) == []


def test_discover_bad_plugin_fails_visible():
    entries = [plugins.PluginEntry(name="nope", enabled=True, path="/does/not/exist")]
    with pytest.raises(plugins.PluginError):
        plugins.discover_external_specs(entries=entries)


# ---------------------------------------------------------------------------
# CLI logic (argparse-free)
# ---------------------------------------------------------------------------
def test_cmd_add_local_path_writes_manifest(tmp_path):
    config = _config(tmp_path)
    receipt = plugins.cmd_add("./local/plug", config=config, scope="project")
    assert receipt["kind"] == "path"
    entries = plugins.read_manifest(config.plugin_manifest)
    assert entries[0].path == "./local/plug"


def test_cmd_add_package_invokes_pip(tmp_path):
    config = _config(tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    receipt = plugins.cmd_add("cool-pkg", config=config, run=fake_run)
    assert receipt["kind"] == "package"
    assert calls and calls[0][-2:] == ["install", "cool-pkg"]
    assert plugins.read_manifest(config.plugin_manifest)[0].name == "cool-pkg"


def test_cmd_add_pip_failure_raises(tmp_path):
    config = _config(tmp_path)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with pytest.raises(plugins.PluginError):
        plugins.cmd_add("bad-pkg", config=config, run=fake_run)
    # A failed install must not write the manifest.
    assert plugins.read_manifest(config.plugin_manifest) == []


def test_cmd_remove_uninstalls_and_cleans(tmp_path):
    config = _config(tmp_path)
    plugins.upsert_entry(config.plugin_manifest, plugins.PluginEntry(name="pkg"))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    receipt = plugins.cmd_remove("pkg", config=config, run=fake_run)
    assert receipt["uninstalled"] is True
    assert "uninstall" in calls[0]
    assert plugins.read_manifest(config.plugin_manifest) == []


def test_cmd_remove_local_path_no_uninstall(tmp_path):
    config = _config(tmp_path)
    plugins.upsert_entry(
        config.plugin_manifest, plugins.PluginEntry(name="loc", path="./loc")
    )
    calls = []
    receipt = plugins.cmd_remove(
        "loc", config=config, run=lambda cmd, **k: calls.append(cmd)
    )
    assert receipt["uninstalled"] is False
    assert calls == []


def test_cmd_remove_missing_fails(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(plugins.PluginError):
        plugins.cmd_remove("ghost", config=config)


def test_cmd_update_all(tmp_path):
    config = _config(tmp_path)
    plugins.write_manifest(
        config.plugin_manifest,
        [
            plugins.PluginEntry(name="p1"),
            plugins.PluginEntry(name="p2", path="./p2"),
            plugins.PluginEntry(name="p3"),
        ],
    )
    calls = []
    result = plugins.cmd_update(
        all_=True,
        config=config,
        run=lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0, stderr=""),
    )
    # Local-path plugin (p2) is not a pip target.
    assert result["updated"] == ["p1", "p3"]
    assert all("-U" in cmd for cmd in calls)


def test_cmd_update_requires_target(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(plugins.PluginError):
        plugins.cmd_update(config=config)


def test_cmd_search_filters_index(tmp_path):
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            [
                {"name": "weather", "summary": "forecast helper"},
                {"name": "music", "summary": "player"},
            ]
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path, plugin_index=str(index))
    assert [m["name"] for m in plugins.cmd_search("forecast", config=config)] == [
        "weather"
    ]
    assert len(plugins.cmd_search(None, config=config)) == 2


def test_read_plugin_index_missing_fails(tmp_path):
    config = _config(tmp_path, plugin_index=str(tmp_path / "gone.json"))
    with pytest.raises(plugins.PluginError):
        plugins.read_plugin_index(config)


def test_read_plugin_index_bad_json(tmp_path):
    index = tmp_path / "index.json"
    index.write_text("{not json", encoding="utf-8")
    config = _config(tmp_path, plugin_index=str(index))
    with pytest.raises(plugins.PluginError):
        plugins.read_plugin_index(config)


def test_cmd_list_reports_state(tmp_path):
    directory = _write_plugin_dir(tmp_path, "listed", cap_id="cap_listed")
    config = _config(tmp_path)
    plugins.write_manifest(
        config.plugin_manifest,
        [
            plugins.PluginEntry(name="listed", enabled=True, path=str(directory)),
            plugins.PluginEntry(name="disabled_one", enabled=False),
        ],
    )
    rows = {row["name"]: row for row in plugins.cmd_list(config)}
    assert rows["listed"]["cap_id"] == "cap_listed"
    assert rows["listed"]["state"] in {"active", "shadow", "unmounted"}
    assert rows["disabled_one"]["state"] == "disabled"


def test_cmd_list_reports_load_error(tmp_path):
    config = _config(tmp_path)
    plugins.write_manifest(
        config.plugin_manifest,
        [plugins.PluginEntry(name="broken", enabled=True, path="/nope")],
    )
    row = plugins.cmd_list(config)[0]
    assert row["state"] == "error"
    assert "reason" in row


def test_cmd_list_keeps_good_plugin_state_when_another_is_broken(tmp_path):
    directory = _write_plugin_dir(tmp_path, "good", cap_id="cap_good")
    config = _config(tmp_path)
    plugins.write_manifest(
        config.plugin_manifest,
        [
            plugins.PluginEntry(name="good", path=str(directory)),
            plugins.PluginEntry(name="broken", path=str(tmp_path / "missing")),
        ],
    )

    rows = {row["name"]: row for row in plugins.cmd_list(config)}

    assert rows["good"]["state"] == "active"
    assert rows["broken"]["state"] == "error"


# ---------------------------------------------------------------------------
# pip base command preference
# ---------------------------------------------------------------------------
def test_pip_base_command_prefers_uv(monkeypatch):
    monkeypatch.setattr(plugins.shutil, "which", lambda name: "/usr/bin/uv")
    assert plugins.pip_base_command() == ["uv", "pip"]


def test_pip_base_command_falls_back(monkeypatch):
    monkeypatch.setattr(plugins.shutil, "which", lambda name: None)
    assert plugins.pip_base_command() == [sys.executable, "-m", "pip"]


# ---------------------------------------------------------------------------
# main_v2 plugin CLI dispatch + agent extra_capabilities merge
# ---------------------------------------------------------------------------
def test_main_plugin_list_dispatch(monkeypatch, capsys, tmp_path):
    import main_v2

    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(
        main_v2.V2Config, "from_env", lambda overrides: _config(tmp_path)
    )
    assert main_v2.main(["plugin", "list"]) == 0
    assert "none configured" in capsys.readouterr().out


def test_main_plugin_error_nonzero_exit(monkeypatch, tmp_path, capsys):
    import main_v2

    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    cfg = _config(tmp_path, plugin_index=str(tmp_path / "missing.json"))
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: cfg)
    assert main_v2.main(["plugin", "search", "x"]) == 1
    assert "error:" in capsys.readouterr().err


def test_external_capabilities_off_switch(tmp_path):
    import main_v2

    config = _config(tmp_path, plugins_enabled=False)
    assert main_v2._external_capabilities(config) == []


def test_external_capabilities_fail_visible(tmp_path, monkeypatch):
    import main_v2

    config = _config(tmp_path)
    monkeypatch.setattr(
        main_v2.V2Config, "from_env", lambda overrides: config, raising=False
    )

    def boom(_config):
        raise plugins.PluginError("bad plugin")

    monkeypatch.setattr(plugins, "discover_external_specs", boom)
    with pytest.raises(SystemExit) as excinfo:
        main_v2._external_capabilities(config)
    assert excinfo.value.code == 1


def test_main_task_loads_authorized_local_plugin(tmp_path, monkeypatch):
    import main_v2
    from phone_agent.v2 import agent as agent_module

    plugin_dir = _write_plugin_dir(tmp_path, "cli_plugin", cap_id="cli_plugin")
    config = _config(tmp_path)
    config.app_kb_enabled = False
    config.dream_mode = "manual"
    plugins.write_manifest(
        config.plugin_manifest,
        [plugins.PluginEntry(name="cli_plugin", path=str(plugin_dir))],
    )
    captured = {}

    class FakeAgent:
        def __init__(self, config, *, extra_capabilities):
            captured["capabilities"] = extra_capabilities
            self.session = SimpleNamespace()

        def run(self, task):
            return SimpleNamespace(
                success=True, reason="done", steps=0, trace_path=None
            )

    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)
    monkeypatch.setattr(agent_module, "ThinPhoneAgent", FakeAgent)

    assert main_v2.main(["test task"]) == 0
    assert [spec.cap_id for spec in captured["capabilities"]] == ["cli_plugin"]


def test_agent_registers_extra_capabilities():
    from phone_agent.v2.capabilities import build_capability_registry

    registry = build_capability_registry(SimpleNamespace())
    before = {row["cap_id"] for row in registry.status()}
    extra = CapabilitySpec("plug_extra", "Extra", "on")
    registry.register(extra)
    after = {row["cap_id"] for row in registry.status()}
    assert after - before == {"plug_extra"}
