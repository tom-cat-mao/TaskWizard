"""WP-S1 source-map coverage for newly attributable v2 layers."""

from __future__ import annotations

import pytest

from sourcemap import add_line_numbers, rule_for
from taxonomy import category_of, classify_result


@pytest.mark.parametrize(
    ("category", "expected_files"),
    [
        (
            "resolver",
            {
                "phone_agent/v2/names.py",
                "phone_agent/v2/tools/actuation.py",
                "phone_agent/v2/middleware/trace.py",
            },
        ),
        (
            "deliverable",
            {
                "phone_agent/v2/tools/deliverable.py",
                "phone_agent/v2/capabilities.py",
            },
        ),
        (
            "secure_screenshot",
            {
                "phone_agent/v2/tools/_obs.py",
                "phone_agent/v2/session.py",
                "phone_agent/adb/screenshot.py",
            },
        ),
        ("recall", {"phone_agent/v2/recall.py"}),
        ("capabilities", {"phone_agent/v2/capabilities.py"}),
    ],
)
def test_new_categories_map_to_current_v2_owners(category, expected_files):
    rule = rule_for(category)

    assert rule is not None
    assert expected_files <= set(rule["files"])
    resolved = add_line_numbers(rule["files"])
    assert all(item["exists"] for item in resolved)
    assert all(item["anchors"] for item in resolved)


def test_launch_rule_includes_unified_name_resolver():
    rule = rule_for("launch")

    assert rule is not None
    assert "phone_agent/v2/names.py" in rule["files"]


def test_resolution_attempt_is_owned_by_resolver_rule():
    rule = rule_for("resolver")

    assert rule is not None
    assert "resolution_attempt" in rule["suggestion"]
    assert "decision/winner/candidates" in rule["verify"]


@pytest.mark.parametrize(
    ("receipt", "expected_file"),
    [
        (
            "ambiguous app '微信': com.tencent.mm(rank_score=0.940, exact/exact_alias), "
            "com.tencent.wework(rank_score=0.913, lexical/registered_containment) — be more specific",
            "phone_agent/v2/names.py",
        ),
        (
            "error: document was not written (html exceeds 262144 byte limit)",
            "phone_agent/v2/tools/deliverable.py",
        ),
        (
            "[OBS] 此屏被系统级保护（登录/支付页）。\n截图不可用。",
            "phone_agent/adb/screenshot.py",
        ),
    ],
)
def test_new_finding_receipts_resolve_to_causal_source(receipt, expected_file):
    category = category_of(classify_result(receipt))
    rule = rule_for(category)

    assert rule is not None
    assert expected_file in rule["files"]
