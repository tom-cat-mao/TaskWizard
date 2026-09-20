"""Tests for ContextPrunerService facade and compaction-internal-call pattern (D5).

Per PLUGIN-D-PLAN.md Phase 3 / D5: context pruning is exposed as a named core
service (``context_pruner``) so a future compaction middleware can call it
internally before summarising.  No real device, MLX, or network.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from phone_agent.v2.agent import ThinPhoneAgent
from phone_agent.v2.capabilities import CapabilityAssemblyContext
from phone_agent.v2.events import MODEL_PRE_REQUEST
from phone_agent.v2.middleware.images import (
    ContextPrunerService,
    ContextPruningMiddleware,
    build_context_pruning_middleware,
)


# --------------------------------------------------------------------------
# helpers (mirrors test_middleware.py)
# --------------------------------------------------------------------------
def _image_msg(seq: int) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": f"screen {seq}"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,AAAA{seq}"},
            },
        ]
    )


def _obs_msg(app: str, seq: int, marks: int = 3) -> HumanMessage:
    digest = " · ".join(f"ax_{i}|Button|t{i}|(0,0)" for i in range(marks))
    return HumanMessage(
        content=[
            {
                "type": "text",
                "text": f"[OBS] app={app} screen#{seq}\nmarks ({marks}): {digest}",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,IMG{seq}"},
                "screen_seq": seq,
            },
        ]
    )


def _action_pair(i: int) -> list[AIMessage | ToolMessage]:
    """One AI tool-call -> Tool result turn carrying real action text."""

    return [
        AIMessage(
            content="",
            id=f"a{i}",
            tool_calls=[
                {
                    "name": "tap",
                    "args": {"target_mark_id": f"ax_{i}"},
                    "id": f"c{i}",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=f"已点击 mark ax_{i}",
            id=f"t{i}",
            tool_call_id=f"c{i}",
            name="tap",
        ),
    ]


def _count_images(message: Any) -> int:
    return sum(
        1
        for block in message.content
        if isinstance(block, dict) and block.get("type") in {"image_url", "image"}
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    if isinstance(message, AIMessage):
        calls = getattr(message, "tool_calls", None) or []
        for call in calls:
            if isinstance(call, dict):
                parts.append(str(call.get("name", "")))
    return " ".join(p for p in parts if p)


def _contains_image_payload(messages: list[Any], payload: str) -> bool:
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    url = block.get("image_url", {}) or {}
                    if isinstance(url, dict) and url.get("url", "") == payload:
                        return True
                    if block.get("image_url", "") == payload:
                        return True
    return False


# --------------------------------------------------------------------------
# (a) service retrievable from the core service namespace
# --------------------------------------------------------------------------
def test_service_retrievable_from_capability_context():
    pruner = ContextPrunerService(keep_images=2, keep_marks=2)
    ctx = CapabilityAssemblyContext({"context_pruner": pruner})

    assert ctx.service("context_pruner") is pruner


def test_service_accessible_via_agent_capability_context(monkeypatch, tmp_path):
    from tests.v2.test_experience import _install_mini_agent_modules

    config = _install_mini_agent_modules(monkeypatch, tmp_path, enabled=False)
    config.image_keep = 2
    config.obs_marks_keep = 2

    agent = ThinPhoneAgent(config)
    service = agent._capability_ctx.service("context_pruner")

    assert isinstance(service, ContextPrunerService)
    assert service.keep_images == 2
    assert service.keep_marks == 2
    # Image pruning is now performed by a model/pre_request listener, not by a
    # middleware in the LangChain stack.
    middleware = agent._capability_ctx.middleware
    assert not any(isinstance(m, ContextPruningMiddleware) for m in middleware)


# --------------------------------------------------------------------------
# (b) direct service call is byte-equivalent to the middleware path
# --------------------------------------------------------------------------
def _fresh_obs_messages() -> list[HumanMessage]:
    return [_obs_msg("app", i) for i in range(1, 5)]


def test_service_prune_matches_middleware_path():
    pruner = ContextPrunerService(keep_images=100, keep_marks=2)
    mw = build_context_pruning_middleware(keep_images=100, keep_marks=2)

    service_msgs = _fresh_obs_messages()
    mw_msgs = _fresh_obs_messages()

    service_modified = pruner.prune(service_msgs)
    mw_result = mw.before_model({"messages": mw_msgs}, runtime=None)

    assert service_modified == mw_result["messages"]
    # Oldest two OBS marks folded; newest two retain the digest.
    assert "[marks 已折叠:3]" in _message_text(mw_msgs[0])
    assert "[marks 已折叠:3]" in _message_text(mw_msgs[1])
    assert "marks (3):" in _message_text(mw_msgs[2])
    assert "marks (3):" in _message_text(mw_msgs[3])


def test_service_and_middleware_dedup_same_message_hit_by_both_passes():
    pruner = ContextPrunerService(keep_images=2, keep_marks=2)
    mw = build_context_pruning_middleware(keep_images=2, keep_marks=2)

    service_msgs = [_obs_msg("app", 1), _obs_msg("app", 2), _obs_msg("app", 3)]
    mw_msgs = [_obs_msg("app", 1), _obs_msg("app", 2), _obs_msg("app", 3)]

    service_modified = pruner.prune(service_msgs)
    mw_result = mw.before_model({"messages": mw_msgs}, runtime=None)

    # The oldest message is hit by both image pruning and marks folding but
    # appears only once in the returned update list (identity dedup).
    assert service_modified == mw_result["messages"]
    assert service_modified.count(mw_msgs[0]) == 1
    assert _count_images(mw_msgs[0]) == 0
    assert "[marks 已折叠:3]" in _message_text(mw_msgs[0])


# --------------------------------------------------------------------------
# (d) compact on/off: image pruning always happens exactly once
# --------------------------------------------------------------------------
def _prune_call_counter(service: ContextPrunerService):
    calls = 0
    orig = service.prune

    def counted(messages: list[Any]) -> list[Any]:
        nonlocal calls
        calls += 1
        return orig(messages)

    service.prune = counted
    return lambda: calls


def test_compact_off_prunes_images_exactly_once(monkeypatch, tmp_path):
    from tests.v2.test_experience import _install_mini_agent_modules

    config = _install_mini_agent_modules(monkeypatch, tmp_path, enabled=False)
    config.compact_enabled = False
    config.image_keep = 2
    config.obs_marks_keep = 2

    agent = ThinPhoneAgent(config)
    service = agent._capability_ctx.service("context_pruner")
    get_calls = _prune_call_counter(service)

    msgs = [_obs_msg("app", i) for i in range(1, 5)]
    agent.event_bus.waterfall(
        MODEL_PRE_REQUEST, msgs, terminal=lambda x: x
    )

    assert get_calls() == 1
    assert _count_images(msgs[0]) == 0
    assert "[screen#1 已剪除]" in _message_text(msgs[0])
    assert _count_images(msgs[3]) == 1


# The compact-ON prune-once invariant is asserted through the assembled chain
# by tests/v2/test_event_chain_behavior.py::test_t2_fold_prunes_once_and_
# taskdoc_stays_pinned (which also pins the TaskDoc re-attach); the compact-OFF
# unit variant below keeps the pruner-count guarded at unit level.


# --------------------------------------------------------------------------
# (c) replacement compact can call pruner internally before summarising
# --------------------------------------------------------------------------
class _RecordingCompactMiddleware(AgentMiddleware):
    """Fake compaction middleware that optionally calls the pruner, then records
    the messages it would hand to a summariser.
    """

    def __init__(self, pruner: ContextPrunerService | None, recorded: list[list[Any]]) -> None:
        super().__init__()
        self._pruner = pruner
        self._recorded = recorded

    def before_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        messages = list(state.get("messages") or [])
        if self._pruner is not None:
            self._pruner.prune(messages)
        self._recorded.append(messages)
        return None

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:  # noqa: ANN001
        return self.before_model(state, runtime)


def _build_action_history() -> list[Any]:
    """A transcript: task + several OBS screenshots + action turns."""

    messages: list[Any] = [
        HumanMessage(content=[{"type": "text", "text": "打开设置并连 WLAN"}])
    ]
    for i in range(1, 5):
        messages.append(_obs_msg("settings", i))
        messages.extend(_action_pair(i))
    return messages


def test_internal_pruner_call_yields_real_action_history_for_summary():
    """C1: a replacement compact that calls pruner before summarising still sees
    the real textual action history; only image payloads are replaced by small
    placeholders.  This proves the internal-call pattern is feasible.
    """

    recorded: list[list[Any]] = []
    pruner = ContextPrunerService(keep_images=2, keep_marks=2)
    compact = _RecordingCompactMiddleware(pruner=pruner, recorded=recorded)

    messages = _build_action_history()
    compact.before_model({"messages": messages}, runtime=None)

    summariser_input = recorded[0]
    flat_text = "\n".join(_message_text(m) for m in summariser_input)

    # Real action history text is preserved for the summariser.
    for i in range(1, 5):
        assert f"已点击 mark ax_{i}" in flat_text
    assert "tap" in flat_text

    # Old image blocks have been pruned to cheap placeholders.
    old_obs = [m for m in summariser_input if m is messages[1]]
    assert old_obs
    assert _count_images(old_obs[0]) == 0
    assert "[screen#1 已剪除]" in flat_text

    # No full base64 payload remains in the pruned old messages.
    assert not _contains_image_payload(summariser_input, "data:image/png;base64,IMG1")


def test_without_internal_pruner_call_summary_input_keeps_image_payloads():
    """Contrast: a compact that does NOT call the pruner internally receives
    full image payloads in its summariser input, wasting the summary budget.
    """

    recorded: list[list[Any]] = []
    compact = _RecordingCompactMiddleware(pruner=None, recorded=recorded)

    messages = _build_action_history()
    compact.before_model({"messages": messages}, runtime=None)

    summariser_input = recorded[0]
    flat_text = "\n".join(_message_text(m) for m in summariser_input)

    # Action text is still there.
    assert "已点击 mark ax_1" in flat_text

    # But old image payloads are still present, not pruned.
    assert _contains_image_payload(summariser_input, "data:image/png;base64,IMG1")
    assert "[screen#1 已剪除]" not in flat_text


# --------------------------------------------------------------------------
# service contract
# --------------------------------------------------------------------------
def test_service_keep_values_clamped_and_int():
    pruner = ContextPrunerService(keep_images=0, keep_marks=-1)
    assert pruner.keep_images == 1
    assert pruner.keep_marks == 1
