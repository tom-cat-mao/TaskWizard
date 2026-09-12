"""WP-I1 experience-plane schema, lifecycle, and dream tests."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from phone_agent.v2.experience import (
    EPISODE_OUTCOME_FIELDS,
    EXPERIENCE_EVENT_FIELDS,
    ExperienceWriter,
    classify_tool_result,
    load_episodes,
)


def _outcome(run_id: str, *, ts_end: float = 100.0, success: bool = True):
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": ts_end - 10,
        "ts_end": ts_end,
        "time_of_day": "morning",
        "day_of_week": 2,
        "device_scope": "device:serial-1",
        "goal_text": "打开设置",
        "apps": ["com.android.settings"],
        "success": success,
        "reason": "finished" if success else "model_stopped",
        "steps": 3,
        "tokens_total": 42,
        "tokens_by_role": {"actor": 40, "verifier": 2},
        "warnings": 1,
        "takeover": None,
        "verifier": "pass",
    }


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_schema_roundtrip_and_exact_shared_contract(tmp_path):
    writer = ExperienceWriter(tmp_path)
    saved = writer.append_outcome({**_outcome("run-1"), "forbidden": "drop me"})
    event = writer.append_event(
        run_id="run-1",
        step=2,
        ts=99.0,
        tool="launch_app",
        result_class="ok",
        app_package="com.android.settings",
        device_scope="device:serial-1",
        args={"text": "never persist"},
    )

    assert tuple(saved) == EPISODE_OUTCOME_FIELDS
    assert tuple(event) == EXPERIENCE_EVENT_FIELDS
    assert load_episodes(tmp_path) == {"run-1": saved}
    assert _lines(tmp_path / "events.jsonl") == [saved, event]


def test_corrupt_materialized_view_rebuilds_from_append_only_log(tmp_path):
    writer = ExperienceWriter(tmp_path)
    saved = writer.append_outcome(_outcome("run-rebuild"))
    writer.episodes_path.write_text("{broken", encoding="utf-8")

    assert load_episodes(tmp_path) == {"run-rebuild": saved}
    assert json.loads(writer.episodes_path.read_text(encoding="utf-8")) == {
        "run-rebuild": saved
    }


def test_goal_stored_verbatim_and_type_text_payload_never_persisted(tmp_path):
    writer = ExperienceWriter(tmp_path)
    writer.append_outcome(
        {
            **_outcome("private"),
            "goal_text": "发给 13800138000，验证码: 123456",
        }
    )
    writer.append_event(
        run_id="private",
        step=1,
        ts=10.0,
        tool="type_text",
        result_class="ok",
        app_package=None,
        device_scope="device:serial-1",
        text="secret-body-must-not-land",
        mark_text="password box must not land",
        screenshot="base64-must-not-land",
    )

    goal_text = load_episodes(tmp_path)["private"]["goal_text"]
    assert goal_text == "发给 13800138000，验证码: 123456"
    raw = writer.events_path.read_text(encoding="utf-8")
    assert "13800138000" in raw
    assert "123456" in raw
    assert "secret-body-must-not-land" not in raw
    assert "password box must not land" not in raw
    assert "base64-must-not-land" not in raw


def test_tool_event_persists_intent_and_note_capped_at_200_chars(tmp_path):
    writer = ExperienceWriter(tmp_path)
    saved = writer.append_event(
        run_id="run-intent",
        step=1,
        ts=1.0,
        tool="tap",
        result_class="ok",
        app_package=None,
        device_scope="device:serial-1",
        intent="点击\n搜索  按钮",
        note="记" * 260,
    )
    bare = writer.append_event(
        run_id="run-intent",
        step=2,
        ts=2.0,
        tool="back",
        result_class="ok",
        app_package=None,
        device_scope="device:serial-1",
        note="   ",
    )

    assert tuple(saved) == EXPERIENCE_EVENT_FIELDS
    assert saved["intent"] == "点击 搜索 按钮"
    assert saved["note"] == "记" * 200
    assert bare["intent"] is None
    assert bare["note"] is None


def test_custom_registered_tool_keeps_bounded_identifier_without_payload(tmp_path):
    from langchain_core.messages import ToolMessage
    from phone_agent.v2.capabilities import CapabilityAssemblyContext
    from phone_agent.v2.middleware.trace import TraceMiddleware

    @tool("plugin_lookup-v2")
    def plugin_lookup(value: str) -> str:
        """Return a synthetic plugin lookup result."""

        return value

    context = CapabilityAssemblyContext()
    with context.applying("plugin_lookup"):
        context.register_tool(plugin_lookup)
    assert [registered.name for registered in context.tools] == ["plugin_lookup-v2"]

    writer = ExperienceWriter(tmp_path)
    middleware = TraceMiddleware(
        "run-plugin", enabled=False, experience_writer=writer
    )
    middleware.on_tool_execute(
        types.SimpleNamespace(
            tool_call={
                "name": "plugin_lookup-v2",
                "args": {"secret": "must-not-land"},
                "id": "c-plugin",
            }
        ),
        lambda _request: ToolMessage(
            content="custom ok",
            tool_call_id="c-plugin",
            name="plugin_lookup-v2",
        ),
    )
    saved = _lines(writer.events_path)[0]
    invalid = writer.append_event(
        run_id="run-plugin",
        step=2,
        ts=2.0,
        tool="not a valid tool name",
        result_class="ok",
        app_package=None,
        device_scope="device:serial-1",
    )

    assert saved["tool"] == "plugin_lookup-v2"
    assert invalid["tool"] == "unknown"
    assert "must-not-land" not in writer.events_path.read_text(encoding="utf-8")


def test_custom_tool_identifier_is_bounded_to_wire_safe_shape(tmp_path):
    writer = ExperienceWriter(tmp_path)
    accepted = writer.append_event(
        run_id="run-bounds",
        tool="x" * 64,
        result_class="ok",
        device_scope="device:test",
    )
    rejected = writer.append_event(
        run_id="run-bounds",
        tool="x" * 65,
        result_class="ok",
        device_scope="device:test",
    )

    assert accepted["tool"] == "x" * 64
    assert rejected["tool"] == "unknown"


def test_known_in_band_failures_share_one_result_classifier():
    from langchain_core.messages import ToolMessage

    failures = [
        "error: finish requires non-empty evidence",
        "stale mark: 'ax_1@e1' is no longer on the current screen",
        "路线仍有未完成项：1。请先完成。",
        "验收未通过：未见成功页。",
        "验收器再次驳回（第 2 次）：未见成功页。",
    ]
    for text in failures:
        result = ToolMessage(content=text, tool_call_id="c1", name="finish")
        assert classify_tool_result(result) == "error"

    warning = ToolMessage(
        content="⚠️ 已拦截（未执行）：tap",
        tool_call_id="c2",
        name="tap",
        status="error",
    )
    assert classify_tool_result(warning) == "warned"


class _MiniConfig:
    lang = "cn"
    max_model_calls = 5
    max_hitl_resumes = 1
    trace_enabled = False
    app_kb_enabled = False
    taskdoc_enabled = False
    compact_enabled = False
    finish_verify = "off"
    safety_mode = "off"
    experience_enabled = True
    device_id = "serial-mini"
    vec_db = None


def _install_mini_agent_modules(monkeypatch, tmp_path, enabled: bool):
    from tests.v2._doubles import FakePhoneSession

    session = FakePhoneSession()

    @tool
    def finish(summary: str, evidence: list[str], intent: str = "") -> str:
        """Finish the mini run with evidence."""
        session.finished = True
        session.finish_summary = summary
        return "已记录完成声明"

    class Model:
        def __init__(self):
            self.responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "finish",
                            "args": {
                                "summary": "done",
                                "evidence": ["screen"],
                                "intent": "finish",
                            },
                            "id": "finish-1",
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                ),
                AIMessage(content="done"),
            ]

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages, config=None, **kwargs):
            return self.responses.pop(0)

    model = Model()
    modules = {
        "phone_agent.v2.model": ("build_chat_model", lambda config, *args, **kwargs: model),
        "phone_agent.v2.session": ("PhoneSession", lambda config: session),
        "phone_agent.v2.tools": ("build_tools", lambda session, config: [finish]),
        "phone_agent.v2.prompts": ("get_system_prompt", lambda lang="cn": "system"),
    }
    for name, (attribute, value) in modules.items():
        module = types.ModuleType(name)
        setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)

    config = _MiniConfig()
    config.experience_enabled = enabled
    config.experience_dir = str(tmp_path / "experience")
    config.trace_dir = str(tmp_path / "traces")
    return config


def test_run_end_writes_exactly_one_outcome_and_usage_ledger(tmp_path, monkeypatch):
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(_install_mini_agent_modules(monkeypatch, tmp_path, True))
    result = agent.run("联系 13800138000")
    events = _lines(tmp_path / "experience" / "events.jsonl")
    outcomes = [event for event in events if event["type"] == "episode_outcome"]

    assert result.success is True
    assert len(outcomes) == 1
    assert outcomes[0]["reason"] == "finished"
    assert outcomes[0]["tokens_total"] == agent.usage_ledger.total
    assert outcomes[0]["tokens_by_role"] == agent.usage_ledger.by_role()
    assert outcomes[0]["tokens_by_role"]["actor"] > 0
    assert outcomes[0]["goal_text"] == "联系 13800138000"
    assert agent.session.run_goal == "联系 13800138000"
    assert agent.session.task_doc is None


def test_experience_off_writes_nothing(tmp_path, monkeypatch):
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(_install_mini_agent_modules(monkeypatch, tmp_path, False))
    assert agent.run("do it").success is True
    assert not (tmp_path / "experience").exists()


def test_run_exception_still_writes_one_failed_outcome_and_reraises(
    tmp_path, monkeypatch
):
    import pytest

    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(_install_mini_agent_modules(monkeypatch, tmp_path, True))

    def fail(_payload, _config):
        raise RuntimeError("secret must not be persisted")

    agent.agent = types.SimpleNamespace(invoke=fail)
    with pytest.raises(RuntimeError, match="secret must not be persisted"):
        agent.run("do it")

    outcomes = [
        event
        for event in _lines(tmp_path / "experience" / "events.jsonl")
        if event["type"] == "episode_outcome"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["success"] is False
    assert outcomes[0]["reason"] == "error:RuntimeError"
    assert "secret must not be persisted" not in json.dumps(outcomes[0])


def test_dream_archives_over_keep_and_emits_category_aggregate(tmp_path, capsys):
    from phone_agent.v2.dream import maintain_experience

    writer = ExperienceWriter(tmp_path)
    writer.append_outcome(
        {**_outcome("old-success", ts_end=100.0), "goal_text": "查询机票"}
    )
    writer.append_outcome(
        {
            **_outcome("old-fail", ts_end=200.0, success=False),
            "goal_text": "预订酒店",
        }
    )
    writer.append_outcome({**_outcome("new", ts_end=300.0), "goal_text": "打开设置"})

    report = maintain_experience(
        str(tmp_path),
        keep=1,
        archive_days=10_000,
        now=datetime.fromtimestamp(400.0, tz=timezone.utc),
    )
    print(f"dream: {json.dumps({'experience': report}, ensure_ascii=False)}")

    assert report == {"library_size": 3, "archived": 2, "kept": 1}
    view = load_episodes(tmp_path)
    assert "new" in view
    assert "old-success" not in view and "old-fail" not in view
    aggregate = view["aggregate:travel"]
    assert aggregate["episodes"] == 2
    assert aggregate["successes"] == 1
    assert aggregate["success_rate"] == 0.5
    archived = _lines(tmp_path / "events.jsonl")[-1]
    assert archived["type"] == "episode_archived"
    assert set(archived["run_ids"]) == {"old-success", "old-fail"}
    assert "查询机票" not in json.dumps(archived, ensure_ascii=False)
    assert '"library_size": 3' in capsys.readouterr().out


def test_dream_archives_episode_past_age_threshold(tmp_path):
    from phone_agent.v2.dream import maintain_experience

    writer = ExperienceWriter(tmp_path)
    writer.append_outcome(_outcome("aged", ts_end=100.0))

    report = maintain_experience(
        str(tmp_path),
        keep=500,
        archive_days=90,
        now=datetime.fromtimestamp(100.0 + 91 * 86400, tz=timezone.utc),
    )

    assert report == {"library_size": 1, "archived": 1, "kept": 0}
    assert "aged" not in load_episodes(tmp_path)


def test_launch_success_records_resolved_package_for_event(tmp_path):
    from langchain_core.messages import ToolMessage
    from phone_agent.v2.middleware.trace import TraceMiddleware

    session = types.SimpleNamespace(launched_apps=[])
    writer = ExperienceWriter(tmp_path / "experience")
    middleware = TraceMiddleware(
        "run-launch",
        enabled=False,
        experience_writer=writer,
        session=session,
    )
    middleware.experience_device_scope = "device:serial-1"
    request = types.SimpleNamespace(
        tool_call={"name": "launch_app", "args": {"app_name": "设置"}, "id": "c1"}
    )

    def handler(_request):
        session.launched_apps.append("com.android.settings")
        return ToolMessage(content="OK", tool_call_id="c1", name="launch_app")

    middleware.on_tool_execute(request, handler)
    event = _lines(writer.events_path)[0]
    assert event == {
        "type": "experience_event",
        "schema_v": 1,
        "run_id": "run-launch",
        "step": 0,
        "ts": event["ts"],
        "tool": "launch_app",
        "result_class": "ok",
        "app_package": "com.android.settings",
        "device_scope": "device:serial-1",
        "intent": None,
        "note": None,
    }


def test_trace_records_tool_intent_and_note_from_call_args(tmp_path):
    from langchain_core.messages import ToolMessage
    from phone_agent.v2.middleware.trace import TraceMiddleware

    writer = ExperienceWriter(tmp_path / "experience")
    middleware = TraceMiddleware("run-intent", enabled=False, experience_writer=writer)
    request = types.SimpleNamespace(
        tool_call={
            "name": "tap",
            "args": {"intent": "打开搜索结果", "note": "列表已加载"},
            "id": "c3",
        }
    )

    middleware.on_tool_execute(
        request,
        lambda _request: ToolMessage(
            content="已点击「搜索」", tool_call_id="c3", name="tap"
        ),
    )

    event = _lines(writer.events_path)[0]
    assert event["intent"] == "打开搜索结果"
    assert event["note"] == "列表已加载"
    assert event["tool"] == "tap"


def test_warned_tool_result_is_classified_without_persisting_content(tmp_path):
    from langchain_core.messages import ToolMessage
    from phone_agent.v2.middleware.trace import TraceMiddleware

    writer = ExperienceWriter(tmp_path / "experience")
    middleware = TraceMiddleware("run-warning", enabled=False, experience_writer=writer)
    request = types.SimpleNamespace(
        tool_call={
            "name": "type_text",
            "args": {"text": "secret-body-must-not-land"},
            "id": "c2",
        }
    )
    warning = "⚠️ 已拦截（未执行）：type_text → 输入 secret-body-must-not-land"
    middleware.on_tool_execute(
        request,
        lambda _request: ToolMessage(
            content=warning, tool_call_id="c2", name="type_text", status="error"
        ),
    )

    raw = writer.events_path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert event["result_class"] == "warned"
    assert "secret-body-must-not-land" not in raw
