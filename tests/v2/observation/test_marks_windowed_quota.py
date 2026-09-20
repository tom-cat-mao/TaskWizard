"""WP-G2cA parser-layer fixes: per-window fair-share quota, window-dimension
dedup, single-pass traversal + candidate stats, and dead-branch removal.

Still a pure display layer — addressing, execution, safety, folding and locate
are untouched. Legacy single-``<hierarchy>`` output must stay byte-identical.
"""

from __future__ import annotations

from phone_agent.grounding.accessibility import _parse_uiautomator_xml


def _parse(xml: str, *, width: int = 1080, height: int = 2400, max_marks: int = 80):
    return _parse_uiautomator_xml(
        xml,
        screen_width=width,
        screen_height=height,
        source="uiautomator",
        max_marks=max_marks,
    )


def _button(text: str, x: int, y: int, pkg: str = "com.x", **extra: str) -> str:
    attrs = {"enabled": "true", **extra}
    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return (
        f'<node text="{text}" class="android.widget.Button" clickable="true" '
        f'bounds="[{x},{y}][{x + 40},{y + 40}]" package="{pkg}" {attr_str}/>'
    )


def _displays(win1_nodes: str, win2_nodes: str) -> str:
    return (
        '<?xml version="1.0"?><displays><display id="0">'
        '<window id="3" layer="10" type="TYPE_APPLICATION" title="App" '
        'bounds="[0,0][1080,2400]" active="false" focused="false"><hierarchy>'
        f'<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" package="com.bg">{win1_nodes}</node>'
        "</hierarchy></window>"
        '<window id="7" layer="42" type="TYPE_SYSTEM" title="Popup" '
        'bounds="[100,100][980,900]" active="true" focused="true"><hierarchy>'
        f'<node class="android.app.Dialog" bounds="[100,100][980,900]" package="com.pop">{win2_nodes}</node>'
        "</hierarchy></window>"
        "</display></displays>"
    )


# --------------------------------------------------------------------------
# A1 — per-window fair-share quota.
# --------------------------------------------------------------------------
def test_top_window_keeps_marks_when_content_window_would_fill_budget():
    # W1 (layer 10) has 20 candidates, W2 popup (layer 42) has 3. With the old
    # doc-order cut and a small budget the content window would eat every slot;
    # the quota floor guarantees the popup its marks.
    content = "".join(_button(f"bg{i}", i, i, pkg="com.bg") for i in range(20))
    popup = "".join(_button(f"pop{i}", 200 + i, 200 + i, pkg="com.pop") for i in range(3))
    parsed = _parse(_displays(content, popup), max_marks=10)
    by_win: dict[str, int] = {}
    for m in parsed["marks"]:
        by_win[m["window_id"]] = by_win.get(m["window_id"], 0) + 1
    assert len(parsed["marks"]) == 10
    # Popup fully retained (3 <= floor); content fills the remainder.
    assert by_win["W2"] == 3
    assert by_win["W1"] == 7
    texts = {m["text_summary"] for m in parsed["marks"]}
    assert {"pop0", "pop1", "pop2"} <= texts


def test_quota_floor_is_min_of_constant_and_fair_share():
    # 4 windows, max_marks=12 -> fair share 12//4 = 3 (< the constant 8).
    xml = (
        '<?xml version="1.0"?><displays><display id="0">'
        + "".join(
            f'<window id="{i}" layer="{10 + i}" type="TYPE_APPLICATION" '
            f'bounds="[0,0][1080,2400]" active="false" focused="false"><hierarchy>'
            f'<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" package="com.w{i}">'
            + "".join(_button(f"w{i}b{j}", j, 50 * i + j, pkg=f"com.w{i}") for j in range(6))
            + "</node></hierarchy></window>"
            for i in range(4)
        )
        + "</display></displays>"
    )
    parsed = _parse(xml, max_marks=12)
    by_win: dict[str, int] = {}
    for m in parsed["marks"]:
        by_win[m["window_id"]] = by_win.get(m["window_id"], 0) + 1
    assert len(parsed["marks"]) == 12
    # Each of the four windows is guaranteed its fair share of 3.
    assert all(by_win[w] == 3 for w in ("W1", "W2", "W3", "W4"))


def test_single_window_below_budget_keeps_every_candidate():
    xml = "<hierarchy>" + "".join(_button(f"b{i}", i, i) for i in range(5)) + "</hierarchy>"
    parsed = _parse(xml, max_marks=80)
    assert [m["mark_id"] for m in parsed["marks"]] == [f"ax_{i}" for i in range(1, 6)]


