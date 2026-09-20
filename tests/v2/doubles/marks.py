"""Mark/screen doubles: synthetic screenshots, foreground apps, UiAutomator dumps.

The XML constants are the drift-prone part. Three shapes exist on purpose and
must **not** be collapsed into one:

``SETTINGS_XML``
    Two clickable nodes (WLAN + 蓝牙) on a 1080-wide screen — the screen the
    archive/lifecycle tests dump.
``SETTINGS_XML_SINGLE``
    One clickable node. The retry/failure-code tests assert ``len(obs.marks) == 1``
    / ``len(sample.marks) == 1``, so adding a second node would change what they
    measure. Same 1080 width as above.
``SETTINGS_XML_NARROW``
    One node on a **1000**-wide screen: ``test_contract_repair_s2`` pins the
    screenshot-vs-dump geometry mismatch, so the width is the point.
"""

from __future__ import annotations

from phone_agent.grounding.provider import MarkCandidate

SETTINGS_XML = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" />'
    '<node text="蓝牙" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,300][1080,500]" />'
    "</hierarchy>"
)

SETTINGS_XML_SINGLE = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" />'
    "</hierarchy>"
)

SETTINGS_XML_NARROW = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1000,300]" />'
    "</hierarchy>"
)

DEFAULT_FOREGROUND = "com.example.app/.Main"


class FakeShot:
    """Duck-typed ``Screenshot``: valid payload, or an invalid failure code."""

    def __init__(
        self,
        payload: str = "shot",
        *,
        valid: bool = True,
        mime_type: str = "image/png",
        width: int = 1080,
        height: int = 2400,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        self.base64_data = payload
        self.width = width
        self.height = height
        self.mime_type = mime_type
        self.is_valid = valid
        if failure_code is not None:
            self.failure_code = failure_code
        else:
            self.failure_code = None if valid else "screenshot_unavailable"
        self.failure_message = failure_message


class FakeForeground:
    """Duck-typed ``ForegroundAppObservation``; package defaults to the component."""

    def __init__(self, component: str = DEFAULT_FOREGROUND, *, package: str | None = None) -> None:
        self.component_name = component
        self.package_name = package if package is not None else component.split("/", 1)[0]
        self.display_name = self.package_name


def make_mark(
    mark_id: str,
    *,
    text: str | None = None,
    role: str | None = None,
    center: tuple[int, int] = (500, 300),
) -> MarkCandidate:
    """A ``MarkCandidate`` centred in relative (0-1000) coordinates."""

    return MarkCandidate(
        mark_id=mark_id,
        bbox=[center[0] - 20, center[1] - 20, center[0] + 20, center[1] + 20],
        center=[center[0], center[1]],
        role=role,
        text_summary=text,
        source="fake",
    )


__all__ = [
    "SETTINGS_XML",
    "SETTINGS_XML_SINGLE",
    "SETTINGS_XML_NARROW",
    "DEFAULT_FOREGROUND",
    "FakeShot",
    "FakeForeground",
    "make_mark",
]
