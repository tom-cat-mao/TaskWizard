"""WP-O regression tests for black-screen detection and observation settle."""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from phone_agent.adb import screenshot as screenshot_mod
from phone_agent.v2.config import V2Config
from phone_agent.v2.session import PhoneSession, ScreenshotError
from phone_agent.v2.tools import build_tools
from phone_agent.v2.tools.actuation import build_actuation_tools
from phone_agent.v2.tools._obs import auto_observation
from tests.v2._doubles import FakeConfig as ToolConfig
from tests.v2._doubles import FakePhoneSession, make_mark
from tests.v2.test_observation_lifecycle import FakeConfig, FakeDeviceFactory


def _install_screencap(
    monkeypatch,
    image: Image.Image | None,
    *,
    output: str = "",
    returncode: int = 0,
):
    """Make get_screenshot exercise its public decode path without ADB."""

    def fake_run(args, **_kwargs):
        if "screencap" in args:
            return SimpleNamespace(returncode=returncode, stdout=output, stderr="")
        if "pull" in args:
            assert image is not None
            image.save(args[-1], format="PNG")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(screenshot_mod.subprocess, "run", fake_run)


def test_uniform_black_png_uses_existing_secure_blocked_channel(monkeypatch):
    _install_screencap(monkeypatch, Image.new("RGB", (12, 20), (4, 4, 4)))

    shot = screenshot_mod.get_screenshot(black_screen_detect=True)

    assert shot.is_valid is False
    assert shot.is_placeholder is True
    assert shot.is_sensitive is True
    assert shot.failure_code == "secure_screenshot_blocked"
    assert shot.failure_message == "系统级保护页（登录/支付等），截图不可用"


def test_dark_ui_with_one_bright_pixel_remains_valid(monkeypatch):
    image = Image.new("RGB", (12, 20), (2, 2, 2))
    image.putpixel((6, 10), (220, 220, 220))
    _install_screencap(monkeypatch, image)

    shot = screenshot_mod.get_screenshot(black_screen_detect=True)

    assert shot.is_valid is True
    assert shot.is_sensitive is False
    assert shot.width == 12
    assert shot.height == 20


def test_screencap_secure_error_keeps_existing_failure_channel(monkeypatch):
    _install_screencap(monkeypatch, None, output="Status: -1")

    shot = screenshot_mod.get_screenshot(black_screen_detect=False)

    assert shot.is_valid is False
    assert shot.is_sensitive is True
    assert shot.failure_code == "secure_screenshot_blocked"


def test_screencap_nonzero_keeps_existing_failure_channel(monkeypatch):
    _install_screencap(monkeypatch, None, returncode=1)

    shot = screenshot_mod.get_screenshot(black_screen_detect=True)

    assert shot.is_valid is False
    assert shot.is_sensitive is False
    assert shot.failure_code == "adb_screencap_failed"


def test_black_screen_detection_can_be_disabled(monkeypatch):
    _install_screencap(monkeypatch, Image.new("RGB", (12, 20), "black"))

    shot = screenshot_mod.get_screenshot(black_screen_detect=False)

    assert shot.is_valid is True


def _real_session(*, settle_ms: int = 300, foreground=None) -> PhoneSession:
    config = FakeConfig()
    config.observe_settle_ms = settle_ms
    return PhoneSession(
        config,
        device_factory=FakeDeviceFactory(foreground=foreground),
    )


def test_observe_default_settle_is_300ms(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)

    _real_session().observe()

    assert sleeps == [0.3]


def test_observe_zero_disables_settle(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)

    _real_session(settle_ms=0).observe()

    assert sleeps == []


def test_observe_foreground_retry_settles_each_attempt(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)

    _real_session(
        foreground=["A/.X", "B/.Y", "B/.Y", "B/.Y"]
    ).observe()

    assert sleeps == [0.3, 0.3]


def test_observe_explicit_settle_replaces_default_and_clamps(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)
    session = _real_session(settle_ms=300)

    session.observe(settle_ms=2000)
    session.observe(settle_ms=99999)

    assert sleeps == [2.0, 5.0]


