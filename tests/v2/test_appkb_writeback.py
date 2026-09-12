"""Fake-only tests for verified ``launch_app`` App-KB feedback."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from phone_agent.v2.appkb import AppKnowledge, AppKnowledgeStore
from phone_agent.v2.session import PhoneSession
from phone_agent.v2.tools.actuation import build_actuation_tools

from tests.v2._doubles import FakeDeviceFactory, FakePhoneSession


WECHAT_PACKAGE = "com.tencent.mm"
TRAVEL_PACKAGE = "com.taobao.trip"


def _tool_map(session, config):
    return {tool.name: tool for tool in build_actuation_tools(session, config)}


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(
        block.get("text", "")
        for block in result
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _entry(
    term: str,
    *,
    label: str = "微信",
    package: str = WECHAT_PACKAGE,
    kind: str = "alias",
    success_count: int = 0,
    stale: bool = False,
) -> dict[str, object]:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    return {
        "term": term,
        "label": label,
        "package": package,
        "kind": kind,
        "scope": "global",
        "confidence": 0.8,
        "success_count": success_count,
        "first_seen": timestamp,
        "last_seen": timestamp,
        "stale": stale,
    }


def _kb_session(tmp_path):
    config = SimpleNamespace(device_id="serial-1", app_kb_enabled=True, resolver_embed=False)
    device = FakeDeviceFactory(installed=frozenset({WECHAT_PACKAGE}))
    session = FakePhoneSession({}, device_factory=device)
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    store.sync_device("serial-1", [(WECHAT_PACKAGE, "微信")])
    session.config = config
    session.app_store = store
    session.app_knowledge = AppKnowledge(store, device_id="serial-1")
    session._kb_device_id = lambda: "serial-1"
    return session, config, store


def _implicit_session(
    tmp_path, *, available: str = TRAVEL_PACKAGE, enabled: bool = True
):
    config = SimpleNamespace(
        device_id="serial-1",
        app_kb_enabled=True,
        implicit_alias_enabled=enabled,
        resolver_embed=False,
    )
    device = FakeDeviceFactory(installed=frozenset({TRAVEL_PACKAGE}))
    session = PhoneSession(config, device_factory=device)
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    store.sync_device("serial-1", [(TRAVEL_PACKAGE, TRAVEL_PACKAGE)])
    session.app_store = store
    session.app_knowledge = AppKnowledge(store, device_id="serial-1")
    session.app_list_for_prompt = lambda _max_n: available
    session.reset_implicit_alias_state("run-i3")
    return session, config, store, device


def test_verified_launch_persists_learned_alias_and_event(tmp_path):
    session, config, store = _kb_session(tmp_path)

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "wechat"}
    )

    assert _text(result).startswith(f"OK. launched wechat ({WECHAT_PACKAGE})")
    learned = store.entries(scope="global", kind="learned")
    assert len(learned) == 1
    assert {
        key: learned[0][key]
        for key in (
            "term",
            "label",
            "package",
            "kind",
            "scope",
            "confidence",
            "success_count",
            "stale",
        )
    } == {
        "term": "wechat",
        "label": "微信",
        "package": WECHAT_PACKAGE,
        "kind": "learned",
        "scope": "global",
        "confidence": 0.9,
        "success_count": 1,
        "stale": False,
    }
    materialized = json.loads(store.kb_path.read_text(encoding="utf-8"))
    assert learned[0] in materialized
    last_event = json.loads(
        store.events_path.read_text(encoding="utf-8").splitlines()[-1]
    )
    assert last_event["op"] == "upsert"
    assert last_event["entry"] == learned[0]


def test_kb_alias_hit_bumps_success_without_duplicate(tmp_path):
    session, config, store = _kb_session(tmp_path)
    store.upsert(_entry("工作聊天", success_count=2))
    old_last_seen = next(
        entry["last_seen"]
        for entry in store.entries()
        if entry["term"] == "工作聊天"
    )
    event_count = len(store.events_path.read_text(encoding="utf-8").splitlines())

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "工作聊天"}
    )

    assert _text(result).startswith(f"OK. launched 工作聊天 ({WECHAT_PACKAGE})")
    matches = [
        entry
        for entry in store.entries(include_stale=True)
        if entry["term"] == "工作聊天" and entry["package"] == WECHAT_PACKAGE
    ]
    assert len(matches) == 1
    assert matches[0]["kind"] == "alias"
    assert matches[0]["success_count"] == 3
    assert matches[0]["last_seen"] > old_last_seen
    assert matches[0]["last_success"] == matches[0]["last_seen"]
    assert store.entries(kind="learned") == []
    assert (
        len(store.events_path.read_text(encoding="utf-8").splitlines())
        == event_count + 1
    )


def test_inventory_refresh_preserves_last_success(tmp_path, monkeypatch):
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    clock = {"value": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr("phone_agent.v2.appkb._iso_now", lambda: clock["value"])
    store.sync_device("serial-1", [(WECHAT_PACKAGE, "微信")])
    clock["value"] = "2026-02-01T00:00:00+00:00"
    store.record_success("微信", WECHAT_PACKAGE)
    used = store.entries(scope="device:serial-1")[0]
    last_success = used["last_success"]

    monkeypatch.setattr(
        "phone_agent.v2.appkb._iso_now", lambda: "2026-03-01T00:00:00+00:00"
    )
    store.sync_device("serial-1", [(WECHAT_PACKAGE, "微信")])
    refreshed = store.entries(scope="device:serial-1")[0]

    assert refreshed["last_seen"] == "2026-03-01T00:00:00+00:00"
    assert refreshed["last_success"] == last_success


def test_same_canonical_term_launch_records_success_without_learning_alias(tmp_path):
    session, config, store = _kb_session(tmp_path)
    before_count = len(store.events_path.read_text(encoding="utf-8").splitlines())

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "微信"}
    )

    assert _text(result).startswith(f"OK. launched 微信 ({WECHAT_PACKAGE})")
    device_entry = store.entries(scope="device:serial-1", kind="device")[0]
    assert device_entry["success_count"] == 1
    assert device_entry["last_success"] == device_entry["last_seen"]
    assert store.entries(kind="learned") == []
    assert len(store.events_path.read_text(encoding="utf-8").splitlines()) == before_count + 1


def test_kb_write_failure_preserves_success_receipt():
    class FailingStore:
        def entries(self, **kwargs):
            return [{"label": "微信", "package": WECHAT_PACKAGE}]

        def upsert(self, entry):
            raise OSError("disk full")

    config = SimpleNamespace(device_id="serial-1", app_kb_enabled=True, resolver_embed=False)
    session = FakePhoneSession(
        {},
        device_factory=FakeDeviceFactory(installed=frozenset({WECHAT_PACKAGE})),
    )
    session.app_store = FailingStore()
    session.app_knowledge = SimpleNamespace(last_match=None)
    session._kb_device_id = lambda: "serial-1"

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "wechat"}
    )

    assert _text(result).startswith(f"OK. launched wechat ({WECHAT_PACKAGE})")
    assert session.last_tool_ok is True


def test_app_kb_disabled_launch_makes_no_filesystem_write(tmp_path):
    memory_dir = tmp_path / "memory"
    config = SimpleNamespace(
        device_id=None, app_kb_enabled=False, memory_dir=str(memory_dir)
    )
    session = FakePhoneSession({})

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "微信"}
    )

    assert _text(result).startswith(f"OK. launched 微信 ({WECHAT_PACKAGE})")
    assert not memory_dir.exists()


def test_sensitive_looking_term_is_not_persisted(tmp_path):
    package = "com.example.phonebook"
    config = SimpleNamespace(device_id="serial-1", app_kb_enabled=True, resolver_embed=False)
    session = FakePhoneSession(
        {}, device_factory=FakeDeviceFactory(installed=frozenset({package}))
    )
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    store.sync_device("serial-1", [(package, "13800138000")])
    session.app_store = store
    session.app_knowledge = AppKnowledge(store, device_id="serial-1")
    session._kb_device_id = lambda: "serial-1"
    before_events = store.events_path.read_bytes()
    before_kb = store.kb_path.read_bytes()

    result = _tool_map(session, config)["launch_app"].invoke(
        {"app_name": "13800138000"}
    )

    assert _text(result).startswith(f"OK. launched 13800138000 ({package})")
    assert store.events_path.read_bytes() == before_events
    assert store.kb_path.read_bytes() == before_kb


def test_last_match_resets_and_record_success_ignores_stale_entries(tmp_path):
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    store.upsert(_entry("工作聊天"))
    store.upsert(_entry("旧入口", stale=True))
    knowledge = AppKnowledge(store)

    assert knowledge.lookup("工作聊天") == WECHAT_PACKAGE
    assert knowledge.last_match is not None
    assert knowledge.last_match["term"] == "工作聊天"
    assert knowledge.lookup("不存在") is None
    assert knowledge.last_match is None
    assert store.record_success("旧入口", WECHAT_PACKAGE) is False


def test_unknown_then_receipt_listed_package_success_persists_implicit_alias(
    tmp_path,
):
    session, config, store, device = _implicit_session(tmp_path)
    launch = _tool_map(session, config)["launch_app"]

    failed = launch.invoke({"app_name": " 飞 猪 "})
    succeeded = launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert failed.endswith(f"；本机可用应用：{TRAVEL_PACKAGE}")
    assert _text(succeeded).startswith(
        f"OK. launched {TRAVEL_PACKAGE} ({TRAVEL_PACKAGE})"
    )
    assert device.calls == [("launch_app", TRAVEL_PACKAGE)]
    learned = store.entries(scope="global", kind="learned")
    assert len(learned) == 1
    assert {
        key: learned[0][key]
        for key in ("term", "label", "package", "kind", "scope")
    } == {
        "term": "飞猪",
        "label": TRAVEL_PACKAGE,
        "package": TRAVEL_PACKAGE,
        "kind": "learned",
        "scope": "global",
    }
    last_event = json.loads(
        store.events_path.read_text(encoding="utf-8").splitlines()[-1]
    )
    assert last_event["evidence_note"] == (
        "implicit: run<run-i3> 失败叫法自愈"
    )
    assert "evidence_note" not in learned[0]


def test_implicit_alias_requires_exact_receipt_candidate(tmp_path):
    session, config, store, _device = _implicit_session(
        tmp_path, available="com.example.other"
    )
    launch = _tool_map(session, config)["launch_app"]

    launch.invoke({"app_name": "飞猪"})
    launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert store.entries(kind="learned") == []


def test_implicit_alias_requires_case_exact_package_candidate(tmp_path):
    session, config, store, _device = _implicit_session(
        tmp_path, available=TRAVEL_PACKAGE.upper()
    )
    launch = _tool_map(session, config)["launch_app"]

    launch.invoke({"app_name": "飞猪"})
    launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert store.entries(kind="learned") == []


def test_implicit_alias_requires_nonempty_receipt_candidates(tmp_path):
    session, config, store, _device = _implicit_session(tmp_path, available="")
    session.app_knowledge.snapshot = lambda: {}
    launch = _tool_map(session, config)["launch_app"]

    launch.invoke({"app_name": "飞猪"})
    launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert store.entries(kind="learned") == []


def test_implicit_alias_skips_term_equal_to_package(tmp_path):
    session, config, store, _device = _implicit_session(tmp_path)
    session.record_failed_launch(f" {TRAVEL_PACKAGE} ", [TRAVEL_PACKAGE])

    _tool_map(session, config)["launch_app"].invoke(
        {"app_name": TRAVEL_PACKAGE}
    )

    assert store.entries(kind="learned") == []


def test_implicit_alias_failure_evidence_does_not_cross_run(tmp_path):
    session, config, store, _device = _implicit_session(tmp_path)
    session.record_failed_launch("飞猪", [TRAVEL_PACKAGE])

    session.reset_implicit_alias_state("run-next")
    _tool_map(session, config)["launch_app"].invoke(
        {"app_name": TRAVEL_PACKAGE}
    )

    assert store.entries(kind="learned") == []


def test_implicit_alias_switch_off_records_nothing(tmp_path):
    session, config, store, _device = _implicit_session(tmp_path, enabled=False)
    launch = _tool_map(session, config)["launch_app"]

    launch.invoke({"app_name": "飞猪"})
    launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert session._failed_launches == []
    assert store.entries(kind="learned") == []


def test_implicit_alias_deduplicates_terms_but_writes_all_distinct_matches(
    tmp_path,
):
    session, config, store, _device = _implicit_session(tmp_path)
    session.record_failed_launch("飞 猪", [TRAVEL_PACKAGE])
    session.record_failed_launch("飞猪", [TRAVEL_PACKAGE])
    session.record_failed_launch("飞猪旅行", [TRAVEL_PACKAGE])
    launch = _tool_map(session, config)["launch_app"]

    launch.invoke({"app_name": TRAVEL_PACKAGE})
    launch.invoke({"app_name": TRAVEL_PACKAGE})

    assert [entry["term"] for entry in store.entries(kind="learned")] == [
        "飞猪",
        "飞猪旅行",
    ]
