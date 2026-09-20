"""S4 values layer: $ENV / ${ENV} interpolation and literal passthrough."""

from __future__ import annotations

import pytest

from phone_agent.v2.providers.values import resolve_headers, resolve_str, resolve_value


def test_literal_passthrough():
    assert resolve_value("https://api.example.com/v1") == "https://api.example.com/v1"


def test_env_forms_resolve(monkeypatch):
    monkeypatch.setenv("TW_TEST_KEY", "sk-123")
    assert resolve_value("$TW_TEST_KEY") == "sk-123"
    assert resolve_value("Bearer ${TW_TEST_KEY}") == "Bearer sk-123"


def test_missing_env_raises():
    with pytest.raises(ValueError, match="TW_TEST_UNDEFINED"):
        resolve_value("$TW_TEST_UNDEFINED")


def test_non_string_and_non_env_passthrough():
    assert resolve_value(42) == 42
    assert resolve_value(None) is None
    # No $ -> literal even if it looks env-ish.
    assert resolve_value("EMPTY") == "EMPTY"


def test_resolution_is_single_pass(monkeypatch):
    monkeypatch.setenv("TW_TEST_A", "$TW_TEST_B")
    monkeypatch.setenv("TW_TEST_B", "boom")
    # An env value containing $ is kept verbatim (no recursive expansion).
    assert resolve_value("${TW_TEST_A}") == "$TW_TEST_B"


def test_resolve_str_none_and_type_error():
    assert resolve_str(None) is None
    with pytest.raises(ValueError, match="string"):
        resolve_str(7)


def test_resolve_headers_interpolates_values(monkeypatch):
    monkeypatch.setenv("TW_TEST_SECRET", "s3cret")
    resolved = resolve_headers(
        {"X-Key": "$TW_TEST_SECRET", "X-Literal": "plain"},
    )
    assert resolved == {"X-Key": "s3cret", "X-Literal": "plain"}


def test_resolve_headers_rejects_missing_env():
    with pytest.raises(ValueError):
        resolve_headers({"X-Key": "$TW_TEST_MISSING_HEADER_ENV"})
