"""WP2: the exec-out screencap channel — one roundtrip, fail-closed on every fault.

``adb exec-out screencap -p`` returns the PNG on stdout (one ADB roundtrip); the
legacy write-on-device + ``pull`` + ``rm`` path stays reachable through
``PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT=off`` or ``use_exec_out=False``. Both paths
must answer failure the same way: an invalid placeholder carrying a stable
``failure_code``, never a fake success (P0 #5).
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from phone_agent.adb import screenshot as screenshot_mod


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clean_switch(monkeypatch):
    """No ambient switch/format state: every test states what it exercises."""

    monkeypatch.delenv("PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT", raising=False)
    monkeypatch.delenv("PHONE_AGENT_BLACK_SCREEN_DETECT", raising=False)
    # PNG output keeps the assertions independent of the JPEG quality knob.
    monkeypatch.setenv("PHONE_AGENT_SCREENSHOT_FORMAT", "png")
    yield


def _run_returning(*, stdout: bytes = b"", returncode: int = 0):
    """``subprocess.run`` double: one CompletedProcess-shaped answer."""

    def fake_run(args, **_kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")

    return fake_run


def test_exec_out_reads_the_png_from_stdout_in_one_roundtrip(monkeypatch):
    calls: list[list[str]] = []
    image = Image.new("RGB", (24, 40), (10, 200, 10))

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=_png_bytes(image), stderr=b"")

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)

    shot = screenshot_mod.get_screenshot(device_id="serial", use_exec_out=True)

    assert shot.is_valid is True
    assert shot.is_placeholder is False
    assert shot.width == 24
    assert shot.height == 40
    assert shot.mime_type == "image/png"
    assert shot.base64_data
    # Exactly one ADB invocation, and it is the exec-out form: no device-side
    # temp file, no pull, no cleanup roundtrip.
    assert calls == [["adb", "-s", "serial", "exec-out", "screencap", "-p"]]


def test_exec_out_is_the_default_channel(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(
            returncode=0, stdout=_png_bytes(Image.new("RGB", (8, 8), "white")), stderr=b""
        )

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)

    shot = screenshot_mod.get_screenshot(use_exec_out=None)

    assert shot.is_valid is True
    assert calls == [["adb", "exec-out", "screencap", "-p"]]


def test_exec_out_switch_off_routes_to_the_legacy_path(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT", "off")

    shot = screenshot_mod.get_screenshot(use_exec_out=None)

    # The legacy write-on-device command ran (and failed closed below); the
    # exec-out form was never used.
    assert calls and calls[0][:3] == ["adb", "shell", "screencap"]
    assert all("exec-out" not in call for call in calls)
    assert shot.is_valid is False
    assert shot.failure_code == "adb_screencap_failed"


def test_explicit_argument_beats_the_env_switch(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(
            returncode=0, stdout=_png_bytes(Image.new("RGB", (8, 8), "white")), stderr=b""
        )

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT", "off")

    assert screenshot_mod.get_screenshot(use_exec_out=True).is_valid is True
    assert calls == [["adb", "exec-out", "screencap", "-p"]]


def test_exec_out_protected_screen_is_blocked_and_invalid(monkeypatch):
    monkeypatch.setattr(
        screenshot_mod.subprocess,
        "run",
        _run_returning(stdout=_png_bytes(Image.new("RGB", (12, 20), (3, 3, 3)))),
    )

    shot = screenshot_mod.get_screenshot(use_exec_out=True, black_screen_detect=True)

    assert shot.is_valid is False
    assert shot.is_placeholder is True
    assert shot.is_sensitive is True
    assert shot.failure_code == "secure_screenshot_blocked"


def test_exec_out_empty_stdout_is_fail_closed(monkeypatch):
    monkeypatch.setattr(screenshot_mod.subprocess, "run", _run_returning(stdout=b""))

    shot = screenshot_mod.get_screenshot(use_exec_out=True)

    assert shot.is_valid is False
    assert shot.is_placeholder is True
    assert shot.failure_code == "empty_screenshot"


def test_exec_out_nonzero_status_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        screenshot_mod.subprocess, "run", _run_returning(returncode=1)
    )

    shot = screenshot_mod.get_screenshot(use_exec_out=True)

    assert shot.is_valid is False
    assert shot.failure_code == "adb_screencap_failed"


def test_exec_out_timeout_is_fail_closed(monkeypatch):
    def fake_run(args, **_kwargs):
        raise screenshot_mod.subprocess.TimeoutExpired(args, 10)

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)

    shot = screenshot_mod.get_screenshot(use_exec_out=True)

    assert shot.is_valid is False
    assert shot.is_placeholder is True
    assert shot.failure_code == "screenshot_timeout"


def test_exec_out_garbage_payload_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        screenshot_mod.subprocess,
        "run",
        _run_returning(stdout=b"not a png at all"),
    )

    shot = screenshot_mod.get_screenshot(use_exec_out=True)

    assert shot.is_valid is False
    assert shot.failure_code == "screenshot_exec_out_failed"


def test_legacy_unexpected_failure_is_still_fail_closed(monkeypatch):
    """The legacy ``finally`` cleanup must not swallow the failure channel."""

    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        if "screencap" in args:
            raise RuntimeError("adb died")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)

    shot = screenshot_mod.get_screenshot(use_exec_out=False)

    assert shot.is_valid is False
    assert shot.is_placeholder is True
    assert shot.failure_code == "screenshot_unavailable"
    # The device-side temp file was still cleaned up.
    assert any(call[:4] == ["adb", "shell", "rm", "-f"] for call in calls)
