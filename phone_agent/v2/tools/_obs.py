"""Shared tool helpers: auto-observation block and relative coordinate conversion.

Kept tool-local so the tools layer stays self-contained before ``v2/session.py``
and ``v2/coords.py`` (owned by the core worktree) land. The §7.4 observation
block is appended by every actuation tool after a successful device action.

Vision reflux (S1 §1): ``auto_observation`` returns a multimodal content list
(``list[dict]``) — a ``[OBS]`` text block plus an ``image_url`` block carrying
the fresh screenshot (with a ``screen_seq`` key the trace/prune layers key on).
The image is **always** emitted when the session has a screenshot payload:
same-screen image dedup was removed (A4) because it never fired in practice —
accessibility marks jitter across dumps, so the screenshot hash effectively
changed every step, making the dedup branch dead code. Historical-image growth
is bounded on the *history* side by ``middleware/images.py`` (keep newest N),
not on the produce side. Re-observation failure keeps the action fact and, when
the failed observation retained a valid earlier frame, ships that frame as an
explicitly earlier/unverified reference image (``reference``/``screen_ref``
metadata, never a fresh batch); with no valid frame it degrades to a single
text block. An actuation success is never lost to an observation hiccup and no
fake image is ever emitted.
"""

from __future__ import annotations

from typing import Any

from phone_agent.v2.session import ScreenshotError, clamp_action_settle_ms


def mark_tool_ok(session) -> None:
    """Record that the most recent actuation/perception tool call succeeded.

    Drives the finish review packet's "last action" mirror and its
    hard-contradiction check (S2 §1.2/§1.5): a value of ``False`` is a hard
    contradiction, ``None`` means unknown. Best-effort: a session double without
    the attribute must not crash the tool path.
    """

    try:
        session.last_tool_ok = True
    except Exception:  # noqa: BLE001 - best-effort state write, never block a tool
        pass


def mark_tool_fail(session) -> None:
    """Record that the most recent actuation/perception tool call failed."""

    try:
        session.last_tool_ok = False
    except Exception:  # noqa: BLE001 - best-effort state write, never block a tool
        pass


def format_marks_digest_fallback(marks, max_items: int = 40) -> str:
    """Render ``mark_id | role | text(<=32) | center`` lines (§6 sketch).

    Used only when the session does not expose ``format_marks_digest``. Matches
    the doc's per-line contract so the model sees a stable marks summary.
    """

    entries = (
        list(marks.items())
        if isinstance(marks, dict)
        else [(getattr(mark, "mark_id", "?"), mark) for mark in list(marks or [])]
    )
    lines: list[str] = []
    for mark_id, mark in entries[:max_items]:
        role = getattr(mark, "role", None) or "?"
        text = (getattr(mark, "text_summary", None) or "").strip()
        if len(text) > 32:
            text = text[:31] + "…"
        center = getattr(mark, "center", None) or [0, 0]
        try:
            cx, cy = int(center[0]), int(center[1])
        except (TypeError, ValueError, IndexError):
            cx, cy = 0, 0
        lines.append(f"{mark_id}|{role}|{text}|({cx},{cy})")
    body = " · ".join(lines)
    extra = ""
    if len(entries) > max_items:
        extra = f" …(+{len(entries) - max_items} more)"
    return body + extra


