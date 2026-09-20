"""Layout gate: a v2 test file is named after what it tests, not after a batch.

Every file under ``tests/v2/`` is named ``test_<subject>.py``, where the subject
is the behaviour or module under test.  A file named after the work package or
incident that produced it (``..._wp3.py``, ``..._s2.py``, ``..._g2c.py``) tells a
later reader nothing about what breaks when it fails — the batch code is
forgotten long before the code it pinned — so it is refused here.

Batches 1-5 of the test-doubles/domain-layout refactor retired sixteen such
names (``_wp1``, ``_g2c``, ``_s2``, ``_s3_s4``, ``_e1``, ``_e4``, ``_wf1`` …
``_wf4c``, ``_wp_r2``, ``_a``).  When a new batch code is invented, add its
letters to the pattern below rather than spending them on a filename.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/v2/harness/test_suite_layout.py -> parents[1] is tests/v2.
V2_ROOT = Path(__file__).resolve().parents[1]

# Batch/incident codes as they appeared: wp/wf (work packages), s/g (slices and
# gates), e (eventing steps), p/r (repair packages).  The trailing digits are
# what makes the token a code rather than a word.
_BATCH_CODE_RE = re.compile(r"_(?:wp|wf|s|g|e|p|r)\d+")


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in V2_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


def test_v2_test_filenames_carry_no_batch_code():
    offenders = []
    for path in _test_files():
        match = _BATCH_CODE_RE.search(path.stem)
        if match:
            offenders.append(f"{path.relative_to(V2_ROOT)}（{match.group(0)}）")

    assert not offenders, (
        "v2 测试文件名不得带批次/事故编号——按被测对象命名（例："
        "test_marks_ime_collapse.py 而不是 test_marks_ime_collapse_wp1.py）："
        + "、".join(offenders)
    )
