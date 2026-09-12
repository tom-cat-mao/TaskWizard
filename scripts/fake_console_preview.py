"""Isolated synthetic preview for the NiceGUI console.

This preview never constructs the production bridge, runner, agent, device,
model client, or memory store. It uses only in-memory state, generated SVG
screens, and a temporary deliverable directory.

The state switcher lives in a small 演示 badge in the command bar, so the
workbench shows exactly the product layout a real run would show.
"""

from __future__ import annotations

import argparse
import base64
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nicegui import ui

from phone_agent.web.app import create_ui, set_deliverable_root

# ------------------------------------------------------------ mock screens
# Synthetic Android-looking frames: clearly demo data (the command bar carries
# a 演示 badge) that never claims to be a real device observation.


def _svg(body: str) -> str:
    xml = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="1280" '
        'viewBox="0 0 720 1280">' + body + "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(xml.encode()).decode()


def _status_bar() -> str:
    return (
        '<rect width="720" height="76" fill="#111827"/>'
        '<text x="40" y="48" fill="#f9fafb" font-family="Arial" font-size="24">9:41</text>'
        '<rect x="596" y="30" width="20" height="16" rx="3" fill="#f9fafb"/>'
        '<rect x="624" y="26" width="24" height="20" rx="3" fill="#f9fafb"/>'
        '<rect x="656" y="22" width="34" height="24" rx="5" fill="#f9fafb"/>'
    )


def _nav_bar(title: str) -> str:
    return (
        '<rect y="76" width="720" height="96" fill="#ffffff"/>'
        '<path d="M56 124 L36 124 M44 112 L32 124 L44 136" stroke="#111827" '
        'stroke-width="4" fill="none" stroke-linecap="round"/>'
        f'<text x="96" y="134" fill="#111827" font-family="Arial" font-size="30" '
        f'font-weight="700">{title}</text>'
        '<rect y="171" width="720" height="2" fill="#e5e7eb"/>'
    )


def _list_row(index: int, label: str, value: str = "", *, active: bool = False) -> str:
    top = 173 + index * 104
    fill = "#eef2ff" if active else "#ffffff"
    text_fill = "#3730a3" if active else "#111827"
    parts = [
        f'<rect x="0" y="{top}" width="720" height="104" fill="{fill}"/>',
        f'<rect x="0" y="{top + 103}" width="720" height="1" fill="#e5e7eb"/>',
        f'<text x="44" y="{top + 62}" fill="{text_fill}" font-family="Arial" '
        f'font-size="27">{label}</text>',
    ]
    if value:
        parts.append(
            f'<text x="676" y="{top + 62}" fill="#6b7280" font-family="Arial" '
            f'font-size="24" text-anchor="end">{value}</text>'
        )
    parts.append(
        f'<path d="M660 {top + 40} L672 {top + 52} L660 {top + 64}" stroke="#9ca3af" '
        'stroke-width="4" fill="none" stroke-linecap="round"/>'
    )
    return "".join(parts)


def _settings_screen(title: str, rows: list[tuple[str, str, bool]]) -> str:
    body = (
        '<rect width="720" height="1280" fill="#f5f6f8"/>'
        + _status_bar()
        + _nav_bar(title)
        + "".join(
            _list_row(i, label, value, active=active)
            for i, (label, value, active) in enumerate(rows)
        )
        + '<rect x="270" y="1240" width="180" height="8" rx="4" fill="#d1d5db"/>'
    )
    return _svg(body)


def _home_screen(connected: bool) -> str:
    apps = ["设置", "WLAN", "浏览器", "相册", "时钟", "音乐"]
    tiles = []
    for index, name in enumerate(apps):
        col, row = index % 3, index // 3
        x, y = 74 + col * 190, 250 + row * 220
        fill = "#4f46e5" if index == 1 else "#94a3b8"
        tiles.append(
            f'<rect x="{x}" y="{y}" width="118" height="118" rx="28" fill="{fill}"/>'
            f'<text x="{x + 59}" y="{y + 158}" fill="#e2e8f0" font-family="Arial" '
            f'font-size="24" text-anchor="middle">{name}</text>'
        )
    badge = (
        '<rect x="60" y="700" width="600" height="96" rx="24" fill="#1f2937"/>'
        '<circle cx="112" cy="748" r="18" fill="#22c55e"/>'
        f'<text x="152" y="758" fill="#e5e7eb" font-family="Arial" font-size="26">'
        f'{"已连接 office 网络" if connected else "未连接 WLAN"}</text>'
    )
    body = (
        '<rect width="720" height="1280" fill="#0f172a"/>'
        + _status_bar()
        + "".join(tiles)
        + badge
        + '<rect x="120" y="1180" width="480" height="76" rx="24" fill="#1e293b"/>'
        + '<rect x="270" y="1240" width="180" height="8" rx="4" fill="#334155"/>'
    )
    return _svg(body)


