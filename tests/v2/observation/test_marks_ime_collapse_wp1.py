"""WP1: soft-keyboard window collapse + node-label priority.

Offline-pinned defects, both in the display layer:

* an open keyboard (``TYPE_INPUT_METHOD``) ships dozens of clickable key nodes.
  As ordinary candidates they took 68 of the 80 retained marks and left the
  foreground app 8, so the model could not see the control it was asked to tap
  and fell back to a slow visual locate for something already in the dump. Keys
  are now dropped before the per-window quota; only action keys survive, and a
  dropped key is never replaced by a synthesized mark (P0 #2 — a mark maps a
  real accessibility node);
* ``_node_text`` merged ``text`` and ``content-desc``. A resource-id-shaped desc
  then spent the 32-char digest budget the model reads. Text now wins outright;
  the desc is the label only for a node with no text.

Neither change touches addressing, execution, the safety gate, folding or
locate, and a legacy ``<hierarchy>`` dump (weak windows, no real type) keeps the
historic behaviour byte-for-byte.
"""

from __future__ import annotations

from phone_agent.grounding.accessibility import _parse_uiautomator_xml
from phone_agent.grounding.provider import MarkCandidate
from phone_agent.v2.session import PhoneSession
from phone_agent.v2.tools._obs import auto_observation
from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.device import FakeDeviceFactory


# --------------------------------------------------------------------------
# Fixtures / helpers. Coordinates are multiples of 108 / 240 on a 1080x2400
# screen so the 0-1000 relative bbox is exact and can be asserted on.
# --------------------------------------------------------------------------
def _node(
    *,
    text: str | None = None,
    desc: str | None = None,
    x: int,
    y: int,
    w: int = 108,
    h: int = 240,
    pkg: str = "com.app",
    **extra: str,
) -> str:
    attrs = {
        "class": "android.widget.Button",
        "clickable": "true",
        "enabled": "true",
        "package": pkg,
        **extra,
    }
    if text is not None:
        attrs["text"] = text
    if desc is not None:
        attrs["content-desc"] = desc
    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return (
        f'<node {attr_str} bounds="[{x},{y}][{x + w},{y + h}]"/>'
    )


def _keyboard_window(keys: str, *, layer: int = 30) -> str:
    return (
        f'<window id="9" layer="{layer}" type="TYPE_INPUT_METHOD" title="键盘" '
        'bounds="[0,1400][1080,2400]" active="true" focused="true"><hierarchy>'
        '<node class="android.widget.LinearLayout" bounds="[0,1400][1080,2400]" '
        f'package="com.ime">{keys}</node>'
        "</hierarchy></window>"
    )


def _app_window(nodes: str, *, layer: int = 10) -> str:
    return (
        f'<window id="3" layer="{layer}" type="TYPE_APPLICATION" title="App" '
        'bounds="[0,0][1080,1400]" active="false" focused="false"><hierarchy>'
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,1400]" '
        f'package="com.app">{nodes}</node>'
        "</hierarchy></window>"
    )


def _displays(*windows: str) -> str:
    return (
        '<?xml version="1.0"?><displays><display id="0">'
        + "".join(windows)
        + "</display></displays>"
    )


def _parse(xml: str, *, max_marks: int = 80):
    return _parse_uiautomator_xml(
        xml,
        screen_width=1080,
        screen_height=2400,
        source="uiautomator",
        max_marks=max_marks,
    )


