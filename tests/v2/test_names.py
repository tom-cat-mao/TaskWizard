"""WP-N2 app-name resolver tests (all fake/local)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from phone_agent.config.app_registry import AppIdentity, AppRegistry
from phone_agent.v2.names import (
    AppNameCandidate,
    ResolverSettings,
    decide_name,
    generate_candidates,
    mention_occurs,
    normalize_name,
    resolve_name,
)
from phone_agent.v2.recall import HashEmbedder, VecIndex
from phone_agent.v2.middleware.trace import TraceMiddleware
from phone_agent.v2.tools.actuation import build_actuation_tools
from tests.v2._doubles import FakeDeviceFactory, FakePhoneSession

BILI_PACKAGE = "tv.danmaku.bili"


def _settings(**overrides):
    values = {
        "decision_mode": "typed",
        "min_score": 0.90,
        "margin": 0.08,
        "typed_margin": 0.08,
        "top_k": 10,
        "lexical": True,
        "pinyin": True,
        "embed": True,
        "w_sim": 0.8,
        "w_prior": 0.2,
        "package_segment_min_len": 4,
        "package_segment_stopwords": (
            "com",
            "org",
            "net",
            "android",
            "example",
            "app",
            "mobile",
            "free",
            "debug",
            "release",
        ),
        "auto_match_types": (
            "exact_alias",
            "exact_label",
            "exact_package",
            "exact_package_segment",
            "registered_containment",
        ),
        "clarify_match_types": (
            "fuzzy",
            "pinyin_full",
            "pinyin_initials",
            "embedding",
        ),
    }
    values.update(overrides)
    return ResolverSettings(**values)


def _identity(name: str, package: str, *aliases: str) -> AppIdentity:
    return AppIdentity(
        canonical_id=name,
        packages=frozenset({package}),
        display_names={"default": name},
        aliases=frozenset(aliases),
    )


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(
        block.get("text", "")
        for block in result
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_l0_normalizes_case_whitespace_width_and_common_traditional():
    assert normalize_name(" Ｇｏｏｇｌｅ  臺 灣 ") == "google台湾"
    assert mention_occurs("Google Chrome", "请打开 GOOGLE   CHROME 浏览器")


def test_exact_route_merges_registry_and_kb_by_package():
    registry = AppRegistry([_identity("chat", "com.example.chat", "聊天")])
    candidates = generate_candidates(
        "聊天",
        registry=registry,
        kb_entries=[
            {
                "term": "聊天",
                "label": "聊天",
                "package": "com.example.chat",
                "kind": "device",
                "success_count": 3,
                "last_success": datetime.now(timezone.utc).isoformat(),
            }
        ],
        settings=_settings(pinyin=False, embed=False),
    )

    assert len(candidates) == 1
    assert candidates[0].source_route == "exact"
    assert candidates[0].match_type == "exact_label"
    assert candidates[0].authority == "device"
    assert candidates[0].package == "com.example.chat"
    assert any(":registry:" in value for value in candidates[0].provenance)
    assert any(":device:" in value for value in candidates[0].provenance)


def test_registered_containment_resolves_from_short_registry_alias():
    registry = AppRegistry([_identity("bilibili", BILI_PACKAGE, "哔哩")])

    result = resolve_name(
        "哔哩哔哩",
        registry=registry,
        settings=_settings(pinyin=False, embed=False),
    )

    assert result.status == "resolved"
    assert result.winner is not None
    assert result.winner.package == BILI_PACKAGE
    assert result.winner.source_route == "lexical"
    assert result.winner.match_type == "registered_containment"
    assert result.winner.matched_term == "哔哩"


def test_pinyin_route_requires_clarification_in_typed_mode():
    registry = AppRegistry([_identity("wechat", "com.tencent.mm", "微信")])

    result = resolve_name(
        "wx",
        registry=registry,
        settings=_settings(lexical=False, embed=False),
    )

    assert result.status == "ambiguous"
    assert result.winner is None
    assert result.candidates[0].source_route == "pinyin"
    assert result.candidates[0].match_type == "pinyin_initials"


def test_embedding_route_uses_vec_index_but_requires_clarification(tmp_path):
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.upsert(
            namespace="app_alias",
            ref_id="alias:music",
            text="云端音乐 播放歌曲 com.example.music",
            metadata={
                "device_scope": "global",
                "app_package": "com.example.music",
                "term": "云端音乐",
                "label": "云端音乐",
                "kind": "learned",
                "semantic_eligible": True,
            },
        )
        result = resolve_name(
            "云端音乐 播放歌曲 com.example.music",
            registry=(),
            embedding_search=lambda query, top_k: index.app_name_vector_candidates(
                query, device_scope="device:serial-1", top_k=top_k
            ),
            settings=_settings(lexical=False, pinyin=False),
        )

    assert result.status == "ambiguous"
    assert result.winner is None
    assert result.candidates[0].source_route == "embedding"
    assert result.candidates[0].match_type == "embedding"
    assert result.candidates[0].package == "com.example.music"


def test_margin_returns_ranked_ambiguous_top_k_for_wechat_class():
    result = resolve_name(
        "微信",
        registry=(),
        kb_entries=[
            {
                "term": "微信",
                "label": "微信工作版",
                "package": "com.example.wechat.work",
                "kind": "device",
            },
            {
                "term": "微信",
                "label": "微信个人版",
                "package": "com.example.wechat.personal",
                "kind": "device",
            },
        ],
        settings=_settings(top_k=2, pinyin=False, embed=False),
    )

    assert result.status == "ambiguous"
    assert [item.package for item in result.candidates] == [
        "com.example.wechat.personal",
        "com.example.wechat.work",
    ]


def test_margin_still_uses_top_two_when_receipt_top_k_is_one():
    candidates = [
        AppNameCandidate("pkg.a", "exact", 1.0, 1.0, 1.0, "same", "exact_alias"),
        AppNameCandidate("pkg.b", "exact", 1.0, 1.0, 1.0, "same", "exact_alias"),
    ]

    result = decide_name("same", candidates, settings=_settings(top_k=1))

    assert result.status == "ambiguous"
    assert [candidate.package for candidate in result.candidates] == ["pkg.a"]


def test_package_segment_resolves_piliplus_and_firefox():
    for mention, package in (
        ("PiliPlus", "com.example.piliplus"),
        ("Firefox", "org.mozilla.firefox"),
    ):
        result = resolve_name(
            mention,
            registry=(),
            kb_entries=[
                {
                    "term": package,
                    "label": package,
                    "package": package,
                    "kind": "device",
                }
            ],
            settings=_settings(pinyin=False, embed=False),
        )

        assert result.status == "resolved"
        assert result.winner is not None
        assert result.winner.package == package
        assert result.winner.match_type == "exact_package_segment"
        assert result.decision_basis == "typed:auto:exact_package_segment"


def test_partial_package_substrings_do_not_resolve():
    for mention, package in (
        ("plus", "com.example.piliplus"),
        ("fox", "org.mozilla.firefox"),
    ):
        result = resolve_name(
            mention,
            registry=(),
            kb_entries=[
                {
                    "term": package,
                    "label": package,
                    "package": package,
                    "kind": "device",
                }
            ],
            settings=_settings(pinyin=False, embed=False),
        )

        assert result.status == "unknown"
        assert result.winner is None
        assert result.candidates == ()


def test_same_package_segment_on_multiple_packages_is_ambiguous():
    result = resolve_name(
        "reader",
        registry=(),
        kb_entries=[
            {
                "term": "com.foo.reader",
                "label": "com.foo.reader",
                "package": "com.foo.reader",
                "kind": "device",
            },
            {
                "term": "com.bar.reader",
                "label": "com.bar.reader",
                "package": "com.bar.reader",
                "kind": "device",
            },
        ],
        settings=_settings(pinyin=False, embed=False),
    )

    assert result.status == "ambiguous"
    assert {candidate.match_type for candidate in result.candidates} == {
        "exact_package_segment"
    }
    assert {candidate.package for candidate in result.candidates} == {
        "com.foo.reader",
        "com.bar.reader",
    }


def test_firefox_beta_dual_install_is_ambiguous():
    result = resolve_name(
        "Firefox Beta",
        registry=(),
        kb_entries=[
            {
                "term": "org.mozilla.firefox",
                "label": "org.mozilla.firefox",
                "package": "org.mozilla.firefox",
                "kind": "device",
            },
            {
                "term": "org.mozilla.firefox_beta",
                "label": "org.mozilla.firefox_beta",
                "package": "org.mozilla.firefox_beta",
                "kind": "device",
            },
        ],
        settings=_settings(pinyin=False, embed=False),
    )

    assert result.status == "ambiguous"
    assert {candidate.package for candidate in result.candidates} == {
        "org.mozilla.firefox",
        "org.mozilla.firefox_beta",
    }


def test_pinyin_top1_never_auto_resolves_feizhu_to_feishu_when_installed():
    package = "com.ss.android.lark"
    result = resolve_name(
        "飞猪",
        registry=(),
        kb_entries=[
            {
                "term": "飞书",
                "label": "飞书",
                "package": package,
                "kind": "device",
            }
        ],
        settings=_settings(lexical=False, embed=False),
    )

    assert result.status in {"unknown", "ambiguous"}
    assert result.winner is None
    if result.candidates:
        assert result.candidates[0].package == package
        assert result.candidates[0].match_type == "pinyin_full"


def test_learned_alias_requires_success_history_for_auto_resolution():
    base_entry = {
        "term": "工作浏览器",
        "label": "Firefox",
        "package": "org.mozilla.firefox",
        "kind": "learned",
    }

    without_success = resolve_name(
        "工作浏览器",
        registry=(),
        kb_entries=[{**base_entry, "success_count": 0}],
        settings=_settings(pinyin=False, embed=False),
    )
    with_success = resolve_name(
        "工作浏览器",
        registry=(),
        kb_entries=[{**base_entry, "success_count": 2}],
        settings=_settings(pinyin=False, embed=False),
    )

    assert without_success.status == "ambiguous"
    assert without_success.winner is None
    assert with_success.status == "resolved"
    assert with_success.winner is not None
    assert with_success.winner.authority == "learned"


def test_legacy_mode_keeps_score_threshold_resolution_for_pinyin():
    registry = AppRegistry([_identity("wechat", "com.tencent.mm", "微信")])

    result = resolve_name(
        "wx",
        registry=registry,
        settings=_settings(decision_mode="legacy", lexical=False, embed=False),
    )

    assert result.status == "resolved"
    assert result.winner is not None
    assert result.winner.source_route == "pinyin"


def test_clarify_only_candidate_is_ambiguous_not_a_guess():
    result = resolve_name("飞猪", settings=_settings(embed=False))

    assert result.status in {"unknown", "ambiguous"}
    assert result.winner is None


def test_optional_embedding_route_failure_is_fail_open():
    registry = AppRegistry([_identity("wechat", "com.tencent.mm", "微信")])

    result = resolve_name(
        "微信",
        registry=registry,
        embedding_search=lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
        settings=_settings(pinyin=False),
    )

    assert result.status == "resolved"
    assert result.winner is not None
    assert result.winner.source_route == "exact"
    assert result.winner.match_type in {"exact_alias", "exact_label"}


def test_launch_anchor_records_trace_and_starts_bili():
    device = FakeDeviceFactory(installed=frozenset({BILI_PACKAGE}))
    session = FakePhoneSession({}, device_factory=device)
    events = []
    session.resolution_trace_recorder = lambda event, **payload: events.append(
        (event, payload)
    )
    config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]

    result = launch.invoke({"app_name": "哔哩哔哩"})

    assert _text(result).startswith(f"OK. launched 哔哩哔哩 ({BILI_PACKAGE})")
    assert device.calls == [("launch_app", BILI_PACKAGE)]
    assert events[-1][0] == "resolution_attempt"
    assert events[-1][1]["decision"] == "resolved"
    assert events[-1][1]["winner"] == BILI_PACKAGE
    assert events[-1][1]["match_type"] == "registered_containment"
    assert events[-1][1]["decision_basis"] == "typed:auto:registered_containment"
    assert events[-1][1]["candidates"][0]["match_type"] == "registered_containment"


def test_launch_package_segment_starts_installed_piliplus_and_firefox():
    for mention, package in (
        ("PiliPlus", "com.example.piliplus"),
        ("Firefox", "org.mozilla.firefox"),
    ):
        device = FakeDeviceFactory(installed=frozenset({package}))
        session = FakePhoneSession({}, device_factory=device)
        config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
        launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
            "launch_app"
        ]

        result = launch.invoke({"app_name": mention})

        assert _text(result).startswith(f"OK. launched {mention} ({package})")
        assert device.calls == [("launch_app", package)]


def test_launch_feizhu_never_starts_installed_feishu():
    package = "com.ss.android.lark"
    device = FakeDeviceFactory(installed=frozenset({package}))
    session = FakePhoneSession({}, device_factory=device)
    config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]

    result = launch.invoke({"app_name": "飞猪"})

    assert isinstance(result, str)
    assert not result.startswith("OK.")
    assert device.calls == []


def test_resolution_attempt_lands_in_production_trace(tmp_path):
    device = FakeDeviceFactory(installed=frozenset({BILI_PACKAGE}))
    session = FakePhoneSession({}, device_factory=device)
    trace = TraceMiddleware("resolver-trace", trace_dir=str(tmp_path))
    session.resolution_trace_recorder = trace.record_event
    config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]

    launch.invoke({"app_name": "哔哩哔哩"})

    events = [
        json.loads(line)
        for line in (tmp_path / "resolver-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    attempt = events[-1]
    assert attempt["event"] == "resolution_attempt"
    assert attempt["mention"] == "哔哩哔哩"
    assert attempt["decision"] == "resolved"
    assert attempt["winner"] == BILI_PACKAGE
    assert attempt["match_type"] == "registered_containment"
    assert attempt["authority"] == "registry"
    assert attempt["decision_basis"] == "typed:auto:registered_containment"
    assert "reason" in attempt
    assert attempt["candidates"][0]["match_type"] == "registered_containment"


def test_launch_ambiguous_receipt_is_ranked_and_executes_nothing():
    packages = frozenset({"com.example.wechat.personal", "com.example.wechat.work"})
    session = FakePhoneSession({}, device_factory=FakeDeviceFactory(installed=packages))
    session.app_knowledge = SimpleNamespace(
        entries=lambda: [
            {
                "term": "微信",
                "label": "微信工作版",
                "package": "com.example.wechat.work",
                "kind": "device",
            },
            {
                "term": "微信",
                "label": "微信个人版",
                "package": "com.example.wechat.personal",
                "kind": "device",
            },
        ]
    )
    config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]

    result = launch.invoke({"app_name": "微信"})

    assert result.startswith("ambiguous app")
    assert result.index("com.example.wechat.personal") < result.index(
        "com.example.wechat.work"
    )
    assert session.device_factory.calls == []


def test_name_candidate_cannot_bypass_observation_only_policy():
    package = "com.android.launcher3"
    session = FakePhoneSession(
        {}, device_factory=FakeDeviceFactory(installed=frozenset({package}))
    )
    session.app_knowledge = SimpleNamespace(
        entries=lambda: [
            {
                "term": "秘密桌面",
                "label": "秘密桌面",
                "package": package,
                "kind": "device",
            }
        ]
    )
    config = SimpleNamespace(device_id="serial-1", resolver_embed=False)
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]

    result = launch.invoke({"app_name": "秘密桌面"})

    assert result.startswith("denied:")
    assert session.device_factory.calls == []
