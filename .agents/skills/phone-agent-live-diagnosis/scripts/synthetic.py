"""Deterministic offline synthetic run fixtures for the diagnosis pipeline.

The offline smoke never touches a device, a model, or the network. It writes a
complete run directory with the **same file shapes the real runner produces**
(``events.jsonl`` / ``control.jsonl`` / ``run.json`` / ``spec.json``) plus a
diagnostic ``evidence.jsonl`` and one real (tiny) PNG screenshot, then runs the
normal analyze + report path over it.

Because it is synthetic, the report labels it as such and the Case used is
:func:`case.synthetic_case`. It is a pipeline smoke, not evidence about any real
device capability.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from case import synthetic_case

# 1x1 transparent PNG — enough for the report's ``<img>`` path to resolve.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SYNTHETIC_RUN_ID = "synthetic-0001"


def write_synthetic_run(run_dir: str | Path, *, run_id: str = SYNTHETIC_RUN_ID) -> Path:
    """Materialize a synthetic run dir; return its path."""

    base = Path(run_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "screenshots").mkdir(exist_ok=True)
    (base / "traces").mkdir(exist_ok=True)
    _chmod(base / "screenshots", 0o700)
    _chmod(base / "traces", 0o700)

    shot = base / "screenshots" / "screen-1.png"
    shot.write_bytes(_PNG_1X1)
    _chmod_600(shot)

    task = synthetic_case().goal
    spec = {
        "run_id": run_id,
        "task": task,
        "overrides": {
            "model_name": "synthetic-scripted",
            "grounding_provider": "none",
            "safety_mode": "wary",
            "taskdoc_enabled": True,
            "image_keep": 2,
            "max_model_calls": 20,
            "token_budget": 1_000_000,
            "token_warn_remaining": 100_000,
            "finish_verify": "auto",
            "diagnostic_evidence": True,
            "diagnostic_unredacted": True,
            "device_id": None,
        },
        "snapshot": {"config_fingerprint": "synthetic", "capabilities": {}, "ts": 0.0},
        "events_path": str((base / "events.jsonl").resolve()),
        "control_path": str((base / "control.jsonl").resolve()),
    }
    _write_json(base / "spec.json", spec)
    (base / "control.jsonl").write_text("", encoding="utf-8")
    _chmod_600(base / "control.jsonl")

    events = _synthetic_events(run_id, task)
    _write_jsonl(base / "events.jsonl", events)
    _chmod_600(base / "events.jsonl")

    evidence = _synthetic_evidence(run_id, task)
    _write_jsonl(base / f"{run_id}.evidence.jsonl", evidence)
    _write_jsonl(base / "evidence.jsonl", evidence)
    for name in (f"{run_id}.evidence.jsonl", "evidence.jsonl"):
        _chmod_600(base / name)

    _write_jsonl(
        base / "traces" / f"{run_id}.jsonl",
        [
            {
                "event": "model_fallback",
                "stage": "invoke",
                "role": "actor",
                "requested": "synthetic-primary",
                "actual": "synthetic-backup",
                "reason": "SyntheticPrimaryError",
                "outcome": "ok",
            }
        ],
    )
    _chmod_600(base / "traces" / f"{run_id}.jsonl")

    _write_json(
        base / "run.json",
        {
            "run_id": run_id,
            "task": task,
            "status": "succeeded",
            "result": {
                "success": True,
                "reason": "已确认完成",
                "steps": 4,
                "trace_path": str(base / "traces" / f"{run_id}.jsonl"),
            },
            "usage": {"actor": 1234},
            "snapshot": spec["snapshot"],
            "finished_at": time.time(),
        },
    )
    return base


def _synthetic_events(run_id: str, task: str) -> list[dict[str, Any]]:
    return [
        {
            "event": "capability_snapshot",
            "capabilities": {"safety": {"cap_id": "safety", "mode": "wary", "state": "active"}},
        },
        {"event": "model_request", "step": 1, "phase": "start", "requested_model": "synthetic-primary"},
        {
            "event": "model_call",
            "step": 1,
            "latency_ms": 12,
            "tokens": 300,
            "tokens_total": 300,
            "requested_model": "synthetic-primary",
            "actual_model": "synthetic-backup",
            "input_tokens": 250,
            "output_tokens": 50,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        },
        {"event": "tool_call", "step": 1, "tool": "read_screen", "args": {"intent": "观测设置页"}},
        {
            "event": "tool_result",
            "step": 1,
            "tool": "read_screen",
            "text": "[OBS] app=com.android.settings screen#1 marks (1): ax_1@e1 | button | 设置 | (100,200)",
            "ok": True,
            "latency_ms": 40,
        },
        {"event": "screen", "step": 1, "image": "[omitted-base64]", "current_app": "com.android.settings", "screen_seq": 1, "reference": False},
        {
            "event": "taskdoc_snapshot",
            "step": 2,
            "text": "[TASK_DOC] 目标：打开设置\n- [ip] 打开设置页 (in_progress)",
        },
        {"event": "model_request", "step": 2, "phase": "start", "requested_model": "synthetic-primary"},
        {
            "event": "model_call",
            "step": 2,
            "latency_ms": 10,
            "tokens": 200,
            "tokens_total": 500,
            "requested_model": "synthetic-primary",
            "actual_model": "synthetic-backup",
            "input_tokens": 160,
            "output_tokens": 40,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        },
        {"event": "tool_call", "step": 2, "tool": "update_task_doc", "args": {"items": [{"id": "s1", "content": "打开设置页", "status": "completed", "evidence_note": "screen#1 设置页可见"}]}},
        {"event": "tool_result", "step": 2, "tool": "update_task_doc", "text": "已更新任务板。", "ok": True, "latency_ms": 3},
        {"event": "pending_hitl", "step": 3, "prompt": "是否确认继续？"},
        {"event": "pending_hitl", "step": 3, "prompt": None},
        {"event": "model_request", "step": 3, "phase": "start", "requested_model": "synthetic-primary"},
        {
            "event": "model_call",
            "step": 3,
            "latency_ms": 11,
            "tokens": 180,
            "tokens_total": 680,
            "requested_model": "synthetic-primary",
            "actual_model": "synthetic-backup",
            "input_tokens": 150,
            "output_tokens": 30,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        },
        {"event": "tool_call", "step": 3, "tool": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"]}},
        {"event": "tool_result", "step": 3, "tool": "finish", "text": "[FINISH 复核包] 已完成前请先核对下列世界事实；确认无误后再调用 finish(confirm=true, ...) 定稿。", "ok": True, "latency_ms": 20},
        {"event": "model_request", "step": 4, "phase": "start", "requested_model": "synthetic-primary"},
        {
            "event": "model_call",
            "step": 4,
            "latency_ms": 9,
            "tokens": 120,
            "tokens_total": 800,
            "requested_model": "synthetic-primary",
            "actual_model": "synthetic-backup",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        },
        {"event": "tool_call", "step": 4, "tool": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"], "confirm": True}},
        {"event": "tool_result", "step": 4, "tool": "finish", "text": "已确认完成", "ok": True, "latency_ms": 18},
        {"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "已确认完成", "steps": 4}, "tokens_total": 800},
    ]


def _synthetic_evidence(run_id: str, task: str) -> list[dict[str, Any]]:
    return [
        {
            "event": "run_start",
            "run_id": run_id,
            "task_goal_base": task,
            "config_digest": {
                "model_name": "synthetic-scripted",
                "grounding_provider": "none",
                "max_model_calls": 20,
                "lang": "cn",
                "device_id": None,
                "taskdoc_enabled": True,
                "unredacted": True,
            },
        },
        {
            "event": "model_request",
            "step": 1,
            "message_count": 2,
            "image_message_count": 1,
            "pruned_screen_count": 0,
            "taskdoc_present": True,
            "taskdoc_open_items": 0,
            "context_chars": 120,
        },
        {
            "event": "model_response",
            "step": 1,
            "thinking": "我先观测屏幕。",
            "tool_calls": [{"name": "read_screen", "args": {"intent": "观测设置页"}}],
            "usage": {"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
        },
        {"event": "tool_invoke", "step": 1, "tool": "read_screen", "args": {"intent": "观测设置页"}},
        {
            "event": "tool_observation",
            "step": 1,
            "tool": "read_screen",
            "latency_ms": 40,
            "result_text": "[OBS] app=com.android.settings screen#1\nmarks (1): ax_1@e1 | button | 设置 | (100,200)",
            "obs": {"current_app": "com.android.settings", "screen_seq": 1, "mark_count": 1},
            "image": {"present": True, "screen_seq": 1, "bytes": 68, "path": "screenshots/screen-1.png"},
            "error": None,
        },
        {
            "event": "taskdoc_snapshot",
            "step": 2,
            "goal_base": task,
            "amendments": [],
            "items": [
                {"id": "s1", "content": "打开设置页", "status": "completed", "reason": None, "evidence_note": "screen#1 设置页可见"}
            ],
            "facts": ["前台包 com.android.settings"],
            "open_item_count": 0,
        },
        {
            "event": "model_response",
            "step": 2,
            "thinking": "记录设置页证据，标记路线完成。",
            "tool_calls": [
                {
                    "name": "update_task_doc",
                    "args": {
                        "intent": "记录证据",
                        "items": [{"id": "s1", "content": "打开设置页", "status": "completed", "evidence_note": "screen#1 设置页可见"}],
                    },
                }
            ],
            "usage": {"input_tokens": 160, "output_tokens": 40, "total_tokens": 200},
        },
        {
            "event": "tool_invoke",
            "step": 2,
            "tool": "update_task_doc",
            "args": {"intent": "记录证据", "items": [{"id": "s1", "status": "completed"}]},
        },
        {
            "event": "tool_observation",
            "step": 2,
            "tool": "update_task_doc",
            "latency_ms": 3,
            "result_text": "已更新任务板。",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
        {
            "event": "model_response",
            "step": 3,
            "thinking": "准备确认完成。",
            "tool_calls": [{"name": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"]}}],
            "usage": {"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
        },
        {"event": "tool_invoke", "step": 3, "tool": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"]}},
        {
            "event": "tool_observation",
            "step": 3,
            "tool": "finish",
            "latency_ms": 20,
            "result_text": "[FINISH 复核包] 已完成前请先核对下列世界事实；确认无误后再调用 finish(confirm=true, ...) 定稿。",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
        {
            "event": "model_response",
            "step": 4,
            "thinking": "确认。",
            "tool_calls": [{"name": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"], "confirm": True}}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        },
        {"event": "tool_invoke", "step": 4, "tool": "finish", "args": {"summary": "已打开设置", "evidence": ["屏幕显示设置页"], "confirm": True}},
        {
            "event": "tool_observation",
            "step": 4,
            "tool": "finish",
            "latency_ms": 18,
            "result_text": "已确认完成",
            "obs": None,
            "image": {"present": False, "screen_seq": None, "bytes": 0},
            "error": None,
        },
        {"event": "run_end", "steps": 4, "terminal": {"finished": True, "takeover_reason": None, "finish_summary": "已打开设置"}},
    ]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _chmod_600(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _chmod_600(path: Path) -> None:
    _chmod(path, 0o600)


__all__ = ["write_synthetic_run", "SYNTHETIC_RUN_ID"]