def _frames(connected: bool) -> list[str]:
    return [
        _home_screen(connected=connected),
        _settings_screen(
            "WLAN",
            [
                ("WLAN", "开", False),
                ("office", "已连接" if connected else "已保存", connected),
                ("guest-network", "", False),
                ("office-5G", "", False),
                ("其他网络…", "", False),
            ],
        ),
    ]


class FakeBridge:
    """Small state source implementing only the public console bridge shape."""

    _STATES: tuple[tuple[str, str], ...] = (
        ("待命", "idle"),
        ("等待模型", "waiting_model"),
        ("执行工具", "tool"),
        ("HITL", "hitl"),
        ("已请求停止", "stopping"),
        ("失败", "error"),
        ("成功", "success"),
        ("参考帧", "reference"),
    )

    def __init__(self, root: Path) -> None:
        now = time.time()
        shots = _frames(connected=False)
        self._state: dict[str, Any] = {
            "run_id": None,
            "task": "",
            "status": "idle",
            "current_screen": shots[1],
            "current_app": "设置",
            "screen_seq": 2,
            "screens": [
                self._frame(1, shots[0], app="桌面"),
                self._frame(2, shots[1], app="设置"),
            ],
            "steps": [],
            "task_board": self._board("idle"),
            "pending_hitl_prompt": None,
            "final_result": None,
            "tokens": 0,
            "usage": {"actor": 0},
            "capabilities": self._capabilities(),
            "error": None,
            "activity": "idle",
            "last_event_ts": now,
            "started_at": None,
            "stop_requested": False,
            "requested_model": None,
            "actual_model": None,
        }
        self._root = root
        self._run_number = 0

    @staticmethod
    def _frame(seq: int, image: str, *, app: str = "设置") -> dict[str, Any]:
        return {
            "seq": seq,
            "app": app,
            "image": image,
            "reference": False,
            "screen_ref": None,
        }

    @staticmethod
    def _capabilities() -> list[dict[str, Any]]:
        return [
            {"cap_id": "safety", "title": "安全", "state": "active"},
            {"cap_id": "memory", "title": "记忆", "state": "shadow"},
            {"cap_id": "deliverable", "title": "产出", "state": "active"},
        ]

    @staticmethod
    def _board(state: str) -> str:
        item = (
            "completed"
            if state == "success"
            else "in_progress"
            if state not in {"idle", "error"}
            else "pending"
        )
        return (
            "[TASK_DOC]\n"
            "## 目标\n"
            "base: 连接 office 网络并返回桌面（演示任务）\n\n"
            "## 路线\n"
            f"- [{item}] 1: 打开设置进入 WLAN（证据：WLAN 列表已显示）\n"
            "- [pending] 2: 连接 office 网络\n"
            "- [pending] 3: 返回桌面并确认连接状态\n\n"
            "## 流程线（最近 2 步）\n"
            "#1 打开设置 → launch_app → ok｜设置已在前台\n"
            "#2 进入 WLAN → tap → 已点击「WLAN」(ax_12)"
        )

    def snapshot(self) -> dict[str, Any]:
        state = dict(self._state)
        state["screens"] = [dict(frame) for frame in self._state["screens"]]
        state["steps"] = [dict(step) for step in self._state["steps"]]
        return state

    def _set_activity(self, activity: str, status: str = "running") -> None:
        now = time.time()
        self._state.update(
            status=status,
            activity=activity,
            last_event_ts=now,
            error=None,
            final_result=None,
            pending_hitl_prompt=None,
            stop_requested=False,
        )
        if status == "idle":
            self._state.update(
                run_id=None,
                task="",
                started_at=None,
                requested_model=None,
                actual_model=None,
            )

    def _step(
        self, status: str, tool: str, intent: str, target: str, result: str
    ) -> dict[str, Any]:
        return {
            "step": 1,
            "intent": intent,
            "tool": tool,
            "target": target,
            "result": result,
            "ok": status == "success",
            "latency_ms": 840,
            "status": status,
            "args": {"intent": intent, "note": "synthetic preview"},
            "model_latency_ms": 480,
            "tool_latency_ms": 360,
            "screen_seq": 2,
        }

    def set_state(self, state: str) -> None:
        if state == "idle":
            shots = _frames(connected=False)
            self._set_activity("idle", "idle")
            self._state["steps"] = []
            self._state["screens"] = [
                self._frame(1, shots[0], app="桌面"),
                self._frame(2, shots[1], app="设置"),
            ]
            self._state["current_screen"] = shots[1]
            self._state["screen_seq"] = 2
            self._state["task_board"] = self._board("idle")
            return
        if self._state["run_id"] is None:
            self.start("演示：连接 office 网络\n进入 WLAN 并返回桌面确认连接状态")
        if state == "waiting_model":
            self._set_activity("waiting_model")
            self._state["steps"] = []
        elif state == "tool":
            self._set_activity("executing_tool")
            self._state["steps"] = [
                self._step("running", "tap", "进入 WLAN 列表", "WLAN", "等待工具返回…")
            ]
        elif state == "hitl":
            self._set_activity("waiting_human", "waiting_hitl")
            self._state["pending_hitl_prompt"] = (
                "检测到「office」是已保存网络，是否允许自动连接？"
                "（合成演示问题，不会连接任何设备）"
            )
            self._state["steps"] = [
                self._step("warning", "tap", "请求人工确认", "连接 office", "等待人工决定")
            ]
        elif state == "stopping":
            self._set_activity("stopping")
            self._state["stop_requested"] = True
            self._state["steps"] = [
                self._step(
                    "running",
                    "tap",
                    "停止当前任务",
                    "当前步骤",
                    "等待当前调用/步骤返回后收尾",
                )
            ]
        elif state == "error":
            self._set_activity("ended", "failed")
            self._state["error"] = "synthetic_error: 仅用于演示失败回执"
            self._state["steps"] = [
                self._step(
                    "error",
                    "read_screen",
                    "读取最新画面",
                    "设置",
                    "error: synthetic_observation_failure",
                )
            ]
            self._state["final_result"] = {
                "success": False,
                "reason": self._state["error"],
                "steps": 1,
                "verifier": "skipped",
            }
        elif state == "success":
            self._set_activity("ended", "succeeded")
            shots = _frames(connected=True)
            self._state["steps"] = [
                self._step("success", "tap", "连接 office 网络", "office", "已点击「office」(ax_18)")
            ]
            self._state["screens"] = [
                self._frame(1, shots[0], app="桌面"),
                self._frame(2, shots[1], app="设置"),
            ]
            self._state["current_screen"] = shots[1]
            self._state["screen_seq"] = 4
            self._state["task_board"] = self._board("success")
            self._state["final_result"] = {
                "success": True,
                "reason": "synthetic_success: 演示完成",
                "steps": 1,
                "verifier": "skipped",
            }
            self._state["usage"] = {"actor": 1240, "verifier": 0}
            self._state["tokens"] = 1240
        elif state == "reference":
            self._set_activity("running")
            self._state["screens"] += [
                {
                    "seq": None,
                    "app": "设置",
                    "image": _settings_screen(
                        "WLAN", [("office", "连接失败", False), ("重试", "", False)]
                    ),
                    "reference": True,
                    "screen_ref": "ref-a",
                },
                {
                    "seq": None,
                    "app": "设置",
                    "image": _settings_screen("WLAN", [("office", "重试中…", False)]),
                    "reference": True,
                    "screen_ref": "ref-b",
                },
            ]
            self._state["current_screen"] = self._state["screens"][-1]["image"]
        self._state["task_board"] = self._board(state)

    def start(self, task: str, overrides: dict[str, Any] | None = None) -> str:
        self._run_number += 1
        run_id = f"demo-run-{self._run_number:02d}"
        self._state.update(
            run_id=run_id,
            task=str(task).strip(),
            status="running",
            activity="waiting_model",
            started_at=time.time(),
            last_event_ts=time.time(),
            requested_model="gateway:primary-demo",
            actual_model=None,
            stop_requested=False,
            final_result=None,
            pending_hitl_prompt=None,
            error=None,
            steps=[],
            tokens=0,
            usage={"actor": 0},
        )
        return run_id

    def request_stop(self) -> bool:
        if self._state["status"] not in {"starting", "running", "waiting_hitl"}:
            return False
        self.set_state("stopping")
        return True

    def submit_hitl(self, answer: str) -> None:
        if not str(answer).strip():
            raise ValueError("回答不能为空")
        if not self._state["pending_hitl_prompt"]:
            raise RuntimeError("当前没有待处理的人工确认")
        self._state["pending_hitl_prompt"] = None
        self._state["status"] = "running"
        self._state["activity"] = "running"
        self._state["last_event_ts"] = time.time()
        self._state["steps"] = [
            self._step(
                "success", "takeover", "处理人工决定", answer, "已提交人工决定（演示）"
            )
        ]

    def kb_entries(self) -> list[dict[str, Any]]:
        return [
            {
                "label": "设置",
                "package": "com.demo.settings",
                "kind": "learned",
                "success_count": 4,
                "stale": False,
            }
        ]

    def memory_snapshot(self) -> dict[str, Any]:
        return {
            "episodes": [
                {
                    "ts_start": time.time() - 3600,
                    "goal_text": "连接 office 网络（演示档案）",
                    "success": True,
                    "reason": "synthetic",
                    "steps": 6,
                    "tokens_total": 1240,
                    "verifier": "skipped",
                }
            ],
            "recall_stats": {
                "evaluations": 5,
                "hits": 4,
                "hit_at_1": 0.8,
                "contaminated_runs": 0,
                "procedure_suppressed": 1,
                "rule_suppressed": 2,
            },
        }

    def run_dream(self) -> dict[str, Any]:
        return {"status": "skipped", "reason": "synthetic_preview"}


