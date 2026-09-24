"""Protocol client contract: official wire shape, retry policy, kev serialization.

Every case here speaks the **official** API: ``Authorization: Bearer``, three
question types (``noul``/``choice``/``score``), ``instructions`` + type-specific
``criteria``, per-type answer objects and the ``{"detail": {"error_type"}}``
error envelope.  The plugin was first written against blog material that
disagreed on all four points; this file is what keeps the correction honest.
"""

from __future__ import annotations

import threading

import pytest

from tests.plugins.systemone_harness import ERROR_CODES, FakeSystemOneServer


def make_client(client_module, server=None, **overrides):
    kwargs = {
        "backend": "kev",
        "model": "kev-4b@qwen3",
        "timeout": 5.0,
        "max_retries": 2,
        "sleeper": lambda _seconds: None,
    }
    if server is not None:
        kwargs["base_url"] = server.url
    kwargs.update(overrides)
    return client_module.SystemOneClient(**kwargs)


# ---------------------------------------------------------------------------
# Question types and builders
# ---------------------------------------------------------------------------
def test_there_are_exactly_three_question_types(client_module):
    assert client_module.QUESTION_TYPES == ("noul", "choice", "score")
    assert "count" not in client_module.QUESTION_TYPES
    assert "boolean" not in client_module.QUESTION_TYPES


def test_question_builders_produce_the_official_shapes(client_module):
    assert client_module.noul_question("is it urgent?") == {
        "type": "noul",
        "instructions": "is it urgent?",
    }
    assert client_module.noul_question(
        "is it urgent?", {"true": "urgent", "false": "not urgent"}
    ) == {
        "type": "noul",
        "instructions": "is it urgent?",
        "criteria": {"true": "urgent", "false": "not urgent"},
    }
    assert client_module.choice_question(
        {"billing": "money", "technical": None}, "which department?"
    ) == {
        "type": "choice",
        "instructions": "which department?",
        "criteria": {"billing": "money", "technical": None},
    }
    # A sequence of names is shorthand for "every option undescribed".
    assert client_module.choice_question(["a", "b"], "pick")["criteria"] == {
        "a": None,
        "b": None,
    }
    assert client_module.score_question(["low", "mid", "high"], "how bad?") == {
        "type": "score",
        "instructions": "how bad?",
        "criteria": ["low", "mid", "high"],
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda c: c.noul_question("   "),
        lambda c: c.noul_question("q", {}),  # an empty criteria map is not a map
        lambda c: c.choice_question([], "q"),
        lambda c: c.choice_question({"a": None}, ""),
        lambda c: c.choice_question([f"x{i}" for i in range(256)], "q"),
        lambda c: c.choice_question(["same", "same"], "q"),
        lambda c: c.score_question(["only one"], "q"),
        lambda c: c.score_question("low|high", "q"),  # a string is not a level list
        lambda c: c.score_question(["a", "b"], None),
    ],
)
def test_question_builders_refuse_malformed_questions(client_module, build):
    with pytest.raises(ValueError):
        build(client_module)


def test_validate_question_normalizes_and_rejects_unknown_types(client_module):
    normalized = client_module.validate_question(
        "q", {"type": "NOUL", "instructions": " is it? ", "criteria": {"true": 1}}
    )
    assert normalized == {
        "type": "noul",
        "instructions": "is it?",
        "criteria": {"true": "1"},
    }
    with pytest.raises(ValueError, match="unknown type"):
        client_module.validate_question("q", {"type": "boolean", "instructions": "x"})
    with pytest.raises(ValueError, match="must be a mapping"):
        client_module.validate_question("q", "not-a-question")


def test_state_accepts_string_object_and_array(client_module, fake_server):
    client = make_client(client_module, fake_server)
    for state in ("a string", {"tool": "tap"}, ["step one", "step two"]):
        client.ask(state, {"q": client_module.noul_question("q")})
        assert fake_server.last_body()["state"] == state
        assert fake_server.last_body()["model"] == "kev-4b@qwen3"


