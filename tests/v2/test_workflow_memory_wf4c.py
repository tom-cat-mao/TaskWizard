"""WP-WF4-C: injector expansion — app-rule delivery + mention prefetch.

Scope (design 增补 v3, Q1-Q5): the entrance (``app/launched``) delivery now
also carries the app's injectable rules (``scope.app ==`` package, device
matched, ``approved``/``auto_approved``) inside a separate ≤2-item/200-token
budget rendered as an ``[APP_RULES]`` section of the same one-shot system
message (C1); run start resolves apps mentioned in the goal through the
deterministic typed resolver and prefetches card + rules for at most two
resolved apps, sharing the per-package per-run delivered set with the
entrance channel (C2); the trace/episode audit plane records
``point=run_start_mention``/``app_launched``, ``app_package``, ``lesson_ids``
and reuses ``injected_lessons`` for the delivered rule ids (C3).  ``shadow``
selects and records statistics but never injects.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.evolution import (
    APP_RULES_MAX_ITEMS,
    APP_RULES_MAX_TOKENS,
    LessonCandidate,
    LessonStore,
    make_lesson_id,
    select_app_rules_for_injection,
)
from phone_agent.v2.middleware._tokens import estimate_text_tokens
from phone_agent.v2.middleware.procedure import (
    APP_RULES_PREFIX,
    PROCEDURE_CARD_PREFIX,
    ProcedureCardInjector,
)
from phone_agent.v2.names import mentioned_apps
from phone_agent.v2.recall import (
    HashEmbedder,
    ProcedureSelection,
    VecIndex,
    load_procedure_lessons,
)
from phone_agent.v2.session import PhoneSession

from tests.v2._doubles import FakePhoneSession
from tests.v2.test_experience import _install_mini_agent_modules

APP = "com.example.flashnote"
OTHER = "com.example.rides"
CARD_ID = "les_0000000000c1"
GOAL = "帮我在闪记里记录一条行程"
MENTION_TERM = "闪记"


def _rule(
    lesson_id: str,
    text: str,
    *,
    app: str | None = APP,
    device: str | None = None,
    app_version: str | None = None,
    status: str = "approved",
    version: int = 1,
    created_ts: float = 1.0,
) -> dict:
    return {
        "lesson_id": lesson_id,
        "schema_v": 1,
        "version": version,
        "status": status,
        "kind": "rule",
        "text": text,
        "scope": {"device": device, "app": app, "app_version": app_version},
        "evidence": [{"run_id": "run-1", "note": "support"}],
        "support_count": 1,
        "task_keys": ["open_app"],
        "conflicts": [],
        "created_ts": created_ts,
        "source": "distill",
    }


def _card(text: str, *, app_scope: str) -> dict:
    return {
        "lesson_id": make_lesson_id(f"{text}|{app_scope}", {}),
        "schema_v": 1,
        "version": 1,
        "status": "auto_approved",
        "kind": "procedure",
        "text": text,
        "steps": ["打开闪记应用", "在输入框记录一条行程并保存"],
        "pitfalls": None,
        "app_scope": app_scope,
        "scope": {"device": None, "app": None, "app_version": None},
        "evidence": [
            {"run_id": run, "note": "outcome pattern"} for run in ("a", "b", "c")
        ],
        "support_count": 3,
        "task_keys": ["search"],
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
    }


def _write_rules(tmp_path: Path, payloads: list[dict]) -> Path:
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    (lessons_dir / "lessons.json").write_text(
        json.dumps(payloads, ensure_ascii=False), encoding="utf-8"
    )
    return lessons_dir


def _kb_session(entries: list[dict]) -> SimpleNamespace:
    knowledge = SimpleNamespace(entries=lambda: list(entries))
    return SimpleNamespace(app_knowledge=knowledge)


def _card_selector(*, target: str = APP, card_id: str = CARD_ID):
    calls: list[str | None] = []

    def selector(goal, *, app_package, device_id):
        calls.append(app_package)
        if app_package != target:
            return ProcedureSelection(
                candidates=1, filtered=0, reason="app_scope_mismatch"
            )
        return ProcedureSelection(
            lesson_id=card_id,
            title="闪记记录行程",
            steps=("打开闪记应用", "记录一条行程"),
            app_scope=target,
            score=0.9,
            candidates=1,
            filtered=1,
            reason="hit",
        )

    selector.calls = calls
    return selector


def _injector(
    tmp_path: Path,
    *,
    mode: str = "on",
    selector=None,
    rules: list[dict] | None = None,
    with_index: bool = True,
    session=None,
) -> tuple[ProcedureCardInjector, list[tuple]]:
    lessons_dir = _write_rules(tmp_path, rules or [])
    config = SimpleNamespace(
        memory_rag=mode,
        vec_db=str(tmp_path / "vec.db") if with_index else None,
        memory_dir=str(tmp_path / "memory"),
        device_id="serial-1",
        lessons_dir=str(lessons_dir),
    )
    trace_events: list[tuple] = []
    trace = SimpleNamespace(
        record_event=lambda event, **payload: trace_events.append((event, payload))
    )
    injector = ProcedureCardInjector(
        config,
        session=session if session is not None else SimpleNamespace(),
        trace=trace,
        selector=selector,
    )
    return injector, trace_events


def _stats(tmp_path: Path) -> dict:
    path = tmp_path / "memory" / "experience" / "recall_stats.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _entries(*terms_packages: tuple[str, str]) -> list[dict]:
    return [
        {"term": term, "label": term, "package": package,
         "kind": "learned", "success_count": 3}
        for term, package in terms_packages
    ]


# --- C1 selector: select_app_rules_for_injection ------------------------


def test_app_rule_selector_scopes_to_package_device_and_status(tmp_path):
    payloads = [
        _rule("les_00000000000a", "笔记规则甲"),
        _rule("les_00000000000b", "笔记规则乙", status="auto_approved"),
        _rule("les_00000000000c", "本机设备规则", device="serial-1"),
        _rule("les_00000000000d", "别家应用规则", app=OTHER),
        _rule("les_00000000000e", "全局规则", app=None),
        _rule("les_00000000000f", "候选规则", status="proposed"),
        _rule("les_000000000010", "复审规则", status="needs_review"),
        _rule("les_000000000011", "已吊销规则", status="revoked"),
        _rule("les_000000000012", "他机规则", device="other-serial"),
        _rule("les_000000000013", "版控规则", app_version="1.2.0"),
    ]
    lessons = _write_rules(tmp_path, payloads)

    # The ≤2-item cap keeps the two id-ordered eligible rules; everything
    # scoped elsewhere or not injectable stays out.
    selected = select_app_rules_for_injection(
        str(lessons), app_package=APP, device_scope="device:serial-1"
    )
    texts = [item.text for item in selected]
    assert "笔记规则甲" in texts and "笔记规则乙" in texts
    for excluded in (
        "别家应用规则",
        "全局规则",
        "候选规则",
        "复审规则",
        "已吊销规则",
        "他机规则",
        "版控规则",
    ):
        assert excluded not in texts

    # A device-pinned rule matches its own device; device-None app rules match
    # every device. (app=None rules belong to the run-start mirror, not here.)
    device_rules = _write_rules(
        tmp_path / "device",
        [
            _rule("les_00000000000c", "本机设备规则", device="serial-1"),
            _rule("les_00000000000e", "全设备应用规则", device=None),
        ],
    )
    selected = select_app_rules_for_injection(
        str(device_rules), app_package=APP, device_scope="device:serial-1"
    )
    assert [item.text for item in selected] == [
        "本机设备规则",
        "全设备应用规则",
    ]
    other = select_app_rules_for_injection(
        str(device_rules), app_package=APP, device_scope="device:other"
    )
    assert [item.text for item in other] == ["全设备应用规则"]


def test_app_rule_selector_excludes_procedure_kind(tmp_path):
    procedure = _card("闪记记录行程的过程卡", app_scope=APP)
    procedure["scope"]["app"] = APP
    payloads = [
        _rule("les_00000000000a", "普通规则"),
        procedure,
    ]
    lessons = _write_rules(tmp_path, payloads)

    selected = select_app_rules_for_injection(
        str(lessons), app_package=APP, device_scope=None
    )

    assert [item.text for item in selected] == ["普通规则"]


def test_app_rule_selector_budget_caps_items_and_tokens(tmp_path):
    payloads = [
        _rule(
            "les_00000000000a",
            "早规则" + "内容" * 300,
            version=1,
            created_ts=1.0,
        ),
        _rule("les_00000000000b", "中间规则", version=2, created_ts=2.0),
        _rule("les_00000000000c", "新规则一", version=3, created_ts=3.0),
        _rule("les_00000000000d", "新规则二", version=3, created_ts=4.0),
    ]
    lessons = _write_rules(tmp_path, payloads)

    selected = select_app_rules_for_injection(
        str(lessons), app_package=APP, device_scope=None
    )

    # ≤2 items: the newest two win; the huge version-1 rule never fits.
    assert [item.text for item in selected] == ["新规则二", "新规则一"]
    assert all(
        estimate_text_tokens(item.text) <= APP_RULES_MAX_TOKENS for item in selected
    )

    # Token trimming: two roomy rules keep only what fits 200 tokens.
    roomy = _write_rules(
        tmp_path / "roomy",
        [
            _rule("les_00000000000a", "甲" * 600),
            _rule("les_00000000000b", "乙" * 600, created_ts=2.0),
        ],
    )
    trimmed = select_app_rules_for_injection(
        str(roomy), app_package=APP, device_scope=None
    )
    assert len(trimmed) < APP_RULES_MAX_ITEMS
    assert sum(estimate_text_tokens(item.text) for item in trimmed) <= APP_RULES_MAX_TOKENS


def test_app_rule_selector_fails_open_on_damaged_view(tmp_path):
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    (lessons / "lessons.json").write_text("{broken", encoding="utf-8")
    assert (
        select_app_rules_for_injection(
            str(lessons), app_package=APP, device_scope=None
        )
        == []
    )
    assert (
        select_app_rules_for_injection(
            str(tmp_path / "missing"), app_package=APP, device_scope=None
        )
        == []
    )
    assert (
        select_app_rules_for_injection(
            str(tmp_path), app_package="", device_scope=None
        )
        == []
    )


# --- C2 mention extraction: names.mentioned_apps ------------------------


def test_mentioned_apps_resolves_unique_learned_mention():
    entries = _entries((MENTION_TERM, APP))

    resolved = mentioned_apps(GOAL, registry=(), kb_entries=entries)

    assert [item.winner.package for item in resolved] == [APP]
    assert resolved[0].status == "resolved"


def test_mentioned_apps_drops_ambiguous_and_unknown_mentions():
    entries = _entries((MENTION_TERM, APP), (MENTION_TERM, OTHER))

    # Same spelling owned by two packages: the typed decision is ambiguous.
    assert mentioned_apps("打开闪记", registry=(), kb_entries=entries) == []
    # No source spelling occurs in the text at all.
    assert mentioned_apps("查一下天气", registry=(), kb_entries=entries) == []


def test_mentioned_apps_orders_by_mention_position_and_dedupes_packages():
    entries = _entries(("闪记", APP), ("速记", OTHER), ("备忘", APP))

    text = "先用速记，再用备忘和闪记各记一笔"
    resolved = mentioned_apps(text, registry=(), kb_entries=entries)

    # Mention order: 速记 before 备忘/闪记; 备忘 and 闪记 dedupe to one package.
    assert [item.winner.package for item in resolved] == [OTHER, APP]


# The cap=2 contract is exercised at the consumption point by
# test_mention_prefetch_caps_at_two_apps_in_mention_order below.


# --- C1 entrance delivery: card + [APP_RULES] ---------------------------


def test_entrance_delivery_merges_card_and_app_rules(tmp_path):
    rules = [
        _rule("les_00000000000a", "进场规则一"),
        _rule("les_00000000000b", "进场规则二", status="auto_approved"),
    ]
    selector = _card_selector()
    injector, trace = _injector(tmp_path, selector=selector, rules=rules)

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP, "device_id": "serial-1"})
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )

    assert len(messages) == 2
    content = messages[-1].content
    assert content.startswith(PROCEDURE_CARD_PREFIX)
    assert APP_RULES_PREFIX in content
    assert "进场规则一" in content and "进场规则二" in content
    assert "仅供参考" in content
    assert estimate_text_tokens(content.split(APP_RULES_PREFIX, 1)[1]) <= (
        APP_RULES_MAX_TOKENS + 50
    )
    # C3 audit: one trace event with the card id followed by the rule ids.
    assert trace == [
        (
            "procedure_injection",
            {
                "point": "app_launched",
                "lesson_ids": [CARD_ID, "les_00000000000a", "les_00000000000b"],
                "count": 3,
                "app_package": APP,
            },
        )
    ]
    assert injector.injected_ids == [CARD_ID]
    assert injector.injected_rule_ids == ["les_00000000000a", "les_00000000000b"]


def test_entrance_rule_budget_truncates_to_two_items(tmp_path):
    rules = [
        _rule("les_00000000000a", "规则甲"),
        _rule("les_00000000000b", "规则乙"),
        _rule("les_00000000000c", "规则丙"),
    ]
    injector, trace = _injector(
        tmp_path, selector=_card_selector(), rules=rules
    )

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP})
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )

    content = messages[-1].content
    assert "规则甲" in content and "规则乙" in content
    assert "规则丙" not in content
    assert injector.injected_rule_ids == ["les_00000000000a", "les_00000000000b"]
    assert trace[0][1]["lesson_ids"] == [CARD_ID, "les_00000000000a", "les_00000000000b"]


def test_entrance_excludes_rules_of_other_devices(tmp_path):
    rules = [
        _rule("les_00000000000a", "本机规则", device="serial-1"),
        _rule("les_00000000000b", "他机规则", device="other-serial"),
    ]
    injector, _trace = _injector(
        tmp_path, selector=_card_selector(), rules=rules
    )

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP})
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )

    content = messages[-1].content
    assert "本机规则" in content
    assert "他机规则" not in content


def test_entrance_delivers_rules_even_without_a_card_hit(tmp_path):
    rules = [_rule("les_00000000000a", "仅规则通道")]
    injector, trace = _injector(
        tmp_path,
        selector=lambda _goal, **_kwargs: ProcedureSelection(
            candidates=1, filtered=0, reason="app_scope_mismatch"
        ),
        rules=rules,
        with_index=False,
    )

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP})
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )

    content = messages[-1].content
    assert content.startswith(APP_RULES_PREFIX)
    assert PROCEDURE_CARD_PREFIX not in content
    assert "仅规则通道" in content
    assert injector.pending_lesson_id is None
    assert injector.injected_ids == []
    assert injector.injected_rule_ids == ["les_00000000000a"]
    assert trace[0][1]["lesson_ids"] == ["les_00000000000a"]


def test_entrance_shadow_selects_but_never_injects(tmp_path):
    rules = [_rule("les_00000000000a", "影子规则")]
    selector = _card_selector()
    injector, trace = _injector(
        tmp_path, mode="shadow", selector=selector, rules=rules
    )

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP})

    assert injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert trace == []
    assert injector.injected_ids == []
    assert injector.injected_rule_ids == []
    # The card selection was still measured (shadow bookkeeping).
    stats = _stats(tmp_path)
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (1, 1)


def test_entrance_off_mode_never_selects(tmp_path):
    selector = _card_selector()
    injector, trace = _injector(tmp_path, mode="off", selector=selector)

    injector.run_start(GOAL)
    injector.on_app_launched({"package": APP})

    assert selector.calls == []
    assert injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert trace == []
    assert _stats(tmp_path) == {}


# --- C2 mention prefetch at run start -----------------------------------


def test_mention_prefetch_delivers_before_the_first_model_call(tmp_path):
    rules = [_rule("les_00000000000a", "预取规则")]
    selector = _card_selector()
    session = _kb_session(_entries((MENTION_TERM, APP)))
    injector, trace = _injector(
        tmp_path, selector=selector, rules=rules, session=session
    )

    injector.run_start(GOAL)
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )

    assert len(messages) == 2
    content = messages[-1].content
    assert PROCEDURE_CARD_PREFIX in content and APP_RULES_PREFIX in content
    assert "预取规则" in content
    assert trace == [
        (
            "procedure_injection",
            {
                "point": "run_start_mention",
                "lesson_ids": [CARD_ID, "les_00000000000a"],
                "count": 2,
                "app_package": APP,
            },
        )
    ]
    assert injector.injected_rule_ids == ["les_00000000000a"]

    # C2 dedup: the prefetched package is never delivered again on entrance.
    injector.on_app_launched({"package": APP, "device_id": "serial-1"})
    assert injector.on_pre_request(messages, next=lambda payload: payload) == messages
    assert selector.calls == [None, APP]
    assert injector.injected_ids == [CARD_ID]


def test_mention_prefetch_skips_ambiguous_and_unknown_mentions(tmp_path):
    ambiguous = _kb_session(_entries((MENTION_TERM, APP), (MENTION_TERM, OTHER)))
    selector = _card_selector()
    injector_a, trace_a = _injector(
        tmp_path / "ambiguous", selector=selector, session=ambiguous
    )
    injector_a.run_start("打开闪记看一眼")

    assert injector_a.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert trace_a == []
    assert selector.calls == [None]  # only the run-start general attempt

    unknown = _kb_session(_entries((MENTION_TERM, APP)))
    injector_b, trace_b = _injector(
        tmp_path / "unknown", selector=selector, session=unknown
    )
    injector_b.run_start("这个目标没有提到任何应用")

    assert injector_b.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert trace_b == []


def test_mention_prefetch_caps_at_two_apps_in_mention_order(tmp_path):
    entries = _entries(
        (MENTION_TERM, APP), ("速记", OTHER), ("云记", "com.example.cloud")
    )
    session = _kb_session(entries)
    selector = _card_selector()
    injector, _trace = _injector(tmp_path, selector=selector, session=session)

    injector.run_start("先用速记，再用云记，最后用闪记各记一笔")

    # Only the first two mentioned apps (速记, 云记) were prefetched; 闪记 was
    # never selected for, and later entrance for it would still be allowed.
    assert selector.calls == [None, OTHER, "com.example.cloud"]


def test_mention_prefetch_shadow_records_but_does_not_inject(tmp_path):
    rules = [_rule("les_00000000000a", "影子预取规则")]
    selector = _card_selector()
    session = _kb_session(_entries((MENTION_TERM, APP)))
    injector, trace = _injector(
        tmp_path, mode="shadow", selector=selector, rules=rules, session=session
    )

    injector.run_start(GOAL)

    assert injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert trace == []
    assert injector.injected_ids == []
    assert injector.injected_rule_ids == []
    # The prefetch selection was measured, and the shared dedup set keeps a
    # later entrance event from selecting the same package again.
    stats = _stats(tmp_path)
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (1, 1)
    injector.on_app_launched({"package": APP})
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (
        (_stats(tmp_path)["procedure_runs"], _stats(tmp_path)["procedure_hits"])
    )


# --- end to end: run start -> entrance over the real capability seam -----


class _EmittingSession(FakePhoneSession):
    """FakePhoneSession carrying the real ``app/launched`` emission surface."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._launched_this_run: set[str] = set()

    def emit_app_launched(self, package: str, device_id: str | None = None) -> bool:
        return PhoneSession.emit_app_launched(self, package, device_id)


