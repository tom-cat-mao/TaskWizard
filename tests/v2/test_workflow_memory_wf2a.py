"""WP-WF2a: procedure-card recall namespace, hard-filter selector, and budget.

Scope: the third ``VecIndex`` namespace (``procedure``) over injectable WP-WF1
procedure cards, the deterministic app/device hard filters plus embedding
top-1, the separate 1-card/300-token injection budget, and the shadow
zero-recall stats channel.  Injection points themselves land in WP-WF3.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from types import SimpleNamespace

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.appkb import AppKnowledgeStore
from phone_agent.v2.evolution import (
    LessonCandidate,
    LessonStore,
    make_lesson_id,
)
from phone_agent.v2.middleware._tokens import estimate_text_tokens
from phone_agent.v2.middleware.trace import TraceMiddleware
from phone_agent.v2.recall import (
    PROCEDURE_MAX_TOKENS,
    PROCEDURE_MIN_SCORE,
    HashEmbedder,
    ProcedureSelection,
    VecIndex,
    format_procedure_block,
    incremental_upsert,
    load_procedure_lessons,
    reconcile_index,
    select_procedure,
    update_procedure_recall_stats,
    update_recall_stats,
)

FOOD = "com.example.food"
WEATHER = "com.example.weather"
FOOD_TITLE = "外卖应用下单到结算"
GENERAL_TITLE = "通用信息查找流程"
SPARE_TITLE = "备用机结算前确认流程"
FOOD_STEPS = ["搜索框输入餐厅", "选店进入", "加购菜品", "到结算页停手问人"]
GENERAL_STEPS = ["打开搜索入口", "输入关键词", "浏览结果列表"]
RUNS = ("run-a", "run-b", "run-c")


def _payload(
    *,
    text: str,
    steps: list[str] | None = None,
    app_scope: str = "general",
    device: str | None = None,
    status: str = "auto_approved",
    kind: str = "procedure",
    pitfalls: str | None = None,
) -> dict:
    scope = {"device": device, "app": None, "app_version": None}
    seed = f"{text}|{app_scope}|{device}|{kind}"
    base = {
        "lesson_id": make_lesson_id(seed, scope),
        "schema_v": 1,
        "version": 1,
        "status": status,
        "text": text,
        "scope": scope,
        "evidence": [{"run_id": run, "note": "outcome pattern"} for run in RUNS],
        "support_count": len(RUNS),
        "task_keys": ["search"],
        "conflicts": [],
        "created_ts": 1.0,
        "source": "distill",
    }
    if kind == "procedure":
        base["kind"] = "procedure"
        base["steps"] = FOOD_STEPS if steps is None else steps
        base["pitfalls"] = pitfalls
        base["app_scope"] = app_scope
    return base


def _propose(lessons_dir: Path, payload: dict) -> LessonCandidate:
    return LessonStore(lessons_dir).propose(LessonCandidate.from_dict(payload))


def _food_card(lessons_dir: Path, *, device: str | None = None) -> LessonCandidate:
    return _propose(
        lessons_dir,
        _payload(
            text=FOOD_TITLE,
            steps=FOOD_STEPS,
            app_scope=FOOD,
            device=device,
            pitfalls="开屏广告点右上角跳过",
        ),
    )


def test_procedure_namespace_follows_injectable_status(tmp_path):
    lessons = tmp_path / "lessons"
    db_path = tmp_path / "vec.db"
    card = _food_card(lessons)

    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        assert index.sync_procedure_lessons(load_procedure_lessons(lessons)) == {
            "procedures": 1,
            "removed_procedures": 0,
        }
        assert index.count() == 1
        # Re-syncing an unchanged card must not rewrite or duplicate the row.
        assert index.sync_procedure_lessons(load_procedure_lessons(lessons)) == {
            "procedures": 1,
            "removed_procedures": 0,
        }
        assert index.count() == 1

    # Demotion (evidence loss) removes the row: it is no longer injectable.
    LessonStore(lessons).demote(card.lesson_id, "evidence archived")
    assert load_procedure_lessons(lessons) == []
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        assert index.sync_procedure_lessons(load_procedure_lessons(lessons)) == {
            "procedures": 0,
            "removed_procedures": 1,
        }
        assert index.count() == 0

    # Human approval makes it injectable again and re-upserts the same ref_id.
    LessonStore(lessons).approve(card.lesson_id)
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        assert index.sync_procedure_lessons(load_procedure_lessons(lessons)) == {
            "procedures": 1,
            "removed_procedures": 0,
        }
        assert index.count() == 1


def test_revocation_and_non_injectable_cards_stay_out_of_namespace(tmp_path):
    lessons = tmp_path / "lessons"
    db_path = tmp_path / "vec.db"
    revoked = _food_card(lessons)
    LessonStore(lessons).revoke(revoked.lesson_id, "wrong card")
    _propose(lessons, _payload(text=FOOD_TITLE, app_scope=FOOD, status="proposed"))
    rule = _propose(
        lessons,
        _payload(
            text="查询前应等待搜索页加载完成",
            kind="rule",
            status="proposed",
        ),
    )
    # An approved rule is injectable but is not a procedure card.
    LessonStore(lessons).approve(rule.lesson_id)

    cards = load_procedure_lessons(lessons)
    assert cards == []
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        assert index.sync_procedure_lessons(cards) == {
            "procedures": 0,
            "removed_procedures": 0,
        }
        assert index.count() == 0


def test_run_end_and_dream_reconcile_sync_the_namespace(tmp_path):
    lessons = tmp_path / "lessons"
    memory = tmp_path / "memory"
    card = _food_card(lessons)
    config = SimpleNamespace(
        vec_db=str(tmp_path / "vec.db"),
        memory_dir=str(memory),
        experience_dir=str(memory / "experience"),
        lessons_dir=str(lessons),
        embed_model="hash-v1",
        embed_dim=64,
        index_min_steps=2,
    )

    first = incremental_upsert(config, episode=None, embedder=HashEmbedder(64))
    assert (first["procedures"], first["removed_procedures"]) == (1, 0)

    # Revocation leaves the card out of the view, so the next maintenance pass
    # (run-end upsert or dream reconcile) removes the derived row.
    LessonStore(lessons).revoke(card.lesson_id, "wrong card")
    second = incremental_upsert(config, episode=None, embedder=HashEmbedder(64))
    assert (second["procedures"], second["removed_procedures"]) == (0, 1)

    # A dream reconcile (or any later maintenance pass) picks a new card up.
    _propose(lessons, _payload(text=GENERAL_TITLE, steps=GENERAL_STEPS))
    third = reconcile_index(
        config, embedder=HashEmbedder(64), app_store=AppKnowledgeStore(str(memory))
    )
    assert (third["procedures"], third["removed_procedures"]) == (1, 0)


def test_hard_filter_matrix_app_and_device(tmp_path):
    lessons = tmp_path / "lessons"
    db_path = tmp_path / "vec.db"
    food = _food_card(lessons, device="serial-1")
    general = _propose(
        lessons, _payload(text=GENERAL_TITLE, steps=GENERAL_STEPS, app_scope="general")
    )
    spare = _propose(
        lessons,
        _payload(
            text=SPARE_TITLE,
            steps=["进入结算页", "确认金额", "停手问人"],
            app_scope="general",
            device="serial-2",
        ),
    )

    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        assert index.sync_procedure_lessons(load_procedure_lessons(lessons)) == {
            "procedures": 3,
            "removed_procedures": 0,
        }

        same_app = index.select_procedure(
            FOOD_TITLE, app_package=FOOD, device_id="serial-1"
        )
        assert same_app.reason == "hit"
        assert same_app.lesson_id == food.lesson_id
        assert same_app.app_scope == FOOD
        # Candidates survive the device gate, filtered survives the app gate.
        assert (same_app.candidates, same_app.filtered) == (2, 1)

        other_app = index.select_procedure(
            FOOD_TITLE, app_package=WEATHER, device_id="serial-1"
        )
        assert other_app.reason == "app_scope_mismatch"
        assert other_app.lesson_id is None
        assert (other_app.candidates, other_app.filtered) == (2, 0)

        # No app known yet: only general cards may be injected, and the
        # serial-2 card is not even a candidate on serial-1.
        no_app = index.select_procedure(
            GENERAL_TITLE, app_package=None, device_id="serial-1"
        )
        assert no_app.reason == "hit"
        assert no_app.lesson_id == general.lesson_id
        assert (no_app.candidates, no_app.filtered) == (2, 1)

        prefixed = index.select_procedure(
            SPARE_TITLE, app_package=None, device_id="device:serial-2"
        )
        assert prefixed.lesson_id == spare.lesson_id
        assert (prefixed.candidates, prefixed.filtered) == (2, 2)

        # An unknown device sees device-global cards only.
        unknown_device = index.select_procedure(
            GENERAL_TITLE, app_package=None, device_id=None
        )
        assert unknown_device.lesson_id == general.lesson_id
        assert unknown_device.candidates == 1

        # A device-scoped card is invisible to a different device: the pool on
        # serial-2 holds the two general cards, never the serial-1 food card.
        other_device = index.select_procedure(
            FOOD_TITLE, app_package=FOOD, device_id="serial-2"
        )
        assert other_device.reason == "app_scope_mismatch"
        assert other_device.candidates == 2


def test_top1_ranking_and_threshold_behaviour(tmp_path):
    lessons = tmp_path / "lessons"
    db_path = tmp_path / "vec.db"
    closer = _propose(
        lessons,
        _payload(text=FOOD_TITLE, steps=FOOD_STEPS, app_scope=FOOD),
    )
    farther = _propose(
        lessons,
        _payload(
            text="天气应用查预报",
            steps=["打开天气应用", "输入城市名", "查看预报"],
            app_scope=FOOD,
        ),
    )

    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons))
        best = index.select_procedure(FOOD_TITLE, app_package=FOOD)
        assert best.lesson_id == closer.lesson_id
        assert best.filtered == 2
        assert best.score > PROCEDURE_MIN_SCORE
        assert farther.lesson_id != closer.lesson_id

        # A tighter threshold keeps the same best score but injects nothing.
        tightened = index.select_procedure(
            FOOD_TITLE, app_package=FOOD, min_score=0.99
        )
        assert tightened.lesson_id is None
        assert tightened.reason == "below_threshold"
        assert tightened.score == best.score
        assert tightened.filtered == 2

        # min_score=0 keeps top-1 semantics (still exactly one card).
        permissive = index.select_procedure(
            "查一下天气", app_package=FOOD, min_score=0.0
        )
        assert permissive.reason == "hit"
        assert permissive.lesson_id == farther.lesson_id

        assert index.select_procedure("", app_package=FOOD).reason == "empty_query"


def test_module_level_select_procedure_reads_a_configured_index(tmp_path):
    lessons = tmp_path / "lessons"
    db_path = tmp_path / "vec.db"
    card = _food_card(lessons)
    with VecIndex(db_path, embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons))

    selection = select_procedure(
        FOOD_TITLE,
        app_package=FOOD,
        device_id="serial-1",
        db_path=db_path,
        embedder=HashEmbedder(64),
    )
    assert selection.lesson_id == card.lesson_id
    assert selection.steps == tuple(FOOD_STEPS)


def test_budget_truncates_steps_and_names_the_source(tmp_path):
    selection = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title=FOOD_TITLE,
        steps=tuple(
            f"第{index}步：在页面上找到对应的入口并确认当前步骤已经完成，记录下观察到的结果，"
            f"确认没有出现弹窗或者广告遮挡之后再继续下一步操作"
            for index in range(1, 40)
        ),
        pitfalls="开屏广告点右上角跳过；权限弹窗选仅本次",
        app_scope=FOOD,
        score=0.75,
        candidates=1,
        filtered=1,
        reason="hit",
    )

    block = format_procedure_block(selection)

    assert block is not None
    assert estimate_text_tokens(block) <= PROCEDURE_MAX_TOKENS
    assert "仅供参考，不是规则" in block
    assert selection.lesson_id in block
    assert FOOD_TITLE in block
    rendered_steps = [
        line for line in block.splitlines() if re.fullmatch(r"\d+\. .+", line)
    ]
    assert 0 < len(rendered_steps) < len(selection.steps)
    assert "截断" in block
    # Steps are dropped, never rewritten.
    for line in rendered_steps:
        assert line.split(". ", 1)[1] in selection.steps


def test_small_card_fits_the_budget_whole():
    selection = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title="天气应用查预报",
        steps=("打开天气应用", "输入城市名", "查看预报"),
        pitfalls="权限弹窗选仅本次",
        app_scope="general",
        score=0.8,
        candidates=1,
        filtered=1,
        reason="hit",
    )

    block = format_procedure_block(selection)

    assert block is not None
    assert "1. 打开天气应用" in block
    assert "注意：权限弹窗选仅本次" in block
    assert "截断" not in block
    assert format_procedure_block(ProcedureSelection()) is None


def test_zero_recall_runs_are_recorded_with_their_reason(tmp_path):
    stats_path = tmp_path / "recall_stats.json"
    missed = ProcedureSelection(candidates=0, filtered=0, reason="no_cards")
    filtered_out = ProcedureSelection(candidates=3, filtered=0, reason="app_scope_mismatch")
    hit = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title=GENERAL_TITLE,
        steps=tuple(GENERAL_STEPS),
        app_scope="general",
        score=0.62,
        candidates=3,
        filtered=1,
        reason="hit",
    )

    update_procedure_recall_stats(stats_path, missed, run_id="run-1")
    update_procedure_recall_stats(stats_path, filtered_out, run_id="run-2")
    stats = update_procedure_recall_stats(stats_path, hit, run_id="run-3")

    assert stats["procedure_runs"] == 3
    assert stats["procedure_hits"] == 1
    assert stats["procedure_hit_rate"] == 0.333333
    assert stats["procedure_candidates"] == 6
    assert stats["procedure_filtered"] == 1
    assert stats["procedure_reasons"] == {
        "no_cards": 1,
        "app_scope_mismatch": 1,
        "hit": 1,
    }
    assert stats["latest_procedure"]["run_id"] == "run-3"
    assert stats["latest_procedure"]["reason"] == "hit"

    # The procedure counters share the existing stats file with episode recall.
    merged = update_recall_stats(
        stats_path, {"hit": False, "recalled_apps": []}, run_id="run-3"
    )
    assert merged["evaluations"] == 1
    assert merged["procedure_runs"] == 3


def test_shadow_run_records_procedure_selection(tmp_path, monkeypatch):
    selection = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title=GENERAL_TITLE,
        steps=tuple(GENERAL_STEPS),
        app_scope="general",
        score=0.7,
        candidates=1,
        filtered=1,
        reason="hit",
    )
    calls: list[dict] = []

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
    middleware = TraceMiddleware("wf2a", trace_dir=str(tmp_path / "trace"))
    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.config = SimpleNamespace(
        memory_rag="shadow",
        device_id="serial-1",
        embed_model="hash-v1",
        embed_dim=64,
        vec_db=str(tmp_path / "vec.db"),
        memory_dir=str(tmp_path / "memory"),
        recall_top_k=1,
        recall_min_score=0.50,
        recall_decay_lambda=0.02,
    )
    agent.session = SimpleNamespace(
        observe=lambda: SimpleNamespace(
            screenshot_b64="", current_app="launcher", screen_seq=1, marks=[]
        )
    )
    agent._trace = middleware
    agent._system_prompt = "SYSTEM"
    agent.run_id = "wf2a"

    agent._shadow_recall_start("打开微信")
    agent._shadow_recall_finish()

    assert calls == [{"app_package": None, "device_id": "serial-1"}]
    stats = json.loads(
        (tmp_path / "memory/experience/recall_stats.json").read_text(encoding="utf-8")
    )
    assert stats["procedure_runs"] == 1
    assert stats["procedure_hits"] == 1
    assert stats["latest_procedure"]["lesson_id"] == selection.lesson_id


# --- S3 FIX 1: revoke propagation (delete_procedure) ---------------------


def test_delete_procedure_removes_only_that_lesson_row(tmp_path):
    lessons = tmp_path / "lessons"
    food = _food_card(lessons)
    general = _propose(
        lessons, _payload(text=GENERAL_TITLE, steps=GENERAL_STEPS, app_scope="general")
    )
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons))
        assert index.count() == 2

        assert index.delete_procedure(food.lesson_id) is True
        # Idempotent: a second delete finds no row.
        assert index.delete_procedure(food.lesson_id) is False
        assert index.delete_procedure("les_0000000000000009") is False
        # The remaining card is still selectable.
        selection = index.select_procedure(
            GENERAL_TITLE, app_package=None, min_score=0.0
        )
        assert selection.lesson_id == general.lesson_id


def test_delete_index_procedure_is_fail_open_and_purges(tmp_path):
    from phone_agent.v2.recall import delete_index_procedure

    lessons = tmp_path / "lessons"
    card = _food_card(lessons)
    config = SimpleNamespace(
        vec_db=str(tmp_path / "vec.db"),
        embed_model="hash-v1",
        embed_dim=64,
    )
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons))

    # Unknown id: no row removed, still a successful no-op report.
    report = delete_index_procedure(config, "les_0000000000000009")
    assert report == {"lesson_id": "les_0000000000000009", "removed": False}

    report = delete_index_procedure(config, card.lesson_id)
    assert report == {"lesson_id": card.lesson_id, "removed": True}
    with VecIndex(tmp_path / "vec.db", embedder=HashEmbedder(64)) as index:
        assert index.count() == 0

    # No index configured → None (best-effort, never an error).
    assert delete_index_procedure(SimpleNamespace(vec_db=None), card.lesson_id) is None
    # Unusable index path → None (fail-open), the CLI command must survive it.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert (
        delete_index_procedure(
            SimpleNamespace(vec_db=str(blocker / "v.db"), embed_model="hash-v1",
                            embed_dim=64),
            card.lesson_id,
        )
        is None
    )


# --- S3 FIX 5: the header must respect the token budget ------------------


def test_block_budget_holds_at_boundary_token_values():
    selection = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title=FOOD_TITLE,
        steps=tuple(FOOD_STEPS),
        app_scope=FOOD,
        score=0.9,
        reason="hit",
    )
    # Budget 0: nothing may be rendered.
    assert format_procedure_block(selection, max_tokens=0) is None
    # Budget 1 and 10: even the fixed header cannot fit — no block, never an
    # oversized one.
    assert format_procedure_block(selection, max_tokens=1) is None
    assert format_procedure_block(selection, max_tokens=10) is None
    # Every rendered budget holds the hard guarantee.
    for budget in (25, 40, 60, 120, 300, 1000):
        block = format_procedure_block(selection, max_tokens=budget)
        assert block is not None
        assert estimate_text_tokens(block) <= budget


def test_block_truncates_title_when_header_alone_exceeds_budget():
    long_title = "超长标题" * 500  # 2000 chars
    selection = ProcedureSelection(
        lesson_id="les_0123456789ab",
        title=long_title,
        steps=("打开天气应用",),
        app_scope=FOOD,
        score=0.9,
        reason="hit",
    )

    block = format_procedure_block(selection, max_tokens=300)

    assert block is not None
    assert estimate_text_tokens(block) <= 300
    # The header survived; the title was cut with an ellipsis, never dropped.
    assert "仅供参考，不是规则" in block
    assert "…" in block
    assert long_title not in block
    assert "1. 打开天气应用" not in block  # the header consumed the remainder
