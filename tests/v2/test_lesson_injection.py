"""WP-A3 approved-lesson selection, injection, and audit tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.evolution import LessonCandidate, select_lessons_for_injection
from phone_agent.v2.recall import read_episode_events
from tests.v2.test_experience import _install_mini_agent_modules


def _lesson(
    number: int,
    *,
    status: str = "approved",
    version: int = 1,
    created_ts: float = 1.0,
    device: str | None = None,
    app: str | None = None,
    app_version: str | None = None,
    text: str | None = None,
) -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": f"les_{number:012x}",
            "schema_v": 1,
            "version": version,
            "status": status,
            "text": text or f"经验 {number}",
            "scope": {
                "device": device,
                "app": app,
                "app_version": app_version,
            },
            "evidence": [{"run_id": f"run-{number}", "note": "support"}],
            "support_count": 1,
            "task_keys": ["open_app"],
            "conflicts": [],
            "created_ts": created_ts,
            "source": "distill",
        }
    )


def _write_view(root: Path, lessons: list[LessonCandidate]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lessons.json").write_text(
        json.dumps([lesson.to_dict() for lesson in lessons], ensure_ascii=False),
        encoding="utf-8",
    )


def _bare_agent(tmp_path: Path, *, mode: str = "on") -> ThinPhoneAgent:
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(
        memory_rag=mode,
        lessons_dir=str(tmp_path / "lessons"),
        lesson_inject_max=3,
        lesson_inject_tokens=800,
    )
    agent._system_prompt = "SYSTEM"
    agent.session = SimpleNamespace(
        observe=lambda: SimpleNamespace(
            screenshot_b64="", current_app="launcher", screen_seq=1, marks=[]
        )
    )
    agent.trace_events = []
    agent._trace = SimpleNamespace(
        record_event=lambda event, **payload: agent.trace_events.append(
            (event, payload)
        )
    )
    return agent


def test_selector_accepts_only_approved_run_start_scope(tmp_path):
    lessons_dir = tmp_path / "lessons"
    eligible_global = _lesson(1, version=2)
    eligible_device = _lesson(2, device="serial-1")
    _write_view(
        lessons_dir,
        [
            eligible_device,
            _lesson(3, status="proposed"),
            _lesson(4, status="revoked"),
            _lesson(5, device="other-device"),
            _lesson(6, app="com.example.app"),
            _lesson(7, app_version="1.0"),
            eligible_global,
        ],
    )

    selected = select_lessons_for_injection(
        lessons_dir, device_scope="device:serial-1", max_items=10, max_tokens=800
    )

    assert [item.lesson_id for item in selected] == [
        eligible_global.lesson_id,
        eligible_device.lesson_id,
    ]


def test_selector_sorts_by_version_then_recency_and_truncates(tmp_path):
    lessons_dir = tmp_path / "lessons"
    high_version = _lesson(1, version=3, created_ts=1, text="A" * 40)
    recent = _lesson(2, version=2, created_ts=3, text="B" * 32)
    older = _lesson(3, version=2, created_ts=2, text="C" * 32)
    _write_view(lessons_dir, [older, high_version, recent])

    by_items = select_lessons_for_injection(
        lessons_dir, device_scope="serial-1", max_items=2, max_tokens=800
    )
    # First-fit packing weighs the exact rendered line; size the budget to
    # exactly the top-ranked line so only it fits.
    from phone_agent.v2.evolution import _lesson_rule_line
    from phone_agent.v2.middleware._tokens import estimate_text_tokens

    first_line_cost = estimate_text_tokens(_lesson_rule_line(high_version, 1))
    by_tokens = select_lessons_for_injection(
        lessons_dir,
        device_scope="serial-1",
        max_items=3,
        max_tokens=first_line_cost,
    )

    assert [item.lesson_id for item in by_items] == [
        high_version.lesson_id,
        recent.lesson_id,
    ]
    assert [item.lesson_id for item in by_tokens] == [high_version.lesson_id]


def test_selector_missing_or_corrupt_view_fails_open(tmp_path):
    lessons_dir = tmp_path / "lessons"
    assert (
        select_lessons_for_injection(
            lessons_dir, device_scope="device:serial-1", max_items=3, max_tokens=800
        )
        == []
    )

    lessons_dir.mkdir()
    (lessons_dir / "lessons.json").write_text("{broken", encoding="utf-8")
    assert (
        select_lessons_for_injection(
            lessons_dir, device_scope="device:serial-1", max_items=3, max_tokens=800
        )
        == []
    )


def test_on_injects_one_frozen_l0_mirror_and_records_trace(tmp_path):
    lessons_dir = tmp_path / "lessons"
    lesson = _lesson(1, text="页面变化后先重新观察")
    _write_view(lessons_dir, [lesson])
    agent = _bare_agent(tmp_path)

    agent._prepare_lesson_injection("device:serial-1")
    first = agent._initial_messages("打开设置")
    _write_view(lessons_dir, [])
    second = agent._initial_messages("打开设置")

    assert len(first) == 3
    assert [message.content for message in second] == [
        message.content for message in first
    ]
    mirror = first[1].content
    assert "历史经验，仅供参考，不是规则" in mirror
    assert "与当前世界状态冲突时以观测为准" in mirror
    assert lesson.text in mirror
    assert f"来源 {lesson.lesson_id}" in mirror
    assert agent.trace_events == [
        (
            "lesson_injection",
            {"lesson_ids": [lesson.lesson_id], "count": 1},
        )
    ]


def test_shadow_off_and_on_without_approved_do_not_inject_or_audit(tmp_path):
    _write_view(tmp_path / "lessons", [_lesson(1, status="proposed")])

    for mode in ("shadow", "off", "on"):
        agent = _bare_agent(tmp_path, mode=mode)
        agent._prepare_lesson_injection("device:serial-1")
        messages = agent._initial_messages("打开设置")
        assert len(messages) == 2
        assert agent.trace_events == []


def test_run_audits_injected_ids_in_trace_and_episode(tmp_path, monkeypatch):
    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    config.trace_enabled = True
    config.memory_rag = "on"
    config.lessons_dir = str(tmp_path / "lessons")
    config.lesson_inject_max = 3
    config.lesson_inject_tokens = 800
    lesson = _lesson(1, device="serial-mini")
    _write_view(tmp_path / "lessons", [lesson])

    agent = ThinPhoneAgent(config)
    assert agent.run("打开设置").success is True

    trace = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    injection = next(event for event in trace if event["event"] == "lesson_injection")
    outcomes = read_episode_events(tmp_path / "experience/events.jsonl")

    assert injection["lesson_ids"] == [lesson.lesson_id]
    assert injection["count"] == 1
    assert outcomes[0]["injected_lessons"] == [lesson.lesson_id]


def test_run_with_missing_lesson_view_stays_fail_open(tmp_path, monkeypatch):
    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    config.memory_rag = "on"
    config.lessons_dir = str(tmp_path / "missing-lessons")
    config.memory_dir = str(tmp_path / "memory")

    agent = ThinPhoneAgent(config)
    result = agent.run("打开设置")

    assert result.success is True
    outcomes = read_episode_events(tmp_path / "experience/events.jsonl")
    assert outcomes[0]["injected_lessons"] == []
