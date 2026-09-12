"""Offline HTML report contract: no CDN, escaped, local-path only, three layers."""

from __future__ import annotations

from report import _escape_report_data, render_html, sanitize_summary_paths


def _summary(**overrides):
    base = {
        "run_id": "t1",
        "created_at": "t",
        "target": "打开设置",
        "verdict": "success",
        "duration_sec": 1.0,
        "steps": 1,
        "harness_terminal": {"state": "succeeded", "finished": True, "run_end_seen": True, "stop_requested": False},
        "case_acceptance": {"overall": "pass", "checkpoints": [{"id": "A1", "description": "d", "status": "met", "evidence": ["step 1"]}]},
        "case": {"id": "c1", "title": "t", "goal": "g", "preconditions": ["解锁"], "safety_boundaries": ["不得删除"], "acceptance": [], "notes": ""},
        "diagnosis": {"inference": True, "caveat": "候选归因，非已证根因"},
        "finish_gate": {"attempted": True, "accepted": True, "blocked_by_open_items": False, "rejections": []},
        "finish_verifier": {"review_packets": 1, "confirmed": 1, "rejections": [], "rejection_count": 0, "dispute_takeover": False, "verifier_status": "pass_or_not_triggered"},
        "taskdoc_final": {"items": [], "counts": {}, "terminal_state": "no_board"},
        "context": {}, "context_errors": {"errors": []},
        "hitl": {"decisions": [], "unresolved_prompts": []},
        "safety": {"warnings": [], "count": 0},
        "budget": {"token_budget": 1000, "used_tokens": 10, "exhausted": False, "loop_fuse_hit": False},
        "tool_health": {"by_tool": {}, "total_calls": 0, "total_errors": 0, "error_rate": 0},
        "grounding": {}, "visual": {}, "windowing": {"present": False}, "model": {"token_usage": {}},
        "fallback": [], "replay": [], "findings": [], "recommendations": [], "notes": [],
    }
    base.update(overrides)
    return base


def test_report_is_offline_no_remote_assets():
    html = render_html(_summary(), [])
    assert "https://" not in html
    assert "http://" not in html
    assert "@import" not in html
    assert "<script src" not in html
    assert "<link" not in html


def test_report_has_three_layers():
    html = render_html(_summary(), [])
    assert "harness 终局" in html
    assert "Case 验收" in html
    assert "诊断推断" in html


def test_screenshot_path_is_allowlisted():
    summary = _summary(
        replay=[
            {
                "step": 1,
                "model_text": "x",
                "model_tool_calls": [],
                "tool_calls": [
                    {"tool": "read_screen", "result_text": "[OBS]", "class": "observation", "image": {"present": True, "path": "../../etc/passwd"}},
                    {"tool": "read_screen", "result_text": "[OBS]", "class": "observation", "image": {"present": True, "path": "http://evil.example/x.png"}},
                    {"tool": "read_screen", "result_text": "[OBS]", "class": "observation", "image": {"present": True, "path": "screenshots/screen-1.png"}},
                ],
            }
        ]
    )
    html = render_html(summary, [])
    assert "../../etc/passwd" not in html
    assert "evil.example" not in html
    assert "screenshots/screen-1.png" in html


def test_sanitize_summary_paths_strips_remote_and_traversal():
    dirty = {
        "run_dir": "/etc",
        "artifacts": {"summary": "https://evil/x", "report": "a/../../b", "evidence": "evidence.jsonl"},
        "replay": [{"image": {"path": "screenshots/screen-2.png"}}, {"image": {"path": "../secret.png"}}],
    }
    clean = sanitize_summary_paths(dirty)
    assert clean["run_dir"] is None
    assert clean["artifacts"]["summary"] is None
    assert clean["artifacts"]["report"] is None
    assert clean["artifacts"]["evidence"] == "evidence.jsonl"
    assert clean["replay"][0]["image"]["path"] == "screenshots/screen-2.png"
    assert clean["replay"][1]["image"]["path"] is None


def test_script_payload_cannot_close_script():
    payload = '</script><script>alert(1)</script>'
    escaped = _escape_report_data(payload)
    assert "</script>" not in escaped
    assert "\\u003c/script" in escaped
    html = render_html(_summary(target=payload), [])
    # the raw closing sequence never appears inside the JSON island
    assert "</script><script>alert(1)" not in html


def test_escape_helper_neutralizes_angle_brackets():
    assert "<" not in _escape_report_data("<b>") and "&" not in _escape_report_data("&")