def _install_wf4c_mini_modules(monkeypatch):
    """Install launch/finish mini modules AFTER the base mini installer."""

    session = _EmittingSession()
    session.app_knowledge = SimpleNamespace(
        entries=lambda: _entries((MENTION_TERM, APP))
    )
    seen: list[list] = []

    @tool
    def launch_app(app_name: str, intent: str = "") -> str:
        """Launch an app."""
        session.record_launched_app(APP)
        session.emit_app_launched(APP, "serial-mini")
        return f"OK. launched {app_name} ({APP})"

    @tool
    def finish(summary: str, evidence: list[str], intent: str = "") -> str:
        """Finish the mini run with evidence."""
        session.finished = True
        session.finish_summary = summary
        return "已记录完成声明"

    class Model:
        def __init__(self):
            self.responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "launch_app",
                            "args": {"app_name": "闪记", "intent": "打开闪记"},
                            "id": "launch-1",
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "finish",
                            "args": {
                                "summary": "done",
                                "evidence": ["screen"],
                                "intent": "finish",
                            },
                            "id": "finish-1",
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                ),
                AIMessage(content="done"),
            ]

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages, config=None, **kwargs):
            seen.append(list(messages))
            return self.responses.pop(0)

    model = Model()
    import sys
    import types

    modules = {
        "phone_agent.v2.model": ("build_chat_model", lambda config: model),
        "phone_agent.v2.session": ("PhoneSession", lambda config: session),
        "phone_agent.v2.tools": (
            "build_tools",
            lambda session, config: [launch_app, finish],
        ),
        "phone_agent.v2.prompts": ("get_system_prompt", lambda lang="cn": "system"),
    }
    for name, (attribute, value) in modules.items():
        module = types.ModuleType(name)
        setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)
    return session, seen