@pytest.mark.parametrize("state", [None, "", "   ", {}, []])
def test_an_empty_state_is_refused_before_the_network(client_module, fake_server, state):
    with pytest.raises(ValueError, match="state must not be empty"):
        make_client(client_module, fake_server).ask(
            state, {"q": client_module.noul_question("q")}
        )
    assert fake_server.calls() == 0


def test_an_unusable_state_type_is_refused(client_module, fake_server):
    with pytest.raises(ValueError, match="string, object or array"):
        make_client(client_module, fake_server).ask(
            42, {"q": client_module.noul_question("q")}
        )


def test_ask_requires_at_least_one_question(client_module, fake_server):
    with pytest.raises(ValueError, match="at least one question"):
        make_client(client_module, fake_server).ask("state", {})
    assert fake_server.calls() == 0


# ---------------------------------------------------------------------------
# Authentication and the happy path
# ---------------------------------------------------------------------------
def test_jev_authenticates_with_a_bearer_header_and_kev_sends_none(
    client_module, fake_server
):
    make_client(client_module, fake_server).ask(
        "s", {"q": client_module.noul_question("q")}
    )
    assert "authorization" not in fake_server.requests[-1]["headers"]

    make_client(
        client_module, fake_server, backend="jev", api_key="sk-jev-SECRET"
    ).ask("s", {"q": client_module.noul_question("q")})
    assert (
        fake_server.requests[-1]["headers"]["authorization"] == "Bearer sk-jev-SECRET"
    )


def test_a_bearer_authenticated_backend_accepts_the_call(client_module):
    """The server side of the official auth contract: wrong shape -> 401."""

    server = FakeSystemOneServer(require_api_key="sk-jev-good").start()
    try:
        client = make_client(
            client_module, server, backend="jev", api_key="sk-jev-good", max_retries=0
        )
        reply = client.ask("s", {"q": client_module.noul_question("q")})
        assert reply.value("q") is True
    finally:
        server.stop()


def test_a_wrong_key_is_rejected_with_401_and_never_retried(client_module):
    server = FakeSystemOneServer(require_api_key="sk-jev-good").start()
    sleeps: list[float] = []
    try:
        client = make_client(
            client_module,
            server,
            backend="jev",
            api_key="sk-jev-wrong",
            sleeper=sleeps.append,
        )
        with pytest.raises(client_module.SystemOneError) as excinfo:
            client.ask("s", {"q": client_module.noul_question("q")})
    finally:
        server.stop()
    # The server's own error_type wins over the status-derived fallback.
    assert excinfo.value.code == "AuthenticationError"
    assert excinfo.value.status == 401
    assert excinfo.value.retryable is False
    assert server.calls() == 1
    assert sleeps == []


def test_ask_sends_the_official_request_envelope(client_module, fake_server):
    client = make_client(client_module, fake_server)
    client.ask(
        "tool: tap target: 支付",
        {
            "risky": client_module.noul_question("risky?", {"true": "yes", "false": "no"}),
            "which": client_module.choice_question({"pay": None, "cancel": None}, "which?"),
            "risk": client_module.score_question(["low", "high"], "how risky?"),
        },
    )

    assert fake_server.requests[-1]["path"] == client_module.PROTOCOL_PATH
    body = fake_server.last_body()
    assert set(body) == {"model", "state", "questions"}
    assert body["questions"]["risky"] == {
        "type": "noul",
        "instructions": "risky?",
        "criteria": {"true": "yes", "false": "no"},
    }
    assert body["questions"]["which"]["criteria"] == {"pay": None, "cancel": None}
    assert body["questions"]["risk"]["criteria"] == ["low", "high"]
    assert "description" not in body["questions"]["risky"]


