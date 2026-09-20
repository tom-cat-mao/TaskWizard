"""Deprecated import path — re-export shim only. **Delete in batch 5.**

The doubles now live in :mod:`tests.v2.doubles` (one module per family). This
module stays so the existing ``from tests.v2._doubles import …`` lines keep
working while the migration lands incrementally; new code imports from
``tests.v2.doubles.<family>`` instead.
"""

from __future__ import annotations

from tests.v2.doubles.config import FakeConfig
from tests.v2.doubles.device import FakeDeviceFactory
from tests.v2.doubles.marks import make_mark
from tests.v2.doubles.session import FakeObservation, FakePhoneSession

__all__ = [
    "FakeConfig",
    "FakeDeviceFactory",
    "FakeObservation",
    "FakePhoneSession",
    "make_mark",
]
