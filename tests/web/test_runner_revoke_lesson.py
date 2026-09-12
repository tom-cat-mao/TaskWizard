"""WP-C2 in-run emergency lesson revocation control channel."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.evolution import LessonCandidate, LessonStore
from phone_agent.v2.run_ipc import append_control
from phone_agent.v2.runner import ControlChannel


def _lesson() -> LessonCandidate:
    return LessonCandidate(
        lesson_id="les_0123456789ab",
        schema_v=1,
        version=1,
        status="proposed",
        text="重新观察后再操作",
        scope={"device": None, "app": None, "app_version": None},
        support_count=3,
        task_keys=["a", "b"],
        evidence=[
            {"run_id": "r1", "note": "ok"},
            {"run_id": "r2", "note": "ok"},
            {"run_id": "r3", "note": "ok"},
        ],
        conflicts=[],
        created_ts=1.0,
        source="distill",
    )


def _wait_until(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_control_revoke_updates_store_and_excludes_future_prompt(tmp_path):
    store = LessonStore(tmp_path / "lessons")
    store.approve(store.propose(_lesson()).lesson_id)
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(lessons_dir=str(tmp_path / "lessons"))
    agent._trace = SimpleNamespace(record_event=lambda *_args, **_kwargs: None)
    agent._revoked_lesson_ids = set()
    agent._run_injected_lessons = [store.get("les_0123456789ab")]
    agent._actually_injected_lesson_ids = []

    path = tmp_path / "control.jsonl"
    channel = ControlChannel(
        path,
        stop_callback=lambda: None,
        revoke_lesson_callback=agent.revoke_lesson,
        poll_seconds=0.01,
    )
    channel.start()
    try:
        append_control(
            path, {"type": "revoke_lesson", "lesson_id": "les_0123456789ab"}
        )
        _wait_until(lambda: "les_0123456789ab" in agent._revoked_lesson_ids)
    finally:
        channel.close()

    assert LessonStore(tmp_path / "lessons").get("les_0123456789ab").status == "revoked"
    assert agent._recall_prompt_block() is None


def test_unknown_id_and_corrupt_control_line_fail_open(tmp_path):
    lessons = tmp_path / "lessons"
    LessonStore(lessons)
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(lessons_dir=str(lessons))
    agent._trace = SimpleNamespace(record_event=lambda *_args, **_kwargs: None)
    agent._revoked_lesson_ids = set()
    path = tmp_path / "control.jsonl"
    path.write_text("{broken}\n", encoding="utf-8")
    calls = []
    channel = ControlChannel(
        path,
        stop_callback=lambda: None,
        revoke_lesson_callback=lambda lesson_id: calls.append(
            agent.revoke_lesson(lesson_id)
        ),
        poll_seconds=0.01,
    )
    channel.start()
    try:
        append_control(path, {"type": "revoke_lesson", "lesson_id": "unknown"})
        _wait_until(lambda: bool(calls))
    finally:
        channel.close()

    assert calls == [False]
    assert json.loads((lessons / "lessons.json").read_text(encoding="utf-8")) == []


def test_revoke_received_during_agent_startup_is_drained_after_attach(tmp_path):
    path = tmp_path / "control.jsonl"
    received = []
    channel = ControlChannel(path, stop_callback=lambda: None, poll_seconds=0.01)
    channel.start()
    try:
        append_control(path, {"type": "revoke_lesson", "lesson_id": "les_early"})
        _wait_until(lambda: bool(channel._pending_revocations))
        channel.set_revoke_lesson_callback(received.append)
    finally:
        channel.close()

    assert received == ["les_early"]
