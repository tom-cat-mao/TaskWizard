"""Real-run config precedence: shell > project .env > default, CLI last.

``V2Config.from_env`` does not itself load the project ``.env``; the real launch
path must call ``load_project_env()`` first. We patch the loader wrapper with a
fake that mimics dotenv semantics (does not overwrite an existing shell env) so
the precedence can be checked without touching the real ``.env``.
"""

from __future__ import annotations

import argparse
import os

import run_diagnosis as rd


def _args(**overrides) -> argparse.Namespace:
    base = {
        "device_id": None, "max_steps": None, "token_budget": None, "base_url": None,
        "model": None, "apikey": None, "model_timeout": None, "model_max_retries": None,
        "grounding_provider": None, "accessibility_timeout": None,
        "accessibility_max_marks": None, "locateanything_model": None,
        "locateanything_max_size": None, "lang": None, "no_taskdoc": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_dotenv(monkeypatch):
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        # dotenv semantics: do not overwrite an already-set shell env.
        os.environ.setdefault("PHONE_AGENT_MODEL", "from-dotenv")

    monkeypatch.setattr(rd, "_load_project_env", fake_load)
    return calls


def test_dotenv_is_loaded_and_used(tmp_path, monkeypatch):
    monkeypatch.delenv("PHONE_AGENT_MODEL", raising=False)
    calls = _patch_dotenv(monkeypatch)
    config = rd._resolve_config(_args(), tmp_path / "r")
    assert calls["n"] == 1
    assert config.model_name == "from-dotenv"


def test_shell_env_beats_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MODEL", "from-shell")
    _patch_dotenv(monkeypatch)
    config = rd._resolve_config(_args(), tmp_path / "r")
    assert config.model_name == "from-shell"


def test_cli_beats_shell_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MODEL", "from-shell")
    _patch_dotenv(monkeypatch)
    config = rd._resolve_config(_args(model="from-cli"), tmp_path / "r")
    assert config.model_name == "from-cli"
