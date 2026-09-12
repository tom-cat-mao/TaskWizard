"""P2: models.json ``roles`` section — per-role session-level call config.

Covers: three-way field application (model / sampling / thinking), env beats
``roles.<role>.model`` at equal specificity, per-role thinking override over
the global level, fail-closed schema validation (unknown role name, illegal
thinking value), empty-string model treated as unwritten, single-level
models.json (user-level files no longer read), ``PHONE_AGENT_MODELS_FILE``
priority for providers AND roles, and zero-regression when no roles section
exists.
"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace

import pytest

from phone_agent.v2.config import V2Config
from phone_agent.v2.model import _build_legacy, build_role_model
from phone_agent.v2.providers import build_provider_registry, resolve_role_ref
from phone_agent.v2.providers.loader import (
    ModelsFileError,
    parse_models_document,
    parse_models_json,
)
from phone_agent.v2.providers.roles import get_role_specs
from phone_agent.v2.providers.types import RoleSpec

_CLIENT_FIELDS = {"client", "async_client", "root_client", "root_async_client"}


def _config(**overrides):
    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="main-model",
        api_key="cfg-key",
        memory_model=None,
        verifier_model=None,
        safety_reviewer_model=None,
        models_file=None,
        thinking="",
        sampling=None,
    )
    defaults.update(overrides)
    return V2Config(**defaults)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolated HOME + cwd (HOME isolation proves the user level is dead)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _config_dict(model):
    return {
        key: value
        for key, value in model.__dict__.items()
        if key not in _CLIENT_FIELDS
    }


# --- parsing & registry attach -------------------------------------------------


def test_roles_section_parsed_and_attached(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "m1"}],
                }
            },
            "roles": {
                "memory": {
                    "model": "acme:m1",
                    "samplingParams": {"temperature": 0.3},
                    "thinking": "medium",
                },
                "actor": {"samplingParams": {"temperature": 1}},
            },
        },
    )
    registry = build_provider_registry(_config())
    assert registry is not None
    assert registry.roles["memory"] == RoleSpec(
        model="acme:m1", sampling_params={"temperature": 0.3}, thinking="medium"
    )
    assert registry.roles["actor"] == RoleSpec(
        model=None, sampling_params={"temperature": 1}, thinking=None
    )


def test_no_roles_section_attaches_empty_dict(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {"providers": {"p": {"api": "openai-completions"}}},
    )
    registry = build_provider_registry(_config())
    assert registry.roles == {}
    assert get_role_specs(registry) == {}


def test_roles_only_file_is_valid(isolated):
    _write(isolated / ".taskwizard.models.json", {"roles": {"distill": {"thinking": "off"}}})
    registry = build_provider_registry(_config())
    assert registry.roles["distill"].thinking == "off"


def test_foreign_registry_get_role_specs_empty():
    from phone_agent.v2.providers.registry import ProviderRegistry

    assert get_role_specs(ProviderRegistry()) == {}
    assert get_role_specs(None) == {}


# --- roles.<role>.model tier ---------------------------------------------------


def test_roles_model_applies_when_env_unset(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "m1"}],
                }
            },
            "roles": {"memory": {"model": "acme:m1"}},
        },
    )
    cfg = _config()
    assert resolve_role_ref(cfg, "memory", registry=build_provider_registry(cfg)) == "acme:m1"
    model = build_role_model(cfg, role="memory")
    assert model.model_name == "m1"
    assert model.openai_api_base == "https://acme.example/v1"


def test_env_beats_roles_model(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "slow"}, {"id": "fast"}],
                }
            },
            "roles": {"memory": {"model": "acme:slow"}},
        },
    )
    cfg = _config(memory_model="acme:fast")  # role env tier wins
    registry = build_provider_registry(cfg)
    assert resolve_role_ref(cfg, "memory", registry=registry) == "acme:fast"
    model = build_role_model(cfg, role="memory", registry=registry)
    assert model.model_name == "fast"


def test_roles_model_beats_fallback_chain(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "file-model"}],
                }
            },
            "roles": {"distill": {"model": "acme:file-model"}},
        },
    )
    cfg = _config(memory_model="mem-model")  # chain head must lose to roles tier
    registry = build_provider_registry(cfg)
    assert resolve_role_ref(cfg, "distill", registry=registry) == "acme:file-model"
    # ...but roles entries only affect their own role.
    assert resolve_role_ref(cfg, "memory", registry=registry) == "mem-model"


def test_roles_model_empty_string_treated_as_unwritten():
    _, roles = parse_models_document({"roles": {"memory": {"model": "  "}}})
    assert roles["memory"].model is None
    cfg = _config(memory_model="mem-model")
    assert resolve_role_ref(cfg, "memory") == "mem-model"


def test_actor_env_model_beats_roles_actor_model(isolated):
    """PHONE_AGENT_MODEL (config.model_name) is the actor env tier: it wins."""
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "file-actor"}],
                }
            },
            "roles": {"actor": {"model": "acme:file-actor"}},
        },
    )
    cfg = _config()
    model = build_role_model(cfg, role="actor")
    assert model.model_name == "main-model"  # env tier always set -> wins


# --- roles.<role>.samplingParams tier ------------------------------------------


def test_roles_sampling_params_highest_tier(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [
                        {"id": "m1", "samplingParams": {"temperature": 0.1, "seed": 7}}
                    ],
                }
            },
            "roles": {"actor": {"samplingParams": {"temperature": 1}}},
        },
    )
    cfg = _config(
        model_name="acme:m1",  # actor env tier points at the acme model
        sampling={"top_p": 0.9},  # config env tier: middle
    )
    built = build_role_model(cfg, role="actor")
    assert built.model_name == "m1"
    assert built.temperature == 1  # roles > config > model entry
    assert built.top_p == 0.9
    assert built.seed == 7  # model-entry value untouched by higher tiers


def test_roles_sampling_params_only_affect_their_role(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "m1"}],
                }
            },
            "roles": {"memory": {"samplingParams": {"temperature": 0.3}}},
        },
    )
    cfg = _config(sampling={"temperature": 0.7})
    memory = build_role_model(cfg, role="memory")
    assert memory.temperature == 0.3
    verifier = build_role_model(cfg, role="verifier")
    assert verifier.temperature == 0.7  # untouched role keeps the env tier


# --- roles.<role>.thinking override --------------------------------------------


def test_roles_thinking_overrides_global_for_that_role_only(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "compat": {"thinkingFormat": "reasoning_effort"},
                    "models": [{"id": "m1"}],
                }
            },
            "roles": {
                "memory": {"model": "acme:m1", "thinking": "medium"},
                "verifier": {"model": "acme:m1"},  # no thinking override
            },
        },
    )
    cfg = _config(thinking="low", memory_model="acme:m1", verifier_model="acme:m1")
    memory = build_role_model(cfg, role="memory")
    assert memory.reasoning_effort == "medium"  # roles tier beats global env
    verifier = build_role_model(cfg, role="verifier")
    assert verifier.reasoning_effort == "low"  # global level untouched


def test_roles_thinking_off_disables_for_that_role(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "compat": {"thinkingFormat": "reasoning_effort"},
                    "models": [{"id": "m1"}],
                }
            },
            "roles": {"memory": {"model": "acme:m1", "thinking": "off"}},
        },
    )
    cfg = _config(thinking="high", memory_model="acme:m1")
    memory = build_role_model(cfg, role="memory")
    assert not memory.reasoning_effort  # "off" omits the param silently


# --- fail-closed schema ---------------------------------------------------------


def test_unknown_role_name_fails_closed():
    with pytest.raises(ModelsFileError, match="unknown role 'boss'"):
        parse_models_json({"roles": {"boss": {"thinking": "low"}}})


def test_illegal_thinking_value_fails_closed():
    with pytest.raises(ModelsFileError, match="thinking"):
        parse_models_json({"roles": {"memory": {"thinking": "maximum"}}})


def test_illegal_role_entry_shapes_fail_closed():
    with pytest.raises(ModelsFileError):
        parse_models_json({"roles": {"memory": {"model": 42}}})
    with pytest.raises(ModelsFileError):
        parse_models_json({"roles": {"memory": {"samplingParams": [1]}}})
    with pytest.raises(ModelsFileError):
        parse_models_json({"roles": {"memory": "nope"}})
    with pytest.raises(ModelsFileError):
        parse_models_json({"roles": ["memory"]})


def test_roles_schema_failure_degrades_with_warning(isolated):
    """An invalid role declaration is skipped and recorded, not fatal."""
    _write(
        isolated / ".taskwizard.models.json",
        {"roles": {"not-a-role": {"thinking": "low"}}},
    )
    registry = build_provider_registry(_config())
    assert registry.roles == {}
    warnings = registry.declaration_warnings
    assert len(warnings) == 1
    assert warnings[0].scope == "role"
    assert "unknown role 'not-a-role'" in warnings[0].error


# --- single-level models.json ----------------------------------------------------


def test_user_level_files_are_not_read(isolated):
    """Both legacy user-level locations are dead; roles there never apply."""
    _write(
        isolated / "home" / ".config" / "taskwizard" / "models.json",
        {
            "providers": {
                "useronly": {
                    "api": "openai-completions",
                    "baseUrl": "https://user.example/v1",
                    "apiKey": "user-key",
                    "models": [{"id": "user-model"}],
                }
            },
            "roles": {
                "memory": {"model": "useronly:user-model", "thinking": "high"}
            },
        },
    )
    registry = build_provider_registry(_config())
    assert registry is not None
    assert registry.list_providers() == ("gateway",)
    assert registry.roles == {}
    cfg = _config()
    model = build_role_model(cfg, role="memory", registry=registry)
    assert model.model_name == "main-model"  # user roles.model never surfaced
    assert model.openai_api_base == "http://localhost:8000/v1"


def test_env_file_priority_for_providers_and_roles(isolated):
    project = isolated / ".taskwizard.models.json"
    _write(
        project,
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://project.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "proj-model"}],
                }
            },
            "roles": {
                "memory": {"model": "acme:proj-model", "samplingParams": {"temperature": 0.2}}
            },
        },
    )
    env_file = isolated / "extra" / "models.json"
    _write(
        env_file,
        {
            "providers": {
                "acme": {"models": [{"id": "env-model"}]}  # upsert, baseUrl kept
            },
            "roles": {
                "memory": {"model": "acme:env-model", "samplingParams": {"temperature": 0.9}}
            },
        },
    )
    cfg = _config(models_file=str(env_file))
    registry = build_provider_registry(cfg)
    # roles upsert wholesale per role name -> env file's entry replaces project's
    assert registry.roles["memory"] == RoleSpec(
        model="acme:env-model", sampling_params={"temperature": 0.9}, thinking=None
    )
    assert set(registry.get("acme").model_ids()) == {"proj-model", "env-model"}
    built = build_role_model(cfg, role="memory", registry=registry)
    assert built.model_name == "env-model"
    assert built.temperature == 0.9


# --- zero regression -------------------------------------------------------------


def test_no_roles_section_matches_legacy_build(isolated):
    cfg = _config(memory_model="mem-model", sampling={"temperature": 0.5})
    registry = build_provider_registry(cfg)
    for role, ref in (("memory", "mem-model"), ("actor", "main-model")):
        built = build_role_model(cfg, role=role, registry=registry)
        expected = _build_legacy(dc_replace(cfg, model_name=ref))
        assert _config_dict(built) == _config_dict(expected), role


def test_roles_entry_without_sampling_keeps_env_tier(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {"roles": {"memory": {}}},
    )
    cfg = _config(sampling={"temperature": 0.6})
    built = build_role_model(cfg, role="memory")
    assert built.temperature == 0.6
