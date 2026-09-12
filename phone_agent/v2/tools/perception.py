"""Perception tools: read_screen / locate.

Per AGENTS.md. ``read_screen`` is a side-effect-free
re-observation; ``locate`` runs deep visual localization (LocateAnything) when
accessibility marks miss the target, registering the resolved mark on success.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from phone_agent.v2.resolver import candidate_summary
from phone_agent.v2.session import (
    LOCATE_PROVIDER_FAULT_STREAK_THRESHOLD,
    LocateAmbiguousError,
    classify_locate_failure,
    normalize_locate_failure_code,
)
from phone_agent.v2.tools._obs import (
    auto_observation,
    locate_observation,
    mark_tool_fail,
    mark_tool_ok,
)


def build_perception_tools(session, config) -> list[StructuredTool]:
    """Return the perception tool list bound to ``session``."""

    def read_screen(
        intent: str = "",
        note: str | None = None,
        settle_ms: int | None = None,
    ) -> list[dict]:
        """Re-observe the current screen (no side effects on the device).

        Returns the current app and a marks digest so you can pick a
        ``target_mark_id`` for the next action, plus a fresh screenshot image
        when the screen changed since the last one sent.

        Always pass ``intent`` (this step's goal). ``note`` optionally records
        what you discovered this step.
        ``settle_ms`` replaces the global observation delay.
        搜索/提交/打开页面后建议 1500-2500ms；普通点击留空。
        """

        mark_tool_ok(session)
        return auto_observation(session, settle_ms=settle_ms)

    def locate(
        description: str,
        intent: str = "",
        note: str | None = None,
        visible_text_hint: str | None = None,
        scope_mark_id: str | None = None,
        scope_start_mark_id: str | None = None,
        scope_end_mark_id: str | None = None,
    ) -> tuple[str | list[dict], dict]:
        """Deep visual localization for a target accessibility marks miss.

        On success the located element is registered as a new mark, tappable via
        ``tap(target_mark_id=...)``. Ambiguous/failed localization returns the
        candidates or the failure reason and registers nothing.
        ``visible_text_hint`` is exact text on or near the target. Optionally
        narrow the search with one container ``scope_mark_id``, or an interval
        from ``scope_start_mark_id`` to ``scope_end_mark_id``.

        Always pass ``intent`` (this step's goal). ``note`` optionally records
        what you discovered this step.
        """

        try:
            mark = session.locate(
                description,
                visible_text_hint=visible_text_hint,
                intent=intent,
                scope_mark_id=scope_mark_id,
                scope_start_mark_id=scope_start_mark_id,
                scope_end_mark_id=scope_end_mark_id,
            )
        except LocateAmbiguousError as exc:
            mark_tool_fail(session)
            failure_code = normalize_locate_failure_code(
                getattr(exc, "failure_code", None)
            )
            failure_class = classify_locate_failure(failure_code)
            if failure_code == "ambiguous":
                content = (
                    f"未定位: {exc}（可用 scope 收紧区域，或补充更独特的可见文字）"
                )
            elif failure_class == "service_fault":
                content = (
                    f"视觉定位服务故障（{failure_code}）：与目标描述无关，修改描述无效。"
                    "可原样重试一次；若再次故障，改用 read_screen 返回的 marks"
                    "（target_mark_id）或 back/导航绕过，并在 finish 时向用户说明该故障。"
                )
                if _locate_fault_circuit_tripped(session):
                    content += (
                        "本轮定位服务持续故障：请停止使用 locate 与描述式 tap，改用 marks"
                    )
            elif failure_class == "transient":
                content = (
                    f"定位暂时失败（{failure_code}）：可原样重试，勿修改目标描述"
                )
            else:
                content = (
                    f"未定位: {exc}（可补充目标外观/可见文字/相对位置，"
                    "或用 scope 圈定区域后重试）"
                )
            return content, _locate_artifact(session, failure_code=failure_code)
        except Exception as exc:  # noqa: BLE001 - surface provider failure text
            mark_tool_fail(session)
            return (
                f"定位失败: {type(exc).__name__}: {exc}",
                _locate_artifact(session, failure_code=type(exc).__name__),
            )
        mark_tool_ok(session)
        head = (
            f"已定位并注册为 mark {mark.mark_id}，可用 "
            f"tap(target_mark_id={mark.mark_id!r}) 点击 [{candidate_summary(mark)}]"
        )
        # U1: return the same frame the visual model located on (no extra observe).
        return locate_observation(session, head), _locate_artifact(session)

    return [
        StructuredTool.from_function(read_screen, parse_docstring=True),
        StructuredTool.from_function(
            locate, parse_docstring=True, response_format="content_and_artifact"
        ),
    ]


def _locate_artifact(session, *, failure_code: str | None = None) -> dict:
    """Trace-safe locate metadata; never includes screenshot pixels/base64."""

    getter = getattr(session, "last_locate_metadata", None)
    if not callable(getter):
        value = {}
    else:
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - metadata must never break the tool result
            value = {}
    artifact = dict(value) if isinstance(value, dict) else {}
    if failure_code is not None:
        artifact.setdefault("failure_code", str(failure_code))
    return artifact


def _locate_fault_circuit_tripped(session) -> bool:
    """Return whether this run has reached the locate-fault circuit threshold."""

    getter = getattr(session, "locate_provider_fault_streak", None)
    if not callable(getter):
        return False
    try:
        return int(getter()) >= LOCATE_PROVIDER_FAULT_STREAK_THRESHOLD
    except Exception:  # noqa: BLE001 - guidance must never break a receipt
        return False
