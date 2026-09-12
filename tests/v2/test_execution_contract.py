from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from phone_agent.device_factory import DeviceFactory
from phone_agent.v2.tools.actuation import build_actuation_tools
from phone_agent.v2.tools.control import build_control_tools
from tests.v2._doubles import FakeDeviceFactory, FakePhoneSession, make_mark


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    i: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-execution-contract"


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _turn(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def _remote_shell_argv(command: list[str]) -> list[str]:
    """Re-tokenize an ``adb shell`` command the way the device-side shell does."""
    return shlex.split(" ".join(command[command.index("shell") + 1 :]))


def _broadcast_payload(command: list[str]) -> str:
    """Return the single ``--es msg`` value as the device-side shell receives it."""
    argv = _remote_shell_argv(command)
    index = argv.index("--es")
    assert argv[index + 1] == "msg"
    value_tokens = argv[index + 2 :]
    assert len(value_tokens) == 1, f"expected one msg value, got {value_tokens!r}"
    return value_tokens[0]


@dataclass
class _Observation:
    screenshot_b64: str = "QUJD"
    width: int = 1080
    height: int = 2400
    current_app: str = "com.example"
    screen_seq: int = 0
    marks: dict = field(default_factory=dict)
    mime_type: str = "image/png"


@dataclass
class _Session:
    config: Any = None
    marks: dict = field(default_factory=dict)
    screen_seq: int = 0
    epoch: int = 1
    finished: bool = False
    finish_summary: str | None = None
    takeover_reason: str | None = None
    finish_reviewed: bool = False
    finish_review_seq: int = -1
    finish_dispute_count: int = 0
    finish_hard_doubts: list[str] = field(default_factory=list)
    last_tool_ok: bool | None = None
    homes: int = 0
    taps: int = 0
    order: list[str] = field(default_factory=list)
    task_doc: Any = None

    def observe(self) -> _Observation:
        self.screen_seq += 1
        return _Observation(screen_seq=self.screen_seq, marks=self.marks)


def _install_agent_modules(monkeypatch, session, model, tools_builder) -> None:
    model_mod = types.ModuleType("phone_agent.v2.model")
    model_mod.build_chat_model = lambda config, *args, **kwargs: model
    session_mod = types.ModuleType("phone_agent.v2.session")
    session_mod.PhoneSession = lambda config: session
    tools_mod = types.ModuleType("phone_agent.v2.tools")
    tools_mod.build_tools = tools_builder
    prompts_mod = types.ModuleType("phone_agent.v2.prompts")
    prompts_mod.get_system_prompt = lambda lang="cn": "system"
    for name, module in (
        ("phone_agent.v2.model", model_mod),
        ("phone_agent.v2.session", session_mod),
        ("phone_agent.v2.tools", tools_mod),
        ("phone_agent.v2.prompts", prompts_mod),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def _config(tmp_path):
    return SimpleNamespace(
        lang="cn",
        max_model_calls=20,
        max_hitl_resumes=5,
        trace_dir=str(tmp_path),
        trace_enabled=True,
        safety_mode="off",
        compact_enabled=False,
        taskdoc_enabled=False,
        app_kb_enabled=False,
        deliverable_enabled=False,
        experience_enabled=False,
        finish_verify="auto",
        memory_rag="off",
    )


def _build_agent(tmp_path, monkeypatch, session, model, tools_builder):
    _install_agent_modules(monkeypatch, session, model, tools_builder)
    from phone_agent.v2.agent import ThinPhoneAgent

    return ThinPhoneAgent(_config(tmp_path))


def test_device_dispatch_failure_is_an_in_band_unknown_outcome() -> None:
    class _FailedDevice(FakeDeviceFactory):
        def tap(self, x, y, device_id=None, delay=None):
            raise RuntimeError("adb failed with secret coords 100,200")

    session = FakePhoneSession(
        {"ax_1": make_mark("ax_1", text="按钮")},
        device_factory=_FailedDevice(),
    )
    tap = {tool.name: tool for tool in build_actuation_tools(session, session.config)}[
        "tap"
    ]

    result = tap.invoke({"target_mark_id": "ax_1"})

    assert isinstance(result, str)
    assert result.startswith("error:")
    assert "无法确认" in result
    assert "OK" not in result
    assert "100,200" not in result


def test_action_success_with_observation_failure_keeps_action_receipt() -> None:
    device = FakeDeviceFactory()
    session = FakePhoneSession(
        {"ax_1": make_mark("ax_1", text="按钮")},
        device_factory=device,
    )
    session._observe_should_fail = True
    tap = {tool.name: tool for tool in build_actuation_tools(session, session.config)}[
        "tap"
    ]

    result = tap.invoke({"target_mark_id": "ax_1"})

    assert result[0]["text"].startswith("OK. 已点击")
    assert "re-observation failed" in result[1]["text"]
    assert device.calls == [("tap", 540, 720)]


def test_text_success_with_restore_failure_does_not_invite_duplicate_input() -> None:
    class _RestoreFailedDevice(FakeDeviceFactory):
        def restore_keyboard(self, ime, device_id=None):
            self.calls.append(("restore_kbd", ime))
            raise RuntimeError("restore failed")

    device = _RestoreFailedDevice()
    session = FakePhoneSession({}, device_factory=device)
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "hello"})

    text = result[0]["text"]
    assert text.startswith("OK. 已输入")
    assert "输入已发送；键盘恢复失败" in text
    assert "请勿" not in text
    assert device.calls.count(("type_text", "hello")) == 1


