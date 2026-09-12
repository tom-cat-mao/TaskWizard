"""WP-G2a windowed marks: dual-format parse, window metadata, actionability,
grouped/flat rendering, config three-state, and device dual-mode dump.

Pure display layer: addressing, tool execution, safety, folding and locate are
never touched here — only how the accessibility tree is grouped, annotated and
rendered.
"""

from __future__ import annotations

import subprocess

import pytest

from phone_agent.adb import device
from phone_agent.grounding.accessibility import _parse_uiautomator_xml
from phone_agent.grounding.provider import MarkCandidate
from phone_agent.v2.config import V2Config
from phone_agent.v2.session import PhoneSession


# --------------------------------------------------------------------------
# Fixtures / helpers.
# --------------------------------------------------------------------------
_LEGACY_SINGLE_ROOT = (
    "<hierarchy>"
    '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" package="com.x">'
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" package="com.x"/>'
    '<node text="蓝牙" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,300][1080,500]" package="com.x"/>'
    "</node>"
    "</hierarchy>"
)

_LEGACY_MULTI_ROOT = (
    "<hierarchy>"
    '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" package="com.bg">'
    '<node text="底部按钮" class="android.widget.Button" clickable="true" '
    'enabled="true" bounds="[400,1000][600,1200]" package="com.bg"/>'
    "</node>"
    '<node class="android.widget.FrameLayout" bounds="[300,900][700,1300]" package="com.top">'
    '<node text="顶部按钮" class="android.widget.Button" clickable="true" '
    'enabled="true" bounds="[350,950][650,1250]" package="com.top"/>'
    "</node>"
    "</hierarchy>"
)

_DISPLAYS_WINDOWED = (
    '<?xml version="1.0"?>'
    "<displays><display id=\"0\">"
    '<window id="3" layer="10" type="TYPE_APPLICATION" title="Maps" '
    'bounds="[0,0][1080,2400]" active="false" focused="false">'
    "<hierarchy>"
    '<node class="android.widget.Toolbar" resource-id="com.x:id/toolbar" '
    'bounds="[0,0][1080,200]" package="com.x">'
    '<node text="返回" class="android.widget.ImageButton" clickable="true" '
    'enabled="true" bounds="[16,60][120,140]" package="com.x"/>'
    "</node>"
    "</hierarchy>"
    "</window>"
    '<window id="7" layer="42" type="TYPE_SYSTEM" title="Perm" '
    'bounds="[100,600][980,1400]" active="true" focused="true">'
    "<hierarchy>"
    '<node class="android.app.Dialog" bounds="[100,600][980,1400]" '
    'package="com.android.permissioncontroller">'
    '<node text="仅本次允许" class="android.widget.Button" clickable="true" '
    'enabled="true" bounds="[126,900][874,1000]" '
    'package="com.android.permissioncontroller"/>'
    "</node>"
    "</hierarchy>"
    "</window>"
    "</display></displays>"
)


def _parse(xml: str, *, width: int = 1080, height: int = 2400, max_marks: int = 80):
    return _parse_uiautomator_xml(
        xml,
        screen_width=width,
        screen_height=height,
        source="uiautomator",
        max_marks=max_marks,
    )


def _to_marks(parsed) -> list[MarkCandidate]:
    return [
        MarkCandidate(
            mark_id=m["mark_id"],
            bbox=m["bbox"],
            center=m["center"],
            role=m.get("role"),
            text_summary=m.get("text_summary"),
            source="uiautomator",
            window_id=m.get("window_id"),
            window_layer=m.get("window_layer"),
            window_type=m.get("window_type"),
            window_title=m.get("window_title"),
            package=m.get("package"),
            container_path=tuple(m.get("container_path") or ()),
            actionability=m.get("actionability"),
            actionability_reasons=tuple(m.get("actionability_reasons") or ()),
        )
        for m in parsed["marks"]
    ]


class _FakeCP:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# --------------------------------------------------------------------------
# Dual-format parse.
# --------------------------------------------------------------------------
def test_legacy_single_root_stays_flat_source_hierarchy():
    parsed = _parse(_LEGACY_SINGLE_ROOT)
    assert parsed["parse_summary"]["window_source"] == "hierarchy"
    assert parsed["parse_summary"]["window_count"] == 1
    ids = [m["mark_id"] for m in parsed["marks"]]
    assert ids == ["ax_1", "ax_2"]  # id sequence unchanged from legacy flatten
    assert [m["text_summary"] for m in parsed["marks"]] == ["WLAN", "蓝牙"]
    assert all(m["window_id"] == "W1" for m in parsed["marks"])


