"""Fake-only tests for App-KB CLI maintenance wiring."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import main_v2
from phone_agent.v2.evolution import LessonCandidate, LessonStore


def _config(*, dream_mode: str = "manual"):
    return SimpleNamespace(
        app_kb_enabled=True,
        dream_mode=dream_mode,
        memory_dir="memory",
        device_id="serial-1",
    )


def test_dream_flag_runs_manual_consolidation_without_task(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(
        main_v2.V2Config, "from_env", lambda overrides: _config()
    )
    monkeypatch.setattr(
        main_v2,
        "_run_dream",
        lambda config, *, light, store=None: calls.append((light, store))
        or {"merged": 1},
    )

    assert main_v2.main(["--dream"]) == 0
    assert calls == [(False, None)]
    assert '"merged": 1' in capsys.readouterr().out


def test_run_dream_is_fail_open_without_device(tmp_path, monkeypatch):
    config = _config()
    config.memory_dir = str(tmp_path / "memory")
    monkeypatch.setattr(main_v2, "_device_inventory", lambda config: None)

    assert main_v2._run_dream(config, light=False) == {
        "merged": 0,
        "staled": 0,
        "deleted": 0,
        "kept": 0,
    }


def test_auto_dream_runs_after_unsuccessful_normal_result(monkeypatch):
    calls = []
    fake_agent = SimpleNamespace(
        session=SimpleNamespace(app_store="STORE"),
        run=lambda task: SimpleNamespace(
            success=False, reason="model_stopped", steps=1, trace_path=None
        ),
    )
    module = types.ModuleType("phone_agent.v2.agent")
    module.ThinPhoneAgent = lambda config: fake_agent
    monkeypatch.setitem(sys.modules, "phone_agent.v2.agent", module)
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(
        main_v2.V2Config,
        "from_env",
        lambda overrides: _config(dream_mode="auto"),
    )
    monkeypatch.setattr(
        main_v2,
        "_run_dream",
        lambda config, *, light, store=None: calls.append((light, store)) or {},
    )

    assert main_v2.main(["做任务"]) == 1
    assert calls == [(True, "STORE")]


def _review_config(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        lessons_dir=str(tmp_path / "lessons"),
        experience_dir=str(tmp_path / "experience"),
        experience_enabled=False,
    )


def _proposed_lesson() -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": "les_aaaaaaaaaaaa",
            "schema_v": 1,
            "version": 1,
            "status": "proposed",
            "text": "在示例应用中应该先确认页面稳定再继续",
            "scope": {
                "device": "serial-1",
                "app": "com.example.travel",
                "app_version": None,
            },
            "evidence": [{"run_id": "run-0", "note": "cited by distill"}],
            "support_count": 1,
            "task_keys": ["open_app"],
            "conflicts": [],
            "created_ts": 1.0,
            "source": "distill",
        }
    )


def test_interactive_review_approves_without_supporting_episodes(
    tmp_path, monkeypatch, capsys
):
    config = _review_config(tmp_path)
    candidate = LessonStore(config.lessons_dir).propose(_proposed_lesson())
    monkeypatch.setattr("builtins.input", lambda prompt="": "a")

    assert main_v2._review_lessons(config) == {
        "reviewed": 1,
        "approved": 1,
        "revoked": 0,
    }

    store = LessonStore(config.lessons_dir)
    assert store.get(candidate.lesson_id).status == "approved"
    out = capsys.readouterr().out
    # The Rule-of-3 facts are printed for reference, never to block approval.
    assert "fact_reference" in out
    assert "blocked" not in out


def test_manual_mode_does_not_run_dream_automatically(monkeypatch):
    calls = []
    fake_agent = SimpleNamespace(
        session=SimpleNamespace(app_store="STORE"),
        run=lambda task: SimpleNamespace(
            success=True, reason="done", steps=1, trace_path=None
        ),
    )
    module = types.ModuleType("phone_agent.v2.agent")
    module.ThinPhoneAgent = lambda config: fake_agent
    monkeypatch.setitem(sys.modules, "phone_agent.v2.agent", module)
    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)
    monkeypatch.setattr(
        main_v2.V2Config, "from_env", lambda overrides: _config()
    )
    monkeypatch.setattr(
        main_v2,
        "_run_dream",
        lambda config, *, light, store=None: calls.append((light, store)) or {},
    )

    assert main_v2.main(["做任务"]) == 0
    assert calls == []


def test_revoke_lesson_purges_procedure_row_from_index(tmp_path, monkeypatch, capsys):
    """S3 FIX 1: CLI revoke propagation best-effort deletes the index row."""

    from phone_agent.v2.recall import HashEmbedder, VecIndex, load_procedure_lessons

    monkeypatch.setattr(main_v2, "load_project_env", lambda: None)

    lessons_dir = tmp_path / "lessons"
    card = _procedure_card()
    LessonStore(lessons_dir).propose(card)
    vec_db = tmp_path / "vec.db"
    with VecIndex(vec_db, embedder=HashEmbedder(64)) as index:
        index.sync_procedure_lessons(load_procedure_lessons(lessons_dir))
        assert index.count() == 1

    config = SimpleNamespace(
        lessons_dir=str(lessons_dir),
        experience_dir=str(tmp_path / "experience"),
        experience_enabled=True,
        vec_db=str(vec_db),
        embed_model="hash-v1",
        embed_dim=64,
        memory_rag="on",
    )
    monkeypatch.setattr(main_v2.V2Config, "from_env", lambda overrides: config)

    rc = main_v2.main(["--revoke-lesson", card.lesson_id, "不信任这张卡"])

    assert rc == 0
    out = capsys.readouterr().out
    assert '"removed": true' in out
    store = LessonStore(lessons_dir)
    assert store.get(card.lesson_id).status == "revoked"
    with VecIndex(vec_db, embedder=HashEmbedder(64)) as index:
        assert index.count() == 0
        assert index.select_procedure("外卖下单").lesson_id is None


def _procedure_card() -> LessonCandidate:
    return LessonCandidate.from_dict(
        {
            "lesson_id": "les_bbbbbbbbbbbb0001",
            "schema_v": 1,
            "version": 1,
            "status": "auto_approved",
            "kind": "procedure",
            "text": "外卖应用下单到结算",
            "steps": ["搜索餐厅", "下单", "结算前停手"],
            "pitfalls": None,
            "app_scope": "com.example.food",
            "scope": {"device": None, "app": None, "app_version": None},
            "evidence": [{"run_id": "run-1", "note": "ok"}],
            "support_count": 1,
            "task_keys": ["order"],
            "conflicts": [],
            "created_ts": 1.0,
            "source": "distill",
        }
    )
