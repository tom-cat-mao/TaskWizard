"""Shared fixtures and doubles for the in-repo ``systemone`` plugin suite.

Everything here is offline: the fake protocol server binds ``127.0.0.1`` on an
ephemeral port and speaks the real wire format, so the client, the provider
adapter and the reviewer are exercised through the real code path — the HTTP
request is real, only the far end is a double.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.session import FakePhoneSession

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "systemone"
PLUGIN_MODULE_NAME = "_taskwizard_plugin_systemone"

# The statuses the official API documents (verified with a real key).
ERROR_CODES: Mapping[int, str] = {
    401: "AUTH",
    422: "SCHEMA_INVALID",
    429: "RATE_LIMITED",
    529: "OVERLOADED",
}


# ---------------------------------------------------------------------------
# Plugin loading (the harness's path-loader semantics: no sys.modules entry)
# ---------------------------------------------------------------------------
def load_plugin_module() -> Any:
    """Import ``plugins/systemone/plugin.py`` the way the harness does."""

    cached = sys.modules.get(PLUGIN_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        PLUGIN_MODULE_NAME, PLUGIN_DIR / "plugin.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load the systemone plugin module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sibling(name: str) -> Any:
    """Load one plugin sibling through the plugin's own loader."""

    return load_plugin_module().load_sibling(name)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class HarnessConfig(FakeConfig):
    """``FakeConfig`` plus the ``V2Config`` plugin-settings mirror."""

    def __init__(self, **overrides: Any) -> None:
        super().__init__(**overrides)
        self._plugin_settings: dict[str, Any] = {}

    @property
    def plugin_settings(self) -> Mapping[str, Any]:
        return dict(self._plugin_settings)

    def declare_plugin_setting(self, key: str, value: Any) -> None:
        self._plugin_settings[str(key)] = value

    def withdraw_plugin_setting(self, key: str) -> None:
        self._plugin_settings.pop(str(key), None)


