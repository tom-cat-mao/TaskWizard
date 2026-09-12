"""WP-S3: windowed marks parsing + windowing dimension + blocked-tap finding.

The parallel package WP-G2a upgrades the OBS marks render to a window-grouped
format. These tests pin the skill's adaptation:

* :func:`parse_obs_windows` parses the ``windowed/v1 source=...`` grouped format
  (window heads + indented mark lines + optional ``op=`` / ``path=`` fields) and
  returns ``None`` for the legacy flat format;
* :func:`build_windowing` aggregates window count, op-tier counts, window types,
  and correlates the model's actuation against ``op=blocked`` marks;
* ``build_summary`` grows an additive ``windowing`` block and a
  ``windowed_blocked_tap`` finding, while a legacy flat run stays inert;
* the report renders a 窗口结构 dimension card and per-step window summaries.
"""

from __future__ import annotations

from typing import Any

from analyze import build_summary, build_windowing
from evidence import EvidenceView, parse_obs_windows
from report import render_html


# --------------------------------------------------------------------------
# a windowed OBS text sample (matches the EXEC-WP-S3 contract)
# --------------------------------------------------------------------------
WINDOWED_OBS = (
    "OK. tap 返回 at (58,35)\n"
    "[OBS] app=微信 screen#12\n"
    "marks (4): windowed/v1 source=shell_windows\n"
    "W1 TYPE_SYSTEM com.android.permissioncontroller layer=42 active focus\n"
    "  ax_1@e12 | Button | 仅本次允许 | (500,522) | op=confirmed | path=dialog\n"
    "  ax_2@e12 | Button | 拒绝 | (300,522) | op=likely | path=dialog\n"
    "W2 TYPE_APPLICATION com.tencent.mm layer=10 covered_by=W1\n"
    "  ax_3@e12 | ImageButton | 返回 | (58,35) | op=blocked | path=toolbar\n"
    "  ax_4@e12 | TextView | 标题 | (540,80)\n"
)

FLAT_OBS = (
    "OK. tap 登录 at (100,200)\n"
    "[OBS] app=com.android.settings screen#3\n"
    "marks (2): ax_1 | Button | 登录 | (100,200); ax_2 | TextView | 用户名 | (50,80)"
)


def _view(events: list[dict[str, Any]]) -> EvidenceView:
    return EvidenceView.from_events(events)


def _obs_event(step: int, tool: str, text: str, **extra: Any) -> dict[str, Any]:
    event = {
        "event": "tool_observation",
        "step": step,
        "tool": tool,
        "latency_ms": 5,
        "result_text": text,
        "obs": {"current_app": "微信", "screen_seq": 12, "mark_count": 4},
        "image": {"present": False, "screen_seq": None, "bytes": 0},
        "error": None,
    }
    event.update(extra)
    return event


# --------------------------------------------------------------------------
# parse_obs_windows
# --------------------------------------------------------------------------
def test_parse_windowed_obs_full_shape():
    parsed = parse_obs_windows(WINDOWED_OBS)
    assert parsed is not None
    assert parsed["present"] is True
    assert parsed["schema"] == "v1"
    assert parsed["source"] == "shell_windows"
    assert parsed["window_count"] == 2

    w1, w2 = parsed["windows"]
    assert w1["id"] == "W1"
    assert w1["type"] == "TYPE_SYSTEM"
    assert w1["package"] == "com.android.permissioncontroller"
    assert w1["layer"] == 42
    assert w1["covered_by"] is None
    assert "active" in w1["flags"] and "focus" in w1["flags"]
    assert w1["mark_count"] == 2
    assert w1["op_counts"] == {"confirmed": 1, "likely": 1}

    assert w2["id"] == "W2"
    assert w2["type"] == "TYPE_APPLICATION"
    assert w2["covered_by"] == "W1"
    assert w2["mark_count"] == 2
    # one blocked + one op-less (unspecified) mark
    assert w2["op_counts"] == {"blocked": 1, "unspecified": 1}

    # aggregate op tally + blocked ids
    assert parsed["op_counts"]["confirmed"] == 1
    assert parsed["op_counts"]["likely"] == 1
    assert parsed["op_counts"]["blocked"] == 1
    assert parsed["op_counts"]["unspecified"] == 1
    assert parsed["blocked_mark_ids"] == ["ax_3@e12"]

    # the op-less mark still parses its fields (path absent -> None)
    op_less = w2["marks"][1]
    assert op_less["mark_id"] == "ax_4@e12"
    assert op_less["op"] is None
    assert op_less["path"] is None
    # the blocked mark keeps its path
    assert w2["marks"][0]["path"] == "toolbar"


