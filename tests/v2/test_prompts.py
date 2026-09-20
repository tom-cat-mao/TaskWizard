"""Prompt contract: ``target_description`` is short visible text, not a sentence.

Real-device evidence (WP3): the model wrote positional sentences such as
``搜索结果第一行「沈阳市」（带蓝色「城市」标签）`` into ``target_description``. The
resolver only does exact / substring / normalized matching over mark text
(``v2/resolver.py``), so every one of those calls missed and silently dropped to
the 43s visual fallback — the prompt had told it, in detail, to write
"appearance + visible text + relative position" for ``locate`` and had left
``target_description`` as "natural language". These tests keep the addressing
rule that distinguishes the two, in both prompt languages.
"""

from __future__ import annotations

import pytest

from phone_agent.v2.prompts import SYSTEM_PROMPT_EN, SYSTEM_PROMPT_ZH, get_system_prompt

# The rule has to name the mechanism (substring on-screen text) and the
# alternative route (locate), or the model has nothing to act on.
_ZH_RULE = ("target_description", "短原文", "包含匹配", "locate")
_EN_RULE = ("target_description", "short text visible on the control", "substring", "locate")


@pytest.mark.parametrize("lang", ["cn", "zh", "zh-cn", "zh_cn", "chinese"])
def test_chinese_prompt_states_the_short_text_rule(lang):
    text = get_system_prompt(lang)
    assert text == SYSTEM_PROMPT_ZH
    missing = [token for token in _ZH_RULE if token not in text]
    assert missing == [], (
        "Chinese system prompt must keep the target_description addressing rule "
        f"(missing: {missing})"
    )


@pytest.mark.parametrize("lang", ["en", "us", "", None])
def test_english_prompt_states_the_short_text_rule(lang):
    text = get_system_prompt(lang)  # type: ignore[arg-type]
    assert text == SYSTEM_PROMPT_EN
    missing = [token for token in _EN_RULE if token not in text]
    assert missing == [], (
        "English system prompt must keep the target_description addressing rule "
        f"(missing: {missing})"
    )


def test_both_prompts_keep_the_mark_id_route_and_the_locate_split():
    """The correction narrows ``target_description``; it never removes routes."""

    for text in (SYSTEM_PROMPT_ZH, SYSTEM_PROMPT_EN):
        assert "target_mark_id" in text
        assert "locate" in text
