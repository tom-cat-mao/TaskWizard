"""System One protocol client: typed questions in, calibrated answers out.

Wire contract — **the official API specification, verified against the live
service with a real key** (2026-09-25).  It differs from the blog/third-party
material that this plugin was first written against; the differences are called
out inline because each one was a real defect::

    POST {base_url}/v1/systemone
    Authorization: Bearer <key>          # Jev cloud only; local Kev has no auth
    {"model": "...", "state": <string|object|array>, "questions": {...}}

    -> {"model": "jev-1.13.0",
        "answers": {key: {...}},          # shape per question type, below
        "usage": {"input_tokens": N, "output_tokens": N}}   # always returned

Question types — there are exactly **three** (there is no ``count`` type, and
the boolean type is called ``noul``).  Every question carries ``instructions``
(**not** ``description``) and an optional/required ``criteria`` whose shape is
type-specific::

    noul    {"type": "noul",   "instructions": "...", "criteria": {"true": "...", "false": "..."}}
    choice  {"type": "choice", "instructions": "...", "criteria": {"option A": "desc-or-null", ...}}
    score   {"type": "score",  "instructions": "...", "criteria": ["level 0", "level 1", ...]}

``choice.criteria`` is a **map** of option -> description (or null) — not an
``options`` array — and ``score.criteria`` is an ordered **array** of at least
two level descriptions with **no** ``min``/``max``.  Builders:
:func:`noul_question`, :func:`choice_question`, :func:`score_question`.

Answer shapes and the internal representation this client maps them onto::

    noul    {"type": "noul",   "noul": 0..1}
            -> Answer(kind="noul",   value=p >= 0.5, p=noul)
    choice  {"type": "choice", "choice": str, "confidence": 0..1, "probabilities": {...}}
            -> Answer(kind="choice", value=choice,    p=confidence, probabilities=...)
    score   {"type": "score",  "score": float, "confidence": 0..1, "legend": {...}, ...}
            -> Answer(kind="score",  value=score,     p=confidence, legend=...)

A ``noul`` answer *is* a probability: ``value`` is that probability thresholded
at 0.5 (the canonical decision), while ``p`` keeps the model's calibration — a
caller that wants to be conservative thresholds ``p`` itself, which is exactly
what the safety reviewer's confidence gate does.

``usage`` is always present, so accounting is **per token** (P0 #13's token
unit, not a call count); :func:`usage_tokens` reads it and the plugin records it
under the declared ``systemone`` role.

Failure policy
--------------

Errors arrive as ``{"detail": {"error_type": ..., "message": ...}}`` with the
HTTP status carrying the class: ``401`` (auth), ``422`` (request validation —
the body names the offending field), ``429`` (rate limited), ``529``
(overloaded).  Only **429/529 and transport-level network failures** are
retried, with bounded exponential backoff plus jitter; every other status —
including an unexpected 5xx — is raised immediately, because a rejected request
or a bad key does not become valid by asking again.  ``max_retries`` defaults
to 2.

The API key never leaves this module: it is only ever written into the
``Authorization`` header, never into an exception message or a log line — and
server-supplied error detail is filtered for it before it is surfaced.

This module is a leaf on purpose (stdlib only): the plugin's provider adapter,
reviewer, governor and smoke script all speak the protocol through it, and the
offline suite drives it against a loopback server.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

BACKEND_JEV = "jev"
BACKEND_KEV = "kev"
BACKENDS: tuple[str, ...] = (BACKEND_JEV, BACKEND_KEV)

#: Human-facing base URLs per backend (``PHONE_AGENT_SYSTEMONE_BASE_URL``
#: overrides the selected one; both values are verified facts).
DEFAULT_BASE_URLS: Mapping[str, str] = {
    BACKEND_JEV: "https://api.typesafe.ai",
    BACKEND_KEV: "http://127.0.0.1:8787",
}
#: Verified model catalogs; a ``provider:model`` reference pins the version.
DEFAULT_MODELS: Mapping[str, tuple[str, ...]] = {
    BACKEND_JEV: ("jev-latest",),
    BACKEND_KEV: ("kev-4b@qwen3", "kev-8b", "kev-9b"),
}
#: Environment variable carrying the cloud key.  Deliberately not
#: ``PHONE_AGENT_``-prefixed: it is the backend's own variable name, so it is
#: read directly and registered as a redaction literal by the plugin.
CLOUD_KEY_ENV = "TYPESAFE_API_KEY"
PROTOCOL_PATH = "/v1/systemone"

#: The cloud API authenticates with ``Authorization: Bearer <key>``.  An
#: ``X-API-Key`` header is answered with 403 "Must supply an API key".
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

#: The protocol's documented request timeout band (seconds).
TIMEOUT_BAND: tuple[float, float] = (5.0, 15.0)
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
#: Base delay for the bounded exponential backoff (jitter is additive).
RETRY_BASE_DELAY = 0.5
#: Client-side sanity bound on a ``choice`` criteria map.  This is NOT a
#: protocol fact (the official spec states no cap); it exists so a runaway
#: generator cannot post a megabyte of options.
MAX_CHOICE_OPTIONS = 255
MAX_ERROR_DETAIL = 200

#: The only three question types the API accepts.
QUESTION_TYPES: tuple[str, ...] = ("noul", "choice", "score")

#: Statuses worth retrying: rate limiting and overload.  Everything else
#: (notably 401/422) is terminal — the request itself is wrong, not the moment.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 529})
#: Server status -> protocol code, used when the body carries no error type.
STATUS_CODES: Mapping[int, str] = {
    401: "AUTH",
    422: "SCHEMA_INVALID",
    429: "RATE_LIMITED",
    529: "OVERLOADED",
}

# Errors that mean "the request never got an answer" — retryable by nature.
_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
    socket.timeout,
    OSError,
)


class SystemOneError(RuntimeError):
    """One failed protocol exchange (never carries the API key).

    ``code`` is the protocol-level error class (the server's ``error_type`` when
    present, else derived from the status, else ``NETWORK``); ``retryable``
    records whether this failure was eligible for the bounded retry (and
    therefore whether the client already retried it).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = status
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class Answer:
    """One typed answer in the internal representation.

    ``p`` is the model's calibrated probability in every kind (the ``noul``
    probability, or the ``confidence`` of a choice/score answer); ``value`` is
    the answer itself (a ``bool`` for ``noul``, the chosen option for
    ``choice``, the raw ``float`` for ``score``).
    """

    kind: str
    value: Any
    p: float | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    legend: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemOneReply:
    """A parsed protocol response: model id, answers, usage, measured latency."""

    answers: Mapping[str, Answer]
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def answer(self, key: str) -> Answer:
        """Return one answer by key (``KeyError`` when the server omitted it)."""

        return self.answers[str(key)]

    def value(self, key: str) -> Any:
        return self.answer(key).value

    def probability(self, key: str) -> float | None:
        return self.answer(key).p

    def tokens(self) -> int | None:
        """Total tokens the backend reported for this call, or ``None``."""

        return usage_tokens(self.usage)


