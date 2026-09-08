"""WP-WF3/WF4 procedure-card + app-rule injection points.

Per ``WORKFLOW-MEMORY-DESIGN.md`` §5 the procedure channel injects at most one
card per point, inside its **own** 1-card/300-token budget
(:data:`phone_agent.v2.recall.PROCEDURE_MAX_ITEMS` /
:data:`phone_agent.v2.recall.PROCEDURE_MAX_TOKENS`) — the rule channel's
3-item/800-token quota is never shared or crowded out.

* **Point one (run start)** — :meth:`ProcedureCardInjector.run_start` selects a
  general card (``app_package=None``, so only ``app_scope == "general"`` cards
  qualify) and renders it for the recall capability's second prompt provider.
  It runs only when ``memory_rag == "on"``; in ``shadow`` the pre-existing
  shadow path already selects and records that card at run end, so this point
  stays silent instead of double-counting the same selection.
* **Point two (after a confirmed ``launch_app``)** — :meth:`on_app_launched`
  selects the card scoped to the launched package and stashes it as *pending*;
  :meth:`on_pre_request` then injects the pending card **once** as a
  ``[PROCEDURE_CARD]`` system message before the model's next call. One package
  is selected at most once per run, and ``on`` injects while ``shadow`` only
  records the recall statistics.

WP-WF4 (chain delivery, design 增补 v3) extends both event points:

* **App rules beside the card** — an entrance delivery now also carries the
  app's injectable rules (``scope.app ==`` the package, device-matched,
  ``approved``/``auto_approved`` rules selected read-only by
  :func:`phone_agent.v2.evolution.select_app_rules_for_injection`) inside its
  own ≤2-item/200-token budget, rendered as an ``[APP_RULES]`` section of the
  same one-shot system message.  A rules-only delivery (no card hit for that
  app) is still delivered.
* **Mention prefetch (run start)** — :meth:`run_start` resolves the apps
  explicitly mentioned in the goal text through
  :func:`phone_agent.v2.names.mentioned_apps` (typed resolver, ``resolved``
  only, ≤2 apps in mention order) and queues each app's card + rules for the
  **first** model call, so planning already sees them (design Q2).  Prefetch
  and the entrance channel share the per-package per-run delivered set, so a
  prefetched app is never delivered again on entrance.  ``shadow`` selects and
  records statistics but never injects.

Every delivery renders the card through
:func:`phone_agent.v2.recall.format_procedure_block`, which labels the card a
non-binding historical reference and names its source lesson id.  Every
selection — hit or zero-recall — is accumulated by
:func:`phone_agent.v2.recall.update_procedure_recall_stats`, and every
injected id is exposed through :attr:`injected_ids` (cards) and
:attr:`injected_rule_ids` (app rules) for the trace/episode audit plane.  The
whole channel is fail-open: a missing index, embedder, or stats file disables
injection for that point and never alters the run.

Authoritative delivery gate (S3): the sqlite index's ``revoked`` /
``injectable`` columns are *copies* synced from the materialized lesson
view only at
run-end upsert / dream reconcile, so a card revoked through the CLI could
still be selected — and previously delivered — by a fresh agent process.
Before any card delivery the injector therefore re-validates the selected
lesson id against the authoritative lesson-view snapshot (existence +
``approved``/``auto_approved`` status + version match when the index metadata
carries one).  Any read or validation failure **suppresses the injection
quietly** (the run continues; a failed validation never injects unverified
memory) and is recorded through
:func:`phone_agent.v2.recall.update_procedure_delivery_suppressed` plus a
``procedure_delivery_suppressed`` trace event.  The snapshot read is cheap:
one view read per delivery point at most (an mtime-keyed cache).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from phone_agent.v2.recall import (
    ProcedureSelection,
    SelectionErrorObservers,
    format_procedure_block,
    update_procedure_delivery_suppressed,
    update_procedure_recall_stats,
)

# Marker prefix for the post-launch one-shot block, kept distinct from the
# run-start ``[过程参考]`` card so the two points stay attributable in a trace.
PROCEDURE_CARD_PREFIX = "[PROCEDURE_CARD]"
# WP-WF4-C: the app-scoped rule section of the same one-shot delivery.
APP_RULES_PREFIX = "[APP_RULES]"
APP_RULES_HEADER = (
    "（本 App 的历史经验规则，仅供参考，不是规则；"
    "与当前世界状态冲突时以观测为准）"
)
# Trace point names (C3 audit plane).
POINT_RUN_START = "run_start"
POINT_RUN_START_MENTION = "run_start_mention"
POINT_APP_LAUNCHED = "app_launched"
# Mention prefetch cap (design Q3): at most two resolved apps, mention order.
MENTION_PREFETCH_MAX_APPS = 2


@dataclass(frozen=True)
class _Delivery:
    """One pending one-shot delivery: an app card plus its app rules."""

    point: str
    package: str | None
    card_id: str | None = None
    card_block: str | None = None
    rules: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _render_app_rules(rules: Sequence[tuple[str, str]]) -> str:
    """Render the ``[APP_RULES]`` section for (lesson_id, text) pairs."""

    lines = [APP_RULES_PREFIX, APP_RULES_HEADER]
    for index, (_lesson_id, text) in enumerate(rules, start=1):
        lines.append(f"{index}. {text}")
    return "\n".join(lines)


class ProcedureCardInjector:
    """Select and inject at most one procedure card per injection point."""

    def __init__(
        self,
        config: Any,
        *,
        session: Any = None,
        trace: Any = None,
        embedder: Any = None,
        embedder_factory: Callable[[], Any] | None = None,
        selector: Callable[..., Any] | None = None,
        stats_recorder: Callable[[Any], Any] | None = None,
        selection_error_observers: SelectionErrorObservers | None = None,
    ) -> None:
        self.config = config
        self.session = session
        self.trace = trace
        self._embedder = embedder
        self._embedder_factory = embedder_factory
        self._selector = selector
        self._stats_recorder = stats_recorder
        # Mount-scoped selection-error observers: every VecIndex this injector
        # opens reports its failures here and nowhere else.
        self.selection_error_observers = (
            selection_error_observers
            if selection_error_observers is not None
            else SelectionErrorObservers()
        )
        # Run-scoped state, cleared by :meth:`reset` at every run boundary.
        self.goal: str = ""
        self.run_start_block: str | None = None
        self.run_start_selection: ProcedureSelection | None = None
        self._pending: _Delivery | None = None
        # WP-WF4-C: mention prefetch deliveries queued at run start, drained
        # (FIFO, before any entrance delivery) at the first model call.
        self._prefetch: list[_Delivery] = []
        self._injected_ids: list[str] = []
        self._injected_rule_ids: list[str] = []
        self._selected_packages: set[str] = set()
        # Emergency revocations (P0 #18): a revoked card must not reach a later
        # injection point. Already-sent messages stay immutable.
        self._revoked: set[str] = set()
        # Authoritative lesson-view snapshot cache, keyed by the view file's
        # (mtime_ns, size) so one delivery point reads the file at most once.
        # ``False`` marks a failed read: validation then fails closed until
        # the file changes.
        self._lessons_cache_key: tuple[int, int] | None = None
        self._lessons_cache: dict[str, tuple[str, int]] | None | bool = False

    # -- run lifecycle ---------------------------------------------------

    def reset(self) -> None:
        """Clear every per-run product of this injector (run boundary)."""

        self.goal = ""
        self.run_start_block = None
        self.run_start_selection = None
        self._pending = None
        self._prefetch = []
        self._injected_ids = []
        self._injected_rule_ids = []
        self._selected_packages = set()
        self._lessons_cache_key = None
        self._lessons_cache = False

    @property
    def injected_ids(self) -> list[str]:
        """Card lesson ids actually handed to the model this run (audit)."""

        return list(self._injected_ids)

    @property
    def injected_rule_ids(self) -> list[str]:
        """App-rule lesson ids actually handed to the model this run (C3)."""

        return list(self._injected_rule_ids)

    @property
    def pending_lesson_id(self) -> str | None:
        """The pending entrance card id waiting for the next pre-request."""

        if self._pending is not None and self._pending.card_id:
            return self._pending.card_id
        return None

    @property
    def mode(self) -> str:
        return str(getattr(self.config, "memory_rag", "off") or "off").strip().lower()

    @property
    def injects(self) -> bool:
        """``on`` is the only mode that may hand a card to the model."""

        return self.mode == "on"

    @property
    def observes(self) -> bool:
        """Whether the channel selects at all (injection or shadow stats)."""

        return self.mode in {"on", "shadow"}

    # -- injection point one: run start (general cards + mention prefetch) -

    def run_start(self, goal: str) -> ProcedureSelection | None:
        """Select the run-start general card and render its prompt block.

        Only ``app_scope="general"`` cards qualify because no app is known yet
        (the selector's hard filter). ``shadow``/``off`` leave the block
        empty: the pre-existing shadow path owns the run-start measurement
        there, so selecting twice would double-count one run.

        WP-WF4-C: after the general card, mentioned apps are prefetched
        (select in ``on``/``shadow``, inject only in ``on``).
        """

        self.goal = str(goal or "").strip()
        self.run_start_block = None
        self.run_start_selection = None
        if not self.goal:
            return None
        if self.injects:
            selection = self._select(self.goal, app_package=None)
            self.run_start_selection = selection
            if selection is not None and selection.selected:
                block = self._card_block(selection, POINT_RUN_START)
                if block:
                    self.run_start_block = block
                    self._injected_ids.append(str(selection.lesson_id))
                    self._record_trace(
                        POINT_RUN_START,
                        [str(selection.lesson_id)],
                        app_package=None,
                    )
        self._prefetch_mentioned_apps()
        return self.run_start_selection

    # -- injection point two: after a confirmed launch -------------------

    def on_app_launched(self, payload: Any) -> None:
        """``app/launched`` listener: card + app rules for that package."""

        if not self.observes:
            return
        package = ""
        if isinstance(payload, Mapping):
            package = str(payload.get("package", "") or "").strip()
        if not package or package in self._selected_packages:
            return
        goal = self.goal or self._goal_from_session()
        if not goal:
            # Nothing is selectable without a goal, so the package must keep
            # its one delivery slot instead of being consumed by a no-op: an
            # empty goal now (e.g. a launch observed before ``run_start``)
            # must not silence that app for the rest of the run.
            return
        # One selection attempt per package per run: a later re-launch of the
        # same app has the same goal and the same index, so it cannot produce
        # a different card and must not inject twice.  Mention-prefetched
        # packages share this set, so entrance never re-delivers them (C2).
        self._selected_packages.add(package)
        delivery = self._build_delivery(
            goal, package, with_rules=self.injects, point=POINT_APP_LAUNCHED
        )
        if delivery is None or not self.injects:
            return
        # Replacing a still-pending entrance delivery keeps one message in
        # flight: two launches before the next model call must never queue two
        # entrance blocks.  Prefetch deliveries queue separately and are never
        # displaced by a launch.
        self._pending = delivery

    def make_delta(self, messages: Sequence[Any]) -> list[Any] | None:
        """Consume pending deliveries as trailing system messages (or ``None``)."""

        deliveries: list[_Delivery] = []
        while self._prefetch:
            deliveries.append(self._prefetch.pop(0))
        if self._pending is not None:
            deliveries.append(self._pending)
            self._pending = None
        try:
            from langchain_core.messages import SystemMessage
        except Exception:  # noqa: BLE001 - injection is fail-open
            return None
        delta = list(messages)
        injected_any = False
        for delivery in deliveries:
            content, card_id, rules = self._render_delivery(delivery)
            if content is None:
                continue
            delta.append(SystemMessage(content=content))
            if card_id:
                self._injected_ids.append(card_id)
            for rule_id, _text in rules:
                self._injected_rule_ids.append(rule_id)
            self._record_trace(
                delivery.point,
                ([card_id] if card_id else []) + [rid for rid, _ in rules],
                app_package=delivery.package,
            )
            injected_any = True
        return delta if injected_any else None

    def on_pre_request(self, messages: Any, next: Callable[[Any], Any]) -> Any:
        """``model/pre_request`` waterfall listener: inject pending deliveries."""

        delta = self.make_delta(messages)
        if delta is None:
            return next(messages)
        return next(delta)

    # -- internals -------------------------------------------------------

    def revoke(self, lesson_id: str) -> None:
        """Exclude one card/rule from every later injection point (P0 #18)."""

        clean = str(lesson_id or "").strip()
        if not clean:
            return
        self._revoked.add(clean)
        if self._pending is not None and self._pending.card_id == clean:
            self._pending = None
        # Queued prefetch deliveries are filtered against ``_revoked`` at
        # delivery time; a pending delivery whose rules got revoked keeps its
        # remaining rules.
        if self.run_start_selection is not None and (
            self.run_start_selection.lesson_id == clean
        ):
            self.run_start_block = None

    def _revoked_selection(self, selection: ProcedureSelection | None) -> bool:
        return (
            selection is not None
            and str(selection.lesson_id or "") in self._revoked
        )

    # -- WP-WF4-C internals: prefetch + delivery assembly -----------------

    def _prefetch_mentioned_apps(self) -> None:
        """Queue card+rules deliveries for apps mentioned in the goal (C2)."""

        if not self.observes or not self.goal:
            return
        try:
            packages = self._mentioned_packages(self.goal)
        except Exception:  # noqa: BLE001 - prefetch is fail-open
            return
        for package in packages:
            clean = str(package or "").strip()
            if not clean or clean in self._selected_packages:
                continue
            self._selected_packages.add(clean)
            delivery = self._build_delivery(
                self.goal,
                clean,
                with_rules=self.injects,
                point=POINT_RUN_START_MENTION,
            )
            if delivery is not None and self.injects:
                self._prefetch.append(delivery)

    def _mentioned_packages(self, goal: str) -> list[str]:
        """Resolve mentioned apps via the typed resolver, mention order."""

        from phone_agent.v2.names import ResolverSettings, mentioned_apps
        from phone_agent.v2.resolver import app_kb_entries

        resolutions = mentioned_apps(
            goal,
            kb_entries=app_kb_entries(self.session),
            settings=ResolverSettings.from_config(self.config),
            limit=MENTION_PREFETCH_MAX_APPS,
        )
        return [
            str(resolution.winner.package)
            for resolution in resolutions
            if resolution.winner is not None
        ]

    def _build_delivery(
        self, goal: str, package: str, *, with_rules: bool, point: str
    ) -> _Delivery | None:
        """Select one app's card (+ rules) and assemble its delivery."""

        card_id: str | None = None
        card_block: str | None = None
        selection = self._select(goal, app_package=package)
        block = self._card_block(selection, point)
        if block:
            card_id = str(selection.lesson_id)
            card_block = block
        rules: tuple[tuple[str, str], ...] = ()
        if with_rules:
            rules = tuple(self._select_app_rules(package))
        if card_block is None and not rules:
            return None
        return _Delivery(
            point=point,
            package=package,
            card_id=card_id,
            card_block=card_block,
            rules=rules,
        )

    def _card_block(self, selection: Any, point: str) -> str | None:
        """Render the selected card for delivery, or ``None`` (skip recorded).

        The authoritative gate applies only when this mode actually injects:
        ``shadow`` selects purely for statistics and never delivers, so it
        must not accrue suppression counts for deliveries that cannot happen.
        """

        if selection is None or not selection.selected:
            return None
        if self._revoked_selection(selection):
            return None
        reason = self._authoritative_check(selection) if self.injects else None
        if reason is not None:
            self._record_suppressed(point, selection, reason)
            return None
        return format_procedure_block(selection)

    # -- authoritative delivery gate (S3) ---------------------------------

    def _authoritative_check(self, selection: ProcedureSelection) -> str | None:
        """Return ``None`` when the authoritative view backs this card.

        The sqlite index's ``revoked``/``injectable`` flags are copies synced
        only at run-end/dream, so the selected lesson id is re-checked against
        the current authoritative lesson view: the lesson must exist, be
        injectable, and — when the index metadata carries a version — carry
        the same version.  Any mismatch (or an unreadable snapshot) suppresses
        the delivery; the run itself is never affected.
        """

        lesson_id = str(selection.lesson_id or "").strip()
        if not lesson_id:
            return "no_lesson_id"
        snapshot = self._lessons_status_snapshot()
        if snapshot is None:
            return "snapshot_unreadable"
        entry = snapshot.get(lesson_id)
        if entry is None:
            return "lesson_missing"
        injectable, version = entry
        if not injectable:
            return "not_injectable"
        if selection.version is not None and version != selection.version:
            return "version_mismatch"
        return None

    def _lessons_status_snapshot(self) -> dict[str, tuple[bool, int]] | None:
        """``{lesson_id: (injectable, version)}`` from the lesson view.

        Read at most once per file change (mtime-keyed cache) per delivery
        point; ``None`` signals an unreadable snapshot (fail closed for the
        injection, never for the run).  Strictly read-only.
        """

        from phone_agent.v2.evolution import (
            lesson_injectable,
            lessons_view_path,
            read_lessons_snapshot,
        )

        lessons_dir = str(
            getattr(self.config, "lessons_dir", "memory/lessons") or "memory/lessons"
        )
        view_path = lessons_view_path(lessons_dir)
        try:
            stat = view_path.stat()
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        if self._lessons_cache_key == key and isinstance(self._lessons_cache, dict):
            return self._lessons_cache
        try:
            snapshot = {
                str(lesson.lesson_id): (lesson_injectable(lesson), int(lesson.version))
                for lesson in read_lessons_snapshot(lessons_dir)
            }
        except Exception:  # noqa: BLE001 - the gate fails closed, not open
            return None
        self._lessons_cache_key = key
        self._lessons_cache = snapshot
        return snapshot

    def _record_suppressed(self, point: str, selection: Any, reason: str) -> None:
        """Record one suppressed delivery in the shadow/stats path (fail-open)."""

        lesson_id = str(getattr(selection, "lesson_id", "") or "") or None
        try:
            stats_path = (
                Path(getattr(self.config, "memory_dir", "memory"))
                / "experience/recall_stats.json"
            )
            update_procedure_delivery_suppressed(
                stats_path, reason=reason, lesson_id=lesson_id
            )
        except Exception:  # noqa: BLE001 - shadow statistics are fail-open
            pass
        record = getattr(self.trace, "record_event", None)
        if not callable(record):
            return
        try:
            record(
                "procedure_delivery_suppressed",
                point=point,
                lesson_id=lesson_id,
                reason=str(reason),
            )
        except Exception:  # noqa: BLE001 - trace cannot change run semantics
            return

    def _select_app_rules(self, package: str) -> list[tuple[str, str]]:
        """Read-only app-rule snapshot rendered as one-liners (fail-open)."""

        try:
            from phone_agent.v2.evolution import select_app_rules_for_injection

            lessons = select_app_rules_for_injection(
                getattr(self.config, "lessons_dir", "memory/lessons"),
                app_package=package,
                device_scope=self._device_id(),
            )
        except Exception:  # noqa: BLE001 - app-rule delivery is fail-open
            return []
        rules: list[tuple[str, str]] = []
        for lesson in lessons:
            lesson_id = str(getattr(lesson, "lesson_id", "") or "")
            if not lesson_id or lesson_id in self._revoked:
                continue
            device = (getattr(lesson, "scope", {}) or {}).get("device")
            scope_label = "全局 scope" if device is None else "设备 scope"
            rules.append(
                (lesson_id, f"{lesson.text}（来源 {lesson_id} · {scope_label}）")
            )
        return rules

    def _render_delivery(
        self, delivery: _Delivery
    ) -> tuple[str | None, str | None, list[tuple[str, str]]]:
        """Render a delivery against current revocations (zero-residue drop)."""

        card_id = delivery.card_id
        if card_id and card_id in self._revoked:
            card_id = None
        rules = [
            (rule_id, text)
            for rule_id, text in delivery.rules
            if rule_id not in self._revoked
        ]
        parts: list[str] = []
        if card_id and delivery.card_block:
            parts.append(f"{PROCEDURE_CARD_PREFIX}\n{delivery.card_block}")
        if rules:
            parts.append(_render_app_rules(rules))
        if not parts:
            return None, None, []
        return "\n".join(parts), card_id, rules

    def _goal_from_session(self) -> str:
        doc = getattr(self.session, "task_doc", None)
        goal = getattr(doc, "goal_base", None)
        return str(goal or "").strip()

    def _device_id(self) -> str | None:
        serial = getattr(self.config, "device_id", None)
        if not serial:
            getter = getattr(self.session, "_kb_device_id", None)
            if callable(getter):
                try:
                    serial = getter()
                except Exception:  # noqa: BLE001 - selection is fail-open
                    serial = None
        return serial

    def _select(
        self, goal: str, *, app_package: str | None
    ) -> ProcedureSelection | None:
        """Hard-filter + top-1 one card, recording the outcome (fail-open)."""

        if not getattr(self.config, "vec_db", None):
            return None
        try:
            if callable(self._selector):
                selection: Any = self._selector(
                    goal, app_package=app_package, device_id=self._device_id()
                )
            else:
                selection = self._select_from_index(goal, app_package=app_package)
        except Exception:  # noqa: BLE001 - the procedure channel is fail-open
            return None
        if selection is None:
            return None
        self._record_stats(selection)
        if isinstance(selection, ProcedureSelection):
            return selection
        return ProcedureSelection(
            lesson_id=selection.get("lesson_id"),
            title=str(selection.get("title", "")),
            steps=tuple(str(step) for step in selection.get("steps", ()) or ()),
            pitfalls=selection.get("pitfalls"),
            app_scope=selection.get("app_scope"),
            device_scope=selection.get("device_scope"),
            score=float(selection.get("score", 0.0)),
            candidates=int(selection.get("candidates", 0)),
            filtered=int(selection.get("filtered", 0)),
            reason=str(selection.get("reason", "")),
        )

    def _select_from_index(
        self, goal: str, *, app_package: str | None
    ) -> ProcedureSelection:
        from phone_agent.v2.recall import MlxEmbedder, VecIndex

        embedder = self._embedder
        if embedder is None and callable(self._embedder_factory):
            embedder = self._embedder_factory()
        if embedder is None:
            embedder = MlxEmbedder(
                getattr(self.config, "embed_model", "Qwen/Qwen3-Embedding-0.6B"),
                int(getattr(self.config, "embed_dim", 1024)),
            )
        with VecIndex(
            getattr(self.config, "vec_db", "memory/vec.db"),
            embedder=embedder,
            selection_error_observers=self.selection_error_observers,
        ) as index:
            return index.select_procedure(
                goal, app_package=app_package, device_id=self._device_id()
            )

    def _record_stats(self, selection: Any) -> None:
        try:
            if callable(self._stats_recorder):
                self._stats_recorder(selection)
                return
            stats_path = (
                Path(getattr(self.config, "memory_dir", "memory"))
                / "experience/recall_stats.json"
            )
            update_procedure_recall_stats(stats_path, selection)
        except Exception:  # noqa: BLE001 - shadow statistics are fail-open
            return

    def _record_trace(
        self,
        point: str,
        lesson_ids: Sequence[str],
        *,
        app_package: str | None,
    ) -> None:
        record = getattr(self.trace, "record_event", None)
        if not callable(record):
            return
        try:
            record(
                "procedure_injection",
                point=point,
                lesson_ids=list(lesson_ids),
                count=len(lesson_ids),
                app_package=app_package,
            )
        except Exception:  # noqa: BLE001 - trace cannot change run semantics
            return


def build_procedure_injector(
    config: Any,
    *,
    session: Any = None,
    trace: Any = None,
    embedder_factory: Callable[[], Any] | None = None,
    selection_error_observers: SelectionErrorObservers | None = None,
) -> ProcedureCardInjector:
    """Build the WP-WF3 injector mounted by the recall capability."""

    return ProcedureCardInjector(
        config,
        session=session,
        trace=trace,
        embedder_factory=embedder_factory,
        selection_error_observers=selection_error_observers,
    )


__all__ = [
    "APP_RULES_PREFIX",
    "MENTION_PREFETCH_MAX_APPS",
    "PROCEDURE_CARD_PREFIX",
    "ProcedureCardInjector",
    "build_procedure_injector",
]
