"""Safe failure handling for ADB commands that may have reached a device."""

from __future__ import annotations

import subprocess
from typing import Literal


_PERMISSION_FAILURE_MARKERS = (
    "inject_events",
    "inject events",
    "permission denial",
    "permission denied",
    "securityexception",
)

KeyboardPreparationStage = Literal["query", "switch", "warmup"]
KeyboardCleanupState = Literal["not_needed", "restored", "failed", "unknown"]


class KeyboardPreparationError(RuntimeError):
    """Expose only the preparation stage and known keyboard cleanup state."""

    def __init__(
        self, stage: KeyboardPreparationStage, keyboard_cleanup: KeyboardCleanupState
    ) -> None:
        self.stage = stage
        self.keyboard_cleanup = keyboard_cleanup
        super().__init__(
            f"ADB keyboard preparation failed before text dispatch at {stage}; "
            f"keyboard cleanup {keyboard_cleanup}"
        )


def adb_command_failed(result: subprocess.CompletedProcess) -> bool:
    """Return whether an ADB result is a known failure.

    Some Android commands report permission failures in their output while adb
    itself exits zero, so the return code alone is insufficient.
    """

    output_parts: list[str] = []
    for stream in (result.stdout, result.stderr):
        if isinstance(stream, bytes):
            output_parts.append(stream.decode("utf-8", errors="ignore"))
        elif isinstance(stream, str):
            output_parts.append(stream)
    output = "\n".join(output_parts).casefold()
    return result.returncode != 0 or any(
        marker in output for marker in _PERMISSION_FAILURE_MARKERS
    )


def require_adb_success(
    result: subprocess.CompletedProcess,
    operation: str,
    *,
    outcome_unknown: bool = True,
) -> None:
    """Raise a safe error without leaking command arguments or device output."""

    if not adb_command_failed(result):
        return
    suffix = "device outcome unknown" if outcome_unknown else "failed before text dispatch"
    raise RuntimeError(f"ADB {operation} failed; {suffix}")


__all__ = [
    "KeyboardPreparationError",
    "adb_command_failed",
    "require_adb_success",
]
