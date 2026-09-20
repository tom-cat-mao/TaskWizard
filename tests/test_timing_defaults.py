"""WP2: the shipped timing defaults and their env overrides.

Action delays are pure waiting between an ADB action and the next observation;
the shipped defaults are 0.3s (slow devices raise them through ``_DELAY`` env
keys). ``ActionTimingConfig`` holds the four text-input delays — currently read
by no consumer, so their defaults are inert and only pinned here to keep the
knob table honest.
"""

from __future__ import annotations

import pytest

from phone_agent.config.timing import (
    ActionTimingConfig,
    ConnectionTimingConfig,
    DeviceTimingConfig,
)

_DEVICE_DELAY_KEYS = (
    "PHONE_AGENT_TAP_DELAY",
    "PHONE_AGENT_DOUBLE_TAP_DELAY",
    "PHONE_AGENT_DOUBLE_TAP_INTERVAL",
    "PHONE_AGENT_LONG_PRESS_DELAY",
    "PHONE_AGENT_SWIPE_DELAY",
    "PHONE_AGENT_BACK_DELAY",
    "PHONE_AGENT_HOME_DELAY",
    "PHONE_AGENT_LAUNCH_DELAY",
    "PHONE_AGENT_ADB_RESTART_DELAY",
    "PHONE_AGENT_SERVER_RESTART_DELAY",
    "PHONE_AGENT_KEYBOARD_SWITCH_DELAY",
    "PHONE_AGENT_TEXT_CLEAR_DELAY",
    "PHONE_AGENT_TEXT_INPUT_DELAY",
    "PHONE_AGENT_KEYBOARD_RESTORE_DELAY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _DEVICE_DELAY_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_device_action_delays_default_to_0_3():
    device = DeviceTimingConfig()

    assert device.default_tap_delay == 0.3
    assert device.default_double_tap_delay == 0.3
    assert device.default_long_press_delay == 0.3
    assert device.default_swipe_delay == 0.3
    assert device.default_back_delay == 0.3
    assert device.default_home_delay == 0.3
    assert device.default_launch_delay == 0.3
    # The gap between the two taps of a double tap is not an action settle.
    assert device.double_tap_interval == 0.1


def test_text_input_delays_default_to_0_3():
    action = ActionTimingConfig()

    assert action.keyboard_switch_delay == 0.3
    assert action.text_clear_delay == 0.3
    assert action.text_input_delay == 0.3
    assert action.keyboard_restore_delay == 0.3


def test_connection_delays_keep_their_own_defaults():
    connection = ConnectionTimingConfig()

    assert connection.adb_restart_delay == 2.0
    assert connection.server_restart_delay == 1.0


@pytest.mark.parametrize(
    ("key", "attribute"),
    [
        ("PHONE_AGENT_TAP_DELAY", "default_tap_delay"),
        ("PHONE_AGENT_DOUBLE_TAP_DELAY", "default_double_tap_delay"),
        ("PHONE_AGENT_LONG_PRESS_DELAY", "default_long_press_delay"),
        ("PHONE_AGENT_SWIPE_DELAY", "default_swipe_delay"),
        ("PHONE_AGENT_BACK_DELAY", "default_back_delay"),
        ("PHONE_AGENT_HOME_DELAY", "default_home_delay"),
        ("PHONE_AGENT_LAUNCH_DELAY", "default_launch_delay"),
        ("PHONE_AGENT_DOUBLE_TAP_INTERVAL", "double_tap_interval"),
    ],
)
def test_device_env_overrides_beat_the_new_default(monkeypatch, key, attribute):
    monkeypatch.setenv(key, "1.5")

    assert getattr(DeviceTimingConfig(), attribute) == 1.5


@pytest.mark.parametrize(
    ("key", "attribute"),
    [
        ("PHONE_AGENT_KEYBOARD_SWITCH_DELAY", "keyboard_switch_delay"),
        ("PHONE_AGENT_TEXT_CLEAR_DELAY", "text_clear_delay"),
        ("PHONE_AGENT_TEXT_INPUT_DELAY", "text_input_delay"),
        ("PHONE_AGENT_KEYBOARD_RESTORE_DELAY", "keyboard_restore_delay"),
    ],
)
def test_text_input_env_overrides_beat_the_new_default(monkeypatch, key, attribute):
    monkeypatch.setenv(key, "1.0")

    assert getattr(ActionTimingConfig(), attribute) == 1.0
