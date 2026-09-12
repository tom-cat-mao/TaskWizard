"""WP-G2cB (B2/B3): observation-layer failure codes + windowed digest badges.

Covers the observation-hardening contract against the **real** ``PhoneSession``
driven by a scriptable fake DeviceFactory (no real device / MLX / network):

    - B2 surfacing — ``refresh_marks_sample`` exposes the provider's
      ``failure_code`` + ``parse_summary``; ``observe()`` retries a *transient*
      marks-dump failure once (timeout / parse error / provider error) at the
      same tier as a foreground change, then commits an annotated zero-mark
      observation (the screenshot is valid, so the batch is NOT invalidated);
    - a genuinely empty screen (``accessibility_dump_empty``) does not retry and
      is not annotated as a failure;
    - a ``marks_windowed=on`` unsupported-dump error is surfaced (annotated), not
      swallowed and not raised;
    - the ``[OBS]`` header renders ``marks (0) [accessibility:<code>]`` for a
      failed dump so the model never reads a dump failure as "no controls";
    - B3 — the production digest carries ``windowed/v1 source=<src>`` + window
      ``active``/``focus`` flags + ``marks (K/total)`` truncation when the
      parser reports ``total_candidates``.
"""

from __future__ import annotations

from phone_agent.v2.session import PhoneSession
from phone_agent.v2.tools._obs import auto_observation


_SETTINGS_XML = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" />'
    "</hierarchy>"
)

_DISPLAYS_WINDOWED = (
    '<?xml version="1.0"?><displays><display id="0">'
    '<window id="3" layer="10" type="TYPE_APPLICATION" title="Maps" '
    'bounds="[0,0][1080,2400]" active="false" focused="false"><hierarchy>'
    '<node class="android.widget.Toolbar" resource-id="com.x:id/toolbar" '
    'bounds="[0,0][1080,200]" package="com.x">'
    '<node text="返回" class="android.widget.ImageButton" clickable="true" '
    'enabled="true" bounds="[16,60][120,140]" package="com.x"/>'
    "</node></hierarchy></window>"
    '<window id="7" layer="42" type="TYPE_SYSTEM" title="Perm" '
    'bounds="[100,600][980,1400]" active="true" focused="true"><hierarchy>'
    '<node class="android.app.Dialog" bounds="[100,600][980,1400]" '
    'package="com.android.permissioncontroller">'
    '<node text="仅本次允许" class="android.widget.Button" clickable="true" '
    'enabled="true" bounds="[126,900][874,1000]" '
    'package="com.android.permissioncontroller"/>'
    "</node></hierarchy></window>"
    "</display></displays>"
)


class FakeShot:
    def __init__(self, payload: str) -> None:
        self.base64_data = payload
        self.width = 1080
        self.height = 2400
        self.mime_type = "image/png"
        self.is_valid = True
        self.failure_code = None


class FakeForeground:
    def __init__(self, component: str = "com.example.app/.Main") -> None:
        self.component_name = component
        self.package_name = "com.example.app"
        self.display_name = "com.example.app"


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
    """Device whose ``dump_uiautomator_xml`` follows a per-call script.

    ``dump_script`` is a list of "actions": a string returns that XML, the
    string ``"timeout"`` raises TimeoutError, ``"boom"`` raises RuntimeError, and
    ``"windows_unsupported"`` raises ValueError (the marks_windowed=on shape).
    The last action repeats once the script is exhausted.
    """

    def __init__(self, dump_script, *, windowed_arg: bool = True) -> None:
        self._script = list(dump_script)
        self._i = 0
        self._windowed_arg = windowed_arg
        self.screenshot_calls = 0
        self.dump_calls = 0
        self.dump_windowed_args: list = []

    def get_screenshot(self, device_id=None, timeout=10, **kwargs):
        self.screenshot_calls += 1
        return FakeShot(f"shot{self.screenshot_calls}")

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        self.dump_calls += 1
        self.dump_windowed_args.append(windowed)
        action = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if action == "timeout":
            raise TimeoutError("dump timed out")
        if action == "boom":
            raise RuntimeError("adb died")
        if action == "windows_unsupported":
            raise ValueError("UiAutomator --windows dump unavailable on this device")
        return action

    def get_foreground_app(self, device_id=None):
        return FakeForeground()


def _session(dump_script, **cfg_over) -> PhoneSession:
    config = FakeConfig()
    for key, value in cfg_over.items():
        setattr(config, key, value)
    return PhoneSession(config, device_factory=ScriptedDevice(dump_script))


# --------------------------------------------------------------------------
# refresh_marks_sample surfaces the provider failure code + parse_summary.
# --------------------------------------------------------------------------
def test_refresh_marks_sample_surfaces_timeout_code():
    session = _session(["timeout"])
    shot = session.screenshot()
    sample = session.refresh_marks_sample(shot)
    assert sample.marks == []
    assert sample.failure_code == "timeout"


def test_refresh_marks_sample_surfaces_dump_empty_and_summary():
    session = _session([""])
    shot = session.screenshot()
    sample = session.refresh_marks_sample(shot)
    assert sample.marks == []
    assert sample.failure_code == "accessibility_dump_empty"
    assert isinstance(sample.parse_summary, dict)
    assert sample.parse_summary["xml_status"] == "accessibility_dump_empty"


