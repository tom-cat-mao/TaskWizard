"""Round-3 G2: the ``run_context`` service carries a run's identity to plugins.

A capability or plugin used to have no way to learn *which* run it was serving:
``run_id``, the goal text and the actor model were only reachable through
private agent factories (compact's memory-state provider, deliverable's tool
factory).  The harness now publishes them as one harness-owned service, so the
ordinary service plane is enough.

These tests pin the lifecycle a consumer must handle, because "absent" and
"present but not started yet" are both reachable states:

* a context with no run at all (the CLI maintenance path) has no service;
* a run-assembling harness publishes it with the assembly context itself, so
  even a plugin mounted by the provider bootstrap pass reads a real ``run_id``;
  ``actor_model`` is filled once the actor model is built, and ``goal`` stays
  ``""`` until ``run()`` starts;
* ``run()`` fills the goal before any run hook fires.

The agent-level tests build a real ``ThinPhoneAgent`` with duck-typed modules, so
"the harness publishes it" is asserted against the real assembly rather than a
hand-built context.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from phone_agent.v2.capabilities import (
    RUN_CONTEXT_SERVICE,
    CapabilityAssemblyContext,
    CapabilitySpec,
    RunContext,
)
from tests.v2.doubles.models import ScriptedModel


# ---------------------------------------------------------------------------
# the object and its lifecycle
# ---------------------------------------------------------------------------
def test_a_fresh_run_context_has_no_goal_and_has_not_started() -> None:
    context = RunContext("run-1", actor_model="local:actor")

    assert context.run_id == "run-1"
    assert context.actor_model == "local:actor"
    assert context.goal == ""
    assert context.started is False


def test_require_goal_fails_visible_before_the_run_starts() -> None:
    context = RunContext("run-1")

    with pytest.raises(RuntimeError, match="run_context.goal is not set"):
        context.require_goal()


def test_the_goal_arrives_when_the_run_starts() -> None:
    context = RunContext("run-1")

    context._begin("打开设置")  # noqa: SLF001 - harness-side start signal

    assert context.started is True
    assert context.goal == "打开设置"
    assert context.require_goal() == "打开设置"


def test_a_reused_context_carries_the_latest_goal() -> None:
    context = RunContext("run-1")

    context._begin("第一个任务")  # noqa: SLF001
    context._begin("第二个任务")  # noqa: SLF001

    assert context.require_goal() == "第二个任务"


def test_a_bare_assembly_context_publishes_no_run_context() -> None:
    """The CLI maintenance context has no run: absent, not a fabricated identity."""

    assert CapabilityAssemblyContext().service(RUN_CONTEXT_SERVICE) is None


def test_a_capability_cannot_replace_the_run_context_service() -> None:
    ctx = CapabilityAssemblyContext()
    ctx.set_service(RUN_CONTEXT_SERVICE, RunContext("run-1"))
    spec = CapabilitySpec(
        "squatter",
        "Squatter",
        "on",
        apply=lambda context: context.register_service(
            RUN_CONTEXT_SERVICE, RunContext("run-2")
        ),
    )

    from phone_agent.v2.capabilities import CapabilityRegistry, assemble_capabilities

    registry = CapabilityRegistry()
    registry.register(spec)
    with pytest.raises(ValueError, match="harness-owned"):
        assemble_capabilities(registry, ctx)


# ---------------------------------------------------------------------------
# the real agent: assembly publishes it, run() completes it
# ---------------------------------------------------------------------------
def _agent_config(tmp_path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "lang": "cn",
        "model_name": "local:actor",
        "base_url": "http://example.invalid/v1",
        "api_key": "test-key",
        "max_model_calls": 20,
        "max_hitl_resumes": 5,
        "trace_dir": str(tmp_path),
        "trace_enabled": True,
        "safety_mode": "off",
        "compact_enabled": False,
        "taskdoc_enabled": False,
        "app_kb_enabled": False,
        "deliverable_enabled": False,
        "experience_enabled": False,
        "finish_verify": "auto",
        "memory_rag": "off",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_agent_modules(monkeypatch, session, model) -> None:
    """Swap the model/session/tools/prompts seams the agent imports lazily.

    The real modules stay in place (their attributes are patched) so
    ``phone_agent.v2.tools._obs`` and friends remain importable.
    """

    from phone_agent.v2 import model as model_module
    from phone_agent.v2 import prompts as prompts_module
    from phone_agent.v2 import session as session_module
    from phone_agent.v2 import tools as tools_module

    monkeypatch.setattr(model_module, "build_chat_model", lambda config, *a, **kw: model)
    monkeypatch.setattr(session_module, "PhoneSession", lambda config: session)
    monkeypatch.setattr(tools_module, "build_base_tools", lambda sess, config: [])
    monkeypatch.setattr(tools_module, "build_tools", lambda sess, config: [])
    monkeypatch.setattr(prompts_module, "get_system_prompt", lambda lang="cn": "system")
    monkeypatch.setattr(
        prompts_module, "get_deliverable_prompt", lambda lang="cn": "deliverable"
    )


class _AgentSession:
    """Minimal duck-typed session: one observation, no device."""

    def __init__(self) -> None:
        self.config = None
        self.marks: dict = {}
        self.screen_seq = 0
        self.finished = False
        self.finish_summary: str | None = None
        self.takeover_reason: str | None = None
        self.task_doc = None
        self.event_bus = None

    def observe(self):  # noqa: ANN201 - duck-typed observation
        self.screen_seq += 1
        return SimpleNamespace(
            screenshot_b64="QUJD",
            current_app="com.example",
            screen_seq=self.screen_seq,
            marks={},
            mime_type="image/png",
        )


def _build_agent(tmp_path, monkeypatch, *, extra_capabilities=(), task="打开设置"):
    from phone_agent.v2.agent import ThinPhoneAgent

    session = _AgentSession()
    model = ScriptedModel(responses=[AIMessage(content="任务结束")])
    _install_agent_modules(monkeypatch, session, model)
    agent = ThinPhoneAgent(
        _agent_config(tmp_path),
        extra_capabilities=list(extra_capabilities),
    )
    return agent, session


def test_the_harness_publishes_run_context_at_assembly(tmp_path, monkeypatch) -> None:
    agent, _session = _build_agent(tmp_path, monkeypatch)

    context = agent._capability_ctx.service(RUN_CONTEXT_SERVICE)  # noqa: SLF001

    assert isinstance(context, RunContext)
    assert context.run_id == agent.run_id
    assert context.actor_model == "local:actor"
    # Assembly is not a run: the goal is not known yet, and saying so is the
    # point of ``started``.
    assert context.started is False
    assert context.goal == ""


def test_an_unresolved_actor_ref_is_published_as_empty(tmp_path, monkeypatch) -> None:
    """No resolved actor ref is an honest ``""``, never a fabricated name."""

    from phone_agent.v2.agent import ThinPhoneAgent

    session = _AgentSession()
    model = ScriptedModel(responses=[AIMessage(content="任务结束")])
    _install_agent_modules(monkeypatch, session, model)
    config = _agent_config(tmp_path)
    del config.model_name
    agent = ThinPhoneAgent(config)

    context = agent._capability_ctx.service(RUN_CONTEXT_SERVICE)  # noqa: SLF001

    assert context.actor_model == ""


def test_a_provider_bootstrap_plugin_reads_the_run_identity(
    tmp_path, monkeypatch
) -> None:
    """The service exists before the provider bootstrap pass, not just after it.

    A plugin that depends on ``providers`` is mounted by the bootstrap pass —
    which runs *before* the actor model is built — so publishing the service
    only in the main assembly would hand that plugin ``None`` for a run that
    exists.
    """

    seen: dict[str, Any] = {}

    def apply(context: CapabilityAssemblyContext) -> None:
        plugin_run = context.service(RUN_CONTEXT_SERVICE)
        seen["at_apply"] = (plugin_run.run_id, plugin_run.actor_model)
        context.add_run_hook(
            "start", lambda _state: seen.__setitem__("at_run", plugin_run.require_goal())
        )

    agent, _session = _build_agent(
        tmp_path,
        monkeypatch,
        extra_capabilities=[
            CapabilitySpec(
                "bootstrap_probe",
                "Bootstrap Probe",
                "on",
                deps=("providers",),
                apply=apply,
            )
        ],
    )

    # run_id is real at bootstrap time; the actor model does not exist yet, and
    # the same handle is completed once it does.
    assert seen["at_apply"] == (agent.run_id, "")
    context = agent._capability_ctx.service(RUN_CONTEXT_SERVICE)  # noqa: SLF001
    assert context.actor_model == "local:actor"

    agent.run("把亮度调到一半")

    assert seen["at_run"] == "把亮度调到一半"


def test_a_plugin_reads_run_identity_from_the_service_plane(tmp_path, monkeypatch) -> None:
    """No private factory: the plugin's apply hook sees the real run identity."""

    seen: dict[str, Any] = {}

    def apply(context: CapabilityAssemblyContext) -> None:
        plugin_run = context.service(RUN_CONTEXT_SERVICE)
        seen["at_apply"] = (
            plugin_run.run_id,
            plugin_run.goal,
            plugin_run.started,
        )

        def record_goal(_state: dict[str, Any]) -> None:
            seen["at_run_start"] = plugin_run.require_goal()

        context.add_run_hook("start", record_goal)

    agent, _session = _build_agent(
        tmp_path,
        monkeypatch,
        extra_capabilities=[
            CapabilitySpec("probe", "Probe", "on", apply=apply),
        ],
    )

    agent.run("把亮度调到一半")

    assert seen["at_apply"] == (agent.run_id, "", False)
    assert seen["at_run_start"] == "把亮度调到一半"


