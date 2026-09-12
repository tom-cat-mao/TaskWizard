"""WP-G2cB (B3) end-to-end: production digest -> skill ``parse_obs_windows``.

Pins the parallel-package contract by running the **real** ``PhoneSession``
observation render (via ``tools/_obs.auto_observation``) and feeding its exact
``[OBS]`` text into the diagnosis skill's ``parse_obs_windows`` (imported the
same way the CLI imports it, through the skill scripts dir on ``sys.path``). If
the production badge / window-head / mark-line format ever drifts from what the
skill parser expects, this test fails — the two packages stay wire-compatible.
"""

from __future__ import annotations

from evidence import parse_obs_windows

from phone_agent.v2.session import PhoneSession
from phone_agent.v2.tools._obs import auto_observation


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


class _Shot:
    def __init__(self, payload: str) -> None:
        self.base64_data = payload
        self.width = 1080
        self.height = 2400
        self.mime_type = "image/png"
        self.is_valid = True
        self.failure_code = None


class _Foreground:
    component_name = "com.android.permissioncontroller/.Perm"
    package_name = "com.android.permissioncontroller"
    display_name = "权限"


class _Config:
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


class _Device:
    def __init__(self, xml: str) -> None:
        self._xml = xml

    def get_screenshot(self, device_id=None, timeout=10, **kwargs):
        return _Shot("cHJvZA==")

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        return self._xml

    def get_foreground_app(self, device_id=None):
        return _Foreground()


def test_production_windowed_render_parses_in_skill():
    session = PhoneSession(_Config(), device_factory=_Device(_DISPLAYS_WINDOWED))
    blocks = auto_observation(session)
    obs_text = blocks[0]["text"]

    parsed = parse_obs_windows(obs_text)
    assert parsed is not None
    assert parsed["present"] is True
    assert parsed["schema"] == "v1"
    assert parsed["source"] == "shell_windows"
    assert parsed["window_count"] == 2

    # Higher-layer system window renders first and is active+focused.
    w_sys = parsed["windows"][0]
    assert w_sys["type"] == "TYPE_SYSTEM"
    assert w_sys["package"] == "com.android.permissioncontroller"
    assert w_sys["layer"] == 42
    assert "active" in w_sys["flags"] and "focus" in w_sys["flags"]
    assert w_sys["mark_count"] == 1
    assert w_sys["marks"][0]["op"] == "confirmed"
    assert w_sys["marks"][0]["path"] == "dialog"

    # Lower window is application-tier and covered by the system window.
    w_app = parsed["windows"][1]
    assert w_app["type"] == "TYPE_APPLICATION"
    assert w_app["marks"][0]["path"] == "toolbar"


def test_production_flat_render_stays_non_windowed_in_skill():
    # A single weak (legacy) window renders flat -> the skill sees no windowed
    # badge and returns None (legacy path), never a false window structure.
    legacy = (
        "<hierarchy>"
        '<node text="WLAN" class="android.widget.TextView" clickable="true" '
        'enabled="true" bounds="[0,100][1080,300]" package="com.x"/>'
        "</hierarchy>"
    )
    session = PhoneSession(_Config(), device_factory=_Device(legacy))
    blocks = auto_observation(session)
    obs_text = blocks[0]["text"]
    assert parse_obs_windows(obs_text) is None