def test_refresh_marks_sample_ok_carries_windows_and_summary():
    session = _session([_DISPLAYS_WINDOWED])
    shot = session.screenshot()
    sample = session.refresh_marks_sample(shot)
    assert len(sample.marks) == 2
    assert sample.failure_code is None
    assert sample.parse_summary["window_source"] == "shell_windows"
    assert sample.windows and len(sample.windows) == 2


def test_refresh_marks_backward_compat_returns_only_marks():
    session = _session([_SETTINGS_XML])
    shot = session.screenshot()
    marks = session.refresh_marks(shot)
    assert isinstance(marks, list)
    assert len(marks) == 1


# --------------------------------------------------------------------------
# observe(): transient marks-dump failure retries once, then commits annotated.
# --------------------------------------------------------------------------
def test_observe_retries_once_on_transient_dump_timeout_then_commits():
    # attempt1: timeout (transient) -> retry; attempt2: success.
    session = _session(["timeout", _SETTINGS_XML])
    obs = session.observe()
    assert obs.epoch == 1
    assert len(obs.marks) == 1
    assert obs.marks_failure_code is None
    # The retry consumed a 2nd screenshot + dump.
    assert session.device_factory.dump_calls == 2


def test_observe_commits_annotated_when_dump_keeps_failing():
    # Both attempts time out; the screenshot is valid so the frame still commits
    # with zero marks + the failure code (never invalidated as an obs failure).
    session = _session(["timeout", "timeout"])
    obs = session.observe()
    assert obs.epoch == 1
    assert obs.marks == []
    assert obs.marks_failure_code == "timeout"
    assert session.marks == {}  # empty batch, but the epoch DID bump (committed).
    assert session.epoch == 1


def test_observe_empty_screen_does_not_retry_or_annotate():
    # accessibility_dump_empty is a legitimate empty screen: no retry, and the
    # header must NOT be annotated as a dump failure.
    session = _session([""])
    obs = session.observe()
    assert obs.epoch == 1
    assert obs.marks == []
    assert obs.marks_failure_code == "accessibility_dump_empty"
    assert session.device_factory.dump_calls == 1  # no retry


def test_observe_provider_error_retries_then_commits():
    session = _session(["boom", "boom"])
    obs = session.observe()
    assert obs.marks_failure_code == "provider_error"
    assert session.device_factory.dump_calls == 2


def test_observe_windows_unsupported_is_surfaced_not_raised():
    # marks_windowed=on + a device that raises the unsupported error: the error
    # is surfaced (annotated code), not swallowed and not raised past observe().
    session = _session(["windows_unsupported", "windows_unsupported"], marks_windowed="on")
    obs = session.observe()
    assert obs.epoch == 1
    assert obs.marks == []
    assert obs.marks_failure_code == "provider_error"


# --------------------------------------------------------------------------
# OBS header annotation (tools/_obs.py).
# --------------------------------------------------------------------------
def test_obs_header_annotates_dump_failure():
    session = _session(["timeout", "timeout"])
    blocks = auto_observation(session)
    text = blocks[0]["text"]
    assert "marks (0) [accessibility:timeout]" in text


def test_obs_header_not_annotated_for_empty_screen():
    session = _session([""])
    blocks = auto_observation(session)
    text = blocks[0]["text"]
    assert "marks (0):" in text
    assert "[accessibility:" not in text


# --------------------------------------------------------------------------
# B3: production digest carries the windowed badge + active/focus flags.
# --------------------------------------------------------------------------
def test_production_obs_carries_windowed_badge_and_flags():
    session = _session([_DISPLAYS_WINDOWED])
    blocks = auto_observation(session)
    text = blocks[0]["text"]
    assert "windowed/v1 source=shell_windows" in text
    # Higher-layer system window is active + focused; its header carries the
    # bare active/focus tokens from the window sidecar.
    sys_head = next(
        ln for ln in text.splitlines() if "TYPE_SYSTEM" in ln and ln.startswith("W")
    )
    assert "active" in sys_head.split()
    assert "focus" in sys_head.split()


def test_obs_marks_truncation_shows_total_when_available(monkeypatch):
    session = _session([_DISPLAYS_WINDOWED])
    # Simulate the parallel package A total-candidates field by patching the
    # parse summary the session extracts (code renders K/total only when > K).
    real = PhoneSession._extract_parse_summary

    def fake_summary(result):
        summary = real(result) or {}
        summary["total_candidates"] = 124
        return summary

    monkeypatch.setattr(PhoneSession, "_extract_parse_summary", staticmethod(fake_summary))
    blocks = auto_observation(session)
    text = blocks[0]["text"]
    assert "marks (2/124)" in text


def test_obs_marks_no_total_field_omits_slash():
    session = _session([_DISPLAYS_WINDOWED])
    blocks = auto_observation(session)
    text = blocks[0]["text"]
    assert "marks (2)" in text
    assert "marks (2/" not in text