def _letter_keys(count: int, *, pkg: str = "com.ime") -> str:
    return "".join(
        _node(text=f"key{i}", x=108 * (i % 8), y=1440 + 120 * (i // 8), h=120, pkg=pkg)
        for i in range(count)
    )


_ACTION_KEYS = ("搜索", "完成", "发送", "确定", "前往", "下一步", "GO", "Search", "Done", "Enter", "Next")


# --------------------------------------------------------------------------
# Quota: the keyboard no longer starves the foreground window.
# --------------------------------------------------------------------------
def test_keyboard_keys_no_longer_starve_the_app_window():
    # 12 app controls + 60 letter keys + 2 action keys, max_marks=20. Without
    # the collapse the higher-layer keyboard window won the remainder and the
    # app kept 8 of its 12 controls (the reported defect); with it every app
    # control survives and the keyboard contributes only its action keys.
    app = "".join(
        _node(text=f"控件{i}", x=108 * (i % 4), y=240 * (i // 4)) for i in range(12)
    )
    keyboard = _letter_keys(60) + _node(text="搜索", x=756, y=2160, pkg="com.ime") + _node(
        text="完成", x=864, y=2160, pkg="com.ime"
    )
    parsed = _parse(_displays(_app_window(app), _keyboard_window(keyboard)), max_marks=20)

    app_texts = {m["text_summary"] for m in parsed["marks"] if m["window_id"] == "W1"}
    assert app_texts == {f"控件{i}" for i in range(12)}
    ime_texts = {m["text_summary"] for m in parsed["marks"] if m["window_id"] == "W2"}
    assert ime_texts == {"搜索", "完成"}
    assert len(parsed["marks"]) == 14


def test_ime_action_keys_survive_with_their_real_geometry():
    keys = "".join(
        (_node(text=label, x=108 * i, y=2160, pkg="com.ime") for i, label in enumerate(_ACTION_KEYS))
    )
    keyboard = keys + _node(text="q", x=0, y=1440, pkg="com.ime")
    parsed = _parse(_displays(_app_window(""), _keyboard_window(keyboard)))

    texts = [m["text_summary"] for m in parsed["marks"]]
    assert set(texts) == set(_ACTION_KEYS)
    assert "q" not in texts
    # The kept key carries the node's own bounds (108/1080 -> 100, 2160/2400 ->
    # 900), i.e. it is the real node, not a stand-in.
    first = next(m for m in parsed["marks"] if m["text_summary"] == "搜索")
    assert first["bbox"] == [0, 900, 100, 1000]
    assert first["window_id"] == "W2"


def test_ime_action_attribute_decides_keys_without_a_named_label():
    # Not every producer spells the action in the label: a real IME action on the
    # node keeps the key, while none / unspecified (0 and 1) does not.
    keys = (
        _node(text="→", x=0, y=2160, pkg="com.ime", **{"ime-action": "next"})
        + _node(text="q", x=108, y=1440, pkg="com.ime", **{"ime-action": "0"})
        + _node(text="w", x=216, y=1440, pkg="com.ime", **{"ime-action": "1"})
        + _node(text="e", x=324, y=1440, pkg="com.ime", **{"ime-action": "5"})
    )
    parsed = _parse(_displays(_app_window(""), _keyboard_window(keys)))

    assert [m["text_summary"] for m in parsed["marks"]] == ["→", "e"]
    assert parsed["parse_summary"]["ime_collapsed_key_count"] == 2


def test_collapsed_keys_never_become_synthetic_marks():
    app = _node(text="提交订单", x=108, y=240)
    parsed = _parse(_displays(_app_window(app), _keyboard_window(_letter_keys(40))))

    # Exactly the one real app node; the 40 keys produced nothing at all.
    assert [m["text_summary"] for m in parsed["marks"]] == ["提交订单"]
    assert [m["mark_id"] for m in parsed["marks"]] == ["ax_1"]
    assert parsed["parse_summary"]["ime_collapsed_key_count"] == 40


def test_parse_summary_reports_keyboard_collapse_counts():
    app = "".join(_node(text=f"控件{i}", x=108 * i, y=240) for i in range(3))
    keyboard = _letter_keys(40) + _node(text="搜索", x=756, y=2160, pkg="com.ime")
    summary = _parse(_displays(_app_window(app), _keyboard_window(keyboard)))["parse_summary"]

    assert summary["ime_window_count"] == 1
    assert summary["ime_collapsed_key_count"] == 40
    # Candidates are counted *after* the collapse, so the model-facing
    # ``marks (K/total)`` never promises keys that cannot be addressed.
    assert summary["total_candidates"] == 4
    assert summary["per_window_counts"] == {"W1": 3, "W2": 1}


def test_ime_window_count_zero_without_a_keyboard():
    summary = _parse(_displays(_app_window(_node(text="控件", x=108, y=240))))["parse_summary"]
    assert summary["ime_window_count"] == 0
    assert summary["ime_collapsed_key_count"] == 0


def test_legacy_hierarchy_dump_never_collapses_keys():
    # A legacy single-root dump yields one *weak* window: no real type, so the
    # keyboard detection must not fire and the marks stay byte-identical.
    xml = (
        "<hierarchy>"
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" package="com.ime">'
        + _node(text="q", x=0, y=1440, pkg="com.ime")
        + _node(text="w", x=108, y=1440, pkg="com.ime")
        + _node(text="完成", x=864, y=2160, pkg="com.ime")
        + "</node></hierarchy>"
    )
    parsed = _parse(xml)
    assert [m["text_summary"] for m in parsed["marks"]] == ["q", "w", "完成"]
    assert parsed["parse_summary"]["ime_window_count"] == 0
    assert parsed["parse_summary"]["ime_collapsed_key_count"] == 0


# --------------------------------------------------------------------------
# Node label: text wins, content-desc is the fallback.
# --------------------------------------------------------------------------
def test_text_wins_over_a_resource_id_shaped_content_desc():
    xml = _displays(
        _app_window(
            _node(text="上海", desc="com.ctrip.flight:id/flight_list_item_title_button", x=108, y=240)
        )
    )
    marks = _parse(xml)["marks"]
    assert [m["text_summary"] for m in marks] == ["上海"]


def test_content_desc_is_used_when_the_node_has_no_text():
    xml = _displays(_app_window(_node(desc="返回", x=108, y=240)))
    assert [m["text_summary"] for m in _parse(xml)["marks"]] == ["返回"]


def test_display_digest_spends_its_budget_on_the_visible_label():
    xml = _displays(
        _app_window(
            _node(text="上海", desc="com.ctrip.flight:id/flight_list_item_title_button", x=108, y=240)
        )
    )
    marks = [
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
        )
        for m in _parse(xml)["marks"]
    ]
    digest = PhoneSession.format_marks_digest(marks, window_source="shell_windows")
    line = next(ln for ln in digest.splitlines() if "ax_1" in ln)
    assert "上海" in line
    assert "com.ctrip" not in line


# --------------------------------------------------------------------------
# Render: the keyboard note is display-only and never an address.
# --------------------------------------------------------------------------
def _obs_text(xml: str) -> str:
    session = PhoneSession(
        FakeConfig(),
        device_factory=FakeDeviceFactory(observing=True, xml=xml),
    )
    return auto_observation(session)[0]["text"]


def test_obs_text_renders_the_keyboard_collapse_note():
    app = _node(text="提交订单", x=108, y=240)
    text = _obs_text(_displays(_app_window(app), _keyboard_window(_letter_keys(40))))
    lines = text.splitlines()
    assert lines[0].startswith("[OBS] app=")
    assert lines[1] == "keyboard: open (40 keys collapsed)"
    # The marks summary keeps the shape the folding/parsing layers key on.
    assert lines[2].startswith("marks (1)")
    assert "提交订单" in text


def test_obs_text_has_no_keyboard_note_without_a_collapse():
    text = _obs_text(_displays(_app_window(_node(text="提交订单", x=108, y=240))))
    assert "keyboard:" not in text
    assert text.splitlines()[1].startswith("marks (1)")