def test_text_uncertain_dispatch_and_restore_failure_report_both() -> None:
    class _InputAndRestoreFailedDevice(FakeDeviceFactory):
        def type_text(self, text, device_id=None):
            self.calls.append(("type_text", text))
            raise RuntimeError("dispatch unknown")

        def restore_keyboard(self, ime, device_id=None):
            self.calls.append(("restore_kbd", ime))
            raise RuntimeError("restore failed")

    device = _InputAndRestoreFailedDevice()
    session = FakePhoneSession({}, device_factory=device)
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "secret"})

    assert isinstance(result, str)
    assert "可能已发送" in result
    assert "键盘恢复失败" in result
    assert "secret" not in result
    assert device.calls.count(("type_text", "secret")) == 1


@pytest.mark.parametrize(
    ("restore_result", "expected"),
    [
        (subprocess.CompletedProcess([], 0, stdout="", stderr=""), "原键盘已恢复"),
        (
            subprocess.CompletedProcess([], 1, stdout="", stderr="restore secret"),
            "键盘恢复命令失败，当前键盘状态未知",
        ),
    ],
)
def test_keyboard_warmup_failure_receipt_uses_real_helper_cleanup(
    monkeypatch, restore_result, expected
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, stdout="com.original/.IME\n", stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 1, stdout="", stderr="warmup secret"
            ),
            restore_result,
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = FakePhoneSession({}, device_factory=DeviceFactory())
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "user secret"})

    assert isinstance(result, str)
    assert "待输入文本尚未发送" in result
    assert expected in result
    assert "user secret" not in result
    assert "com.original" not in result
    assert "warmup secret" not in result
    broadcasts = [command for command in calls if "ADB_INPUT_B64" in command]
    assert len(broadcasts) == 1
    assert _broadcast_payload(broadcasts[0]) == ""
    assert calls[-1][-3:] == ["ime", "set", "com.original/.IME"]


