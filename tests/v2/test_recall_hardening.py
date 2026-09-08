"""Recall hardening (WP-DISTILL-AUTO, package B): embedder warm-up and
selection-path failure evidence.

Two contracts are covered here:

* **B1** — the shared embedder is warmed on a daemon thread when the recall
  capability mounts in ``on``/``shadow`` mode, and never in ``off`` mode.  A
  warm-up failure is recorded but cannot break assembly or the run.
* **B2** — every exception on the ``episode`` / ``app_alias`` / ``procedure``
  selection path leaves one ``recall_selection_error`` trace event (namespace
  plus error type, no stack trace or message) and bumps one per-namespace
  counter in the observe-only shadow scorecard, without changing the exception
  the caller sees — the fail-open return is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.recall import (
    HashEmbedder,
    VecIndex,
    embedder_needs_warmup,
    update_selection_error_stats,
)
from phone_agent.v2.recall import MlxEmbedder as _MlxEmbedder  # noqa: F401 - patch target

WEATHER = "com.example.weather"


# --- doubles -----------------------------------------------------------


class _RecordingEmbedder:
    """Lazy (``loaded=False``) stand-in for :class:`MlxEmbedder`."""

    def __init__(self, dimension: int = 8, *, fail: bool = False) -> None:
        self.model_id = "warm-v1"
        self.dimension = dimension
        self.loaded = False
        self.fail = fail
        self.calls: list[str] = []
        self._done = threading.Event()

    def embed(self, texts):
        self.calls.extend(str(text) for text in texts)
        self._done.set()
        if self.fail:
            raise RuntimeError("mlx exploded")
        return [[0.0] * self.dimension for _ in texts]

    def wait(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self._done.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._done.is_set()


class _CountingHash(HashEmbedder):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(str(text) for text in texts)
        return super().embed(texts)


class _FaultEmbedder:
    """Same identity as the test index, but every embed blows up."""

    model_id = "hash-v1"
    dimension = 64
    loaded = False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def embed(self, _texts):
        raise RuntimeError("embedder is on fire")


# Per-test isolation root: the autouse fixture below points it at tmp_path so
# no test can ever touch the repo's real memory/ directory (S3 test hygiene).
_THIS_MODULE = sys.modules[__name__]
_TEST_ROOT = Path("memory").parent


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    """Force every config's memory_dir/vec_db under this test's tmp_path."""

    monkeypatch.setattr(_THIS_MODULE, "_TEST_ROOT", tmp_path)


def _config(**overrides) -> SimpleNamespace:
    values = {
        "memory_rag": "shadow",
        "experience_enabled": True,
        "device_id": "serial-1",
        "memory_dir": str(_TEST_ROOT / "memory"),
        # The warm-up only runs for a mounted index; a config without one is
        # the offline/index-less case that must never load a model.
        "vec_db": str(_TEST_ROOT / "vec.db"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(config, *, injector=None, session=None) -> CapabilityAssemblyContext:
    return CapabilityAssemblyContext(
        {
            "config": config,
            "session": session if session is not None else SimpleNamespace(),
            "procedure_injector": injector,
            "recall_run_start": lambda _state: None,
            "recall_run_end": lambda _state: None,
            "experience_run_start": lambda _state: None,
            "experience_run_end": lambda _state: None,
        }
    )


def _assemble(config, **kwargs):
    return assemble_capabilities(
        build_capability_registry(config), _context(config, **kwargs)
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _collect_errors(index: VecIndex) -> list[tuple[str, str]]:
    """Register a ``(namespace, error_type)`` collector on this index's scope.

    Observers are instance-scoped (S3): registering on the index — or on a
    capability mount's shared registry — is the only way to observe, so two
    coexisting contexts can no longer cross-notify each other.
    """

    observed: list[tuple[str, str]] = []
    index.selection_error_observers.add(
        lambda namespace, error_type: observed.append((namespace, error_type))
    )
    return observed


# --- B1: embedder warm-up ----------------------------------------------


@pytest.mark.parametrize("mode", ["on", "shadow"])
def test_warmup_runs_in_background_for_on_and_shadow(mode):
    embedder = _RecordingEmbedder()
    ctx = _assemble(
        _config(memory_rag=mode),
        injector=SimpleNamespace(embedder_factory=lambda: embedder),
    )

    assert embedder.wait() is True
    assert embedder.calls == ["warmup"]
    # Warm-up owns nothing on the assembly ledger: no new service/tool/hook.
    assert ctx.service("recall_event_disposers")


def test_warmup_is_skipped_when_recall_is_off():
    embedder = _RecordingEmbedder()
    _assemble(
        _config(memory_rag="off"),
        injector=SimpleNamespace(embedder_factory=lambda: embedder),
    )

    time.sleep(0.2)
    assert embedder.calls == []
    assert embedder.wait(0.05) is False


def test_warmup_is_skipped_without_a_configured_index():
    """Offline commands and index-less mounts never pay for a model load."""

    embedder = _RecordingEmbedder()
    _assemble(
        _config(memory_rag="on", vec_db=None),
        injector=SimpleNamespace(embedder_factory=lambda: embedder),
    )

    time.sleep(0.2)
    assert embedder.calls == []


def test_warmup_also_reads_the_private_factory_name():
    """The capability duck-types the factory off an object it does not own."""

    embedder = _RecordingEmbedder()
    _assemble(
        _config(memory_rag="on"),
        injector=SimpleNamespace(**{"_embedder_factory": lambda: embedder}),
    )
    assert embedder.wait() is True
    assert embedder.calls == ["warmup"]


def test_warmup_without_a_reachable_factory_is_silent():
    # No procedure injector mounted (e.g. a plugin replaced the capability).
    _assemble(_config(memory_rag="on"), injector=None)


def test_deterministic_hash_embedder_is_never_warmed():
    embedder = _CountingHash(8)
    _assemble(
        _config(memory_rag="on"),
        injector=SimpleNamespace(embedder_factory=lambda: embedder),
    )
    time.sleep(0.2)

    assert embedder_needs_warmup(embedder) is False
    assert embedder.calls == []


def test_warmup_failure_is_recorded_and_never_breaks_assembly(tmp_path):
    events: list[dict] = []
    embedder = _RecordingEmbedder(fail=True)
    session = SimpleNamespace(
        resolution_trace_recorder=lambda event, **payload: events.append(
            {"event": event, **payload}
        )
    )
    _assemble(
        _config(memory_rag="shadow", memory_dir=str(tmp_path / "memory")),
        injector=SimpleNamespace(embedder_factory=lambda: embedder),
        session=session,
    )

    # The warm-up records the failure after the embed call returns, so wait for
    # the evidence instead of for the call itself.
    assert _wait_until(lambda: bool(events)) is True
    assert events == [
        {
            "event": "recall_selection_error",
            "namespace": "embedder",
            "error_type": "RuntimeError",
        }
    ]
    # The warm-up namespace is trace-only: no scorecard counter, no file.
    assert not (tmp_path / "memory" / "experience" / "recall_stats.json").exists()


# --- B2: selection-path failure evidence --------------------------------


def _index(tmp_path: Path, *, episode: bool = False) -> Path:
    db_path = tmp_path / "vec.db"
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        if episode:
            index.index_episode(
                {
                    "type": "episode_outcome",
                    "schema_v": 1,
                    "run_id": "run-1",
                    "goal_text": "打开天气应用查预报",
                    "apps": [WEATHER],
                    "steps": 4,
                    "success": True,
                    "reason": "finished",
                    "device_scope": "device:serial-1",
                    "ts_end": 1.0,
                }
            )
    return db_path


def _alias_index(tmp_path: Path) -> Path:
    """An index holding one App-KB alias, so the vector route runs for real."""

    db_path = tmp_path / "alias.db"
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        index.upsert(
            namespace="app_alias",
            ref_id="alias:weather",
            text="天气 com.example.weather",
            metadata={
                "device_scope": "global",
                "app_package": WEATHER,
                "term": "天气",
                "label": "天气",
                "kind": "learned",
                "semantic_eligible": True,
            },
        )
    return db_path


def test_episode_selection_error_is_recorded_before_the_caller_fails_open(
    tmp_path,
):
    with VecIndex(_index(tmp_path, episode=True), embedder=_FaultEmbedder()) as index:
        observed = _collect_errors(index)
        with pytest.raises(RuntimeError):
            index.recall("天气应用查预报", device_scope="device:serial-1")

    # Fail-open semantics are unchanged: the caller still sees the exception,
    # and the failure is attributable before it swallows it.
    assert observed == [("episode", "RuntimeError")]


def test_app_alias_selection_error_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "phone_agent.v2.names.mention_occurs",
        lambda _term, _query: (_ for _ in ()).throw(RuntimeError("names exploded")),
    )
    with VecIndex(_index(tmp_path), embedder=HashEmbedder(64)) as index:
        observed = _collect_errors(index)
        with pytest.raises(RuntimeError):
            index.recall("打开微信", device_scope="device:serial-1")

    assert observed == [("app_alias", "RuntimeError")]


def test_app_alias_vector_route_error_is_recorded_before_names_swallows_it(
    tmp_path,
):
    """The names.py embedding route records its failure before the caller's
    fail-open ``except Exception: embedded = ()`` discards it."""

    from phone_agent.v2.names import resolve_name

    db_path = _alias_index(tmp_path)
    with VecIndex(db_path, embedder=_FaultEmbedder()) as index:
        observed = _collect_errors(index)

        def embedding_search(query, top_k):
            return index.app_name_vector_candidates(
                query, device_scope="device:serial-1", top_k=top_k
            )

        result = resolve_name(
            "微信",
            registry=(),
            embedding_search=embedding_search,
        )

    # names.py's fail-open swallow is untouched: no exception escapes and the
    # resolution still returns, but the failure left evidence first.
    assert result.status in {"ambiguous", "unknown"}
    assert observed == [("app_alias", "RuntimeError")]


def test_app_alias_vector_route_wrap_preserves_healthy_results(tmp_path):
    """Wrap-only change: real hits and short-circuits behave as before."""

    with VecIndex(_alias_index(tmp_path), embedder=HashEmbedder(64)) as index:
        hits = index.app_name_vector_candidates("天气", device_scope="device:serial-1")
        assert [hit["package"] for hit in hits] == [WEATHER]
        # An empty query/scope short-circuits before the embedder is reached.
        assert index.app_name_vector_candidates("", device_scope="device:serial-1") == []
        assert index.app_name_vector_candidates("天气", device_scope="") == []


def test_procedure_selection_error_is_recorded(tmp_path):
    with VecIndex(_index(tmp_path), embedder=_FaultEmbedder()) as index:
        observed = _collect_errors(index)
        with pytest.raises(RuntimeError):
            index.select_procedure("天气应用查预报", device_id="serial-1")

    assert observed == [("procedure", "RuntimeError")]


def test_error_stats_counter_is_per_namespace_and_only_extends(tmp_path):
    stats_path = tmp_path / "memory" / "experience" / "recall_stats.json"
    update_selection_error_stats(stats_path, "episode")
    update_selection_error_stats(stats_path, "procedure")
    update_selection_error_stats(stats_path, "procedure")
    stats = json.loads(stats_path.read_text(encoding="utf-8"))

    assert stats["episode_errors"] == 1
    assert stats["procedure_errors"] == 2
    assert "app_alias_errors" not in stats
    assert stats["schema_v"] == 2


def test_unknown_namespace_records_no_counter(tmp_path):
    stats_path = tmp_path / "memory" / "experience" / "recall_stats.json"
    assert update_selection_error_stats(stats_path, "embedder") == {}
    assert not stats_path.exists()


def test_release_of_the_recall_capability_removes_the_observer(tmp_path):
    events: list[dict] = []
    session = SimpleNamespace(
        resolution_trace_recorder=lambda event, **payload: events.append(
            {"event": event, **payload}
        )
    )
    ctx = _assemble(
        _config(memory_rag="shadow", memory_dir=str(tmp_path / "memory")),
        session=session,
    )

    # Production wiring: an index opened by this mount reports to the mount's
    # scoped registry, so the capability observer sees its failures.
    mount_observers = ctx.service("recall_selection_observers")

    def failing_recall() -> None:
        with VecIndex(
            _index(tmp_path, episode=True),
            embedder=_FaultEmbedder(),
            selection_error_observers=mount_observers,
        ) as index:
            with pytest.raises(RuntimeError):
                index.recall("天气应用查预报", device_scope="device:serial-1")

    failing_recall()
    assert [item["namespace"] for item in events] == ["episode"]

    # Switching the capability off must leave zero residue (P0 #18): the
    # observer is disposed, so a later failure is silent again.
    assemble_capabilities(build_capability_registry(_config(memory_rag="off")), ctx)
    failing_recall()
    assert [item["namespace"] for item in events] == ["episode"]
    stats_path = tmp_path / "memory" / "experience" / "recall_stats.json"
    assert json.loads(stats_path.read_text(encoding="utf-8"))["episode_errors"] == 1


def test_coexisting_mounts_never_cross_notify_each_other(tmp_path):
    """Two mounted recall capabilities observe only their own scopes (S3)."""

    def mount():
        events: list[dict] = []
        session = SimpleNamespace(
            resolution_trace_recorder=lambda event, **payload: events.append(
                {"event": event, **payload}
            )
        )
        ctx = _assemble(_config(memory_rag="shadow"), session=session)
        return events, ctx

    events_a, ctx_a = mount()
    events_b, ctx_b = mount()

    def failing_recall(observers) -> None:
        with VecIndex(
            _index(tmp_path, episode=True),
            embedder=_FaultEmbedder(),
            selection_error_observers=observers,
        ) as index:
            with pytest.raises(RuntimeError):
                index.recall("天气应用查预报", device_scope="device:serial-1")

    failing_recall(ctx_a.service("recall_selection_observers"))
    # Only A's observer fired; B's mount saw nothing of A's failure.
    assert [item["namespace"] for item in events_a] == ["episode"]
    assert events_b == []
    failing_recall(ctx_b.service("recall_selection_observers"))
    assert [item["namespace"] for item in events_b] == ["episode"]
    assert [item["namespace"] for item in events_a] == ["episode"]


# --- B3 integration: a faulty embedder leaves the run intact ------------


def _mini_config(monkeypatch, tmp_path: Path, *, mode: str):
    from tests.v2.test_experience import _install_mini_agent_modules

    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    config.trace_enabled = True
    config.memory_rag = mode
    config.memory_dir = str(tmp_path / "memory")
    config.vec_db = str(tmp_path / "vec.db")
    config.embed_model = "hash-v1"
    config.embed_dim = 64
    config.lessons_dir = str(tmp_path / "lessons")
    return config


def _install_fault_embedder(monkeypatch) -> None:
    monkeypatch.setattr("phone_agent.v2.recall.MlxEmbedder", _FaultEmbedder)


def _trace_events(agent) -> list[dict]:
    path = Path(agent.trace_path)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


def test_shadow_run_survives_a_faulty_embedder_and_leaves_evidence(
    tmp_path, monkeypatch
):
    _index(tmp_path, episode=True)
    config = _mini_config(monkeypatch, tmp_path, mode="shadow")
    _install_fault_embedder(monkeypatch)

    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(config)
    assert agent.run("打开天气应用查预报").success is True

    pairs = [
        (event["namespace"], event["error_type"])
        for event in _trace_events(agent)
        if event.get("event") == "recall_selection_error"
    ]
    # The warm-up namespace may (or may not, depending on thread timing) also
    # report the same broken embedder; the episode failure is what must land.
    assert ("episode", "RuntimeError") in pairs
    assert {namespace for namespace, _ in pairs} <= {"episode", "embedder"}
    # No stack trace, message, or caller text crosses the redaction boundary.
    assert "embedder is on fire" not in str(pairs)
    stats = json.loads(
        (tmp_path / "memory" / "experience" / "recall_stats.json").read_text(
            encoding="utf-8"
        )
    )
    assert stats["episode_errors"] == 1


def test_on_run_survives_a_faulty_procedure_selection(tmp_path, monkeypatch):
    _index(tmp_path)
    config = _mini_config(monkeypatch, tmp_path, mode="on")
    _install_fault_embedder(monkeypatch)

    from phone_agent.v2.agent import ThinPhoneAgent

    agent = ThinPhoneAgent(config)
    assert agent.run("打开天气应用查预报").success is True

    assert ("procedure", "RuntimeError") in [
        (event["namespace"], event["error_type"])
        for event in _trace_events(agent)
        if event.get("event") == "recall_selection_error"
    ]
    stats = json.loads(
        (tmp_path / "memory" / "experience" / "recall_stats.json").read_text(
            encoding="utf-8"
        )
    )
    assert stats["procedure_errors"] == 1
