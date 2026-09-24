"""Round-1 CLI seam (G1): external plugin commands reach the dispatcher.

``main_v2`` mounts maintenance commands through the assembled ``cli_handlers``
registry; these tests pin that an external capability's ``add_cli_command`` is
actually invocable from argv (``--<command>``, optional ``=VALUE``), that a
harness flag still dispatches exactly as before, and that an unrecognized
leftover token stays a hard argparse-style error instead of being ignored.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import main_v2


def _config(tmp_path, **overrides) -> SimpleNamespace:
    values = {
        "plugins_enabled": True,
        "plugin_manifest": str(tmp_path / ".taskwizard.toml"),
        "plugin_index": str(tmp_path / "index.json"),
        "app_kb_enabled": True,
        "dream_mode": "manual",
        "memory_dir": str(tmp_path / "memory"),
        "device_id": "serial-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _write_plugin(tmp_path, name: str = "cli_plug", cap_id: str = "cli_plug"):
    """A local plugin whose only contribution is one CLI command."""

    directory = tmp_path / name
    directory.mkdir()
    (directory / "plugin.py").write_text(
        "import json\n"
        "from phone_agent.v2.capabilities import CapabilitySpec\n"
        "\n"
        "def apply(ctx):\n"
        "    def handler(args):\n"
        "        print('plugin-cli: ' + json.dumps({\n"
        "            'name': 'ping',\n"
        "            'value': getattr(args, 'ping', None),\n"
        "            'device': getattr(args, 'device_id', None),\n"
        "        }, sort_keys=True))\n"
        "        return 0\n"
        "    ctx.add_cli_command('ping', handler)\n"
        "\n"
        f"CAPABILITY = CapabilitySpec({cap_id!r}, 'CLI Plug', 'on', apply=apply)\n",
        encoding="utf-8",
    )
    return directory


def _write_manifest(config, directory) -> None:
    from phone_agent.v2 import plugins

    plugins.write_manifest(
        config.plugin_manifest,
        [plugins.PluginEntry(name="cli_plug", path=str(directory))],
    )


def _install(monkeypatch, config) -> None:
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)


def test_plugin_cli_command_is_invocable(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    # The handler receives the parsed namespace, so a plugin command can coexist
    # with the ordinary flags.
    assert main_v2.main(["--ping", "--device-id", "serial-9"]) == 0

    assert json.loads(capsys.readouterr().out.strip().removeprefix("plugin-cli: ")) == {
        "name": "ping",
        "value": True,
        "device": "serial-9",
    }


def test_plugin_cli_command_accepts_an_inline_value(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    assert main_v2.main(["--ping=hello"]) == 0

    payload = json.loads(
        capsys.readouterr().out.strip().removeprefix("plugin-cli: ")
    )
    assert payload["value"] == "hello"


def test_unknown_flag_stays_a_hard_error(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--pign"])

    assert excinfo.value.code == 2


def test_plugin_cli_command_is_gated_by_the_plugins_switch(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path, plugins_enabled=False)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--ping"])

    assert excinfo.value.code == 2


def test_plugin_command_cannot_ride_along_with_a_task(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["做任务", "--ping"])

    assert excinfo.value.code == 2


def test_plugin_command_cannot_ride_along_with_a_harness_command(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--dream", "--ping"])

    assert excinfo.value.code == 2


def test_stray_positional_is_an_argv_mistake_not_a_plugin_command(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["做任务", "extra"])

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# ``--`` end-of-options separator (H4)
# ---------------------------------------------------------------------------
def _capturing_agent(monkeypatch, tasks: list[str]) -> None:
    from phone_agent.v2 import agent as agent_module

    class FakeAgent:
        def __init__(self, config, *, extra_capabilities=()):
            self.session = SimpleNamespace()

        def run(self, task):
            tasks.append(task)
            return SimpleNamespace(
                success=True, reason="done", steps=0, trace_path=None
            )

    monkeypatch.setattr(agent_module, "ThinPhoneAgent", FakeAgent)


def test_separator_turns_a_command_like_token_into_task_text(
    tmp_path, monkeypatch
) -> None:
    """After ``--`` the token is task text even when a same-named command exists."""

    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)
    tasks: list[str] = []
    _capturing_agent(monkeypatch, tasks)

    assert main_v2.main(["--", "ping"]) == 0
    assert main_v2.main(["--", "--ping"]) == 0

    assert tasks == ["ping", "--ping"]


def test_separator_keeps_trailing_tokens_out_of_command_matching(
    tmp_path, monkeypatch
) -> None:
    """Leftovers after ``--`` are argv errors, never capability commands."""

    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--", "做任务", "--ping"])

    assert excinfo.value.code == 2


def test_command_like_task_without_separator_stays_an_argv_error(
    tmp_path, monkeypatch
) -> None:
    """Without ``--`` an unregistered option-like token keeps erroring (locked)."""

    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--ping-task"])

    assert excinfo.value.code == 2


def test_separator_still_keeps_harness_flags_working(tmp_path, monkeypatch, capsys) -> None:
    """A harness flag before ``--`` and task text after it coexist."""

    config = _config(tmp_path)
    _install(monkeypatch, config)
    calls: list[tuple[bool, object]] = []
    monkeypatch.setattr(
        main_v2,
        "_run_dream",
        lambda config, *, light, store=None: calls.append((light, store)) or {},
    )

    # ``--dream -- X`` is a contradiction (command + task) and must error.
    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--dream", "--", "做任务"])
    assert excinfo.value.code == 2

    assert calls == []
    assert capsys.readouterr().err.strip()


def test_cli_context_registers_harness_and_plugin_commands(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)

    commands = main_v2._build_cli_capability_context(config).cli_commands

    assert "ping" in commands
    assert {
        "dream",
        "rebuild_vec",
        "distill",
        "review_lessons",
        "approve_lesson",
        "revoke_lesson",
        "supersede_lesson",
        "learn_alias",
        "forget_alias",
    } <= set(commands)


def test_harness_flag_still_dispatches_through_the_registry(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _config(tmp_path)
    _install(monkeypatch, config)
    calls: list[tuple[bool, object]] = []
    monkeypatch.setattr(
        main_v2,
        "_run_dream",
        lambda config, *, light, store=None: calls.append((light, store)) or {},
    )

    assert main_v2.main(["--dream"]) == 0

    assert calls == [(False, None)]
    assert "dream:" in capsys.readouterr().out


def test_two_harness_commands_at_once_fail_visible(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _install(monkeypatch, config)

    with pytest.raises(SystemExit) as excinfo:
        main_v2.main(["--dream", "--learn-alias", "x=y"])

    assert excinfo.value.code == 2


def test_parser_maintenance_dests_match_the_declared_flags() -> None:
    parser = main_v2.build_parser()

    assert tuple(parser.maintenance_dests) == (
        "dream",
        "rebuild_vec",
        "distill",
        "review_lessons",
        "approve_lesson",
        "revoke_lesson",
        "supersede_lesson",
        "learn_alias",
        "forget_alias",
        "list_models",
    )


def test_task_run_never_assembles_the_cli_context(tmp_path, monkeypatch) -> None:
    """A task run must not apply plugin code a second time for CLI discovery."""

    from phone_agent.v2 import agent as agent_module

    config = _config(tmp_path)
    _write_manifest(config, _write_plugin(tmp_path))
    _install(monkeypatch, config)
    assemblies: list[str] = []
    monkeypatch.setattr(
        main_v2,
        "_build_cli_capability_context",
        lambda *args, **kwargs: assemblies.append("built"),
    )

    class FakeAgent:
        def __init__(self, config, *, extra_capabilities=()):
            self.session = SimpleNamespace()

        def run(self, task):
            return SimpleNamespace(
                success=True, reason="done", steps=0, trace_path=None
            )

    monkeypatch.setattr(agent_module, "ThinPhoneAgent", FakeAgent)

    assert main_v2.main(["做任务"]) == 0
    assert assemblies == []
