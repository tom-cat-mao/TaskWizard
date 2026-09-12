from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Queue
import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from phone_agent.v2.capabilities import CapabilitySpec
from phone_agent.v2.config import V2Config
from phone_agent.v2.events import OBSERVE, TOOL_EXECUTE
from phone_agent.v2.run_events import WebEventMiddleware
from phone_agent.v2.run_ipc import JsonlReader, read_run_spec
from phone_agent.v2.runner import run_spec
from phone_agent.web.bridge import WebRunBridge


class _ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    i: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return "contract-integration"


@dataclass
class _Observation:
    screenshot_b64: str = "QUJD"
    current_app: str = "com.example"
    screen_seq: int = 1
    marks: dict = field(default_factory=dict)
    mime_type: str = "image/png"


@dataclass
class _Session:
    config: Any = None
    finished: bool = False
    finish_summary: str | None = None
    takeover_reason: str | None = None
    finish_reviewed: bool = False
    finish_review_seq: int = -1
    finish_dispute_count: int = 0
    finish_hard_doubts: list[str] = field(default_factory=list)
    last_tool_ok: bool | None = None
    screen_seq: int = 0
    marks: dict = field(default_factory=dict)
    homes: int = 0

    def observe(self):
        self.screen_seq += 1
        return _Observation(screen_seq=self.screen_seq, marks=self.marks)


def _config(tmp_path: Path, **overrides: Any) -> V2Config:
    values = dict(
        base_url="http://example.invalid/v1",
        model_name="fake",
        api_key="test-key",
        memory_dir=str(tmp_path / "memory"),
        runs_dir=str(tmp_path / "runs"),
        trace_dir=str(tmp_path / "traces"),
        trace_enabled=True,
        experience_enabled=False,
        memory_rag="off",
        app_kb_enabled=False,
        taskdoc_enabled=False,
        compact_enabled=False,
        deliverable_enabled=False,
        safety_mode="off",
        finish_verify="off",
    )
    values.update(overrides)
    return V2Config(**values)


def _install_session_and_tools(monkeypatch, session: _Session, build_tools):
    session_module = types.ModuleType("phone_agent.v2.session")
    session_module.PhoneSession = lambda config: session
    tools_module = types.ModuleType("phone_agent.v2.tools")
    tools_module.build_base_tools = build_tools
    monkeypatch.setitem(sys.modules, "phone_agent.v2.session", session_module)
    monkeypatch.setitem(sys.modules, "phone_agent.v2.tools", tools_module)


