"""The session double: a duck-typed §6 ``PhoneSession`` for tools-level tests.

Used where the real ``PhoneSession`` would drag in ADB/MLX; the observation-layer
tests use the real session against :class:`~tests.v2.doubles.device.FakeDeviceFactory`
instead. Coordinate conversion mirrors ``v2.coords`` semantics inline
(``x = int(rel / 1000 * w)``) so the double stays import-light.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from phone_agent.grounding.provider import MarkCandidate
from phone_agent.v2.resolver import LocateAmbiguousError, StaleMarkError
from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.device import FakeDeviceFactory


@dataclass
class FakeObservation:
    current_app: str
    marks: dict[str, MarkCandidate]
    screen_seq: int
    screenshot_b64: str = ""
    width: int = 1080
    height: int = 2400
    screen_hash: str = ""
    mime_type: str = "image/png"


class FakePhoneSession:
    """Duck-typed §6 session used by resolver/tool/agent-loop tests."""

    def __init__(
        self,
        marks: dict[str, MarkCandidate] | None = None,
        *,
        width: int = 1080,
        height: int = 2400,
        current_app: str = "com.example.app",
        locate_result: MarkCandidate | None = None,
        locate_error: Exception | None = None,
        device_factory: FakeDeviceFactory | None = None,
        screenshot_b64: str = "QUJD",
        static_screen: bool = False,
    ) -> None:
        self.config = FakeConfig()
        self.device_factory = device_factory or FakeDeviceFactory()
        self.marks: dict[str, MarkCandidate] = dict(marks or {})
        self.screen_seq = 0
        self.screen_width = width
        self.screen_height = height
        self.current_app = current_app
        self.finished = False
        self.finish_summary: str | None = None
        self.takeover_reason: str | None = None
        self.launched_apps: list[str] = []
        self.finish_verifier: str = "skipped"
        # WP3: the finish verifier's audit record (approve/status/reason/
        # latency/usage), written by tools/control.py and persisted into
        # run.json by the runner. Stays None until a verdict exists.
        self.finish_verifier_verdict: dict | None = None
        # finish two-step review state (S2 §1.2): mirrors PhoneSession so the
        # control-tool tests exercise the real review/confirm seq guard.
        self.last_tool_ok: bool | None = None
        self.finish_reviewed: bool = False
        self.finish_review_seq: int = -1
        self.finish_dispute_count: int = 0
        self.finish_hard_doubts: list[str] = []
        self._locate_result = locate_result
        self._locate_error = locate_error
        self.observe_count = 0
        self._observe_should_fail = False
        # screenshot payload: a static screen keeps the same b64 across observes
        # (drives image-dedup tests); otherwise each observe bumps the payload.
        self._screenshot_b64 = screenshot_b64
        self._static_screen = static_screen

    def record_launched_app(self, package: str) -> None:
        self.launched_apps.append(package)

    # --- §6 surface -----------------------------------------------------
    def resolve_mark(self, mark_id: str) -> MarkCandidate:
        if mark_id not in self.marks:
            raise StaleMarkError(mark_id)
        return self.marks[mark_id]

    def mark_center_abs(self, mark: MarkCandidate) -> tuple[int, int]:
        cx, cy = mark.center
        return (
            int(cx / 1000 * self.screen_width),
            int(cy / 1000 * self.screen_height),
        )

    def relative_to_abs(self, rx: int, ry: int) -> tuple[int, int]:
        return (
            int(rx / 1000 * self.screen_width),
            int(ry / 1000 * self.screen_height),
        )

    def locate(
        self,
        description: str,
        *,
        visible_text_hint: str | None = None,
        intent: str | None = None,
        scope_mark_id: str | None = None,
        scope_start_mark_id: str | None = None,
        scope_end_mark_id: str | None = None,
    ) -> MarkCandidate:
        if self._locate_error is not None:
            raise self._locate_error
        if self._locate_result is None:
            raise LocateAmbiguousError(f"no candidate for {description!r}")
        self.marks[self._locate_result.mark_id] = self._locate_result
        return self._locate_result

    def observe(self) -> FakeObservation:
        self.observe_count += 1
        if self._observe_should_fail:
            raise RuntimeError("boom")
        self.screen_seq += 1
        # Static screen -> constant payload; dynamic -> per-observe payload.
        b64 = (
            self._screenshot_b64
            if self._static_screen
            else f"{self._screenshot_b64}{self.screen_seq}"
        )
        screen_hash = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:16]
        return FakeObservation(
            current_app=self.current_app,
            marks=self.marks,
            screen_seq=self.screen_seq,
            screenshot_b64=b64,
            width=self.screen_width,
            height=self.screen_height,
            screen_hash=screen_hash,
            mime_type="image/png",
        )


__all__ = ["FakeObservation", "FakePhoneSession"]