class _RecordingSession(FakePhoneSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settle_args: list[int | None] = []

    def observe(self, settle_ms: int | None = None):
        self.settle_args.append(settle_ms)
        return super().observe()


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(
        block.get("text", "")
        for block in result
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_action_settle_override_and_clamp_receipt():
    marks = {"ax_1": make_mark("ax_1", text="搜索")}
    session = _RecordingSession(marks)
    tools = {tool.name: tool for tool in build_tools(session, ToolConfig())}

    tools["tap"].invoke({"target_mark_id": "ax_1", "settle_ms": 2000})
    clamped = tools["back"].invoke({"settle_ms": 99999})

    assert session.settle_args == [2000, 5000]
    assert "settle_ms 已从 99999 clamp 为 5000ms" in _text(clamped)


def test_negative_action_settle_clamps_to_zero_with_receipt():
    session = _RecordingSession({})
    tools = {tool.name: tool for tool in build_tools(session, ToolConfig())}

    clamped = tools["home"].invoke({"settle_ms": -50})

    assert session.settle_args == [0]
    assert "settle_ms 已从 -50 clamp 为 0ms" in _text(clamped)


def test_execution_tool_override_drives_real_observe_sleep(monkeypatch):
    class ActionDevice(FakeDeviceFactory):
        def back(self, device_id=None, delay=None):
            return None

    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)
    config = FakeConfig()
    config.observe_settle_ms = 300
    session = PhoneSession(config, device_factory=ActionDevice())
    tools = {
        tool.name: tool for tool in build_actuation_tools(session, config)
    }

    tools["back"].invoke({"settle_ms": 2000})

    assert sleeps == [2.0]


def test_read_screen_supports_default_and_explicit_settle():
    session = _RecordingSession({})
    tools = {tool.name: tool for tool in build_tools(session, ToolConfig())}

    tools["read_screen"].invoke({})
    tools["read_screen"].invoke({"settle_ms": 2000})

    assert session.settle_args == [None, 2000]


def test_secure_screen_receipt_contains_required_guidance():
    class SecureDevice(FakeDeviceFactory):
        def get_screenshot(
            self, device_id=None, timeout=10, *, black_screen_detect=None
        ):
            return SimpleNamespace(
                is_valid=False,
                failure_code="secure_screenshot_blocked",
                failure_message="系统级保护页（登录/支付等），截图不可用",
            )

    config = FakeConfig()
    config.observe_settle_ms = 0
    session = PhoneSession(config, device_factory=SecureDevice())
    receipt = _text(auto_observation(session))

    assert "此屏被系统级保护（登录/支付页）" in receipt
    assert "截图不可用" in receipt
    assert "accessibility marks 为空" in receipt
    assert "涉及登录/支付时考虑 take_over 交人处理" in receipt
    assert "image_url" not in receipt
    assert session.marks == {}


def test_secure_screen_receipt_reports_remaining_marks():
    class SecureSession:
        marks = {"ax_1@e1": object(), "ax_2@e1": object()}

        def observe(self):
            raise ScreenshotError(
                "screenshot invalid: secure_screenshot_blocked",
                failure_code="secure_screenshot_blocked",
            )

    receipt = _text(auto_observation(SecureSession()))

    assert "accessibility marks 剩 2 个" in receipt


def test_every_execution_tool_and_read_screen_expose_settle_ms():
    session = _RecordingSession({})
    tools = {tool.name: tool for tool in build_tools(session, ToolConfig())}

    for name in (
        "tap",
        "long_press",
        "type_text",
        "swipe",
        "scroll",
        "back",
        "home",
        "launch_app",
        "read_screen",
    ):
        assert "settle_ms" in tools[name].args
    assert "settle_ms" not in tools["wait"].args


def test_observation_config_defaults_and_env(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_OBSERVE_SETTLE_MS", raising=False)
    monkeypatch.delenv("PHONE_AGENT_BLACK_SCREEN_DETECT", raising=False)
    defaults = V2Config.from_env()
    assert defaults.observe_settle_ms == 300
    assert defaults.black_screen_detect is True

    monkeypatch.setenv("PHONE_AGENT_OBSERVE_SETTLE_MS", "0")
    monkeypatch.setenv("PHONE_AGENT_BLACK_SCREEN_DETECT", "off")
    disabled = V2Config.from_env()
    assert disabled.observe_settle_ms == 0
    assert disabled.black_screen_detect is False


def test_negative_global_observe_settle_is_rejected(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_OBSERVE_SETTLE_MS", "-1")

    try:
        V2Config.from_env()
    except ValueError as exc:
        assert "PHONE_AGENT_OBSERVE_SETTLE_MS" in str(exc)
    else:
        raise AssertionError("negative observe settle must be rejected")