def test_run_delivers_prefetch_then_dedups_entrance(tmp_path, monkeypatch):
    lessons = tmp_path / "lessons"
    rule = _rule("les_00000000000a", "端到端应用规则", status="proposed")
    card = _card(GOAL, app_scope=APP)
    store = LessonStore(lessons)
    store.approve(store.propose(LessonCandidate.from_dict(rule)).lesson_id)
    store.propose(LessonCandidate.from_dict(card))
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons))
    card_id = card["lesson_id"]

    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
    session, seen = _install_wf4c_mini_modules(monkeypatch)
    config.trace_enabled = True
    config.memory_rag = "on"
    config.memory_dir = str(tmp_path / "memory")
    config.vec_db = str(tmp_path / "vec.db")
    config.embed_model = "hash-v1"
    config.embed_dim = 64
    config.lessons_dir = str(lessons)
    monkeypatch.setattr(
        "phone_agent.v2.recall.MlxEmbedder",
        lambda *_args, **_kwargs: HashEmbedder(64),
    )

    agent = ThinPhoneAgent(config)
    assert agent.run(GOAL).success is True
    assert APP in session.launched_apps

    # The model saw the prefetch delivery (card + [APP_RULES]) at the FIRST
    # call, and the launch produced no second delivery for the same package.
    def _count(messages, marker):
        return sum(
            1
            for message in messages
            if marker in str(getattr(message, "content", ""))
        )

    assert _count(seen[0], PROCEDURE_CARD_PREFIX) == 1
    assert _count(seen[0], APP_RULES_PREFIX) == 1
    assert _count(seen[1], PROCEDURE_CARD_PREFIX) == 1
    first_call = "\n".join(
        str(getattr(message, "content", "")) for message in seen[0]
    )
    assert "端到端应用规则" in first_call
    assert card_id in first_call

    trace = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    injections = [
        event for event in trace if event["event"] == "procedure_injection"
    ]
    assert [event["point"] for event in injections] == ["run_start_mention"]
    assert injections[0]["app_package"] == APP
    assert injections[0]["lesson_ids"] == [card_id, "les_00000000000a"]

    outcomes = [
        json.loads(line)
        for line in (tmp_path / "experience" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    outcomes = [event for event in outcomes if event["type"] == "episode_outcome"]
    assert outcomes[0]["injected_procedures"] == [card_id]
    # C3: the delivered app rule is booked under injected_lessons.
    assert outcomes[0]["injected_lessons"] == ["les_00000000000a"]
