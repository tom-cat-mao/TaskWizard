"""Capability-owned assembly for the v2 thin loop.

The registry remains the public status/audit plane, but its lifecycle hooks now
mount every optional capability through five declared seams.  The concrete
context records each registration under the currently applying ``cap_id`` so a
later release can remove the whole contribution without guessing object names.

This is intentionally a static assembly layer.  Reconciliation is useful while
building agents and in tests/consoles, but it does not mutate a compiled agent's
tool table while a run is in progress.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import re
from typing import Any, Protocol, runtime_checkable

from phone_agent.v2.events import (
    MODEL_POST_REQUEST,
    MODEL_PRE_REQUEST,
    MODEL_REQUEST,
    TOOL_EXECUTE,
)

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

# Middleware is now reserved for LangChain bridge middleware only; all policy
# behavior lives on the event bus.  Capabilities should not register middleware
# directly; the core harness owns the four bridges plus optional extra_middleware.
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
    """The only five seams through which a capability may affect assembly."""

    def register_middleware(self, middleware: Any) -> None: ...

    def register_tool(self, tool: Any) -> None: ...

    def add_prompt_block(self, provider: PromptProvider) -> None: ...

    def add_run_hook(self, when: str, fn: RunHook) -> None: ...

    def add_cli_command(self, name: str, handler: CliHandler) -> None: ...

    def register_service(self, name: str, value: Any) -> None: ...


@dataclass(frozen=True)
class PromptBlock:
    """One provider result and where it belongs in the initial messages."""

    content: str
    placement: str = "system_message"

    def __post_init__(self) -> None:
        if self.placement not in {"system_suffix", "system_message"}:
            raise ValueError(f"invalid prompt block placement: {self.placement!r}")


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

    def __post_init__(self) -> None:
        if not _CAPABILITY_ID.fullmatch(self.cap_id):
            raise ValueError(f"invalid capability id: {self.cap_id!r}")
        if not str(self.title).strip():
            raise ValueError("capability title must not be empty")
        if not str(self.mode).strip():
            raise ValueError("capability mode must not be empty")
        for dependency in self.deps:
            if not _CAPABILITY_ID.fullmatch(dependency):
                raise ValueError(f"invalid dependency id: {dependency!r}")
        if self.provides is not None and not _SERVICE_NAME.fullmatch(self.provides):
            raise ValueError(f"invalid provides service key: {self.provides!r}")
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
    """Capability-owned mount ledger for the five assembly seams."""

    def __init__(self, services: Mapping[str, Any] | None = None) -> None:
        self._services = dict(services or {})
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
        self._service_owners: dict[str, str] = {}

    @property
    def current_cap_id(self) -> str | None:
        return self._current_cap_id

    def service(self, name: str, default: Any = None) -> Any:
        """Return a harness-supplied dependency without making it a seam."""

        return self._services.get(name, default)

    def set_service(self, name: str, value: Any) -> None:
        self._services[name] = value

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

    def register_tool(self, tool: Any) -> None:
        self._mount(self._tools, tool, 100 + self._capability_order.get(self._owner(), 0))

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
        capabilities registering the same key is a fail-visible conflict.
        """

        owner = self._owner()
        clean = str(name).strip()
        if not _SERVICE_NAME.fullmatch(clean):
            raise ValueError(f"invalid service name: {name!r}")
        existing_owner = self._service_owners.get(clean)
        if existing_owner is not None and existing_owner != owner:
            raise ValueError(
                f"service {clean!r} already registered by capability "
                f"{existing_owner!r}"
            )
        self._services[clean] = value
        self._service_owners[clean] = owner

    # Core harness products use the same ordered collections, but cannot be
    # released by a capability because their owner is outside the cap_id space.
    def register_core_middleware(
        self, middleware: Any, *, order: int, replace_key: str | None = None
    ) -> None:
        self._sequence += 1
        self._middleware.append(
            _Mount("__core__", middleware, self._sequence, order, replace_key)
        )

    def register_core_tool(self, tool: Any, *, order: int) -> None:
        self._sequence += 1
        self._tools.append(_Mount("__core__", tool, self._sequence, order))

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
        self._mounted.pop(cap_id, None)


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

    Reuses the registry the harness already built for the actor model (the
    ``_provider_registry`` side channel on config) when present; otherwise
    assembles one from config.  Fail-open: an unusable models.json leaves the
    service unmounted and every role build degrades to the legacy
    single-gateway path.  Plugins register extra providers through
    :func:`phone_agent.v2.providers.register_provider` on this service.
    """

    config = ctx.service("config")
    registry = getattr(config, "_provider_registry", None)
    if registry is None:
        try:
            from phone_agent.v2.providers import build_provider_registry

            registry = build_provider_registry(config)
        except Exception:  # noqa: BLE001 - provider layer must never crash assembly
            registry = None
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
        disposer = bus.on(MODEL_PRE_REQUEST, injector)
        ctx.register_service("taskdoc_event_disposer", disposer)
    _register_factory(ctx, "taskdoc_tool_factory", "register_tool")
    _register_service_hook(ctx, "start", "taskdoc_run_start")


def _apply_safety(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    if bus is None:
        return
    from phone_agent.v2.middleware.safety import (
        build_capability_safety_listener,
        register_default_safety_listener,
    )

    session = ctx.service("session")
    config = ctx.service("config")
    mode = getattr(config, "safety_mode", "wary")
    if mode in {"wary", "reviewer"}:
        pair = register_default_safety_listener(bus, session, config)
        if pair is not None:
            listener, disposer = pair
            ctx.set_service("_safety_warning_listener", listener)
            ctx.set_service("_safety_event_disposer", disposer)
    elif mode == "hard":
        listener = build_capability_safety_listener(session, config)
        if listener is not None:
            disposer = bus.on(TOOL_EXECUTE, listener)
            ctx.set_service("_safety_event_disposer", disposer)


def _apply_budget(ctx: CapabilityAssemblyContext) -> None:
    bus = ctx.service("event_bus")
    factory = ctx.service("budget_middleware_factory")
    if not callable(factory) or bus is None:
        return
    budget = factory()
    if budget is None:
        return
    disposers = [
        bus.on(MODEL_PRE_REQUEST, budget.on_pre_request),
        bus.on(MODEL_REQUEST, budget.on_model_request),
        bus.on(MODEL_POST_REQUEST, budget.on_post_request),
    ]
    ctx.register_service("budget_instance", budget)
    ctx.register_service("budget_event_disposers", disposers)


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
    disposer = bus.on(MODEL_PRE_REQUEST, compact.on_pre_request, prepend=True)
    ctx.register_service("compact_instance", compact)
    ctx.register_service("compact_pre_request_disposer", disposer)


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
) -> Callable[[], None]:
    """Make every selection-path failure visible: one trace event + one count.

    The observer is installed for the lifetime of the capability and only
    *adds* to the audit plane: the trace event carries ``namespace`` and
    ``error_type`` (never a stack trace or message) and the per-namespace
    counter extends the existing schema-v2 scorecard.
    """

    from pathlib import Path

    from phone_agent.v2.recall import (
        RECALL_SELECTION_ERROR,
        add_selection_error_observer,
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

    return add_selection_error_observer(observer)


def _warmup_recall_embedder(ctx: CapabilityAssemblyContext) -> None:
    """Load the shared embedder off the run path (``on``/``shadow`` only)."""

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
    warmup_embedder(factory)


def _apply_recall(ctx: CapabilityAssemblyContext) -> None:
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
    disposers: list[Callable[[], None]] = [_install_recall_selection_observer(ctx)]
    bus = ctx.service("event_bus")
    injector = ctx.service("procedure_injector")
    if bus is not None and injector is not None:
        from phone_agent.v2.events import APP_LAUNCHED

        disposers.append(bus.on(APP_LAUNCHED, injector.on_app_launched))
        disposers.append(bus.on(MODEL_PRE_REQUEST, injector.on_pre_request))
    ctx.register_service("recall_event_disposers", disposers)
    # Warm the shared embedder on a daemon thread so the model load is not
    # charged to the first recall/selection of the run (fail-open).
    _warmup_recall_embedder(ctx)
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
        if cap_id == "safety":
            disposer = ctx.service("_safety_event_disposer")
            if callable(disposer):
                disposer()
            ctx.set_service("_safety_event_disposer", None)
            ctx.set_service("_safety_warning_listener", None)
        if cap_id == "taskdoc":
            disposer = ctx.service("taskdoc_event_disposer")
            if callable(disposer):
                disposer()
                ctx.set_service("taskdoc_event_disposer", None)
        # Dispose any model-domain event listeners registered by the capability.
        disposer = ctx.service(f"{cap_id}_pre_request_disposer")
        if callable(disposer):
            disposer()
            ctx.set_service(f"{cap_id}_pre_request_disposer", None)
        for d in ctx.service(f"{cap_id}_event_disposers") or ():
            if callable(d):
                d()
        ctx.set_service(f"{cap_id}_event_disposers", None)
        ctx.release_capability(cap_id)

    return release


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
    ctx._capability_order = {
        spec.cap_id: index for index, spec in enumerate(registry.specs())
    }

    for cap_id, (old_mode, old_spec) in reversed(tuple(ctx._mounted.items())):
        next_item = desired.get(cap_id)
        if next_item is not None and next_item[0] == old_mode:
            continue
        try:
            with ctx.applying(cap_id):
                if old_spec.release is not None:
                    old_spec.release(ctx)
        finally:
            # Enforce zero residue even if a custom release hook is incomplete.
            ctx.release_capability(cap_id)
            ctx._mounted.pop(cap_id, None)

    for spec in registry.specs():
        item = desired.get(spec.cap_id)
        if item is None or spec.cap_id in ctx._mounted:
            continue
        mode, active_spec = item
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
            ctx.release_capability(spec.cap_id)
            raise
        ctx._mounted[spec.cap_id] = (mode, active_spec)
    return ctx


def build_capability_registry(config: Any) -> CapabilityRegistry:
    """Build the ten-capability composition shared by agent and runner."""

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
            getattr(config, "safety_mode", "wary"),
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
            "finish_verify",
            "Finish verifier",
            getattr(config, "finish_verify", "auto"),
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
            getattr(config, "dream_mode", "manual"),
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
            getattr(config, "memory_rag", "shadow"),
            deps=("experience",),
            apply=_owned_apply("recall", _apply_recall),
            release=_owned_release("recall"),
        ),
    ):
        registry.register(spec)
    return registry


__all__ = [
    "PLUGIN_API_VERSION",
    "CapabilityAssemblyContext",
    "CapabilityContext",
    "CapabilityRegistry",
    "CapabilitySpec",
    "MiddlewareReplacement",
    "PromptBlock",
    "assemble_capabilities",
    "build_capability_registry",
]
