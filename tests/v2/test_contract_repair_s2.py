"""S2 harness-contract regressions for observation state and rendering.

All cases use synthetic screenshots/devices/providers.  No ADB, model, network,
MLX, environment file, or user-memory access is involved.
"""

from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
from PIL import Image
import pytest

from phone_agent.adb.screenshot import Screenshot
from phone_agent.grounding.provider import MarkCandidate, MarkProviderResult
from phone_agent.v2.agent import ThinPhoneAgent, _first_observation_content
from phone_agent.v2.middleware.diagnostic import _parse_obs_block
from phone_agent.v2.middleware.images import ContextPrunerService
from phone_agent.v2.session import (
    LocateAmbiguousError,
    PhoneSession,
    ScreenshotError,
)
from phone_agent.v2.tools._obs import auto_observation


_SETTINGS_XML = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1000,300]" />'
    "</hierarchy>"
)


def _png(width: int, height: int) -> str:
    image = Image.new("RGB", (width, height), color=(20, 40, 60))
    output = BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _config(**overrides):
    values = {
        "device_id": None,
        "accessibility_timeout": 3.0,
        "accessibility_max_marks": 80,
        "grounding_provider": "fake",
        "locateanything_model": None,
        "locateanything_max_size": 960,
        "locateanything_context_max_chars": 200,
        "locate_max_size": 0,
        "scope_padding_ratio": 0.0,
        "observe_settle_ms": 0,
        "marks_windowed": "off",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Foreground:
    component_name = "com.example/.Main"
    package_name = "com.example"
    display_name = "Example"


class _DumpThenBadScreenshotDevice:
    def __init__(self) -> None:
        self.screenshot_calls = 0
        self.dump_calls = 0

    def get_screenshot(self, device_id=None, **kwargs):
        self.screenshot_calls += 1
        if self.screenshot_calls == 1:
            return Screenshot(_png(1200, 2400), width=1200, height=2400)
        return Screenshot(
            "",
            width=0,
            height=0,
            is_valid=False,
            failure_code="adb_screencap_failed",
        )

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        self.dump_calls += 1
        raise TimeoutError("synthetic dump timeout")

    def get_foreground_app(self, device_id=None):
        return _Foreground()


def test_dump_failure_then_invalid_retry_does_not_commit_first_frame():
    session = PhoneSession(_config(), device_factory=_DumpThenBadScreenshotDevice())
    session.epoch = 4
    session._last_width = 1080
    session._last_height = 2400
    old = MarkCandidate(
        "ax_old@e4", [0, 0, 100, 100], [50, 50], epoch=4
    )
    session.marks = {old.mark_id: old}

    with pytest.raises(ScreenshotError, match="adb_screencap_failed"):
        session.observe()

    assert session.epoch == 4
    assert session.screen_seq == 0
    assert session.marks == {}
    assert (session.screen_width, session.screen_height) == (1080, 2400)


class _DumpThenUnstableForegroundDevice(_DumpThenBadScreenshotDevice):
    def __init__(self) -> None:
        super().__init__()
        self.foregrounds = iter(["A/.Main", "A/.Main", "A/.Main", "B/.Main"])

    def get_screenshot(self, device_id=None, **kwargs):
        self.screenshot_calls += 1
        return Screenshot(_png(1200, 2400), width=1200, height=2400)

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        self.dump_calls += 1
        if self.dump_calls == 1:
            raise TimeoutError("synthetic dump timeout")
        return _SETTINGS_XML

    def get_foreground_app(self, device_id=None):
        component = next(self.foregrounds)
        return SimpleNamespace(
            component_name=component,
            package_name=component.split("/", 1)[0],
            display_name=component,
        )


def test_dump_failure_then_unstable_retry_does_not_commit_first_frame():
    session = PhoneSession(
        _config(), device_factory=_DumpThenUnstableForegroundDevice()
    )
    session.epoch = 4
    session._last_width = 1080
    session._last_height = 2400

    with pytest.raises(ScreenshotError, match="observation unstable"):
        session.observe()

    assert session.epoch == 4
    assert session.screen_seq == 0
    assert session.marks == {}
    assert (session.screen_width, session.screen_height) == (1080, 2400)


class _LocateMissProvider:
    name = "synthetic-miss"
    version = "test"

    def __init__(self) -> None:
        self.input_sizes: list[tuple[int, int]] = []

    def provide_marks(
        self, screenshot, screen_binding, hints=None, timeout=None, max_size=None
    ):
        self.input_sizes.append((screenshot.width, screenshot.height))
        return MarkProviderResult(
            success=False,
            provider=self.name,
            failure_code="grounding_no_candidate",
        )


class _RotatedDevice:
    def __init__(self) -> None:
        self.shot = Screenshot(_png(2000, 1000), width=2000, height=1000)

    def get_screenshot(self, device_id=None, **kwargs):
        return self.shot

    def get_foreground_app(self, device_id=None):
        return _Foreground()


def test_scoped_locate_miss_uses_fresh_frame_geometry_without_committing_it():
    provider = _LocateMissProvider()
    session = PhoneSession(_config(), device_factory=_RotatedDevice())
    session.epoch = 2
    session.screen_seq = 7
    session._last_width = 1000
    session._last_height = 2000
    scope = MarkCandidate(
        "ax_scope@e2",
        [0, 0, 500, 500],
        [250, 250],
        role="View",
        epoch=2,
    )
    session.marks = {scope.mark_id: scope}
    session._locate_provider = provider
    session._locate_provider_built = True

    with pytest.raises(LocateAmbiguousError):
        session.locate("missing", scope_mark_id=scope.mark_id)

    assert provider.input_sizes == [(1000, 500)]
    assert session.epoch == 2
    assert session.screen_seq == 7
    assert session.marks == {scope.mark_id: scope}
    assert (session.screen_width, session.screen_height) == (1000, 2000)


def test_unscoped_locate_miss_without_prior_geometry_keeps_geometry_unknown():
    provider = _LocateMissProvider()
    session = PhoneSession(_config(), device_factory=_RotatedDevice())
    session._locate_provider = provider
    session._locate_provider_built = True

    with pytest.raises(LocateAmbiguousError):
        session.locate("missing")

    assert provider.input_sizes == [(2000, 1000)]
    assert session.epoch == 0
    assert session.screen_seq == 0
    assert (session.screen_width, session.screen_height) == (0, 0)


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_opening_observation_uses_real_mime_and_dump_failure_annotation(mime):
    obs = SimpleNamespace(
        screenshot_b64="QUJD",
        mime_type=mime,
        screen_seq=3,
        current_app="com.example",
        marks=[],
        marks_failure_code="timeout",
        parse_summary={"total_candidates": 0},
        windows=None,
    )

    content = _first_observation_content(obs, "do it")

    image = next(block for block in content if block.get("type") == "image_url")
    text = next(block["text"] for block in content if str(block.get("text", "")).startswith("[OBS]"))
    assert image["image_url"]["url"].startswith(f"data:{mime};base64,")
    assert "marks (0) [accessibility:timeout]" in text


def test_initial_observation_failure_is_visible_and_descriptive():
    class _FailingSession:
        def observe(self):
            raise ScreenshotError(
                "screenshot invalid: adb_screencap_failed",
                failure_code="adb_screencap_failed",
            )

    agent = object.__new__(ThinPhoneAgent)
    agent.session = _FailingSession()
    agent._system_prompt = "system"
    agent._capability_ctx = None
    agent._render_lesson_prompt_block = lambda: None

    messages = agent._initial_messages("do it")

    human = next(message for message in messages if isinstance(message, HumanMessage))
    texts = [block["text"] for block in human.content if block.get("type") == "text"]
    assert texts[0] == "do it"
    assert texts[1].startswith("[OBS] (opening observation failed:")
    assert "adb_screencap_failed" in texts[1]
    assert "retry" not in texts[1].casefold()


def _window_mark(index: int, window_id: str, layer: int, text: str) -> MarkCandidate:
    return MarkCandidate(
        f"ax_{index}@e1",
        [0, index, 10, index + 10],
        [5, index + 5],
        role="Button",
        text_summary=text,
        window_id=window_id,
        window_layer=layer,
        window_type="TYPE_SYSTEM" if layer > 10 else "TYPE_APPLICATION",
        package="com.popup" if layer > 10 else "com.content",
        actionability="blocked" if layer <= 10 else "confirmed",
        epoch=1,
    )


def test_final_digest_budget_keeps_late_top_popup_and_reports_all_counts():
    marks = [
        *[_window_mark(i, "W1", 10, f"background-{i}") for i in range(1, 46)],
        _window_mark(46, "W2", 42, "popup-allow"),
        _window_mark(47, "W2", 42, "popup-deny"),
    ]
    obs = SimpleNamespace(
        screenshot_b64="",
        screen_seq=1,
        current_app="com.content",
        marks=marks,
        marks_failure_code=None,
        parse_summary={"window_source": "shell_windows", "total_candidates": 100},
        windows=[],
    )

    opening = _first_observation_content(obs, "do it", session=PhoneSession)
    opening_text = next(
        block["text"]
        for block in opening
        if str(block.get("text", "")).startswith("[OBS]")
    )

    class _ObservedSession:
        format_marks_digest = staticmethod(PhoneSession.format_marks_digest)

        def observe(self):
            return obs

    regular_text = auto_observation(_ObservedSession())[0]["text"]

    for text in (opening_text, regular_text):
        assert "marks (40/100) [retained:47]:" in text
        assert "popup-allow" in text
        assert "popup-deny" in text
        assert "op=blocked" in text
        assert "... (+7 more)" in text
        assert sum(line.startswith("  ax_") for line in text.splitlines()) == 40

    parsed = _parse_obs_block(regular_text)
    assert parsed is not None
    assert parsed["mark_count"] == 40

    old = HumanMessage(content=[{"type": "text", "text": regular_text}])
    newest = HumanMessage(
        content=[
            {"type": "text", "text": "[OBS] app=x screen#2\nmarks (0): "}
        ]
    )
    ContextPrunerService(keep_images=2, keep_marks=1).prune([old, newest])
    assert old.content == [
        {
            "type": "text",
            "text": "[OBS] app=com.content screen#1 [marks 已折叠:40/100]",
        }
    ]


def test_final_digest_budget_prefers_highest_windows_when_windows_exceed_budget():
    marks = [
        _window_mark(index, f"W{index}", index, f"window-{index}")
        for index in range(1, 43)
    ]

    digest = PhoneSession.format_marks_digest(
        marks, max_items=40, window_source="shell_windows"
    )

    assert "window-42" in digest
    assert "window-41" in digest
    assert "ax_1@e1 |" not in digest
    assert "ax_2@e1 |" not in digest
    assert "... (+2 more)" in digest