def test_parse_flat_obs_returns_none():
    # Legacy flat format has no ``windowed/`` badge -> not windowed.
    assert parse_obs_windows(FLAT_OBS) is None


def test_parse_no_obs_or_no_body_returns_none():
    assert parse_obs_windows("") is None
    assert parse_obs_windows("no obs marker here") is None
    # header with the badge but no body lines still returns a (present) block
    # with zero windows rather than raising.
    only_header = "[OBS] app=x screen#1\nmarks (0): windowed/v1 source=shell_windows"
    parsed = parse_obs_windows(only_header)
    assert parsed is None  # no trailing newline/body => no window section


def test_parse_is_fail_open_on_garbage_body():
    # Malformed window/mark lines are tolerated (skipped), never raise.
    text = (
        "[OBS] app=x screen#1\n"
        "marks (3): windowed/v1 source=shell\n"
        "garbage line without pipe or W head\n"
        "W1 TYPE_APPLICATION com.x layer=nan\n"
        "  | | |\n"  # empty mark id -> skipped
        "  ax_9@e1 | Button | ok | (1,2) | op=confirmed\n"
    )
    parsed = parse_obs_windows(text)
    assert parsed is not None
    assert parsed["window_count"] == 1
    w1 = parsed["windows"][0]
    assert w1["layer"] is None  # 'nan' failed int() -> None, not a crash
    assert w1["mark_count"] == 1  # the empty-id line was skipped
    assert w1["op_counts"] == {"confirmed": 1}


def test_parse_windowed_obs_accepts_shown_retained_total_count():
    text = (
        "[OBS] app=x screen#1\n"
        "marks (40/100) [retained:47]: windowed/v1 source=shell_windows\n"
        "W2 TYPE_SYSTEM com.popup layer=42 active focus\n"
        "  ax_47@e1 | Button | allow | (500,500) | op=confirmed\n"
    )

    parsed = parse_obs_windows(text)

    assert parsed is not None
    assert parsed["source"] == "shell_windows"
    assert parsed["window_count"] == 1
    assert parsed["windows"][0]["marks"][0]["mark_id"] == "ax_47@e1"


# --------------------------------------------------------------------------
# build_windowing dimension
# --------------------------------------------------------------------------
def test_build_windowing_aggregates_across_observations():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "返回上一页"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", WINDOWED_OBS),
        {"event": "run_end", "steps": 1, "terminal": {"finished": True}},
    ]
    windowing = build_windowing(_view(events))
    assert windowing["present"] is True
    assert windowing["windowed_observations"] == 1
    assert windowing["peak_window_count"] == 2
    assert windowing["op_counts"]["blocked"] == 1
    assert windowing["window_types"] == {"TYPE_SYSTEM": 1, "TYPE_APPLICATION": 1}
    assert windowing["blocked_taps"] == []


def test_build_windowing_inert_for_flat_run():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "登录"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", FLAT_OBS),
        {"event": "run_end", "steps": 1, "terminal": {"finished": True}},
    ]
    windowing = build_windowing(_view(events))
    assert windowing["present"] is False
    assert windowing["windowed_observations"] == 0
    assert windowing["peak_window_count"] == 0
    assert windowing["window_types"] == {}
    assert windowing["blocked_taps"] == []


def test_blocked_tap_detected_when_model_taps_blocked_mark():
    # Step 1 observes a windowed screen where ax_3@e12 is op=blocked.
    # Step 2 taps ax_3@e12 by mark id -> a blocked-tap finding.
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "返回"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", WINDOWED_OBS),
        {
            "event": "tool_invoke",
            "step": 2,
            "tool": "tap",
            "args": {"target_mark_id": "ax_3@e12"},
        },
        _obs_event(2, "tap", "OK. 已点击 返回 (ax_3@e12)\n" + WINDOWED_OBS),
        {"event": "run_end", "steps": 2, "terminal": {"finished": True}},
    ]
    windowing = build_windowing(_view(events))
    assert len(windowing["blocked_taps"]) == 1
    tap = windowing["blocked_taps"][0]
    assert tap["step"] == 2
    assert tap["tool"] == "tap"
    assert tap["target_mark_id"] == "ax_3@e12"


