"""WP-WF3 procedure-card injection: two event-bus points, one card each.

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

Both points render through
:func:`phone_agent.v2.recall.format_procedure_block`, which labels the card a
non-binding historical reference and names its source lesson id. Every
selection — hit or zero-recall — is accumulated by
:func:`phone_agent.v2.recall.update_procedure_recall_stats`, and every injected
id is exposed through :attr:`injected_ids` for the trace/episode audit plane.
The whole channel is fail-open: a missing index, embedder, or stats file
disables injection for that point and never alters the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from phone_agent.v2.recall import (
    ProcedureSelection,
    format_procedure_block,
    update_procedure_recall_stats,
)

# Marker prefix for the post-launch one-shot block, kept distinct from the
# run-start ``[过程参考]`` card so the two points stay attributable in a trace.
PROCEDURE_CARD_PREFIX = "[PROCEDURE_CARD]"


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
    ) -> None:
        self.config = config
        self.session = session
        self.trace = trace
        self._embedder = embedder
        self._embedder_factory = embedder_factory
        self._selector = selector
        self._stats_recorder = stats_recorder
        # Run-scoped state, cleared by :meth:`reset` at every run boundary.
        self.goal: str = ""
        self.run_start_block: str | None = None
        self.run_start_selection: ProcedureSelection | None = None
        self._pending: tuple[str, str, str] | None = None
        self._injected_ids: list[str] = []
        self._selected_packages: set[str] = set()
        # Emergency revocations (P0 #18): a revoked card must not reach a later
        # injection point. Already-sent messages stay immutable.
        self._revoked: set[str] = set()

    # -- run lifecycle ---------------------------------------------------

    def reset(self) -> None:
        """Clear every per-run product of this injector (run boundary)."""

        self.goal = ""
        self.run_start_block = None
        self.run_start_selection = None
        self._pending = None
        self._injected_ids = []
        self._selected_packages = set()

    @property
    def injected_ids(self) -> list[str]:
        """Lesson ids actually handed to the model this run (audit plane)."""

        return list(self._injected_ids)

    @property
    def pending_lesson_id(self) -> str | None:
        """The lesson id waiting for the next ``model/pre_request``, if any."""

        return self._pending[0] if self._pending is not None else None

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

    # -- injection point one: run start (general cards) ------------------

    def run_start(self, goal: str) -> ProcedureSelection | None:
        """Select the run-start general card and render its prompt block.

        Only ``app_scope="general"`` cards qualify because no app is known yet
        (the selector's hard filter). ``shadow``/``off`` leave the block
        empty: the pre-existing shadow path owns the run-start measurement
        there, so selecting twice would double-count one run.
        """

        self.goal = str(goal or "").strip()
        self.run_start_block = None
        self.run_start_selection = None
        if not self.injects or not self.goal:
            return None
        selection = self._select(self.goal, app_package=None)
        self.run_start_selection = selection
        if selection is None or not selection.selected:
            return selection
        if self._revoked_selection(selection):
            return selection
        block = format_procedure_block(selection)
        if not block:
            return selection
        self.run_start_block = block
        self._injected_ids.append(str(selection.lesson_id))
        self._record_trace("run_start", [str(selection.lesson_id)], app_package=None)
        return selection

    # -- injection point two: after a confirmed launch -------------------

    def on_app_launched(self, payload: Any) -> None:
        """``app/launched`` listener: select the app-scoped card (WP-WF3)."""

        if not self.observes:
            return
        package = ""
        if isinstance(payload, Mapping):
            package = str(payload.get("package", "") or "").strip()
        if not package or package in self._selected_packages:
            return
        # One selection attempt per package per run: a later re-launch of the
        # same app has the same goal and the same index, so it cannot produce a
        # different card and must not inject twice.
        self._selected_packages.add(package)
        goal = self.goal or self._goal_from_session()
        if not goal:
            return
        selection = self._select(goal, app_package=package)
        if selection is None or not selection.selected or not self.injects:
            return
        if self._revoked_selection(selection):
            return
        block = format_procedure_block(selection)
        if not block:
            return
        # Replacing a still-pending card keeps one card in flight: two launches
        # before the next model call must never queue two blocks.
        self._pending = (str(selection.lesson_id), block, package)

    def make_delta(self, messages: Sequence[Any]) -> list[Any] | None:
        """Consume the pending card as one trailing system message (or ``None``)."""

        pending = self._pending
        if pending is None or pending[0] in self._revoked:
            self._pending = None
            return None
        self._pending = None
        lesson_id, block, package = pending
        try:
            from langchain_core.messages import SystemMessage

            delta = [
                *messages,
                SystemMessage(content=f"{PROCEDURE_CARD_PREFIX}\n{block}"),
            ]
        except Exception:  # noqa: BLE001 - injection is fail-open
            return None
        self._injected_ids.append(lesson_id)
        self._record_trace("app_launched", [lesson_id], app_package=package)
        return delta

    def on_pre_request(self, messages: Any, next: Callable[[Any], Any]) -> Any:
        """``model/pre_request`` waterfall listener: inject the pending card."""

        delta = self.make_delta(messages)
        if delta is None:
            return next(messages)
        return next(delta)

    # -- internals -------------------------------------------------------

    def revoke(self, lesson_id: str) -> None:
        """Exclude one card from every later injection point (P0 #18)."""

        clean = str(lesson_id or "").strip()
        if not clean:
            return
        self._revoked.add(clean)
        if self._pending is not None and self._pending[0] == clean:
            self._pending = None
        if self.run_start_selection is not None and (
            self.run_start_selection.lesson_id == clean
        ):
            self.run_start_block = None

    def _revoked_selection(self, selection: ProcedureSelection | None) -> bool:
        return (
            selection is not None
            and str(selection.lesson_id or "") in self._revoked
        )

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
            getattr(self.config, "vec_db", "memory/vec.db"), embedder=embedder
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
) -> ProcedureCardInjector:
    """Build the WP-WF3 injector mounted by the recall capability."""

    return ProcedureCardInjector(
        config,
        session=session,
        trace=trace,
        embedder_factory=embedder_factory,
    )


__all__ = [
    "PROCEDURE_CARD_PREFIX",
    "ProcedureCardInjector",
    "build_procedure_injector",
]
