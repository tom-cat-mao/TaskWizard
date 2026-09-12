"""A-package availability regressions: lenient models.json, recorded fallback.

Everything is synthetic: fake registries/transports/models, tmp_path models
files, no network, device, MLX, or real .env.  The fallback path is exercised
through the real ``create_agent`` assembly so ``bind_tools``, multimodal
messages, and the system/TaskDoc request shape are covered end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from phone_agent.v2.config import V2Config
from phone_agent.v2.model import (
    actual_role_ref,
    build_actor_fallback,
    build_chat_model,
    build_role_model,
    record_model_fallback,
)
from phone_agent.v2.providers import (
    build_provider_registry,
    load_raw_document,
    register_api_builder,
    unregister_api_builder,
)
from phone_agent.v2.providers.loader import ModelsFileError


class _MarkerModel(BaseChatModel):
    """Fake transport model with call accounting and scripted failure."""

    marker: str = "marker"
    fail_times: int = 0
    failure: str = "RuntimeError"
    calls: int = 0
    seen_messages: list = Field(default_factory=list)
    seen_kwargs: list = Field(default_factory=list)
    seen_tools: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        self.seen_tools.append(list(tools))
        return self

    @property
    def _llm_type(self) -> str:
        return "availability-marker"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.seen_messages.append(list(messages))
        self.seen_kwargs.append(dict(kwargs))
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.failure == "ValueError":
                raise ValueError("fallback down")
            raise RuntimeError("primary down")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.marker))]
        )


def _raising_builder(*args, **kwargs):
    raise RuntimeError("transport unavailable")


def _config(**overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="cfg-key",
        model_timeout=3.0,
        model_max_retries=2,
        memory_model=None,
        verifier_model=None,
        safety_reviewer_model=None,
        fallback_model=None,
        models_file=None,
        context_window=None,
        sampling=None,
        parallel_tool_calls=False,
        thinking="",
    )
    defaults.update(overrides)
    return V2Config(**defaults)


def _agent_config(tmp_path, **overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="cfg-key",
        trace_dir=str(tmp_path / "traces"),
        trace_enabled=True,
        safety_mode="off",
        compact_enabled=False,
        taskdoc_enabled=False,
        memory_rag="off",
        experience_enabled=False,
        deliverable_enabled=False,
    )
    defaults.update(overrides)
    return V2Config(**defaults)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def transports():
    registered: list[str] = []

    def add(api: str, builder) -> None:
        register_api_builder(api, builder)
        registered.append(api)

    yield add
    for api in registered:
        unregister_api_builder(api)


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _patch_agent_modules(monkeypatch, tools=None):
    import phone_agent.v2.session as session_module
    import phone_agent.v2.tools as tools_module

    session = SimpleNamespace(event_bus=None, usage_ledger=None)
    monkeypatch.setattr(session_module, "PhoneSession", lambda config: session)
    monkeypatch.setattr(
        tools_module, "build_base_tools", lambda session, config: list(tools or [])
    )
    return session


def _trace_events(agent) -> list[dict]:
    path = Path(agent.trace_path)
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _invoke(agent, message: HumanMessage) -> dict:
    return agent.agent.invoke(
        {"messages": [message]},
        {"configurable": {"thread_id": "fallback-thread"}},
    )


def _error_chain(exc: BaseException) -> str:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    parts: list[str] = []
    while current is not None and current not in seen:
        seen.append(current)
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


# --- lenient models.json assembly ------------------------------------------------


def test_broken_project_models_file_falls_back_to_env_gateway(isolated):
    (isolated / ".taskwizard.models.json").write_text("{broken", encoding="utf-8")
    cfg = _config()

    registry = build_provider_registry(cfg)

    assert registry.list_providers() == ("gateway",)
    warnings = list(registry.declaration_warnings)
    assert [w.scope for w in warnings] == ["file"]
    assert "malformed JSON" in warnings[0].error
    model = build_chat_model(cfg, role="actor", registry=registry)
    assert model.model_name == "main-model"
    assert model.openai_api_base == "http://localhost:8000/v1"


def test_bad_unused_provider_and_role_entries_do_not_block_default(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "badapi": {"api": "carrier-pigeon", "models": [{"id": "x"}]},
                "badmodel": {
                    "api": "openai-completions",
                    "baseUrl": "https://bad.example/v1",
                    "models": [{"id": 42}, {"id": "good"}],
                },
                "good": {
                    "api": "openai-completions",
                    "baseUrl": "https://good.example/v1",
                    "models": [{"id": "m"}],
                },
            },
            "roles": {"memory": {"model": "good:m"}, "boss": {"thinking": "low"}},
        },
    )
    cfg = _config()

    registry = build_provider_registry(cfg)

    assert set(registry.list_providers()) == {"gateway", "badmodel", "good"}
    assert registry.get("badmodel").model_ids() == ("good",)
    assert registry.roles["memory"].model == "good:m"
    scopes = {(w.scope, w.name) for w in registry.declaration_warnings}
    assert ("provider", "badapi") in scopes
    assert ("model", "badmodel") in scopes
    assert ("role", "boss") in scopes
    model = build_chat_model(cfg, role="actor", registry=registry)
    assert model.model_name == "main-model"


def test_missing_or_directory_explicit_models_file_warns_and_keeps_gateway(isolated):
    cfg = _config(models_file=str(isolated / "missing-models.json"))
    registry = build_provider_registry(cfg)
    assert registry.list_providers() == ("gateway",)
    assert any("does not exist" in w.error for w in registry.declaration_warnings)

    directory = isolated / "models-dir"
    directory.mkdir()
    cfg = _config(models_file=str(directory))
    registry = build_provider_registry(cfg)
    assert registry.list_providers() == ("gateway",)
    assert registry.declaration_warnings


def test_strict_loader_still_raises_for_explicit_validation(isolated):
    path = isolated / "models.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ModelsFileError, match="malformed JSON"):
        load_raw_document(path)


# --- build-time degradation ------------------------------------------------------


def test_role_build_failure_falls_back_to_main_model_and_records(isolated, transports):
    transports("a-fb-flaky-role", _raising_builder)
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "flaky": {"api": "a-fb-flaky-role", "models": [{"id": "fast"}]}
            }
        },
    )
    cfg = _config(memory_model="flaky:fast")

    model = build_role_model(cfg, role="memory")

    assert model.model_name == "main-model"
    records = list(cfg._model_fallbacks)
    assert len(records) == 1
    assert records[0]["stage"] == "build"
    assert records[0]["role"] == "memory"
    assert records[0]["requested"] == "flaky:fast"
    assert records[0]["actual"] == "main-model"
    assert records[0]["reason"] == "RuntimeError"


def test_fallback_build_ignores_primary_specific_role_sampling_and_thinking(
    isolated, transports
):
    seen: dict = {}
    built: list = []

    def probe(provider, model, config, *, sampling, headers, level, compat):
        seen["sampling"] = dict(sampling)
        seen["level"] = level
        fallback = _MarkerModel(marker="backup", fail_times=1)
        built.append(fallback)
        return fallback

    transports("a-fb-probe", probe)
    transports("a-fb-broken-primary", _raising_builder)
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "primary": {"api": "a-fb-broken-primary", "models": [{"id": "p1"}]},
                "backup": {
                    "api": "a-fb-probe",
                    "models": [{"id": "b1", "samplingParams": {"temperature": 0.3}}],
                },
            },
            "roles": {
                "actor": {"samplingParams": {"temperature": 0.1}, "thinking": "high"}
            },
        },
    )
    cfg = _config(model_name="primary:p1", fallback_model="backup:b1")

    model = build_chat_model(cfg, role="actor", registry=build_provider_registry(cfg))

    assert model.marker == "backup"
    assert seen["sampling"] == {"temperature": 0.3}
    assert seen["level"] == ""
    assert actual_role_ref(cfg, "actor") == "backup:b1"


def test_same_target_fallback_reference_is_ignored(isolated):
    cfg = _config(model_name="main-model", fallback_model="gateway:main-model")

    assert build_actor_fallback(cfg, registry=build_provider_registry(cfg)) is None


def test_build_degraded_actor_disables_same_target_invoke_fallback(isolated, transports):
    built: list = []

    def backup_builder(provider, model, config, **kwargs):
        fallback = _MarkerModel(marker="backup", fail_times=1)
        built.append(fallback)
        return fallback

    transports("a-fb-broken-build", _raising_builder)
    transports("a-fb-backup-build", backup_builder)
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "primary": {"api": "a-fb-broken-build", "models": [{"id": "p1"}]},
                "backup": {"api": "a-fb-backup-build", "models": [{"id": "b1"}]},
            }
        },
    )
    cfg = _config(model_name="primary:p1", fallback_model="backup:b1")
    registry = build_provider_registry(cfg)

    model = build_chat_model(cfg, role="actor", registry=registry)

    assert model.marker == "backup"
    actual = actual_role_ref(cfg, "actor")
    assert actual == "backup:b1"
    assert build_actor_fallback(cfg, registry=registry, primary_ref=actual) is None
    assert len(built) == 1


# --- invocation fallback through the real create_agent path ----------------------


def _fallback_agent(tmp_path, monkeypatch, transports, *, primary_failures,
                    fallback_marker="backup-ok", fallback_failures=0,
                    fallback_failure="RuntimeError", tools=None):
    primary = _MarkerModel(marker="primary", fail_times=primary_failures)
    fallback = _MarkerModel(
        marker=fallback_marker,
        fail_times=fallback_failures,
        failure=fallback_failure,
    )

    def primary_builder(provider, model, config, **kwargs):
        return primary

    def fallback_builder(provider, model, config, **kwargs):
        return fallback

    transports("a-fb-primary", primary_builder)
    transports("a-fb-backup", fallback_builder)
    _write(
        tmp_path / ".taskwizard.models.json",
        {
            "providers": {
                "primary": {"api": "a-fb-primary", "models": [{"id": "actor"}]},
                "backup": {"api": "a-fb-backup", "models": [{"id": "actor"}]},
            }
        },
    )
    cfg = _agent_config(
        tmp_path,
        model_name="primary:actor",
        fallback_model="backup:actor",
    )
    monkeypatch.chdir(tmp_path)
    _patch_agent_modules(monkeypatch, tools=tools)
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(cfg, run_id="fallback-run")
    return agent, primary, fallback


def test_actor_invoke_failure_uses_fallback_once_in_real_agent(
    tmp_path, monkeypatch, transports
):
    agent, primary, fallback = _fallback_agent(
        tmp_path, monkeypatch, transports, primary_failures=1
    )

    result = _invoke(agent, HumanMessage(content="hello"))

    assert result["messages"][-1].content == "backup-ok"
    assert primary.calls == 1
    assert fallback.calls == 1
    events = _trace_events(agent)
    fallbacks = [event for event in events if event["event"] == "model_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["stage"] == "invoke"
    assert fallbacks[0]["role"] == "actor"
    assert fallbacks[0]["requested"] == "primary:actor"
    assert fallbacks[0]["actual"] == "backup:actor"
    assert fallbacks[0]["reason"] == "RuntimeError"
    assert fallbacks[0]["outcome"] == "ok"
    assert len([event for event in events if event["event"] == "model_call"]) == 1


def test_fallback_preserves_tool_binding_and_multimodal_message(
    tmp_path, monkeypatch, transports
):
    calls: list[str] = []

    @tool
    def ping(value: str) -> str:
        """Ping the harness."""
        calls.append(value)
        return "pong"

    agent, primary, fallback = _fallback_agent(
        tmp_path, monkeypatch, transports, primary_failures=1, tools=[ping]
    )
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]
    )

    result = _invoke(agent, message)

    assert result["messages"][-1].content == "backup-ok"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert fallback.seen_tools
    assert any(getattr(item, "name", None) == "ping" for item in fallback.seen_tools[0])
    assert primary.seen_messages[0] == fallback.seen_messages[0]
    blocks = fallback.seen_messages[0][-1].content
    assert isinstance(blocks, list)
    assert any(block.get("type") == "image_url" for block in blocks)
    assert calls == []


def test_primary_and_fallback_failure_raises_without_loop(
    tmp_path, monkeypatch, transports
):
    agent, primary, fallback = _fallback_agent(
        tmp_path,
        monkeypatch,
        transports,
        primary_failures=1,
        fallback_failures=1,
        fallback_failure="ValueError",
    )

    with pytest.raises(Exception) as excinfo:
        _invoke(agent, HumanMessage(content="hello"))

    chain = _error_chain(excinfo.value)
    assert "primary down" in chain
    assert "fallback down" in chain
    assert primary.calls == 1
    assert fallback.calls == 1
    fallbacks = [
        event
        for event in _trace_events(agent)
        if event["event"] == "model_fallback"
    ]
    assert len(fallbacks) == 1
    assert fallbacks[0]["outcome"] == "failed"
    assert fallbacks[0]["reason"] == "ValueError"


def test_default_success_path_makes_no_extra_model_call(
    tmp_path, monkeypatch, transports
):
    agent, primary, fallback = _fallback_agent(
        tmp_path, monkeypatch, transports, primary_failures=0
    )

    result = _invoke(agent, HumanMessage(content="hello"))

    assert result["messages"][-1].content == "primary"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert not [
        event
        for event in _trace_events(agent)
        if event["event"] == "model_fallback"
    ]


def test_build_degradation_reaches_trace_and_agent_has_no_second_attempt(
    tmp_path, monkeypatch, transports
):
    built: list = []

    def dead_builder(provider, model, config, **kwargs):
        dead = _MarkerModel(marker="backup", fail_times=1)
        built.append(dead)
        return dead

    transports("a-fb-dead-primary", _raising_builder)
    transports("a-fb-dead-backup", dead_builder)
    _write(
        tmp_path / ".taskwizard.models.json",
        {
            "providers": {
                "primary": {"api": "a-fb-dead-primary", "models": [{"id": "actor"}]},
                "backup": {"api": "a-fb-dead-backup", "models": [{"id": "actor"}]},
            }
        },
    )
    cfg = _agent_config(
        tmp_path, model_name="primary:actor", fallback_model="backup:actor"
    )
    monkeypatch.chdir(tmp_path)
    _patch_agent_modules(monkeypatch)
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(cfg, run_id="degraded-run")

    assert agent._actor_fallback is None
    with pytest.raises(Exception) as excinfo:
        _invoke(agent, HumanMessage(content="hello"))
    assert "primary down" in _error_chain(excinfo.value)
    assert built[0].calls == 1
    events = _trace_events(agent)
    builds = [
        event
        for event in events
        if event["event"] == "model_fallback" and event["stage"] == "build"
    ]
    assert len(builds) == 1
    assert builds[0]["requested"] == "primary:actor"
    assert builds[0]["actual"] == "backup:actor"
    assert not [
        event
        for event in events
        if event["event"] == "model_fallback" and event["stage"] == "invoke"
    ]


def test_agent_flushes_loader_warnings_into_trace(tmp_path, monkeypatch):
    (tmp_path / ".taskwizard.models.json").write_text("{broken", encoding="utf-8")
    cfg = _agent_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _patch_agent_modules(monkeypatch)
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(cfg, run_id="lenient-run")

    warnings = [
        event
        for event in _trace_events(agent)
        if event["event"] == "models_declaration_warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["scope"] == "file"
    assert "malformed JSON" in warnings[0]["error"]
    assert agent.model.model_name == "main-model"


# --- targeted availability regressions ---------------------------------------


def test_new_provider_without_api_is_warned_and_other_declarations_survive(isolated):
    path = isolated / ".taskwizard.models.json"
    _write(
        path,
        {
            "providers": {
                "unused": {"models": [{"id": "x"}]},
                "good": {
                    "api": "openai-completions",
                    "baseUrl": "https://good.example/v1",
                    "models": [{"id": "m"}],
                },
            }
        },
    )
    cfg = _config()

    registry = build_provider_registry(cfg)

    assert set(registry.list_providers()) == {"gateway", "good"}
    assert registry.get("unused") is None
    warnings = [w for w in registry.declaration_warnings if w.scope == "provider"]
    assert [(w.name, w.error.split(":", 1)[0]) for w in warnings] == [
        ("unused", "ValueError")
    ]
    assert "requires an api" in warnings[0].error
    model = build_chat_model(cfg, role="actor", registry=registry)
    assert model.model_name == "main-model"

    strict_providers, _ = load_raw_document(path)
    empty = build_provider_registry(_config(), load_declarations=False)
    with pytest.raises(ValueError, match="requires an api"):
        empty.override(strict_providers["unused"])


def test_undecodable_models_file_degrades_to_gateway_without_byte_leak(isolated):
    (isolated / ".taskwizard.models.json").write_bytes(b"\xff\xfebroken")
    cfg = _config()

    registry = build_provider_registry(cfg)

    assert registry.list_providers() == ("gateway",)
    warnings = list(registry.declaration_warnings)
    assert [w.scope for w in warnings] == ["file"]
    assert "cannot decode" in warnings[0].error
    assert "broken" not in warnings[0].error
    assert "xff" not in warnings[0].error
    model = build_chat_model(cfg, role="actor", registry=registry)
    assert model.model_name == "main-model"


def test_models_file_permission_faults_warn_and_keep_gateway(isolated, monkeypatch):
    path = isolated / ".taskwizard.models.json"
    _write(
        path,
        {"providers": {"good": {"api": "openai-completions", "models": [{"id": "m"}]}}},
    )
    real_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", denied_stat)
        registry = build_provider_registry(_config())

    assert registry.list_providers() == ("gateway",)
    assert any("cannot stat" in w.error for w in registry.declaration_warnings)

    real_read = Path.read_text

    def denied_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_read(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", denied_read)
        registry = build_provider_registry(_config())

    assert registry.list_providers() == ("gateway",)
    assert any("cannot read" in w.error for w in registry.declaration_warnings)


def test_build_fallback_keeps_compact_and_pruner_on_backup_window(
    tmp_path, monkeypatch, transports
):
    def backup_builder(provider, model, config, **kwargs):
        return _MarkerModel(marker="backup")

    transports("a-fb-compact-backup", backup_builder)
    _write(
        tmp_path / ".taskwizard.models.json",
        {
            "providers": {
                "backup": {
                    "api": "a-fb-compact-backup",
                    "models": [{"id": "backup-model", "contextWindow": 123_456}],
                }
            }
        },
    )
    cfg = _agent_config(
        tmp_path,
        model_name="missing:actor",
        fallback_model="backup:backup-model",
        compact_enabled=True,
    )
    monkeypatch.chdir(tmp_path)
    _patch_agent_modules(monkeypatch)
    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(cfg, run_id="compact-fallback-run")

    assert agent.model.marker == "backup"
    assert actual_role_ref(cfg, "actor") == "backup:backup-model"
    assert agent._actor_fallback is None
    assert agent._compact is not None
    assert agent._compact._pruner is not None
    assert agent._compact.window == 123_456
    messages = [HumanMessage(content="hello")]
    assert agent._compact.on_pre_request(messages, lambda items: items) == messages

    explicit_cfg = _agent_config(
        tmp_path,
        model_name="missing:actor",
        fallback_model="backup:backup-model",
        compact_enabled=True,
        context_window=32_000,
    )
    explicit_agent = ThinPhoneAgent(explicit_cfg, run_id="compact-explicit-run")

    assert explicit_agent._compact.window == 32_000


def test_compact_actor_ref_tolerates_stubbed_model_module(monkeypatch):
    import sys
    import types

    cfg = _config(model_name="stub-model")
    registry = build_provider_registry(cfg)
    stub = types.ModuleType("phone_agent.v2.model")
    stub.build_chat_model = lambda config, *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "phone_agent.v2.model", stub)
    cfg._provider_registry = registry

    from phone_agent.v2.middleware.compact import CompactMiddleware

    compact = CompactMiddleware(SimpleNamespace(), cfg)

    assert compact.window == 256_000


def test_record_model_fallback_creates_missing_stash():
    cfg = SimpleNamespace()

    record_model_fallback(
        cfg,
        stage="build",
        role="actor",
        requested="primary:actor",
        actual="backup:actor",
        reason="RuntimeError",
    )

    assert cfg._model_fallbacks == [
        {
            "stage": "build",
            "role": "actor",
            "requested": "primary:actor",
            "actual": "backup:actor",
            "reason": "RuntimeError",
            "outcome": "ok",
        }
    ]
