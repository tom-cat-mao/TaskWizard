"""Capability-owned assembly for the v2 thin loop.

The registry remains the public status/audit plane, but its lifecycle hooks now
mount every optional capability through five declared seams.  The concrete
context records each registration under the currently applying ``cap_id`` so a
later release can remove the whole contribution without guessing object names.

Alongside the mount seams the context owns the **declaration plane** (P0 #18):
``register_tool(..., risk=...)`` records a tool's risk into the
``tool_risk_registry`` service the safety classifier reads,
``register_pin_prefix`` claims a pinned message-id prefix in
``pin_prefix_registry``, ``register_setting`` declares a configuration key whose
value resolves through the same CLI > env > .env > manifest > default chain as
the built-in ``V2Config`` fields, ``register_usage_role`` extends the accounting
roles the usage ledger and the experience schema validate against, and
``register_redaction`` declares a sensitive literal for the v2 egress redaction
boundary.  Declarations decide *how something is classified, charged or
redacted* — never whether it is allowed — and every declaration point fails
visibly (duplicate tool names, invalid modes, overwriting a harness-owned
service, undeclared pin prefixes, unprefixed setting env vars, unknown units,
too-short redaction literals).  ``CapabilitySpec.mode`` is the mount mode and is
limited to ``off`` / ``shadow`` / ``on``; a domain mode (``wary``, ``auto``,
``manual``, ...) is translated to ``on`` by ``_capability_mode``.

A capability's manifest entry may carry a ``[plugin.config]`` table; it travels
on ``CapabilitySpec.manifest_config`` and is readable inside ``apply`` through
:meth:`CapabilityAssemblyContext.plugin_config`, where it is also the tier that
replaces the declared defaults of that capability's settings.

This is intentionally a static assembly layer.  Reconciliation is useful while
building agents and in tests/consoles, but it does not mutate a compiled agent's
tool table while a run is in progress.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import re
import sys
from typing import Any, Protocol, runtime_checkable

from phone_agent.v2.events import (
    CAPABILITY_TOOLS_UNDECLARED,
    MODEL_POST_REQUEST,
    MODEL_PRE_REQUEST,
    MODEL_REQUEST,
    TOOL_EXECUTE,
)
from phone_agent.v2.pins import PinPrefixRegistry
from phone_agent.v2.redaction import REDACTION_REGISTRY, RedactionRegistry
from phone_agent.v2.settings import SettingRegistry
from phone_agent.v2.tool_risk import TOOL_RISKS, ToolRisk, declared_risk
from phone_agent.v2.usage_roles import USAGE_ROLE_REGISTRY, UsageRoleRegistry

CapabilityHook = Callable[..., None]
PromptProvider = Callable[..., Any]
RunHook = Callable[..., Any]
CliHandler = Callable[..., Any]
# Plugin API compatibility gate shared with the external plugin loader (Phase C).
# External capabilities declare ``REQUIRES_API`` and are refused at startup when
# it does not match this integer.  Bumped only on a breaking seam change.
PLUGIN_API_VERSION = 1
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# A ``provides`` service key follows the same grammar as a capability id.
_SERVICE_NAME = _CAPABILITY_ID
_RUN_HOOK_WHEN = frozenset({"start", "end"})
# The only legal capability mount modes (P0 #18).  Domain modes (``wary``,
# ``auto``, ``manual``, ...) are translated by ``_capability_mode`` — a
# non-empty value outside this set used to mean "active" silently, which let a
# typo mount a capability at full strength.
CAPABILITY_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})
# Owner label for harness-published products.  Deliberately outside the cap_id
# grammar (``^[a-z][a-z0-9_]*$``) so it can never collide with a capability.
HARNESS_OWNER = "__harness__"
_CORE_OWNER = "__core__"

# Middleware is now reserved for LangChain bridge middleware only; all policy
# behavior lives on the event bus.  Capabilities should not register middleware
# directly; the core harness owns the five bridges plus optional extra_middleware.
_RUN_HOOK_ORDER = {
    "start": {
        "taskdoc": 10,
        "app_kb": 20,
        "experience": 35,
        "recall": 40,
    },
    "end": {
        # The recall updater consumes the authoritative episode written by the
        # experience hook for this run, so persistence must land first.
        "experience": 40,
        "recall": 50,
        "dream": 90,
    },
}


@runtime_checkable
class CapabilityContext(Protocol):
    """Public assembly seams plus owner-scoped lifecycle registration."""

    def register_middleware(self, middleware: Any) -> None: ...

    def register_tool(self, tool: Any, *, risk: ToolRisk | None = None) -> None: ...

    def add_prompt_block(self, provider: PromptProvider) -> None: ...

    def add_run_hook(self, when: str, fn: RunHook) -> None: ...

    def add_cli_command(self, name: str, handler: CliHandler) -> None: ...

    def register_service(self, name: str, value: Any) -> None: ...

    def register_pin_prefix(self, prefix: str) -> None: ...

    def register_setting(
        self,
        key: str,
        *,
        env_var: str,
        default: Any,
        description: str,
    ) -> Any: ...

    def register_usage_role(self, role: str, *, unit: str = "tokens") -> None: ...

    def register_redaction(self, literal: str) -> None: ...

    def plugin_config(self) -> Mapping[str, Any]: ...

    def on(
        self, event: str, listener: Callable[..., Any], *, prepend: bool = False
    ) -> Callable[[], None]: ...

    def on_dispose(self, disposer: Callable[[], None]) -> None: ...


@dataclass(frozen=True)
class PromptBlock:
    """One provider result and where it belongs in the initial messages."""

    content: str
    placement: str = "system_message"

    def __post_init__(self) -> None:
        if self.placement not in {"system_suffix", "system_message"}:
            raise ValueError(f"invalid prompt block placement: {self.placement!r}")


class ToolRiskRegistry(Mapping[str, str]):
    """Assembly-time tool-risk declarations (P0 #18 declaration plane).

    Mapping of tool name -> declared risk (``actuation`` / ``readonly``).  Only
    *declared* names appear: a consumer that finds no entry must fail closed to
    ``actuation`` (the safety classifier does), so a silent registration cannot
    open the observation-only path.

    Entries stack by owner, exactly like the tool mounts they describe: a
    capability that replaces a same-named core tool supersedes its declaration
    and reveals the core one again on release.  Registration is assembly-time
    only — consumers get a read-only mapping; population happens through
    :meth:`CapabilityAssemblyContext.register_tool`.
    """

    def __init__(self) -> None:
        self._declared: dict[str, list[tuple[str, str | None]]] = {}

    def declare(self, name: str, risk: str | None, *, owner: str) -> None:
        """Record one declaration under ``owner`` (``None`` = no declaration)."""

        clean = str(name).strip()
        if not clean:
            raise ValueError("tool risk declaration requires a tool name")
        if risk is not None and risk not in TOOL_RISKS:
            raise ValueError(
                f"invalid tool risk: {risk!r} (expected actuation/readonly)"
            )
        entries = self._declared.setdefault(clean, [])
        if entries and entries[-1][0] == owner:
            entries[-1] = (owner, risk)
            return
        entries.append((owner, risk))

    def withdraw(self, owner: str) -> None:
        """Drop every entry declared by ``owner`` (release/replacement)."""

        for name in list(self._declared):
            entries = [
                entry for entry in self._declared[name] if entry[0] != owner
            ]
            if entries:
                self._declared[name] = entries
            else:
                self._declared.pop(name, None)

    def risk_for(self, name: str) -> str | None:
        """The active declaration for ``name`` (``None`` when undeclared)."""

        entries = self._declared.get(str(name))
        return entries[-1][1] if entries else None

    def as_dict(self) -> dict[str, str]:
        """Snapshot of the active declarations (for docs/tests/trace)."""

        return {
            name: risk
            for name in self._declared
            if (risk := self.risk_for(name)) is not None
        }

    def undeclared(self) -> dict[str, str]:
        """Registered tools whose active declaration is missing, by owner.

        These are the names that fail closed to ``actuation`` at classification
        time; assembly reports them so a plugin author sees the omission without
        reading a run trace.
        """

        return {
            name: entries[-1][0]
            for name, entries in self._declared.items()
            if entries[-1][1] is None
        }

    def __getitem__(self, name: str) -> str:
        risk = self.risk_for(name)
        if risk is None:
            raise KeyError(name)
        return risk

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True)
class CapabilitySpec:
    """Stable identity, configured mode, dependencies, and lifecycle hooks."""

    cap_id: str
    title: str
    mode: str
    deps: tuple[str, ...] = ()
    apply: CapabilityHook | None = None
    release: CapabilityHook | None = None
    provides: str | None = None
    # ``[plugin.config]`` of the manifest entry that loaded this capability (the
    # plugin loader attaches it; an entry without one leaves this empty, so an
    # in-code spec and its loaded form stay equal).  It is operator metadata,
    # not identity: excluded from equality/repr and read by the assembly layer
    # as the plugin's own values (``ctx.plugin_config()``) and as the override
    # tier beneath the environment for the settings the plugin declares.
    manifest_config: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if not _CAPABILITY_ID.fullmatch(self.cap_id):
            raise ValueError(f"invalid capability id: {self.cap_id!r}")
        if not str(self.title).strip():
            raise ValueError("capability title must not be empty")
        mode = str(self.mode).strip().lower()
        if mode not in CAPABILITY_MODES:
            raise ValueError(
                f"invalid capability mode: {self.mode!r} (expected one of "
                f"{', '.join(sorted(CAPABILITY_MODES))}); domain modes such as "
                "'wary', 'hard', 'auto' or 'manual' are config-side *_mode "
                "settings translated by _capability_mode — they are not legal "
                "CapabilitySpec.mode values"
            )
        for dependency in self.deps:
            if not _CAPABILITY_ID.fullmatch(dependency):
                raise ValueError(f"invalid dependency id: {dependency!r}")
        if self.provides is not None and not _SERVICE_NAME.fullmatch(self.provides):
            raise ValueError(f"invalid provides service key: {self.provides!r}")
        if not isinstance(self.manifest_config, Mapping):
            raise TypeError(
                "capability manifest_config must be a mapping of declared "
                f"[plugin.config] values, got {type(self.manifest_config).__name__}"
            )
        if self.apply is not None and not callable(self.apply):
            raise TypeError("capability apply hook must be callable")
        if self.release is not None and not callable(self.release):
            raise TypeError("capability release hook must be callable")


class CapabilityRegistry:
    """Insertion-ordered registry with fail-visible dependency status."""

    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec) -> None:
        """Register one stable id, rejecting accidental replacement."""

        if not isinstance(spec, CapabilitySpec):
            raise TypeError("spec must be a CapabilitySpec")
        if spec.cap_id in self._specs:
            raise ValueError(f"capability already registered: {spec.cap_id}")
        self._specs[spec.cap_id] = spec

    def specs(self) -> tuple[CapabilitySpec, ...]:
        """Return the immutable insertion-ordered composition."""

        return tuple(self._specs.values())

    def status(self) -> list[dict[str, Any]]:
        """Return deterministic status rows without applying effects.

        ``off`` takes precedence for the capability itself.  A non-off
        capability is ``pending`` when an immediate dependency is absent, off,
        or itself pending.  A ready ``shadow`` mode remains visible as shadow;
        every other ready non-off mode is active.
        """

        states: dict[str, str] = {}

        def resolve(cap_id: str, resolving: frozenset[str] = frozenset()) -> str:
            if cap_id in states:
                return states[cap_id]
            spec = self._specs.get(cap_id)
            if spec is None or cap_id in resolving:
                return "pending"
            mode = str(spec.mode).strip().lower()
            if mode == "off":
                states[cap_id] = "off"
                return "off"
            nested = resolving | {cap_id}
            missing = [
                dependency
                for dependency in spec.deps
                if resolve(dependency, nested) in {"off", "pending"}
            ]
            state = "pending" if missing else "shadow" if mode == "shadow" else "active"
            states[cap_id] = state
            return state

        rows: list[dict[str, Any]] = []
        for cap_id, spec in self._specs.items():
            state = resolve(cap_id)
            missing_deps = [
                dependency
                for dependency in spec.deps
                if resolve(dependency, frozenset({cap_id})) in {"off", "pending"}
            ]
            rows.append(
                {
                    "cap_id": cap_id,
                    "title": spec.title,
                    "mode": str(spec.mode).strip().lower(),
                    "state": state,
                    "missing_deps": missing_deps if state == "pending" else [],
                }
            )
        return rows


@dataclass(frozen=True)
class _Mount:
    owner: str
    value: Any
    sequence: int
    order: int
    replace_key: str | None = None


@dataclass(frozen=True)
class MiddlewareReplacement:
    """Mount ``middleware`` into an existing ordered core slot."""

    middleware: Any
    replace_key: str


class CapabilityAssemblyContext:
    """Capability-owned mount ledger for the five assembly seams.

    The context also owns the declaration-plane registries the harness and
    capabilities publish into: ``tool_risk_registry`` (P0 #18 tool risk, read by
    the safety listener), ``pin_prefix_registry`` (declared pinned message-id
    prefixes), ``setting_registry`` (declared configuration keys),
    ``usage_role_registry`` (declared accounting roles) and
    ``redaction_registry`` (declared egress redaction literals).  All start
    harness-owned, so a capability can read them but not silently replace them
    (see :meth:`register_service`).
    """

    def __init__(self, services: Mapping[str, Any] | None = None) -> None:
        self._services = dict(services or {})
        # Harness-published services (event_bus/config/session/factories plus
        # everything the harness set_service'd) are owner-marked so a capability
        # cannot quietly replace the bus, the session or the config.
        self._service_owners: dict[str, str] = {
            name: HARNESS_OWNER for name in self._services
        }
        self._current_cap_id: str | None = None
        self._sequence = 0
        self._middleware: list[_Mount] = []
        self._tools: list[_Mount] = []
        self._prompt_blocks: list[_Mount] = []
        self._run_hooks: dict[str, list[_Mount]] = {"start": [], "end": []}
        self._cli_commands: list[_Mount] = []
        self._mounted: dict[str, tuple[str, CapabilitySpec]] = {}
        self._capability_order: dict[str, int] = {}
        # Capability-owned services share the harness service namespace but are
        # tracked by owner so release removes them with zero residue and a
        # second mounted owner registering the same key fails visibly.
        self._disposers: dict[str, list[Callable[[], None]]] = {}
        # Tool ownership mirrors the mount ledger: the capability that owns a
        # tool name, plus the core names already mounted (fail-visible
        # duplicates inside each of the two planes).
        self._tool_owners: dict[str, str] = {}
        self._core_tool_names: set[str] = set()
        # Per-capability ``[plugin.config]`` values, published by
        # ``assemble_capabilities`` from the spec and dropped with the mount.
        self._manifest_configs: dict[str, Mapping[str, Any]] = {}
        for name, value in (
            ("tool_risk_registry", ToolRiskRegistry()),
            ("pin_prefix_registry", PinPrefixRegistry()),
            # The harness CLI tier for declared settings: an operator override
            # map (declared key -> value) a CLI entry point may publish before
            # assembly.  Absent means the tier is empty.
            (
                "setting_registry",
                SettingRegistry(overrides=self._services.get("setting_overrides")),
            ),
            ("usage_role_registry", USAGE_ROLE_REGISTRY),
            ("redaction_registry", REDACTION_REGISTRY),
        ):
            self._services.setdefault(name, value)
            self._service_owners.setdefault(name, HARNESS_OWNER)

    @property
    def current_cap_id(self) -> str | None:
        return self._current_cap_id

    def service(self, name: str, default: Any = None) -> Any:
        """Return a harness-supplied dependency without making it a seam."""

        return self._services.get(name, default)

    def _risk_registry(self) -> ToolRiskRegistry:
        registry = self._services.get("tool_risk_registry")
        if not isinstance(registry, ToolRiskRegistry):
            raise RuntimeError("tool_risk_registry service is unavailable")
        return registry

    def _pin_registry(self) -> PinPrefixRegistry:
        registry = self._services.get("pin_prefix_registry")
        if not isinstance(registry, PinPrefixRegistry):
            raise RuntimeError("pin_prefix_registry service is unavailable")
        return registry

    def _setting_registry(self) -> SettingRegistry:
        registry = self._services.get("setting_registry")
        if not isinstance(registry, SettingRegistry):
            raise RuntimeError("setting_registry service is unavailable")
        return registry

    def _usage_role_registry(self) -> UsageRoleRegistry:
        registry = self._services.get("usage_role_registry")
        if not isinstance(registry, UsageRoleRegistry):
            raise RuntimeError("usage_role_registry service is unavailable")
        return registry

    def _redaction_registry(self) -> RedactionRegistry:
        registry = self._services.get("redaction_registry")
        if not isinstance(registry, RedactionRegistry):
            raise RuntimeError("redaction_registry service is unavailable")
        return registry

    def _guard_service_write(self, name: str, owner: str) -> None:
        """Refuse a capability write to a service someone else owns (G6)."""

        existing = self._service_owners.get(name)
        if existing is None or existing == owner:
            return
        if existing == HARNESS_OWNER:
            raise ValueError(
                f"service {name!r} is harness-owned and cannot be replaced by "
                f"capability {owner!r}"
            )
        raise ValueError(
            f"service {name!r} already registered by capability {existing!r}"
        )

    def set_service(self, name: str, value: Any) -> None:
        """Publish a service as the harness (assembly-plane) owner.

        Called outside an apply hook this is the harness publishing (or
        replacing) its own services.  Called *inside* an apply hook it writes as
        the applying capability, so a plugin can publish its own services and
        overwrite its own — but not a harness-owned one (fail-visible).
        """

        clean = str(name).strip()
        if not _SERVICE_NAME.fullmatch(clean):
            raise ValueError(f"invalid service name: {name!r}")
        owner = self._current_cap_id
        if owner is not None:
            self._guard_service_write(clean, owner)
        else:
            owner = HARNESS_OWNER
        self._services[clean] = value
        self._service_owners[clean] = owner

    @contextmanager
    def applying(self, cap_id: str):
        previous = self._current_cap_id
        self._current_cap_id = cap_id
        try:
            yield
        finally:
            self._current_cap_id = previous

    def _owner(self) -> str:
        if self._current_cap_id is None:
            raise RuntimeError("capability registration requires an active cap_id")
        return self._current_cap_id

    def _mount(
        self,
        target: list[_Mount],
        value: Any,
        order: int,
        *,
        replace_key: str | None = None,
    ) -> None:
        self._sequence += 1
        target.append(_Mount(self._owner(), value, self._sequence, order, replace_key))

    def register_middleware(self, middleware: Any) -> None:
        """Register one bridge middleware owned by the applying capability.

        Policy code must use the event bus; this seam is reserved for LangChain
        bridge middleware.  The caller must supply an explicit order via
        :meth:`register_core_middleware` or accept the neutral default of 50.
        """

        replace_key = None
        if isinstance(middleware, MiddlewareReplacement):
            replace_key = middleware.replace_key
            middleware = middleware.middleware
        self._mount(
            self._middleware,
            middleware,
            50,
            replace_key=replace_key,
        )

    @staticmethod
    def _tool_name(tool: Any) -> str:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            raise ValueError("registered tool requires a non-empty name")
        return name

    def _record_tool(self, name: str, owner: str, risk: str | None) -> None:
        """Declare ``name``'s risk (``None`` = undeclared, fails closed)."""

        self._tool_owners[name] = owner
        self._risk_registry().declare(name, risk, owner=owner)

    def register_tool(self, tool: Any, *, risk: ToolRisk | None = None) -> None:
        """Mount one capability-owned tool and record its risk declaration.

        ``risk`` declares how the safety classifier must treat calls to the tool
        (``actuation`` = routed through the risk classifier, ``readonly`` =
        observation-only).  Omitting it falls back to the declaration the tool
        carries in ``metadata["risk"]`` (how the built-ins self-declare) and,
        failing that, declares nothing — which the classifier reads as
        ``actuation`` (fail-closed).  The declaration never grants permission and
        never bypasses a gate.

        Tool names must be unique among capability registrations: a second
        capability registering the same name raises (fail-visible) instead of
        silently replacing the first.  A same-named *core* tool is still
        replaced in place (the finish-verifier contract); the replacing
        declaration supersedes the core one until release restores it.
        """

        owner = self._owner()
        name = self._tool_name(tool)
        existing = self._tool_owners.get(name)
        if existing is not None and existing != _CORE_OWNER:
            where = "this capability" if existing == owner else f"capability {existing!r}"
            raise ValueError(f"tool {name!r} is already registered by {where}")
        self._mount(self._tools, tool, 100 + self._capability_order.get(owner, 0))
        self._record_tool(name, owner, risk if risk is not None else declared_risk(tool))

    def register_pin_prefix(self, prefix: str) -> None:
        """Declare one pinned message-id prefix owned by the applying capability.

        A pin is a message id that context hygiene must never summarise, fold or
        remove (see :mod:`phone_agent.v2.pins`); declaring the prefix is how a
        capability claims one.  The declaration is released with the capability,
        and minting an id under an undeclared prefix raises (fail-visible).
        """

        self._pin_registry().declare(str(prefix), owner=self._owner())

    def plugin_config(self) -> Mapping[str, Any]:
        """The manifest ``[plugin.config]`` table of the applying capability.

        Empty outside an apply hook or when the manifest entry declares none.
        Values act as the per-plugin override tier for the settings the
        capability declares through :meth:`register_setting` (below the
        environment, above the declared default) and are otherwise the plugin's
        own business: keys it never declares are simply readable here.
        """

        if self._current_cap_id is None:
            return {}
        return self._manifest_configs.get(self._current_cap_id, {})

    def register_setting(
        self,
        key: str,
        *,
        env_var: str,
        default: Any,
        description: str,
    ) -> Any:
        """Declare one configuration key owned by the applying capability.

        The declared key resolves through the built-in precedence chain —
        harness CLI override (``setting_overrides`` service) > shell env /
        ``.env`` > this plugin's ``[plugin.config]`` value > ``default`` — and
        the resolved value is returned *and* recorded on
        ``V2Config.plugin_settings`` (read-only) for the run.  ``env_var`` must
        carry the ``PHONE_AGENT_`` prefix because only that prefix is loaded
        from ``.env``; anything else raises at assembly time, as does a second
        capability declaring the same key.  A declaration never grants anything:
        it only tells the harness where a plugin's value comes from.
        """

        owner = self._owner()
        value = self._setting_registry().declare(
            key,
            env_var=env_var,
            default=default,
            description=description,
            owner=owner,
            plugin_config=self._manifest_configs.get(owner),
        )
        declare = getattr(self._services.get("config"), "declare_plugin_setting", None)
        if callable(declare):
            declare(str(key).strip(), value)
        return value

    def register_usage_role(self, role: str, *, unit: str = "tokens") -> None:
        """Declare one accounting role owned by the applying capability.

        ``unit="tokens"`` roles join the token budget exactly like the harness
        roles; ``unit="calls"`` roles are counted per invocation and reported
        separately, never adjudicated by the budget (P0 #13).  The declaration
        is released with the capability, and recording an undeclared role still
        raises at ``UsageLedger.record`` time (fail-visible).
        """

        self._usage_role_registry().declare(role, unit=unit, owner=self._owner())

    def register_redaction(self, literal: str) -> None:
        """Declare one sensitive literal for the v2 egress redaction boundary.

        The literal (not a regex) is replaced with ``<redacted>`` in every
        string that goes through the shared ``redact_text`` used by trace,
        diagnostic evidence, Web run events and the streaming preview.  It must
        be one line and at least four characters long — a shorter one would
        redact unrelated text — and it is released with the capability.
        Declarations change redaction only: the safety classifier and
        prompt-side sanitization keep the built-in patterns.
        """

        self._redaction_registry().declare(literal, owner=self._owner())

    def add_prompt_block(self, provider: PromptProvider) -> None:
        if not callable(provider):
            raise TypeError("prompt block provider must be callable")
        self._mount(
            self._prompt_blocks,
            provider,
            100 + self._capability_order.get(self._owner(), 0),
        )

    def add_run_hook(self, when: str, fn: RunHook) -> None:
        if when not in _RUN_HOOK_WHEN:
            raise ValueError(f"invalid run hook phase: {when!r}")
        if not callable(fn):
            raise TypeError("run hook must be callable")
        owner = self._owner()
        self._mount(
            self._run_hooks[when],
            fn,
            _RUN_HOOK_ORDER[when].get(owner, 50),
        )

    def add_cli_command(self, name: str, handler: CliHandler) -> None:
        clean = str(name).strip()
        if not clean or not callable(handler):
            raise ValueError("CLI command requires a name and callable handler")
        self._mount(
            self._cli_commands,
            (clean, handler),
            100 + self._capability_order.get(self._owner(), 0),
        )

    def register_service(self, name: str, value: Any) -> None:
        """Publish a capability-owned service into the shared namespace.

        The service is read back through :meth:`service` like any harness
        factory.  Ownership is recorded under the applying ``cap_id`` so
        :meth:`release_capability` removes it with zero residue.  Two mounted
        capabilities registering the same key is a fail-visible conflict, and a
        harness-owned key (``event_bus`` / ``session`` / ``config`` / any
        ``set_service`` product) cannot be replaced at all (G6).
        """

        owner = self._owner()
        clean = str(name).strip()
        if not _SERVICE_NAME.fullmatch(clean):
            raise ValueError(f"invalid service name: {name!r}")
        self._guard_service_write(clean, owner)
        self._services[clean] = value
        self._service_owners[clean] = owner

    def on(
        self, event: str, listener: Callable[..., Any], *, prepend: bool = False
    ) -> Callable[[], None]:
        """Subscribe on the harness event bus under the current owner."""

        self._owner()
        bus = self.service("event_bus")
        subscribe = getattr(bus, "on", None)
        if not callable(subscribe):
            raise RuntimeError("event_bus service is unavailable")
        disposer = subscribe(event, listener, prepend=prepend)
        self.on_dispose(disposer)
        return disposer

    def on_dispose(self, disposer: Callable[[], None]) -> None:
        """Register one cleanup callback under the current capability."""

        if not callable(disposer):
            raise TypeError("capability disposer must be callable")
        owner = self._owner()
        self._disposers.setdefault(owner, []).append(disposer)

    # Core harness products use the same ordered collections, but cannot be
    # released by a capability because their owner is outside the cap_id space.
    def register_core_middleware(
        self, middleware: Any, *, order: int, replace_key: str | None = None
    ) -> None:
        self._sequence += 1
        self._middleware.append(
            _Mount(_CORE_OWNER, middleware, self._sequence, order, replace_key)
        )

    def register_core_tool(
        self, tool: Any, *, order: int, risk: ToolRisk | None = None
    ) -> None:
        """Mount one harness tool and record its risk declaration.

        Same declaration semantics as :meth:`register_tool`, and the same
        fail-visible duplicate check *within* the core plane; a capability may
        still replace a core tool by name.  ``risk`` defaults to the declaration
        the tool carries (the built-ins stamp it at build time).
        """

        name = self._tool_name(tool)
        if name in self._core_tool_names:
            raise ValueError(f"core tool {name!r} is already registered")
        self._sequence += 1
        self._tools.append(_Mount(_CORE_OWNER, tool, self._sequence, order))
        self._core_tool_names.add(name)
        self._record_tool(
            name, _CORE_OWNER, risk if risk is not None else declared_risk(tool)
        )

    def add_core_run_hook(self, when: str, fn: RunHook, *, order: int) -> None:
        if when not in _RUN_HOOK_WHEN:
            raise ValueError(f"invalid run hook phase: {when!r}")
        self._sequence += 1
        self._run_hooks[when].append(_Mount("__core__", fn, self._sequence, order))

    @staticmethod
    def _ordered(mounts: Sequence[_Mount]) -> list[_Mount]:
        return sorted(mounts, key=lambda item: (item.order, item.sequence))

    @property
    def middleware(self) -> list[Any]:
        result: list[Any] = []
        positions: dict[str, int] = {}
        for mount in self._ordered(self._middleware):
            key = mount.replace_key
            if key is not None and key in positions:
                result[positions[key]] = mount.value
            else:
                if key is not None:
                    positions[key] = len(result)
                result.append(mount.value)
        return result

    @property
    def tools(self) -> list[Any]:
        # A capability may replace a same-named baseline tool (finish verifier).
        # Release removes the upper mount and reveals the untouched baseline at
        # its original position.
        result: list[Any] = []
        positions: dict[str, int] = {}
        for mount in self._ordered(self._tools):
            tool = mount.value
            name = str(getattr(tool, "name", ""))
            if name and name in positions:
                result[positions[name]] = tool
            else:
                if name:
                    positions[name] = len(result)
                result.append(tool)
        return result

    @property
    def prompt_providers(self) -> list[PromptProvider]:
        return [mount.value for mount in self._ordered(self._prompt_blocks)]

    def run_hooks(self, when: str) -> list[RunHook]:
        if when not in _RUN_HOOK_WHEN:
            raise ValueError(f"invalid run hook phase: {when!r}")
        return [mount.value for mount in self._ordered(self._run_hooks[when])]

    @property
    def cli_commands(self) -> dict[str, CliHandler]:
        commands: dict[str, CliHandler] = {}
        for mount in self._ordered(self._cli_commands):
            name, handler = mount.value
            commands[name] = handler
        return commands

    def owned_values(self, cap_id: str, seam: str) -> list[Any]:
        """Expose owned products for agent compatibility attributes/tests."""

        collections = {
            "middleware": self._middleware,
            "tool": self._tools,
            "prompt": self._prompt_blocks,
            "run_start": self._run_hooks["start"],
            "run_end": self._run_hooks["end"],
            "cli": self._cli_commands,
        }
        if seam not in collections:
            raise ValueError(f"unknown capability seam: {seam}")
        return [mount.value for mount in collections[seam] if mount.owner == cap_id]

    def release_capability(self, cap_id: str) -> None:
        """Remove every registration owned by ``cap_id`` from all seams."""

        disposers = self._disposers.pop(cap_id, [])
        disposer_error: Exception | None = None
        for disposer in reversed(disposers):
            try:
                disposer()
            except Exception as exc:  # noqa: BLE001 - finish remaining cleanup
                if disposer_error is None:
                    disposer_error = exc

        self._middleware = [item for item in self._middleware if item.owner != cap_id]
        self._tools = [item for item in self._tools if item.owner != cap_id]
        self._prompt_blocks = [
            item for item in self._prompt_blocks if item.owner != cap_id
        ]
        for when in _RUN_HOOK_WHEN:
            self._run_hooks[when] = [
                item for item in self._run_hooks[when] if item.owner != cap_id
            ]
        self._cli_commands = [
            item for item in self._cli_commands if item.owner != cap_id
        ]
        for name in [
            name
            for name, owner in self._service_owners.items()
            if owner == cap_id
        ]:
            self._service_owners.pop(name, None)
            self._services.pop(name, None)
        self._tool_owners = {
            name: owner
            for name, owner in self._tool_owners.items()
            if owner != cap_id
        }
        # Declarations stack per owner, so withdrawal reveals whatever the
        # replaced core tool declared (or leaves the name undeclared).
        self._risk_registry().withdraw(cap_id)
        self._pin_registry().withdraw(cap_id)
        self._usage_role_registry().withdraw(cap_id)
        self._redaction_registry().withdraw(cap_id)
        withdrawn = self._setting_registry().withdraw(cap_id)
        config = self._services.get("config")
        withdraw_setting = getattr(config, "withdraw_plugin_setting", None)
        if callable(withdraw_setting):
            for key in withdrawn:
                withdraw_setting(key)
        self._manifest_configs.pop(cap_id, None)
        self._mounted.pop(cap_id, None)
        if disposer_error is not None:
            raise disposer_error


def _register_factory(
    ctx: CapabilityAssemblyContext,
    service: str,
    seam: str,
    *,
    fail_open: bool = False,
) -> None:
    factory = ctx.service(service)
    if not callable(factory):
        return
    try:
        value = factory()
    except Exception:
        if fail_open:
            return
        raise
    if value is None:
        return
    getattr(ctx, seam)(value)


def _register_service_hook(
    ctx: CapabilityAssemblyContext, when: str, service: str
) -> None:
    hook = ctx.service(service)
    if callable(hook):
        ctx.add_run_hook(when, hook)


def _register_prompt(ctx: CapabilityAssemblyContext, service: str) -> None:
    provider = ctx.service(service)
    if callable(provider):
        ctx.add_prompt_block(provider)


def _register_service(
    ctx: CapabilityAssemblyContext, factory: str, name: str
) -> None:
    """Mount one capability-owned service from a harness-supplied factory.

    The factory produces the live handle/facade; a missing factory or a ``None``
    result leaves the service unregistered (a declared ``provides`` then fails
    visibly during assembly).
    """

    build = ctx.service(factory)
    if not callable(build):
        return
    value = build()
    if value is None:
        return
    ctx.register_service(name, value)


def _register_cli(ctx: CapabilityAssemblyContext, names: Sequence[str]) -> None:
    handlers = ctx.service("cli_handlers", {})
    if not isinstance(handlers, Mapping):
        return
    for name in names:
        handler = handlers.get(name)
        if callable(handler):
            ctx.add_cli_command(name, handler)


def _apply_providers(ctx: CapabilityAssemblyContext) -> None:
    """Mount the S4 provider registry as the ``provider_registry`` service.

    Reuses the registry the harness bootstrap assembled before the actor model
    (the ``_provider_registry`` side channel on config) when present; otherwise
    assembles one from config. Invalid explicit models files fail visibly.
    Plugins register extra providers through
    :func:`phone_agent.v2.providers.register_provider` on this service, and
    custom transports (api -> builder mappings) through
    :func:`phone_agent.v2.providers.register_api_builder` — both callable
    directly from a plugin ``apply`` hook (assembly-time; the transport
    registry is a process-global table, so no per-run ctx plumbing is needed).
    """

    config = ctx.service("config")
    registry = getattr(config, "_provider_registry", None)
    if registry is None:
        from phone_agent.v2.providers import build_provider_registry

        registry = build_provider_registry(config)
    if registry is not None:
        # Leave the same handle on config so auxiliary role builds (compact,
        # verify, safety reviewer, distill) resolve providers without reaching
        # into the assembly context.  Best-effort: some test configs reject
        # attribute writes.
        try:
            config._provider_registry = registry
        except Exception:  # noqa: BLE001 - side channel is best-effort
            pass
        ctx.register_service("provider_registry", registry)


def _apply_taskdoc(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    if bus is not None:
        from phone_agent.v2.middleware.taskdoc import TaskDocInjector

        session = ctx.service("session")
        config = ctx.service("config")
        injector = TaskDocInjector(
            session,
            lang=getattr(config, "lang", "cn"),
            nudge_steps=getattr(config, "taskdoc_nudge_steps", 5),
        )
        ctx.on(MODEL_PRE_REQUEST, injector)
    _register_factory(ctx, "taskdoc_tool_factory", "register_tool")
    _register_service_hook(ctx, "start", "taskdoc_run_start")


def _apply_safety(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    if bus is None:
        return
    from phone_agent.v2.middleware.safety import (
        build_capability_safety_listener,
    )

    session = ctx.service("session")
    config = ctx.service("config")
    mode = getattr(config, "safety_mode", "wary")
    # The declaration registry is live: capabilities that mount tools later in
    # this same assembly pass (deliverable, obs_archive, ...) are classified
    # through their own declarations without any re-ordering.
    listener = build_capability_safety_listener(
        session, config, risks=ctx.service("tool_risk_registry")
    )
    if listener is not None:
        ctx.on(TOOL_EXECUTE, listener)
        if mode in {"wary", "reviewer"}:
            ctx.register_service("safety_warning_listener", listener)


def _apply_budget(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    factory = ctx.service("budget_middleware_factory")
    if not callable(factory) or bus is None:
        return
    budget = factory()
    if budget is None:
        return
    ctx.on(MODEL_PRE_REQUEST, budget.on_pre_request)
    ctx.on(MODEL_REQUEST, budget.on_model_request)
    ctx.on(MODEL_POST_REQUEST, budget.on_post_request)
    tool_guard = getattr(budget, "on_tool_execute", None)
    if callable(tool_guard):
        ctx.on(TOOL_EXECUTE, tool_guard, prepend=True)
    ctx.register_service("budget_instance", budget)


def _apply_compact(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    factory = ctx.service("compact_middleware_factory")
    if not callable(factory) or bus is None:
        return
    pruner = ctx.service("context_pruner")
    try:
        compact = factory(pruner=pruner) if pruner is not None else factory()
    except Exception:
        return
    if compact is None:
        return
    # Compact must run before other model/pre_request listeners (taskdoc, budget,
    # model_limit) so the coarse fold and image pruning happen at the old slot.
    ctx.on(MODEL_PRE_REQUEST, compact.on_pre_request, prepend=True)
    ctx.register_service("compact_instance", compact)


def _apply_boundary_compact(ctx: CapabilityAssemblyContext) -> None:
    """Mount the route-boundary fold trigger on top of the compact instance.

    Inert (registers nothing) when there is no compact instance to fold through or
    no bus to observe; the mode itself is resolved by the registry, so ``shadow``
    and ``on`` both mount here and differ only inside the listener.
    """

    from phone_agent.v2.middleware.boundary_compact import apply_boundary_compact

    apply_boundary_compact(ctx)


def _apply_finish_verify(ctx: CapabilityAssemblyContext) -> None:
    _register_factory(ctx, "finish_verify_tool_factory", "register_tool")


def _apply_deliverable(ctx: CapabilityAssemblyContext) -> None:
    factory = ctx.service("deliverable_tools_factory")
    if callable(factory):
        tools = factory()
        for tool in tools or ():
            ctx.register_tool(tool)
    _register_prompt(ctx, "deliverable_prompt_provider")


def _apply_app_kb(ctx: CapabilityAssemblyContext) -> None:
    _register_service_hook(ctx, "start", "app_kb_run_start")
    _register_prompt(ctx, "app_kb_prompt_provider")
    _register_cli(ctx, ("learn_alias", "forget_alias"))
    _register_service(ctx, "app_kb_service_factory", "app_kb")


def _apply_dream(ctx: CapabilityAssemblyContext) -> None:
    _register_service_hook(ctx, "end", "dream_run_end")
    _register_cli(ctx, ("dream",))


def _apply_experience(ctx: CapabilityAssemblyContext) -> None:
    _register_service_hook(ctx, "start", "experience_run_start")
    _register_service_hook(ctx, "end", "experience_run_end")
    _register_service(ctx, "experience_service_factory", "experience")


def _install_recall_selection_observer(
    ctx: CapabilityAssemblyContext,
    observers: Any,
) -> Callable[[], None]:
    """Make every selection-path failure visible: one trace event + one count.

    The observer is registered on this mount's *scoped* observer registry (not
    a process-global list), so two coexisting agent contexts never cross-notify
    each other's observers.  It only *adds* to the audit plane: the trace event
    carries ``namespace`` and ``error_type`` (never a stack trace or message)
    and the per-namespace counter extends the existing schema-v2 scorecard.
    """

    from pathlib import Path

    from phone_agent.v2.recall import (
        RECALL_SELECTION_ERROR,
        update_selection_error_stats,
    )

    config = ctx.service("config")
    session = ctx.service("session")
    stats_path = (
        Path(getattr(config, "memory_dir", "memory")) / "experience/recall_stats.json"
    )

    def observer(namespace: str, error_type: str) -> None:
        record = getattr(session, "resolution_trace_recorder", None)
        if callable(record):
            try:
                record(
                    RECALL_SELECTION_ERROR,
                    namespace=namespace,
                    error_type=error_type,
                )
            except Exception:  # noqa: BLE001 - trace cannot change run semantics
                pass
        try:
            update_selection_error_stats(stats_path, namespace)
        except Exception:  # noqa: BLE001 - the scorecard is observe-only
            pass

    return observers.add(observer)


def _warmup_recall_embedder(ctx: CapabilityAssemblyContext, observers: Any) -> None:
    """Warm the shared embedder during assembly (``on``/``shadow`` only).

    This deliberately pays the cold-import cost on the main thread before the
    run loop starts, so no background thread can import the shared native MLX
    stack concurrently with run-start recall.
    """

    from phone_agent.v2.recall import resolve_embedder_factory, warmup_embedder

    config = ctx.service("config")
    mode = str(getattr(config, "memory_rag", "off") or "off").strip().lower()
    if mode not in {"on", "shadow"}:
        return
    # Nothing selects without a configured index, so an index-less mount
    # (offline commands, minimal test configs) never pays for a model load.
    if not getattr(config, "vec_db", None):
        return
    factory = resolve_embedder_factory(ctx.service("procedure_injector"))
    if factory is None:
        return
    warmup_embedder(factory, observers=observers)


def _apply_obs_archive(ctx: CapabilityAssemblyContext) -> None:
    """Mount the text-only observation archive + read-only recall tools.

    The archive object comes from the harness factory ``obs_archive_factory``
    (it owns the run_id) and is installed on the session as the fail-open
    observation sink — P0 #15 keeps ``session.observe()`` the single producer;
    the sink only *receives* committed observations.  The two recall tools are
    read-only evidence (marks-first is never bypassed).  The images-middleware
    fold placeholder gains the ``[可 recall_screen]`` suffix only while this
    capability is mounted, and every failure here degrades open: a missing
    session/factory or a failed build leaves the capability inert.
    """

    factory = ctx.service("obs_archive_factory")
    session = ctx.service("session")
    if session is None or not callable(factory):
        return
    try:
        archive = factory()
    except Exception:  # noqa: BLE001 - optional archive never blocks assembly
        return
    if archive is None:
        return
    from phone_agent.v2.obs_archive import RECALL_HINT

    pruner = ctx.service("context_pruner")
    if pruner is not None:
        try:
            pruner.fold_hint = RECALL_HINT
            ctx.on_dispose(lambda: setattr(pruner, "fold_hint", ""))
        except Exception:  # noqa: BLE001, S110 - hint is cosmetic; mounting continues
            pass
    try:
        session.obs_archive_sink = archive
        ctx.on_dispose(lambda: setattr(session, "obs_archive_sink", None))
    except Exception:  # noqa: BLE001, S110 - duck-typed sessions may reject writes
        pass
    ctx.on_dispose(archive.close)
    from phone_agent.v2.tools.obs_archive import make_obs_archive_tools

    for tool in make_obs_archive_tools(session, archive):
        ctx.register_tool(tool)


def _apply_recall(ctx: CapabilityAssemblyContext) -> None:
    from phone_agent.v2.recall import SelectionErrorObservers

    _register_service_hook(ctx, "start", "recall_run_start")
    _register_service_hook(ctx, "end", "recall_run_end")
    _register_service(ctx, "recall_service_factory", "recall")
    _register_prompt(ctx, "recall_prompt_provider")
    # WP-WF3/WF4: the procedure card rides a second prompt provider (separate
    # 1-card/300-token budget) and two event listeners — ``app/launched``
    # stages the entrance delivery, ``model/pre_request`` injects what is
    # pending.  Three delivery situations feed those listeners: the general
    # card at run start (this provider), the mention prefetch, and the app
    # entrance (card plus that app's ≤2 injectable rules).
    _register_prompt(ctx, "procedure_prompt_provider")
    # Selection-error observers are scoped to this mount: the registry is
    # capability-owned (released with the capability) and shared with the
    # injector's indexes and the agent's shadow-recall index, so two
    # coexisting contexts never double-count into shared stats.
    observers = SelectionErrorObservers()
    ctx.register_service("recall_selection_observers", observers)
    injector = ctx.service("procedure_injector")
    if injector is not None:
        # The injector's indexes (and anything else that reads this attribute)
        # must report to this mount's registry, not to a per-object default.
        try:
            injector.selection_error_observers = observers
        except Exception:  # noqa: BLE001 - assembly must never fail here
            pass
    ctx.on_dispose(_install_recall_selection_observer(ctx, observers))
    bus = ctx.service("event_bus")
    if bus is not None and injector is not None:
        from phone_agent.v2.events import APP_LAUNCHED

        ctx.on(APP_LAUNCHED, injector.on_app_launched)
        ctx.on(MODEL_PRE_REQUEST, injector.on_pre_request)
    # Warm the shared embedder before the run loop (fail-open).  The synchronous
    # call ensures native dependencies are imported by the main thread only.
    _warmup_recall_embedder(ctx, observers)
    _register_cli(
        ctx,
        (
            "rebuild_vec",
            "distill",
            "review_lessons",
            "approve_lesson",
            "revoke_lesson",
            "supersede_lesson",
        ),
    )


def _owned_apply(cap_id: str, hook: CapabilityHook) -> CapabilityHook:
    """Bind a public lifecycle hook to its stable ownership identity."""

    def apply(ctx: CapabilityAssemblyContext) -> None:
        with ctx.applying(cap_id):
            hook(ctx)

    return apply


def _owned_release(cap_id: str) -> CapabilityHook:
    def release(ctx: CapabilityAssemblyContext) -> None:
        ctx.release_capability(cap_id)

    return release


def _report_undeclared_tools(ctx: CapabilityAssemblyContext) -> None:
    """Announce tools whose active registration declares no risk (fail-visible).

    The classification itself is already fail-closed (undeclared == ``actuation``
    in the safety classifier); this only removes the *silence* so a plugin author
    learns the names at assembly time.  Three best-effort channels, none of which
    may fail the assembly: stderr, the session's ``resolution_trace_recorder``
    (when the harness published one) and the ``capability/tools_undeclared``
    event on the bus (when one exists).
    """

    registry = ctx.service("tool_risk_registry")
    if not isinstance(registry, ToolRiskRegistry):
        return
    undeclared = registry.undeclared()
    if not undeclared:
        return
    names = sorted(undeclared)
    try:
        described = ", ".join(f"{name} [{undeclared[name]}]" for name in names)
        print(
            "[capability] undeclared tool risk (fail-closed as 'actuation'): "
            f"{described}; declare it with ctx.register_tool(tool, risk=...)",
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001, S110 - the notice must never fail assembly
        pass
    try:
        record = getattr(ctx.service("session"), "resolution_trace_recorder", None)
        if callable(record):
            record(CAPABILITY_TOOLS_UNDECLARED, tools=names, owners=undeclared)
    except Exception:  # noqa: BLE001, S110 - trace is observe-only
        pass
    try:
        emit = getattr(ctx.service("event_bus"), "emit", None)
        if callable(emit):
            emit(CAPABILITY_TOOLS_UNDECLARED, {"tools": names, "owners": undeclared})
    except Exception:  # noqa: BLE001, S110 - observers must never fail assembly
        pass


def assemble_capabilities(
    registry: CapabilityRegistry, ctx: CapabilityAssemblyContext
) -> CapabilityAssemblyContext:
    """Reconcile ``ctx`` to active/shadow specs using a cap-id/mode diff.

    Removed and changed-mode capabilities release first; changed/new specs then
    apply in registry order.  ``pending`` and ``off`` specs never apply.
    """

    if not isinstance(registry, CapabilityRegistry):
        raise TypeError("registry must be a CapabilityRegistry")
    if not isinstance(ctx, CapabilityAssemblyContext):
        raise TypeError("ctx must be a CapabilityAssemblyContext")

    specs = {spec.cap_id: spec for spec in registry.specs()}
    rows = {str(row["cap_id"]): row for row in registry.status()}
    desired = {
        cap_id: (str(row["mode"]), specs[cap_id])
        for cap_id, row in rows.items()
        if row["state"] in {"active", "shadow"}
    }
    ordered_specs: list[CapabilitySpec] = []
    pending = list(registry.specs())
    ordered_ids: set[str] = set()
    while pending:
        ready = [
            spec
            for spec in pending
            if all(dep not in desired or dep in ordered_ids for dep in spec.deps)
        ]
        if not ready:
            ordered_specs.extend(pending)
            break
        for spec in ready:
            ordered_specs.append(spec)
            ordered_ids.add(spec.cap_id)
            pending.remove(spec)
    ctx._capability_order = {
        spec.cap_id: index for index, spec in enumerate(ordered_specs)
    }

    release_error: Exception | None = None
    for cap_id, (old_mode, old_spec) in reversed(tuple(ctx._mounted.items())):
        next_item = desired.get(cap_id)
        if next_item is not None and next_item[0] == old_mode:
            continue
        try:
            with ctx.applying(cap_id):
                if old_spec.release is not None:
                    old_spec.release(ctx)
        except Exception as exc:
            if release_error is None:
                release_error = exc
        finally:
            try:
                ctx.release_capability(cap_id)
            except Exception as exc:
                if release_error is None:
                    release_error = exc
            ctx._mounted.pop(cap_id, None)

    if release_error is not None:
        raise release_error

    for spec in ordered_specs:
        item = desired.get(spec.cap_id)
        if item is None or spec.cap_id in ctx._mounted:
            continue
        mode, active_spec = item
        # Publish the manifest values before the apply hook runs: the plugin
        # reads them through ctx.plugin_config() and they are the override tier
        # for the settings it declares.  Dropped by release_capability.
        ctx._manifest_configs[spec.cap_id] = dict(active_spec.manifest_config)
        try:
            with ctx.applying(spec.cap_id):
                if active_spec.apply is not None:
                    active_spec.apply(ctx)
                # A declared service must actually be mounted by this capability
                # so a "declared but never wired" seam fails loudly, not silently.
                if active_spec.provides is not None and (
                    ctx._service_owners.get(active_spec.provides) != spec.cap_id
                ):
                    raise ValueError(
                        f"capability {spec.cap_id!r} declares provides="
                        f"{active_spec.provides!r} but registered no such service"
                    )
        except Exception:
            try:
                ctx.release_capability(spec.cap_id)
            except Exception:
                pass
            raise
        ctx._mounted[spec.cap_id] = (mode, active_spec)
    _report_undeclared_tools(ctx)
    return ctx


def _capability_mode(raw: Any, *, default: str) -> str:
    """Map a capability's configured mode onto the three legal mount modes.

    ``off`` and ``shadow`` pass through; every other non-empty value (``wary``,
    ``hard``, ``auto``, ``manual``, ...) means "mount at full strength", which is
    what the registry's state derivation did before the modes were validated —
    so translating here keeps every built-in status row identical.
    """

    mode = str(raw if raw is not None else "").strip().lower() or default
    return mode if mode in CAPABILITY_MODES else "on"


def build_capability_registry(config: Any) -> CapabilityRegistry:
    """Build the thirteen-capability composition shared by agent and runner."""

    registry = CapabilityRegistry()
    for spec in (
        CapabilitySpec(
            "providers",
            "Model providers",
            "on",
            apply=_owned_apply("providers", _apply_providers),
            release=_owned_release("providers"),
        ),
        CapabilitySpec(
            "taskdoc",
            "TaskDoc",
            "on" if getattr(config, "taskdoc_enabled", True) else "off",
            apply=_owned_apply("taskdoc", _apply_taskdoc),
            release=_owned_release("taskdoc"),
        ),
        CapabilitySpec(
            "safety",
            "Safety",
            _capability_mode(getattr(config, "safety_mode", "wary"), default="wary"),
            apply=_owned_apply("safety", _apply_safety),
            release=_owned_release("safety"),
        ),
        CapabilitySpec(
            "budget",
            "Token budget",
            "on",
            apply=_owned_apply("budget", _apply_budget),
            release=_owned_release("budget"),
        ),
        CapabilitySpec(
            "compact",
            "Auto compact",
            "on" if getattr(config, "compact_enabled", True) else "off",
            apply=_owned_apply("compact", _apply_compact),
            release=_owned_release("compact"),
        ),
        CapabilitySpec(
            "boundary_compact",
            "Boundary-aware compact",
            _capability_mode(
                getattr(config, "boundary_compact_mode", "shadow"), default="shadow"
            ),
            deps=("compact",),
            apply=_owned_apply("boundary_compact", _apply_boundary_compact),
            release=_owned_release("boundary_compact"),
        ),
        CapabilitySpec(
            "finish_verify",
            "Finish verifier",
            _capability_mode(getattr(config, "finish_verify", "auto"), default="auto"),
            apply=_owned_apply("finish_verify", _apply_finish_verify),
            release=_owned_release("finish_verify"),
        ),
        CapabilitySpec(
            "deliverable",
            "HTML deliverable",
            "on" if getattr(config, "deliverable_enabled", True) else "off",
            apply=_owned_apply("deliverable", _apply_deliverable),
            release=_owned_release("deliverable"),
        ),
        CapabilitySpec(
            "app_kb",
            "App knowledge",
            "on" if getattr(config, "app_kb_enabled", True) else "off",
            apply=_owned_apply("app_kb", _apply_app_kb),
            release=_owned_release("app_kb"),
        ),
        CapabilitySpec(
            "dream",
            "Memory maintenance",
            _capability_mode(getattr(config, "dream_mode", "manual"), default="manual"),
            deps=("app_kb",),
            apply=_owned_apply("dream", _apply_dream),
            release=_owned_release("dream"),
        ),
        CapabilitySpec(
            "experience",
            "Experience plane",
            "on" if getattr(config, "experience_enabled", True) else "off",
            apply=_owned_apply("experience", _apply_experience),
            release=_owned_release("experience"),
        ),
        CapabilitySpec(
            "recall",
            "Memory recall",
            _capability_mode(getattr(config, "memory_rag", "shadow"), default="shadow"),
            deps=("experience",),
            apply=_owned_apply("recall", _apply_recall),
            release=_owned_release("recall"),
        ),
        CapabilitySpec(
            "obs_archive",
            "Observation archive",
            _capability_mode(getattr(config, "obs_archive", "off"), default="off"),
            apply=_owned_apply("obs_archive", _apply_obs_archive),
            release=_owned_release("obs_archive"),
        ),
    ):
        registry.register(spec)
    return registry


__all__ = [
    "CAPABILITY_MODES",
    "HARNESS_OWNER",
    "PLUGIN_API_VERSION",
    "CapabilityAssemblyContext",
    "CapabilityContext",
    "CapabilityRegistry",
    "CapabilitySpec",
    "MiddlewareReplacement",
    "PromptBlock",
    "ToolRisk",
    "ToolRiskRegistry",
    "assemble_capabilities",
    "build_capability_registry",
]