def test_runner_plugin_supplies_actor_once_and_preserves_runtime_budget(
    tmp_path, monkeypatch
):
    from phone_agent.v2 import plugins
    from phone_agent.v2.providers import unregister_api_builder

    api = "integration-runner-transport"
    imports = tmp_path / "imports.txt"
    applies = tmp_path / "applies.txt"
    plugin_dir = tmp_path / "provider-plugin"
    plugin_dir.mkdir()
    plugin_source = f'''
from pathlib import Path
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from phone_agent.v2.capabilities import CapabilitySpec
from phone_agent.v2.providers import register_api_builder

imports = Path({str(imports)!r})
applies = Path({str(applies)!r})
imports.write_text(imports.read_text() + "i" if imports.exists() else "i")

class Model(BaseChatModel):
    @property
    def _llm_type(self):
        return "local-plugin-model"
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])
    def bind_tools(self, tools, **kwargs):
        return self

def apply(ctx):
    applies.write_text(applies.read_text() + "a" if applies.exists() else "a")
    register_api_builder({api!r}, lambda provider, model, config, **kwargs: Model())

CAPABILITY = CapabilitySpec(
    "runner_provider", "Runner Provider", "on",
    deps=("providers",), apply=apply,
)
'''
    (plugin_dir / "plugin.py").write_text(plugin_source, encoding="utf-8")
    manifest = tmp_path / "profile.toml"
    plugins.write_manifest(
        manifest, [plugins.PluginEntry(name="runner-provider", path=str(plugin_dir))]
    )
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "api": api,
                        "models": [{"id": "actor"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _config(
        tmp_path,
        model_name="local:actor",
        models_file=str(models),
        plugin_manifest=str(manifest),
    )

    class PendingProcess:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    captured: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        captured["spec"] = read_run_spec(argv[-1])
        return PendingProcess()

    monkeypatch.setattr(WebRunBridge, "_start_tail_thread", lambda _self: None)
    bridge = WebRunBridge(
        config_factory=lambda _overrides: config, popen_factory=fake_popen
    )
    idle_cap_ids = {
        row["cap_id"] for row in bridge.snapshot()["capabilities"]
    }
    assert "runner_provider" not in idle_cap_ids
    bridge.start("test")
    assert not imports.exists()

    session = _Session()
    _install_session_and_tools(monkeypatch, session, lambda _session, _config: [])
    built: dict[str, Any] = {}

    def actual_agent_factory(config, **kwargs):
        from phone_agent.v2.agent import ThinPhoneAgent

        agent = ThinPhoneAgent(config, **kwargs)
        built["agent"] = agent
        return agent

    try:
        assert run_spec(
            captured["spec"], agent_factory=actual_agent_factory, poll_seconds=0.01
        ) == 0
        agent = built["agent"]
        assert agent.model._llm_type == "local-plugin-model"
        assert agent._budget is not None
        assert imports.read_text(encoding="utf-8") == "i"
        assert applies.read_text(encoding="utf-8") == "a"
        events = JsonlReader(Path(captured["spec"].events_path)).read_new()
        snapshot = next(
            event for event in events if event["event"] == "capability_snapshot"
        )
        assert snapshot["capabilities"]["runner_provider"]["state"] == "active"
    finally:
        unregister_api_builder(api)


def test_web_pairs_s5_failures_and_never_marks_them_ok():
    from phone_agent.v2.agent import _ExecutionAdmissionListener

    sink = Queue()
    web = WebEventMiddleware(sink)
    stale_request = SimpleNamespace(
        tool_call={"id": "stale", "name": "tap", "args": {}}
    )
    stale = ToolMessage(
        content="stale mark: current batch changed",
        tool_call_id="stale",
        name="tap",
    )
    web.wrap_tool_call(stale_request, lambda _request: stale)

    session = _Session()
    admission = _ExecutionAdmissionListener(session)
    finish_request = SimpleNamespace(
        tool_call={"id": "finish", "name": "finish"}
    )

    def finish(_request):
        session.finished = True
        return ToolMessage(content="OK", tool_call_id="finish", name="finish")

    admission(finish_request, finish)
    skipped_request = SimpleNamespace(
        tool_call={"id": "skipped", "name": "home", "args": {}}
    )
    web.wrap_tool_call(
        skipped_request,
        lambda request: admission(request, lambda _request: "must not run"),
    )

    events = []
    while not sink.empty():
        events.append(sink.get_nowait())
    assert [event["event"] for event in events] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
    ]
    assert [event["ok"] for event in events if event["event"] == "tool_result"] == [
        False,
        False,
    ]


def test_runtime_plugin_listener_cannot_bypass_trace_or_terminal_fence(
    tmp_path, monkeypatch
):
    from phone_agent.v2.tools.control import build_control_tools

    session = _Session()
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "tap", "args": {}, "id": "tap", "type": "tool_call"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "finish",
                        "args": {"summary": "done", "evidence": ["proof"]},
                        "id": "review",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "finish",
                        "args": {
                            "summary": "done",
                            "evidence": ["proof"],
                            "confirm": True,
                        },
                        "id": "confirm",
                        "type": "tool_call",
                    },
                    {"name": "home", "args": {}, "id": "home", "type": "tool_call"},
                ],
            ),
            AIMessage(content="must not be sampled"),
        ]
    )
    model_module = types.ModuleType("phone_agent.v2.model")
    model_module.build_chat_model = lambda config, *args, **kwargs: model
    monkeypatch.setitem(sys.modules, "phone_agent.v2.model", model_module)

    def build_tools(current, config):
        @tool
        def tap() -> str:
            """Tap a fake target."""

            return "OK. tapped"

        @tool
        def home() -> str:
            """Press fake Home."""

            current.homes += 1
            return "OK. home"

        return [tap, home, *build_control_tools(current, config)]

    _install_session_and_tools(monkeypatch, session, build_tools)
    helper_seen: list[str] = []
    seen: list[str] = []

    def helper_apply(ctx):
        def listener(request, next):
            helper_seen.append(request.tool_call["name"])
            return next(request)

        ctx.on(TOOL_EXECUTE, listener)

    def apply(ctx):
        def listener(request, next):
            name = request.tool_call["name"]
            seen.append(name)
            if name in {"tap", "home"}:
                return ToolMessage(
                    content="plugin short-circuit",
                    tool_call_id=request.tool_call["id"],
                    name=name,
                )
            return next(request)

        ctx.on(TOOL_EXECUTE, listener, prepend=True)

    contributor = CapabilitySpec(
        "event_provider",
        "Event Provider",
        "on",
        deps=("event_helper",),
        apply=apply,
    )
    helper = CapabilitySpec(
        "event_helper", "Event Helper", "on", apply=helper_apply
    )
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(
        _config(
            tmp_path,
            finish_verify="auto",
            diagnostic_evidence=True,
            diagnostic_evidence_dir=str(tmp_path / "evidence"),
        ),
        extra_capabilities=[contributor, helper],
    )
    result = agent.run("finish")

    assert result.success is True
    assert session.homes == 0
    assert model.i == 3
    assert helper_seen == ["finish", "finish"]
    assert seen == ["tap", "finish", "finish"]
    trace = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    for name in ("tap", "home"):
        assert [
            event["event"]
            for event in trace
            if event.get("tool") == name
            and event.get("event") in {"tool_call", "tool_result"}
        ] == ["tool_call", "tool_result"]
    home_result = next(
        event
        for event in trace
        if event.get("tool") == "home" and event.get("event") == "tool_result"
    )
    assert "已终止" in str(home_result.get("result"))
    evidence = [
        json.loads(line)
        for line in Path(agent.evidence_path).read_text(encoding="utf-8").splitlines()
    ]
    assert [
        event["event"]
        for event in evidence
        if event.get("tool") == "home"
        and event.get("event") in {"tool_invoke", "tool_observation"}
    ] == ["tool_invoke", "tool_observation"]


