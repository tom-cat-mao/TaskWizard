"""S4/P2 loader: models.json parsing, precedence, merge, and recorded skips."""

from __future__ import annotations

import json

import pytest

from phone_agent.v2.providers.loader import (
    ModelsFileError,
    build_provider_registry,
    candidate_paths,
    load_raw_document,
    parse_models_json,
)


def _config(**overrides):
    from types import SimpleNamespace

    defaults = dict(
        base_url="http://localhost:8000/v1",
        model_name="autoglm-phone-9b",
        api_key="EMPTY",
        model_timeout=180.0,
        model_max_retries=2,
        http_headers=None,
        user_agent=None,
        cf_access_client_id=None,
        cf_access_client_secret=None,
        sampling=None,
        parallel_tool_calls=False,
        thinking="",
        models_file=None,
        _provider_registry=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolated HOME + cwd so nothing leaks in (HOME is only for hermeticity
    since P2: the user-level models.json layer no longer exists)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_parse_minimal_provider():
    parsed = parse_models_json(
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://acme.example/v1",
                    "apiKey": "k",
                    "models": [{"id": "fast", "name": "Fast"}],
                }
            }
        }
    )
    provider = parsed["acme"]
    assert provider.api == "openai-completions"
    assert provider.base_url == "https://acme.example/v1"
    assert provider.get_model("fast").name == "Fast"


def test_parse_env_interpolation():
    parsed = parse_models_json(
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "apiKey": "$TW_LOADER_KEY",
                    "headers": {"X-Auth": "${TW_LOADER_TOKEN}"},
                    "models": [],
                }
            }
        },
        env={"TW_LOADER_KEY": "sk", "TW_LOADER_TOKEN": "tok"},
    )
    assert parsed["acme"].api_key == "sk"
    assert parsed["acme"].headers["X-Auth"] == "tok"


def test_parse_missing_env_is_models_file_error():
    with pytest.raises(ModelsFileError):
        parse_models_json(
            {
                "providers": {
                    "acme": {"api": "openai-completions", "apiKey": "$TW_NOPE"}
                }
            }
        )


def test_parse_thinking_map_three_states():
    data = {
        "providers": {
            "p": {
                "api": "openai-completions",
                "models": [
                    {"id": "absent"},
                    {"id": "unsupported", "thinkingLevelMap": None},
                    {"id": "verbatim", "thinkingLevelMap": "low"},
                    {"id": "mapped", "thinkingLevelMap": {"high": "medium"}},
                ],
            }
        }
    }
    models = parse_models_json(data)["p"].models
    import phone_agent.v2.providers.types as t

    assert models["absent"].thinking_level_map is t.THINKING_MAP_UNSET
    assert models["unsupported"].thinking_level_map is None
    assert models["verbatim"].thinking_level_map == "low"
    assert models["mapped"].thinking_level_map == {"high": "medium"}


def test_parse_model_overrides_partial_patch():
    parsed = parse_models_json(
        {
            "providers": {
                "p": {
                    "api": "openai-completions",
                    "models": [
                        {"id": "m", "contextWindow": 128000, "samplingParams": {"temperature": 0.2}}
                    ],
                    "modelOverrides": {
                        "m": {"maxTokens": 8192, "samplingParams": {"top_p": 0.9}}
                    },
                }
            }
        }
    )
    model = parsed["p"].get_model("m")
    assert model.context_window == 128000
    assert model.max_tokens == 8192
    assert model.sampling_params == {"temperature": 0.2, "top_p": 0.9}


def test_build_registry_gateway_synthesized(isolated):
    registry = build_provider_registry(_config())
    assert registry is not None
    assert registry.list_providers() == ("gateway",)
    assert "autoglm-phone-9b" in registry.list_models("gateway")


def test_parse_compat_camelcase_keys():
    parsed = parse_models_json(
        {
            "providers": {
                "p": {
                    "api": "openai-completions",
                    "compat": {
                        "thinkingFormat": "reasoning_effort",
                        "supportsParallelToolCalls": False,
                        "extraBody": {"top_k": 40},
                    },
                }
            }
        }
    )
    compat = parsed["p"].compat
    assert compat.thinking_format == "reasoning_effort"
    assert compat.supports_parallel_tool_calls is False
    assert compat.extra_body == {"top_k": 40}


def test_build_registry_skips_malformed_json_with_warning(isolated):
    (isolated / ".taskwizard.models.json").write_text("{not json", encoding="utf-8")
    registry = build_provider_registry(_config())
    assert registry.list_providers() == ("gateway",)
    assert any("malformed JSON" in w.error for w in registry.declaration_warnings)


