"""WP-WF4-A: multi-source ``app/launched`` (scheme C foreground-change path).

Scope: the session tracks the current foreground package at every committed
observation; a change to a package not yet announced this run emits
``app/launched`` with ``source="foreground"``, sharing the per-run dedupe set
with the device-confirmed ``launch_app`` path (``source="launch_app"``).
System packages (shell, permission/installer dialogs, launcher family) and the
additive config blocklist never announce from the foreground path. Downstream
consumers (``ProcedureCardInjector.on_app_launched``) are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

from phone_agent.v2.events import APP_LAUNCHED, EventBus
from phone_agent.v2.session import (
    FOREGROUND_EVENT_SYSTEM_PACKAGES,
    PhoneSession,
)
from phone_agent.v2.tools.actuation import build_actuation_tools

BILI = "tv.danmaku.bili"
WECHAT = "com.tencent.mm"
FOOD = "com.example.food"
BLOCKED = "com.example.blocked"


# --------------------------------------------------------------------------
# Fakes: screenshot, foreground observation, scriptable device, config.
# --------------------------------------------------------------------------
class FakeShot:
    def __init__(self, payload: str) -> None:
        self.base64_data = payload
        self.width = 1080
        self.height = 2400
        self.mime_type = "image/png"
        self.is_valid = True
        self.failure_code = None


class FakeForeground:
    def __init__(self, package: str, component: str | None = None) -> None:
        self.package_name = package
        self.component_name = component or f"{package}/.Main"
        self.display_name = package


class FakeScreenDevice:
    """Screenshot + dump + mutable foreground; no real device I/O.

    ``foreground`` is a package name the test mutates between observes; both
    atomic-window samples read the same value, so every observe is stable.
    """

    def __init__(self, foreground: str | None = BILI) -> None:
        self.foreground = foreground
        self._shots = 0

    def get_screenshot(self, device_id=None, black_screen_detect=True):
        self._shots += 1
        return FakeShot(f"shot-{self._shots}")

    def dump_uiautomator_xml(self, device_id=None, timeout=None, windowed=None):
        return "<hierarchy></hierarchy>"

    def get_foreground_app(self, device_id=None):
        if self.foreground is None:
            return None  # device cannot report the foreground
        return FakeForeground(self.foreground)


class FakeLaunchDevice(FakeScreenDevice):
    """Adds launch_app + installed inventory to the screen fake."""

    def __init__(
        self,
        foreground: str | None = BILI,
        installed: frozenset = frozenset({WECHAT}),
    ) -> None:
        super().__init__(foreground)
        self._installed = installed
        self.launched: list[str] = []

    def get_installed_app_inventory(self, device_id=None):
        from phone_agent.config.app_registry import InstalledAppInventory

        return InstalledAppInventory(self._installed, device_id=device_id)

    def launch_app(self, app_name, device_id=None, delay=None, **kwargs):
        self.launched.append(app_name)
        return True


def _fake_config(**overrides) -> SimpleNamespace:
    fields = dict(
        device_id="serial-1",
        app_kb_enabled=True,
        resolver_embed=False,
        observe_settle_ms=0,
        black_screen_detect=False,
        marks_windowed="off",
        foreground_event_blocked_packages=(),
        accessibility_max_marks=80,
        accessibility_timeout=3.0,
        grounding_provider="accessibility",
        locate_max_size=0,
        scope_padding_ratio=0.05,
        locateanything_max_size=960,
        locateanything_context_max_chars=200,
        locateanything_model=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _session(
    foreground: str | None = BILI, config: SimpleNamespace | None = None
) -> tuple[PhoneSession, EventBus, list[dict]]:
    bus = EventBus()
    seen: list[dict] = []
    bus.on(APP_LAUNCHED, seen.append)
    session = PhoneSession(
        config or _fake_config(), device_factory=FakeScreenDevice(foreground)
    )
    session.event_bus = bus
    return session, bus, seen


# --------------------------------------------------------------------------
# Foreground-change path.
# --------------------------------------------------------------------------
def test_first_observe_announces_initial_foreground_package():
    """A run starting with the target app already in the foreground announces
    it on the first committed observation (the WF4-A motivating scenario)."""

    session, _bus, seen = _session(foreground=BILI)
    session.observe()

    assert seen == [
        {"package": BILI, "device_id": "serial-1", "source": "foreground"}
    ]


def test_foreground_change_announces_once_per_package():
    session, _bus, seen = _session(foreground=BILI)
    session.observe()  # BILI announced
    session.device_factory.foreground = WECHAT
    session.observe()  # change -> announced
    session.observe()  # same foreground -> silent

    assert [(p["package"], p["source"]) for p in seen] == [
        (BILI, "foreground"),
        (WECHAT, "foreground"),
    ]


def test_unreadable_foreground_announces_nothing_then_recovers():
    session, _bus, seen = _session(foreground=None)
    session.observe()  # unreadable -> no announcement, tracker -> None
    session.device_factory.foreground = BILI
    session.observe()  # next readable package announces

    assert seen == [
        {"package": BILI, "device_id": "serial-1", "source": "foreground"}
    ]


def test_reset_launched_events_reannounces_current_foreground():
    session, _bus, seen = _session(foreground=BILI)
    session.observe()
    session.reset_launched_events()
    session.observe()

    assert [(p["package"], p["source"]) for p in seen] == [
        (BILI, "foreground"),
        (BILI, "foreground"),
    ]


# --------------------------------------------------------------------------
# System-package filter.
# --------------------------------------------------------------------------
def test_system_packages_never_announce_from_foreground():
    session, _bus, seen = _session(foreground=BILI)
    session.observe()  # baseline announced

    factory = session.device_factory
    for package in (
        "com.android.systemui",
        "android",
        "com.android.launcher",
        "com.android.launcher3",
        "com.google.android.apps.nexuslauncher",
        "com.android.permissioncontroller",
        "com.google.android.packageinstaller",
    ):
        factory.foreground = package
        session.observe()

    assert [p["package"] for p in seen] == [BILI]
    assert FOREGROUND_EVENT_SYSTEM_PACKAGES  # the constant ships non-empty


def test_system_package_does_not_shadow_a_later_real_app():
    session, _bus, seen = _session(foreground="com.android.systemui")
    session.observe()
    session.device_factory.foreground = BILI
    session.observe()

    assert [p["package"] for p in seen] == [BILI]


def test_config_blocklist_filters_foreground_source_only():
    session, _bus, seen = _session(
        foreground=BILI,
        config=_fake_config(foreground_event_blocked_packages=(BLOCKED,)),
    )
    session.observe()  # BILI announced
    session.device_factory.foreground = BLOCKED
    session.observe()  # blocked -> silent (tracker still advances)
    session.observe()  # no re-check churn
    assert [p["package"] for p in seen] == [BILI]

    # The launch path is never filtered: the same package can still be
    # announced by an explicit device-confirmed launch.
    assert session.emit_app_launched(BLOCKED) is True
    assert seen[-1]["package"] == BLOCKED


# --------------------------------------------------------------------------
# launch_app path + shared dedupe.
# --------------------------------------------------------------------------
def _launch_session():
    bus = EventBus()
    seen: list[dict] = []
    bus.on(APP_LAUNCHED, seen.append)
    device = FakeLaunchDevice(foreground=WECHAT)
    config = _fake_config()
    session = PhoneSession(config, device_factory=device)
    session.event_bus = bus
    launch = {tool.name: tool for tool in build_actuation_tools(session, config)}[
        "launch_app"
    ]
    return session, launch, seen


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(
        block.get("text", "")
        for block in result
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_launch_app_emits_launch_app_source():
    session, launch, seen = _launch_session()

    result = launch.invoke({"app_name": WECHAT})

    assert _text(result).startswith(f"OK. launched {WECHAT} ({WECHAT})")
    assert seen == [
        {"package": WECHAT, "device_id": "serial-1", "source": "launch_app"}
    ]


def test_launch_and_foreground_sources_share_one_dedupe():
    """launch_app announces the package; the post-launch observation (and any
    later observe) sees the same foreground package and stays silent; the next
    real foreground change announces with source="foreground"."""

    session, launch, seen = _launch_session()

    launch.invoke({"app_name": WECHAT})
    session.observe()  # foreground == WECHAT, already announced -> silent
    session.device_factory.foreground = BILI
    session.observe()

    assert [(p["package"], p["source"]) for p in seen] == [
        (WECHAT, "launch_app"),
        (BILI, "foreground"),
    ]
    # Switching back to the launched app stays silent (once per package per
    # run across both sources).
    session.device_factory.foreground = WECHAT
    session.observe()
    assert len(seen) == 2


def test_emit_app_launched_source_lands_in_payload():
    bus = EventBus()
    seen: list[dict] = []
    bus.on(APP_LAUNCHED, seen.append)
    session = PhoneSession.__new__(PhoneSession)
    session.event_bus = bus
    session.config = SimpleNamespace(device_id="serial-1")
    session._launched_this_run = set()

    assert session.emit_app_launched(FOOD, source="foreground") is True
    assert session.emit_app_launched(BILI) is True

    assert [p["source"] for p in seen] == ["foreground", "launch_app"]
    assert all(p["device_id"] == "serial-1" for p in seen)


def test_package_of_falls_back_to_component_prefix():
    assert PhoneSession._package_of(None) is None
    assert PhoneSession._package_of(FakeForeground("com.foo")) == "com.foo"
    component_only = SimpleNamespace(
        package_name="", component_name="com.foo/.Bar"
    )
    assert PhoneSession._package_of(component_only) == "com.foo"