# ---------------------------------------------------------------------------
# Answer parsing (official shapes -> internal representation)
# ---------------------------------------------------------------------------
def test_noul_answer_is_a_probability_mapped_to_a_decision(client_module, fake_server):
    server = FakeSystemOneServer(answer={"q": {"type": "noul", "noul": 0.95}}).start()
    try:
        reply = make_client(client_module, server).ask(
            "s", {"q": client_module.noul_question("q")}
        )
    finally:
        server.stop()
    answer = reply.answer("q")
    assert answer.kind == "noul"
    assert answer.p == pytest.approx(0.95)
    assert answer.value is True  # 0.95 >= 0.5


def test_a_low_noul_probability_decides_false(client_module):
    server = FakeSystemOneServer(answer={"q": {"type": "noul", "noul": 0.2}}).start()
    try:
        reply = make_client(client_module, server).ask(
            "s", {"q": client_module.noul_question("q")}
        )
    finally:
        server.stop()
    assert reply.answer("q").value is False
    assert reply.answer("q").p == pytest.approx(0.2)


def test_choice_answer_maps_to_value_plus_confidence(client_module):
    server = FakeSystemOneServer(
        answer={
            "q": {
                "type": "choice",
                "choice": "billing",
                "confidence": 0.83,
                "probabilities": {"billing": 0.91, "technical": 0.09},
            }
        }
    ).start()
    try:
        reply = make_client(client_module, server).ask(
            "s", {"q": client_module.choice_question({"billing": None, "technical": None}, "q")}
        )
    finally:
        server.stop()
    answer = reply.answer("q")
    assert (answer.kind, answer.value) == ("choice", "billing")
    assert answer.p == pytest.approx(0.83)
    assert answer.probabilities["billing"] == pytest.approx(0.91)


def test_score_answer_maps_to_value_plus_confidence(client_module):
    server = FakeSystemOneServer(
        answer={
            "q": {
                "type": "score",
                "score": 3.5,
                "confidence": 0.66,
                "probabilities": {"high": 0.66},
                "legend": {"high": "hard to undo"},
            }
        }
    ).start()
    try:
        reply = make_client(client_module, server).ask(
            "s", {"q": client_module.score_question(["low", "high"], "q")}
        )
    finally:
        server.stop()
    answer = reply.answer("q")
    assert (answer.kind, answer.value) == ("score", 3.5)
    assert answer.p == pytest.approx(0.66)
    assert answer.legend == {"high": "hard to undo"}


def test_the_response_model_id_is_reported(client_module, fake_server):
    reply = make_client(client_module, fake_server).ask(
        "s", {"q": client_module.noul_question("q")}
    )
    assert reply.model == "jev-test-1.0"


def test_usage_is_read_as_tokens(client_module, fake_server):
    reply = make_client(client_module, fake_server).ask(
        "s", {"q": client_module.noul_question("q")}
    )
    assert reply.usage == {"input_tokens": 128, "output_tokens": 24}
    assert reply.tokens() == 152
    assert client_module.usage_tokens({"input_tokens": 3, "output_tokens": 4}) == 7
    assert client_module.usage_tokens({"total_tokens": 9}) == 9
    assert client_module.usage_tokens({"unrelated": "x"}) is None
    assert client_module.usage_tokens(None) is None


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"q": {"noul": 0.5}},  # no type
        {"q": {"type": "boolean", "noul": 0.5}},  # the removed type name
        {"q": {"type": "noul", "noul": 1.4}},
        {"q": {"type": "noul", "noul": "high"}},
        {"q": {"type": "choice", "confidence": 0.5}},  # no choice string
        {"q": {"type": "choice", "choice": "a", "confidence": 2.0}},
        {"q": {"type": "choice", "choice": "a", "probabilities": "nope"}},
        {"q": {"type": "score", "confidence": 0.5}},  # no score
        {"q": {"type": "score", "score": True}},
    ],
)
def test_malformed_answers_are_refused(client_module, answer):
    server = FakeSystemOneServer(answer=answer).start()
    try:
        with pytest.raises(client_module.SystemOneError) as excinfo:
            make_client(client_module, server, max_retries=0).ask(
                "s", {"q": client_module.noul_question("q")}
            )
    finally:
        server.stop()
    assert excinfo.value.code == "SCHEMA_INVALID"


