"""What the model sees when the opening observation fails.

Moved verbatim out of the S2 observation-contract file: these two cases pin the
diagnostic *surface* the runner leaves behind — a real mime type on the image
block, the ``[accessibility:<code>]`` annotation, and the honest "opening
observation failed" vocabulary (never a promise to retry).  Synthetic
screenshots only; no device, model, network or MLX.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage
import pytest

from phone_agent.v2.agent import ThinPhoneAgent, _first_observation_content
from phone_agent.v2.session import ScreenshotError


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_opening_observation_uses_real_mime_and_dump_failure_annotation(mime):
    obs = SimpleNamespace(
        screenshot_b64="QUJD",
        mime_type=mime,
        screen_seq=3,
        current_app="com.example",
        marks=[],
        marks_failure_code="timeout",
        parse_summary={"total_candidates": 0},
        windows=None,
    )

    content = _first_observation_content(obs, "do it")

    image = next(block for block in content if block.get("type") == "image_url")
    text = next(block["text"] for block in content if str(block.get("text", "")).startswith("[OBS]"))
    assert image["image_url"]["url"].startswith(f"data:{mime};base64,")
    assert "marks (0) [accessibility:timeout]" in text


def test_initial_observation_failure_is_visible_and_descriptive():
    class _FailingSession:
        def observe(self):
            raise ScreenshotError(
                "screenshot invalid: adb_screencap_failed",
                failure_code="adb_screencap_failed",
            )

    agent = object.__new__(ThinPhoneAgent)
    agent.session = _FailingSession()
    agent._system_prompt = "system"
    agent._capability_ctx = None
    agent._render_lesson_prompt_block = lambda: None

    messages = agent._initial_messages("do it")

    human = next(message for message in messages if isinstance(message, HumanMessage))
    texts = [block["text"] for block in human.content if block.get("type") == "text"]
    assert texts[0] == "do it"
    assert texts[1].startswith("[OBS] (opening observation failed:")
    assert "adb_screencap_failed" in texts[1]
    assert "retry" not in texts[1].casefold()
