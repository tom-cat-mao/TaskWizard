"""NiceGUI application for live thin-loop watch and HITL steering (v2).

Layout: one fixed command bar (brand, truthful run status, settings) followed by
a viewport-height two-column workbench — a 336px device rail whose screenshot is
contained to the remaining height, and a wide stage column holding a compact
multi-line task composer, one line of run metadata, and the secondary tabs
(steps / task board / app library / memory / outputs). The narrow breakpoint
falls back to a natural single-column flow.

Truthfulness rules kept from the console contract: an absent provider model
label reads 未上报 (never the requested label), a requested stop reads 已请求停止
until a terminal event lands, and a screenshot that carries no committed
``screen_seq`` is labeled an unverified reference frame instead of a fresh
observation. The bridge stays the sole owner of runner state; this module only
renders the public snapshot and forwards control actions.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from fastapi.responses import FileResponse
from nicegui import app, ui

from phone_agent.v2.config import V2Config, load_project_env
from phone_agent.web.bridge import WebRunBridge

# ---------------------------------------------------------------- design tokens
# Deep navy surfaces with a single violet accent. Hierarchy comes from spacing,
# type scale and hairlines — not from stacking bordered grey boxes.

_ACCENT = "#7c6cf7"
_ACCENT_SOFT = "rgba(124,108,247,.13)"
_BG = "#070a13"
_PANEL = "#0c1220"
_PANEL_SOFT = "#111a2c"
_LINE = "rgba(148,163,184,.10)"
_LINE_2 = "rgba(148,163,184,.20)"
_TEXT = "#e6ebf5"
_MUTED = "#8d9ab2"
_DIM = "rgba(148,163,184,.55)"
_OK = "#34d399"
_WARN = "#fbbf24"
_BAD = "#f87171"

# ------------------------------------------------------- deliverables (WP-DOC)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DELIVERABLE_ROOT = Path("outputs/deliverables")


def set_deliverable_root(path: str) -> None:
    global _DELIVERABLE_ROOT
    _DELIVERABLE_ROOT = Path(path)


def _deliverable_path(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise ValueError("invalid run_id")
    root = _DELIVERABLE_ROOT.resolve()
    target = (root / f"{run_id}.html").resolve()
    if target.parent != root:
        raise ValueError("path escapes deliverable root")
    return target


def _deliverable_title(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return ""
    match = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:80] if match else ""


@app.get("/api/deliverables")
def _api_deliverables() -> list[dict[str, Any]]:
    root = _DELIVERABLE_ROOT
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*.html"), key=lambda p: -p.stat().st_mtime):
        run_id = path.stem
        if not _RUN_ID_RE.match(run_id):
            continue
        stat = path.stat()
        items.append(
            {
                "run_id": run_id,
                "title": _deliverable_title(path),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    return items


@app.get("/deliverables/{run_id}")
def _serve_deliverable(run_id: str) -> FileResponse:
    try:
        target = _deliverable_path(run_id)
    except ValueError as exc:
        raise FileNotFoundError(run_id) from exc
    if not target.is_file():
        raise FileNotFoundError(run_id)
    return FileResponse(target, media_type="text/html")


@app.post("/api/deliverables/{run_id}/delete")
def _delete_deliverable(run_id: str) -> dict[str, bool]:
    try:
        target = _deliverable_path(run_id)
    except ValueError:
        return {"deleted": False}
    existed = target.is_file()
    if existed:
        target.unlink()
    return {"deleted": existed}


_CSS = f"""
:root {{ color-scheme: dark; }}
html, body {{ height: 100%; }}
body {{ background: {_BG}; color: {_TEXT}; margin: 0; font-size: 14px;
  font-family: -apple-system, "SF Pro Text", "PingFang SC", "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; }}
.mono {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
body::before {{ content: ''; position: fixed; left: 0; right: 0; top: 0; height: 260px;
  pointer-events: none; z-index: 0;
  background: radial-gradient(720px 200px at 24% -80px, {_ACCENT}29, transparent 72%); }}

/* ---------------------------------------------------------- command bar */
.tw-bar {{ height: 52px; background: rgba(8,11,20,.90); backdrop-filter: blur(14px);
  border-bottom: 1px solid {_LINE}; }}
.tw-mark {{ width: 25px; height: 25px; border-radius: 8px;
  background: linear-gradient(135deg, #8b5cf6, #4f46e5);
  box-shadow: 0 5px 16px -7px rgba(139,92,246,.95); }}
.tw-title {{ font-size: 15px; font-weight: 700; line-height: 17px; letter-spacing: .01em; }}
.tw-sub {{ font-size: 10.5px; line-height: 13px; color: {_MUTED}; letter-spacing: .05em; }}
.tw-pill {{ display: inline-flex; align-items: center; gap: 7px; padding: 4px 11px;
  border-radius: 999px; font-size: 12.5px; font-weight: 600;
  border: 1px solid {_LINE}; background: {_PANEL_SOFT}; }}
.tw-dot {{ width: 7px; height: 7px; border-radius: 50%; }}
.tw-dot.live {{ animation: tw-pulse 1.5s ease-in-out infinite; }}
@keyframes tw-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .32; }} }}
.tw-hint {{ font-size: 11.5px; color: {_MUTED}; }}
.tw-badge {{ font-size: 10px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; color: {_WARN}; background: rgba(251,191,36,.10);
  border: 1px solid rgba(251,191,36,.30); border-radius: 6px; padding: 2px 6px; }}
.tw-gear .q-icon {{ font-size: 19px; color: {_MUTED}; }}
.tw-gear:hover .q-icon {{ color: {_TEXT}; }}

/* ---------------------------------------------------------------- shell */
.tw-shell {{ max-width: 1440px; margin: 0 auto; padding: 12px; display: flex;
  flex-direction: column; gap: 12px; position: relative; z-index: 1; }}
@media (min-width: 1081px) {{
  body {{ overflow: hidden; }}
  .tw-shell {{ position: fixed; left: 0; right: 0; top: 52px; bottom: 0;
    padding: 14px 18px; flex-direction: row; gap: 14px; }}
}}

/* ----------------------------------------------------------- device rail */
.tw-rail {{ width: 336px; flex: 0 0 336px; min-height: 0; display: flex;
  flex-direction: column; gap: 10px; padding: 12px; border-radius: 16px;
  background: {_PANEL}; border: 1px solid {_LINE}; }}
.tw-rail-head {{ display: flex; align-items: center; gap: 8px; }}
.tw-screen {{ flex: 1 1 auto; min-height: 0; height: 58vh; display: flex;
  align-items: center; justify-content: center; padding: 8px; border-radius: 13px;
  background: #05070e; border: 1px solid {_LINE}; overflow: hidden; }}
.tw-screen img {{ display: block; max-width: 100%; max-height: 100%;
  width: auto; height: auto; object-fit: contain; border-radius: 9px; }}
.tw-screen.is-ref {{ border-color: rgba(251,191,36,.42); }}
.tw-film {{ display: flex; gap: 6px; overflow-x: auto; overflow-y: hidden;
  padding-bottom: 2px; scrollbar-width: thin; flex: 0 0 auto; }}
.tw-thumb {{ width: 42px; height: 58px; border-radius: 6px; cursor: pointer;
  opacity: .5; border: 2px solid transparent; transition: opacity .15s, border-color .15s;
  flex: 0 0 auto; }}
.tw-thumb:hover {{ opacity: .85; }}
.tw-thumb.sel {{ border-color: {_ACCENT}; opacity: 1; }}
.tw-thumb.is-ref {{ border-style: dashed; border-color: rgba(251,191,36,.55); }}
.tw-thumb.is-ref.sel {{ border-color: {_WARN}; }}

/* ---------------------------------------------------------------- stage */
.tw-stage {{ flex: 1 1 auto; min-width: 0; min-height: 0; display: flex;
  flex-direction: column; gap: 10px; }}
.tw-board {{ flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;
  gap: 8px; padding: 10px 12px 8px; border-radius: 16px;
  background: {_PANEL}; border: 1px solid {_LINE}; }}

/* composer */
.tw-composer {{ display: flex; flex-direction: column; gap: 6px; }}
.tw-field .q-field__control {{ background: {_PANEL_SOFT}; border-radius: 10px;
  border: 1px solid {_LINE}; }}
.tw-field .q-field__control:hover {{ border-color: {_LINE_2}; }}
.tw-field.q-field--focused .q-field__control {{ border-color: {_ACCENT};
  box-shadow: 0 0 0 3px {_ACCENT_SOFT}; }}
.tw-field .q-field__control::before, .tw-field .q-field__control::after {{ display: none; }}
.tw-field .q-field__native, .tw-field .q-field__input {{ color: {_TEXT};
  font-size: 13.5px; line-height: 1.45; }}
.tw-field textarea {{ padding-top: 9px !important; padding-bottom: 9px !important; }}
.tw-run {{ width: 92px; height: 31px; border-radius: 9px; font-weight: 700;
  background: {_ACCENT}; color: #fff;
  box-shadow: 0 6px 18px -10px rgba(124,108,247,1); }}
.tw-stop {{ width: 92px; height: 31px; border-radius: 9px; color: {_BAD};
  border: 1px solid rgba(248,113,113,.35); }}
.tw-meta-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px;
  padding: 0 2px; font-size: 11.5px; }}
.tw-sep {{ color: {_LINE_2}; }}
.tw-meta-k {{ color: {_DIM}; }}
.tw-meta-v {{ color: {_MUTED}; }}

