"""WP-WF4-B: cross-App copy guidance in the distill call-1 prompt.

Scope: the one-sentence prompt guidance asking the distiller to fan a
cross-App handoff lesson into one candidate per involved App (scope.app
carrying each package), and the regression that two same-text copies bound to
the two batch apps both survive the closed-world validation and land in the
store as distinct lesson ids.  No harness validation is added: the fanout is a
preference, and a single general entry is not a bug.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import AIMessage

from phone_agent.v2.evolution import (
    _build_distill_messages,
    _task_key,
    LessonStore,
    distill_lessons,
)

APP_A = "com.example.notes"
APP_B = "com.example.shop"
COPY_TEXT = "切走 App 前先把关键数据写进任务板再切换"
TASK_KEY = _task_key({"goal_text": "在笔记应用记录清单后到购物应用下单"})


def _episode(run_id: str, *, success: bool, ts: float) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_end": ts,
        "device_scope": "device:serial-1",
        "goal_text": "在笔记应用记录清单后到购物应用下单",
        "apps": [APP_A, APP_B],
        "success": success,
        "reason": "finished",
    }


def _write_episodes(path: Path, episodes: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in episodes),
        encoding="utf-8",
    )


class _ScriptedModel:
    """Return one response per call; a callable response sees the messages."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages):  # noqa: ANN001
        index = len(self.calls)
        self.calls.append(messages)
        if index >= len(self.responses):
            raise AssertionError("unexpected extra model call")
        response = self.responses[index]
        if callable(response):
            response = response(messages)
        return AIMessage(
            content=response,
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
            },
        )


def _grading_reply(grade: str, basis: str = "同批双 App 证据一致"):
    """Grade every candidate in the grading prompt with ``grade``."""

    def build(messages) -> str:  # noqa: ANN001
        payload = json.loads(str(messages[-1].content).split("\n", 1)[1])
        return json.dumps(
            {
                "grades": [
                    {"lesson_id": item["lesson_id"], "grade": grade, "basis": basis}
                    for item in payload["candidates"]
                ]
            },
            ensure_ascii=False,
        )

    return build


def _scope_copy(app: str | None) -> dict:
    """One slim rule candidate; only scope.app varies between the copies."""

    return {
        "text": COPY_TEXT,
        "kind": "rule",
        "evidence": [{"run_id": "run-0"}, {"run_id": "run-1"}],
        "scope": {"device": None, "app": app},
        "task_keys": [TASK_KEY],
    }


# --- prompt guidance --------------------------------------------------------


def test_distill_prompt_carries_cross_app_copy_guidance():
    # Single anchor-phrase smoke check; the fanout behaviour itself is covered
    # by test_cross_app_copies_both_land_with_distinct_lesson_ids below.
    system = _build_distill_messages([], {})[0].content

    assert "偏好不是闸门" in system


# --- fanout regression ------------------------------------------------------


def test_cross_app_copies_both_land_with_distinct_lesson_ids(tmp_path):
    """Same text, two scope.app copies: both survive and neither overwrites."""

    events_path = tmp_path / "experience/events.jsonl"
    _write_episodes(events_path, [_episode("run-0", success=False, ts=1),
                                  _episode("run-1", success=True, ts=2)])
    first_call = {
        "rules": [_scope_copy(APP_A), _scope_copy(APP_B)],
        "procedures": [],
    }
    model = _ScriptedModel(
        json.dumps(first_call, ensure_ascii=False), _grading_reply("auto_approved")
    )
    lessons_dir = tmp_path / "lessons"

    result = distill_lessons(events_path, lessons_dir, model=model)

    assert result.groups_rejected == 0
    assert len(result.proposed) == 2
    assert all(item.status == "auto_approved" for item in result.proposed)
    # Distinct identity: the id hash includes scope, so the two copies differ.
    ids = {item.lesson_id for item in result.proposed}
    assert len(ids) == 2
    assert {item.scope["app"] for item in result.proposed} == {APP_A, APP_B}
    assert all(item.text == COPY_TEXT for item in result.proposed)

    stored = {item.lesson_id: item for item in LessonStore(lessons_dir).lessons()}
    assert set(stored) == ids
    for lesson_id, lesson in stored.items():
        assert lesson.scope["app"] in {APP_A, APP_B}
        assert lesson.text == COPY_TEXT
