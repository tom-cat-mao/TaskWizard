"""Subprocess reproducer for the cold NumPy import rendezvous."""

from __future__ import annotations

import importlib.util
import json
import platform
import subprocess
import textwrap

import pytest


_HAS_NATIVE_STACK = all(
    importlib.util.find_spec(name) is not None
    for name in ("mlx_embeddings", "sqlite_vec")
)


_RENDEZVOUS = textwrap.dedent(
    r'''
    import importlib
    import importlib.abc
    import importlib.machinery
    import json
    import socket
    import sys
    import threading
    import traceback

    events = []
    lock = threading.Lock()
    linalg_waiting = threading.Event()
    typing_entered = threading.Event()

    def emit(event, **values):
        with lock:
            events.append({"event": event, **values})

    def prohibit(*args, **kwargs):
        raise RuntimeError("network prohibited")

    socket.create_connection = prohibit
    socket.socket.connect = prohibit
    socket.socket.connect_ex = prohibit

    class CoordinatedLoader(importlib.abc.Loader):
        def __init__(self, loader, name):
            self.loader = loader
            self.name = name

        def create_module(self, spec):
            return self.loader.create_module(spec)

        def exec_module(self, module):
            if (
                self.name == "numpy.linalg"
                and threading.current_thread().name == "recall-embedder-warmup"
            ):
                emit("pause_before_real_linalg_exec")
                linalg_waiting.set()
                if not typing_entered.wait(10):
                    emit("typing_rendezvous_not_reached")
            elif self.name == "numpy._typing._array_like":
                emit("real_typing_array_like_enter")
                typing_entered.set()
            self.loader.exec_module(module)

    class CoordinatedFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in ("numpy.linalg", "numpy._typing._array_like"):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = CoordinatedLoader(spec.loader, fullname)
            return spec

    sys.meta_path.insert(0, CoordinatedFinder())

    def attempt(name, function):
        try:
            function()
        except BaseException as error:
            emit(
                "failure",
                name=name,
                error_type=type(error).__name__,
                error=str(error),
                traceback=traceback.format_exc(),
            )
        else:
            emit("success", name=name)

    def embedding():
        from mlx_embeddings import generate, load
        assert callable(generate) and callable(load)

    worker = threading.Thread(
        target=lambda: attempt("mlx_embeddings", embedding),
        name="recall-embedder-warmup",
    )
    worker.start()
    if linalg_waiting.wait(20):
        attempt("sqlite_vec", lambda: importlib.import_module("sqlite_vec"))
    else:
        emit("parent_rendezvous_not_reached")
    worker.join(20)
    if worker.is_alive():
        emit("timeout")

    checks = {}
    def check(name, function):
        try:
            function()
        except BaseException as error:
            checks[name] = {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        else:
            checks[name] = {"ok": True}

    check("numpy", lambda: importlib.import_module("numpy"))
    check("numpy.linalg", lambda: importlib.import_module("numpy.linalg"))
    check("numpy._typing.NDArray", lambda: getattr(
        importlib.import_module("numpy._typing"), "NDArray"
    ))
    print(json.dumps({"events": events, "checks": checks}, ensure_ascii=False))
    '''
)


@pytest.mark.skipif(
    platform.system() != "Darwin" or not _HAS_NATIVE_STACK,
    reason="requires Darwin with mlx_embeddings and sqlite_vec installed",
)
def test_native_import_rendezvous_is_clean_or_has_the_known_numpy_fingerprint():
    completed = subprocess.run(
        [__import__("sys").executable, "-c", _RENDEZVOUS],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    checks = payload["checks"]

    if all(item["ok"] for item in checks.values()):
        return

    fingerprint = any(
        event.get("event") == "failure"
        and event.get("error_type") == "ImportError"
        and "NDArray" in event.get("error", "")
        and "numpy._typing" in event.get("error", "")
        and "partially initialized" in event.get("error", "")
        for event in payload["events"]
    )
    assert fingerprint, json.dumps(payload, ensure_ascii=False)
