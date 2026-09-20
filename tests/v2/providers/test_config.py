"""Tests for v2 config resolution: env/.env/override priority, sampling, CF pair, headers."""

from __future__ import annotations

import os

import pytest

from phone_agent.v2 import config as config_mod
from phone_agent.v2.config import V2Config, load_project_env
from phone_agent.v2.model import DEFAULT_MODEL_USER_AGENT, build_default_headers
from tests.v2.doubles.paths import REPO_ROOT

PHONE_AGENT_KEYS = [
    "PHONE_AGENT_BASE_URL",
    "PHONE_AGENT_MODEL",
    "PHONE_AGENT_API_KEY",
    "PHONE_AGENT_MODEL_TIMEOUT",
    "PHONE_AGENT_MODEL_MAX_RETRIES",
    "PHONE_AGENT_DEVICE_ID",
    "PHONE_AGENT_MEMORY_DIR",
    "PHONE_AGENT_APP_KB",
    "PHONE_AGENT_IMPLICIT_ALIAS",
    "PHONE_AGENT_ALIAS_OVERWRITE",
    "PHONE_AGENT_ALIAS_OVERWRITE_NOTES",
    "PHONE_AGENT_APP_LIST_MAX",
    "PHONE_AGENT_DREAM",
    "PHONE_AGENT_RESOLVER_DECISION_MODE",
    "PHONE_AGENT_RESOLVER_MIN_SCORE",
    "PHONE_AGENT_RESOLVER_MARGIN",
    "PHONE_AGENT_RESOLVER_TYPED_MARGIN",
    "PHONE_AGENT_RESOLVER_TOP_K",
    "PHONE_AGENT_RESOLVER_LEXICAL",
    "PHONE_AGENT_RESOLVER_PINYIN",
    "PHONE_AGENT_RESOLVER_EMBED",
    "PHONE_AGENT_RESOLVER_W_SIM",
    "PHONE_AGENT_RESOLVER_W_PRIOR",
    "PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_MIN_LEN",
    "PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_STOPWORDS",
    "PHONE_AGENT_RESOLVER_AUTO_MATCH_TYPES",
    "PHONE_AGENT_RESOLVER_CLARIFY_MATCH_TYPES",
    "PHONE_AGENT_EXPERIENCE",
    "PHONE_AGENT_EXPERIENCE_DIR",
    "PHONE_AGENT_EPISODE_KEEP",
    "PHONE_AGENT_EPISODE_ARCHIVE_DAYS",
    "PHONE_AGENT_LESSON_INJECT_MAX",
    "PHONE_AGENT_LESSON_INJECT_TOKENS",
    "PHONE_AGENT_MAX_STEPS",
    "PHONE_AGENT_MAX_HITL_RESUMES",
    "PHONE_AGENT_BUDGET_WARN_RATIO",
    "PHONE_AGENT_TOKEN_BUDGET",
    "PHONE_AGENT_TOKEN_WARN_REMAINING",
    "PHONE_AGENT_COMPACT",
    "PHONE_AGENT_COMPACT_WARN_RATIO",
    "PHONE_AGENT_COMPACT_TRIGGER_RATIO",
    "PHONE_AGENT_CONTEXT_WORK_TARGET",
    "PHONE_AGENT_COMPACT_TARGET_RATIO",
    "PHONE_AGENT_COMPACT_SUMMARY_TOKENS",
    "PHONE_AGENT_COMPACT_MIN_REDUCTION_TOKENS",
    "PHONE_AGENT_COMPACT_MIN_REDUCTION_RATIO",
    "PHONE_AGENT_CONTEXT_WINDOW",
    "PHONE_AGENT_IMAGE_KEEP",
    "PHONE_AGENT_OBS_MARKS_KEEP",
    "PHONE_AGENT_GROUNDING_PROVIDER",
    "PHONE_AGENT_ACCESSIBILITY_TIMEOUT",
    "PHONE_AGENT_ACCESSIBILITY_MAX_MARKS",
    "PHONE_AGENT_OBSERVE_SETTLE_MS",
    "PHONE_AGENT_OBSERVE_RETRY_MAX_LOOPS",
    "PHONE_AGENT_OBSERVE_RETRY_BACKOFF_S",
    "PHONE_AGENT_BLACK_SCREEN_DETECT",
    "PHONE_AGENT_TASKDOC",
    "PHONE_AGENT_TASKDOC_NUDGE_STEPS",
    "PHONE_AGENT_LOCATEANYTHING_MODEL",
    "PHONE_AGENT_LOCATEANYTHING_MAX_SIZE",
    "PHONE_AGENT_LOCATEANYTHING_CONTEXT_MAX_CHARS",
    "PHONE_AGENT_LOCATE_MAX_SIZE",
    "PHONE_AGENT_SCOPE_PADDING_RATIO",
    "PHONE_AGENT_LANG",
    "PHONE_AGENT_TRACE_DIR",
    "PHONE_AGENT_TRACE",
    "PHONE_AGENT_FINISH_VERIFY",
    "PHONE_AGENT_TEMPERATURE",
    "PHONE_AGENT_TOP_P",
    "PHONE_AGENT_FREQUENCY_PENALTY",
    "PHONE_AGENT_USER_AGENT",
    "PHONE_AGENT_HTTP_HEADERS",
    "PHONE_AGENT_CF_ACCESS_CLIENT_ID",
    "PHONE_AGENT_CF_ACCESS_CLIENT_SECRET",
    "PHONE_AGENT_MEMORY_MODEL",
    "PHONE_AGENT_VERIFIER_MODEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test with all PHONE_AGENT_* keys unset."""

    for key in PHONE_AGENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


# -- defaults ------------------------------------------------------------


def test_from_env_defaults():
    # Whole-dataclass comparison against the declared field defaults — one
    # equality absorbs the per-field noise and also pins the recall/evolution
    # defaults (memory_rag/embed_*/vec_db/evolution_mode/lessons_dir/...).
    # Only base_url/model_name lack dataclass defaults, so they are spelled out.
    assert V2Config.from_env() == V2Config(
        base_url="http://localhost:8000/v1",
        model_name="autoglm-phone-9b",
    )


def test_from_env_reads_shell_env(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("PHONE_AGENT_MODEL", "kimi-x")
    monkeypatch.setenv("PHONE_AGENT_MAX_STEPS", "7")
    monkeypatch.setenv("PHONE_AGENT_DEVICE_ID", "SERIAL123")
    monkeypatch.setenv("PHONE_AGENT_TRACE", "false")
    cfg = V2Config.from_env()
    assert cfg.base_url == "https://gw.example/v1"
    assert cfg.model_name == "kimi-x"
    assert cfg.max_model_calls == 7
    assert cfg.device_id == "SERIAL123"
    assert cfg.trace_enabled is False


def test_locate_resolution_and_scope_padding_config(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_LOCATE_MAX_SIZE", "1536")
    monkeypatch.setenv("PHONE_AGENT_SCOPE_PADDING_RATIO", "0.08")
    monkeypatch.setenv("PHONE_AGENT_LOCATEANYTHING_CONTEXT_MAX_CHARS", "180")
    cfg = V2Config.from_env()
    assert cfg.locate_max_size == 1536
    assert cfg.scope_padding_ratio == 0.08
    assert cfg.locateanything_context_max_chars == 180

    overridden = V2Config.from_env({"locate_max_size": 0, "scope_padding_ratio": 0.02})
    assert overridden.locate_max_size == 0
    assert overridden.scope_padding_ratio == 0.02


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PHONE_AGENT_LOCATE_MAX_SIZE", "-1"),
        ("PHONE_AGENT_SCOPE_PADDING_RATIO", "-0.01"),
    ],
)
def test_invalid_locate_config_is_rejected(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        V2Config.from_env()


def test_context_pruning_keys_env_and_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_IMAGE_KEEP", "3")
    monkeypatch.setenv("PHONE_AGENT_OBS_MARKS_KEEP", "4")
    cfg = V2Config.from_env()
    assert cfg.image_keep == 3
    assert cfg.obs_marks_keep == 4
    # CLI override beats env.
    cfg2 = V2Config.from_env({"image_keep": 1, "obs_marks_keep": 5})
    assert cfg2.image_keep == 1
    assert cfg2.obs_marks_keep == 5


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
def test_app_kb_opt_out_tokens(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_APP_KB", raw)
    assert V2Config.from_env().app_kb_enabled is False


def test_app_kb_config_keys_parse(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MEMORY_DIR", "/tmp/phone-memory")
    monkeypatch.setenv("PHONE_AGENT_APP_KB", "yes")
    monkeypatch.setenv("PHONE_AGENT_APP_LIST_MAX", "12")
    monkeypatch.setenv("PHONE_AGENT_DREAM", "AUTO")
    cfg = V2Config.from_env()
    assert cfg.memory_dir == "/tmp/phone-memory"
    assert cfg.app_kb_enabled is True
    assert cfg.app_list_max == 12
    assert cfg.dream_mode == "auto"


def test_experience_config_keys_parse(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_EXPERIENCE", "off")
    monkeypatch.setenv("PHONE_AGENT_EXPERIENCE_DIR", "/tmp/experience")
    monkeypatch.setenv("PHONE_AGENT_EPISODE_KEEP", "12")
    monkeypatch.setenv("PHONE_AGENT_EPISODE_ARCHIVE_DAYS", "30")
    cfg = V2Config.from_env()
    assert cfg.experience_enabled is False
    assert cfg.experience_dir == "/tmp/experience"
    assert cfg.episode_keep == 12
    assert cfg.episode_archive_days == 30


def test_lesson_injection_limits_parse(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_LESSON_INJECT_MAX", "2")
    monkeypatch.setenv("PHONE_AGENT_LESSON_INJECT_TOKENS", "400")
    cfg = V2Config.from_env()
    assert cfg.lesson_inject_max == 2
    assert cfg.lesson_inject_tokens == 400


@pytest.mark.parametrize(
    "key",
    ["PHONE_AGENT_LESSON_INJECT_MAX", "PHONE_AGENT_LESSON_INJECT_TOKENS"],
)
def test_negative_lesson_injection_limit_is_rejected(monkeypatch, key):
    monkeypatch.setenv(key, "-1")
    with pytest.raises(ValueError, match=key.removeprefix("PHONE_AGENT_")):
        V2Config.from_env()


@pytest.mark.parametrize("raw", ["", "bogus", "true"])
def test_experience_illegal_value_falls_back_to_on(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_EXPERIENCE", raw)
    assert V2Config.from_env().experience_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off"])
def test_implicit_alias_opt_out_tokens(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_IMPLICIT_ALIAS", raw)
    assert V2Config.from_env().implicit_alias_enabled is False


def test_alias_overwrite_config(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_ALIAS_OVERWRITE", "off")
    monkeypatch.setenv("PHONE_AGENT_ALIAS_OVERWRITE_NOTES", "开错了, wrong app ,选错")
    config = V2Config.from_env()

    assert config.alias_overwrite_enabled is False
    assert config.alias_overwrite_notes == ("开错了", "wrong app", "选错")


@pytest.mark.parametrize("raw", ["", "bogus", "always"])
def test_dream_illegal_value_falls_back_to_manual(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_DREAM", raw)
    assert V2Config.from_env().dream_mode == "manual"


def test_typed_resolver_config_keys_parse(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_RESOLVER_DECISION_MODE", "LEGACY")
    monkeypatch.setenv("PHONE_AGENT_RESOLVER_TYPED_MARGIN", "0.12")
    monkeypatch.setenv("PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_MIN_LEN", "5")
    monkeypatch.setenv(
        "PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_STOPWORDS",
        "com, org, example",
    )
    monkeypatch.setenv(
        "PHONE_AGENT_RESOLVER_AUTO_MATCH_TYPES",
        "exact_alias,exact_package_segment",
    )
    monkeypatch.setenv(
        "PHONE_AGENT_RESOLVER_CLARIFY_MATCH_TYPES",
        "fuzzy,embedding",
    )

    cfg = V2Config.from_env()

    assert cfg.resolver_decision_mode == "legacy"
    assert cfg.resolver_typed_margin == 0.12
    assert cfg.resolver_package_segment_min_len == 5
    assert cfg.resolver_package_segment_stopwords == ("com", "org", "example")
    assert cfg.resolver_auto_match_types == (
        "exact_alias",
        "exact_package_segment",
    )
    assert cfg.resolver_clarify_match_types == ("fuzzy", "embedding")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PHONE_AGENT_RESOLVER_TYPED_MARGIN", "-0.01"),
        ("PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_MIN_LEN", "0"),
        ("PHONE_AGENT_RESOLVER_AUTO_MATCH_TYPES", "exact_alias,nope"),
        ("PHONE_AGENT_RESOLVER_CLARIFY_MATCH_TYPES", "fuzzy,nope"),
    ],
)
def test_invalid_typed_resolver_config_is_rejected(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        V2Config.from_env()


def test_budget_and_resume_keys_env_and_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MAX_HITL_RESUMES", "7")
    monkeypatch.setenv("PHONE_AGENT_BUDGET_WARN_RATIO", "0.5")
    cfg = V2Config.from_env()
    assert cfg.max_hitl_resumes == 7
    assert cfg.budget_warn_ratio == 0.5
    # CLI override beats env.
    cfg2 = V2Config.from_env({"max_hitl_resumes": 3, "budget_warn_ratio": 0.9})
    assert cfg2.max_hitl_resumes == 3
    assert cfg2.budget_warn_ratio == 0.9


def test_token_budget_keys_env_and_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TOKEN_BUDGET", "500000")
    monkeypatch.setenv("PHONE_AGENT_TOKEN_WARN_REMAINING", "20000")
    cfg = V2Config.from_env()
    assert cfg.token_budget == 500_000
    assert cfg.token_warn_remaining == 20_000
    # CLI override beats env.
    cfg2 = V2Config.from_env({"token_budget": 42, "token_warn_remaining": 7})
    assert cfg2.token_budget == 42
    assert cfg2.token_warn_remaining == 7


def test_compact_keys_env_and_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_COMPACT", "off")
    monkeypatch.setenv("PHONE_AGENT_COMPACT_WARN_RATIO", "0.6")
    monkeypatch.setenv("PHONE_AGENT_COMPACT_TRIGGER_RATIO", "0.88")
    monkeypatch.setenv("PHONE_AGENT_CONTEXT_WINDOW", "128000")
    cfg = V2Config.from_env()
    assert cfg.compact_enabled is False
    assert cfg.compact_warn_ratio == 0.6
    assert cfg.compact_trigger_ratio == 0.88
    assert cfg.context_window == 128000
    # CLI override beats env.
    cfg2 = V2Config.from_env({"compact_enabled": True, "context_window": 262144})
    assert cfg2.compact_enabled is True
    assert cfg2.context_window == 262144


def test_max_steps_default_is_loop_fuse_100(monkeypatch):
    # A4: MAX_STEPS is now a runaway-loop fuse (default 100), not the cost budget.
    assert V2Config.from_env().max_model_calls == 100
    monkeypatch.setenv("PHONE_AGENT_MAX_STEPS", "250")
    assert V2Config.from_env().max_model_calls == 250


@pytest.mark.parametrize("raw", ["off", "auto", "always", "ALWAYS", "Off"])
def test_finish_verify_valid_values(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_FINISH_VERIFY", raw)
    assert V2Config.from_env().finish_verify == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "  ", "bogus", "yes", "1"])
def test_finish_verify_illegal_falls_back_to_auto(monkeypatch, raw):
    monkeypatch.setenv("PHONE_AGENT_FINISH_VERIFY", raw)
    assert V2Config.from_env().finish_verify == "auto"


# -- priority: override > shell env > .env > default ---------------------


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MODEL", "env-model")
    monkeypatch.setenv("PHONE_AGENT_MAX_STEPS", "5")
    cfg = V2Config.from_env({"model_name": "cli-model", "max_model_calls": 99})
    assert cfg.model_name == "cli-model"
    assert cfg.max_model_calls == 99


def test_none_override_does_not_clobber(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MODEL", "env-model")
    cfg = V2Config.from_env({"model_name": None, "device_id": None})
    assert cfg.model_name == "env-model"


def test_unknown_override_raises():
    with pytest.raises(ValueError):
        V2Config.from_env({"not_a_field": "x"})


def test_env_beats_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'export PHONE_AGENT_MODEL="dotenv-model"\n'
        "PHONE_AGENT_LANG='en'\n"
        "# comment line\n"
        "PHONE_AGENT_MAX_STEPS=15\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    # shell env already sets MODEL -> .env must NOT override it
    monkeypatch.setenv("PHONE_AGENT_MODEL", "shell-model")
    load_project_env()
    assert __import__("os").environ["PHONE_AGENT_MODEL"] == "shell-model"
    # keys not in shell env come from .env, with quotes/export stripped
    assert __import__("os").environ["PHONE_AGENT_LANG"] == "en"
    assert __import__("os").environ["PHONE_AGENT_MAX_STEPS"] == "15"
    cfg = V2Config.from_env()
    assert cfg.model_name == "shell-model"
    assert cfg.lang == "en"
    assert cfg.max_model_calls == 15


def test_dotenv_missing_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "ROOT", tmp_path / "nope")
    load_project_env()  # must not raise


# -- .env parsing: inline comments (WP2) ---------------------------------

_EXAMPLE_TEMPLATE = REPO_ROOT / ".env.example"


def _commented_active_template_lines() -> list[str]:
    """Active ``PHONE_AGENT_*`` lines of the shipped template that end in a comment."""

    return [
        line
        for line in _EXAMPLE_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.startswith("PHONE_AGENT_") and "#" in line
    ]


def _value_as_an_operator_reads_it(line: str) -> str:
    """The line's literal meaning: the value, then an inline ``#`` comment."""

    return line.split("=", 1)[1].split("#", 1)[0].strip().strip("\"'")


def test_dotenv_parses_the_shipped_template_inline_comments(monkeypatch, tmp_path):
    """Regression (WP2): ``KEY="value"   # note`` used to load the whole tail.

    The cases are not synthetic: they are the commented active lines of the
    shipped ``.env.example``, the file operators copy into ``.env``.
    """

    commented = _commented_active_template_lines()
    assert len(commented) >= 3, (
        "the template no longer exercises inline comments; "
        "pick another real case or drop this test"
    )
    (tmp_path / ".env").write_text("\n".join(commented) + "\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)

    load_project_env()

    for line in commented:
        key = line.split("=", 1)[0].strip()
        value = os.environ[key]
        assert value == _value_as_an_operator_reads_it(line), f"{key}={value!r}"
        assert "#" not in value, f"{key} kept its inline comment: {value!r}"


def test_dotenv_inline_comment_honours_quotes_and_bare_hashes(monkeypatch, tmp_path):
    """``#`` only starts a comment when it stands alone (shell rule)."""

    (tmp_path / ".env").write_text(
        "PHONE_AGENT_GROUNDING_PROVIDER='hybrid' # quoted, then comment\n"
        "PHONE_AGENT_API_KEY=sk-abc#def\n"
        "PHONE_AGENT_MEMORY_DIR=/tmp/a#b/c\n"
        "PHONE_AGENT_LANG=#nothing-but-a-comment\n"
        'PHONE_AGENT_LOCATEANYTHING_MODEL="models/x#y"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)

    load_project_env()

    assert os.environ["PHONE_AGENT_GROUNDING_PROVIDER"] == "hybrid"
    assert os.environ["PHONE_AGENT_API_KEY"] == "sk-abc#def"
    assert os.environ["PHONE_AGENT_MEMORY_DIR"] == "/tmp/a#b/c"
    assert os.environ["PHONE_AGENT_LANG"] == ""
    assert os.environ["PHONE_AGENT_LOCATEANYTHING_MODEL"] == "models/x#y"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"hybrid"   # hybrid | accessibility', '"hybrid"   '),
        ("'config.yml'\t# path", "'config.yml'\t"),
        ("plain-value", "plain-value"),
        ("value # comment", "value "),
        ("value\t# tab-separated", "value\t"),
        ("sk-abc#def", "sk-abc#def"),
        ('"models/x#y"', '"models/x#y"'),
        ('"a\\"#b"', '"a\\"#b"'),
        ("#whole-value-comment", ""),
    ],
)
def test_strip_inline_comment_follows_the_shell_rule(raw, expected):
    assert config_mod._strip_inline_comment(raw) == expected


# -- WP2: observe retry knobs --------------------------------------------


def test_observe_retry_defaults_keep_the_two_round_window():
    cfg = V2Config.from_env()

    assert cfg.observe_retry_max_loops == 1
    assert cfg.observe_retry_backoff_s == 2.0


def test_observe_retry_keys_read_env_and_lose_to_an_override(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_OBSERVE_RETRY_MAX_LOOPS", "3")
    monkeypatch.setenv("PHONE_AGENT_OBSERVE_RETRY_BACKOFF_S", "0.5")

    cfg = V2Config.from_env({"observe_retry_max_loops": 0})

    assert cfg.observe_retry_max_loops == 0
    assert cfg.observe_retry_backoff_s == 0.5


@pytest.mark.parametrize(
    "key",
    ["PHONE_AGENT_OBSERVE_RETRY_MAX_LOOPS", "PHONE_AGENT_OBSERVE_RETRY_BACKOFF_S"],
)
def test_negative_observe_retry_values_are_rejected(monkeypatch, key):
    monkeypatch.setenv(key, "-1")

    with pytest.raises(ValueError, match=key):
        V2Config.from_env()


# -- sampling ------------------------------------------------------------


def test_sampling_parsing(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TEMPERATURE", "1.0")
    monkeypatch.setenv("PHONE_AGENT_TOP_P", "0.95")
    monkeypatch.setenv("PHONE_AGENT_FREQUENCY_PENALTY", "0")
    cfg = V2Config.from_env()
    assert cfg.sampling == {"temperature": 1.0, "top_p": 0.95, "frequency_penalty": 0.0}


def test_sampling_partial(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TEMPERATURE", "0.7")
    cfg = V2Config.from_env()
    assert cfg.sampling == {"temperature": 0.7}


def test_sampling_bad_float_raises(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_TEMPERATURE", "hot")
    with pytest.raises(ValueError):
        V2Config.from_env()


def test_bad_float_timeout_raises(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_MODEL_TIMEOUT", "soon")
    with pytest.raises(ValueError):
        V2Config.from_env()


# -- CF Access pair validation -------------------------------------------


def test_cf_access_pair_ok(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_ID", "cid")
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_SECRET", "csecret")
    cfg = V2Config.from_env()
    assert cfg.cf_access_client_id == "cid"
    assert cfg.cf_access_client_secret == "csecret"


def test_cf_access_id_only_raises(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_ID", "cid")
    with pytest.raises(ValueError):
        V2Config.from_env()


def test_cf_access_secret_only_raises(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_SECRET", "csecret")
    with pytest.raises(ValueError):
        V2Config.from_env()


# -- headers -------------------------------------------------------------


def test_headers_default_ua(monkeypatch):
    cfg = V2Config.from_env()
    headers = build_default_headers(cfg)
    assert headers["User-Agent"] == DEFAULT_MODEL_USER_AGENT
    assert "CF-Access-Client-Id" not in headers


def test_headers_custom_ua_and_http_headers(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_USER_AGENT", "MyUA/1.0")
    monkeypatch.setenv("PHONE_AGENT_HTTP_HEADERS", "X-A=1;X-B=2")
    cfg = V2Config.from_env()
    headers = build_default_headers(cfg)
    assert headers["User-Agent"] == "MyUA/1.0"
    assert headers["X-A"] == "1"
    assert headers["X-B"] == "2"


def test_headers_http_headers_do_not_override_explicit_ua(monkeypatch):
    # A UA supplied via HTTP_HEADERS takes precedence over the default constant
    monkeypatch.setenv("PHONE_AGENT_HTTP_HEADERS", "User-Agent=FromHeaders/9")
    cfg = V2Config.from_env()
    headers = build_default_headers(cfg)
    assert headers["User-Agent"] == "FromHeaders/9"


def test_headers_with_cf_access(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_ID", "cid")
    monkeypatch.setenv("PHONE_AGENT_CF_ACCESS_CLIENT_SECRET", "csecret")
    cfg = V2Config.from_env()
    headers = build_default_headers(cfg)
    assert headers["CF-Access-Client-Id"] == "cid"
    assert headers["CF-Access-Client-Secret"] == "csecret"