def test_parse_summary_reports_total_candidates_and_per_window_counts():
    content = "".join(_button(f"bg{i}", i, i, pkg="com.bg") for i in range(20))
    popup = "".join(_button(f"pop{i}", 200 + i, 200 + i, pkg="com.pop") for i in range(3))
    parsed = _parse(_displays(content, popup), max_marks=10)
    summary = parsed["parse_summary"]
    assert summary["total_candidates"] == 23  # pre-truncation
    assert summary["mark_count"] == 10
    assert summary["per_window_counts"] == {"W1": 20, "W2": 3}


def test_empty_summary_exposes_new_fields():
    parsed = _parse("   ")
    assert parsed["parse_summary"]["total_candidates"] == 0
    assert parsed["parse_summary"]["per_window_counts"] == {}


# --------------------------------------------------------------------------
# A2 — dedup key carries window_id.
# --------------------------------------------------------------------------
def test_same_shape_across_windows_not_merged():
    # Identical bbox/role/text in two different strong windows must both survive.
    same = _button("确定", 300, 400, pkg="com.dup")
    parsed = _parse(_displays(same, same), max_marks=80)
    confirms = [m for m in parsed["marks"] if m["text_summary"] == "确定"]
    assert len(confirms) == 2
    assert {m["window_id"] for m in confirms} == {"W1", "W2"}


def test_same_shape_same_window_still_merged():
    dup = _button("确定", 300, 400)
    xml = (
        '<hierarchy><node class="android.widget.FrameLayout" '
        'bounds="[0,0][1080,2400]" package="com.x">' + dup + dup + "</node></hierarchy>"
    )
    parsed = _parse(xml, max_marks=80)
    assert len([m for m in parsed["marks"] if m["text_summary"] == "确定"]) == 1


# --------------------------------------------------------------------------
# A3 — dominant-package resolution (the single-traversal mechanism itself is
# not pinned: the contract is that each window's package resolves correctly).
# --------------------------------------------------------------------------
def test_dominant_package_still_resolved_for_strong_and_weak_windows():
    parsed = _parse(_displays(_button("a", 10, 10, pkg="com.bg"), _button("b", 200, 200, pkg="com.pop")))
    windows = {w["window_id"]: w for w in parsed["windows"]}
    assert windows["W1"]["package"] == "com.bg"
    assert windows["W2"]["package"] == "com.pop"


# --------------------------------------------------------------------------
# A4 — dead disabled/invisible blocked branch removed.
# --------------------------------------------------------------------------
def test_disabled_and_invisible_nodes_never_produce_a_mark():
    xml = (
        '<hierarchy><node class="android.widget.FrameLayout" '
        'bounds="[0,0][1080,2400]" package="com.x">'
        + _button("live", 0, 0)
        + _button("dead", 100, 100, enabled="false")
        + _button("gone", 200, 200, **{"visible-to-user": "false"})
        + "</node></hierarchy>"
    )
    parsed = _parse(xml)
    texts = {m["text_summary"] for m in parsed["marks"]}
    assert texts == {"live"}
    # No mark ever carries the removed disabled/invisible reasons.
    for m in parsed["marks"]:
        assert "disabled" not in m["actionability_reasons"]
        assert "invisible" not in m["actionability_reasons"]


def test_blocked_only_from_strong_window_coverage():
    xml = (
        '<?xml version="1.0"?><displays><display id="0">'
        '<window id="3" layer="10" type="TYPE_APPLICATION" bounds="[0,0][1080,2400]" '
        'active="false" focused="false"><hierarchy>'
        + _button("背景", 300, 900, pkg="com.x")
        + "</hierarchy></window>"
        '<window id="7" layer="42" type="TYPE_SYSTEM" bounds="[100,600][980,1400]" '
        'active="true" focused="true"><hierarchy>'
        + _button("允许", 400, 850, pkg="com.pop")
        + "</hierarchy></window>"
        "</display></displays>"
    )
    parsed = _parse(xml)
    by_text = {m["text_summary"]: m for m in parsed["marks"]}
    assert by_text["背景"]["actionability"] == "blocked"
    assert "covered_by:W2" in by_text["背景"]["actionability_reasons"]
    assert by_text["允许"]["actionability"] == "confirmed"