def test_empty_answer_set_is_refused(client_module):
    server = FakeSystemOneServer(answer={}).start()
    try:
        with pytest.raises(client_module.SystemOneError, match="no answers"):
            make_client(client_module, server, max_retries=0).ask(
                "s", {"q": client_module.noul_question("q")}
            )
    finally:
        server.stop()


def test_unparseable_body_is_a_schema_error(client_module):
    server = FakeSystemOneServer(raw_body=b"<html>not json</html>").start()
    try:
        with pytest.raises(client_module.SystemOneError) as excinfo:
            make_client(client_module, server, max_retries=0).ask(
                "s", {"q": client_module.noul_question("q")}
            )
    finally:
        server.stop()
    assert excinfo.value.code == "SCHEMA_INVALID"


# ---------------------------------------------------------------------------
# Error envelope and retry policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,code", sorted(ERROR_CODES.items()))
def test_official_error_statuses_surface_with_their_error_type(
    client_module, status, code
):
    server = FakeSystemOneServer(
        error=(status, code), error_message="field 'questions' is required"
    ).start()
    try:
        client = make_client(client_module, server, max_retries=0)
        with pytest.raises(client_module.SystemOneError) as excinfo:
            client.ask("s", {"q": client_module.noul_question("q")})
    finally:
        server.stop()
    assert excinfo.value.code == code
    assert excinfo.value.status == status
    assert "field 'questions' is required" in str(excinfo.value)
    assert server.calls() == 1


def test_the_error_envelope_is_parsed_as_detail_error_type(client_module):
    server = FakeSystemOneServer(error=(422, "ValidationError")).start()
    try:
        with pytest.raises(client_module.SystemOneError) as excinfo:
            make_client(client_module, server, max_retries=0).ask(
                "s", {"q": client_module.noul_question("q")}
            )
    finally:
        server.stop()
    assert excinfo.value.code == "ValidationError"
    assert excinfo.value.retryable is False


def test_network_failure_reports_the_network_code(client_module, no_listener_port):
    client = make_client(
        client_module, base_url=f"http://127.0.0.1:{no_listener_port}", max_retries=0
    )
    with pytest.raises(client_module.SystemOneError) as excinfo:
        client.ask("s", {"q": client_module.noul_question("q")})
    assert excinfo.value.code == "NETWORK"


def test_rate_limit_is_retried_with_bounded_backoff(client_module):
    server = FakeSystemOneServer(error=(429, "RATE_LIMITED"), failures_first=2).start()
    sleeps: list[float] = []
    try:
        client = make_client(
            client_module, server, sleeper=sleeps.append, jitter=lambda: 0.0
        )
        reply = client.ask("s", {"q": client_module.noul_question("q")})
    finally:
        server.stop()
    assert reply.value("q") is True
    assert server.calls() == 3
    assert sleeps == [0.5, 1.0]


def test_overload_is_retried_then_raised_when_retries_run_out(client_module):
    server = FakeSystemOneServer(error=(529, "OVERLOADED")).start()
    sleeps: list[float] = []
    try:
        client = make_client(
            client_module, server, max_retries=2, sleeper=sleeps.append, jitter=lambda: 0.0
        )
        with pytest.raises(client_module.SystemOneError) as excinfo:
            client.ask("s", {"q": client_module.noul_question("q")})
    finally:
        server.stop()
    assert excinfo.value.code == "OVERLOADED"
    assert server.calls() == 3
    assert sleeps == [0.5, 1.0]


def test_validation_and_auth_errors_are_never_retried(client_module):
    for status, code in ((422, "ValidationError"), (401, "AuthenticationError")):
        server = FakeSystemOneServer(error=(status, code)).start()
        sleeps: list[float] = []
        try:
            client = make_client(client_module, server, sleeper=sleeps.append)
            with pytest.raises(client_module.SystemOneError):
                client.ask("s", {"q": client_module.noul_question("q")})
        finally:
            server.stop()
        assert server.calls() == 1, code
        assert sleeps == [], code


