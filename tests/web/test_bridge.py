"""Fakes-only tests for the web event middleware and run bridge."""

from __future__ import annotations

import queue
import os
import sys
import threading
import time
import types
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from phone_agent.v2.config import V2Config
from phone_agent.v2.run_ipc import JsonlReader, JsonlWriter, read_run_spec
from phone_agent.web.bridge import WebEventMiddleware, WebRunBridge
from tests.v2.test_agent_loop import ScriptedToolModel


def _drain(events: queue.Queue) -> list[dict]:
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    return collected


def test_web_event_middleware_emits_scripted_model_tool_screen_and_taskdoc():
    events: queue.Queue[dict] = queue.Queue()
    middleware = WebEventMiddleware(events)
    model = ScriptedToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tap",
                        "args": {
                            "intent": "打开 WLAN",
                            "target_mark_id": "ax_1@e1",
                        },
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            )
        ]
    )

    middleware.before_model(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": "打开 WLAN\n[OBS] app=系统 设置 screen#1",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,QUJA"},
                            "screen_seq": 1,
                        },
                    ]
                ),
                SystemMessage(
                    content="[TASK_DOC]\n## 目标\nbase: 打开 WLAN\n## 路线\n- [in_progress] 1: 打开设置"
                ),
            ]
        },
        runtime=None,
    )
    middleware.wrap_model_call(
        SimpleNamespace(), handler=lambda request: model.invoke([])
    )
    request = SimpleNamespace(
        tool_call={
            "name": "tap",
            "args": {"intent": "打开 WLAN", "target_mark_id": "ax_1@e1"},
        }
    )
    result = ToolMessage(
        content=[
            {"type": "text", "text": "已点击 WLAN\n[OBS] app=设置 screen#2"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJD"},
                "screen_seq": 2,
            },
        ],
        tool_call_id="c1",
        name="tap",
    )
    assert middleware.wrap_tool_call(request, handler=lambda _: result) is result

    emitted = _drain(events)
    assert [event["event"] for event in emitted] == [
        "taskdoc_snapshot",
        "screen",
        "model_request",
        "model_call",
        "tool_call",
        "tool_result",
        "screen",
    ]
    assert emitted[1]["current_app"] == "系统 设置"
    assert emitted[1]["screen_seq"] == 1
    assert emitted[2]["event"] == "model_request"
    assert emitted[2]["phase"] == "start"
    assert emitted[2]["step"] == 1
    assert emitted[3]["step"] == 1
    assert emitted[3]["tokens_total"] == 15
    assert emitted[4]["args"]["intent"] == "打开 WLAN"
    assert emitted[5]["ok"] is True
    assert emitted[6] == {
        "event": "screen",
        "step": 1,
        "image": "data:image/png;base64,QUJD",
        "current_app": "设置",
        "screen_seq": 2,
        "reference": False,
        "screen_ref": None,
        "ts": emitted[6]["ts"],
    }


def test_web_event_middleware_marks_warning_as_failed_and_emits_warning():
    events: queue.Queue[dict] = queue.Queue()
    middleware = WebEventMiddleware(events)
    request = SimpleNamespace(tool_call={"name": "tap", "args": {"intent": "确认付款"}})
    warning = ToolMessage(
        content="⚠️ 已拦截（未执行）：tap\n带 confirm_irreversible=true 重试",
        tool_call_id="c1",
        name="tap",
        status="error",
    )

    middleware.wrap_tool_call(request, handler=lambda _: warning)
    emitted = _drain(events)

    assert emitted[1]["event"] == "tool_result"
    assert emitted[1]["ok"] is False
    assert emitted[2]["event"] == "safety_warning"


def test_web_event_middleware_reuses_experience_failure_classification(monkeypatch):
    from phone_agent.v2 import experience

    events: queue.Queue[dict] = queue.Queue()
    middleware = WebEventMiddleware(events)
    request = SimpleNamespace(tool_call={"name": "tap", "args": {}})
    receipt = ToolMessage(
        content="plain factual failure", tool_call_id="c1", name="tap"
    )
    monkeypatch.setattr(experience, "classify_tool_result", lambda *_args: "error")

    middleware.wrap_tool_call(request, handler=lambda _: receipt)
    emitted = _drain(events)

    assert emitted[1]["event"] == "tool_result"
    assert emitted[1]["ok"] is False


