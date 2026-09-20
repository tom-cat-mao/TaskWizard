"""The device double: records calls, scripts screenshots/dumps/foreground.

One class covers both families that used to be copy-pasted per test file:

* the **action side** (``launch_app`` / ``tap`` / ``swipe`` / …), asserted through
  ``calls`` and ``launched`` — the tools layer runs against it without ADB;
* the **observation side** (``get_screenshot`` / ``dump_uiautomator_xml`` /
  ``get_foreground_app``), which drives the *real* ``PhoneSession`` so the
  atomic-observe window can be tested without a device.

Both sides are inert: no ADB, no MLX, no network, no sleeping. Scripted lists
(``shots`` / ``dumps``) advance one entry per call and repeat their last entry, so
a test can say "round 1 times out, round 2 settles".
"""

from __future__ import annotations

from collections.abc import Sequence

from tests.v2.doubles.marks import DEFAULT_FOREGROUND, SETTINGS_XML, FakeForeground, FakeShot


class FakeDeviceFactory:
    """``DeviceFactory`` double for tools tests and real-``PhoneSession`` tests.

    ``launch_result`` controls the bool ``launch_app`` returns (the tool must
    honor it — P0 #5). ``installed`` set to a frozenset enables the optional
    ``get_installed_app_inventory`` capability; left ``None`` the fake has no
    inventory (the tool then resolves without one, best-effort).

    The observation surface (``get_screenshot`` / ``dump_uiautomator_xml`` /
    ``get_foreground_app``) is **opt-in** via ``observing=True``. It is not a
    cosmetic switch: an action-side fake that suddenly answers foreground
    queries changes what a real ``PhoneSession`` reports (it starts emitting
    ``app/launched`` with ``source="foreground"``), so tests that never asked
    for a device surface must keep seeing "no such method".

    Observation keywords: ``xml`` is the static UiAutomator dump; ``shots`` and
    ``dumps`` are per-call scripts (``dumps`` entries: XML text, ``""`` for an
    empty screen, ``"timeout"``, ``"boom"`` or ``"windows_unsupported"``);
    ``foreground`` is a component name or a list consumed one call at a time.
    """

    def __init__(
        self,
        *,
        launch_result: bool = True,
        installed: frozenset | None = None,
        observing: bool = False,
        xml: str = SETTINGS_XML,
        screenshot_valid: bool = True,
        screenshot_prefix: str = "shot",
        shots: Sequence[str | FakeShot] | None = None,
        dumps: Sequence[str] | None = None,
        foreground: str | list[str] = DEFAULT_FOREGROUND,
    ) -> None:
        self.calls: list[tuple] = []
        self.launched: list[str] = []
        self.taps: list[tuple[int, int]] = []
        self.screenshot_calls = 0
        self.dump_calls = 0
        self.dump_windowed_args: list = []
        self._launch_result = launch_result
        self._installed = installed
        self._observing = observing
        self._xml = xml
        self._screenshot_valid = screenshot_valid
        self._screenshot_prefix = screenshot_prefix
        self._shots = (
            None
            if shots is None
            else [FakeShot(entry) if isinstance(entry, str) else entry for entry in shots]
        )
        self._shot_i = 0
        self._dumps = None if dumps is None else list(dumps)
        self._dump_i = 0
        self._foreground = foreground
        self._fg_i = 0

    # -- action side ------------------------------------------------------
    def launch_app(self, app_name, device_id=None, delay=None, **kwargs):
        self.calls.append(("launch_app", app_name))
        if self._launch_result:
            self.launched.append(app_name)
        return self._launch_result

    def get_installed_app_inventory(self, device_id=None):
        from phone_agent.config.app_registry import InstalledAppInventory

        if self._installed is None:
            raise RuntimeError("inventory unavailable on this fake")
        return InstalledAppInventory(self._installed, device_id=device_id)

    def tap(self, x, y, device_id=None, delay=None):
        self.taps.append((int(x), int(y)))
        self.calls.append(("tap", x, y))

    def long_press(self, x, y, duration_ms=3000, device_id=None, delay=None):
        self.calls.append(("long_press", x, y))

    def swipe(self, sx, sy, ex, ey, duration_ms=None, device_id=None, delay=None):
        self.calls.append(("swipe", sx, sy, ex, ey))

    def back(self, device_id=None, delay=None):
        self.calls.append(("back",))

    def home(self, device_id=None, delay=None):
        self.calls.append(("home",))

    def type_text(self, text, device_id=None):
        self.calls.append(("type_text", text))

    def detect_and_set_adb_keyboard(self, device_id=None) -> str:
        self.calls.append(("detect_kbd",))
        return "com.original/.IME"

    def restore_keyboard(self, ime, device_id=None):
        self.calls.append(("restore_kbd", ime))

    # -- observation side (``observing=True`` only) -----------------------
    def _require_observing(self, name: str) -> None:
        """Behave like a device without this capability when not configured."""

        if not self._observing:
            raise AttributeError(
                f"{type(self).__name__!s} has no attribute {name!r}; "
                "pass observing=True to drive a real PhoneSession"
            )

    def get_screenshot(self, device_id=None, timeout=10, black_screen_detect=None):
        self._require_observing("get_screenshot")
        self.screenshot_calls += 1
        if self._shots is not None:
            spec = self._shots[min(self._shot_i, len(self._shots) - 1)]
            self._shot_i += 1
            return spec
        return FakeShot(
            f"{self._screenshot_prefix}{self.screenshot_calls}", valid=self._screenshot_valid
        )

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        self._require_observing("dump_uiautomator_xml")
        self.dump_calls += 1
        self.dump_windowed_args.append(windowed)
        if self._dumps is None:
            return self._xml
        action = self._dumps[min(self._dump_i, len(self._dumps) - 1)]
        self._dump_i += 1
        if action == "timeout":
            raise TimeoutError("dump timed out")
        if action == "boom":
            raise RuntimeError("adb died")
        if action == "windows_unsupported":
            raise ValueError("UiAutomator --windows dump unavailable on this device")
        return action

    def get_foreground_app(self, device_id=None):
        self._require_observing("get_foreground_app")
        value = self._foreground
        if isinstance(value, list):
            component = value[min(self._fg_i, len(value) - 1)]
            self._fg_i += 1
        else:
            component = value
        return FakeForeground(component)


__all__ = ["FakeDeviceFactory"]
