"""Regression coverage for honest visual-locate infrastructure failures."""

from __future__ import annotations

from phone_agent.grounding.fake import FakeGroundingProvider
from phone_agent.v2.tools.actuation import build_actuation_tools
from phone_agent.v2.tools.perception import build_perception_tools

from tests.v2.test_locate_upgrade import _config, _session


def _tool(tools, name: str):
    return {item.name: item for item in tools}[name]


def _invoke_locate(tool, description: str):
    return tool.invoke(
        {
            "name": "locate",
            "args": {"description": description},
            "id": "locate-test",
            "type": "tool_call",
        }
    )


def test_provider_fault_is_preserved_in_session_and_locate_artifact():
    provider = FakeGroundingProvider(failure_code="provider_error")
    session = _session(provider)
    locate = _tool(build_perception_tools(session, _config()), "locate")

    result = _invoke_locate(locate, "target")

    assert "视觉定位服务故障（provider_error）" in result.content
    assert "refine" not in result.content
    assert "补充描述" not in result.content
    assert result.artifact["failure_code"] == "provider_error"
    assert session.marks == {}
    assert session.epoch == 2
    assert session.locate_provider_fault_streak() == 1


def test_describe_tap_provider_fault_does_not_suggest_description_refinement():
    provider = FakeGroundingProvider(failure_code="provider_error")
    session = _session(provider)
    tap = _tool(build_actuation_tools(session, _config()), "tap")

    result = tap.invoke({"target_description": "target"})

    assert "视觉定位服务故障（provider_error）" in result
    assert "refine" not in result
    assert "补充描述" not in result
    assert session.marks == {}


def test_locate_fault_circuit_trips_on_third_failure_and_success_resets_it():
    provider = FakeGroundingProvider(failure_code="provider_error")
    session = _session(provider)
    locate = _tool(build_perception_tools(session, _config()), "locate")

    receipts = [_invoke_locate(locate, "target") for _ in range(3)]

    assert "本轮定位服务持续故障：请停止使用 locate 与描述式 tap，改用 marks" not in receipts[1].content
    assert "本轮定位服务持续故障：请停止使用 locate 与描述式 tap，改用 marks" in receipts[2].content
    assert session.locate_provider_fault_streak() == 3

    provider.failure_code = None
    success = _invoke_locate(locate, "target")

    assert "已定位并注册为 mark" in "\n".join(
        block["text"]
        for block in success.content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert session.locate_provider_fault_streak() == 0


def test_grounding_no_candidate_keeps_existing_locate_receipt():
    session = _session(FakeGroundingProvider(failure_code="grounding_no_candidate"))
    locate = _tool(build_perception_tools(session, _config()), "locate")

    result = _invoke_locate(locate, "target")

    assert result.content == (
        "未定位: no confident match for 'target'（可补充目标外观/可见文字/相对位置，"
        "或用 scope 圈定区域后重试）"
    )
    assert "视觉定位服务故障" not in result.content


def test_bare_locate_exception_receipt_includes_exception_type_and_artifact_code():
    session = _session()
    # Keep the provider out of this test: it verifies the tool's final safety
    # net for a non-provider exception raised by the session boundary.
    session.locate = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("late"))
    locate = _tool(build_perception_tools(session, _config()), "locate")

    result = _invoke_locate(locate, "target")

    assert result.content == "定位失败: TimeoutError: late"
    assert result.artifact["failure_code"] == "TimeoutError"