def format_observation_text(session, obs) -> str:
    """Build the canonical model-facing ``[OBS]`` text for one observation."""

    current_app = getattr(obs, "current_app", None) or "?"
    seq = getattr(obs, "screen_seq", getattr(session, "screen_seq", 0))
    marks = getattr(obs, "marks", None)
    if marks is None:
        marks = getattr(session, "marks", {})

    parse_summary = getattr(obs, "parse_summary", None)
    windows = getattr(obs, "windows", None)
    window_source = None
    total_candidates = None
    if isinstance(parse_summary, dict):
        window_source = parse_summary.get("window_source")
        # B3: total-before-cut lives in the parser's ``total_candidates`` (parallel
        # package A). Render ``marks (K/total)`` only when it is present and larger
        # than the shown count; absent field => omit (never hardcode a total).
        raw_total = parse_summary.get("total_candidates")
        if isinstance(raw_total, int):
            total_candidates = raw_total

    items = list(marks.values()) if isinstance(marks, dict) else list(marks or [])
    digest_fn = getattr(session, "format_marks_digest", None)
    if callable(digest_fn):
        try:
            digest = digest_fn(
                items, window_source=window_source, windows=windows
            )
        except TypeError:
            # Older/duck-typed digest signature without the B3 kwargs.
            digest = digest_fn(items)
    else:
        digest = format_marks_digest_fallback(items)

    retained_count = len(items)
    shown_count = min(retained_count, 40)
    select_fn = getattr(session, "select_marks_for_digest", None)
    if callable(select_fn):
        try:
            shown_count = len(select_fn(items, max_items=40))
        except TypeError:
            shown_count = len(select_fn(items))

    reported_total = max(retained_count, total_candidates or 0)
    count_field = f"{shown_count}"
    if reported_total > shown_count:
        count_field = f"{shown_count}/{reported_total}"
    retained_note = ""
    if shown_count < retained_count < reported_total:
        retained_note = f" [retained:{retained_count}]"
    # B2: a valid frame whose marks *dump* failed is annotated so the model never
    # reads "no controls" when the dump timed out / errored. A genuinely empty
    # screen (dump_empty / no_interactive_marks) is not annotated as a failure.
    annotation = _marks_failure_annotation(obs)
    return (
        f"[OBS] app={current_app} screen#{seq}\n"
        f"marks ({count_field}){retained_note}{annotation}: {digest}"
    )


def _obs_text(session, settle_ms: int | None = None) -> tuple[str, object]:
    """Observe once and build the ``[OBS]`` text; return ``(text, observation)``."""

    if settle_ms is None:
        obs = session.observe()
    else:
        obs = session.observe(settle_ms=settle_ms)
    return format_observation_text(session, obs), obs


_ANNOTATED_MARK_FAILURES = frozenset(
    {"timeout", "provider_error", "accessibility_xml_parse_error"}
)


def _marks_failure_annotation(obs) -> str:
    """Return `` [accessibility:<code>]`` when the marks dump failed (B2).

    Only transient/parse failures are annotated (an empty screen is legitimate).
    An unsupported ``marks_windowed=on`` dump surfaces here too rather than being
    swallowed. The parse_summary is available on the observation for the trace
    layer; the visible header just names the failure so a zero-mark frame reads
    as "dump failed" not "screen is empty".
    """

    code = getattr(obs, "marks_failure_code", None)
    if not code:
        return ""
    if code in _ANNOTATED_MARK_FAILURES or "unavailable" in str(code):
        return f" [accessibility:{code}]"
    return ""


_REFERENCE_NOTE = (
    "参考图：本次观测较早采样的一帧有效截图（screen_ref={ref}），"
    "当前画面未验证，不是当前可操作的标记批次，不能据它使用 mark 或坐标。"
)


def _reference_frame(session) -> dict[str, Any] | None:
    """Pull the session's unverified reference frame; missing accessor -> None."""

    getter = getattr(session, "last_reference_frame", None)
    if not callable(getter):
        return None
    try:
        frame = getter()
    except Exception:  # noqa: BLE001 - degrade to text-only, never crash
        return None
    if not isinstance(frame, dict):
        return None
    b64 = frame.get("b64")
    if not b64:
        return None
    return {
        "b64": b64,
        "mime": frame.get("mime") or "image/png",
        "ref": str(frame.get("ref") or "ref"),
    }


def observation_failure_blocks(
    session, headline: str, *, failure_code: str | None = None
) -> list[dict]:
    """Failure-side observation blocks: factual text + optional reference image.

    ``headline`` is the complete failure fact (e.g.
    ``[OBS] (re-observation failed: ...)``). When the failed observation retained
    a valid earlier-sampled frame, it is attached as an ordinary multimodal image
    block carrying ``reference``/``screen_ref`` metadata, and the text says
    explicitly that it is an earlier, unverified reference — never a fresh mark
    batch. A protected-screen failure or the absence of a valid frame yields a
    text-only result; no image is ever fabricated.
    """

    if failure_code == "secure_screenshot_blocked":
        return [{"type": "text", "text": headline}]
    frame = _reference_frame(session)
    if frame is None:
        return [{"type": "text", "text": headline}]
    note = _REFERENCE_NOTE.format(ref=frame["ref"])
    return [
        {"type": "text", "text": f"{headline}\n{note}"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{frame['mime']};base64,{frame['b64']}"},
            "reference": True,
            "screen_ref": frame["ref"],
        },
    ]