/* hitl */
.tw-hitl {{ padding: 12px; border-radius: 14px;
  background: linear-gradient(180deg, rgba(251,191,36,.10), rgba(251,191,36,.04));
  border: 1px solid rgba(251,191,36,.34); }}
.tw-hitl-title {{ font-size: 14px; font-weight: 700; color: {_WARN}; }}
.tw-hitl-body {{ font-size: 13px; white-space: pre-wrap; margin-top: 6px; }}

/* toolbar + panels */
.tw-toolbar {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  min-height: 26px; }}
.tw-panels {{ flex: 1 1 auto; min-height: 0; }}
.tw-panels .q-tab-panel {{ padding: 0; height: 100%; overflow: hidden; }}
.tw-fill {{ height: 100%; display: flex; flex-direction: column; gap: 8px; min-height: 0; }}
.tw-scroll {{ flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden;
  padding-right: 2px; }}
@media (max-width: 1080px) {{
  .tw-rail {{ width: 100%; flex: 0 0 auto; }}
  .tw-screen {{ height: 62vh; }}
  .tw-panels .q-tab-panel {{ height: auto; overflow: visible; }}
  .tw-fill, .tw-scroll {{ height: auto; overflow: visible; }}
}}
@media (max-width: 900px) {{
  .tw-hide-sm {{ display: none !important; }}
}}

/* tabs */
.tw-tabs .q-tab {{ text-transform: none; font-weight: 600; font-size: 13px;
  color: {_MUTED}; padding: 4px 10px; min-height: 34px; }}
.tw-tabs .q-tab--active {{ color: {_TEXT}; }}
.tw-tabs .q-tab__indicator {{ background: {_ACCENT}; height: 2px; }}
.q-tab-panels {{ background: transparent; }}
.q-tab-panels .q-tab-panel {{ padding: 0; }}

/* type helpers */
.tw-label {{ font-size: 11.5px; font-weight: 700; letter-spacing: .09em;
  text-transform: uppercase; color: {_MUTED}; }}
.tw-note {{ font-size: 11.5px; color: {_DIM}; line-height: 1.5; }}
.tw-chip {{ display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px;
  border-radius: 6px; font-size: 11px; font-weight: 600; white-space: nowrap;
  background: {_ACCENT_SOFT}; color: {_ACCENT}; border: 1px solid {_ACCENT}38; }}
.tw-chip.grey {{ background: rgba(148,163,184,.08); color: {_MUTED};
  border-color: rgba(148,163,184,.18); }}
.tw-chip.ok {{ color: {_OK}; border-color: {_OK}55; background: {_OK}18; }}
.tw-chip.warn {{ color: {_WARN}; border-color: {_WARN}55; background: {_WARN}18; }}
.tw-chip.bad {{ color: {_BAD}; border-color: {_BAD}55; background: {_BAD}18; }}
.tw-pre {{ font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
  color: {_MUTED}; white-space: pre-wrap; word-break: break-word; }}
.tw-result {{ font-size: 13px; white-space: pre-wrap; word-break: break-word;
  line-height: 1.55; }}
.tw-dim {{ color: {_DIM}; }}
.tw-link {{ color: {_ACCENT}; font-size: 11.5px; }}
.tw-latbar {{ height: 3px; border-radius: 2px; background: rgba(148,163,184,.14);
  overflow: hidden; }}
.tw-latbar > div {{ height: 100%; border-radius: 2px; }}

/* steps */
.tw-steps {{ display: flex; flex-direction: column; }}
.tw-current {{ display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  border-radius: 10px; background: {_ACCENT_SOFT}; border: 1px solid {_ACCENT}33; }}
.tw-current-icon {{ font-size: 16px; color: {_ACCENT}; }}
.tw-current-text {{ font-size: 13.5px; font-weight: 600; flex: 0 1 auto; }}
.tw-stream {{ display: flex; flex-direction: column; gap: 6px; padding: 8px 10px;
  border-radius: 10px; border: 1px solid {_LINE_2};
  background: rgba(148,163,184,.05); }}
.tw-stream-text {{ font-size: 13px; white-space: pre-wrap; word-break: break-word;
  line-height: 1.55; max-height: 168px; overflow-y: auto; }}
.tw-stream-think {{ font-size: 12px; white-space: pre-wrap; word-break: break-word;
  color: {_MUTED}; border-left: 2px solid {_LINE_2}; padding-left: 8px;
  max-height: 96px; overflow-y: auto; }}
.tw-step {{ border-bottom: 1px solid {_LINE}; }}
.tw-step:last-child {{ border-bottom: none; }}
.tw-step .q-expansion-item__container > .q-item {{ padding: 0; min-height: 0; }}
.tw-step .q-item__section--side {{ display: none; }}
.tw-step .q-expansion-item__content {{ padding: 0; }}
.tw-step .q-item {{ background: transparent; }}
.tw-step .q-focus-helper {{ display: none; }}
.tw-step .q-item__label {{ padding: 0; }}
.tw-step-body {{ display: flex; flex-direction: column; gap: 6px;
  padding: 4px 10px 12px; }}
.tw-step-num {{ font-family: ui-monospace, Menlo, monospace; font-size: 11px;
  color: {_DIM}; min-width: 24px; }}
.tw-step-title {{ font-size: 13.5px; font-weight: 600; }}
.tw-step-lat {{ font-family: ui-monospace, Menlo, monospace; font-size: 11px;
  color: {_DIM}; }}
.tw-step[data-st="running"] {{ background: {_ACCENT_SOFT}; box-shadow: inset 2px 0 0 {_ACCENT}; }}
.tw-step[data-st="error"] {{ box-shadow: inset 2px 0 0 {_BAD}; }}
.tw-step[data-st="warning"] {{ box-shadow: inset 2px 0 0 {_WARN}; }}
.tw-step:hover {{ background: rgba(148,163,184,.04); }}

/* task board */
.tw-board-goal {{ padding: 10px 12px; border-radius: 11px; background: {_PANEL_SOFT};
  border: 1px solid {_LINE}; }}
.tw-board-item {{ display: flex; gap: 9px; padding: 6px 2px; align-items: flex-start;
  border-bottom: 1px solid {_LINE}; }}
.tw-board-flow {{ font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
  color: {_MUTED}; padding: 3px 0; border-bottom: 1px dashed {_LINE};
  white-space: pre-wrap; word-break: break-all; }}

/* outputs */
.tw-out {{ display: flex; align-items: center; gap: 10px; padding: 9px 10px;
  border-bottom: 1px solid {_LINE}; }}
.tw-out:hover {{ background: rgba(148,163,184,.04); }}

/* stats + empty states */
.tw-stats {{ display: flex; align-items: stretch; gap: 14px; flex-wrap: wrap; }}
.tw-stat {{ display: flex; flex-direction: column; gap: 1px; padding-right: 14px;
  border-right: 1px solid {_LINE}; }}
.tw-stat:last-child {{ border-right: none; }}
.tw-stat-num {{ font-size: 17px; font-weight: 700;
  font-family: ui-monospace, Menlo, monospace; line-height: 20px; }}
.tw-stat-cap {{ font-size: 11px; color: {_MUTED}; }}
.tw-empty {{ display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 6px; padding: 44px 16px; text-align: center; }}
.tw-empty .q-icon {{ font-size: 30px; color: rgba(148,163,184,.42); }}
.tw-empty-title {{ font-size: 13.5px; font-weight: 600; color: {_MUTED}; }}
.tw-empty-hint {{ font-size: 12px; color: {_DIM}; max-width: 400px;
  line-height: 1.55; }}

/* tables + drawer */
.q-table__container, .q-table, .q-table__card {{ background: transparent;
  box-shadow: none; border: none; }}
.q-table th {{ font-size: 10.5px; letter-spacing: .07em; text-transform: uppercase;
  color: {_MUTED}; border-bottom: 1px solid {_LINE}; background: transparent; }}
.q-table td {{ border-bottom: 1px solid {_LINE}; font-size: 12.5px;
  background: transparent; }}
.q-table tbody tr:hover {{ background: rgba(148,163,184,.05); }}
.q-drawer {{ background: {_PANEL}; border-left: 1px solid {_LINE}; }}
.q-drawer .q-field .q-field__control {{ background: rgba(148,163,184,.06);
  border-radius: 9px; }}
