"""The committed-observation event carries a screen content hash (P0 #6 / #15).

Downstream capabilities need "did the picture materially change?" without
touching pixels: the ``observe`` event payload therefore carries
``screen_hash``, the short sha256 of the committed frame's screenshot payload
that the atomic observation already computes for screen binding.  These tests
pin that the hash is present and equals the observation's own digest, that it is
stable for identical input and changes with the frame, and that the event stays
redaction-safe — a digest, never an image and never raw screenshot bytes.
"""

from __future__ import annotations

import json
import string

from phone_agent.v2.middleware._redact import redact_text
from phone_agent.v2.session import PhoneSession
from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.device import FakeDeviceFactory
from tests.v2.doubles.marks import SETTINGS_XML


def _session(**kwargs) -> PhoneSession:
    return PhoneSession(
        FakeConfig(),
        device_factory=FakeDeviceFactory(observing=True, xml=SETTINGS_XML, **kwargs),
    )


def _capture(session: PhoneSession) -> list[dict]:
    """Subscribe to ``observe`` through the same bus the session emits on."""

    from phone_agent.v2.events import EventBus

    bus = EventBus()
    payloads: list[dict] = []
    bus.on("observe", payloads.append)
    session.event_bus = bus
    return payloads


def test_observe_event_carries_the_observations_screen_hash():
    session = _session(shots=["frame-a"])
    payloads = _capture(session)

    observation = session.observe()

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["screen_hash"] == observation.screen_hash
    assert payload["screen_hash"]
    assert (
        payload["epoch"],
        payload["screen_seq"],
        payload["marks_count"],
        payload["marks_failure_code"],
    ) == (
        observation.epoch,
        observation.screen_seq,
        len(observation.marks),
        observation.marks_failure_code,
    )


def test_screen_hash_is_stable_for_identical_input_and_follows_the_frame():
    session = _session(shots=["identical", "identical", "different"])
    payloads = _capture(session)

    session.observe()
    session.observe()
    session.observe()

    first, second, third = (payload["screen_hash"] for payload in payloads)
    assert first == second
    assert third != first


def test_screen_hash_is_a_digest_not_a_screenshot_payload():
    session = _session(shots=["RAW-SCREENSHOT-BYTES"])
    payloads = _capture(session)

    session.observe()

    digest = payloads[0]["screen_hash"]
    assert len(digest) == 16
    assert set(digest) <= set(string.hexdigits)
    # The event is a summary: no image, no base64, no frame payload.
    serialized = json.dumps(payloads[0], ensure_ascii=False)
    assert "RAW-SCREENSHOT-BYTES" not in serialized
    assert "base64" not in serialized
    assert set(payloads[0]) == {
        "epoch",
        "screen_seq",
        "marks_count",
        "marks_failure_code",
        "screen_hash",
    }


def test_screen_hash_survives_trace_redaction_unchanged():
    session = _session(shots=["frame-a"])
    payloads = _capture(session)

    session.observe()

    digest = payloads[0]["screen_hash"]
    assert redact_text(digest) == digest
    assert redact_text(f"screen {digest} unchanged") == f"screen {digest} unchanged"