def test_network_failure_is_retried(client_module, no_listener_port):
    sleeps: list[float] = []
    client = make_client(
        client_module,
        base_url=f"http://127.0.0.1:{no_listener_port}",
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )
    with pytest.raises(client_module.SystemOneError):
        client.ask("s", {"q": client_module.noul_question("q")})
    assert sleeps == [0.5, 1.0]


def test_api_key_never_appears_in_an_error_message(client_module):
    secret = "sk-jev-DO-NOT-LEAK"
    server = FakeSystemOneServer(
        error=(401, "AuthenticationError"), error_message=f"bad key {secret}"
    ).start()
    try:
        client = make_client(
            client_module, server, backend="jev", api_key=secret, max_retries=0
        )
        with pytest.raises(client_module.SystemOneError) as excinfo:
            client.ask("s", {"q": client_module.noul_question("q")})
    finally:
        server.stop()
    assert secret not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Client construction, kev serialization, liveness probe
# ---------------------------------------------------------------------------
def test_client_refuses_unknown_backend_and_empty_model(client_module):
    with pytest.raises(ValueError, match="unknown systemone backend"):
        client_module.SystemOneClient(backend="nope", model="m")
    with pytest.raises(ValueError, match="model id"):
        client_module.SystemOneClient(backend="kev", model="  ")
    with pytest.raises(ValueError, match="base_url"):
        client_module.SystemOneClient(backend="kev", model="m", base_url=" ")


def test_kev_requests_are_serialized_one_at_a_time(client_module):
    server = FakeSystemOneServer(delay=0.2).start()
    try:

        def ask() -> None:
            make_client(client_module, server).ask(
                "s", {"q": client_module.noul_question("q")}
            )

        threads = [threading.Thread(target=ask) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
    finally:
        server.stop()
    assert server.calls() == 3
    assert server.max_concurrent == 1


def test_jev_requests_are_not_serialized_by_the_local_lock(client_module):
    server = FakeSystemOneServer(delay=0.25).start()
    try:

        def ask() -> None:
            make_client(
                client_module, server, backend="jev", api_key="sk-jev-test"
            ).ask("s", {"q": client_module.noul_question("q")})

        threads = [threading.Thread(target=ask) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
    finally:
        server.stop()
    assert server.max_concurrent >= 2


def test_probe_endpoint_passes_on_a_listening_socket(client_module, fake_server):
    client_module.probe_endpoint(fake_server.url, timeout=0.5)


def test_probe_endpoint_fails_visibly_on_a_closed_port(client_module, no_listener_port):
    with pytest.raises(client_module.SystemOneError) as excinfo:
        client_module.probe_endpoint(f"http://127.0.0.1:{no_listener_port}", timeout=0.5)
    assert excinfo.value.code == "NETWORK"
    assert "unreachable" in str(excinfo.value)


def test_probe_endpoint_rejects_a_url_without_host(client_module):
    with pytest.raises(client_module.SystemOneError, match="no host"):
        client_module.probe_endpoint("/v1/systemone")


def test_default_catalogs_and_auth_constants(client_module):
    assert client_module.DEFAULT_BASE_URLS["jev"] == "https://api.typesafe.ai"
    assert client_module.DEFAULT_BASE_URLS["kev"] == "http://127.0.0.1:8787"
    assert client_module.DEFAULT_MODELS["jev"] == ("jev-latest",)
    assert set(client_module.DEFAULT_MODELS["kev"]) == {
        "kev-4b@qwen3",
        "kev-8b",
        "kev-9b",
    }
    assert client_module.CLOUD_KEY_ENV == "TYPESAFE_API_KEY"
    assert client_module.AUTH_HEADER == "Authorization"
    assert client_module.AUTH_SCHEME == "Bearer"
    assert client_module.RETRYABLE_STATUS == frozenset({429, 529})