def usage_tokens(usage: Mapping[str, Any] | None) -> int | None:
    """Total token count of a protocol ``usage`` block, or ``None``.

    ``total_tokens`` wins when the backend sends it; otherwise it is the sum of
    the reported input/output counts.  A block with no usable number yields
    ``None`` so nothing invented reaches the ledger.
    """

    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    parts = [
        value
        for value in (usage.get("input_tokens"), usage.get("output_tokens"))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    return sum(parts) if parts else None


# ---------------------------------------------------------------------------
# Question builders (validation happens here, not at the server)
# ---------------------------------------------------------------------------
def _require_instructions(instructions: Any) -> str:
    text = str(instructions or "").strip()
    if not text:
        raise ValueError("a systemone question requires non-empty instructions")
    return text


def _require_criteria_map(criteria: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(criteria, Mapping) or not criteria:
        raise ValueError(f"a systemone {label} question requires a non-empty criteria map")
    if len(criteria) > MAX_CHOICE_OPTIONS:
        raise ValueError(
            f"a systemone {label} question carries at most {MAX_CHOICE_OPTIONS} "
            f"criteria entries (client-side bound), got {len(criteria)}"
        )
    return {
        str(key): (None if value is None else str(value))
        for key, value in criteria.items()
    }


def _require_criteria_levels(criteria: Any) -> list[str]:
    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Sequence):
        raise ValueError(
            "a systemone score question requires an ordered criteria list of "
            "level descriptions"
        )
    levels = [str(item) for item in criteria]
    if len(levels) < 2:
        raise ValueError(
            "a systemone score question needs at least two criteria levels, "
            f"got {len(levels)}"
        )
    return levels


def noul_question(instructions: Any, criteria: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a ``noul`` question (the boolean type; criteria optional)."""

    question: dict[str, Any] = {
        "type": "noul",
        "instructions": _require_instructions(instructions),
    }
    if criteria is not None:
        question["criteria"] = _require_criteria_map(criteria, label="noul")
    return question


def choice_question(
    criteria: Mapping[str, Any] | Sequence[str], instructions: Any
) -> dict[str, Any]:
    """Build a ``choice`` question from an option -> description map.

    A plain sequence of option names is accepted as shorthand for "every option
    undescribed" (``{name: None}``).  Option order is part of the question: the
    same options in another order can shift the answer, and the returned
    ``choice`` is matched against these keys.
    """

    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Mapping):
        names = [str(item) for item in (criteria or ())]
        if len(set(names)) != len(names):
            raise ValueError(
                "a systemone choice question cannot repeat an option name "
                "(criteria keys are the options)"
            )
        criteria = dict.fromkeys(names)
    return {
        "type": "choice",
        "instructions": _require_instructions(instructions),
        "criteria": _require_criteria_map(criteria, label="choice"),
    }


def score_question(criteria: Sequence[str], instructions: Any) -> dict[str, Any]:
    """Build a ``score`` question from ordered level descriptions (≥2)."""

    return {
        "type": "score",
        "instructions": _require_instructions(instructions),
        "criteria": _require_criteria_levels(criteria),
    }


def validate_question(key: Any, question: Any) -> dict[str, Any]:
    """Validate + normalize one question entry for the wire (fail-visible)."""

    if not isinstance(question, Mapping):
        raise ValueError(f"systemone question {key!r} must be a mapping")
    kind = str(question.get("type") or "").strip().lower()
    if kind not in QUESTION_TYPES:
        raise ValueError(
            f"systemone question {key!r}: unknown type {kind!r} "
            f"(the API accepts only {', '.join(QUESTION_TYPES)})"
        )
    if kind == "noul":
        return noul_question(question.get("instructions"), question.get("criteria"))
    if kind == "choice":
        return choice_question(question.get("criteria") or {}, question.get("instructions"))
    return score_question(question.get("criteria") or (), question.get("instructions"))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
Transport = Callable[[str, Mapping[str, str], bytes, float], "tuple[int, bytes]"]
"""``(url, headers, body, timeout) -> (status, raw_body)`` — injectable for tests."""

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _local_lock(base_url: str) -> threading.Lock:
    """The per-base-URL serialization lock for a single-slot local server."""

    key = str(base_url)
    with _LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


def _urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # a status we must still read
        return int(exc.code), exc.read()


def probe_endpoint(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Fail visibly when ``base_url`` accepts no TCP connection.

    A cheap liveness check used at **build time** for an explicitly referenced
    local server: the API has no health endpoint, so this only proves a listener
    exists (no request, no auth header, nothing secret is sent).  Raises
    :class:`SystemOneError` with code ``NETWORK`` when unreachable.
    """

    parsed = urllib.parse.urlsplit(str(base_url))
    if not parsed.hostname:
        raise SystemOneError(
            f"systemone base_url {base_url!r} has no host", code="NETWORK"
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection(
            (parsed.hostname, port), timeout=min(2.0, max(0.05, float(timeout)))
        ):
            return
    except OSError as exc:
        raise SystemOneError(
            f"systemone endpoint {parsed.hostname}:{port} is unreachable "
            f"({type(exc).__name__})",
            code="NETWORK",
        ) from None


class SystemOneClient:
    """One backend + model, one transport, one bounded retry policy.

    ``transport`` and ``sleeper`` exist for the offline test suite: the suite
    drives a real local HTTP server through the real code path and records the
    backoff schedule instead of waiting it out.
    """

    def __init__(
        self,
        *,
        backend: str,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: Transport | None = None,
        sleeper: Callable[[float], Any] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        clean_backend = str(backend or "").strip().lower()
        if clean_backend not in BACKENDS:
            raise ValueError(
                f"unknown systemone backend: {backend!r} "
                f"(expected one of {', '.join(BACKENDS)})"
            )
        clean_model = str(model or "").strip()
        if not clean_model:
            raise ValueError("systemone requires a model id")
        resolved_url = str(base_url or DEFAULT_BASE_URLS[clean_backend]).strip()
        if not resolved_url:
            raise ValueError("systemone requires a base_url")
        self.backend = clean_backend
        self.model = clean_model
        self.base_url = resolved_url
        self._api_key = str(api_key).strip() if api_key else ""
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("systemone timeout must be positive")
        self.max_retries = max(0, int(max_retries))
        self._transport: Transport = transport or _urllib_transport
        self._sleeper = sleeper
        self._jitter = jitter
        self.calls = 0

    # -- public API --------------------------------------------------------
    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> SystemOneReply:
        """Ask many typed questions in ONE call; returns the typed answers.

        ``state`` is the unstructured context the model reasons about: the API
        accepts a string, an object or an array and passes it through verbatim.
        """

        _validate_state(state)
        if not questions:
            raise ValueError("systemone requires at least one question")
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                str(key): validate_question(key, value)
                for key, value in questions.items()
            },
        }
        return self._request(body)

    def ask_noul(
        self, state: Any, instructions: Any, criteria: Mapping[str, Any] | None = None
    ) -> Answer:
        """Convenience: one ``noul`` question in one call."""

        return self.ask(state, {"answer": noul_question(instructions, criteria)}).answer(
            "answer"
        )

    def ask_choice(
        self, state: Any, criteria: Mapping[str, Any] | Sequence[str], instructions: Any
    ) -> Answer:
        """Convenience: one ``choice`` question in one call."""

        return self.ask(state, {"answer": choice_question(criteria, instructions)}).answer(
            "answer"
        )

    # -- internals ---------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        """Bounded exponential backoff with additive jitter (attempt is 0-based)."""

        return RETRY_BASE_DELAY * (2**attempt) * (1.0 + float(self._jitter()))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers[AUTH_HEADER] = f"{AUTH_SCHEME} {self._api_key}"
        return headers

    def _redact_detail(self, text: Any) -> str:
        """Bound + key-filter a server-supplied detail string (P0 #6)."""

        detail = str(text or "").replace("\n", " ").strip()[:MAX_ERROR_DETAIL]
        if self._api_key and self._api_key in detail:
            detail = detail.replace(self._api_key, "<redacted>")
        return detail

    def _server_error(self, status: int, payload: Any) -> SystemOneError:
        code = str(STATUS_CODES.get(status, "HTTP"))
        detail = ""
        detail_block = payload.get("detail") if isinstance(payload, Mapping) else None
        if isinstance(detail_block, Mapping):
            code = str(detail_block.get("error_type") or code)
            detail = self._redact_detail(
                detail_block.get("message") or detail_block.get("detail")
            )
        elif detail_block is not None:
            detail = self._redact_detail(detail_block)
        elif isinstance(payload, Mapping) and payload.get("error"):
            # Tolerated shape from earlier protocol revisions: {"error": {...}}.
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or code)
                detail = self._redact_detail(error.get("message"))
            else:
                detail = self._redact_detail(error)
        message = f"systemone request failed: {code} (status {status})"
        if detail:
            message += f": {detail}"
        return SystemOneError(
            message,
            code=code,
            status=int(status),
            retryable=int(status) in RETRYABLE_STATUS,
        )

    def _exchange(self, body: Mapping[str, Any]) -> tuple[int, Any]:
        url = self.base_url.rstrip("/") + PROTOCOL_PATH
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        lock = _local_lock(self.base_url) if self.backend == BACKEND_KEV else None
        if lock is not None:
            lock.acquire()
        try:
            try:
                self.calls += 1
                status, raw = self._transport(url, headers, data, self.timeout)
            except _NETWORK_EXCEPTIONS as exc:
                raise SystemOneError(
                    f"systemone request failed: NETWORK ({type(exc).__name__})",
                    code="NETWORK",
                    retryable=True,
                ) from None
        finally:
            if lock is not None:
                lock.release()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if status >= 400:
                raise SystemOneError(
                    f"systemone request failed: HTTP {status} (unparseable body)",
                    code=str(STATUS_CODES.get(status, "HTTP")),
                    status=int(status),
                    retryable=int(status) in RETRYABLE_STATUS,
                ) from None
            raise SystemOneError(
                "systemone response is not valid json",
                code="SCHEMA_INVALID",
                status=int(status),
            ) from None
        return int(status), payload

    def _request(self, body: Mapping[str, Any]) -> SystemOneReply:
        attempts = self.max_retries + 1
        started = time.perf_counter()
        for attempt in range(attempts):
            try:
                status, payload = self._exchange(body)
                if status >= 400:
                    raise self._server_error(status, payload)
            except SystemOneError as exc:
                if not exc.retryable or attempt >= attempts - 1:
                    raise
                self._sleeper(self._backoff(attempt))
                continue
            return self._parse(
                payload, latency_ms=int((time.perf_counter() - started) * 1000)
            )
        raise SystemOneError(  # pragma: no cover - the loop always returns or raises
            "systemone request failed: retries exhausted",
            code="NETWORK",
            retryable=False,
        )

    def _parse(self, payload: Any, *, latency_ms: int) -> SystemOneReply:
        if not isinstance(payload, Mapping):
            raise SystemOneError(
                "systemone response is not a json object", code="SCHEMA_INVALID"
            )
        if "detail" in payload or "error" in payload:
            raise self._server_error(200, payload)
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, Mapping) or not raw_answers:
            raise SystemOneError(
                "systemone response carries no answers", code="SCHEMA_INVALID"
            )
        answers = {
            str(key): self._parse_answer(key, item)
            for key, item in raw_answers.items()
        }
        usage = payload.get("usage")
        return SystemOneReply(
            answers=answers,
            model=str(payload.get("model") or self.model),
            usage=dict(usage) if isinstance(usage, Mapping) else {},
            latency_ms=int(latency_ms),
        )

    @staticmethod
    def _parse_answer(key: Any, item: Any) -> Answer:
        if not isinstance(item, Mapping):
            raise SystemOneError(
                f"systemone answer {key!r} is not an object", code="SCHEMA_INVALID"
            )
        kind = str(item.get("type") or "").strip().lower()
        if kind not in QUESTION_TYPES:
            raise SystemOneError(
                f"systemone answer {key!r} has unknown type {kind!r}",
                code="SCHEMA_INVALID",
            )
        if kind == "noul":
            probability = _probability(item.get("noul"), key=key, field="noul")
            return Answer(kind=kind, value=probability >= 0.5, p=probability)
        if kind == "choice":
            choice = item.get("choice")
            if not isinstance(choice, str) or not choice:
                raise SystemOneError(
                    f"systemone answer {key!r} has no choice string",
                    code="SCHEMA_INVALID",
                )
            return Answer(
                kind=kind,
                value=choice,
                p=_optional_probability(item.get("confidence"), key=key, field="confidence"),
                probabilities=_probabilities(item.get("probabilities"), key=key),
            )
        value = item.get("score")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SystemOneError(
                f"systemone answer {key!r} has no numeric score", code="SCHEMA_INVALID"
            )
        legend = item.get("legend")
        return Answer(
            kind=kind,
            value=float(value),
            p=_optional_probability(item.get("confidence"), key=key, field="confidence"),
            probabilities=_probabilities(item.get("probabilities"), key=key),
            legend=dict(legend) if isinstance(legend, Mapping) else {},
        )