def test_thin_agent_appends_web_middleware_via_extension_point(monkeypatch, tmp_path):
    captured = {}

    def fake_create_agent(model, *, tools, middleware, checkpointer):  # noqa: ANN001
        captured["middleware"] = middleware
        return SimpleNamespace()

    monkeypatch.setattr("langchain.agents.create_agent", fake_create_agent)
    modules = {
        "phone_agent.v2.model": {"build_chat_model": lambda config, *args, **kwargs: SimpleNamespace()},
        "phone_agent.v2.session": {"PhoneSession": lambda config: SimpleNamespace()},
        "phone_agent.v2.tools": {"build_tools": lambda session, config: []},
        "phone_agent.v2.prompts": {"get_system_prompt": lambda lang="cn": "system"},
    }
    for name, attrs in modules.items():
        module = types.ModuleType(name)
        for attr, value in attrs.items():
            setattr(module, attr, value)
        monkeypatch.setitem(sys.modules, name, module)

    config = SimpleNamespace(
        trace_dir=str(tmp_path),
        trace_enabled=False,
        taskdoc_enabled=False,
        compact_enabled=False,
        diagnostic_evidence=False,
        safety_mode="off",
        lang="cn",
    )
    observer = WebEventMiddleware(queue.Queue())

    from phone_agent.v2.agent import ThinPhoneAgent

    ThinPhoneAgent(config, extra_middleware=[observer])

    from phone_agent.v2.agent import _ToolExecuteBridgeMiddleware

    assert captured["middleware"][-2] is observer
    assert isinstance(captured["middleware"][-1], _ToolExecuteBridgeMiddleware)


class _FakeProcess:
    def __init__(self, argv, producer):  # noqa: ANN001
        self.pid = os.getpid()
        self.returncode = None
        self._thread = threading.Thread(
            target=self._run, args=(read_run_spec(argv[-1]), producer), daemon=True
        )
        self._thread.start()

    def _run(self, spec, producer):  # noqa: ANN001
        producer(spec)
        self.returncode = 0

    def poll(self):
        return self.returncode


def _config_factory(tmp_path):
    return lambda _overrides: V2Config(
        base_url="http://example.invalid/v1",
        model_name="fake",
        runs_dir=str(tmp_path / "runs"),
    )


def _popen(producer):
    return lambda argv, **_kwargs: _FakeProcess(argv, producer)


def _run_end(spec, *, status="succeeded", reason="done", success=True, steps=1):
    with JsonlWriter(spec.events_path) as writer:
        writer.put(
            {
                "event": "run_end",
                "status": status,
                "result": {
                    "success": success,
                    "reason": reason,
                    "steps": steps,
                    "trace_path": None,
                },
                "tokens_total": 0,
            }
        )


def test_bridge_hitl_blocks_until_answer_and_terminal_result_lands(tmp_path):
    answers = []

    def producer(spec):
        with JsonlWriter(spec.events_path) as writer:
            writer.put({"event": "pending_hitl", "prompt": f"是否执行：{spec.task}"})
            reader = JsonlReader(spec.control_path)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                for message in reader.read_new():
                    if message.get("type") == "hitl":
                        answers.append(message["answer"])
                        writer.put({"event": "pending_hitl", "prompt": None})
                        writer.put(
                            {
                                "event": "run_end",
                                "status": "succeeded",
                                "result": {
                                    "success": True,
                                    "reason": "done",
                                    "steps": 1,
                                    "trace_path": None,
                                },
                                "tokens_total": 0,
                            }
                        )
                        return
                time.sleep(0.01)
        raise AssertionError("HITL answer was not received")

    bridge = WebRunBridge(
        config_factory=_config_factory(tmp_path), popen_factory=_popen(producer)
    )
    bridge.start("打开设置")

    deadline = time.monotonic() + 2
    while bridge.snapshot()["pending_hitl_prompt"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert bridge.wait(0.02) is False

    bridge.submit_hitl("approve")
    assert bridge.wait(2) is True
    state = bridge.snapshot()

    assert answers == ["approve"]
    assert state["status"] == "succeeded"
    assert state["pending_hitl_prompt"] is None
    assert state["final_result"] == {
        "success": True,
        "reason": "done",
        "steps": 1,
        "trace_path": None,
    }


def test_bridge_maps_budget_exhaustion_to_terminal_status(tmp_path):
    bridge = WebRunBridge(
        config_factory=_config_factory(tmp_path),
        popen_factory=_popen(
            lambda spec: _run_end(
                spec,
                status="budget_exhausted",
                reason="token_budget_exhausted",
                success=False,
                steps=4,
            )
        ),
    )
    bridge.start("长任务")
    assert bridge.wait(2) is True
    assert bridge.snapshot()["status"] == "budget_exhausted"


def test_bridge_maps_takeover_to_terminal_status(tmp_path):
    bridge = WebRunBridge(
        config_factory=_config_factory(tmp_path),
        popen_factory=_popen(
            lambda spec: _run_end(
                spec,
                status="takeover",
                reason="请人工完成登录",
                success=False,
                steps=2,
            )
        ),
    )
    bridge.start("登录")
    assert bridge.wait(2) is True
    assert bridge.snapshot()["status"] == "takeover"


def test_bridge_surfaces_background_error_as_terminal_result(tmp_path):
    bridge = WebRunBridge(
        config_factory=_config_factory(tmp_path),
        popen_factory=_popen(
            lambda spec: _run_end(
                spec,
                status="error",
                reason="error: RuntimeError: boom",
                success=False,
            )
        ),
    )
    bridge.start("触发错误")
    assert bridge.wait(2) is True
    state = bridge.snapshot()
    assert state["status"] == "error"
    assert state["final_result"]["success"] is False
    assert state["final_result"]["reason"] == "error: RuntimeError: boom"
