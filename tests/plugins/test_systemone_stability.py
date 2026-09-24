"""``wait_for_stable`` contract: hash polling, bounds, honest failure text."""

from __future__ import annotations

from tests.plugins.systemone_harness import ScriptedPhoneSession


class FakeClock:
    """Deterministic ``monotonic``/``sleep`` pair so no test waits for real."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def build(stability_module, session, *, max_wait_s=6.0, interval_s=0.5, consecutive=2):
    clock = FakeClock()
    tool = stability_module.make_wait_for_stable_tool(
        session,
        max_wait_s=max_wait_s,
        interval_s=interval_s,
        consecutive=consecutive,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return tool, clock


def test_stable_when_the_hash_repeats_consecutively(stability_module):
    session = ScriptedPhoneSession(["h1", "h1", "h1"])
    tool, clock = build(stability_module, session, interval_s=0.1)

    text = tool.func(intent="等页面稳定")

    assert "stable=yes consecutive=2" in text
    assert "polls=2" in text
    assert "screen#2" in text
    assert stability_module.STALE_MARKS_NOTE in text
    assert clock.sleeps == [0.1]


def test_timeout_reports_no_stability_with_the_observed_streak(stability_module):
    session = ScriptedPhoneSession(["h1", "h2", "h3", "h4", "h5"])
    tool, _ = build(stability_module, session, max_wait_s=0.5, interval_s=0.2)

    text = tool.func()

    assert "stable=no reason=timeout" in text
    assert "consecutive=1/2" in text
    assert "polls=4" in text
    assert "elapsed_ms=500" in text
    assert stability_module.STALE_MARKS_NOTE in text


def test_consecutive_setting_defines_stability(stability_module):
    session = ScriptedPhoneSession(["h1", "h1", "h1"])
    tool, _ = build(stability_module, session, consecutive=3)
    assert "stable=yes consecutive=3" in tool.func()


def test_an_observation_failure_is_reported_and_never_faked(stability_module):
    session = ScriptedPhoneSession(["h1", "h1"], fail_at=2)
    tool, _ = build(stability_module, session)

    text = tool.func()

    assert "stable=no reason=observe_failed" in text
    assert "RuntimeError" in text
    assert "polls=1" in text


def test_a_session_without_a_screen_hash_says_so(stability_module):
    session = ScriptedPhoneSession([""])
    tool, _ = build(stability_module, session)

    text = tool.func()

    assert "stable=no reason=no_screen_hash" in text
    assert "无法判断" in text


def test_per_call_wait_budget_is_clamped_to_the_documented_band(stability_module):
    session = ScriptedPhoneSession([f"h{i}" for i in range(50)])  # never repeats
    tool, _ = build(stability_module, session, max_wait_s=6.0, interval_s=0.05)

    text = tool.func(max_wait_s=0.01)  # below the band -> clamped to 0.5s

    assert "reason=timeout" in text
    assert "elapsed_ms=500" in text


def test_settle_ms_is_forwarded_to_the_session(stability_module):
    session = ScriptedPhoneSession(["h1", "h1"])
    seen: list[object] = []
    original = session.observe

    def observe(settle_ms=None):
        seen.append(settle_ms)
        return original(settle_ms=settle_ms)

    session.observe = observe  # type: ignore[method-assign]
    tool, _ = build(stability_module, session)
    tool.func(settle_ms=1234)
    assert seen == [1234, 1234]


def test_the_tool_is_named_for_the_model_and_takes_intent(stability_module):
    tool, _ = build(stability_module, ScriptedPhoneSession(["h1"]))
    assert tool.name == "wait_for_stable"
    assert "intent" in tool.description
