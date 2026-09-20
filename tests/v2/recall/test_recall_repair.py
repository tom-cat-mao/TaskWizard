"""Regression coverage for the WP-R2 recall repair package."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
from types import SimpleNamespace

import pytest

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.appkb import AppKnowledgeStore
from phone_agent.v2.config import V2Config
from phone_agent.v2.recall import (
    HashEmbedder,
    VecIndex,
    _keyword_score,
    evaluate_recall,
    incremental_upsert,
    reconcile_index,
    update_recall_stats,
)


def _episode(run_id: str, *, goal: str = "打开淘宝", steps: int = 3) -> dict:
    return {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": run_id,
        "ts_start": 1_800_000_000.0,
        "ts_end": 1_800_000_010.0,
        "time_of_day": "morning",
        "day_of_week": 0,
        "device_scope": "device:serial-1",
        "goal_text": goal,
        "apps": ["com.taobao.taobao"],
        "success": True,
        "reason": "finished",
        "steps": steps,
        "tokens_total": 10,
        "tokens_by_role": {},
        "warnings": 0,
        "takeover": None,
        "verifier": "pass",
    }


def _alias(
    term: str,
    label: str,
    package: str,
    *,
    kind: str = "learned",
    scope: str = "global",
    success_count: int = 1,
    stale: bool = False,
) -> dict:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    return {
        "term": term,
        "label": label,
        "package": package,
        "kind": kind,
        "scope": scope,
        "confidence": 0.9,
        "success_count": success_count,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "last_success": timestamp,
        "stale": stale,
    }


def _config(tmp_path, *, min_steps: int = 2):
    return SimpleNamespace(
        vec_db=str(tmp_path / "vec.db"),
        memory_dir=str(tmp_path / "memory"),
        experience_dir=str(tmp_path / "experience"),
        embed_model="hash-v1",
        embed_dim=64,
        index_min_steps=min_steps,
    )


def _process_incremental_write(db_path: str, number: int) -> None:
    config = SimpleNamespace(
        vec_db=db_path,
        embed_model="hash-v1",
        embed_dim=64,
        index_min_steps=2,
    )
    incremental_upsert(
        config,
        episode=_episode(f"process-{number}"),
        embedder=HashEmbedder(64),
    )


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__(64)
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return super().embed(texts)


def test_wp_r2_config_env_and_validation(monkeypatch):
    monkeypatch.setenv("PHONE_AGENT_INDEX_MIN_STEPS", "4")
    monkeypatch.setenv("PHONE_AGENT_RECALL_TOP_K", "2")
    monkeypatch.setenv("PHONE_AGENT_RECALL_MIN_SCORE", "0.55")
    config = V2Config.from_env()

    assert config.index_min_steps == 4
    assert config.recall_top_k == 2
    assert config.recall_min_score == 0.55

    monkeypatch.setenv("PHONE_AGENT_INDEX_MIN_STEPS", "-1")
    with pytest.raises(ValueError, match="PHONE_AGENT_INDEX_MIN_STEPS"):
        V2Config.from_env()


def test_incremental_upsert_is_idempotent_and_applies_episode_quality_gate(tmp_path):
    config = _config(tmp_path)
    embedder = CountingEmbedder()
    alias = _alias("淘宝购物", "淘宝", "com.taobao.taobao")

    first = incremental_upsert(
        config,
        episode=_episode("run-good"),
        alias_entries=[alias],
        all_alias_entries=[alias],
        embedder=embedder,
    )
    calls_after_first = embedder.calls
    second = incremental_upsert(
        config,
        episode=_episode("run-good"),
        alias_entries=[alias],
        all_alias_entries=[alias],
        embedder=embedder,
    )
    assert embedder.calls == calls_after_first
    short = incremental_upsert(
        config,
        episode=_episode("run-short", steps=1),
        alias_entries=[
            _alias("短跑别名", "短跑别名", "com.example.short")
        ],
        embedder=embedder,
    )
    empty = incremental_upsert(
        config,
        episode=_episode("run-empty", goal=""),
        embedder=embedder,
    )

    assert first == second == {
        "status": "updated",
        "episode": 1,
        "episode_skipped": 0,
        "app_aliases": 1,
    }
    assert short["episode_skipped"] == empty["episode_skipped"] == 1
    assert short["app_aliases"] == 1
    with VecIndex(config.vec_db, embedder=HashEmbedder(64)) as index:
        assert index.count() == 3


def test_incremental_upsert_serializes_concurrent_writers(tmp_path):
    config = _config(tmp_path)

    def write(number: int) -> None:
        incremental_upsert(
            config,
            episode=_episode(f"run-{number}"),
            embedder=HashEmbedder(64),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))

    with VecIndex(config.vec_db, embedder=HashEmbedder(64)) as index:
        assert index.count() == 8

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_process_incremental_write, args=(config.vec_db, number)
        )
        for number in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    with VecIndex(config.vec_db, embedder=HashEmbedder(64)) as index:
        assert index.count() == 12


def test_recall_end_hook_passes_persisted_episode_and_fails_open(monkeypatch):
    calls = []
    trace_events = []
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(memory_rag="shadow", vec_db="unused-in-mock.db")
    agent.session = SimpleNamespace(app_store=None)
    agent._trace = SimpleNamespace(
        record_event=lambda event, **payload: trace_events.append((event, payload))
    )
    agent._shadow_recall_ready = False
    agent._recall_alias_snapshot = {}
    episode = _episode("run-hook")

    monkeypatch.setattr(
        "phone_agent.v2.recall.incremental_upsert",
        lambda config, **kwargs: calls.append(kwargs) or {"status": "updated"},
    )
    agent._recall_run_end({"episode_outcome": episode})
    assert calls[0]["episode"] == episode
    assert trace_events[-1][1]["index_update"]["status"] == "updated"

    monkeypatch.setattr(
        "phone_agent.v2.recall.incremental_upsert",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    agent._recall_run_end({"episode_outcome": episode})
    assert trace_events[-1][1]["index_update"] == {
        "status": "error",
        "error": "RuntimeError",
    }


def test_alias_text_uses_learned_then_static_names_and_marks_package_only(tmp_path):
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    learned = _alias("淘淘", "淘淘商城", "com.taobao.taobao")
    device = _alias(
        "Taobao",
        "Taobao",
        "com.taobao.taobao",
        kind="device",
        scope="device:serial-1",
        success_count=0,
    )
    static_only = _alias(
        "WeChat",
        "WeChat",
        "com.tencent.mm",
        kind="device",
        scope="device:serial-1",
        success_count=0,
    )
    package_only = _alias(
        "com.example.opaque",
        "com.example.opaque",
        "com.example.opaque",
        kind="device",
        scope="device:serial-1",
        success_count=0,
    )
    for entry in (learned, device, static_only, package_only):
        store.upsert(entry)

    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.index_app_aliases(memory_dir=tmp_path / "memory", store=store)
        rows = index.connection.execute(
            "SELECT text, metadata_json FROM recall_items "
            "WHERE namespace = 'app_alias' ORDER BY app_package, id"
        ).fetchall()
        taobao = [row for row in rows if "com.taobao.taobao" in row["text"]]
        wechat = next(row for row in rows if "com.tencent.mm" in row["text"])
        opaque = next(row for row in rows if "com.example.opaque" in row["text"])
        exact = index.recall(
            "请打开淘宝看看",
            device_scope="device:serial-1",
            namespaces=("app_alias",),
        )
        learned_exact = index.recall(
            "请打开淘淘",
            device_scope="device:serial-1",
            namespaces=("app_alias",),
        )
        package_exact = index.recall(
            "打开 com.example.opaque",
            device_scope="device:serial-1",
            namespaces=("app_alias",),
        )
        unrelated = index.recall(
            "播放天气预报",
            device_scope="device:serial-1",
            namespaces=("app_alias",),
        )

    assert all(row["text"].startswith("淘淘商城 |") for row in taobao)
    assert all("淘宝" in row["text"] for row in taobao)
    assert wechat["text"].startswith("微信 |")
    assert opaque["text"] == "com.example.opaque"
    assert json.loads(opaque["metadata_json"])["semantic_eligible"] is False
    assert exact[0]["namespace"] == "app_alias"
    assert exact[0]["metadata"]["app_package"] == "com.taobao.taobao"
    assert learned_exact[0]["metadata"]["app_package"] == "com.taobao.taobao"
    assert package_exact[0]["metadata"]["app_package"] == "com.example.opaque"
    assert unrelated == []


def test_split_leaderboards_keep_app_mention_outside_episode_top_k(tmp_path):
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.index_episode(_episode("best", goal="打开淘宝搜索商品"))
        index.index_episode(_episode("second", goal="打开淘宝查看订单"))
        recalled = index.recall(
            "打开淘宝搜索商品",
            device_scope="device:serial-1",
            top_k=1,
            min_score=0.0,
        )

    assert [item["namespace"] for item in recalled] == ["app_alias", "episode"]
    assert [item["ref_id"] for item in recalled if item["namespace"] == "episode"] == [
        "best"
    ]


def test_threshold_has_no_fts_floor_and_recency_is_tiebreak_only(tmp_path):
    assert _keyword_score("alpha beta gamma", "alpha", fts_hit=True) == pytest.approx(
        1 / 3
    )
    now = 1_800_000_000.0
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        for ref_id, timestamp in (("old", now - 100 * 86_400), ("new", now)):
            index.upsert(
                namespace="episode",
                ref_id=ref_id,
                text="完全相同的任务",
                metadata={"device_scope": "device:serial-1", "ts": timestamp},
            )
        recalled = index.recall(
            "完全相同的任务",
            device_scope="device:serial-1",
            top_k=2,
            now=now,
            namespaces=("episode",),
        )
        noisy = index.recall(
            "毫不相关的天气任务",
            device_scope="device:serial-1",
            namespaces=("episode",),
        )

    assert [item["ref_id"] for item in recalled] == ["new", "old"]
    assert recalled[0]["score"] == recalled[1]["score"]
    assert all("time_decay=" not in reason for item in recalled for reason in item["match_reasons"])
    assert noisy == []


def test_dream_reconcile_removes_invalid_rows_and_backfills_sources(tmp_path):
    config = _config(tmp_path)
    store = AppKnowledgeStore(config.memory_dir)
    live_alias = _alias("淘淘", "淘宝", "com.taobao.taobao")
    store.upsert(live_alias)
    from phone_agent.v2.experience import ExperienceWriter

    ExperienceWriter(config.experience_dir).append_outcome(_episode("live"))
    with VecIndex(config.vec_db, embedder=HashEmbedder(64)) as index:
        index.index_episode(_episode("live"))
        index.index_episode(_episode("revoked"))
        index.upsert(
            namespace="app_alias",
            ref_id="alias:orphan",
            text="orphan",
            metadata={"device_scope": "global", "app_package": "com.old.app"},
        )
        live_id = index.connection.execute(
            "SELECT id FROM recall_items WHERE ref_id = 'live'"
        ).fetchone()
        if live_id is not None:
            index.connection.execute(
                "DELETE FROM recall_vectors WHERE rowid = ?", (int(live_id[0]),)
            )
            index.connection.commit()
    with VecIndex(config.vec_db, embedder=HashEmbedder(64, "old-model")) as index:
        index.index_episode(_episode("live"))

    result = reconcile_index(config, embedder=HashEmbedder(64), app_store=store)

    assert result["removed_episodes"] == 2
    assert result["removed_app_aliases"] == 1
    assert result["episodes"] == result["app_aliases"] == 1
    assert result["total"] == 2


def test_dream_runs_vec_reconciliation_as_third_job(tmp_path, monkeypatch):
    from phone_agent.v2.dream import run_maintenance

    config = SimpleNamespace(
        memory_dir=str(tmp_path / "memory"),
        experience_enabled=False,
        vec_db=str(tmp_path / "vec.db"),
    )
    calls = []
    monkeypatch.setattr(
        "phone_agent.v2.recall.reconcile_index",
        lambda configured, *, app_store: calls.append((configured, app_store))
        or {"status": "reconciled"},
    )

    result = run_maintenance(config, light=True)

    assert result["vec"] == {"status": "reconciled"}
    assert calls[0][0] is config
    assert isinstance(calls[0][1], AppKnowledgeStore)


def test_metrics_use_evaluation_and_prediction_denominators_consistently(tmp_path):
    mixed = evaluate_recall(
        [
            {
                "namespace": "app_alias",
                "metadata": {"app_package": "com.tencent.mm"},
            },
            {
                "namespace": "episode",
                "metadata": {"apps": ["com.taobao.taobao"]},
            },
        ],
        {"com.tencent.mm"},
    )
    empty = evaluate_recall([], {"com.tencent.mm"}, intent_apps={"com.tencent.mm"})
    stats_path = tmp_path / "recall_stats.json"
    update_recall_stats(stats_path, mixed, run_id="mixed")
    stats = update_recall_stats(stats_path, empty, run_id="empty")

    assert mixed["intent_apps"] == ["com.tencent.mm"]
    assert mixed["hit_at_1"] is True
    assert mixed["package_precision"] == 0.5
    assert stats["evaluations"] == 2
    assert stats["recall_runs"] == 1
    assert stats["hit_rate"] == stats["hit_at_1"] == 0.5
    assert stats["conditional_hit_rate"] == 1.0
    assert stats["contaminated_run_rate"] == 0.5
    assert stats["false_hit_rate"] == 0.5
    assert stats["precision_at_k"] == stats["package_precision"] == 0.5
    assert stats["recall_at_k"] == stats["package_recall"] == 0.5


def test_metrics_do_not_mix_legacy_denominators_into_schema_v2(tmp_path):
    stats_path = tmp_path / "recall_stats.json"
    stats_path.write_text(
        json.dumps(
            {
                "schema_v": 1,
                "evaluations": 57,
                "recall_runs": 34,
                "hits": 3,
                "false_hits": 34,
            }
        ),
        encoding="utf-8",
    )
    evaluation = evaluate_recall([], {"com.tencent.mm"})

    stats = update_recall_stats(stats_path, evaluation, run_id="schema-v2")

    assert stats["schema_v"] == 2
    assert stats["evaluations"] == 1
    assert stats["recall_runs"] == 0
    assert stats["contaminated_runs"] == stats["false_hits"] == 0
