"""WP-J capability registry and run-start composition snapshots."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import CapabilityRegistry, CapabilitySpec
from phone_agent.v2.recall import read_episode_events
from phone_agent.web.bridge import WebRunBridge
from tests.v2.test_experience import _install_mini_agent_modules


def _by_id(registry: CapabilityRegistry) -> dict[str, dict]:
    return {row["cap_id"]: row for row in registry.status()}


def test_registry_maps_modes_and_reports_unready_dependencies() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("taskdoc", "TaskDoc", "off"))
    registry.register(CapabilitySpec("recall", "Recall", "shadow"))
    registry.register(
        CapabilitySpec("dream", "Dream", "manual", deps=("app_kb",))
    )
    registry.register(
        CapabilitySpec("planner", "Planner", "custom", deps=("taskdoc",))
    )

    rows = _by_id(registry)

    assert rows["taskdoc"]["state"] == "off"
    assert rows["recall"]["state"] == "shadow"
    assert rows["dream"] == {
        "cap_id": "dream",
        "title": "Dream",
        "mode": "manual",
        "state": "pending",
        "missing_deps": ["app_kb"],
    }
    assert rows["planner"]["state"] == "pending"
    assert rows["planner"]["missing_deps"] == ["taskdoc"]


def test_registry_reserves_passive_apply_release_contract() -> None:
    calls: list[str] = []
    spec = CapabilitySpec(
        "effect",
        "Effect",
        "on",
        apply=lambda: calls.append("apply"),
        release=lambda: calls.append("release"),
    )
    registry = CapabilityRegistry()
    registry.register(spec)

    assert calls == []
    assert spec.apply is not None and spec.release is not None
    spec.apply()
    spec.release()
    assert calls == ["apply", "release"]


def test_registry_rejects_duplicate_stable_id() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("budget", "Budget", "on"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(CapabilitySpec("budget", "Other budget", "off"))


def test_compact_memory_state_reuses_run_capability_snapshot():
    from phone_agent.v2.agent import ThinPhoneAgent

    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("compact", "Compact", "on"))
    registry.register(CapabilitySpec("recall", "Recall", "shadow"))
    trace_events: list[tuple[str, dict]] = []
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.capability_registry = registry
    agent._trace = SimpleNamespace(
        record_event=lambda event, **payload: trace_events.append((event, payload))
    )
    agent._shadow_candidates = [{"ref_id": "episode-1"}, {"id": "alias-2"}]
    agent._app_kb_generation = lambda: {
        "source": "kb.json.generation",
        "value": 3,
    }

    agent._record_capability_snapshot()

    state = agent._compact_memory_state()
    assert state == {
        "capabilities": {"compact": "active", "recall": "shadow"},
        "memory_generation": {
            "source": "kb.json.generation",
            "value": 3,
        },
        "shadow_candidate_ids": ["episode-1", "alias-2"],
    }
    assert trace_events[0][1]["memory_generation"] == state["memory_generation"]


def test_mini_run_writes_same_capability_snapshot_to_trace_and_episode(
    tmp_path, monkeypatch
) -> None:
    from phone_agent.v2.agent import ThinPhoneAgent

    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    config.trace_enabled = True
    config.app_kb_enabled = True
    config.dream_mode = "manual"
    config.memory_rag = "off"
    config.memory_dir = str(tmp_path / "memory")
    kb_path = tmp_path / "memory/app_kb/kb.json"
    kb_path.parent.mkdir(parents=True)
    kb_path.write_text("[]\n", encoding="utf-8")
    generation = kb_path.stat().st_mtime_ns

    agent = ThinPhoneAgent(config)
    assert agent.run("打开设置").success is True

    trace = [
        json.loads(line)
        for line in (tmp_path / "traces" / f"{agent.run_id}.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    snapshot = next(event for event in trace if event["event"] == "capability_snapshot")
    trace_states = {
        row["cap_id"]: row["state"] for row in snapshot["capabilities"]
    }
    outcomes = [
        event
        for event in read_episode_events(tmp_path / "experience/events.jsonl")
        if event["run_id"] == agent.run_id
    ]

    assert outcomes[0]["capabilities"] == trace_states
    assert trace_states == {
        "taskdoc": "off",
        "safety": "off",
        "budget": "active",
        "compact": "off",
        "finish_verify": "off",
        "deliverable": "active",
        "app_kb": "active",
        "dream": "active",
        "experience": "active",
        "providers": "active",
        "recall": "off",
    }
    assert snapshot["memory_generation"] == {
        "source": "kb.json.mtime_ns",
        "value": generation,
    }


def test_recall_reader_accepts_legacy_episode_without_capabilities(tmp_path) -> None:
    legacy = {
        "type": "episode_outcome",
        "schema_v": 1,
        "run_id": "legacy",
        "goal_text": "打开设置",
        "apps": [],
        "success": True,
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    assert read_episode_events(path) == [legacy]


def test_web_snapshot_exposes_registry_status_when_agent_exists() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("budget", "Budget", "on"))
    bridge = WebRunBridge(config_factory=lambda _: SimpleNamespace())
    bridge._agent = SimpleNamespace(
        capability_registry=registry, session=SimpleNamespace(usage_ledger=None)
    )

    assert bridge.snapshot()["capabilities"] == registry.status()