def auto_observation(session, settle_ms: int | None = None) -> list[dict]:
    """Return the §7.4 ``[OBS]`` block as a multimodal content list.

    Success: ``[{text}, {image_url, screen_seq}]`` — the fresh screenshot is
    always shipped when the session exposes a screenshot payload. (A4 removed the
    same-screen image dedup: it never fired because accessibility dumps jitter the
    screen hash almost every step; total image growth is bounded on the history
    side by ``middleware/images.py``.)

    Failure (re-observation raised) keeps a text block and, when the failed
    observation retained a valid earlier frame, adds it as an explicitly
    earlier/unverified ``reference`` image. With no valid frame — or on a
    protected screen — the result is text only; a fake image is never emitted.
    """

    effective_settle_ms = settle_ms
    clamp_note = ""
    if settle_ms is not None:
        effective_settle_ms, was_clamped = clamp_action_settle_ms(settle_ms)
        if was_clamped:
            clamp_note = (
                f"settle_ms 已从 {settle_ms} clamp 为 {effective_settle_ms}ms。"
            )

    try:
        text, obs = _obs_text(session, effective_settle_ms)
    except ScreenshotError as exc:
        if getattr(exc, "failure_code", None) == "secure_screenshot_blocked":
            marks = getattr(session, "marks", {})
            count = len(marks) if hasattr(marks, "__len__") else 0
            marks_text = (
                f"accessibility marks 剩 {count} 个"
                if count
                else "accessibility marks 为空"
            )
            receipt = (
                "[OBS] 此屏被系统级保护（登录/支付页）。\n"
                "截图不可用。\n"
                f"{marks_text}；涉及登录/支付时考虑 take_over 交人处理。"
            )
            if clamp_note:
                receipt += f" {clamp_note}"
            return [{"type": "text", "text": receipt}]
        suffix = f" {clamp_note}" if clamp_note else ""
        return observation_failure_blocks(
            session,
            f"[OBS] (re-observation failed: {exc}){suffix}",
            failure_code=getattr(exc, "failure_code", None),
        )
    except Exception as exc:  # noqa: BLE001 - observation is best-effort here
        suffix = f" {clamp_note}" if clamp_note else ""
        return observation_failure_blocks(
            session, f"[OBS] (re-observation failed: {exc}){suffix}"
        )

    if clamp_note:
        text += f"\n{clamp_note}"

    b64 = getattr(obs, "screenshot_b64", None)
    if not b64:
        # No screenshot payload (bring-up / text-only session): text only.
        return [{"type": "text", "text": text}]

    seq = getattr(obs, "screen_seq", getattr(session, "screen_seq", 0))
    mime = getattr(obs, "mime_type", None) or "image/png"
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
            "screen_seq": seq,
        },
    ]


def locate_observation(session, head: str) -> list[dict]:
    """Return the locate tool's same-frame observation (U1).

    ``locate`` runs the visual model on exactly one screenshot; the tool ships
    *that* frame back (text describing the registered mark + the screenshot the
    model located on) without a second ``observe()`` — single-producer
    discipline. When the session exposes no stashed locate frame (test doubles /
    text-only bring-up) it degrades to a single text block; a fake image is never
    emitted (fail-closed).
    """

    blocks: list[dict] = [{"type": "text", "text": head}]
    getter = getattr(session, "last_locate_frame", None)
    frame = None
    if callable(getter):
        try:
            frame = getter()
        except Exception:  # noqa: BLE001 - degrade to text-only, never crash
            frame = None
    if not frame:
        return blocks
    b64 = frame.get("b64") if isinstance(frame, dict) else None
    if not b64:
        return blocks
    mime = (frame.get("mime") if isinstance(frame, dict) else None) or "image/png"
    seq = frame.get("screen_seq", getattr(session, "screen_seq", 0))
    blocks.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
            "screen_seq": seq,
        }
    )
    return blocks