def test_a_second_run_updates_the_goal_before_the_run_hooks(tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    def apply(context: CapabilityAssemblyContext) -> None:
        plugin_run = context.service(RUN_CONTEXT_SERVICE)
        context.add_run_hook("start", lambda _state: seen.append(plugin_run.goal))

    agent, _session = _build_agent(
        tmp_path,
        monkeypatch,
        extra_capabilities=[CapabilitySpec("probe", "Probe", "on", apply=apply)],
    )

    agent.run("第一个任务")
    agent.run("第二个任务")

    assert seen == ["第一个任务", "第二个任务"]


def test_the_cli_maintenance_context_publishes_no_run_context(
    tmp_path, monkeypatch
) -> None:
    """Both run paths publish through ``ThinPhoneAgent``; a maintenance command
    has no run and therefore no identity to publish."""

    import main_v2

    config = SimpleNamespace(
        plugins_enabled=False,
        plugin_manifest=str(tmp_path / ".taskwizard.toml"),
        plugin_index=str(tmp_path / "index.json"),
        app_kb_enabled=False,
        dream_mode="manual",
        memory_dir=str(tmp_path / "memory"),
    )
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)

    context = main_v2._build_cli_capability_context(config)

    assert context.service(RUN_CONTEXT_SERVICE) is None