def test_legacy_multi_root_infers_one_weak_window_each():
    parsed = _parse(_LEGACY_MULTI_ROOT)
    assert parsed["parse_summary"]["window_source"] == "hierarchy"
    assert parsed["parse_summary"]["window_count"] == 2
    by_text = {m["text_summary"]: m for m in parsed["marks"]}
    assert by_text["底部按钮"]["window_id"] == "W1"
    assert by_text["顶部按钮"]["window_id"] == "W2"
    # Weak windows carry no real layer/type.
    assert all(m["window_layer"] is None for m in parsed["marks"])
    assert all(m["window_type"] is None for m in parsed["marks"])


def test_displays_windowed_gives_strong_window_metadata():
    parsed = _parse(_DISPLAYS_WINDOWED)
    assert parsed["parse_summary"]["window_source"] == "shell_windows"
    assert parsed["parse_summary"]["window_count"] == 2
    by_text = {m["text_summary"]: m for m in parsed["marks"]}
    back = by_text["返回"]
    allow = by_text["仅本次允许"]
    assert back["window_layer"] == 10
    assert back["window_type"] == "TYPE_APPLICATION"
    assert back["window_title"] == "Maps"
    assert back["package"] == "com.x"
    assert allow["window_layer"] == 42
    assert allow["window_type"] == "TYPE_SYSTEM"
    assert allow["package"] == "com.android.permissioncontroller"


def test_window_sidecar_populated_in_screen_structures():
    parsed = _parse(_DISPLAYS_WINDOWED)
    windows = {w["window_id"]: w for w in parsed["windows"]}
    assert windows["W1"]["source_confidence"] == "strong"
    assert windows["W2"]["layer"] == 42
    assert windows["W2"]["focused"] is True
    assert windows["W1"]["mark_count"] == 1
    assert windows["W2"]["mark_count"] == 1


def test_empty_and_unparseable_roots_are_safe():
    empty = _parse("   ")
    assert empty["marks"] == []
    assert empty["parse_summary"]["xml_status"] == "accessibility_dump_empty"
    broken = _parse("<hierarchy><node ")
    assert broken["marks"] == []
    assert broken["parse_summary"]["xml_status"] == "accessibility_xml_parse_error"


# --------------------------------------------------------------------------
# Container path (sparse semantic ancestry).
# --------------------------------------------------------------------------
def test_container_path_keeps_semantic_containers_drops_nameless_layouts():
    xml = (
        "<hierarchy>"
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">'
        '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,200][1080,2000]">'
        '<node class="android.widget.LinearLayout" bounds="[0,200][1080,400]">'
        '<node text="行" class="android.widget.TextView" clickable="true" '
        'enabled="true" bounds="[0,200][1080,400]"/>'
        "</node></node></node>"
        "</hierarchy>"
    )
    parsed = _parse(xml)
    row = next(m for m in parsed["marks"] if m["text_summary"] == "行")
    # FrameLayout + LinearLayout dropped; RecyclerView kept as "list".
    assert row["container_path"] == ("list",)


def test_container_path_capped_at_three_levels():
    xml = (
        "<hierarchy>"
        '<node class="android.app.Dialog" bounds="[0,0][1080,2400]">'
        '<node class="android.widget.Toolbar" bounds="[0,0][1080,200]">'
        '<node class="android.widget.TabLayout" bounds="[0,0][1080,200]">'
        '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,0][1080,200]">'
        '<node text="深" class="android.widget.TextView" clickable="true" '
        'enabled="true" bounds="[0,0][100,100]"/>'
        "</node></node></node></node>"
        "</hierarchy>"
    )
    parsed = _parse(xml)
    deep = next(m for m in parsed["marks"] if m["text_summary"] == "深")
    assert len(deep["container_path"]) == 3
    # Keeps the innermost three: toolbar > tab > list.
    assert deep["container_path"] == ("toolbar", "tab", "list")


# --------------------------------------------------------------------------
# Actionability tiers.
# --------------------------------------------------------------------------
def test_strong_window_enabled_node_is_confirmed():
    parsed = _parse(_DISPLAYS_WINDOWED)
    for m in parsed["marks"]:
        assert m["actionability"] == "confirmed"