.q-dialog .q-card {{ background: {_PANEL}; border: 1px solid {_LINE_2}; }}
"""

# Terminal / non-active status → pill label + color.
_STATUS_META = {
    "idle": ("待命", _MUTED, False),
    "succeeded": ("已完成", _OK, False),
    "failed": ("未完成", _BAD, False),
    "takeover": ("已接管/停止", _WARN, False),
    "budget_exhausted": ("预算耗尽", _BAD, False),
    "loop_fuse": ("保险丝触发", _BAD, False),
    "error": ("运行错误", _BAD, False),
}

# Active-run activity → pill label + color + a short "what we're waiting on" hint.
_ACTIVITY_META = {
    "starting": ("启动中", _ACCENT, True, "正在拉起运行进程"),
    "running": ("运行中", _ACCENT, True, "模型已返回，正在处理"),
    "waiting_model": ("等待模型调用返回", _ACCENT, True, "网络 / 队列 / 推理，无法区分"),
    "executing_tool": ("执行工具", _ACCENT, True, "工具正在设备上执行"),
    "waiting_human": ("等待人工", _WARN, True, "需要你做出决定"),
    "stopping": ("已请求停止", _WARN, True, "等待当前调用/步骤返回后收尾"),
}

_STEP_META = {
    "running": ("执行中", _ACCENT),
    "success": ("成功", _OK),
    "warning": ("预警", _WARN),
    "error": ("失败", _BAD),
}

_STREAM_TAIL_CHARS = 2400
_STREAM_STATUS = {
    "streaming": ("接收中", ""),
    "done": ("完成", "ok"),
    "failed": ("中断", "bad"),
}


def _stream_selection(
    attempts: list[dict[str, Any]], *, attempt: int | None, follow: bool
) -> tuple[dict[str, Any] | None, int | None]:
    """Pick the attempt to display: newest while following, else the pinned one.

    Following is the default; a pinned attempt survives later appends and only
    falls back to the newest when it leaves the retained window.
    """

    if not attempts:
        return None, attempt
    numbers = [int(item["attempt"]) for item in attempts]
    if follow or attempt not in numbers:
        attempt = numbers[-1]
    record = next(
        (item for item in attempts if item["attempt"] == attempt), attempts[-1]
    )
    return record, attempt


def _stream_tail(text: str, limit: int = _STREAM_TAIL_CHARS) -> str:
    """Tail view for the live panel; a leading marker keeps the cut visible."""

    if len(text) <= limit:
        return text
    return "…" + text[-limit:]

_TOOL_ICON = {
    "tap": "touch_app",
    "long_press": "touch_app",
    "type_text": "keyboard",
    "launch_app": "rocket_launch",
    "locate": "my_location",
    "swipe": "swipe",
    "scroll": "unfold_more",
    "wait": "hourglass_empty",
    "finish": "flag",
    "update_task_doc": "edit_note",
    "ask_user": "help_outline",
    "take_over": "pan_tool",
    "read_screen": "visibility",
    "press_key": "smart_button",
    "home": "home",
    "back": "arrow_back",
}

_USAGE_ROLE_TEXT = {
    "actor": "主模型",
    "compact": "压缩",
    "verifier": "验收器",
    "reviewer": "安全复核",
    "distill": "蒸馏",
}

_VERIFIER_TEXT = {"pass": "通过", "fail": "未通过", "skipped": "跳过"}

_RAG_MODE_TEXT = {
    "on": "on：注入已批准课程（非强制参考）",
    "shadow": "shadow：只观测、不注入",
    "off": "off：关闭",
}

_BOARD_ITEM_RE = re.compile(r"^- \[(?P<status>\w+)\] (?P<ident>\S+): (?P<rest>.*)$")
_BOARD_NOTE_RE = re.compile(r"（(?:证据|原因)：(?P<note>.*)）$|\((?:evidence|reason): (?P<note_en>.*)\)$")
_BOARD_SECTIONS = {
    "goal": ("目标", "Goal"),
    "items": ("路线", "Plan"),
    "flow": ("流程线", "Flow"),
}


def _parse_board(text: str) -> dict[str, Any]:
    """Parse the pinned TaskDoc block into structured sections for the 任务板 tab.

    Input format (see ``taskdoc.render`` + ``TaskDocMiddleware._flow_block``):
    ``## 目标/Goal`` → ``base: …`` + ``- amendment``; ``## 路线/Plan`` →
    ``- [status] id: content（证据/原因：…）``; ``## 流程线/Flow…`` → ``#N …`` lines.
    Unknown shapes land in ``raw`` so nothing is ever lost.
    """

    out: dict[str, Any] = {"goal": "", "amendments": [], "items": [], "flow": [], "raw": ""}
    if not text or not text.strip():
        return out
    lines = [ln.rstrip() for ln in str(text).splitlines()]
    section: str | None = None
    in_amendments = False
    for ln in lines:
        stripped = ln.strip()
        if not stripped or stripped == "[TASK_DOC]":
            continue
        header = re.match(r"^##\s*(.+)$", stripped)
        if header:
            title = header.group(1)
            section = None
            for key, names in _BOARD_SECTIONS.items():
                if any(title.startswith(name) for name in names):
                    section = key
                    break
            in_amendments = False
            if section is None:
                out["raw"] += ln + "\n"
            continue
        if section == "goal":
            if stripped.startswith(("base:", "Base:")):
                out["goal"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith(("补充", "Amendments")):
                in_amendments = True
            elif in_amendments and stripped.startswith("- "):
                out["amendments"].append(stripped[2:])
            else:
                out["raw"] += ln + "\n"
        elif section == "items":
            match = _BOARD_ITEM_RE.match(stripped)
            if match:
                rest = match.group("rest")
                note = ""
                note_match = _BOARD_NOTE_RE.search(rest)
                if note_match:
                    note = note_match.group("note") or note_match.group("note_en") or ""
                    rest = rest[: note_match.start()].rstrip()
                out["items"].append(
                    {
                        "status": match.group("status"),
                        "id": match.group("ident"),
                        "content": rest,
                        "note": note,
                    }
                )
            else:
                out["raw"] += ln + "\n"
        elif section == "flow":
            if stripped.startswith("#"):
                out["flow"].append(stripped)
            else:
                out["raw"] += ln + "\n"
        else:
            out["raw"] += ln + "\n"
    out["raw"] = out["raw"].strip()
    return out


_KIND_TEXT = {"device": "设备", "alias": "别名", "learned": "学习", "user": "用户"}

_CAP_STATE_STYLE = {
    "active": (_OK, "生效"),
    "shadow": (_ACCENT, "影子"),
    "off": (_MUTED, "关闭"),
    "pending": (_WARN, "待岗"),
}


def _display(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text or fallback


def _frame_key(frame: dict) -> str:
    """Stable identity for a frame.

    A verified observation is keyed by its committed ``screen_seq``; an
    unverified reference frame (``seq is None``) is keyed by its own
    ``screen_ref`` so multiple reference frames never collapse onto one another
    or onto a real observation.
    """

    if frame.get("reference"):
        ref = frame.get("screen_ref")
        return f"ref:{ref if ref is not None else id(frame)}"
    return f"seq:{frame.get('seq')}"


def _choose_frame(screens: list[dict], selected: dict) -> dict | None:
    """Main-frame choice: follow the newest frame unless the user pinned one."""

    latest = screens[-1] if screens else None
    if not selected.get("pinned"):
        selected["key"] = _frame_key(latest) if latest else None
        return latest
    keys = {_frame_key(s) for s in screens}
    if latest is not None and selected.get("key") not in keys:
        selected["pinned"] = False
        selected["key"] = _frame_key(latest)
        return latest
    return next(
        (s for s in screens if _frame_key(s) == selected.get("key")), latest
    )


def _pin_toggle(selected: dict, key: Any) -> None:
    """Click a thumbnail/step: pin it; click the pinned one again: follow latest."""

    if selected.get("pinned") and selected.get("key") == key:
        selected.update(key=None, pinned=False)
    else:
        selected.update(key=key, pinned=True)


def _mask_url(url: str) -> str:
    text = str(url or "")
    if len(text) <= 28:
        return text
    return text[:18] + "…" + text[-8:]


def _tokens_fmt(n: int | float) -> str:
    n = int(n)
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{n:,}"


def _duration_fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{sec:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}"


def _age_fmt(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 1:
        return "刚刚"
    if seconds < 60:
        return f"{seconds}s 前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}min 前"
    return f"{minutes // 60}h 前"


def _empty(icon: str, title: str, hint: str = "") -> ui.element:
    """Product-grade empty state: a short guide, never a fabricated result."""

    with ui.element("div").classes("tw-empty") as box:
        ui.icon(icon)
        ui.label(title).classes("tw-empty-title")
        if hint:
            ui.label(hint).classes("tw-empty-hint")
    return box


def _stat_inline(label: str, accent: str = _ACCENT) -> ui.label:
    with ui.element("div").classes("tw-stat"):
        value = ui.label("—").classes("tw-stat-num").style(f"color:{accent}")
        ui.label(label).classes("tw-stat-cap")
    return value


def _device_override(value: Any) -> str:
    """Return the explicit UI device serial; blank or whitespace means auto."""

    return str(value or "").strip()


def _streaming_hint(config: Any, *, switch_on: bool) -> str:
    """Effective actor streaming decision for the drawer (best-effort, read-only).

    A models.json model entry or ``roles.actor.streaming`` outweighs the
    drawer's global switch, so the panel must state what will actually run
    instead of echoing the switch.
    """

    try:
        from dataclasses import replace

        from phone_agent.v2.providers import (
            build_provider_registry,
            get_role_specs,
            resolve_role_ref,
            resolve_streaming_mode,
        )

        active = replace(config, streaming="on" if switch_on else "off")
        registry = build_provider_registry(active)
        enabled = resolve_streaming_mode(active, "actor", registry) == "on"
        role_mode = getattr(get_role_specs(registry).get("actor"), "streaming", None)
        model_mode = None
        try:
            resolved = registry.resolve(resolve_role_ref(active, "actor", registry=registry))
            model_mode = getattr(resolved.model, "streaming", None)
        except Exception:  # noqa: BLE001 - unresolved refs add no tier
            model_mode = None
        if role_mode:
            source = "roles.actor.streaming"
        elif model_mode:
            source = "models.json 模型条目"
        elif switch_on:
            source = "抽屉/全局开关"
        else:
            source = "默认"
        text = f"流式生效：{'on' if enabled else 'off'}（{source}）"
        if enabled != switch_on:
            text += " · 模型/角色声明覆盖了抽屉开关"
        return text
    except Exception:  # noqa: BLE001 - the hint must never break the drawer
        return f"流式生效：{'on' if switch_on else 'off'}（未能读取 models.json）"


class _ConfigPanel:
    """Right-drawer config: per-run overrides (never written back to .env)."""

    def __init__(self, config: V2Config) -> None:
        self._config = config
        with ui.drawer("right", bordered=True, value=False).classes(
            "p-4 gap-3 w-80"
        ) as drawer:
            ui.label("运行配置").classes("text-base font-bold")
            ui.label("仅对下一次运行生效，不写回 .env").classes("text-[11.5px]").style(
                f"color:{_MUTED}; margin-top:-8px"
            )
            self.device_id = ui.input(
                "设备 serial（留空=自动）", value=config.device_id or ""
            ).props("outlined dense dark").classes("w-full")
            self.model_name = ui.input("主模型", value=config.model_name).props(
                "outlined dense dark"
            ).classes("w-full")
            # A-package fallback field: read compatibly. When the core config
            # has no fallback_model yet, default to empty and never guess a
            # substitute for the user.
            self.fallback_model = ui.input(
                "备用模型（留空=不设）",
                value=str(getattr(config, "fallback_model", "") or ""),
            ).props("outlined dense dark").classes("w-full")
            self.safety_mode = ui.select(
                ["wary", "off", "hard", "reviewer"],
                value=getattr(config, "safety_mode", "wary"),
                label="安全模式",
            ).props("outlined dense dark").classes("w-full")
            self.lang = ui.select(
                ["cn", "en"], value=getattr(config, "lang", "cn"), label="语言"
            ).props("outlined dense dark").classes("w-full")
            self.max_steps = ui.number(
                "最大步数（保险丝）", value=getattr(config, "max_model_calls", 100), min=1
            ).props("outlined dense dark").classes("w-full")
            self.token_budget = ui.number(
                "Token 预算", value=getattr(config, "token_budget", 1_000_000), min=1000
            ).props("outlined dense dark").classes("w-full")
            self.grounding_provider = ui.select(
                ["hybrid", "accessibility", "locateanything"],
                value=getattr(config, "grounding_provider", "hybrid"),
                label="Grounding",
            ).props("outlined dense dark").classes("w-full")
            self.app_kb = ui.switch(
                "App-KB 记忆", value=bool(getattr(config, "app_kb_enabled", True))
            ).props("dark")
            self.streaming = ui.switch(
                "模型流式增量", value=getattr(config, "streaming", "off") == "on"
            ).props("dark")
            self.streaming_hint = ui.label("").classes("text-[11.5px]").style(
                f"color:{_MUTED}; margin-top:-6px"
            )
            self.streaming.on_value_change(lambda _e: self._refresh_streaming())
            self.model_name.on_value_change(lambda _e: self._refresh_streaming())
            self._refresh_streaming()
            ui.separator()
            ui.label("当前生效（只读）").classes("tw-label")
            ui.label(f"网关 {_mask_url(getattr(config, 'base_url', ''))}").classes(
                "text-xs mono"
            ).style(f"color:{_MUTED}")
            ui.label(
                f"图片保留 {getattr(config, 'image_keep', 2)} 张 · "
                f"compact {getattr(config, 'compact_warn_ratio', 0.75)}/"
                f"{getattr(config, 'compact_trigger_ratio', 0.92)}"
            ).classes("text-xs").style(f"color:{_MUTED}")
            ui.button("恢复默认", icon="restart_alt", on_click=self.reset).props(
                "flat dense no-caps"
            )
        self.drawer = drawer

    def _refresh_streaming(self) -> None:
        """Show the effective streaming decision, not just the switch value."""

        from dataclasses import replace

        active = replace(
            self._config,
            model_name=str(self.model_name.value or self._config.model_name).strip(),
        )
        self.streaming_hint.set_text(
            _streaming_hint(active, switch_on=bool(self.streaming.value))
        )

    def reset(self) -> None:
        cfg = self._config
        self.device_id.value = cfg.device_id or ""
        self.model_name.value = cfg.model_name
        self.fallback_model.value = str(getattr(cfg, "fallback_model", "") or "")
        self.safety_mode.value = getattr(cfg, "safety_mode", "wary")
        self.lang.value = getattr(cfg, "lang", "cn")
        self.max_steps.value = getattr(cfg, "max_model_calls", 100)
        self.token_budget.value = getattr(cfg, "token_budget", 1_000_000)
        self.grounding_provider.value = getattr(cfg, "grounding_provider", "hybrid")
        self.app_kb.value = bool(getattr(cfg, "app_kb_enabled", True))
        self.streaming.value = getattr(cfg, "streaming", "off") == "on"
        self._refresh_streaming()
        ui.notify("已恢复为当前 .env 生效值", type="positive")

    def overrides(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "device_id": _device_override(self.device_id.value),
            "model_name": str(self.model_name.value or "") or None,
            "safety_mode": self.safety_mode.value,
            "lang": self.lang.value,
            "max_model_calls": int(self.max_steps.value or 100),
            "token_budget": int(self.token_budget.value or 1_000_000),
            "grounding_provider": self.grounding_provider.value,
            "app_kb_enabled": bool(self.app_kb.value),
        }
        if hasattr(self._config, "streaming"):
            result["streaming"] = "on" if self.streaming.value else "off"
        # Only send fallback_model when the core config actually accepts it, so
        # a base checkout without the A-package field is not handed an unknown
        # override (V2Config.from_env rejects unknown names).
        if hasattr(self._config, "fallback_model"):
            result["fallback_model"] = str(self.fallback_model.value or "") or None
        return result


# ------------------------------------------------------------------ main UI


def create_ui(
    bridge: WebRunBridge,
    *,
    config: V2Config,
    refresh_seconds: float = 0.5,
    header_slot: Callable[[], None] | None = None,
) -> None:
    """Build the single-page UI and attach it to ``bridge``.

    ``header_slot`` renders extra trailing content inside the command bar; the
    synthetic preview uses it for its small demo badge, so debug affordances
    never occupy the workbench.
    """

    ui.colors(primary=_ACCENT, positive=_OK, negative=_BAD, warning=_WARN)
    ui.dark_mode().enable()
    ui.add_css(_CSS)

    panel = _ConfigPanel(config)
    set_deliverable_root(config.deliverable_dir)

    # --- command bar ------------------------------------------------------
    with ui.header().classes("tw-bar items-center gap-3 px-4"):
        ui.element("div").classes("tw-mark")
        with ui.column().classes("gap-0"):
            ui.label("TaskWizard").classes("tw-title")
            ui.label("thin-loop 实时控制台").classes("tw-sub")
        ui.element("div").classes("w-px h-6").style(f"background:{_LINE_2}")
        with ui.element("div").classes("tw-pill") as status_pill:
            status_dot = ui.element("span").classes("tw-dot").style(f"background:{_MUTED}")
            status_text = ui.label("待命").classes("text-[12.5px]")
        activity_hint = ui.label("").classes("tw-hint tw-hide-sm")
        ui.space()
        ui.button(icon="tune", on_click=panel.drawer.toggle).props(
            "flat round dense"
        ).classes("tw-gear")
        if header_slot is not None:
            header_slot()

    # --- workbench --------------------------------------------------------
    with ui.element("div").classes("tw-shell"):

        # device rail
        with ui.element("section").classes("tw-rail"):
            with ui.element("div").classes("tw-rail-head"):
                ui.label("设备").classes("tw-label")
                pin_chip = ui.label("跟随最新").classes("tw-chip grey")
                ui.space()
                screen_meta = ui.label("—").classes("tw-note mono truncate")
                back_live = ui.button(
                    icon="my_location", on_click=lambda: _release_pin()
                ).props("flat dense round size=sm").classes("tw-link")
            reference_tag = ui.element("div").classes("w-full")
            screen_box = ui.element("div").classes("tw-screen")
            with screen_box:
                screen_image = ui.element("img")
            screen_box.set_visibility(False)
            with ui.element("div").classes("tw-screen") as no_screen:
                _empty(
                    "smartphone",
                    "运行后显示设备实时画面",
                    "截图按窗口等比缩放；历史帧进入下方缩略条，点击可钉住对比。",
                )
            thumbs = ui.element("div").classes("tw-film")

        # stage
        with ui.element("section").classes("tw-stage"):

            # HITL first: the decision belongs at the top of the workspace.
            with ui.element("div").classes("tw-hitl") as hitl_panel:
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("front_hand").style(f"color:{_WARN}; font-size:18px")
                    ui.label("需要你决定").classes("tw-hitl-title")
                hitl_prompt = ui.label().classes("tw-hitl-body")
                with ui.row().classes("w-full gap-2 mt-3 items-center no-wrap"):
                    hitl_answer = (
                        ui.input(placeholder="也可以输入文本回答")
                        .props("outlined dense dark")
                        .classes("grow tw-field")
                    )
                    approve_button = ui.button("同意", icon="check").props(
                        "unelevated dense no-caps"
                    ).classes("tw-run").style(
                        "width:auto; padding:0 12px; background:#12a06a"
                    )
                    reject_button = ui.button("拒绝", icon="close").props(
                        "flat dense no-caps"
                    ).classes("tw-stop").style("width:auto; padding:0 12px")
                    answer_button = ui.button("提交", icon="send").props(
                        "outline dense no-caps"
                    ).classes("tw-stop").style(
                        f"width:auto; padding:0 12px; color:{_TEXT};"
                        f" border-color:{_LINE_2}"
                    )
            hitl_panel.set_visibility(False)

            # composer: multi-line task + run/stop + one line of run metadata
            with ui.element("div").classes("tw-composer"):
                with ui.row().classes("w-full items-stretch gap-2 no-wrap"):
                    task_input = (
                        ui.textarea(
                            placeholder="描述手机任务，可多行 — 例如：打开设置进入 WLAN，"
                            "连接名为 office 的网络后返回桌面"
                        )
                        .props("outlined dense autogrow rows=2 input-style=max-height:92px")
                        .classes("grow tw-field")
                    )
                    with ui.column().classes("gap-2 shrink-0 justify-between"):
                        start_button = ui.button("运行", icon="play_arrow").props(
                            "unelevated dense no-caps"
                        ).classes("tw-run")
                        stop_button = ui.button("停止", icon="stop").props(
                            "outline dense no-caps"
                        ).classes("tw-stop")
                with ui.element("div").classes("tw-meta-row"):
                    ui.label("run").classes("tw-meta-k")
                    run_id_label = ui.label("—").classes("tw-meta-v mono")
                    ui.label("·").classes("tw-sep")
                    ui.label("首选模型").classes("tw-meta-k")
                    req_model_label = ui.label("—").classes("tw-meta-v mono")
                    ui.label("·").classes("tw-sep")
                    ui.label("实际模型").classes("tw-meta-k")
                    act_model_label = ui.label("未上报").classes("tw-meta-v mono")
                    ui.label("·").classes("tw-sep")
                    tokens_label = ui.label("—").classes("tw-meta-v mono")
                    ui.label("·").classes("tw-sep")
                    elapsed_label = ui.label("—").classes("tw-meta-v mono")
                    ui.label("·").classes("tw-sep")
                    age_label = ui.label("—").classes("tw-meta-v mono")
                    ui.space()
                    ui.label("Ctrl / ⌘ + Enter 运行，回车换行").classes("tw-meta-k")

            # secondary tabs board
            with ui.element("section").classes("tw-board"):
                with ui.tabs().classes("w-full tw-tabs") as tabs:
                    tab_steps = ui.tab("steps", label="步骤")
                    tab_board = ui.tab("board", label="任务板")
                    tab_kb = ui.tab("appkb", label="应用库")
                    tab_memory = ui.tab("memory", label="记忆")
                    tab_outputs = ui.tab("outputs", label="产出")
                with ui.tab_panels(tabs, value=tab_steps).classes("w-full tw-panels"):

                    with ui.tab_panel(tab_steps).classes("tw-fill"):
                        with ui.element("div").classes("tw-toolbar"):
                            step_count = ui.label("0 步").classes("tw-label")
                            usage_total = ui.label("").classes("tw-note mono")
                            ui.space()
                            usage_bars = ui.element("div").classes(
                                "flex items-center gap-2 flex-wrap justify-end"
                            )
                        with ui.element("div").classes("tw-stream") as stream_panel:
                            with ui.row().classes(
                                "items-center gap-2 no-wrap w-full"
                            ):
                                ui.icon("graphic_eq").style(
                                    f"color:{_ACCENT}; font-size:15px"
                                )
                                ui.label("模型流式输出").classes("tw-label")
                                stream_chip = ui.label("").classes("tw-chip")
                                stream_meta = ui.label("").classes(
                                    "tw-note mono truncate"
                                )
                                ui.space()
                                stream_picks = ui.element("div").classes(
                                    "flex items-center gap-1"
                                )
                            stream_think_label = ui.label("推理（SDK 上报）").classes(
                                "tw-label"
                            )
                            stream_think_label.set_visibility(False)
                            stream_think = ui.label("").classes("tw-stream-think")
                            stream_think.set_visibility(False)
                            stream_text = ui.label("").classes("tw-stream-text")
                            stream_tail = ui.label("").classes("tw-note")
                        stream_panel.set_visibility(False)
                        with ui.element("div").classes("tw-current") as current_step:
                            current_icon = ui.icon("bolt").classes("tw-current-icon")
                            current_text = ui.label("—").classes(
                                "tw-current-text truncate"
                            )
                            current_meta = ui.label("").classes("tw-note mono truncate")
                            current_state = ui.label("").classes("tw-chip")
                        current_step.set_visibility(False)
                        with ui.element("div").classes("tw-scroll"):
                            empty_steps = _empty(
                                "route",
                                "还没有执行步骤",
                                "运行任务后，每一步的意图、工具与真实结果会实时出现在这里；"
                                "点开任一步可看参数、回执和耗时拆分。",
                            )
                            timeline = ui.column().classes("tw-steps")

                    with ui.tab_panel(tab_board).classes("tw-fill"):
                        board_box = ui.column().classes("tw-scroll w-full gap-3")

                    with ui.tab_panel(tab_kb).classes("tw-fill"):
                        with ui.element("div").classes("tw-toolbar"):
                            kb_count = ui.label("0 条").classes("tw-label")
                            ui.space()
                            dream_button = ui.button(
                                "立即整理", icon="auto_fix_high"
                            ).props("outline dense no-caps").classes("tw-stop").style(
                                f"width:auto; padding:0 10px; color:{_MUTED};"
                                f" border-color:{_LINE_2}"
                            )
                        kb_hint = ui.label(
                            "agent 成功启动过的应用会沉淀在这里；整理只做合并与失效标记。"
                        ).classes("tw-note")
                        with ui.element("div").classes("tw-scroll"):
                            kb_table = ui.table(
                                columns=[
                                    {"name": "label", "label": "名称", "field": "label"},
                                    {"name": "package", "label": "包名", "field": "package"},
                                    {"name": "kind", "label": "类型", "field": "kind"},
                                    {
                                        "name": "success_count",
                                        "label": "成功",
                                        "field": "success_count",
                                    },
                                    {"name": "stale", "label": "状态", "field": "stale"},
                                ],
                                rows=[],
                                row_key="package",
                            ).props("flat dense hide-bottom no-data-label=暂无条目").classes("w-full")

                    with ui.tab_panel(tab_memory).classes("tw-fill"):
                        ui.label("能力状态").classes("tw-label")
                        caps_row = ui.element("div").classes(
                            "w-full flex gap-2 flex-wrap mb-1"
                        )
                        with ui.element("div").classes("tw-stats"):
                            stat_eps = _stat_inline("任务档案", _ACCENT)
                            stat_evals = _stat_inline("回想评估", "#818cf8")
                            stat_hit = _stat_inline("Hit@1", _OK)
                            stat_false = _stat_inline("污染率", _WARN)
                            stat_card_sup = _stat_inline("卡片抑制", _WARN)
                            stat_rule_sup = _stat_inline("规则抑制", _WARN)
                        rag_mode_label = ui.label("").classes("tw-note")
                        with ui.element("div").classes("tw-scroll"):
                            memory_table = ui.table(
                                columns=[
                                    {"name": "time", "label": "时间", "field": "time"},
                                    {"name": "goal", "label": "任务", "field": "goal"},
                                    {
                                        "name": "outcome",
                                        "label": "结果",
                                        "field": "outcome",
                                    },
                                    {"name": "steps", "label": "步数", "field": "steps"},
                                    {"name": "tokens", "label": "Token", "field": "tokens"},
                                    {
                                        "name": "verifier",
                                        "label": "验收",
                                        "field": "verifier",
                                    },
                                ],
                                rows=[],
                                row_key="time",
                            ).props("flat dense hide-bottom no-data-label=暂无档案").classes("w-full")

                    with ui.tab_panel(tab_outputs).classes("tw-fill"):
                        with ui.element("div").classes("tw-toolbar"):
                            outputs_count = ui.label("0 份").classes("tw-label")
                            ui.space()
                            ui.label("agent 可写可改；删除只有你能做").classes("tw-note")
                        with ui.element("div").classes("tw-scroll"):
                            outputs_list = ui.column().classes("w-full gap-0")
                        with ui.dialog() as preview_dialog, ui.card().classes(
                            "w-[86vw] max-w-5xl p-4"
                        ):
                            with ui.row().classes(
                                "w-full items-center justify-between mb-2"
                            ):
                                preview_title = ui.label("").classes("text-sm font-bold")
                                ui.button(
                                    icon="close", on_click=preview_dialog.close
                                ).props("flat round dense")
                            preview_frame = ui.element("iframe").classes(
                                "w-full rounded-lg"
                            ).props("sandbox").style(
                                f"height:72vh; border:1px solid {_LINE}; background:#fff"
                            )
                        with ui.dialog() as delete_dialog, ui.card().classes("p-4"):
                            delete_hint = ui.label("").classes("text-sm mb-3")
                            with ui.row().classes("gap-2 justify-end w-full"):
                                ui.button("取消", on_click=delete_dialog.close).props(
                                    "flat no-caps dense"
                                )
                                delete_confirm = ui.button("删除").props(
                                    "unelevated no-caps dense color=negative"
                                )

    # --- mutable view state ----------------------------------------------
    ui_state: dict[str, Any] = {"sig": None}
    selected: dict[str, Any] = {"key": None, "pinned": False}
    last_run_id: dict[str, Any] = {"id": None}
    clock_state: dict[str, Any] = {
        "started_at": None,
        "last_event_ts": None,
        "active": False,
    }
    step_rows: dict[int, dict[str, Any]] = {}
    step_choice: dict[int, bool] = {}
    stream_view: dict[str, Any] = {
        "attempt": None,
        "follow": True,
        "sig": None,
        "shown": False,
    }

    # --- actions ---------------------------------------------------------
    def submit_hitl(answer: str) -> None:
        try:
            bridge.submit_hitl(answer)
            hitl_answer.value = ""
            ui.notify("已提交人工决定", type="positive")
        except (ValueError, RuntimeError) as exc:
            ui.notify(str(exc), type="warning")

    approve_button.on("click", lambda: submit_hitl("approve"))
    reject_button.on("click", lambda: submit_hitl("reject"))
    answer_button.on("click", lambda: submit_hitl(str(hitl_answer.value or "")))
    hitl_answer.on(
        "keydown.enter", lambda: submit_hitl(str(hitl_answer.value or ""))
    )

    def start_run() -> None:
        try:
            bridge.start(str(task_input.value or ""), overrides=panel.overrides())
            ui.notify("任务已启动", type="positive")
        except (ValueError, RuntimeError) as exc:
            ui.notify(str(exc), type="warning")

    start_button.on("click", start_run)
    # Ctrl/Cmd+Enter runs; a bare Enter keeps inserting newlines (multi-line safe).
    task_input.on("keydown.enter.ctrl", start_run)
    task_input.on("keydown.enter.meta", start_run)

    def stop_run() -> None:
        if bridge.request_stop():
            ui.notify("已请求停止（等待当前调用/步骤返回后收尾）", type="warning")
        else:
            ui.notify("当前没有可停止的运行", type="warning")

    stop_button.on("click", stop_run)

    def run_dream() -> None:
        summary = bridge.run_dream()
        ui.notify(f"dream 整理：{summary}", type="info", multi_line=True)

    dream_button.on("click", run_dream)

    def pin_frame(key: str) -> None:
        selected.update(key=key, pinned=True)
        render()

    def toggle_pin(key: str) -> None:
        """Thumbnail click: pin the frame, or release it back to latest."""

        _pin_toggle(selected, key)
        render()

    def _release_pin() -> None:
        selected.update(key=None, pinned=False)
        render()

    # --- renderers -------------------------------------------------------
    def _reset_steps() -> None:
        step_rows.clear()
        step_choice.clear()
        timeline.clear()
        stream_view.update(attempt=None, follow=True, sig=None, shown=False)
        stream_panel.set_visibility(False)
        stream_text.set_text("")
        stream_think.set_text("")
        stream_think.set_visibility(False)
        stream_think_label.set_visibility(False)
        stream_tail.set_text("")
        stream_picks.clear()

    def _render_step_body(body: ui.element, step: dict, is_closing: bool, color: str) -> None:
        body.clear()
        with body:
            badge_text = "收尾" if is_closing else _STEP_META.get(step["status"], ("",))[0]
            with ui.row().classes("items-center gap-2 flex-wrap"):
                if badge_text:
                    cls = "tw-chip"
                    if step["status"] == "error":
                        cls = "tw-chip bad"
                    elif step["status"] == "warning":
                        cls = "tw-chip warn"
                    elif step["status"] == "success":
                        cls = "tw-chip ok"
                    ui.label(badge_text).classes(cls)
                if step.get("screen_seq") is not None:
                    ui.button(
                        "查看该步画面",
                        icon="image",
                        on_click=lambda _e, s=step["screen_seq"]: pin_frame(f"seq:{s}"),
                    ).props("flat dense no-caps size=sm").classes("tw-link")
            if step.get("args"):
                ui.label("参数").classes("tw-label")
                ui.label(str(step["args"])).classes("tw-pre")
            if step.get("result"):
                ui.label("结果").classes("tw-label")
                ui.label(str(step["result"])).classes("tw-result")
            elif step["status"] == "running":
                ui.label("等待工具返回…").classes("tw-result tw-dim")
            else:
                ui.label("没有结果回执").classes("tw-result tw-dim")
            model_lat = int(step.get("model_latency_ms", 0) or 0)
            tool_lat = int(step.get("tool_latency_ms", 0) or 0)
            total = model_lat + tool_lat
            if total:
                with ui.element("div").classes("tw-latbar"):
                    ui.element("div").style(
                        f"width:{model_lat / total * 100:.0f}%; background:{_ACCENT}"
                    )
                ui.label(
                    f"模型调用 {model_lat}ms · 工具 {tool_lat}ms（端到端，含网络）"
                ).classes("tw-note mono")

    def _sync_step_row(step: dict) -> None:
        """Create or update one step row in place (keeps scroll and expansion)."""

        number = int(step["step"])
        is_closing = not step.get("tool") and not step.get("result")
        status_key = "success" if is_closing else step["status"]
        color = _STEP_META.get(status_key, ("", _MUTED))[1]
        icon = "check_circle" if is_closing else _TOOL_ICON.get(step.get("tool", ""), "bolt")
        title = "模型收尾" if is_closing else _display(step["intent"], "（未声明意图）")
        lat = int(step.get("model_latency_ms", 0) or 0) + int(
            step.get("tool_latency_ms", 0) or 0
        )
        sig = (
            status_key,
            title,
            step.get("tool", ""),
            step.get("target", ""),
            step.get("result", ""),
            step.get("args"),
            lat,
            step.get("screen_seq"),
        )

        refs = step_rows.get(number)
        if refs is None:
            with timeline:
                row = ui.element("div").classes("tw-step").props(
                    f'data-st="{status_key}"'
                )
                with row:
                    ex = ui.expansion(
                        value=step_choice.get(number, status_key == "running")
                    )
                    ex.classes("w-full")
                    ex.on_value_change(
                        lambda e, n=number: step_choice.__setitem__(n, bool(e.value))
                    )
                    with ex.add_slot("header"):
                        with ui.row().classes(
                            "w-full items-center gap-2 no-wrap py-2 px-3"
                        ):
                            icon_label = ui.icon(icon).style(
                                f"color:{color}; font-size:17px"
                            )
                            ui.label(f"#{number}").classes("tw-step-num")
                            title_label = ui.label(title).classes(
                                "tw-step-title truncate grow"
                            )
                            target_label = ui.label("").classes("tw-chip grey mono")
                            tool_label = ui.label("").classes("tw-chip mono")
                            lat_label = ui.label("").classes("tw-step-lat")
                    body = ui.element("div").classes("tw-step-body")
            refs = {
                "row": row,
                "ex": ex,
                "icon": icon_label,
                "title": title_label,
                "target": target_label,
                "tool": tool_label,
                "lat": lat_label,
                "body": body,
                "sig": None,
            }
            step_rows[number] = refs

        if refs["sig"] == sig:
            return
        refs["sig"] = sig
        refs["row"].props(f'data-st="{status_key}"')
        refs["icon"].set_name(icon)
        refs["icon"].style(f"color:{color}; font-size:17px")
        refs["title"].set_text(title)
        tool = str(step.get("tool") or "")
        refs["tool"].set_text(tool or "—")
        refs["tool"].set_visibility(bool(tool))
        target = str(step.get("target") or "")
        refs["target"].set_text(target[:28] + ("…" if len(target) > 28 else ""))
        refs["target"].set_visibility(bool(target))
        refs["lat"].set_text(f"{lat / 1000:.1f}s" if lat else "")
        _render_step_body(refs["body"], step, is_closing, color)
        if status_key == "running":
            refs["ex"].value = step_choice.get(number, True)

    def _render_current_step(steps: list[dict[str, Any]]) -> None:
        """Keep the in-flight step visible above the (scrolling) history."""

        running = next(
            (
                step
                for step in reversed(steps)
                if step["status"] == "running"
                and (step.get("tool") or step.get("result"))
            ),
            None,
        )
        if running is None:
            current_step.set_visibility(False)
            return
        tool = str(running.get("tool") or "")
        target = str(running.get("target") or "")
        current_icon.set_name(_TOOL_ICON.get(tool, "bolt"))
        current_text.set_text(
            f"#{running['step']} " + _display(running["intent"], "（当前步骤未声明意图）")
        )
        current_meta.set_text(" · ".join(part for part in (tool, target[:30]) if part))
        current_state.set_text("执行中")
        current_step.set_visibility(True)

    def _pick_stream_attempt(number: int) -> None:
        """Pin one streamed attempt for the live panel (keeps history)."""

        stream_view.update(attempt=int(number), follow=False, sig=None)
        render()

    def _follow_stream_latest() -> None:
        """Resume following the newest streamed attempt."""

        stream_view.update(follow=True, sig=None)
        render()

    def _render_stream(attempts: list[dict[str, Any]]) -> None:
        """Lightweight incremental model-text view (per attempt, observe-only).

        Follows the newest attempt by default; clicking a history chip pins it
        until "回到最新".  Each attempt keeps its own text: a failed partial
        answer is labeled ``中断`` and never merged with the attempt that
        replaced it. Only provider-reported reasoning is shown.
        """

        if not attempts:
            if stream_view["sig"] is not None or stream_view["shown"]:
                stream_view.update(sig=None, attempt=None, shown=False)
                stream_panel.set_visibility(False)
            return
        numbers = [int(item["attempt"]) for item in attempts]
        record, stream_view["attempt"] = _stream_selection(
            attempts, attempt=stream_view["attempt"], follow=stream_view["follow"]
        )
        if record is None:
            stream_panel.set_visibility(False)
            return
        signature = (
            tuple(
                (
                    item["attempt"],
                    item["status"],
                    item.get("revision"),
                    item.get("chars_total"),
                    item.get("reasoning_total"),
                    item.get("text_truncated"),
                    item.get("reasoning_truncated"),
                    item["error"],
                )
                for item in attempts
            ),
            stream_view["attempt"],
            stream_view["follow"],
        )
        if stream_view["sig"] == signature:
            return
        stream_view["sig"] = signature
        stream_view["shown"] = True
        stream_panel.set_visibility(True)

        label, chip_cls = _STREAM_STATUS.get(record["status"], ("—", "grey"))
        stream_chip.set_text(label)
        stream_chip.classes(f"tw-chip {chip_cls}", remove="grey ok bad")
        text = str(record.get("text") or "")
        reasoning = str(record.get("reasoning") or "")
        chars_total = int(record.get("chars_total") or len(text))
        step_part = f"第 {record['step']} 步 · " if record.get("step") else ""
        stream_meta.set_text(f"尝试 {record['attempt']} · {step_part}{chars_total} 字")
        stream_picks.clear()
        with stream_picks:
            if not stream_view["follow"]:
                ui.button(
                    "回到最新",
                    icon="south",
                    on_click=lambda _e: _follow_stream_latest(),
                ).props("flat dense no-caps size=sm").classes("tw-link")
            for number in numbers:
                picked = number == record["attempt"]
                ui.button(
                    f"#{number}",
                    on_click=lambda _e, n=number: _pick_stream_attempt(n),
                ).props("flat dense no-caps size=sm").classes(
                    "tw-label" if picked else "tw-link"
                )
        if reasoning:
            stream_think_label.set_visibility(True)
            stream_think.set_visibility(True)
            stream_think.set_text(_stream_tail(reasoning))
        else:
            stream_think_label.set_visibility(False)
            stream_think.set_visibility(False)
        stream_text.set_text(_stream_tail(text) or "（本次尝试没有可展示的正文）")
        notes: list[str] = []
        if record["status"] == "streaming":
            notes.append("接收中，完成后仍会保留完整消息与工具调用")
        if record["status"] == "failed":
            notes.append(
                f"该次尝试未完成（{_display(record.get('error'), '未上报')}）；"
                "以上是已收到的部分文本，后续尝试单独呈现"
            )
        if record.get("text_truncated"):
            notes.append(f"仅显示末尾 {_STREAM_TAIL_CHARS} 字（已接收 {chars_total} 字）")
        if record.get("reasoning_truncated"):
            notes.append("推理文本同样只显示末尾")
        stream_tail.set_text(" · ".join(notes))

    def _render_steps(steps: list[dict[str, Any]]) -> None:
        live = {int(step["step"]) for step in steps}
        for number in [n for n in step_rows if n not in live]:
            refs = step_rows.pop(number)
            refs["row"].delete()
        for step in steps:
            _sync_step_row(step)
        empty_steps.set_visibility(not steps)
        _render_current_step(steps)

    _BOARD_STATUS_ICON = {
        "completed": ("check_circle", _OK),
        "in_progress": ("radio_button_checked", _ACCENT),
        "pending": ("radio_button_unchecked", _MUTED),
        "blocked": ("block", _BAD),
    }

    def _render_board(text: str) -> None:
        """任务板：结构化渲染 TaskDoc——目标 + 路线检查单 + 紧凑流程线。"""

        board_box.clear()
        parsed = _parse_board(text)
        with board_box:
            if not any(
                [parsed["goal"], parsed["amendments"], parsed["items"], parsed["flow"]]
            ):
                _empty(
                    "assignment",
                    "任务板还没有内容",
                    "agent 写入 TaskDoc 后，这里显示目标、路线检查单与流程线。",
                )
                return
            if parsed["goal"] or parsed["amendments"]:
                ui.label("目标").classes("tw-label")
                with ui.element("div").classes("tw-board-goal w-full"):
                    if parsed["goal"]:
                        ui.label(parsed["goal"]).classes("text-[13.5px] font-semibold")
                    for amendment in parsed["amendments"]:
                        ui.label(f"· {amendment}").classes("text-xs mt-1").style(
                            f"color:{_MUTED}"
                        )
            if parsed["items"]:
                ui.label("路线").classes("tw-label")
                with ui.column().classes("w-full gap-0"):
                    for item in parsed["items"]:
                        icon, color = _BOARD_STATUS_ICON.get(
                            item["status"], ("radio_button_unchecked", _MUTED)
                        )
                        with ui.row().classes("tw-board-item w-full no-wrap"):
                            ui.icon(icon).style(f"font-size:17px; color:{color}")
                            ui.label(item["id"]).classes("tw-step-num")
                            with ui.column().classes("gap-0 grow"):
                                ui.label(item["content"]).classes("text-[13px]")
                                if item["note"]:
                                    ui.label(item["note"]).classes("tw-note")
            if parsed["flow"]:
                ui.label("流程线").classes("tw-label mt-1")
                with ui.column().classes("w-full gap-0"):
                    for line in parsed["flow"]:
                        ui.label(line).classes("tw-board-flow")
            if parsed["raw"]:
                ui.label(parsed["raw"]).classes(
                    "text-xs whitespace-pre-wrap"
                ).style(f"color:{_MUTED}")

    def _render_kb() -> None:
        entries = bridge.kb_entries()
        rows = [
            {
                "label": entry.get("label", ""),
                "package": entry.get("package", ""),
                "kind": _KIND_TEXT.get(entry.get("kind", ""), entry.get("kind", "")),
                "success_count": entry.get("success_count", 0),
                "stale": "已失效" if entry.get("stale") else "有效",
            }
            for entry in entries
        ]
        kb_table.rows = rows
        kb_table.update()
        kb_count.set_text(f"{len(rows)} 条")
        kb_hint.set_visibility(not rows)

    def _render_caps(caps: list[dict[str, Any]]) -> None:
        caps_row.clear()
        with caps_row:
            if not caps:
                ui.label("首次运行后显示能力状态").classes("tw-note")
                return
            for cap in caps:
                state_key = str(cap.get("state", ""))
                color, text = _CAP_STATE_STYLE.get(state_key, (_MUTED, state_key))
                missing = cap.get("missing_deps") or []
                label = f"{cap.get('title', cap.get('cap_id'))} · {text}"
                if missing:
                    label += f"（缺 {', '.join(missing)}）"
                with ui.element("span").classes("tw-chip grey"):
                    ui.element("span").style(
                        f"background:{color}; color:{color}"
                    ).classes("tw-dot" + (" live" if state_key == "shadow" else ""))
                    ui.label(label)

    def _render_memory() -> None:
        snapshot = bridge.memory_snapshot()
        stats = snapshot.get("recall_stats") or {}
        evaluations = int(stats.get("evaluations", 0) or 0)
        hits = int(stats.get("hits", 0) or 0)
        hit_at_1 = stats.get("hit_at_1")
        contaminated = int(
            stats.get("contaminated_runs", stats.get("false_hits", 0)) or 0
        )
        stat_evals.set_text(str(evaluations))
        if hit_at_1 is not None:
            stat_hit.set_text(f"{float(hit_at_1):.0%}")
        else:
            stat_hit.set_text(f"{hits / evaluations:.0%}" if evaluations else "—")
        stat_false.set_text(
            f"{contaminated / evaluations:.0%}" if evaluations else "—"
        )
        stat_card_sup.set_text(str(int(stats.get("procedure_suppressed", 0) or 0)))
        stat_rule_sup.set_text(str(int(stats.get("rule_suppressed", 0) or 0)))
        episodes = snapshot.get("episodes") or []
        stat_eps.set_text(str(len(episodes)))
        rows = []
        for episode in episodes:
            ts = episode.get("ts_start")
            time_text = (
                time.strftime("%m-%d %H:%M", time.localtime(float(ts))) if ts else "—"
            )
            goal = str(episode.get("goal_text", ""))
            rows.append(
                {
                    "time": time_text,
                    "goal": goal[:28] + ("…" if len(goal) > 28 else ""),
                    "outcome": ("✓ " if episode.get("success") else "✗ ")
                    + str(episode.get("reason", "")),
                    "steps": episode.get("steps", 0),
                    "tokens": _tokens_fmt(episode.get("tokens_total", 0)),
                    "verifier": _VERIFIER_TEXT.get(
                        str(episode.get("verifier", "")), "—"
                    ),
                }
            )
        memory_table.rows = rows
        memory_table.update()
        # Mode text reflects the actual configured RAG mode, not a hardcoded label.
        mode = str(getattr(config, "memory_rag", "shadow"))
        rag_mode_label.set_text(
            "回想模式 "
            + _RAG_MODE_TEXT.get(mode, mode)
            + "。抑制计数=授权复检拒绝下发的卡片/规则数，仅记 id 与原因，不含经验正文。"
        )

    outputs_sig: list[tuple] = [()]

    def _render_outputs() -> None:
        try:
            items = _api_deliverables()
        except OSError:
            items = []
        sig = tuple((i["run_id"], i["mtime"], i["size"]) for i in items)
        if sig == outputs_sig[0]:
            return
        outputs_sig[0] = sig
        outputs_count.set_text(f"{len(items)} 份")
        outputs_list.clear()
        if not items:
            with outputs_list:
                _empty(
                    "description",
                    "还没有产出",
                    "当任务要求攻略、计划或报告时，agent 会把 HTML 产物写到这里，"
                    "可沙箱预览、打开或删除。",
                )
            return
        for item in items:
            rid = item["run_id"]
            with outputs_list, ui.element("div").classes("tw-out w-full"):
                ui.icon("description").style(f"color:{_ACCENT}; font-size:19px")
                with ui.column().classes("gap-0 grow min-w-0"):
                    ui.label(item["title"] or rid).classes("text-[13.5px] font-bold truncate")
                    ui.label(
                        f"{time.strftime('%m-%d %H:%M', time.localtime(item['mtime']))}"
                        f" · {item['size'] / 1024:.1f} KB · {rid[:8]}"
                    ).classes("tw-note mono")

                def _preview(_e, rid=rid, title=item["title"]):
                    preview_title.set_text(title or rid)
                    preview_frame.props(f'sandbox src="/deliverables/{rid}"')
                    preview_dialog.open()

                def _delete(_e, rid=rid, title=item["title"]):
                    delete_hint.set_text(f"确定删除「{title or rid}」？此操作不可恢复。")
                    delete_confirm.on_click.clear()
                    delete_confirm.on("click", lambda _e2, rid=rid: _do_delete(rid))
                    delete_dialog.open()

                def _do_delete(rid):
                    try:
                        _delete_deliverable(rid)
                    except (OSError, ValueError):
                        pass
                    delete_dialog.close()
                    outputs_sig[0] = ()
                    _render_outputs()

                ui.button("预览", icon="visibility", on_click=_preview).props(
                    "flat dense no-caps"
                ).classes("tw-link")
                ui.button(
                    "打开",
                    icon="open_in_new",
                    on_click=lambda _e, rid=rid: ui.navigate.to(
                        f"/deliverables/{rid}", new_tab=True
                    ),
                ).props("flat dense no-caps").classes("tw-link")
                ui.button("删除", icon="delete", on_click=_delete).props(
                    "flat dense no-caps"
                ).style(f"color:{_BAD}; font-size:11.5px")

    def _tick_clock() -> None:
        """Update time-derived labels every second, independent of new events.

        A long stall must still visibly age — the elapsed and last-event
        counters advance here so the operator can tell the run is waiting, not
        dead, even when no new event arrives.
        """

        if not clock_state["active"]:
            return
        started = clock_state["started_at"]
        if started:
            elapsed_label.set_text(f"已持续 {_duration_fmt(time.time() - started)}")
        last_ts = clock_state["last_event_ts"]
        age_label.set_text(
            f"最近事件 {_age_fmt(time.time() - last_ts)}" if last_ts else "最近事件 —"
        )

    def render() -> None:
        state = bridge.snapshot()
        result = state["final_result"] or {}

        # Clock inputs are refreshed every render so _tick_clock has current data.
        clock_state["started_at"] = state.get("started_at")
        clock_state["last_event_ts"] = state.get("last_event_ts")
        status = state["status"]
        active = status in {"starting", "running", "waiting_hitl"}
        clock_state["active"] = active

        signature = (
            status,
            state.get("activity"),
            state.get("stop_requested"),
            state["screen_seq"],
            len(state["steps"]),
            tuple(
                (
                    step["step"],
                    step["intent"],
                    step["tool"],
                    step["target"],
                    step["status"],
                    step["result"],
                    step["latency_ms"],
                    step.get("tool_latency_ms"),
                    step.get("screen_seq"),
                )
                for step in state["steps"]
            ),
            state["current_app"],
            tuple(_frame_key(frame) for frame in state["screens"]),
            selected["key"],
            selected["pinned"],
            state["task_board"],
            state["pending_hitl_prompt"],
            state["tokens"],
            tuple(sorted((state["usage"] or {}).items())),
            result.get("reason"),
            state.get("requested_model"),
            state.get("actual_model"),
            tuple(
                (
                    item.get("attempt"),
                    item.get("status"),
                    item.get("revision"),
                    item.get("chars_total"),
                    item.get("reasoning_total"),
                    item.get("text_truncated"),
                    item.get("reasoning_truncated"),
                    item.get("error"),
                )
                for item in (state.get("stream_attempts") or [])
            ),
            stream_view["attempt"],
            stream_view["follow"],
            state.get("run_id"),
            tuple(
                (cap.get("cap_id"), cap.get("state"))
                for cap in (state.get("capabilities") or [])
            ),
        )
        if signature == ui_state["sig"]:
            return
        ui_state["sig"] = signature

        # --- status pill + "what are we waiting on" -----------------------
        activity = str(state.get("activity") or "")
        if active and activity in _ACTIVITY_META:
            text, color, live, hint = _ACTIVITY_META[activity]
        elif active:
            text, color, live, hint = ("运行中", _ACCENT, True, "")
        else:
            text, color, live = _STATUS_META.get(status, (status, _MUTED, False))
            hint = ""
        status_text.set_text(text)
        status_dot.style(f"background:{color}")
        status_dot.classes("tw-dot" + (" live" if live else ""), remove="tw-dot")
        status_pill.style(f"border-color:{color}55")
        activity_hint.set_text(hint)

        start_button.set_enabled(not active)
        stop_button.set_enabled(bool(active))
        step_count.set_text(f"{len(state['steps'])} 步")

        # --- run metadata (one compact line) ------------------------------
        run_id = state.get("run_id")
        run_id_label.set_text(run_id[:12] if run_id else "—")
        # Requested vs actual model. Actual is only ever what the provider
        # reported; with no such evidence we show 未上报 and never substitute
        # the requested (or a configured fallback) as if it were used.
        req_model = state.get("requested_model")
        req_model_label.set_text(_display(req_model))
        act_model = state.get("actual_model")
        if act_model:
            act_model_label.set_text(str(act_model))
            act_model_label.style(f"color:{_TEXT}")
        else:
            act_model_label.set_text("未上报")
            act_model_label.style(f"color:{_MUTED}")
        tokens_label.set_text(f"{_tokens_fmt(state['tokens'])} tokens")
        _tick_clock()

        usage = state["usage"] or {}
        total_usage = sum(usage.values())
        usage_total.set_text(f"共 {_tokens_fmt(total_usage)}" if usage else "")
        usage_bars.clear()
        if usage and total_usage:
            with usage_bars:
                for role, tokens in sorted(usage.items(), key=lambda pair: -pair[1]):
                    ui.label(_USAGE_ROLE_TEXT.get(role, role)).classes("tw-chip grey")
                    ui.label(_tokens_fmt(tokens)).classes("tw-note mono")

        # --- device frame + filmstrip ------------------------------------
        screens = state["screens"]
        if state["run_id"] != last_run_id["id"]:
            last_run_id["id"] = state["run_id"]
            selected["key"] = None
            selected["pinned"] = False
            _reset_steps()
        shown = _choose_frame(screens, selected)
        is_reference = bool(shown and shown.get("reference"))
        has_shot = bool(shown and shown.get("image"))
        screen_box.set_visibility(has_shot)
        no_screen.set_visibility(not has_shot)
        if has_shot:
            screen_image.props(f'src="{shown["image"]}"')
            if is_reference:
                screen_box.classes("is-ref")
            else:
                screen_box.classes(remove="is-ref")

        latest = screens[-1] if screens else None
        pinned_history = (
            selected["pinned"] and shown is not None and shown is not latest
        )
        if not has_shot:
            screen_meta.set_text("—")
        elif pinned_history:
            screen_meta.set_text(
                f"{_display(shown.get('app'))} · "
                + (
                    f"参考帧 {_display(shown.get('screen_ref'))}"
                    if is_reference
                    else f"#{_display(shown.get('seq'))}"
                )
            )
        elif is_reference:
            screen_meta.set_text(
                f"{_display(shown.get('app'))} · 参考帧 {_display(shown.get('screen_ref'))}"
            )
        else:
            screen_meta.set_text(
                f"{_display(state['current_app'])} · #{_display(state['screen_seq'])}"
            )
        if pinned_history:
            pin_chip.set_text("已钉住")
            pin_chip.classes("tw-chip", remove="grey")
        elif is_reference:
            pin_chip.set_text("参考帧未验证")
            pin_chip.classes("tw-chip warn", remove="grey")
        else:
            pin_chip.set_text("跟随最新")
            pin_chip.classes("tw-chip grey", remove="warn")
        back_live.set_visibility(pinned_history)

        reference_tag.clear()
        if is_reference:
            with reference_tag:
                with ui.row().classes("items-center gap-2"):
                    ui.label(
                        "参考图：当前未验证（无 screen_seq，不代表新观测）"
                    ).classes("tw-chip warn")

        thumbs.clear()
        with thumbs:
            for item in screens[-14:]:
                key = _frame_key(item)
                cls = "tw-thumb"
                if item.get("reference"):
                    cls += " is-ref"
                if selected["pinned"] and key == selected["key"]:
                    cls += " sel"
                ui.image(item["image"]).classes(cls).props("fit=cover").on(
                    "click", lambda _e, k=key: toggle_pin(k)
                )

        _render_steps(state["steps"])
        _render_stream(state.get("stream_attempts") or [])
        _render_board(state["task_board"] or "")

        prompt = state["pending_hitl_prompt"]
        hitl_prompt.set_text(prompt or "")
        hitl_panel.set_visibility(bool(prompt))

        _render_caps(state.get("capabilities") or [])

    ui.timer(refresh_seconds, render)
    ui.timer(1.0, _tick_clock)
    ui.timer(2.0, _render_kb)
    ui.timer(2.0, _render_memory)
    ui.timer(2.0, _render_outputs)


def run(
    *,
    device_id: str | None = None,
    model: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    """Resolve configuration, build the UI, and start NiceGUI."""

    load_project_env()
    overrides = {"device_id": device_id, "model_name": model}
    config = V2Config.from_env(overrides)
    bridge = WebRunBridge(overrides)

    @ui.page("/")
    def _index() -> None:
        # Explicit page registration: the auto-index page would re-execute
        # ``sys.argv[0]`` per request, which breaks ``python -m phone_agent.web``.
        create_ui(bridge, config=config)

    ui.run(
        host=host,
        port=port,
        title="TaskWizard 实时控制台",
        show=False,
        reload=False,
    )


__all__ = ["create_ui", "run"]
