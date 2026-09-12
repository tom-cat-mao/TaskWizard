"""Case model contract: validation, the shipped example, and acceptance rollup."""

from __future__ import annotations

from pathlib import Path

import pytest

from case import (
    CASE_FAIL,
    CASE_INCOMPLETE,
    CASE_PASS,
    CASE_UNKNOWN,
    STATUS_MET,
    STATUS_UNKNOWN,
    STATUS_UNMET,
    Acceptance,
    AcceptanceEvidence,
    Case,
    evaluate_acceptance,
    load_case,
    rollup_acceptance,
    synthetic_case,
)

_ROOT = Path(__file__).resolve().parents[2]


def test_case_requires_goal():
    with pytest.raises(ValueError):
        Case.from_dict({"title": "no goal"})


def test_shipped_synthetic_case_loads():
    path = _ROOT / ".agents/skills/phone-agent-live-diagnosis/references/cases/settings-wifi.synthetic.json"
    case = load_case(path)
    assert case.goal
    assert len(case.acceptance) == 2
    assert case.safety_boundaries


def test_acceptance_unknown_without_evidence():
    case = Case(
        id="c",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("NEVER",)),),
    )
    evidence = AcceptanceEvidence([("step 1", "无关文本")], harness_finished=True)
    results = evaluate_acceptance(case, evidence)
    assert results[0]["status"] == STATUS_UNKNOWN
    assert rollup_acceptance(results, harness_finished=True) == CASE_UNKNOWN


def test_acceptance_met_on_match_unmet_on_contradiction():
    from case import KIND_OBJECTIVE

    case = Case(
        id="c",
        title="t",
        goal="g",
        acceptance=(
            Acceptance(id="A1", description="met", match=("设置",)),
            Acceptance(id="A2", description="unmet", match=("NEVER",), contradict=("已确认完成",)),
        ),
    )
    evidence = AcceptanceEvidence(
        [
            ("step 1", "设置页可见", KIND_OBJECTIVE),
            ("step 2", "已确认完成", KIND_OBJECTIVE),
        ],
        harness_finished=True,
    )
    results = evaluate_acceptance(case, evidence)
    assert [r["status"] for r in results] == [STATUS_MET, STATUS_UNMET]
    assert rollup_acceptance(results, harness_finished=True) == CASE_FAIL


def test_candidate_claims_never_pass():
    # A 2-tuple defaults to candidate; even a literal match stays unknown.
    case = Case(
        id="c",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("设置",)),),
    )
    evidence = AcceptanceEvidence([("finish 自述", "设置已完成")], harness_finished=True)
    results = evaluate_acceptance(case, evidence)
    assert results[0]["status"] == STATUS_UNKNOWN
    assert results[0]["evidence"] == ["finish 自述"]


def test_objective_match_and_contradiction_conflict_is_unknown():
    from case import KIND_OBJECTIVE

    case = Case(
        id="c",
        title="t",
        goal="g",
        acceptance=(Acceptance(id="A1", description="d", match=("设置",), contradict=("未找到",)),),
    )
    evidence = AcceptanceEvidence(
        [("step 1", "设置", KIND_OBJECTIVE), ("step 2", "未找到", KIND_OBJECTIVE)],
        harness_finished=True,
    )
    results = evaluate_acceptance(case, evidence)
    assert results[0]["status"] == STATUS_UNKNOWN


def test_rollup_incomplete_when_not_finished():
    results = [{"id": "A1", "status": STATUS_UNKNOWN}]
    assert rollup_acceptance(results, harness_finished=False) == CASE_INCOMPLETE


def test_rollup_pass_only_when_all_met():
    results = [{"id": "A1", "status": STATUS_MET}, {"id": "A2", "status": STATUS_MET}]
    assert rollup_acceptance(results, harness_finished=True) == CASE_PASS


def test_synthetic_case_shape():
    case = synthetic_case()
    assert case.acceptance and case.preconditions and case.safety_boundaries


def test_task_text_includes_constraints_not_verified_by_default():
    case = Case(
        id="c",
        title="t",
        goal="打开 Wi-Fi",
        preconditions=("设备已解锁",),
        safety_boundaries=("不得忘记网络",),
    )
    text = case.task_text()
    assert "打开 Wi-Fi" in text
    assert "未经验证的假设" in text
    assert "设备已解锁" in text
    assert "不得忘记网络" in text
    assert "safety 层不会自动执行" in text


def test_task_text_marks_confirmed_preconditions():
    case = Case(
        id="c",
        title="t",
        goal="g",
        preconditions=("已解锁",),
        preconditions_confirmed=True,
    )
    assert "已人工确认" in case.task_text()