@pytest.mark.parametrize(
    ("results", "expected", "broadcasts", "restores"),
    [
        (
            [subprocess.CompletedProcess([], 1, stdout="", stderr="query secret")],
            "未执行需要恢复的输入法切换",
            0,
            0,
        ),
        (
            [
                subprocess.CompletedProcess(
                    [], 0, stdout="com.original/.IME\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="switch secret"
                ),
            ],
            "输入法切换结果无法确认",
            0,
            0,
        ),
        (
            [
                subprocess.CompletedProcess(
                    [], 0, stdout="com.android.adbkeyboard/.AdbIME\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="warmup secret"
                ),
            ],
            "未执行需要恢复的输入法切换",
            1,
            0,
        ),
        (
            [
                subprocess.CompletedProcess([], 0, stdout="\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="warmup secret"
                ),
            ],
            "键盘恢复结果无法确认",
            1,
            0,
        ),
    ],
)
def test_keyboard_preparation_receipt_is_factual_without_user_dispatch(
    monkeypatch, results, expected, broadcasts, restores
) -> None:
    scripted = iter(results)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(scripted)

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = FakePhoneSession({}, device_factory=DeviceFactory())
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "user secret"})

    assert isinstance(result, str)
    assert "待输入文本尚未发送" in result
    assert expected in result
    assert "user secret" not in result
    b64_commands = [command for command in calls if "ADB_INPUT_B64" in command]
    assert len(b64_commands) == broadcasts
    assert all(_broadcast_payload(command) == "" for command in b64_commands)
    assert sum(
        command[-3:] == ["ime", "set", "com.original/.IME"]
        for command in calls
    ) == restores


def test_keyboard_success_through_tool_sends_user_payload_once(monkeypatch) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, stdout="com.original/.IME\n", stderr=""
            ),
            *[subprocess.CompletedProcess([], 0, stdout="", stderr="")] * 4,
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = FakePhoneSession({}, device_factory=DeviceFactory())
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "user payload"})

    encoded = base64.b64encode(b"user payload").decode("utf-8")
    assert isinstance(result, list)
    assert result[0]["text"].startswith("OK. 已输入")
    broadcasts = [command for command in calls if "ADB_INPUT_B64" in command]
    assert [_broadcast_payload(command) for command in broadcasts] == ["", encoded]
    assert sum(
        command[-3:] == ["ime", "set", "com.original/.IME"]
        for command in calls
    ) == 1


def test_keyboard_warmup_empty_payload_and_text_cross_remote_shell_once(
    monkeypatch,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, stdout="com.original/.IME\n", stderr=""
            ),
            *[subprocess.CompletedProcess([], 0, stdout="", stderr="")] * 4,
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = FakePhoneSession({}, device_factory=DeviceFactory())
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "沈阳旅游"})

    assert isinstance(result, list)
    assert result[0]["text"].startswith("OK. 已输入")
    encoded = base64.b64encode("沈阳旅游".encode("utf-8")).decode("utf-8")
    broadcasts = [command for command in calls if "ADB_INPUT_B64" in command]
    assert [_broadcast_payload(command) for command in broadcasts] == ["", encoded]
    assert (
        sum(
            command[-3:] == ["ime", "set", "com.android.adbkeyboard/.AdbIME"]
            for command in calls
        )
        == 1
    )
    assert (
        sum(
            command[-3:] == ["ime", "set", "com.original/.IME"] for command in calls
        )
        == 1
    )


