"""WP-S2 analyzer dimensions from trace and memory artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from analyze import build_summary
from evidence import EvidenceView


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _alias_id(entry: dict) -> str:
    identity = "\0".join(
        str(entry.get(key, "")) for key in ("term", "package", "kind", "scope")
    )
    return "alias:" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _view(run_id: str) -> EvidenceView:
    return EvidenceView.from_events(
        [
            {"event": "run_start", "run_id": run_id, "task_goal_base": "打开聊天"},
            {
                "event": "tool_invoke",
                "step": 2,
                "tool": "launch_app",
                "args": {"app_name": "微信"},
            },
            {
                "event": "tool_observation",
                "step": 2,
                "tool": "launch_app",
                "result_text": "OK. launched 微信 (com.tencent.mm)",
                "image": {"present": False},
            },
            {"event": "run_end", "steps": 2, "terminal": {"finished": True}},
        ]
    )


def test_trace_and_memory_dimensions_join_by_agent_run_id(tmp_path):
    run_id = "agent-run-1"
    run_dir = tmp_path / "diagnosis-folder"
    memory_dir = tmp_path / "memory"
    learned = {
        "term": "聊天",
        "label": "微信",
        "package": "com.tencent.mm",
        "kind": "learned",
        "scope": "global",
    }
    trace = [
        {
            "event": "run_start",
            "memory_rag": {
                "mode": "shadow",
                "status": "ok",
                "candidates": [
                    {
                        "namespace": "episode",
                        "ref_id": "old-run",
                        "score": 0.91,
                        "match_reasons": ["vector=0.950"],
                    },
                    {
                        "namespace": "app_alias",
                        "ref_id": _alias_id(learned),
                        "score": 1.0,
                        "match_reasons": ["route=exact"],
                    },
                ],
            },
        },
        {
            "event": "capability_snapshot",
            "capabilities": [
                {
                    "cap_id": "app_kb",
                    "title": "App KB",
                    "mode": "on",
                    "state": "active",
                    "missing_deps": [],
                },
                {
                    "cap_id": "recall",
                    "title": "Recall",
                    "mode": "shadow",
                    "state": "shadow",
                    "missing_deps": [],
                },
            ],
            "memory_generation": {"source": "kb.json.generation", "value": 7},
        },
        {"event": "tool_call", "step": 1, "tool": "launch_app"},
        {
            "event": "resolution_attempt",
            "mention": "聊天软件",
            "decision": "ambiguous",
            "winner": None,
            "match_type": "embedding",
            "authority": "embedding",
            "decision_basis": "typed:clarify_only",
            "reason": "only clarification evidence types are present: embedding",
            "candidates": [
                {
                    "package": "com.tencent.mm",
                    "source_route": "embedding",
                    "match_type": "embedding",
                    "score": 0.93,
                },
                {
                    "package": "com.example.work",
                    "source_route": "embedding",
                    "match_type": "embedding",
                    "score": 0.91,
                },
            ],
        },
        {
            "event": "tool_result",
            "step": 1,
            "tool": "launch_app",
            "result": "ambiguous app",
        },
        {"event": "tool_call", "step": 2, "tool": "launch_app"},
        {
            "event": "resolution_attempt",
            "mention": "微信",
            "decision": "resolved",
            "winner": "com.tencent.mm",
            "match_type": "exact_alias",
            "authority": "registry",
            "decision_basis": "typed:auto:exact_alias",
            "reason": "exact_alias from registry; rank_score=0.970; margin=0.250",
            "candidates": [
                {
                    "package": "com.tencent.mm",
                    "source_route": "embedding",
                    "match_type": "exact_alias",
                    "score": 0.97,
                },
                {
                    "package": "com.example.work",
                    "source_route": "embedding",
                    "match_type": "embedding",
                    "score": 0.72,
                },
            ],
        },
        {
            "event": "tool_result",
            "step": 2,
            "tool": "launch_app",
            "result": "OK. launched 微信 (com.tencent.mm)",
        },
    ]
    _write_jsonl(run_dir / "traces" / f"{run_id}.jsonl", trace)
    _write_jsonl(
        memory_dir / "experience/events.jsonl",
        [
            {
                "type": "episode_outcome",
                "run_id": "old-run",
                "apps": ["com.tencent.mm"],
            },
            {
                "type": "episode_outcome",
                "run_id": run_id,
                "ts_start": 100.0,
                "ts_end": 200.0,
                "apps": ["com.tencent.mm"],
                "injected_lessons": ["les_0123456789ab"],
                "deliverable_path": "outputs/deliverables/agent-run-1.html",
            },
        ],
    )
    _write_jsonl(
        memory_dir / "app_kb/events.jsonl",
        [
            {
                "op": "upsert",
                "entry": learned,
                "evidence_note": f"implicit: run<{run_id}> 失败叫法自愈",
                "ts": "1970-01-01T00:02:00+00:00",
            },
            {
                "op": "alias_overwritten",
                "entry": learned,
                "old_package": "com.example.old",
                "new_package": "com.tencent.mm",
                "evidence_run_id": run_id,
                "ts": "1970-01-01T00:02:10+00:00",
            },
            {
                "op": "alias_user_set",
                "entry": {**learned, "kind": "user"},
                "changed": True,
                "ts": "1970-01-01T00:02:20+00:00",
            },
            {
                "op": "upsert",
                "entry": {**learned, "term": "other"},
                "evidence_note": "implicit: run<another-run>",
                "ts": "1970-01-01T00:10:00+00:00",
            },
        ],
    )

    summary = build_summary(
        {"finished": True},
        _view(run_id),
        run_id="diagnosis-folder",
        created_at="2026-01-01T00:00:00",
        target="打开聊天",
        run_dir=str(run_dir),
        trace=str(run_dir / "traces"),
        memory_dir=str(memory_dir),
    )

    resolver = summary["resolver"]
    assert resolver["total_attempts"] == 2
    assert resolver["ambiguous_count"] == 1
    assert resolver["attempts"][1]["route"] == "embedding"
    assert resolver["attempts"][1]["match_type"] == "exact_alias"
    assert resolver["attempts"][1]["decision_basis"] == "typed:auto:exact_alias"
    assert "exact_alias from registry" in resolver["attempts"][1]["reason"]
    assert resolver["attempts"][1]["top1_score"] == 0.97
    assert resolver["attempts"][1]["margin"] == 0.25
    assert resolver["route_stats"]["embedding"]["launch_success_rate"] == 0.5
    assert resolver["match_type_stats"]["exact_alias"]["resolution_rate"] == 1.0
    assert resolver["ambiguous_recoveries"][0]["winner"] == "com.tencent.mm"

    memory = summary["memory"]
    assert memory["source_run_id"] == run_id
    assert memory["memory_rag"]["hit"] is True
    assert memory["memory_rag"]["matched_packages"] == ["com.tencent.mm"]
    assert memory["memory_rag"]["candidates"][0]["namespace"] == "episode"
    assert memory["memory_rag"]["candidates"][0]["packages"] == ["com.tencent.mm"]
    assert [event["op"] for event in memory["alias_events"]] == [
        "upsert",
        "alias_overwritten",
        "alias_user_set",
    ]
    assert memory["episode"]["injected_lessons"] == ["les_0123456789ab"]
    assert memory["episode"]["deliverable_path"].endswith("agent-run-1.html")

    capabilities = summary["capabilities"]
    assert capabilities["counts"] == {"active": 1, "shadow": 1}
    assert capabilities["by_id"]["recall"] == {"mode": "shadow", "state": "shadow"}
    assert capabilities["items"][1]["mode"] == "shadow"
    categories = {finding["category"] for finding in summary["findings"]}
    assert "resolver_embedding_hit" in categories
    assert "resolver_ambiguous_recovered" in categories


def test_missing_optional_artifacts_produce_empty_dimension_blocks(tmp_path):
    summary = build_summary(
        {"finished": False},
        _view("missing"),
        run_id="folder",
        created_at="2026-01-01T00:00:00",
        target="x",
        run_dir=str(tmp_path / "missing-run"),
        memory_dir=str(tmp_path / "missing-memory"),
    )

    assert summary["resolver"]["attempts"] == []
    assert summary["memory"]["memory_rag"]["candidates"] == []
    assert summary["memory"]["alias_events"] == []
    assert summary["memory"]["episode"] == {
        "found": False,
        "injected_lessons": [],
        "deliverable_path": None,
    }
    assert summary["capabilities"] == {
        "items": [],
        "by_id": {},
        "counts": {},
        "memory_generation": None,
    }


def test_malformed_optional_artifacts_fail_open(tmp_path):
    run_dir = tmp_path / "run"
    _write_jsonl(
        run_dir / "traces/bad.jsonl",
        [
            {"event": "resolution_attempt", "candidates": "bad"},
            {
                "event": "capability_snapshot",
                "capabilities": [{"cap_id": "recall", "missing_deps": 7}],
            },
        ],
    )
    summary = build_summary(
        {"finished": False},
        _view("bad"),
        run_id="folder",
        created_at="2026-01-01T00:00:00",
        target="x",
        run_dir=str(run_dir),
        memory_dir=str(tmp_path / "missing-memory"),
    )

    assert summary["resolver"]["attempts"][0]["candidate_count"] == 0
    assert summary["capabilities"]["items"][0]["missing_deps"] == []
