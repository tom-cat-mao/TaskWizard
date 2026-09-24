#!/usr/bin/env python3
"""One real System One call, for a human to run against the live service.

The offline suite proves the plugin speaks the protocol; this script proves the
*service* answers it.  It asks one typed question (``noul`` by default, the
official boolean type) and prints the typed answers, the reported usage and the
measured latency as one JSON line::

    TYPESAFE_API_KEY=... python3 plugins/systemone/smoke.py
    {"ok": true, "backend": "jev", "model": "jev-1.13.0",
     "answers": {"answer": {"kind": "noul", "value": true, "p": 0.95, ...}},
     "usage": {"input_tokens": 333, "output_tokens": 50}, "tokens": 383,
     "latency_ms": 812}

Both backends work: ``--backend kev`` talks to a local server (no key, and its
requests are serialized by the client), ``--backend jev`` (default) to the cloud
endpoint with the key from ``TYPESAFE_API_KEY`` sent as
``Authorization: Bearer <key>``.  The key is read from the environment only and
never printed — not even on an error path (the client filters it out of
server-supplied error detail).

``--kind choice`` / ``--kind score`` exercise the other two question types, so
the three shapes can be verified against the live service in one sitting.

The module is stdlib-only and loads ``client.py`` by path, so the smoke test
runs without the repository's virtualenv being active.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_DIR = Path(__file__).resolve().parent
DEFAULT_STATE = (
    "tool: tap\n"
    "target: 「确认支付」 button on a checkout page, amount 100 CNY"
)
CHOICE_CRITERIA = {
    "proceed": "continue with the action",
    "stop": "abandon the action",
}
SCORE_LEVELS = (
    "no risk at all",
    "low risk",
    "moderate risk",
    "high risk",
    "critical risk",
)


def load_client_module():
    """Import the sibling ``client.py`` without importing the harness.

    The module is registered in ``sys.modules`` *before* execution (the same
    discipline as ``plugin.load_sibling``): a module loaded by
    ``spec_from_file_location`` is otherwise invisible to ``sys.modules``, and
    dataclass/generic machinery resolves a class's own module through it.
    """

    name = "_taskwizard_systemone_smoke_client"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _DIR / "client.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {_DIR / 'client.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def answer_payload(answer) -> dict:
    """One answer as JSON: the internal kind/value/probability plus its detail."""

    payload = {"kind": answer.kind, "value": answer.value, "p": answer.p}
    if answer.probabilities:
        payload["probabilities"] = dict(answer.probabilities)
    if answer.legend:
        payload["legend"] = dict(answer.legend)
    return payload


def build_question(client, kind: str, instructions: str) -> dict:
    if kind == "choice":
        return client.choice_question(CHOICE_CRITERIA, instructions)
    if kind == "score":
        return client.score_question(list(SCORE_LEVELS), instructions)
    return client.noul_question(instructions)


def main(argv: list[str] | None = None) -> int:
    client = load_client_module()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--backend",
        default=client.BACKEND_JEV,
        choices=list(client.BACKENDS),
        help="jev (cloud, needs TYPESAFE_API_KEY) or kev (local server)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model id; defaults to the backend catalog's first entry",
    )
    parser.add_argument(
        "--kind",
        default="noul",
        choices=list(client.QUESTION_TYPES),
        help="which of the three question types to ask",
    )
    parser.add_argument("--state", default=DEFAULT_STATE, help="the state to ask about")
    parser.add_argument(
        "--question",
        default="Does this state warrant a yes?",
        help="the question instructions",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="override the backend's base URL (e.g. a local server on another port)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=client.DEFAULT_TIMEOUT,
        help=(
            "request timeout in seconds "
            f"({client.TIMEOUT_BAND[0]:g}-{client.TIMEOUT_BAND[1]:g})"
        ),
    )
    args = parser.parse_args(argv)

    api_key = str(os.getenv(client.CLOUD_KEY_ENV) or "").strip()
    if args.backend == client.BACKEND_JEV and not api_key:
        print(
            f"error: {client.CLOUD_KEY_ENV} is not set; export it or use "
            f"--backend {client.BACKEND_KEV}",
            file=sys.stderr,
        )
        return 2
    model = args.model or client.DEFAULT_MODELS[args.backend][0]

    instance = client.SystemOneClient(
        backend=args.backend,
        model=model,
        base_url=args.base_url,
        api_key=api_key or None,
        timeout=args.timeout,
        max_retries=client.DEFAULT_MAX_RETRIES,
    )
    question = build_question(client, args.kind, args.question)
    started = time.perf_counter()
    try:
        reply = instance.ask(args.state, {"answer": question})
    except client.SystemOneError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "backend": args.backend,
                    "model": model,
                    "code": exc.code,
                    "status": exc.status,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    answers = {
        key: answer_payload(answer) for key, answer in reply.answers.items()
    }
    print(
        json.dumps(
            {
                "ok": True,
                "backend": args.backend,
                "model": reply.model,
                "answers": answers,
                "usage": dict(reply.usage),
                "tokens": reply.tokens(),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