def test_runtime_prepend_listener_cannot_bypass_control_hitl(tmp_path, monkeypatch):
    from phone_agent.v2.agent import ThinPhoneAgent
    from phone_agent.v2.tools.control import build_control_tools

    session = _Session()
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {"question": "Proceed?"},
                        "id": "question",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    model_module = types.ModuleType("phone_agent.v2.model")
    model_module.build_chat_model = lambda config, *args, **kwargs: model
    monkeypatch.setitem(sys.modules, "phone_agent.v2.model", model_module)
    _install_session_and_tools(
        monkeypatch,
        session,
        lambda current, config: build_control_tools(current, config),
    )
    plugin_seen: list[str] = []

    def apply(ctx):
        def listener(request, next):
            plugin_seen.append(request.tool_call["name"])
            return next(request)

        ctx.on(TOOL_EXECUTE, listener, prepend=True)

    contributor = CapabilitySpec(
        "runtime_hitl_observer", "Runtime HITL Observer", "on", apply=apply
    )
    prompts: list[str] = []

    agent = ThinPhoneAgent(
        _config(tmp_path), extra_capabilities=[contributor]
    )
    result = agent.run(
        "ask once", hitl_handler=lambda prompt: prompts.append(prompt) or "Yes"
    )

    assert result.reason == "model_stopped"
    assert len(prompts) == 1
    assert plugin_seen == []


def test_failed_actor_construction_releases_bootstrap_listener(
    tmp_path, monkeypatch
):
    session = _Session()
    _install_session_and_tools(monkeypatch, session, lambda _session, _config: [])
    model_module = types.ModuleType("phone_agent.v2.model")
    model_module.build_chat_model = lambda *args, **kwargs: (_ for _ in ()).throw(
        ValueError("ORIGINAL ACTOR CONFIG ERROR")
    )
    monkeypatch.setitem(sys.modules, "phone_agent.v2.model", model_module)
    captured: dict[str, Any] = {}
    seen: list[str] = []

    def apply(ctx):
        captured["ctx"] = ctx
        ctx.on_dispose(
            lambda: (_ for _ in ()).throw(
                RuntimeError("SECONDARY CLEANUP ERROR")
            )
        )

    def helper_apply(ctx):
        captured["ctx"] = ctx
        ctx.on(OBSERVE, lambda _payload: seen.append("leaked"))
        ctx.on_dispose(lambda: seen.append("disposed"))

    helper = CapabilitySpec(
        "actor_failure_helper",
        "Actor Failure Helper",
        "on",
        apply=helper_apply,
    )

    contributor = CapabilitySpec(
        "failing_actor_provider",
        "Failing Actor Provider",
        "on",
        deps=("providers", "actor_failure_helper"),
        apply=apply,
    )
    from phone_agent.v2.agent import ThinPhoneAgent

    import pytest

    with pytest.raises(ValueError, match="ORIGINAL ACTOR CONFIG ERROR"):
        ThinPhoneAgent(
            _config(tmp_path), extra_capabilities=[contributor, helper]
        )

    ctx = captured["ctx"]
    ctx.service("event_bus").emit(OBSERVE, {})
    assert seen == ["disposed"]
    assert ctx._mounted == {}


def test_failed_provider_apply_releases_prior_helper_listener(tmp_path, monkeypatch):
    session = _Session()
    _install_session_and_tools(monkeypatch, session, lambda _session, _config: [])
    captured: dict[str, Any] = {}
    seen: list[str] = []

    def helper_apply(ctx):
        captured["ctx"] = ctx
        ctx.on(OBSERVE, lambda _payload: seen.append("leaked"))

    def provider_apply(ctx):
        raise RuntimeError("provider apply failed")

    helper = CapabilitySpec(
        "failing_apply_helper",
        "Failing Apply Helper",
        "on",
        apply=helper_apply,
    )
    contributor = CapabilitySpec(
        "failing_apply_provider",
        "Failing Apply Provider",
        "on",
        deps=("providers", "failing_apply_helper"),
        apply=provider_apply,
    )
    from phone_agent.v2.agent import ThinPhoneAgent

    import pytest

    with pytest.raises(RuntimeError, match="provider apply failed"):
        ThinPhoneAgent(
            _config(tmp_path), extra_capabilities=[contributor, helper]
        )

    ctx = captured["ctx"]
    ctx.service("event_bus").emit(OBSERVE, {})
    assert seen == []
