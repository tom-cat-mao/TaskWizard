"""WP-WF3: the two real procedure-card injection points.

Scope: the ``app/launched`` event (emitted once per package per run on a
device-confirmed ``launch_app`` success), the run-start **general** card
injection through the recall capability's second prompt provider, the
post-launch **app-scoped** card injected as a one-shot ``[PROCEDURE_CARD]``
system message at the next ``model/pre_request``, the separate 1-card/300-token
budget per point (never shared with the 3-item/800-token rule channel), and the
trace/episode audit of injected card ids.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types
from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.evolution import LessonCandidate, LessonStore, make_lesson_id
from phone_agent.v2.events import APP_LAUNCHED, EventBus
from phone_agent.v2.middleware._tokens import estimate_text_tokens
from phone_agent.v2.middleware.procedure import (
    PROCEDURE_CARD_PREFIX,
    ProcedureCardInjector,
)
from phone_agent.v2.recall import (
    PROCEDURE_MAX_TOKENS,
    HashEmbedder,
    ProcedureSelection,
    VecIndex,
    load_procedure_lessons,
)
from phone_agent.v2.session import PhoneSession
from phone_agent.v2.tools.actuation import build_actuation_tools

from tests.v2._doubles import FakeDeviceFactory, FakePhoneSession
from tests.v2.test_experience import _install_mini_agent_modules

FOOD = "com.example.food"
WEATHER = "com.example.weather"
WECHAT = "com.tencent.mm"
FOOD_TITLE = "外卖应用下单到结算"
WEATHER_TITLE = "天气应用查预报"
GENERAL_TITLE = "天气应用查预报的通用流程"
FOOD_STEPS = ["搜索框输入餐厅", "选店进入", "加购菜品", "到结算页停手问人"]
GENERAL_STEPS = ["打开天气应用", "输入城市名", "浏览结果列表"]
WEATHER_STEPS = ["打开天气应用", "输入城市名", "查看预报"]
# Both cards above are indexed against a goal that names the same task, so the
# deterministic test embedder reproduces a real hit instead of a coin flip.
GOAL = "天气应用查预报"
_STEP_BODY = (
    "在页面上找到对应入口并确认当前步骤已经完成后再继续下一步操作，"
    "记录观察到的结果并核对是否与预期一致，出现异常时先停下来重新观察当前屏幕"
)
LONG_STEPS = [f"第{index}步：{_STEP_BODY}" for index in range(1, 40)]


def _payload(*, text: str, steps: list[str], app_scope: str) -> dict:
    return {
        "lesson_id": make_lesson_id(f"{text}|{app_scope}", {}),
        "schema_v": 1,
        "version": 1,
        "status": "auto_approved",
        "kind": "procedure",
        "text": text,
        "steps": steps,
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


def _index_cards(tmp_path: Path, lessons_dir: Path) -> dict[str, str]:
    """Materialize + index a general, a food, and a weather card."""

    cards = {
        "general": _payload(text=GENERAL_TITLE, steps=GENERAL_STEPS, app_scope="general"),
        FOOD: _payload(text=FOOD_TITLE, steps=FOOD_STEPS, app_scope=FOOD),
        WEATHER: _payload(text=WEATHER_TITLE, steps=WEATHER_STEPS, app_scope=WEATHER),
    }
    ids = {}
    for key, payload in cards.items():
        lesson = LessonStore(lessons_dir).propose(LessonCandidate.from_dict(payload))
        ids[key] = lesson.lesson_id
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons_dir))
    return ids


def _selection(lesson_id: str, *, title: str, steps, app_scope: str) -> ProcedureSelection:
    return ProcedureSelection(
        lesson_id=lesson_id,
        title=title,
        steps=tuple(steps),
        app_scope=app_scope,
        score=0.8,
        candidates=2,
        filtered=1,
        reason="hit",
    )


def _bare_agent(tmp_path: Path, *, mode: str = "on", selector=None) -> ThinPhoneAgent:
    config = SimpleNamespace(
        memory_rag=mode,
        vec_db=str(tmp_path / "vec.db"),
        memory_dir=str(tmp_path / "memory"),
        device_id="serial-1",
        embed_model="hash-v1",
        embed_dim=64,
    )
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = config
    agent._system_prompt = "SYSTEM"
    agent.run_id = "wf3"
    agent.trace_events: list[tuple] = []
    agent.session = SimpleNamespace(
        observe=lambda: SimpleNamespace(
            screenshot_b64="", current_app="launcher", screen_seq=1, marks=[]
        )
    )
    agent._trace = SimpleNamespace(
        record_event=lambda event, **payload: agent.trace_events.append(
            (event, payload)
        )
    )
    agent._procedure_injector = ProcedureCardInjector(
        config,
        session=agent.session,
        trace=agent._trace,
        embedder=HashEmbedder(64),
        selector=selector,
    )
    return agent


def _stats(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "memory" / "experience" / "recall_stats.json").read_text(
            encoding="utf-8"
        )
    )


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(
        block.get("text", "")
        for block in result
        if isinstance(block, dict) and block.get("type") == "text"
    )


# --- app/launched event -------------------------------------------------
# The session-method-level dedup/payload coverage is folded into
# test_launch_app_success_emits_once_per_package (production path through the
# launch_app tool); reset behaviour is guarded in test_workflow_memory_wf4a.


def test_app_launched_emission_is_fail_open():
    class BrokenBus:
        def emit(self, _event, _payload):
            raise RuntimeError("listener exploded")

    session = PhoneSession.__new__(PhoneSession)
    session.event_bus = BrokenBus()
    session.config = SimpleNamespace(device_id="serial-1")
    session._launched_this_run = set()
    assert session.emit_app_launched(WECHAT) is False

    session.event_bus = None
    assert session.emit_app_launched(FOOD) is False


def _launch_session(tmp_path: Path):
    from phone_agent.v2.appkb import AppKnowledge, AppKnowledgeStore

    bus = EventBus()
    config = SimpleNamespace(device_id="serial-1", app_kb_enabled=True)
    device = FakeDeviceFactory(installed=frozenset({WECHAT}))
    session = PhoneSession(config, device_factory=device)
    session.event_bus = bus
    store = AppKnowledgeStore(str(tmp_path / "memory"))
    store.sync_device("serial-1", [(WECHAT, "微信")])
    session.app_store = store
    session.app_knowledge = AppKnowledge(store, device_id="serial-1")
    session._kb_device_id = lambda: "serial-1"
    return session, config, bus


def test_launch_app_success_emits_once_per_package(tmp_path):
    session, config, bus = _launch_session(tmp_path)
    seen: list[dict] = []
    bus.on(APP_LAUNCHED, seen.append)
    launch = {
        tool.name: tool for tool in build_actuation_tools(session, config)
    }["launch_app"]

    first = launch.invoke({"app_name": "wechat"})
    launch.invoke({"app_name": "wechat"})
    failed = launch.invoke({"app_name": "不存在的软件"})

    assert _text(first).startswith(f"OK. launched wechat ({WECHAT})")
    # The device launched twice, but the event fires once per package per run.
    assert len(session.launched_apps) == 2
    assert seen == [
        {"package": WECHAT, "device_id": "serial-1", "source": "launch_app"}
    ]
    assert _text(failed).startswith("unknown app")
    assert len(seen) == 1


# --- injection point one: run-start general card ------------------------


def test_run_start_general_card_injects_in_on_mode(tmp_path):
    general_id = "les_0123456789ab"
    calls: list[dict] = []

    def selector(goal, *, app_package, device_id):
        calls.append({"goal": goal, "app_package": app_package, "device_id": device_id})
        return _selection(
            general_id, title=GENERAL_TITLE, steps=GENERAL_STEPS, app_scope="general"
        )

    agent = _bare_agent(tmp_path, selector=selector)
    agent._prepare_procedure_injection(GOAL)
    block = agent._procedure_prompt_block()

    assert calls == [{"goal": GOAL, "app_package": None, "device_id": "serial-1"}]
    assert block is not None
    assert block.placement == "system_message"
    assert "仅供参考，不是规则" in block.content
    assert general_id in block.content
    assert GENERAL_TITLE in block.content
    assert estimate_text_tokens(block.content) <= PROCEDURE_MAX_TOKENS
    assert agent.trace_events == [
        (
            "procedure_injection",
            {
                "point": "run_start",
                "lesson_ids": [general_id],
                "count": 1,
                "app_package": None,
            },
        )
    ]
    assert agent._procedure_injector.injected_ids == [general_id]
    stats = _stats(tmp_path)
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (1, 1)
    assert stats["latest_procedure"]["app_scope"] == "general"


def test_run_start_shadow_and_off_do_not_inject(tmp_path):
    calls: list[dict] = []

    def selector(_goal, *, app_package, device_id):
        calls.append({"app_package": app_package})
        return _selection(
            "les_0123456789ab",
            title=GENERAL_TITLE,
            steps=GENERAL_STEPS,
            app_scope="general",
        )

    for mode in ("shadow", "off"):
        agent = _bare_agent(tmp_path / mode, mode=mode, selector=selector)
        agent._prepare_procedure_injection(GOAL)
        assert agent._procedure_prompt_block() is None
        assert agent._procedure_injector.injected_ids == []
        assert agent.trace_events == []

    # Nothing was ever selected here: in shadow the pre-existing shadow path
    # owns the run-start measurement, so this point must not double-count it.
    assert calls == []


def test_shadow_run_still_records_the_general_card_outcome(tmp_path, monkeypatch):
    from phone_agent.v2.recall import VecIndex as RealVecIndex

    calls: list[dict] = []
    selection = _selection(
        "les_0123456789ab",
        title=GENERAL_TITLE,
        steps=GENERAL_STEPS,
        app_scope="general",
    )

    class FakeIndex:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def recall(self, *_args, **_kwargs):
            return []

        def select_procedure(self, _goal, **kwargs):
            calls.append(kwargs)
            return selection

    monkeypatch.setattr("phone_agent.v2.recall.VecIndex", FakeIndex)
    try:
        agent = _bare_agent(tmp_path, mode="shadow")
        agent._recall_run_start({"task": GOAL, "device_scope": "device:serial-1"})
        assert agent._procedure_prompt_block() is None
        agent._shadow_recall_finish()
    finally:
        monkeypatch.setattr("phone_agent.v2.recall.VecIndex", RealVecIndex)

    assert calls == [{"app_package": None, "device_id": "serial-1"}]
    stats = _stats(tmp_path)
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (1, 1)
    assert stats["latest_procedure"]["lesson_id"] == selection.lesson_id


def test_run_start_zero_recall_is_recorded_with_its_reason(tmp_path):
    def selector(_goal, *, app_package, device_id):
        return ProcedureSelection(candidates=2, filtered=0, reason="app_scope_mismatch")

    agent = _bare_agent(tmp_path, selector=selector)
    agent._prepare_procedure_injection(GOAL)

    assert agent._procedure_prompt_block() is None
    assert agent._procedure_injector.injected_ids == []
    assert agent.trace_events == []
    stats = _stats(tmp_path)
    assert (stats["procedure_runs"], stats["procedure_hits"]) == (1, 0)
    assert stats["procedure_reasons"] == {"app_scope_mismatch": 1}
    assert stats["latest_procedure"]["reason"] == "app_scope_mismatch"


# --- injection point two: post-launch app card --------------------------


def test_post_launch_card_is_injected_once_at_the_next_pre_request(tmp_path):
    agent = _bare_agent(tmp_path)
    injector = agent._procedure_injector
    injector.run_start(GOAL)
    assert injector.injected_ids == []

    selected: list[dict] = []

    def selector(goal, *, app_package, device_id):
        selected.append({"goal": goal, "app_package": app_package})
        return _selection(
            "les_food000001", title=FOOD_TITLE, steps=FOOD_STEPS, app_scope=FOOD
        )

    injector._selector = selector
    injector.on_app_launched({"package": FOOD, "device_id": "serial-1"})
    assert injector.pending_lesson_id == "les_food000001"

    messages = [SystemMessage(content="SYSTEM")]
    first = injector.on_pre_request(messages, next=lambda payload: payload)
    second = injector.on_pre_request(first, next=lambda payload: payload)

    assert len(first) == 2
    assert first[-1].content.startswith(PROCEDURE_CARD_PREFIX)
    assert "les_food000001" in first[-1].content
    assert FOOD_TITLE in first[-1].content
    assert estimate_text_tokens(first[-1].content) <= PROCEDURE_MAX_TOKENS
    # One-shot: the next pre-request adds nothing, and the same package is
    # never selected twice in one run.
    assert second == first
    injector.on_app_launched({"package": FOOD, "device_id": "serial-1"})
    assert injector.on_pre_request(second, next=lambda payload: payload) == second
    assert selected == [{"goal": GOAL, "app_package": FOOD}]
    assert injector.injected_ids == ["les_food000001"]


def test_post_launch_card_is_hard_filtered_to_the_launched_app(tmp_path):
    lessons = tmp_path / "lessons"
    ids = _index_cards(tmp_path, lessons)
    agent = _bare_agent(tmp_path)
    injector = agent._procedure_injector
    injector.run_start(GOAL)

    injector.on_app_launched({"package": WEATHER, "device_id": "serial-1"})
    weather_messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )
    card = weather_messages[-1].content

    assert ids[WEATHER] in card
    assert ids[FOOD] not in card
    assert ids["general"] not in card

    # A different package later in the same run selects its own card.
    injector.goal = FOOD_TITLE
    injector.on_app_launched({"package": FOOD, "device_id": "serial-1"})
    food_messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )
    assert ids[FOOD] in food_messages[-1].content
    assert ids[WEATHER] not in food_messages[-1].content


def test_revoked_card_never_reaches_a_later_injection_point(tmp_path):
    agent = _bare_agent(tmp_path)
    injector = agent._procedure_injector
    def selector(_goal, *, app_package, device_id):
        if app_package is None:
            return _selection(
                "les_general0001",
                title=GENERAL_TITLE,
                steps=GENERAL_STEPS,
                app_scope="general",
            )
        return _selection(
            "les_food000001", title=FOOD_TITLE, steps=FOOD_STEPS, app_scope=app_package
        )

    injector._selector = selector
    injector.run_start(GOAL)
    injector.on_app_launched({"package": FOOD, "device_id": "serial-1"})
    assert injector.pending_lesson_id == "les_food000001"

    injector.revoke("les_food000001")

    assert injector.pending_lesson_id is None
    assert injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert injector.injected_ids == ["les_general0001"]
    # A later launch of the same app cannot re-select the revoked card either.
    injector.on_app_launched({"package": WEATHER, "device_id": "serial-1"})
    assert injector.pending_lesson_id is None


def test_post_launch_card_missing_for_that_app_injects_nothing(tmp_path):
    lessons = tmp_path / "lessons"
    _index_cards(tmp_path, lessons)
    agent = _bare_agent(tmp_path)
    injector = agent._procedure_injector
    injector.run_start(GOAL)

    before = injector.injected_ids
    injector.on_app_launched({"package": "com.example.rides", "device_id": "serial-1"})

    assert injector.pending_lesson_id is None
    assert injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    ) == [SystemMessage(content="SYSTEM")]
    assert injector.injected_ids == before
    assert _stats(tmp_path)["latest_procedure"]["reason"] == "app_scope_mismatch"


def test_post_launch_card_budget_truncates_steps(tmp_path):
    agent = _bare_agent(tmp_path)
    injector = agent._procedure_injector
    injector.run_start(GOAL)
    injector._selector = lambda _goal, **_kwargs: _selection(
        "les_0123456789ab", title=FOOD_TITLE, steps=LONG_STEPS, app_scope=FOOD
    )

    injector.on_app_launched({"package": FOOD, "device_id": "serial-1"})
    messages = injector.on_pre_request(
        [SystemMessage(content="SYSTEM")], next=lambda payload: payload
    )
    card = messages[-1].content
    # The card body keeps the 300-token budget; the ``[PROCEDURE_CARD]`` marker
    # is harness overhead on top of it.
    body = card.split("\n", 1)[1]

    assert estimate_text_tokens(body) <= PROCEDURE_MAX_TOKENS
    assert "截断" in card
    # Steps are dropped, never rewritten.
    rendered = [
        line.split(". ", 1)[1]
        for line in body.splitlines()
        if line[:1].isdigit() and ". " in line
    ]
    assert 0 < len(rendered) < len(LONG_STEPS)
    for step in rendered:
        assert step in LONG_STEPS


# --- budget separation + audit ------------------------------------------


def test_procedure_and_rule_blocks_coexist_with_separate_budgets(tmp_path):
    lessons_dir = tmp_path / "lessons"
    rules = [
        LessonCandidate.from_dict(
            {
                "lesson_id": f"les_{index:012x}",
                "schema_v": 1,
                "version": 1,
                "status": "proposed",
                "text": f"规则 {index}：先观察再操作",
                "scope": {"device": None, "app": None, "app_version": None},
                "evidence": [{"run_id": f"run-{index}", "note": "support"}],
                "support_count": 1,
                "task_keys": ["open_app"],
                "conflicts": [],
                "created_ts": 1.0,
                "source": "distill",
            }
        )
        for index in (1, 2, 3)
    ]
    for rule in rules:
        proposed = LessonStore(lessons_dir).propose(rule)
        LessonStore(lessons_dir).approve(proposed.lesson_id)

    agent = _bare_agent(tmp_path)
    agent.config.lessons_dir = str(lessons_dir)
    # One rule only, though three are approved and the rule budget is roomy:
    # the rule quota is its own and the card quota is untouched by it.
    agent.config.lesson_inject_max = 1
    agent.config.lesson_inject_tokens = 800
    agent._run_injected_lessons = []
    agent._revoked_lesson_ids = set()
    agent._actually_injected_lesson_ids = []
    agent._procedure_injector.run_start(GOAL)
    agent._procedure_injector._selector = lambda _goal, **_kwargs: _selection(
        "les_0123456789ab",
        title=GENERAL_TITLE,
        steps=GENERAL_STEPS,
        app_scope="general",
    )
    agent._prepare_lesson_injection("device:serial-1")
    agent._prepare_procedure_injection(GOAL)
    # Mount through the real recall capability seam so both providers coexist
    # exactly as a production run assembles them.
    ctx = CapabilityAssemblyContext(
        {
            "session": agent.session,
            "config": agent.config,
            "recall_prompt_provider": agent._recall_prompt_block,
            "procedure_prompt_provider": agent._procedure_prompt_block,
        }
    )
    assemble_capabilities(build_capability_registry(agent.config), ctx)
    agent._capability_ctx = ctx

    messages = agent._initial_messages(GOAL)

    bodies = [str(message.content) for message in messages]
    rule_blocks = [body for body in bodies if "历史经验，仅供参考，不是规则" in body]
    procedure_blocks = [body for body in bodies if "历史过程卡，仅供参考" in body]
    assert len(rule_blocks) == 1
    assert len(procedure_blocks) == 1
    assert rule_blocks[0].count("规则 ") == 1
    # Every step of the card survives: the 300-token card budget is separate.
    for step in GENERAL_STEPS:
        assert step in procedure_blocks[0]


class _EmittingSession(FakePhoneSession):
    """FakePhoneSession carrying the real ``app/launched`` emission surface."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._launched_this_run: set[str] = set()

    def emit_app_launched(
        self,
        package: str,
        device_id: str | None = None,
        *,
        source: str = "launch_app",
    ) -> bool:
        """Mirror the WF4 multi-source signature (``launch_app``/``foreground``)."""

        return PhoneSession.emit_app_launched(
            self, package, device_id, source=source
        )


