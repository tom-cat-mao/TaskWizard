"""Fake-only coverage for the WP-I2 shadow recall layer."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.appkb import AppKnowledgeStore
from phone_agent.v2.middleware.trace import TraceMiddleware
from phone_agent.v2.recall import (
    HashEmbedder,
    MlxEmbedder,
    VecIndex,
    evaluate_recall,
    extract_launched_apps,
    read_episode_events,
    rebuild_index,
    update_recall_stats,
)
from phone_agent.v2.config import V2Config


def _episode(
    run_id: str,
    goal: str,
    app: str,
    *,
    scope: str = "device:serial-1",
    timestamp: float = 1_800_000_000.0,
) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": timestamp - 10,
        "ts_end": timestamp,
        "time_of_day": "morning",
        "day_of_week": 0,
        "device_scope": scope,
        "goal_text": goal,
        "apps": [app],
        "success": True,
        "reason": "done",
        "steps": 3,
        "tokens_total": 100,
        "tokens_by_role": {},
        "warnings": 0,
        "takeover": None,
        "verifier": "pass",
    }


def _write_events(path: Path, *events: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def _alias(term: str, label: str, package: str, *, scope: str = "global") -> dict:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    return {
        "term": term,
        "label": label,
        "package": package,
        "kind": "alias",
        "scope": scope,
        "confidence": 0.9,
        "success_count": 1,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "stale": False,
    }


def test_mlx_embedder_is_lazy_without_importing_or_loading_model():
    embedder = MlxEmbedder("fake/model", dimension=8)
    assert embedder.loaded is False
    assert embedder.model_id == "fake/model"


def test_memory_rag_illegal_value_falls_back_to_shadow(monkeypatch):
    # Default values live in test_config.py::test_from_env_defaults (single
    # dataclass comparison); here only the illegal-value fallback contract.
    monkeypatch.delenv("PHONE_AGENT_MEMORY_RAG", raising=False)
    monkeypatch.setenv("PHONE_AGENT_MEMORY_RAG", "unsupported")
    assert V2Config.from_env().memory_rag == "shadow"


def test_episode_reader_consumes_only_shared_schema_v1(tmp_path):
    events_path = tmp_path / "events.jsonl"
    valid = _episode("valid", "打开微信", "com.tencent.mm")
    _write_events(
        events_path,
        valid,
        {**valid, "run_id": "wrong-version", "schema_v": 2},
        {"type": "something_else", "schema_v": 1},
    )
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")

    assert read_episode_events(events_path) == [valid]


def test_episode_and_app_alias_upsert_and_rebuild(tmp_path):
    memory = tmp_path / "memory"
    events_path = memory / "experience/events.jsonl"
    _write_events(events_path, _episode("run-1", "打开微信", "com.tencent.mm"))
    store = AppKnowledgeStore(str(memory))
    store.upsert(_alias("工作聊天", "微信", "com.tencent.mm"))
    with VecIndex(memory / "vec.db", embedder=HashEmbedder(64)) as index:
        assert index.index_episodes(events_path) == {"episodes": 1}
        assert index.index_episodes(events_path) == {"episodes": 1}
        assert index.index_app_aliases(memory_dir=memory, store=store) == {
            "app_aliases": 1
        }
        assert index.index_app_aliases(memory_dir=memory, store=store) == {
            "app_aliases": 1
        }
        assert index.count() == 2
    config = SimpleNamespace(
        vec_db=str(memory / "vec.db"),
        memory_dir=str(memory),
        embed_model="hash-v1",
        embed_dim=64,
    )

    first = rebuild_index(config, embedder=HashEmbedder(64), app_store=store)
    second = rebuild_index(config, embedder=HashEmbedder(64), app_store=store)

    assert first["episodes"] == second["episodes"] == 1
    assert first["app_aliases"] == second["app_aliases"] == 1
    assert second["total"] == 2


def test_embed_model_device_scope_and_revoked_hard_filters(tmp_path):
    db = tmp_path / "vec.db"
    now = 1_800_000_000.0
    with VecIndex(db, embedder=HashEmbedder(64, "model-a")) as index:
        index.upsert(
            namespace="episode",
            ref_id="keep",
            text="打开微信发送消息",
            metadata={
                "apps": ["com.tencent.mm"],
                "device_scope": "device:one",
                "ts": now,
            },
        )
        index.upsert(
            namespace="episode",
            ref_id="other-device",
            text="打开微信发送消息",
            metadata={
                "apps": ["com.tencent.mm"],
                "device_scope": "device:two",
                "ts": now,
            },
        )
        index.upsert(
            namespace="episode",
            ref_id="revoked",
            text="打开微信发送消息",
            metadata={
                "apps": ["com.tencent.mm"],
                "device_scope": "device:one",
                "ts": now,
                "revoked": True,
            },
        )

    with VecIndex(db, embedder=HashEmbedder(64, "model-b")) as other:
        other.upsert(
            namespace="episode",
            ref_id="other-model",
            text="打开微信发送消息",
            metadata={
                "apps": ["com.tencent.mm"],
                "device_scope": "device:one",
                "ts": now,
            },
        )

    with VecIndex(db, embedder=HashEmbedder(64, "model-a")) as index:
        recalled = index.recall(
            "打开微信发送消息",
            device_scope="device:one",
            min_score=0.0,
            now=now,
            namespaces=("episode",),
        )
    assert [candidate["ref_id"] for candidate in recalled] == ["keep"]


def test_global_alias_is_semantic_mirror_for_current_device(tmp_path):
    memory = tmp_path / "memory"
    store = AppKnowledgeStore(str(memory))
    store.upsert(_alias("工作聊天", "微信", "com.tencent.mm"))
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.index_app_aliases(memory_dir=memory, store=store)
        recalled = index.recall(
            "工作聊天",
            device_scope="device:any-phone",
            min_score=0.0,
            now=1_800_000_000.0,
        )
    assert recalled[0]["namespace"] == "app_alias"
    assert recalled[0]["metadata"]["app_package"] == "com.tencent.mm"


def test_threshold_top_k_and_time_decay(tmp_path):
    now = 1_800_000_000.0
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        for ref_id, timestamp in (
            ("new", now),
            ("old", now - 100 * 86_400),
            ("third", now - 10 * 86_400),
        ):
            index.upsert(
                namespace="episode",
                ref_id=ref_id,
                text="打开微信发送消息",
                metadata={
                    "apps": ["com.tencent.mm"],
                    "device_scope": "device:one",
                    "ts": timestamp,
                },
            )
        ranked = index.recall(
            "打开微信发送消息",
            device_scope="device:one",
            top_k=2,
            min_score=0.0,
            decay_lambda=0.02,
            now=now,
            namespaces=("episode",),
        )
        empty = index.recall(
            "完全不相关的问题",
            device_scope="device:one",
            min_score=0.99,
            now=now,
            namespaces=("episode",),
        )

    assert len(ranked) == 2
    assert ranked[0]["ref_id"] == "new"
    assert ranked[0]["score"] == next(
        candidate["score"]
        for candidate in index_results(tmp_path, now)
        if candidate["ref_id"] == "old"
    )
    assert empty == []


def index_results(tmp_path, now):
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        return index.recall(
            "打开微信发送消息",
            device_scope="device:one",
            top_k=5,
            min_score=0.0,
            decay_lambda=0.02,
            now=now,
            namespaces=("episode",),
        )


def test_evaluation_stats_and_successful_launch_extraction(tmp_path):
    recalled = [
        {
            "ref_id": "r1",
            "metadata": {"apps": ["com.tencent.mm"]},
        }
    ]
    hit = evaluate_recall(recalled, {"com.tencent.mm"})
    false_hit = evaluate_recall(recalled, {"com.taobao.taobao"})
    mixed = evaluate_recall(
        [
            *recalled,
            {
                "ref_id": "r2",
                "metadata": {"apps": ["com.taobao.taobao"]},
            },
        ],
        {"com.tencent.mm"},
    )
    assert hit["hit"] is True and hit["false_hit"] is False
    assert false_hit["hit"] is False and false_hit["false_hit"] is True
    assert mixed["hit"] is True and mixed["false_hit"] is True
    assert extract_launched_apps(
        [{"type": "text", "text": "OK. launched 微信 (com.tencent.mm)"}]
    ) == {"com.tencent.mm"}
    assert extract_launched_apps("error: launched 微信 (com.tencent.mm)") == set()

    stats_path = tmp_path / "recall_stats.json"
    first = update_recall_stats(stats_path, hit, run_id="run-1")
    second = update_recall_stats(stats_path, false_hit, run_id="run-2")
    assert first["hit_rate"] == 1.0
    assert second["evaluations"] == 2
    assert second["hit_rate"] == 0.5
    assert second["false_hit_rate"] == 0.5


def test_trace_tracks_only_successful_launch_receipt(tmp_path):
    middleware = TraceMiddleware("launches", trace_dir=str(tmp_path))
    request = SimpleNamespace(
        tool_call={"name": "launch_app", "args": {"app_name": "微信"}}
    )
    middleware.on_tool_execute(
        request,
        lambda _request: SimpleNamespace(
            content=[
                {"type": "text", "text": "OK. launched 微信 (com.tencent.mm)"}
            ]
        ),
    )
    middleware.on_tool_execute(
        request, lambda _request: SimpleNamespace(content="error: launch failed")
    )
    assert middleware.launched_apps == {"com.tencent.mm"}


def test_shadow_start_writes_trace_without_changing_model_messages(tmp_path, monkeypatch):
    candidates = [
        {
            "ref_id": "r1",
            "score": 0.8,
            "match_reasons": ["vector=0.900"],
            "metadata": {"apps": ["com.tencent.mm"]},
        }
    ]

    class FakeIndex:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def recall(self, *_args, **_kwargs):
            return candidates

    monkeypatch.setattr("phone_agent.v2.recall.VecIndex", FakeIndex)
    middleware = TraceMiddleware("shadow", trace_dir=str(tmp_path))
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(
        memory_rag="shadow",
        device_id="serial-1",
        embed_model="fake/model",
        embed_dim=64,
        vec_db=str(tmp_path / "vec.db"),
        recall_top_k=5,
        recall_min_score=0.35,
        recall_decay_lambda=0.02,
    )
    agent.session = SimpleNamespace(
        observe=lambda: SimpleNamespace(
            screenshot_b64="", current_app="launcher", screen_seq=1, marks=[]
        )
    )
    agent._trace = middleware
    agent._system_prompt = "SYSTEM"
    before = agent._initial_messages("打开微信")

    agent._shadow_recall_start("打开微信")
    after = agent._initial_messages("打开微信")

    assert [message.content for message in after] == [message.content for message in before]
    assert agent._system_prompt == "SYSTEM"
    events = [
        json.loads(line)
        for line in (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["event"] == "run_start"
    assert events[0]["memory_rag"]["candidates"][0]["ref_id"] == "r1"


def test_shadow_finish_writes_evaluation_and_cumulative_stats(tmp_path):
    middleware = TraceMiddleware("shadow-finish", trace_dir=str(tmp_path / "trace"))
    middleware._launched_apps.add("com.tencent.mm")
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(memory_dir=str(tmp_path / "memory"))
    agent.run_id = "shadow-finish"
    agent._trace = middleware
    agent._shadow_recall_ready = True
    agent._shadow_candidates = [
        {"ref_id": "r1", "metadata": {"apps": ["com.tencent.mm"]}}
    ]

    agent._shadow_recall_finish()

    stats = json.loads(
        (tmp_path / "memory/experience/recall_stats.json").read_text(encoding="utf-8")
    )
    assert stats["evaluations"] == 1
    assert stats["hit_rate"] == 1.0
    events = [
        json.loads(line)
        for line in (tmp_path / "trace/shadow-finish.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event"] == "recall_evaluation"
    assert events[-1]["evaluation"]["hit"] is True
