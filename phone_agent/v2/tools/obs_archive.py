"""Observation-archive recall tools: ``recall_screen`` / ``search_screens``.

Mounted only while the ``obs_archive`` capability is on.  Both tools are
**read-only evidence** over this run's archived frames — they never observe the
device, never register marks and never execute anything (P0 #2 / P0 #15).  Every
historical mark id in their receipts is rendered non-addressable
(``历史:ax_3@e12（已失效）``), and the receipt text says explicitly that the
frame is a historical archive and that any action needs a fresh ``read_screen``.

Failures return honest error text (P0 #5); they never pretend success and never
alter the current mark batch.  Receipts carry no absolute pixels (P0 #1) and no
screenshot payload (P0 #6 — this plane is text-only by construction).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

from phone_agent.v2.obs_archive import (
    PAGE_LINES_DEFAULT,
    SEARCH_LIMIT_DEFAULT,
    ObsArchiveError,
)

_READ_ONLY_NOTE = (
    "历史证据，只读；帧内 mark 已失效（渲染为 历史:<id>（已失效）），"
    "不能用于 tap/long_press/type_text 等任何执行动作；"
    "要操作当前屏幕请先 read_screen。"
)


def _error_text(tool: str, exc: BaseException) -> str:
    """Honest failure receipt (P0 #5) for an archive read that could not run."""

    return (
        f"error: {tool} 失败（{type(exc).__name__}: {exc}）。"
        f"{_READ_ONLY_NOTE}存档读取失败不影响当前屏幕状态。"
    )


def _format_ts(ts: float) -> str:
    try:
        return (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except (OverflowError, OSError, ValueError):
        return "?"


def make_obs_archive_tools(session: Any, archive: Any) -> list[StructuredTool]:
    """Build the two read-only recall tools bound to *session*/*archive*."""

    def recall_screen(
        screen_seq: int,
        offset: int = 0,
        limit: int = PAGE_LINES_DEFAULT,
        intent: str = "",
        note: str | None = None,
    ) -> str:
        """回看本 run 某一历史观测帧的完整存档（只读证据，不是当前屏幕）。

        用于确认“之前那一屏到底有什么”。存档帧里的 mark 已失效，不能用于
        任何执行动作；要操作当前屏幕请先 read_screen。文本按行分页：回执
        给出本页行号范围与下一段 offset，读完为止。

        参数：
        - screen_seq：要回看的存档帧号（[OBS] 头的 screen#N）。
        - offset：起始行号（默认 0）；用上一页回执的 next offset 续读。
        - limit：本页最多返回的行数（默认 40，上限 80）。
        - intent：本步意图（务必填写，会汇入流程线）。
        - note：本步发现（可选）。
        """

        try:
            page = archive.page(screen_seq, offset=offset, limit=limit)
        except ObsArchiveError as exc:
            return _error_text("recall_screen", exc)
        except Exception as exc:  # noqa: BLE001 - honest error text, never a crash
            return _error_text("recall_screen", exc)

        lines = page.text.splitlines()
        if not lines:
            position = f"offset={page.offset} 超出范围（共 {page.total_lines} 行，可传 offset=0 从头读）"
        else:
            first = page.offset + 1
            last = page.offset + len(lines)
            position = f"第 {first}-{last} 行 / 共 {page.total_lines} 行"
            if page.next_offset is not None:
                position += f"（续读用 offset={page.next_offset}）"
            else:
                position += "（已读完）"
        current_seq = int(getattr(session, "screen_seq", 0) or 0)
        return (
            f"[RECALL] screen#{page.screen_seq} app={page.app} epoch={page.epoch} "
            f"存档时间={_format_ts(page.ts)}；{_READ_ONLY_NOTE}\n"
            f"当前观测已是 screen#{current_seq}，本帧是历史存档，屏幕可能早已变化。\n"
            f"{position}\n"
            f"---\n{page.text}"
        )

    def search_screens(
        query: str,
        limit: int = SEARCH_LIMIT_DEFAULT,
        intent: str = "",
        note: str | None = None,
    ) -> str:
        """在本 run 的观测存档里做全文检索（只读证据，不是当前屏幕）。

        返回命中的 screen_seq 列表与短摘要，再用 recall_screen 看完整帧。
        命中片段中的 mark id 已失效，不能用于任何执行动作；要操作当前屏幕
        请先 read_screen。

        参数：
        - query：检索词（中文/英文均可）。
        - limit：最多返回的命中帧数（默认 5，上限 10）。
        - intent：本步意图（务必填写，会汇入流程线）。
        - note：本步发现（可选）。
        """

        try:
            hits = archive.search(query, limit=limit)
        except Exception as exc:  # noqa: BLE001 - honest error text, never a crash
            return _error_text("search_screens", exc)
        if not hits:
            return (
                f"[RECALL-SEARCH] {query!r}: 本 run 观测存档中没有匹配帧。"
                f"（可用 recall_screen(screen_seq) 直接回看已知帧号）"
            )
        lines = [f"[RECALL-SEARCH] {query!r}: 命中 {len(hits)} 帧；{_READ_ONLY_NOTE}"]
        for hit in hits:
            lines.append(
                f"- screen#{hit.screen_seq} app={hit.app} epoch={hit.epoch} "
                f"存档时间={_format_ts(hit.ts)} | {hit.snippet}"
            )
        return "\n".join(lines)

    return [
        StructuredTool.from_function(recall_screen, parse_docstring=True),
        StructuredTool.from_function(search_screens, parse_docstring=True),
    ]


__all__ = ["make_obs_archive_tools"]
