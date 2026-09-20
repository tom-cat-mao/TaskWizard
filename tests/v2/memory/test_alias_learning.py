"""WP-N2b alias overwrite mining and user-correction CLI tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

import main_v2
from phone_agent.v2.appkb import AppKnowledge, AppKnowledgeStore
from phone_agent.v2.dream import (
    apply_alias_overwrites,
    consolidate,
    detect_alias_overwrite_signatures,
)
from phone_agent.v2.middleware.trace import TraceMiddleware


NOTES = ("开错", "不对", "不是", "错了", "wrong app")
OLD = "com.example.old"
NEW = "com.example.new"


def _entry(term, package, *, kind="learned", scope="global"):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    return {
        "term": term,
        "label": term,
        "package": package,
        "kind": kind,
        "scope": scope,
        "confidence": 1.0 if kind in {"user", "device"} else 0.9,
        "success_count": 1,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "stale": False,
    }


def _observed(store, run_id, step, tool, **kwargs):
    store.record_alias_tool_event(
        run_id=run_id, step=step, tool=tool, success=True, **kwargs
    )


def test_signature_matrix_only_complete_same_run_sequence_matches(tmp_path):
    store = AppKnowledgeStore(str(tmp_path))
    # Complete signature: launch A, explicit wrong-app back one step later, launch B.
    _observed(store, "complete", 1, "launch_app", term="小红书", package=OLD)
    _observed(store, "complete", 2, "back", note_marker="开错")
    _observed(store, "complete", 3, "launch_app", term="新名字", package=NEW)
    # Exit without self-declaration.
    _observed(store, "exit-only", 1, "launch_app", term="A", package=OLD)
    _observed(store, "exit-only", 2, "back")
    _observed(store, "exit-only", 3, "launch_app", term="A", package=NEW)
    # Self-declaration without back/other-app exit.
    _observed(store, "note-only", 1, "launch_app", term="B", package=OLD)
    _observed(store, "note-only", 2, "wait", note_marker="错了")
    _observed(store, "note-only", 3, "launch_app", term="B", package=NEW)
    # A wrong-app note cannot bridge two runs.
    _observed(store, "run-a", 1, "launch_app", term="C", package=OLD)
    _observed(store, "run-a", 2, "back", note_marker="不对")
    _observed(store, "run-b", 3, "launch_app", term="C", package=NEW)
    # An unrelated cross-name launch without a marker does not complete a signature.
    _observed(store, "cross-name", 1, "launch_app", term="D", package=OLD)
    _observed(store, "cross-name", 2, "launch_app", term="E", package=NEW)

    signatures = detect_alias_overwrite_signatures(store.events_path, note_terms=NOTES)

    assert len(signatures) == 1
    assert signatures[0].run_id == "complete"
    assert signatures[0].term == "小红书"
    assert signatures[0].old_package == OLD
    assert signatures[0].new_package == NEW


def test_signature_accepts_launching_other_app_as_immediate_correction(tmp_path):
    store = AppKnowledgeStore(str(tmp_path))
    _observed(store, "run-1", 4, "launch_app", term="小红书", package=OLD)
    _observed(
        store,
        "run-1",
        6,
        "launch_app",
        term="com.example.new",
        package=NEW,
        note_marker="wrong app",
    )

    signatures = detect_alias_overwrite_signatures(store.events_path, note_terms=NOTES)

    assert [(item.exit_step, item.corrected_step) for item in signatures] == [(6, 6)]


def test_overwrite_replaces_old_learned_alias_records_event_and_is_idempotent(
    tmp_path,
):
    store = AppKnowledgeStore(str(tmp_path))
    store.upsert(_entry("小红书", OLD))
    store.upsert(_entry("New App", NEW, kind="device", scope="device:one"))
    _observed(store, "run-1", 1, "launch_app", term="小红书", package=OLD)
    _observed(store, "run-1", 2, "back", note_marker="开错")
    _observed(store, "run-1", 3, "launch_app", term="New App", package=NEW)

    first = apply_alias_overwrites(store, note_terms=NOTES)
    second = apply_alias_overwrites(store, note_terms=NOTES)

    assert first == {"candidates": 1, "overwritten": 1}
    assert second == {"candidates": 0, "overwritten": 0}
    learned = store.entries(kind="learned")
    assert [(entry["term"], entry["package"]) for entry in learned] == [("小红书", NEW)]
    events = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    overwritten = [event for event in events if event.get("op") == "alias_overwritten"]
    assert len(overwritten) == 1
    assert overwritten[0]["old_package"] == OLD
    assert overwritten[0]["new_package"] == NEW
    assert overwritten[0]["evidence_run_id"] == "run-1"
    assert AppKnowledgeStore(str(tmp_path)).entries(kind="learned") == learned

    consolidate(store, light=True)
    assert apply_alias_overwrites(store, note_terms=NOTES) == {
        "candidates": 0,
        "overwritten": 0,
    }


def test_overwrite_learns_when_old_alias_is_absent_but_never_overrides_user(tmp_path):
    store = AppKnowledgeStore(str(tmp_path))
    created = store.overwrite_learned_alias(
        "新叫法",
        OLD,
        NEW,
        evidence_run_id="run-new",
        signature_fingerprint="new-fingerprint",
    )
    store.set_user_alias("用户指定", OLD)
    blocked = store.overwrite_learned_alias(
        "用户指定",
        OLD,
        NEW,
        evidence_run_id="run-blocked",
        signature_fingerprint="blocked-fingerprint",
    )

    assert created is not None and created["package"] == NEW
    assert blocked is None
    assert AppKnowledge(store).lookup("用户指定") == OLD


def test_user_alias_has_priority_over_same_name_device_entry(tmp_path):
    store = AppKnowledgeStore(str(tmp_path))
    store.upsert(_entry("小红书", OLD, kind="device", scope="device:serial-1"))
    store.set_user_alias("小红书", NEW)

    assert AppKnowledge(store, device_id="serial-1").lookup("小红书") == NEW


def test_dream_merge_keeps_user_kind_over_learned_and_separate_from_device(tmp_path):
    store = AppKnowledgeStore(str(tmp_path))
    learned = _entry("旧称", NEW)
    learned["label"] = "小红书"
    store.upsert(learned)
    store.upsert(_entry("小红书", NEW, kind="user"))
    store.upsert(_entry("小红书", NEW, kind="device", scope="device:serial-1"))

    consolidate(store, light=True)

    assert {(entry["kind"], entry["scope"]) for entry in store.entries()} == {
        ("user", "global"),
        ("device", "device:serial-1"),
    }


def test_trace_writes_minimal_alias_evidence_and_discards_full_note(tmp_path):
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    session = SimpleNamespace(app_store=store, launched_apps=[])
    middleware = TraceMiddleware(
        "run-evidence",
        enabled=False,
        session=session,
        alias_overwrite_enabled=True,
        alias_overwrite_notes=NOTES,
    )
    middleware._step = 7
    request = SimpleNamespace(
        tool_call={
            "name": "launch_app",
            "args": {
                "app_name": "小红书",
                "intent": "打开社区",
                "note": "开错了，这是不应持久化的完整模型说明",
            },
            "id": "call-1",
        }
    )

    def handler(_request):
        session.launched_apps.append(OLD)
        return ToolMessage(content="OK. launched", tool_call_id="call-1")

    middleware.on_tool_execute(request, handler)

    raw = store.events_path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert event == {
        "op": "tool_observed",
        "run_id": "run-evidence",
        "step": 7,
        "tool": "launch_app",
        "term": "小红书",
        "package": OLD,
        "success": True,
        "note_marker": "开错",
        "ts": event["ts"],
    }
    assert "不应持久化" not in raw


def _cli_config(tmp_path):
    return SimpleNamespace(
        app_kb_enabled=True,
        dream_mode="manual",
        experience_enabled=False,
        memory_rag="off",
        memory_dir=str(tmp_path / "memory"),
        device_id="serial-1",
    )


def test_cli_learn_overrides_learned_with_user_and_warns_when_not_installed(
    tmp_path, monkeypatch, capsys
):
    config = _cli_config(tmp_path)
    store = AppKnowledgeStore(config.memory_dir)
    store.upsert(_entry("小红书", OLD))
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda _overrides: config)
    monkeypatch.setattr(main_v2, "_device_inventory", lambda _config: {OLD})

    assert main_v2.main(["--learn-alias", f"小红书={NEW}"]) == 0

    output = capsys.readouterr().out
    assert '"kind": "user"' in output
    assert "not installed" in output
    aliases = [
        entry
        for entry in AppKnowledgeStore(config.memory_dir).entries()
        if entry["term"] == "小红书"
    ]
    assert [(entry["kind"], entry["package"]) for entry in aliases] == [("user", NEW)]
    assert (
        json.loads(store.events_path.read_text(encoding="utf-8").splitlines()[-1])["op"]
        == "alias_user_set"
    )


def test_cli_forget_removes_user_and_learned_but_preserves_device(
    tmp_path, monkeypatch, capsys
):
    config = _cli_config(tmp_path)
    store = AppKnowledgeStore(config.memory_dir)
    store.upsert(_entry("小红书", OLD))
    store.upsert(_entry("小红书", NEW, kind="user"))
    store.upsert(_entry("小红书", NEW, kind="device", scope="device:serial-1"))
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda _overrides: config)

    assert main_v2.main(["--forget-alias", "小红书"]) == 0

    assert '"removed": 2' in capsys.readouterr().out
    remaining = AppKnowledgeStore(config.memory_dir).entries()
    assert [(entry["kind"], entry["package"]) for entry in remaining] == [
        ("device", NEW)
    ]
    assert (
        json.loads(store.events_path.read_text(encoding="utf-8").splitlines()[-1])["op"]
        == "alias_forgotten"
    )


@pytest.mark.parametrize(
    "argument,error",
    [
        ("小红书", "expects NAME=PACKAGE"),
        ("小红书=not_a_package", "invalid Android package name"),
        ("=com.example.app", "alias name must not be empty"),
    ],
)
def test_cli_learn_rejects_invalid_input(
    tmp_path, monkeypatch, capsys, argument, error
):
    config = _cli_config(tmp_path)
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda _overrides: config)

    assert main_v2.main(["--learn-alias", argument]) == 1
    assert error in capsys.readouterr().err
    assert not (tmp_path / "memory").exists()