def _config(root: Path) -> Any:
    return SimpleNamespace(
        device_id=None,
        model_name="gateway:primary-demo",
        fallback_model="gateway:backup-demo",
        safety_mode="wary",
        lang="cn",
        max_model_calls=100,
        token_budget=1_000_000,
        grounding_provider="hybrid",
        app_kb_enabled=True,
        base_url="https://synthetic.invalid/v1",
        image_keep=2,
        compact_warn_ratio=0.75,
        compact_trigger_ratio=0.92,
        memory_rag="on",
        deliverable_dir=str(root / "deliverables"),
        experience_dir=str(root / "experience"),
        memory_dir=str(root / "memory"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskWizard synthetic console preview")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.root.resolve()
    deliverables = root / "deliverables"
    deliverables.mkdir(parents=True, exist_ok=True)
    (root / "experience").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (deliverables / "demo-run-output.html").write_text(
        "<html><head><title>合成演示产出</title></head><body>"
        "<h1>演示产物</h1><p>仅用于控制台预览，不含真实数据。</p></body></html>",
        encoding="utf-8",
    )
    set_deliverable_root(str(deliverables))
    config = _config(root)
    bridge = FakeBridge(root)

    def demo_badge() -> None:
        """A small command-bar badge; the state switcher hides in its menu."""

        with ui.button().props("flat dense no-caps").classes("tw-badge"):
            ui.label("演示")
            with ui.menu().classes("p-1").style("min-width:148px"):
                ui.label("切换合成状态").classes("tw-label px-3 py-1")
                for label, state in FakeBridge._STATES:
                    ui.menu_item(
                        label, on_click=lambda s=state: bridge.set_state(s)
                    )

    @ui.page("/")
    def index() -> None:
        create_ui(
            bridge,
            config=config,
            refresh_seconds=0.25,
            header_slot=demo_badge,
        )

    ui.run(
        host="127.0.0.1",
        port=args.port,
        title="TaskWizard 合成预览",
        show=False,
        reload=False,
    )


if __name__ == "__main__":
    main()
