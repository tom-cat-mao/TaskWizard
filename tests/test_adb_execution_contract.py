from __future__ import annotations

import base64
import shlex
import subprocess

import pytest

from phone_agent.adb import device
from phone_agent.adb import input as adb_input


def _result(*, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _remote_shell_argv(command: list[str]) -> list[str]:
    """Re-tokenize an ``adb shell`` command the way the device-side shell does."""
    return shlex.split(" ".join(command[command.index("shell") + 1 :]))


def _broadcast_payload(command: list[str]) -> str:
    """Return the single ``--es msg`` value as the device-side shell receives it."""
    argv = _remote_shell_argv(command)
    index = argv.index("--es")
    assert argv[index + 1] == "msg"
    value_tokens = argv[index + 2 :]
    assert len(value_tokens) == 1, f"expected one msg value, got {value_tokens!r}"
    return value_tokens[0]


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: device.tap(123, 456, delay=0),
        lambda: device.long_press(123, 456, delay=0),
        lambda: device.swipe(123, 456, 789, 999, delay=0),
        lambda: device.back(delay=0),
    ],
)
def test_device_action_nonzero_exit_is_not_reported_as_success(
    monkeypatch, invoke
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _result(
            returncode=1, stderr="permission denied at 123 456 secret-value"
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        invoke()

    message = str(raised.value)
    assert "unknown" in message.lower()
    assert "123" not in message
    assert "secret-value" not in message


def test_device_action_permission_diagnostic_with_zero_exit_is_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _result(
            stdout="java.lang.SecurityException: Injecting to another application requires INJECT_EVENTS"
        ),
    )

    with pytest.raises(RuntimeError, match="unknown"):
        device.tap(10, 20, delay=0)


def test_home_keeps_successful_activity_fallback(monkeypatch) -> None:
    results = iter(
        [
            _result(returncode=1, stderr="INJECT_EVENTS denied"),
            _result(stdout="Starting: Intent"),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)

    device.home(delay=0)

    assert len(calls) == 2
    assert calls[0][-2:] == ["keyevent", "KEYCODE_HOME"]
    assert "android.intent.category.HOME" in calls[1]


def test_home_failed_fallback_is_not_reported_as_success(monkeypatch) -> None:
    results = iter(
        [
            _result(returncode=1, stderr="INJECT_EVENTS denied"),
            _result(returncode=1, stderr="Permission Denial: secret component"),
        ]
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: next(results)
    )

    with pytest.raises(RuntimeError) as raised:
        device.home(delay=0)

    assert "unknown" in str(raised.value).lower()
    assert "secret component" not in str(raised.value)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: adb_input.type_text("typed-secret"),
        lambda: adb_input.clear_text(),
        lambda: adb_input.restore_keyboard("secret.ime/.IME"),
    ],
)
def test_input_command_failure_is_safe_and_visible(monkeypatch, invoke) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _result(
            returncode=1, stderr="Permission denial typed-secret secret.ime/.IME"
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        invoke()

    message = str(raised.value)
    assert "unknown" in message.lower()
    assert "typed-secret" not in message
    assert "secret.ime" not in message


def test_keyboard_detection_checks_query_and_switch_results(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _result(returncode=1, stderr="device offline"),
    )
    with pytest.raises(RuntimeError, match="before text dispatch"):
        adb_input.detect_and_set_adb_keyboard()

    results = iter(
        [
            _result(stdout="com.original/.IME\n"),
            _result(returncode=1, stderr="Permission denial"),
        ]
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: next(results)
    )
    with pytest.raises(RuntimeError, match="before text dispatch"):
        adb_input.detect_and_set_adb_keyboard()


def test_keyboard_warmup_failure_restores_confirmed_switch(monkeypatch) -> None:
    results = iter(
        [
            _result(stdout="com.original/.IME\n"),
            _result(),
            _result(returncode=1, stderr="warmup secret"),
            _result(),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(adb_input.KeyboardPreparationError) as raised:
        adb_input.detect_and_set_adb_keyboard()

    assert raised.value.stage == "warmup"
    assert raised.value.keyboard_cleanup == "restored"
    assert [command[-3:] for command in calls if "ime" in command] == [
        ["ime", "set", "com.android.adbkeyboard/.AdbIME"],
        ["ime", "set", "com.original/.IME"],
    ]
    assert len([command for command in calls if "ADB_INPUT_B64" in command]) == 1
    assert "com.original" not in str(raised.value)
    assert "warmup secret" not in str(raised.value)


def test_keyboard_warmup_failure_reports_failed_restore(monkeypatch) -> None:
    results = iter(
        [
            _result(stdout="com.original/.IME\n"),
            _result(),
            _result(returncode=1),
            _result(returncode=1, stderr="restore secret"),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(adb_input.KeyboardPreparationError) as raised:
        adb_input.detect_and_set_adb_keyboard()

    assert raised.value.stage == "warmup"
    assert raised.value.keyboard_cleanup == "failed"
    assert len([command for command in calls if command[-3:-1] == ["ime", "set"]]) == 2
    assert "restore secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("results", "stage", "cleanup", "expected_calls"),
    [
        ([_result(returncode=1, stderr="query secret")], "query", "not_needed", 1),
        (
            [
                _result(stdout="com.original/.IME\n"),
                _result(returncode=1, stderr="switch secret"),
            ],
            "switch",
            "unknown",
            2,
        ),
        (
            [
                _result(stdout="com.android.adbkeyboard/.AdbIME\n"),
                _result(returncode=1, stderr="warmup secret"),
            ],
            "warmup",
            "not_needed",
            2,
        ),
    ],
)
def test_keyboard_preparation_failure_reports_known_cleanup_state(
    monkeypatch, results, stage, cleanup, expected_calls
) -> None:
    calls: list[list[str]] = []
    scripted = iter(results)

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(scripted)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(adb_input.KeyboardPreparationError) as raised:
        adb_input.detect_and_set_adb_keyboard()

    assert raised.value.stage == stage
    assert raised.value.keyboard_cleanup == cleanup
    assert len(calls) == expected_calls
    assert not any(
        command[-3:-1] == ["ime", "set"]
        and command[-1] != "com.android.adbkeyboard/.AdbIME"
        for command in calls
    )


def test_keyboard_success_sends_payload_once_then_restores(monkeypatch) -> None:
    results = iter([_result(stdout="com.original/.IME\n"), *[_result()] * 4])
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)

    original = adb_input.detect_and_set_adb_keyboard()
    adb_input.type_text("user payload")
    adb_input.restore_keyboard(original)

    encoded = base64.b64encode(b"user payload").decode("utf-8")
    broadcasts = [command for command in calls if "ADB_INPUT_B64" in command]
    assert [_broadcast_payload(command) for command in broadcasts] == ["", encoded]
    assert calls[-1][-3:] == ["ime", "set", "com.original/.IME"]


def test_type_text_empty_payload_survives_remote_shell_reparse(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    adb_input.type_text("")

    (command,) = calls
    assert command[-1] != "", "empty payload must stay a non-empty adb argv token"
    assert _broadcast_payload(command) == ""


@pytest.mark.parametrize(
    "text",
    ["", "沈阳旅游", "hello world", "  ", "a'b\"c$d;e|f", "emoji 🚀", "line1\nline2"],
)
def test_type_text_payload_round_trips_through_remote_shell(monkeypatch, text) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs.get("shell", False) is False
        return _result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    adb_input.type_text(text)

    (command,) = calls
    payload = _broadcast_payload(command)
    assert payload == base64.b64encode(text.encode("utf-8")).decode("utf-8")
    assert base64.b64decode(payload).decode("utf-8") == text
