"""WP2: ``observe()`` backs off before re-sampling an unstable marks dump.

The dump is the fragile half of the atomic window: ``uiautomator`` waits out a
post-navigation busy period internally, so a first-round failure is usually
transient. The retry therefore (a) sleeps ``observe_retry_backoff_s`` first and
(b) re-captures the *whole* window — screenshot included — so the committed
frame stays internally consistent (P0 #15). ``observe_retry_max_loops`` bounds
the extra rounds and still commits an annotated zero-mark observation when the
dump keeps failing (the screenshot itself was valid).
"""

from __future__ import annotations

import pytest

from phone_agent.v2.session import PhoneSession
from tests.v2.test_observation_failure_codes import (
    _SETTINGS_XML,
    FakeConfig,
    ScriptedDevice,
)
from tests.v2.test_observation_lifecycle import FakeDeviceFactory


def _session(dump_script, **config_overrides) -> PhoneSession:
    config = FakeConfig()
    config.observe_settle_ms = 0
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return PhoneSession(config, device_factory=ScriptedDevice(dump_script))


@pytest.fixture
def recorded_sleeps(monkeypatch) -> list[float]:
    """Capture every ``time.sleep`` observe() performs (no real waiting)."""

    sleeps: list[float] = []
    monkeypatch.setattr("phone_agent.v2.session.time.sleep", sleeps.append)
    return sleeps


def test_transient_dump_failure_backs_off_then_recollects_the_whole_round(
    recorded_sleeps,
):
    # round 1: dump times out (transient); round 2: a settled screen.
    session = _session(["timeout", _SETTINGS_XML], observe_retry_backoff_s=2.0)

    obs = session.observe()

    assert recorded_sleeps == [2.0]
    assert obs.epoch == 1
    assert len(obs.marks) == 1
    assert obs.marks_failure_code is None
    # The retry re-sampled the entire atomic window: a second screenshot AND a
    # second dump — never a marks-only re-read.
    assert session.device_factory.screenshot_calls == 2
    assert session.device_factory.dump_calls == 2


def test_second_round_screenshot_is_the_committed_frame(recorded_sleeps):
    session = _session(["timeout", _SETTINGS_XML])

    obs = session.observe()

    # ``shot<N>`` comes from the ScriptedDevice's nth capture: the committed
    # screenshot is round 2's, matching the marks taken against it.
    assert obs.screenshot_b64 == "shot2"


def test_zero_max_loops_commits_annotated_without_retrying(recorded_sleeps):
    session = _session(["timeout"], observe_retry_max_loops=0)

    obs = session.observe()

    assert recorded_sleeps == []
    assert session.device_factory.dump_calls == 1
    assert session.device_factory.screenshot_calls == 1
    # A valid screenshot with a failed dump still commits, annotated (B2).
    assert obs.marks_failure_code == "timeout"
    assert obs.epoch == 1


def test_extra_loops_are_honoured(recorded_sleeps):
    # Two transient failures, then success: needs 2 extra rounds, not just 1.
    session = _session(
        ["timeout", "boom", _SETTINGS_XML],
        observe_retry_max_loops=2,
        observe_retry_backoff_s=0.5,
    )

    obs = session.observe()

    assert recorded_sleeps == [0.5, 0.5]
    assert session.device_factory.dump_calls == 3
    assert session.device_factory.screenshot_calls == 3
    assert len(obs.marks) == 1


def test_persistent_failure_commits_on_the_last_round(recorded_sleeps):
    session = _session(
        ["timeout"], observe_retry_max_loops=2, observe_retry_backoff_s=0.25
    )

    obs = session.observe()

    # Retries are bounded and the outcome is the same annotated commit, not a
    # batch-invalidating observation failure (the screenshot was valid).
    assert recorded_sleeps == [0.25, 0.25]
    assert session.device_factory.dump_calls == 3
    assert obs.marks_failure_code == "timeout"
    assert obs.epoch == 1


def test_zero_backoff_skips_the_sleep_but_still_retries(recorded_sleeps):
    session = _session(["timeout", _SETTINGS_XML], observe_retry_backoff_s=0.0)

    session.observe()

    assert recorded_sleeps == []
    assert session.device_factory.dump_calls == 2


def test_empty_screen_is_not_retried(recorded_sleeps):
    """``accessibility_dump_empty`` is a legitimate screen, not instability."""

    session = _session([""], observe_retry_backoff_s=2.0)

    obs = session.observe()

    assert recorded_sleeps == []
    assert session.device_factory.dump_calls == 1
    # The code is surfaced for diagnostics but is not a transient dump fault.
    assert obs.marks_failure_code == "accessibility_dump_empty"


def test_foreground_change_retry_does_not_back_off(recorded_sleeps):
    """The backoff is for dump transients; a mid-capture move retries at once."""

    config = FakeConfig()
    config.observe_settle_ms = 0
    config.observe_retry_backoff_s = 2.0
    session = PhoneSession(
        config,
        device_factory=FakeDeviceFactory(
            foreground=[
                "com.example.app/.First",
                "com.example.app/.Second",
                "com.example.app/.Second",
                "com.example.app/.Second",
            ]
        ),
    )

    session.observe()

    assert recorded_sleeps == []
    assert session.device_factory.dump_calls == 2
