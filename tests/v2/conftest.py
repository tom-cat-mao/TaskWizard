"""v2 test defaults: a test never pays the production observation waits.

``PhoneSession.observe`` settles for ``observe_settle_ms`` (300ms) and backs off
``observe_retry_backoff_s`` (2s) before re-sampling an unstable marks dump. Both
are wall-clock cost a test must not inherit, so the shared
:class:`~tests.v2.doubles.config.FakeConfig` defaults them to zero and this
fixture re-pins the class defaults for every test in the tree — a future edit
that restores a production value cannot silently slow the suite down.

The values themselves are under test (``test_observation_hardening.py``,
``test_observe_retry_backoff.py``): those tests set them explicitly on the
instance, which the fixture does not touch.
"""

from __future__ import annotations

import pytest

from tests.v2.doubles.config import FakeConfig


@pytest.fixture(autouse=True)
def no_production_timing(monkeypatch):
    monkeypatch.setattr(FakeConfig, "observe_settle_ms", 0)
    monkeypatch.setattr(FakeConfig, "observe_retry_backoff_s", 0.0)