def test_confirmed_mark_tap_is_not_flagged():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "允许"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", WINDOWED_OBS),
        {
            "event": "tool_invoke",
            "step": 2,
            "tool": "tap",
            "args": {"target_mark_id": "ax_1@e12"},  # op=confirmed
        },
        _obs_event(2, "tap", "OK. 已点击 仅本次允许 (ax_1@e12)\n" + WINDOWED_OBS),
        {"event": "run_end", "steps": 2, "terminal": {"finished": True}},
    ]
    windowing = build_windowing(_view(events))
    assert windowing["blocked_taps"] == []


# --------------------------------------------------------------------------
# build_summary wiring: additive windowing block + finding
# --------------------------------------------------------------------------
def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return build_summary(
        {"finished": True, "reason": "finished"},
        _view(events),
        run_id="t1",
        created_at="2026-01-01T00:00:00",
        target="返回",
    )


def test_summary_has_windowing_block_and_blocked_finding():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "返回"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", WINDOWED_OBS),
        {
            "event": "tool_invoke",
            "step": 2,
            "tool": "tap",
            "args": {"target_mark_id": "ax_3@e12"},
        },
        _obs_event(2, "tap", "OK. 已点击 返回 (ax_3@e12)\n" + WINDOWED_OBS),
        {"event": "run_end", "steps": 2, "terminal": {"finished": True}},
    ]
    summary = _summary(events)
    windowing = summary["windowing"]
    assert windowing["present"] is True
    assert windowing["peak_window_count"] == 2
    # the blocked-tap surfaces as a finding
    categories = {f["category"] for f in summary["findings"]}
    assert "windowed_blocked_tap" in categories
    finding = next(f for f in summary["findings"] if f["category"] == "windowed_blocked_tap")
    assert finding["count"] == 1
    assert finding["severity"] == "P1"
    # replay carries the per-step window summary for the report
    replay_calls = [
        call for step in summary["replay"] for call in step["tool_calls"]
    ]
    windowed_calls = [c for c in replay_calls if c.get("windows")]
    assert windowed_calls, "replay lost the windowed summary"
    assert windowed_calls[0]["windows"]["window_count"] == 2


def test_summary_flat_run_windowing_absent_and_no_finding():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "登录"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", FLAT_OBS),
        {"event": "run_end", "steps": 1, "terminal": {"finished": True}},
    ]
    summary = _summary(events)
    assert summary["windowing"]["present"] is False
    categories = {f["category"] for f in summary["findings"]}
    assert "windowed_blocked_tap" not in categories
    # legacy flat replay carries no window summary (None), unchanged behavior.
    for step in summary["replay"]:
        for call in step["tool_calls"]:
            assert call.get("windows") is None


# --------------------------------------------------------------------------
# report rendering
# --------------------------------------------------------------------------
def _render(events: list[dict[str, Any]]) -> str:
    summary = _summary(events)
    return render_html(summary, events)


def test_report_renders_windowing_card_and_step_summary():
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "返回"},
        {
            "event": "model_response",
            "step": 1,
            "thinking": "看看窗口",
            "tool_calls": [{"name": "read_screen", "args": {}}],
            "usage": None,
        },
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", WINDOWED_OBS),
        {"event": "run_end", "steps": 1, "terminal": {"finished": True}},
    ]
    html = _render(events)
    assert "窗口结构" in html
    assert "renderWindowingCard" in html
    assert "renderWindowsSummary" in html
    # the window-structure functions must be defined and wired
    assert "summary.windowing" in html


def test_report_windowing_card_present_for_flat_run():
    # A flat run still renders the card (inert note), never breaks.
    events = [
        {"event": "run_start", "run_id": "t1", "task_goal_base": "登录"},
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {}},
        _obs_event(1, "read_screen", FLAT_OBS),
        {"event": "run_end", "steps": 1, "terminal": {"finished": True}},
    ]
    html = _render(events)
    assert "窗口结构" in html
    # base64-free guarantee still holds
    assert "data:image" not in html
