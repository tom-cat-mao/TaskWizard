"""The v2 test-tree config double: every field defaulted, consumers override diffs.

The observation-layer tests drive the **real** ``PhoneSession`` against a fake
device, so this fake must carry every config attribute the session reads. Two of
those reads are wall-clock waits in production (``observe_settle_ms`` 300ms,
``observe_retry_backoff_s`` 2s); the defaults here are pinned to zero so no test
pays them, and ``tests/v2/conftest.py::no_production_timing`` re-pins them for
every test (the timing values themselves are under test in
``test_observation_hardening.py`` / ``test_observe_retry_backoff.py``, which set
them explicitly on the instance).

Class attributes (not dataclass fields) on purpose: the autouse fixture pins the
defaults by patching the class, and a subclass such as ``class Cfg(FakeConfig):
taskdoc_enabled = False`` keeps working. A real ``PhoneSession`` reads unknown
attributes through ``getattr(config, name, <production default>)``, so a field
listed here is only a default — never a behavior switch.
"""

from __future__ import annotations

from typing import Any


class FakeConfig:
    """Duck-typed ``V2Config`` for tests; override per instance via keywords."""

    # -- device / observation ------------------------------------------------
    device_id: str | None = None
    # Production waits, pinned to zero: see the module docstring.
    observe_settle_ms: int = 0
    observe_retry_max_loops: int = 1
    observe_retry_backoff_s: float = 0.0
    accessibility_max_marks: int = 80
    accessibility_timeout: float = 3.0
    grounding_provider: str = "accessibility"
    locateanything_max_size: int = 960
    locateanything_context_max_chars: int = 200
    locate_max_size: int = 0
    scope_padding_ratio: float = 0.05
    locateanything_model: str | None = None
    # Matches both the production default and the session's getattr fallback.
    marks_windowed: str = "auto"

    def __init__(self, **overrides: Any) -> None:
        for name, value in overrides.items():
            setattr(self, name, value)


__all__ = ["FakeConfig"]
