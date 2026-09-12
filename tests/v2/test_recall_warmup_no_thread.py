"""Regression guards for synchronous recall embedder warm-up."""

from __future__ import annotations

import threading

from phone_agent.v2.recall import SelectionErrorObservers, warmup_embedder


_WARMUP_THREAD = "recall-embedder-warmup"


class _RecordingEmbedder:
    loaded = False

    def __init__(self, calls: list[str], thread_names: list[str]) -> None:
        self.calls = calls
        self.thread_names = thread_names

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        self.thread_names.extend(
            thread.name for thread in threading.enumerate()
        )
        return [[1.0] for _ in texts]


def test_warmup_embeds_before_return_and_never_starts_background_thread() -> None:
    calls: list[str] = []
    thread_names: list[str] = []
    embedder = _RecordingEmbedder(calls, thread_names)
    release = threading.Event()

    def factory():
        # The old implementation returns while this daemon is still inside
        # the factory.  The synchronous contract never enters this branch.
        if threading.current_thread().name == _WARMUP_THREAD:
            release.wait(timeout=2)
        return embedder

    result = warmup_embedder(factory)
    try:
        assert result is None
        assert calls == ["warmup"]
        assert _WARMUP_THREAD not in thread_names
        assert all(
            thread.name != _WARMUP_THREAD for thread in threading.enumerate()
        )
    finally:
        release.set()
        if isinstance(result, threading.Thread):
            result.join(timeout=2)


def test_warmup_failure_is_fail_open_and_observed_before_return() -> None:
    observed: list[tuple[str, str]] = []
    observers = SelectionErrorObservers()
    observers.add(lambda namespace, error_type: observed.append((namespace, error_type)))
    release = threading.Event()

    class _FailingEmbedder:
        loaded = False

        def embed(self, _texts: list[str]) -> list[list[float]]:
            raise RuntimeError("cold embed failed")

    def factory():
        if threading.current_thread().name == _WARMUP_THREAD:
            release.wait(timeout=2)
        return _FailingEmbedder()

    result = warmup_embedder(factory, observers=observers)
    try:
        assert result is None
        assert observed == [("embedder", "RuntimeError")]
    finally:
        release.set()
        if isinstance(result, threading.Thread):
            result.join(timeout=2)