def _validate_state(state: Any) -> None:
    """The state is a string, object or array; it may not be empty."""

    if state is None:
        raise ValueError("systemone state must not be empty")
    if isinstance(state, str):
        if not state.strip():
            raise ValueError("systemone state must not be empty")
        return
    if isinstance(state, (Mapping, list, tuple)):
        if not state:
            raise ValueError("systemone state must not be empty")
        return
    raise ValueError(
        "systemone state must be a string, object or array, got "
        f"{type(state).__name__}"
    )


def _probability(value: Any, *, key: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemOneError(
            f"systemone answer {key!r} has a non-numeric {field}", code="SCHEMA_INVALID"
        )
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise SystemOneError(
            f"systemone answer {key!r} has {field} outside [0, 1]",
            code="SCHEMA_INVALID",
        )
    return number


def _optional_probability(value: Any, *, key: Any, field: str) -> float | None:
    if value is None:
        return None
    return _probability(value, key=key, field=field)


def _probabilities(value: Any, *, key: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SystemOneError(
            f"systemone answer {key!r} has a malformed probabilities block",
            code="SCHEMA_INVALID",
        )
    cleaned: dict[str, float] = {}
    for name, probability in value.items():
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise SystemOneError(
                f"systemone answer {key!r} has a non-numeric probability for {name!r}",
                code="SCHEMA_INVALID",
            )
        cleaned[str(name)] = float(probability)
    return cleaned


__all__ = [
    "Answer",
    "AUTH_HEADER",
    "AUTH_SCHEME",
    "BACKENDS",
    "BACKEND_JEV",
    "BACKEND_KEV",
    "CLOUD_KEY_ENV",
    "DEFAULT_BASE_URLS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MODELS",
    "DEFAULT_TIMEOUT",
    "MAX_CHOICE_OPTIONS",
    "PROTOCOL_PATH",
    "QUESTION_TYPES",
    "RETRY_BASE_DELAY",
    "RETRYABLE_STATUS",
    "STATUS_CODES",
    "SystemOneClient",
    "SystemOneError",
    "SystemOneReply",
    "TIMEOUT_BAND",
    "choice_question",
    "noul_question",
    "probe_endpoint",
    "score_question",
    "usage_tokens",
    "validate_question",
]
