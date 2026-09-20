"""Scripted chat-model double: canned responses, no gateway, no weights.

``responses`` are returned in order and the last one repeats once the script is
exhausted, so a test can script "call tap → call finish → talk". ``llm_type`` is
the ``_llm_type`` label LangChain reports; nothing in v2 branches on it, but the
per-suite labels are kept so traces stay readable.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    i: int = 0
    llm_type: str = "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    @property
    def _llm_type(self) -> str:
        return self.llm_type


__all__ = ["ScriptedModel"]
