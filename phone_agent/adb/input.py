"""Input utilities for Android device text input."""

import base64
import shlex
import subprocess

from phone_agent.adb.errors import KeyboardPreparationError, require_adb_success


def type_text(text: str, device_id: str | None = None) -> None:
    """
    Type text into the currently focused input field using ADB Keyboard.

    Args:
        text: The text to type.
        device_id: Optional ADB device ID for multi-device setups.

    The base64 payload is shell-quoted before dispatch because ``adb shell`` joins
    its arguments and the device-side shell re-tokenizes them; an unquoted empty
    payload (the keyboard warm-up) would otherwise disappear and make the remote
    ``am broadcast`` fail with a missing ``--es msg`` value.

    Note:
        Requires ADB Keyboard to be installed on the device.
        See: https://github.com/nicnocquee/AdbKeyboard
    """
    adb_prefix = _get_adb_prefix(device_id)
    encoded_text = base64.b64encode(text.encode("utf-8")).decode("utf-8")

    result = subprocess.run(
        adb_prefix
        + [
            "shell",
            "am",
            "broadcast",
            "-a",
            "ADB_INPUT_B64",
            "--es",
            "msg",
            shlex.quote(encoded_text),
        ],
        capture_output=True,
        text=True,
    )
    require_adb_success(result, "text input")


def clear_text(device_id: str | None = None) -> None:
    """
    Clear text in the currently focused input field.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
    """
    adb_prefix = _get_adb_prefix(device_id)

    result = subprocess.run(
        adb_prefix + ["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
        capture_output=True,
        text=True,
    )
    require_adb_success(result, "clear text")


def detect_and_set_adb_keyboard(device_id: str | None = None) -> str:
    """
    Detect current keyboard and switch to ADB Keyboard if needed.

    Args:
        device_id: Optional ADB device ID for multi-device setups.

    Returns:
        The original keyboard IME identifier for later restoration.
    """
    adb_prefix = _get_adb_prefix(device_id)

    # Get current IME
    try:
        result = subprocess.run(
            adb_prefix
            + ["shell", "settings", "get", "secure", "default_input_method"],
            capture_output=True,
            text=True,
        )
        require_adb_success(result, "keyboard query", outcome_unknown=False)
    except Exception as exc:
        raise KeyboardPreparationError("query", "not_needed") from exc
    current_ime = (result.stdout or "").strip()

    switched = "com.android.adbkeyboard/.AdbIME" not in current_ime
    if switched:
        try:
            switch_result = subprocess.run(
                adb_prefix
                + ["shell", "ime", "set", "com.android.adbkeyboard/.AdbIME"],
                capture_output=True,
                text=True,
            )
            require_adb_success(switch_result, "keyboard switch")
        except Exception as exc:
            raise KeyboardPreparationError("switch", "unknown") from exc

    try:
        type_text("", device_id)
    except Exception as exc:
        if not switched:
            raise KeyboardPreparationError("warmup", "not_needed") from exc
        if not current_ime:
            raise KeyboardPreparationError("warmup", "unknown") from exc
        try:
            restore_keyboard(current_ime, device_id)
        except Exception as restore_exc:
            raise KeyboardPreparationError("warmup", "failed") from restore_exc
        raise KeyboardPreparationError("warmup", "restored") from exc

    return current_ime


def restore_keyboard(ime: str, device_id: str | None = None) -> None:
    """
    Restore the original keyboard IME.

    Args:
        ime: The IME identifier to restore.
        device_id: Optional ADB device ID for multi-device setups.
    """
    adb_prefix = _get_adb_prefix(device_id)

    result = subprocess.run(
        adb_prefix + ["shell", "ime", "set", ime], capture_output=True, text=True
    )
    require_adb_success(result, "keyboard restore")


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]