def test_build_registry_skips_undefined_env_provider_with_warning(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {"providers": {"p": {"api": "openai-completions", "apiKey": "$TW_MISSING"}}},
    )
    registry = build_provider_registry(_config())
    assert registry.get("p") is None
    assert any(
        "undefined env var: TW_MISSING" in w.error
        for w in registry.declaration_warnings
    )


def test_build_registry_skips_unknown_api_provider_with_warning(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {"providers": {"p": {"api": "carrier-pigeon"}}},
    )
    registry = build_provider_registry(_config())
    assert registry.get("p") is None
    assert any(
        "unsupported api 'carrier-pigeon'" in w.error
        for w in registry.declaration_warnings
    )


def test_user_level_files_are_not_read(isolated):
    """P2 single-level: ~/.taskwizard/models.json AND the old
    ~/.config/taskwizard/models.json are both ignored; project wins."""
    user_old = isolated / "home" / ".config" / "taskwizard" / "models.json"
    _write(
        user_old,
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://user.example/v1",
                    "apiKey": "user-key",
                    "models": [{"id": "a", "samplingParams": {"temperature": 0.1}}],
                }
            },
            "roles": {"memory": {"model": "acme:a"}},
        },
    )
    _write(
        isolated / "home" / ".taskwizard.models.json",
        {"providers": {"homedot": {"api": "openai-completions"}}},
    )
    # Only the user-level files exist -> registry stays gateway-only.
    registry = build_provider_registry(_config())
    assert registry is not None
    assert registry.list_providers() == ("gateway",)
    assert registry.roles == {}

    # Project file coexists -> its values win, user values never leak.
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "baseUrl": "https://project.example/v1",
                    "models": [{"id": "b"}],
                }
            }
        },
    )
    registry = build_provider_registry(_config())
    provider = registry.get("acme")
    assert provider.base_url == "https://project.example/v1"
    assert provider.api_key is None  # user key never merged
    assert provider.model_ids() == ("b",)
    assert registry.get("homedot") is None
    assert registry.roles == {}  # user-level roles section ignored


def test_env_file_only_entry_keeps_project_models(isolated):
    """A baseUrl/headers-only patch in the explicit env file keeps the
    project file's model catalog (former two-level use case, now across
    project + PHONE_AGENT_MODELS_FILE)."""
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "anthropic-messages",
                    "apiKey": "k",
                    "models": [{"id": "claude-x"}],
                }
            }
        },
    )
    env_file = isolated / "extra" / "models.json"
    _write(
        env_file,
        {"providers": {"acme": {"headers": {"X-Extra": "1"}}}},
    )
    registry = build_provider_registry(_config(models_file=str(env_file)))
    provider = registry.get("acme")
    assert provider.headers["X-Extra"] == "1"
    assert provider.api_key == "k"
    assert provider.model_ids() == ("claude-x",)


def test_env_file_has_highest_priority(isolated):
    _write(
        isolated / ".taskwizard.models.json",
        {
            "providers": {
                "acme": {
                    "api": "openai-completions",
                    "models": [{"id": "m", "samplingParams": {"temperature": 0.5}}]
                }
            }
        },
    )
    env_file = isolated / "extra" / "models.json"
    _write(
        env_file,
        {
            "providers": {
                "acme": {
                    "models": [{"id": "m", "samplingParams": {"temperature": 0.9}}]
                }
            }
        },
    )
    registry = build_provider_registry(_config(models_file=str(env_file)))
    model = registry.get("acme").get_model("m")
    assert model.sampling_params == {"temperature": 0.9}


def test_candidate_paths_order(isolated):
    _config_models = _config(models_file="/tmp/extra-models.json")
    paths = candidate_paths(_config_models)
    assert len(paths) == 2
    assert paths[0] == isolated / ".taskwizard.models.json"  # project level
    assert paths[1].name == "extra-models.json"  # explicit env file wins
    assert candidate_paths(_config()) == [isolated / ".taskwizard.models.json"]


def test_load_raw_document_returns_providers_and_roles(isolated):
    path = isolated / "doc.json"
    _write(
        path,
        {
            "providers": {"p": {"api": "openai-completions"}},
            "roles": {"memory": {"thinking": "low"}},
        },
    )
    providers, roles = load_raw_document(path)
    assert set(providers) == {"p"}
    assert roles["memory"].thinking == "low"