def test_emitting_fake_session_accepts_the_wf4_source_argument():
    """The fixture keeps pace with the multi-source emission surface."""

    session = _EmittingSession()
    bus = EventBus()
    seen: list[dict] = []
    bus.on(APP_LAUNCHED, seen.append)
    session.event_bus = bus

    assert (
        session.emit_app_launched(WEATHER, "serial-mini", source="foreground") is True
    )
    assert session.emit_app_launched(FOOD) is True

    assert [payload["source"] for payload in seen] == ["foreground", "launch_app"]
    assert [payload["package"] for payload in seen] == [WEATHER, FOOD]
    assert seen[0]["device_id"] == "serial-mini"


def _install_launching_mini_modules(monkeypatch, tmp_path):
    session = _EmittingSession()
    seen: list[list] = []

    @tool
    def launch_app(app_name: str, intent: str = "") -> str:
        """Launch an app."""
        session.record_launched_app(WEATHER)
        session.emit_app_launched(WEATHER, "serial-mini")
        return f"OK. launched {app_name} ({WEATHER})"

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
                            "args": {
                                "app_name": "weather",
                                "intent": "打开天气应用",
                            },
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
    modules = {
        "phone_agent.v2.model": ("build_chat_model", lambda config, *args, **kwargs: model),
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


def test_run_audits_both_injection_points_in_trace_and_episode(tmp_path, monkeypatch):
    lessons = tmp_path / "lessons"
    ids = _index_cards(tmp_path, lessons)
    config = _install_mini_agent_modules(monkeypatch, tmp_path, True)
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
    _session, seen = _install_launching_mini_modules(monkeypatch, tmp_path)

    agent = ThinPhoneAgent(config)
    assert agent.run(GOAL).success is True

    trace = [
        json.loads(line)
        for line in Path(agent.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    injections = [event for event in trace if event["event"] == "procedure_injection"]
    assert [event["point"] for event in injections] == ["run_start", "app_launched"]
    assert injections[0]["lesson_ids"] == [ids["general"]]
    assert injections[1]["lesson_ids"] == [ids[WEATHER]]
    assert injections[1]["app_package"] == WEATHER

    outcomes = [
        json.loads(line)
        for line in (tmp_path / "experience" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    outcomes = [event for event in outcomes if event["type"] == "episode_outcome"]
    assert outcomes[0]["injected_procedures"] == [ids["general"], ids[WEATHER]]
    # The never-launched app's card is not in the run's audit trail.
    assert ids[FOOD] not in outcomes[0]["injected_procedures"]

    # The model sees the post-launch card from the next call on, exactly once.
    per_call = [
        sum(
            1
            for message in messages
            if PROCEDURE_CARD_PREFIX in str(getattr(message, "content", ""))
        )
        for messages in seen
    ]
    assert per_call == [0, 1, 1]
    card = next(
        message
        for messages in seen[1:]
        for message in messages
        if PROCEDURE_CARD_PREFIX in str(getattr(message, "content", ""))
    )
    assert ids[WEATHER] in str(card.content)


def test_empty_goal_launch_keeps_the_package_delivery_slot(tmp_path):
    """A launch seen before any goal must not burn that app's one slot."""

    card_id = "les_0123456789ab"
    agent = _bare_agent(
        tmp_path,
        selector=lambda _goal, **_kwargs: _selection(
            card_id, title=WEATHER_TITLE, steps=WEATHER_STEPS, app_scope=WEATHER
        ),
    )
    injector = agent._procedure_injector

    # No run_start yet: nothing is selectable, so the launch is a silent no-op
    # that must *not* mark the package as already delivered.
    injector.on_app_launched({"package": WEATHER})
    assert injector.pending_lesson_id is None
    assert WEATHER not in injector._selected_packages

    # The goal arrives; the very same package is still deliverable.
    injector.run_start(GOAL)
    injector.on_app_launched({"package": WEATHER})
    assert injector.pending_lesson_id == card_id
    assert WEATHER in injector._selected_packages

    # The one-shot-per-package rule is unchanged: a re-launch after the
    # delivery was consumed queues nothing new.
    assert injector.make_delta([]) is not None
    injector.on_app_launched({"package": WEATHER})
    assert injector.pending_lesson_id is None


def test_rule_injection_channel_excludes_procedure_cards(tmp_path: Path) -> None:
    """Regression: an auto_approved procedure must not leak into the rule
    mirror as a one-liner (it has its own channel, budget and format)."""
    import json

    from phone_agent.v2.evolution import select_lessons_for_injection

    lessons = tmp_path / "lessons"
    lessons.mkdir()
    rule = {
        "lesson_id": "les_" + "1" * 16, "schema_v": 1, "version": 1,
        "status": "approved", "text": "权限弹窗一律选仅本次",
        "scope": {"device": None, "app": None, "app_version": None},
        "evidence": [{"run_id": "r1", "note": "ok"}], "support_count": 1,
        "task_keys": ["open_app"], "conflicts": [], "created_ts": 1.0,
        "source": "distill", "kind": "rule",
    }
    card = dict(rule) | {
        "lesson_id": "les_" + "2" * 16, "status": "auto_approved",
        "text": "美团系下单到结算", "kind": "procedure",
        "app_scope": "general", "steps": ["搜索", "选店", "结算前停手"],
        "pitfalls": None,
    }
    (lessons / "lessons.json").write_text(
        json.dumps([rule, card], ensure_ascii=False), encoding="utf-8"
    )
    selected = select_lessons_for_injection(
        str(lessons), device_scope=None, max_items=3, max_tokens=800
    )
    assert [item.text for item in selected] == ["权限弹窗一律选仅本次"]