def test_strong_higher_window_covers_lower_yields_blocked():
    xml = (
        '<?xml version="1.0"?><displays><display id="0">'
        '<window id="3" layer="10" type="TYPE_APPLICATION" bounds="[0,0][1080,2400]" '
        'active="false" focused="false"><hierarchy>'
        '<node text="背景" class="android.widget.Button" clickable="true" enabled="true" '
        'bounds="[300,900][700,1100]" package="com.x"/>'
        "</hierarchy></window>"
        '<window id="7" layer="42" type="TYPE_SYSTEM" bounds="[100,600][980,1400]" '
        'active="true" focused="true"><hierarchy>'
        '<node text="允许" class="android.widget.Button" clickable="true" enabled="true" '
        'bounds="[126,900][874,1000]" package="com.android.permissioncontroller"/>'
        "</hierarchy></window>"
        "</display></displays>"
    )
    parsed = _parse(xml)
    by_text = {m["text_summary"]: m for m in parsed["marks"]}
    assert by_text["背景"]["actionability"] == "blocked"
    assert "covered_by:W2" in by_text["背景"]["actionability_reasons"]
    assert by_text["允许"]["actionability"] == "confirmed"


def test_heuristic_windows_never_emit_blocked_only_unknown():
    # Two inferred windows overlapping: the covered one is unknown, not blocked.
    parsed = _parse(_LEGACY_MULTI_ROOT)
    tiers = {m["text_summary"]: m["actionability"] for m in parsed["marks"]}
    assert tiers["底部按钮"] == "unknown"  # center covered by later (higher) window
    assert tiers["顶部按钮"] == "likely"
    assert all(m["actionability"] != "blocked" for m in parsed["marks"])


def test_plain_accessibility_candidate_defaults_to_likely():
    parsed = _parse(_LEGACY_SINGLE_ROOT)
    assert all(m["actionability"] == "likely" for m in parsed["marks"])
    assert parsed["parse_summary"]["actionability_counts"] == {"likely": 2}


# --------------------------------------------------------------------------
# Rendering: grouped windowed vs flat fallback.
# --------------------------------------------------------------------------
def test_windowed_render_groups_by_layer_desc_with_op_and_path():
    digest = PhoneSession.format_marks_digest(_to_marks(_parse(_DISPLAYS_WINDOWED)))
    lines = digest.splitlines()
    # Higher layer window (W2, layer 42) header comes first.
    assert lines[0].startswith("W2 TYPE_SYSTEM com.android.permissioncontroller layer=42")
    assert lines[1].startswith("  ax_2 | Button | 仅本次允许")
    assert "op=confirmed" in lines[1]
    assert "path=dialog" in lines[1]
    assert lines[2].startswith("W1 TYPE_APPLICATION com.x layer=10")
    assert "op=confirmed" in lines[3]
    assert "path=toolbar" in lines[3]


def test_windowed_render_shows_covered_by_in_header():
    xml = (
        '<?xml version="1.0"?><displays><display id="0">'
        '<window id="3" layer="10" type="TYPE_APPLICATION" bounds="[0,0][1080,2400]" '
        'active="false" focused="false"><hierarchy>'
        '<node text="背景" class="android.widget.Button" clickable="true" enabled="true" '
        'bounds="[300,900][700,1100]" package="com.x"/>'
        "</hierarchy></window>"
        '<window id="7" layer="42" type="TYPE_SYSTEM" bounds="[100,600][980,1400]" '
        'active="true" focused="true"><hierarchy>'
        '<node text="允许" class="android.widget.Button" clickable="true" enabled="true" '
        'bounds="[126,900][874,1000]" package="com.android.permissioncontroller"/>'
        "</hierarchy></window>"
        "</display></displays>"
    )
    digest = PhoneSession.format_marks_digest(_to_marks(_parse(xml)))
    w1_header = next(ln for ln in digest.splitlines() if ln.startswith("W1 "))
    assert "covered_by=W2" in w1_header


def test_single_weak_window_keeps_flat_layout():
    digest = PhoneSession.format_marks_digest(_to_marks(_parse(_LEGACY_SINGLE_ROOT)))
    lines = digest.splitlines()
    # No window header, no leading indent — historic flat lines.
    assert lines[0].startswith("ax_1 | TextView | WLAN | (")
    assert not lines[0].startswith("  ")
    assert "op=" not in digest  # flat mode omits the op column


def test_flat_layout_appends_optional_container_path():
    xml = (
        "<hierarchy>"
        '<node class="androidx.recyclerview.widget.RecyclerView" scrollable="true" '
        'bounds="[0,0][1080,2400]">'
        '<node text="item" class="android.widget.TextView" clickable="true" '
        'enabled="true" bounds="[0,100][1080,300]"/>'
        "</node></hierarchy>"
    )
    digest = PhoneSession.format_marks_digest(_to_marks(_parse(xml)))
    item_line = next(ln for ln in digest.splitlines() if "item" in ln)
    assert item_line.endswith("path=list")


def test_marks_digest_still_labels_container_roles_flat():
    # Regression from the legacy container-role test: [容器] label preserved.
    container = MarkCandidate("ax_1@e2", [0, 0, 1000, 800], [500, 400], role="GridView")
    text = MarkCandidate("ax_2@e2", [0, 100, 1000, 140], [500, 120], role="TextView")
    digest = PhoneSession.format_marks_digest([container, text])
    assert "ax_1@e2 | [容器]GridView" in digest
    assert "ax_2@e2 | TextView" in digest