def test_text_payload_failure_after_empty_warmup_reports_once_without_duplicate(
    monkeypatch,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(
                [], 0, stdout="com.original/.IME\n", stderr=""
            ),
            *[subprocess.CompletedProcess([], 0, stdout="", stderr="")] * 2,
            subprocess.CompletedProcess([], 1, stdout="", stderr="send secret"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return next(results)

    monkeypatch.setattr(subprocess, "run", fake_run)
    session = FakePhoneSession({}, device_factory=DeviceFactory())
    type_text = {
        tool.name: tool for tool in build_actuation_tools(session, session.config)
    }["type_text"]

    result = type_text.invoke({"text": "user secret"})

    assert isinstance(result, str)
    assert "可能已发送" in result
    assert "user secret" not in result
    assert "send secret" not in result
    encoded = base64.b64encode("user secret".encode("utf-8")).decode("utf-8")
    broadcasts = [command for command in calls if "ADB_INPUT_B64" in command]
    assert [_broadcast_payload(command) for command in broadcasts] == ["", encoded]
    assert (
        sum(
            command[-3:] == ["ime", "set", "com.original/.IME"] for command in calls
        )
        == 1
    )


def test_finish_confirm_stops_later_call_and_next_model_turn(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call(
                    "finish",
                    {"summary": "done", "evidence": ["proof"]},
                    "review",
                )
            ),
            _turn(
                _call(
                    "finish",
                    {
                        "summary": "done",
                        "evidence": ["proof"],
                        "confirm": True,
                    },
                    "confirm",
                ),
                _call("home", {}, "home-after-finish"),
            ),
            AIMessage(content="must not be sampled"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [*build_control_tools(sess, config), home]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    result = agent.run("finish task")

    assert result.success is True
    assert session.homes == 0
    assert model.i == 2
    events = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    skipped = [
        event
        for event in events
        if event.get("event") == "tool_result"
        and event.get("tool") == "home"
    ]
    assert len(skipped) == 1
    assert "已终止" in str(skipped[0].get("result"))


def test_terminal_admission_skip_is_an_experience_error() -> None:
    from phone_agent.v2.agent import _ExecutionAdmissionListener
    from phone_agent.v2.experience import classify_tool_result

    session = _Session()
    listener = _ExecutionAdmissionListener(session)
    finish_request = SimpleNamespace(
        tool_call={"id": "finish", "name": "finish"}
    )

    def finish(_request):
        session.finished = True
        return ToolMessage(
            content="OK. finished", tool_call_id="finish", name="finish"
        )

    listener(finish_request, finish)

    later_request = SimpleNamespace(
        tool_call={"id": "home-after-finish", "name": "home"}
    )

    def must_not_run(_request):
        raise AssertionError("terminal sibling executed")

    skipped = listener(later_request, must_not_run)

    assert skipped.status == "error"
    assert classify_tool_result(skipped) == "error"


def test_finish_review_is_not_terminal_and_later_call_runs(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call(
                    "finish",
                    {"summary": "done", "evidence": ["proof"]},
                    "review",
                ),
                _call("home", {}, "home-after-review"),
            ),
            AIMessage(content="continue"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [*build_control_tools(sess, config), home]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    result = agent.run("review task")

    assert result.success is False
    assert session.finished is False
    assert session.homes == 1
    assert model.i == 2


def test_rejected_finish_confirm_is_not_terminal(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call(
                    "finish",
                    {"summary": "done", "evidence": ["proof"]},
                    "review",
                )
            ),
            _turn(
                _call(
                    "finish",
                    {
                        "summary": "done",
                        "evidence": ["proof"],
                        "confirm": True,
                    },
                    "rejected-confirm",
                ),
                _call("home", {}, "home-after-reject"),
            ),
            AIMessage(content="continue"),
        ]
    )
    monkeypatch.setattr(
        "phone_agent.v2.tools.control._maybe_verify_finish",
        lambda sess, config: SimpleNamespace(approve=False, reason="not proven"),
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [*build_control_tools(sess, config), home]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    result = agent.run("verify task")

    assert result.success is False
    assert session.finished is False
    assert session.homes == 1
    assert model.i == 3


def test_accepted_takeover_stops_later_call_and_model(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call("take_over", {"reason": "captcha"}, "takeover"),
                _call("home", {}, "home-after-takeover"),
            ),
            AIMessage(content="must not be sampled"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [*build_control_tools(sess, config), home]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    prompts: list[str] = []

    def approve(prompt: str) -> str:
        prompts.append(prompt)
        return "approve"

    result = agent.run("captcha task", hitl_handler=approve)

    assert result.success is False
    assert result.reason == "captcha"
    assert len(prompts) == 1
    assert session.homes == 0
    assert model.i == 1
    events = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    for tool_name in ("take_over", "home"):
        calls = [
            event
            for event in events
            if event.get("event") == "tool_call"
            and event.get("tool") == tool_name
        ]
        results = [
            event
            for event in events
            if event.get("event") == "tool_result"
            and event.get("tool") == tool_name
        ]
        assert len(calls) == len(results)
        assert calls


def test_rejected_takeover_keeps_later_call_and_model(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call("take_over", {"reason": "captcha"}, "takeover"),
                _call("home", {}, "home-after-reject"),
            ),
            AIMessage(content="continue"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [*build_control_tools(sess, config), home]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    result = agent.run("captcha task", hitl_handler=lambda prompt: "reject")

    assert result.success is False
    assert result.reason == "model_stopped"
    assert session.takeover_reason is None
    assert session.homes == 1
    assert model.i == 2


def test_same_reply_executes_in_declared_order_and_uses_fresh_marks(
    tmp_path, monkeypatch
) -> None:
    session = _Session(marks={"ax@e1": object()})
    model = _ScriptedModel(
        responses=[
            _turn(
                _call("read_screen", {}, "read"),
                _call("tap", {"target_mark_id": "ax@e1"}, "tap"),
            ),
            AIMessage(content="done"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def read_screen() -> str:
            """Refresh the screen."""
            time.sleep(0.05)
            sess.order.append("read_screen")
            sess.epoch = 2
            sess.marks = {"ax@e2": object()}
            return "[OBS] fresh"

        @tool
        def tap(target_mark_id: str) -> str:
            """Tap a current mark."""
            sess.order.append("tap")
            if not target_mark_id.endswith(f"@e{sess.epoch}"):
                return "stale mark: current batch changed"
            sess.taps += 1
            return "OK. tapped"

        return [read_screen, tap]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    agent.run("refresh then tap")

    assert session.order == ["read_screen", "tap"]
    assert session.taps == 0


def test_hitl_resume_does_not_repeat_an_earlier_action(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call("home", {}, "home-before-question"),
                _call("ask_user", {"question": "Proceed?"}, "question"),
            ),
            AIMessage(content="done"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def home() -> str:
            """Press Home."""
            sess.homes += 1
            return "OK. home"

        return [home, *build_control_tools(sess, config)]

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    agent.run("ask after acting", hitl_handler=lambda prompt: "Yes, Keep Case")

    assert session.homes == 1
    assert model.i == 2


def test_same_batch_ask_user_then_tap_resumes_once_without_duplicate_action(
    tmp_path, monkeypatch
) -> None:
    session = _Session()
    model = _ScriptedModel(
        responses=[
            _turn(
                _call("ask_user", {"question": "Proceed?"}, "question"),
                _call("tap", {}, "tap-after-answer"),
            ),
            AIMessage(content="done"),
        ]
    )

    def tools_builder(sess, config):
        @tool
        def tap() -> str:
            """Tap the selected control."""
            sess.taps += 1
            return "OK. tapped"

        return [*build_control_tools(sess, config), tap]

    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "Keep Mixed CASE"

    agent = _build_agent(tmp_path, monkeypatch, session, model, tools_builder)
    result = agent.run("ask then tap", hitl_handler=answer)

    assert result.reason == "model_stopped"
    assert prompts == ["Tool execution requires approval\n\nTool: ask_user\nArgs: {'question': 'Proceed?'}"]
    assert session.taps == 1
    assert model.i == 2


def test_respond_decision_preserves_human_text_verbatim() -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    interrupt = SimpleNamespace(
        value={
            "action_requests": [
                {"name": "ask_user", "description": "Answer"}
            ],
            "review_configs": [
                {"allowed_decisions": ["respond"]}
            ],
        }
    )

    decisions = ThinPhoneAgent._decisions_for(
        interrupt, lambda prompt: "  Keep Mixed CASE 原文  "
    )

    assert decisions == [
        {"type": "respond", "message": "  Keep Mixed CASE 原文  "}
    ]
