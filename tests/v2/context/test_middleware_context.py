"""Context-side middleware: image/marks pruning and the token-budget mirror.

Split out of the former ``test_middleware.py`` when the v2 suite was laid out by
domain; the safety/trace halves live in ``safety/test_middleware_safety.py`` and
every assertion moved verbatim. No real device, no MLX, no network.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from phone_agent.v2.middleware.images import (
    ImagePruningMiddleware,
    build_context_pruning_middleware,
)
from phone_agent.v2.middleware.budget import (
    BudgetMiddleware,
    build_budget_middleware,
)


# --------------------------------------------------------------------------
# 9.2 image pruning
# --------------------------------------------------------------------------
def _image_msg(seq: int) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": f"screen {seq}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,AAAA{seq}"}},
        ]
    )


def _obs_msg(app: str, seq: int, marks: int = 3) -> HumanMessage:
    """A ToolMessage-shaped observation: OBS text (with marks) + an image block."""

    digest = " · ".join(f"ax_{i}|Button|t{i}|(0,0)" for i in range(marks))
    return HumanMessage(
        content=[
            {"type": "text", "text": f"[OBS] app={app} screen#{seq}\nmarks ({marks}): {digest}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,IMG{seq}"}, "screen_seq": seq},
        ]
    )


def _count_images(message) -> int:
    return sum(
        1
        for block in message.content
        if isinstance(block, dict) and block.get("type") in {"image_url", "image"}
    )


def _obs_texts(message) -> list[str]:
    return [
        b["text"]
        for b in message.content
        if isinstance(b, dict) and b.get("type") == "text" and b["text"].startswith("[OBS] ")
    ]


def test_images_pruning_keeps_only_newest():
    m1, m2, m3 = _image_msg(1), _image_msg(2), _image_msg(3)
    state = {"messages": [m1, m2, m3]}
    # Legacy alias keeps only the newest image.
    mw = ImagePruningMiddleware()
    result = mw.before_model(state, runtime=None)

    assert result is not None
    assert _count_images(m1) == 0
    assert _count_images(m2) == 0
    assert _count_images(m3) == 1
    placeholders = [
        b["text"] for b in m1.content if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert any("已剪除" in text for text in placeholders)


def test_images_pruning_noop_with_single_image():
    m = _image_msg(1)
    mw = ImagePruningMiddleware()
    assert mw.before_model({"messages": [m]}, runtime=None) is None
    assert _count_images(m) == 1


def test_context_pruning_keeps_newest_two_images():
    m1, m2, m3 = _image_msg(1), _image_msg(2), _image_msg(3)
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=2)
    result = mw.before_model({"messages": [m1, m2, m3]}, runtime=None)

    assert result is not None
    # Newest two keep their image; the oldest is placeholdered.
    assert _count_images(m1) == 0
    assert _count_images(m2) == 1
    assert _count_images(m3) == 1
    assert any(
        isinstance(b, dict) and b.get("type") == "text" and "已剪除" in b["text"]
        for b in m1.content
    )


def test_context_pruning_noop_at_or_below_keep():
    m1, m2 = _image_msg(1), _image_msg(2)
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=2)
    assert mw.before_model({"messages": [m1, m2]}, runtime=None) is None
    assert _count_images(m1) == 1
    assert _count_images(m2) == 1


def test_context_pruning_placeholder_uses_real_screen_seq():
    # C1: the image block carries a real screen_seq (5,6,7); the placeholder must
    # point back at the true frame (screen#5), not the crop-order counter (#1).
    msgs = [_obs_msg("app", 5), _obs_msg("app", 6), _obs_msg("app", 7)]
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=100)
    result = mw.before_model({"messages": msgs}, runtime=None)

    assert result is not None
    placeholders = [
        b["text"]
        for b in msgs[0].content
        if isinstance(b, dict) and b.get("type") == "text" and "已剪除" in b["text"]
    ]
    assert placeholders == ["[screen#5 已剪除]"]


def test_context_pruning_placeholder_falls_back_to_crop_counter():
    # Image blocks without a screen_seq keep the chronological fallback (#1).
    m1, m2, m3 = _image_msg(1), _image_msg(2), _image_msg(3)
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=2)
    mw.before_model({"messages": [m1, m2, m3]}, runtime=None)
    placeholders = [
        b["text"]
        for b in m1.content
        if isinstance(b, dict) and b.get("type") == "text" and "已剪除" in b["text"]
    ]
    assert placeholders == ["[screen#1 已剪除]"]


def test_context_pruning_folds_old_marks_keeps_newest_two():
    msgs = [_obs_msg("app", i) for i in range(1, 5)]  # 4 OBS messages
    mw = build_context_pruning_middleware(keep_images=100, keep_marks=2)
    result = mw.before_model({"messages": msgs}, runtime=None)

    assert result is not None
    # Oldest two folded; newest two keep the full marks digest.
    assert "[marks 已折叠:3]" in _obs_texts(msgs[0])[0]
    assert "[marks 已折叠:3]" in _obs_texts(msgs[1])[0]
    assert "marks (3):" in _obs_texts(msgs[2])[0]
    assert "marks (3):" in _obs_texts(msgs[3])[0]
    # Folded lines keep the header (app/screen).
    assert _obs_texts(msgs[0])[0].startswith("[OBS] app=app screen#1")


def test_context_pruning_marks_fold_is_idempotent():
    msgs = [_obs_msg("app", i) for i in range(1, 5)]
    mw = build_context_pruning_middleware(keep_images=100, keep_marks=2)
    mw.before_model({"messages": msgs}, runtime=None)
    snapshot = [list(m.content) for m in msgs]
    # Second pass must not change already-folded history (stable prefix).
    second = mw.before_model({"messages": msgs}, runtime=None)
    assert second is None
    assert [list(m.content) for m in msgs] == snapshot


def test_context_pruning_image_and_marks_same_message_deduped():
    # Newest keeps both; oldest loses image AND marks in one returned message.
    msgs = [_obs_msg("app", 1), _obs_msg("app", 2), _obs_msg("app", 3)]
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=2)
    result = mw.before_model({"messages": msgs}, runtime=None)

    returned = result["messages"]
    # msgs[0] was hit by both passes but must appear only once.
    assert returned.count(msgs[0]) == 1
    assert _count_images(msgs[0]) == 0
    assert "[marks 已折叠:3]" in _obs_texts(msgs[0])[0]
    # Newest message keeps both image and full marks.
    assert _count_images(msgs[2]) == 1
    assert "marks (3):" in _obs_texts(msgs[2])[0]


def test_context_pruning_first_obs_without_marks_not_folded():
    # Opening HumanMessage's [OBS] app=... has no marks section -> never folded.
    opening = HumanMessage(content=[{"type": "text", "text": "[OBS] app=com.x"}])
    msgs = [opening, _obs_msg("app", 1), _obs_msg("app", 2), _obs_msg("app", 3)]
    mw = build_context_pruning_middleware(keep_images=100, keep_marks=2)
    mw.before_model({"messages": msgs}, runtime=None)
    # The opening block is untouched (no "marks (" marker to fold).
    assert opening.content[0]["text"] == "[OBS] app=com.x"

# --------------------------------------------------------------------------
# 3.1 budget middleware (A4: token-cost mirror + hard ceiling, cumulative)
# --------------------------------------------------------------------------
def _budget_text(result) -> str:
    assert result is not None
    return result["messages"][0].content


def _ai(input_tokens: int, output_tokens: int, mid: str) -> AIMessage:
    return AIMessage(
        content="",
        id=mid,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def _advance(mw: BudgetMiddleware, msg: AIMessage) -> None:
    """Simulate one model call: after_model accounts the newest AI turn."""

    mw.after_model({"messages": [msg]}, runtime=None)


def test_budget_warn_fires_once_when_remaining_crosses_line():
    mw = build_budget_middleware(token_budget=1000, warn_remaining=200)
    # Below the line: no mirror injected (used 700 -> remaining 300 > 200).
    _advance(mw, _ai(500, 200, "a1"))
    assert mw.before_model({"messages": []}, runtime=None) is None
    # Cross the line: used 850 -> remaining 150 <= 200 -> one mirror.
    _advance(mw, _ai(100, 50, "a2"))
    text = _budget_text(mw.before_model({"messages": []}, runtime=None))
    assert text.startswith("Token 预算余量：已用约 850/1000")
    assert "剩约 150" in text
    # One-shot: never fires again even as usage grows (still below ceiling).
    _advance(mw, _ai(10, 10, "a3"))
    assert mw.before_model({"messages": []}, runtime=None) is None


def test_budget_hard_ceiling_jumps_to_end_once():
    mw = build_budget_middleware(token_budget=100, warn_remaining=30)
    _advance(mw, _ai(80, 30, "a1"))  # used 110 >= 100
    result = mw.before_model({"messages": []}, runtime=None)
    assert result is not None
    assert result["jump_to"] == "end"
    assert "[TOKEN_BUDGET_EXHAUSTED]" in result["messages"][0].content
    assert mw.exhausted is True
    # Still jumps on a subsequent call but does not re-inject the marker message.
    again = mw.before_model({"messages": []}, runtime=None)
    assert again == {"jump_to": "end"}


def test_budget_uses_usage_metadata_cumulatively_not_live_transcript():
    # Cumulative counter survives compaction: even if the live transcript is
    # replaced (AIMessages dropped), used_tokens keeps the billed total.
    mw = build_budget_middleware(token_budget=10_000, warn_remaining=1)
    _advance(mw, _ai(1000, 500, "a1"))
    _advance(mw, _ai(1000, 500, "a2"))
    assert mw.used_tokens == 3000
    # A "compaction" empties the transcript; before_model must not reset the gauge.
    mw.before_model({"messages": []}, runtime=None)
    assert mw.used_tokens == 3000


def test_budget_double_count_guard_on_same_ai_id():
    mw = build_budget_middleware(token_budget=10_000, warn_remaining=1)
    msg = _ai(100, 50, "same")
    _advance(mw, msg)
    _advance(mw, msg)  # same id -> not counted twice
    assert mw.used_tokens == 150


def test_budget_falls_back_to_estimate_without_usage():
    mw = build_budget_middleware(token_budget=10_000, warn_remaining=1)
    # No usage_metadata -> crude len//4 estimate over the content text.
    _advance(mw, AIMessage(content="x" * 400, id="a1"))
    assert mw.used_tokens == 100


def test_budget_reset_re_arms_everything():
    mw = BudgetMiddleware(token_budget=100, warn_remaining=30)
    _advance(mw, _ai(80, 30, "a1"))
    assert mw.before_model({"messages": []}, runtime=None)["jump_to"] == "end"
    mw.reset()
    assert mw.used_tokens == 0
    assert mw.exhausted is False
    assert mw.before_model({"messages": []}, runtime=None) is None


def test_budget_warn_remaining_out_of_range_clamps():
    # A warn line larger than the budget or non-positive is clamped sanely.
    assert BudgetMiddleware(token_budget=1000, warn_remaining=0).warn_remaining == 1000
    assert BudgetMiddleware(token_budget=500, warn_remaining=99999).warn_remaining == 500


def test_budget_english_text():
    mw = BudgetMiddleware(token_budget=1000, warn_remaining=200, lang="en")
    _advance(mw, _ai(600, 250, "a1"))  # used 850 -> remaining 150
    text = _budget_text(mw.before_model({"messages": []}, runtime=None))
    assert text.startswith("Token budget remaining: ~850/1000 used, ~150 left.")