def test_max_items_semantics_unchanged_windowed():
    marks = [
        MarkCandidate(
            f"ax_{i}",
            [0, i, 10, i + 10],
            [5, i + 5],
            role="Button",
            text_summary=f"b{i}",
            window_id="W1",
            window_layer=5,
            window_type="TYPE_APPLICATION",
            actionability="confirmed",
        )
        for i in range(1, 6)
    ]
    digest = PhoneSession.format_marks_digest(marks, max_items=2)
    assert "... (+3 more)" in digest
    # Only the first two marks rendered.
    assert "b1" in digest and "b2" in digest
    assert "b3" not in digest


def test_non_windowed_marks_render_flat():
    # locate marks / test doubles carry no window_id -> flat.
    marks = [MarkCandidate("loc_1@e1", [0, 0, 10, 10], [5, 5], role="ImageView", text_summary="隐藏目标")]
    digest = PhoneSession.format_marks_digest(marks)
    assert digest.startswith("loc_1@e1 | ImageView | 隐藏目标")


# --------------------------------------------------------------------------
# Config three-state.
# --------------------------------------------------------------------------
def test_marks_windowed_default_is_auto(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_MARKS_WINDOWED", raising=False)
    assert V2Config.from_env().marks_windowed == "auto"


@pytest.mark.parametrize("value", ["on", "off", "auto"])
def test_marks_windowed_valid_values(monkeypatch, value):
    monkeypatch.setenv("PHONE_AGENT_MARKS_WINDOWED", value)
    assert V2Config.from_env().marks_windowed == value


def test_marks_windowed_illegal_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MARKS_WINDOWED", "garbage")
    assert V2Config.from_env().marks_windowed == "auto"


def test_marks_windowed_override(monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_MARKS_WINDOWED", raising=False)
    assert V2Config.from_env({"marks_windowed": "on"}).marks_windowed == "on"


# --------------------------------------------------------------------------
# Device dual-mode dump + auto fallback.
# --------------------------------------------------------------------------
def test_auto_mode_uses_windows_when_supported(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCP(stdout="junk" + _DISPLAYS_WINDOWED + "tail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = device.dump_uiautomator_xml(windowed="auto")
    assert out.startswith("<?xml") and out.endswith("</displays>")
    assert "--windows" in calls[0]
    assert len(calls) == 1  # windows dump succeeded, no second dump


def test_auto_mode_falls_back_to_legacy_when_windows_unsupported(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--windows" in cmd:
            return _FakeCP(stdout="", returncode=1)
        return _FakeCP(
            stdout='pre<?xml version="1.0"?><hierarchy><node bounds="[0,0][1,1]"/></hierarchy>post'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = device.dump_uiautomator_xml(windowed="auto")
    assert out.endswith("</hierarchy>")
    assert "<displays" not in out
    assert any("--windows" in c for c in calls) and len(calls) == 2


def test_on_mode_raises_when_windows_unsupported(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--windows" in cmd:
            return _FakeCP(stdout="", returncode=1)
        return _FakeCP(stdout='<?xml version="1.0"?><hierarchy></hierarchy>')

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        device.dump_uiautomator_xml(windowed="on")


def test_off_mode_never_requests_windows(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCP(
            stdout='<?xml version="1.0"?><hierarchy><node bounds="[0,0][1,1]"/></hierarchy>'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = device.dump_uiautomator_xml(windowed="off")
    assert out.endswith("</hierarchy>")
    assert not any("--windows" in c for c in calls)


def test_auto_mode_reuses_windows_hierarchy_output(monkeypatch):
    # A device whose --windows returns a plain <hierarchy> (no <displays>) is
    # reused directly rather than paying for a second legacy dump.
    def fake_run(cmd, **kwargs):
        if "--windows" in cmd:
            return _FakeCP(
                stdout='<?xml version="1.0"?><hierarchy><node bounds="[0,0][1,1]"/></hierarchy>'
            )
        raise AssertionError("legacy dump should not be called")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = device.dump_uiautomator_xml(windowed="auto")
    assert out.endswith("</hierarchy>")


def test_displays_truncation_survives_trailing_noise(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCP(stdout="lead" + _DISPLAYS_WINDOWED + "UI hierchary dumped trailing")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = device.dump_uiautomator_xml(windowed="on")
    assert out.startswith("<?xml")
    assert out.endswith("</displays>")
    # The full window payload survived the slice.
    assert "TYPE_SYSTEM" in out