class ScriptedPhoneSession(FakePhoneSession):
    """A ``FakePhoneSession`` whose committed frames carry scripted hashes.

    Also carries the two run-scoped collaborators the plugin's listeners look up
    lazily: ``resolution_trace_recorder`` (P0 #6 trace) and ``usage_ledger``.
    """

    def __init__(
        self,
        screen_hashes: Sequence[str] = (),
        *,
        fail_at: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._hashes = list(screen_hashes) or ["h0"]
        self.fail_at = fail_at
        self.observe_count = 0
        self.trace_events: list[dict[str, Any]] = []
        self.usage_ledger: Any = None
        self.resolution_trace_recorder: Callable[..., None] = self._record

    def _record(self, event: str, **payload: Any) -> None:
        self.trace_events.append({"event": event, **payload})

    def events(self, name: str) -> list[dict[str, Any]]:
        return [item for item in self.trace_events if item["event"] == name]

    def observe(self, settle_ms: int | None = None) -> Any:  # type: ignore[override]
        self.observe_count += 1
        if self.fail_at is not None and self.observe_count == self.fail_at:
            raise RuntimeError("observation failed")
        digest = self._hashes[min(self.observe_count - 1, len(self._hashes) - 1)]
        self.screen_seq += 1
        return SimpleNamespace(
            screen_hash=digest,
            screen_seq=self.screen_seq,
            current_app="com.example.app",
            marks=self.marks,
            screenshot_b64="",
        )


class RecordingClient:
    """A client double that records the (state, questions) of every call.

    Answers are built with the **real** ``Answer`` / ``SystemOneReply`` types
    from the protocol client, so a listener unit test cannot drift from the
    transport's own parsing.
    """

    def __init__(
        self,
        reply: Any = None,
        *,
        error: Exception | None = None,
        usage: Mapping[str, int] | None = None,
    ) -> None:
        self.reply = reply
        self.error = error
        self.usage = dict(usage) if usage is not None else {"input_tokens": 21, "output_tokens": 9}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.backend = "kev"
        self.model = "kev-4b@qwen3"

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> Any:
        self.calls.append((str(state), {k: dict(v) for k, v in questions.items()}))
        if self.error is not None:
            raise self.error
        if self.reply is not None:
            return self.reply
        client_module = sibling("client")
        answers: dict[str, Any] = {}
        for key, question in questions.items():
            kind = str(question.get("type") or "noul")
            if kind == "choice":
                options = [str(name) for name in (question.get("criteria") or {})]
                answers[key] = client_module.Answer(
                    kind="choice",
                    value=options[0],
                    p=0.83,
                    probabilities={options[0]: 0.91},
                )
            elif kind == "score":
                answers[key] = client_module.Answer(
                    kind="score",
                    value=0.8,
                    p=0.77,
                    probabilities={"level": 0.77},
                    legend={"level": "high"},
                )
            else:
                answers[key] = client_module.Answer(kind="noul", value=True, p=0.97)
        return client_module.SystemOneReply(
            answers=answers, model="fake-model", usage=self.usage, latency_ms=1
        )


class RecordingLedger:
    """Minimal ``UsageLedger`` double: records (role, tokens) accounting calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def record(
        self, role: str, message_or_none: Any = None, *, estimate_tokens: int | None = None
    ) -> int:
        self.calls.append((str(role), estimate_tokens))
        return int(estimate_tokens or 0)

    def count(self, role: str) -> int:
        return sum(1 for name, _ in self.calls if name == role)


# ---------------------------------------------------------------------------
# Fake protocol server
# ---------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` without the blocking ``getfqdn`` reverse lookup.

    ``HTTPServer.server_bind`` resolves the bound address to a fully qualified
    name; on a machine whose resolver is slow that costs tens of seconds per
    bind.  Nothing in this suite uses ``server_name``.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        fake: FakeSystemOneServer = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            parsed = None
        record = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": parsed,
        }
        with fake.lock:
            fake.requests.append(record)
            fake._inflight += 1
            fake.max_concurrent = max(fake.max_concurrent, fake._inflight)
        try:
            if fake.delay:
                time.sleep(fake.delay)
            status, payload = fake.respond(parsed)
        finally:
            with fake.lock:
                fake._inflight -= 1
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence the test log
        return None


class FakeSystemOneServer:
    """A local server implementing ``POST /v1/systemone`` (official wire shape).

    ``answer`` may be a mapping ``answer_key -> <answer object>`` (used verbatim)
    or a callable ``(key, question) -> <answer object>``.  Answers default to the
    official shapes per question type (``noul`` probability, ``choice`` +
    ``confidence`` + ``probabilities``, ``score`` + ``legend`` + ``confidence``),
    and every response carries ``usage`` and ``model`` because the real API does.

    ``error=(status, error_type)`` answers every request with the official error
    envelope ``{"detail": {"error_type", "message"}}``; ``raw_body`` answers with
    an unparseable body; ``failures_first`` answers the first N requests with
    ``error`` and then succeeds (retry policy tests).  ``require_api_key`` makes
    the server demand ``Authorization: Bearer <key>`` exactly like the cloud
    endpoint does.
    """

    def __init__(
        self,
        *,
        answer: Mapping[str, Any] | Callable[[str, Mapping[str, Any]], Any] | None = None,
        error: tuple[int, str] | None = None,
        error_type: str | None = None,
        error_message: str = "request rejected",
        raw_body: bytes | None = None,
        delay: float = 0.0,
        usage: Mapping[str, Any] | None = None,
        include_usage: bool = True,
        model: str = "jev-test-1.0",
        require_api_key: str | None = None,
        failures_first: int = 0,
    ) -> None:
        self.answer = answer
        self.error = error
        self.error_type = error_type
        self.error_message = error_message
        self.raw_body = raw_body
        self.delay = delay
        self.usage = usage
        self.include_usage = include_usage
        self.model = model
        self.require_api_key = require_api_key
        self.failures_first = failures_first
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.max_concurrent = 0
        self._inflight = 0
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> FakeSystemOneServer:
        httpd = _Server(("127.0.0.1", 0), _Handler)
        httpd.fake = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            # A short poll interval keeps ``shutdown()`` (called by ``stop()`` in
            # every test's teardown) from costing the default 0.5s wake-up.
            target=lambda: httpd.serve_forever(poll_interval=0.02),
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- behaviour ---------------------------------------------------------
    def respond(self, parsed: Any) -> tuple[int, Any]:
        with self.lock:
            index = len(self.requests) - 1  # 0-based index of this request
            last = dict(self.requests[-1]) if self.requests else {}
        if self.raw_body is not None:
            return 200, self.raw_body
        if self.require_api_key is not None:
            expected = f"Bearer {self.require_api_key}"
            if last.get("headers", {}).get("authorization") != expected:
                return 401, self.error_response(
                    "AuthenticationError", "invalid API key"
                )
        if self.error is not None and (
            self.failures_first == 0 or index < self.failures_first
        ):
            status, code = self.error
            return status, self.error_response(self.error_type or code, self.error_message)
        questions = (parsed or {}).get("questions") if isinstance(parsed, Mapping) else None
        if not isinstance(questions, Mapping):
            return 422, self.error_response(
                "ValidationError", "questions: field required"
            )
        if isinstance(self.answer, Mapping):
            # An explicit mapping IS the answers payload (no per-key fallback),
            # so ``{}`` means "answers nothing" and malformed shapes surface.
            answers: dict[str, Any] = {
                str(key): dict(value) for key, value in self.answer.items()
            }
        else:
            answers = {
                key: self.answer_for(str(key), question)
                for key, question in questions.items()
            }
        response: dict[str, Any] = {"model": self.model, "answers": answers}
        if self.include_usage:
            response["usage"] = dict(
                self.usage or {"input_tokens": 128, "output_tokens": 24}
            )
        return 200, response

    def error_response(self, error_type: str, message: str) -> dict[str, Any]:
        """The official error envelope: ``{"detail": {"error_type", "message"}}``."""

        return {"detail": {"error_type": error_type, "message": message}}

    def answer_for(self, key: str, question: Mapping[str, Any]) -> Any:
        if callable(self.answer):
            return self.answer(key, question)
        kind = str(question.get("type") or "noul")
        if kind == "choice":
            options = [str(name) for name in (question.get("criteria") or {})]
            if not options:
                return {"type": "choice", "choice": "", "confidence": 0.5}
            rest = {name: round(0.09 / max(1, len(options) - 1), 4) for name in options[1:]}
            return {
                "type": "choice",
                "choice": options[0],
                "confidence": 0.83,
                "probabilities": {options[0]: 0.91, **rest},
            }
        if kind == "score":
            levels = [str(level) for level in (question.get("criteria") or [])]
            return {
                "type": "score",
                "score": 0.8,
                "confidence": 0.77,
                "probabilities": {
                    levels[min(3, len(levels) - 1)] if levels else "level": 0.77
                },
                "legend": {"level": levels[min(3, len(levels) - 1)] if levels else "level"},
            }
        return {"type": "noul", "noul": 0.95}

    # -- assertions helpers ------------------------------------------------
    def calls(self) -> int:
        with self.lock:
            return len(self.requests)

    def last_body(self) -> Mapping[str, Any]:
        with self.lock:
            return dict(self.requests[-1]["body"])


def closed_port() -> int:
    """A port nothing listens on (bound and released by the OS)."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# Assembly helper
# ---------------------------------------------------------------------------
@dataclass
class Mounted:
    """One assembled mount of the plugin, with the pieces tests reach for."""

    plugin: Any
    ctx: Any
    registry: Any
    bus: Any
    session: Any
    config: Any

    def tools(self) -> dict[str, Any]:
        return {tool.name: tool for tool in self.ctx.tools}

    def listeners(self, event: str) -> list[Any]:
        return list(self.bus._listeners.get(event, []))

    def service(self, name: str, default: Any = None) -> Any:
        return self.ctx.service(name, default)


def mount_systemone(
    *,
    config: Any = None,
    session: Any = None,
    bus: Any = None,
    with_bus: bool = True,
    extra_services: Mapping[str, Any] | None = None,
) -> Mounted:
    """Assemble the plugin next to a stub ``providers`` capability.

    The stub is what makes the plugin's ``deps=("providers",)`` satisfiable
    outside a real agent; it is deliberately inert (no apply hook).
    """

    from phone_agent.v2.capabilities import (
        CapabilityAssemblyContext,
        CapabilityRegistry,
        CapabilitySpec,
        assemble_capabilities,
    )
    from phone_agent.v2.events import EventBus

    plugin = load_plugin_module()
    event_bus = bus if bus is not None else (EventBus() if with_bus else None)
    services: dict[str, Any] = {}
    if event_bus is not None:
        services["event_bus"] = event_bus
    if session is not None:
        services["session"] = session
    if config is not None:
        services["config"] = config
    services.update(extra_services or {})
    ctx = CapabilityAssemblyContext(services)
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("providers", "Model providers", "on"))
    registry.register(plugin.CAPABILITY)
    assemble_capabilities(registry, ctx)
    return Mounted(
        plugin=plugin,
        ctx=ctx,
        registry=registry,
        bus=event_bus,
        session=session,
        config=config,
    )


def tool_call_request(
    name: str, args: Mapping[str, Any] | None = None, call_id: str = "call_1"
) -> Any:
    """A langchain-shaped tool-call request for the ``tool/execute`` waterfall."""

    return SimpleNamespace(
        tool_call={"name": name, "args": dict(args or {}), "id": call_id}
    )


def run_waterfall(listeners: Sequence[Any], request: Any, terminal: Callable[[Any], Any]) -> Any:
    """Drive a ``(request, next)`` onion chain the way the event bus does."""

    chain = list(listeners)

    def make_next(index: int) -> Callable[[Any], Any]:
        if index >= len(chain):
            return terminal

        def _next(current: Any) -> Any:
            return chain[index](current, make_next(index + 1))

        return _next

    return make_next(0)(request)


__all__ = [
    "ERROR_CODES",
    "FakeSystemOneServer",
    "HarnessConfig",
    "Mounted",
    "PLUGIN_DIR",
    "RecordingClient",
    "RecordingLedger",
    "ScriptedPhoneSession",
    "closed_port",
    "load_plugin_module",
    "mount_systemone",
    "run_waterfall",
    "sibling",
    "tool_call_request",
]
