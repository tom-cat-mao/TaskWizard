"""B-package regressions: a failed observation still returns a reference frame.

Contract covered (plan §4):

    - an observation attempt that captured a valid screenshot keeps that frame as
      an explicitly earlier/unverified reference when the atomic window cannot be
      committed (transient dump failure followed by an invalid retry, unstable
      foreground, etc.);
    - the reference frame never becomes a batch: ``epoch``/``screen_seq``/geometry
      stay frozen, ``marks`` stay invalidated, and old badged ids still fail
      closed in ``resolve_mark``;
    - ``auto_observation`` returns the reference image together with a factual
      earlier-sampled / unverified note, keeps an action-success receipt intact,
      and never fabricates an image when no valid frame was captured;
    - a protected/black screen (``secure_screenshot_blocked``) never exposes a
      retained frame — the secure-black boundary is not bypassed;
    - the committed zero-mark frame (valid screenshot + stable foreground +
      persistent dump failure) is unchanged and is NOT labelled a reference;
    - the opening observation path uses the same reference handling;
    - the diagnostic writer saves the reference under its own file name and never
      overwrites an existing ``screen-<seq>.png``;
    - reference images are pruned by the normal historical-image pass.

All cases drive the real ``PhoneSession`` and production renderers with fake
device responses; no ADB, model, network, MLX, environment file, or user memory
is touched.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage
from PIL import Image
import pytest

from phone_agent.grounding.provider import MarkCandidate
from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.middleware.diagnostic import DiagnosticEvidenceWriter
from phone_agent.v2.middleware.images import ContextPrunerService
from phone_agent.v2.run_events import WebEventMiddleware
from phone_agent.v2.session import PhoneSession, ScreenshotError, StaleMarkError
from phone_agent.v2.tools._obs import auto_observation

_SETTINGS_XML = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" />'
    "</hierarchy>"
)


def _png_b64(color: tuple[int, int, int] = (20, 40, 60)) -> str:
    image = Image.new("RGB", (24, 24), color=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class _ShotSpec:
    """Synthetic screenshot response: payload + real mime, or an invalid code."""

    def __init__(
        self,
        payload: str = "shot",
        *,
        mime: str = "image/png",
        failure_code: str | None = None,
    ) -> None:
        self.payload = payload
        self.mime = mime
        self.failure_code = failure_code
        self.valid = failure_code is None


def good(payload: str, mime: str = "image/png") -> _ShotSpec:
    return _ShotSpec(payload, mime=mime)


def bad(failure_code: str = "adb_screencap_failed") -> _ShotSpec:
    return _ShotSpec("", failure_code=failure_code)


class FakeShot:
    def __init__(self, spec: _ShotSpec) -> None:
        self.base64_data = spec.payload
        self.width = 1080
        self.height = 2400
        self.mime_type = spec.mime
        self.is_valid = spec.valid
        self.failure_code = spec.failure_code
        self.failure_message = None


class FakeForeground:
    def __init__(self, component: str = "com.example.app/.Main") -> None:
        self.component_name = component
        self.package_name = component.split("/", 1)[0]
        self.display_name = self.package_name


class FakeConfig:
    device_id = None
    accessibility_max_marks = 80
    accessibility_timeout = 3.0
    grounding_provider = "accessibility"
    locateanything_max_size = 960
    locateanything_context_max_chars = 200
    locate_max_size = 0
    scope_padding_ratio = 0.05
    locateanything_model = None
    observe_settle_ms = 0
    marks_windowed = "auto"


class ScriptedDevice:
    """Per-call screenshot/dump/foreground script; the last entry repeats.

    Shot entries are :class:`_ShotSpec` values. Dump entries are XML strings,
    ``""`` (an empty dump is ``accessibility_dump_empty``), or ``"timeout"``
    (raises ``TimeoutError``). ``foreground`` is a component string or a list
    consumed one call at a time.
    """

    def __init__(self, shots, dumps, foreground="com.example.app/.Main") -> None:
        self._shots = [_ShotSpec(entry) if isinstance(entry, str) else entry for entry in shots]
        self._dumps = list(dumps)
        self._foreground = foreground
        self._shot_i = 0
        self._dump_i = 0
        self._fg_i = 0
        self.screenshot_calls = 0
        self.dump_calls = 0
        self.taps: list[tuple[int, int]] = []

    def get_screenshot(self, device_id=None, timeout=10, **kwargs):
        spec = self._shots[min(self._shot_i, len(self._shots) - 1)]
        self._shot_i += 1
        self.screenshot_calls += 1
        return FakeShot(spec)

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        action = self._dumps[min(self._dump_i, len(self._dumps) - 1)]
        self._dump_i += 1
        self.dump_calls += 1
        if action == "timeout":
            raise TimeoutError("dump timed out")
        return action

    def get_foreground_app(self, device_id=None):
        value = self._foreground
        if isinstance(value, list):
            component = value[min(self._fg_i, len(value) - 1)]
            self._fg_i += 1
        else:
            component = value
        return FakeForeground(component)

    def tap(self, x, y, device_id=None, delay=None):
        self.taps.append((int(x), int(y)))


def _session(shots, dumps, foreground="com.example.app/.Main", **overrides) -> PhoneSession:
    config = FakeConfig()
    for key, value in overrides.items():
        setattr(config, key, value)
    return PhoneSession(
        config, device_factory=ScriptedDevice(shots, dumps, foreground=foreground)
    )


def _text(blocks: list[dict]) -> str:
    return "\n".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    )


def _images(blocks: list[dict]) -> list[dict]:
    return [block for block in blocks if block.get("type") == "image_url"]


def test_failed_retry_keeps_last_valid_frame_as_reference():
    session = _session([good("shot1"), bad()], ["timeout", "timeout"])

    with pytest.raises(ScreenshotError):
        session.observe()

    frame = session.last_reference_frame()
    assert frame is not None
    assert frame["b64"] == "shot1"
    assert frame["mime"] == "image/png"
    assert frame["ref"] == "ref1"
    assert session.epoch == 0
    assert session.screen_seq == 0
    assert session.marks == {}


def test_no_valid_frame_means_no_reference():
    session = _session([bad()], ["timeout"])

    with pytest.raises(ScreenshotError):
        session.observe()

    assert session.last_reference_frame() is None


def test_reference_frame_keeps_epoch_marks_and_geometry_frozen():
    session = _session([good("shot1"), bad()], ["timeout", "timeout"])
    session.epoch = 4
    session.screen_seq = 7
    session._last_width = 1000
    session._last_height = 2000
    old = MarkCandidate("ax_old@e4", [0, 0, 100, 100], [50, 50], epoch=4)
    session.marks = {old.mark_id: old}

    with pytest.raises(ScreenshotError):
        session.observe()

    assert session.last_reference_frame()["b64"] == "shot1"
    assert session.epoch == 4
    assert session.screen_seq == 7
    assert session.marks == {}
    assert (session.screen_width, session.screen_height) == (1000, 2000)
    with pytest.raises(StaleMarkError):
        session.resolve_mark(old.mark_id)


def test_reference_frame_uses_the_most_recent_valid_attempt():
    session = _session(
        [good("shot1"), good("shot2")],
        [_SETTINGS_XML, _SETTINGS_XML],
        foreground=["A/.X", "B/.Y", "C/.Z", "D/.W"],
    )

    with pytest.raises(ScreenshotError, match="observation unstable"):
        session.observe()

    assert session.last_reference_frame()["b64"] == "shot2"


def test_secure_block_never_exposes_retained_frame():
    session = _session([good("shot1"), bad("secure_screenshot_blocked")], ["timeout", "timeout"])

    with pytest.raises(ScreenshotError) as exc:
        session.observe()

    assert exc.value.failure_code == "secure_screenshot_blocked"
    assert session.last_reference_frame() is None


def test_next_observe_attempt_clears_previous_reference():
    session = _session([good("shot1"), bad(), bad()], ["timeout", "timeout"])

    with pytest.raises(ScreenshotError):
        session.observe()
    assert session.last_reference_frame()["b64"] == "shot1"

    with pytest.raises(ScreenshotError):
        session.observe()
    assert session.last_reference_frame() is None


def test_auto_observation_failure_returns_labelled_reference_image():
    session = _session([good("shot1"), bad()], ["timeout", "timeout"])

    blocks = auto_observation(session)

    text = _text(blocks)
    assert text.startswith("[OBS] (re-observation failed:")
    assert "较早采样" in text
    assert "当前画面未验证" in text
    assert "不是当前可操作的标记批次" in text
    assert "screen_ref=ref1" in text
    images = _images(blocks)
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == "data:image/png;base64,shot1"
    assert images[0]["reference"] is True
    assert images[0]["screen_ref"] == "ref1"
    assert "screen_seq" not in images[0]


def test_auto_observation_reference_keeps_real_jpeg_mime():
    session = _session([good("shot1", mime="image/jpeg"), bad()], ["timeout", "timeout"])

    images = _images(auto_observation(session))

    assert images[0]["image_url"]["url"] == "data:image/jpeg;base64,shot1"


def test_auto_observation_without_reference_stays_text_only():
    session = _session([bad()], ["timeout"])

    blocks = auto_observation(session)

    assert _images(blocks) == []
    assert "较早采样" not in _text(blocks)


def test_committed_zero_mark_frame_is_not_a_reference():
    session = _session([good("shot1"), good("shot2")], ["timeout", "timeout"])

    observation = session.observe()

    assert observation.marks == []
    assert observation.marks_failure_code == "timeout"
    assert observation.epoch == 1
    assert session.last_reference_frame() is None

    blocks = auto_observation(session)
    images = _images(blocks)
    assert "[accessibility:timeout]" in _text(blocks)
    assert images[0]["screen_seq"] == session.screen_seq
    assert not images[0].get("reference")


def test_action_success_survives_failed_observation_with_reference():
    from phone_agent.v2.tools.actuation import build_actuation_tools

    session = _session(
        [good("shot1"), good("shot2"), bad()], [_SETTINGS_XML, "timeout", "timeout"]
    )
    session.observe()
    target = next(iter(session.marks))

    tools = {tool.name: tool for tool in build_actuation_tools(session, FakeConfig())}
    out = tools["tap"].invoke({"target_mark_id": target})

    assert isinstance(out, list)
    assert out[0]["text"].startswith("OK. 已点击")
    assert session.last_tool_ok is True
    assert len(session.device_factory.taps) == 1
    images = _images(out)
    assert images and images[0]["screen_ref"] == "ref1"
    assert "较早采样" in _text(out)


def test_foreground_change_reference_never_poses_as_current_frame():
    session = _session(
        [good("shot1"), good("shot2")],
        [_SETTINGS_XML, _SETTINGS_XML],
        foreground=[
            "A1/.X",
            "A2/.X",
            "B1/.X",
            "B2/.X",
            "C1/.X",
            "C2/.X",
            "D1/.X",
            "D2/.X",
        ],
    )

    blocks = auto_observation(session)

    text = _text(blocks)
    assert "observation unstable" in text
    assert "较早采样" in text
    images = _images(blocks)
    assert images[0]["reference"] is True
    assert images[0]["screen_ref"] == "ref1"
    assert "screen_seq" not in images[0]


def test_read_screen_failure_returns_reference_image():
    from phone_agent.v2.tools.perception import build_perception_tools

    session = _session([good("shot1"), bad()], ["timeout", "timeout"])
    tools = {tool.name: tool for tool in build_perception_tools(session, FakeConfig())}

    out = tools["read_screen"].invoke({})

    assert isinstance(out, list)
    assert "较早采样" in _text(out)
    images = _images(out)
    assert images and images[0]["screen_ref"] == "ref1"


def _opening_messages(session, task: str = "do it"):
    agent = object.__new__(ThinPhoneAgent)
    agent.session = session
    agent._system_prompt = "system"
    agent._capability_ctx = None
    agent._render_lesson_prompt_block = lambda: None
    return agent._initial_messages(task)


class _OpeningFailSession:
    def __init__(self, frame=None, failure_code="adb_screencap_failed") -> None:
        self._frame = frame
        self._failure_code = failure_code

    def observe(self):
        raise ScreenshotError(
            f"screenshot invalid: {self._failure_code}",
            failure_code=self._failure_code,
        )

    def last_reference_frame(self):
        return self._frame


def test_opening_failure_uses_reference_frame():
    session = _OpeningFailSession(
        {"b64": "shot1", "mime": "image/jpeg", "ref": "ref1"}
    )

    messages = _opening_messages(session)

    human = next(message for message in messages if isinstance(message, HumanMessage))
    texts = [block["text"] for block in human.content if block.get("type") == "text"]
    images = _images(human.content)
    assert texts[0] == "do it"
    assert texts[1].startswith("[OBS] (opening observation failed: adb_screencap_failed)")
    assert "较早采样" in texts[1]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == "data:image/jpeg;base64,shot1"
    assert images[0]["screen_ref"] == "ref1"


def test_opening_failure_without_reference_or_accessor_is_text_only():
    session = _OpeningFailSession(None)

    class _NoAccessor:
        def observe(self):
            raise ScreenshotError("screenshot invalid: adb_screencap_failed")

    for current in (session, _NoAccessor()):
        human = next(
            message
            for message in _opening_messages(current)
            if isinstance(message, HumanMessage)
        )
        assert _images(human.content) == []
        assert "[OBS] (opening observation failed:" in _text(human.content)


def test_opening_failure_secure_block_omits_reference():
    session = _OpeningFailSession(
        {"b64": "shot1", "mime": "image/png", "ref": "ref1"},
        failure_code="secure_screenshot_blocked",
    )

    human = next(
        message for message in _opening_messages(session) if isinstance(message, HumanMessage)
    )

    assert _images(human.content) == []
    assert "secure_screenshot_blocked" in _text(human.content)


def test_diagnostic_reference_file_does_not_overwrite_screen_frame(tmp_path: Path):
    writer = DiagnosticEvidenceWriter(
        "run-ref", evidence_dir=str(tmp_path), enabled=True, unredacted=True
    )
    frame_b64 = _png_b64((10, 20, 30))
    writer._write_screenshot(3, f"data:image/png;base64,{frame_b64}")
    frame_path = tmp_path / "screenshots" / "screen-3.png"
    assert frame_path.exists()

    reference_b64 = _png_b64((200, 10, 10))
    content = [
        {"type": "text", "text": "参考图：较早采样的一帧有效截图"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{reference_b64}"},
            "reference": True,
            "screen_ref": "ref2",
        },
    ]
    request = SimpleNamespace(tool_call={"name": "read_screen", "args": {}})
    writer.on_tool_execute(
        request,
        lambda _request: ToolMessage(
            content=content, tool_call_id="c1", name="read_screen"
        ),
    )

    assert frame_path.read_bytes() == base64.b64decode(frame_b64)
    reference_path = tmp_path / "screenshots" / "screen-ref2.png"
    assert reference_path.read_bytes() == base64.b64decode(reference_b64)

    events = [
        json.loads(line)
        for line in Path(writer.evidence_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    observation = next(event for event in events if event["event"] == "tool_observation")
    assert observation["image"]["present"] is True
    assert observation["image"]["reference"] == "ref2"
    assert observation["image"]["path"] == "screenshots/screen-ref2.png"
    raw = Path(writer.evidence_path).read_text(encoding="utf-8")
    assert reference_b64 not in raw
    assert "data:image" not in raw


def test_diagnostic_opening_capture_saves_reference_file(tmp_path: Path):
    writer = DiagnosticEvidenceWriter("run-open", evidence_dir=str(tmp_path), enabled=True)
    reference_b64 = _png_b64((1, 2, 3))
    content = [
        {"type": "text", "text": "do it"},
        {"type": "text", "text": "[OBS] (opening observation failed: adb_screencap_failed)"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{reference_b64}"},
            "reference": True,
            "screen_ref": "ref1",
        },
    ]
    writer._capture_opening_screens([HumanMessage(content=content)])

    assert (tmp_path / "screenshots" / "screen-ref1.png").read_bytes() == base64.b64decode(
        reference_b64
    )


def test_reference_image_is_pruned_like_any_history_image():
    reference = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
        "reference": True,
        "screen_ref": "ref1",
    }
    newest = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,REVG"},
        "screen_seq": 2,
    }
    messages = [
        HumanMessage(content=[{"type": "text", "text": "first"}, reference]),
        HumanMessage(content=[{"type": "text", "text": "second"}, newest]),
    ]

    modified = ContextPrunerService(keep_images=1, keep_marks=1).prune(messages)

    assert messages[0] in modified
    assert messages[1] not in modified
    assert messages[0].content[1]["type"] == "text"
    assert "已剪除" in messages[0].content[1]["text"]
    assert messages[1].content[1] == newest


def test_web_screen_event_carries_reference_image_without_fake_seq():
    session = _session([good("shot1"), bad()], ["timeout", "timeout"])
    blocks = auto_observation(session)
    sink: Queue = Queue()
    web = WebEventMiddleware(sink)
    request = SimpleNamespace(tool_call={"id": "c1", "name": "read_screen", "args": {}})
    web.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content=blocks, tool_call_id="c1", name="read_screen"
        ),
    )

    events = []
    while not sink.empty():
        events.append(sink.get())
    screen = next(event for event in events if event["event"] == "screen")
    assert screen["image"] == "data:image/png;base64,shot1"
    assert screen["screen_seq"] is None
