"""TaskDoc: the unified goal + plan + facts task board (thin-loop v2 增量一).

The TaskDoc is a *single* document with three sections — 目标 (goal), 路线
(plan), 关键事实 (facts). It lives on the :class:`~phone_agent.v2.session.PhoneSession`
and is mutated *only* through the ``update_task_doc`` tool (the model is the sole
writer). The harness seeds ``goal_base`` at run start; the model never rewrites
it. A ``before_model`` middleware renders the doc as a pinned block so it is
immune to context compaction.

See ``AGENTS.md`` §1-§2.1 for the binding contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Section limits / vocabulary (spec §1).
MAX_ITEMS = 15
MAX_FACTS = 10
MAX_FACT_LEN = 120
OPEN_STATUSES = ("pending", "in_progress")
VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")
# Structural budgets (S3 hardening): the pinned block is compression-immune,
# so every field the model writes is length-capped to keep the board small.
MAX_AMENDMENTS = 10
MAX_AMENDMENT_LEN = 500
MAX_ITEM_CONTENT_LEN = 500
MAX_EVIDENCE_LEN = 500
# Total rendered budget for the pinned block (S3 hardening).  ``render()``
# tail-truncates the truncatable sections (amendments/facts) with a visible
# marker when the output would exceed it; ``goal_base`` and route items are
# never truncated, so the guarantee is best-effort when the protected content
# alone exceeds the budget.
MAX_RENDER_CHARS = 4000
_RENDER_TRUNCATION_MARKER = "…(已截断)"


def _is_cn(lang: str) -> bool:
    """Return True for Chinese language codes (mirrors ``prompts.get_system_prompt``)."""

    return (lang or "").strip().lower() in {"cn", "zh", "zh-cn", "zh_cn", "chinese"}


@dataclass
class TaskItem:
    """One 路线 milestone.

    ``status`` is one of :data:`VALID_STATUSES`; ``reason`` is required only when
    ``status == "blocked"`` (why the item is stuck). ``evidence_note`` is required
    only when ``status == "completed"`` (the on-screen proof for the completion),
    feeding the finish review packet / verifier with a trustworthy evidence source.
    """

    id: str
    content: str
    status: str = "pending"
    reason: str | None = None
    evidence_note: str | None = None


@dataclass
class TaskDoc:
    """The unified goal + plan + facts board. Mutated only via ``update_task_doc``.

    - ``goal_base``: the user's original task text; harness-seeded, never rewritten.
    - ``amendments``: append-only refinements (model understanding / user additions).
    - ``items``: the 路线 milestones (full-replaced on each tool write).
    - ``facts``: short model notes (prices / chosen values / gotchas).
    """

    goal_base: str = ""
    amendments: list[str] = field(default_factory=list)
    items: list[TaskItem] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    # -- validation -------------------------------------------------------

    def validate(self, previous: "TaskDoc | None" = None) -> str | None:
        """Return ``None`` when valid, else a human-readable error string.

        Enforces the spec §1 structural constraints: at most one ``in_progress``
        item; at most :data:`MAX_ITEMS` items; every ``blocked`` item carries a
        ``reason``; every ``completed`` item carries an ``evidence_note`` (S2 §2);
        only known statuses; at most :data:`MAX_FACTS` facts, each at most
        :data:`MAX_FACT_LEN` characters.

        S3 hardening keeps the pinned board small and explainable (the model
        keeps its planning rights): item ids are unique; item content is at
        most :data:`MAX_ITEM_CONTENT_LEN` characters; ``evidence_note`` at most
        :data:`MAX_EVIDENCE_LEN`; at most :data:`MAX_AMENDMENTS` amendments,
        each at most :data:`MAX_AMENDMENT_LEN`.

        When ``previous`` (the task board *before* this write) is supplied, the
        A4 **state-transition** disciplines are additionally enforced against it:

        * A single item jumping ``pending`` → ``completed`` (skipping
          ``in_progress``) is rejected — the model must first mark the item
          ``in_progress`` (proof it actually worked the step), then complete it.
        * Marking **multiple** ``pending`` items ``completed`` in one call
          (batch back-filling to slip past the finish gate) is rejected — items
          must be advanced one at a time.
        * A previously-``pending`` id that vanished from the board is
          rejected — pending route items are transitioned (``blocked`` /
          ``completed``), never silently deleted.
        * A brand-new id that first appears already-``completed`` is rejected —
          new items must enter as ``pending``/``in_progress`` so every
          completion carries a real transition history.
        """

        if len(self.items) > MAX_ITEMS:
            return f"路线项过多：{len(self.items)} 项，至多 {MAX_ITEMS} 项。"

        seen_ids: set[str] = set()
        in_progress = 0
        for item in self.items:
            if item.status not in VALID_STATUSES:
                return (
                    f"未知状态 {item.status!r}（item {item.id!r}）；"
                    f"合法状态：{', '.join(VALID_STATUSES)}。"
                )
            if item.id in seen_ids:
                return f"路线项 id 重复：{item.id!r}。"
            seen_ids.add(item.id)
            if len(item.content) > MAX_ITEM_CONTENT_LEN:
                return (
                    f"路线项内容过长（>{MAX_ITEM_CONTENT_LEN} 字符）："
                    f"{item.content[:20]!r}…（item {item.id!r}）。"
                )
            if item.status == "in_progress":
                in_progress += 1
            if item.status == "blocked" and not (item.reason or "").strip():
                return f"blocked 项 {item.id!r} 必须带 reason（说明为何卡住）。"
            if item.status == "completed":
                if not (item.evidence_note or "").strip():
                    return f"completed 项 {item.id!r} 缺少 evidence_note（完成项需给出屏幕证据）。"
                if len(item.evidence_note) > MAX_EVIDENCE_LEN:
                    return (
                        f"evidence_note 过长（>{MAX_EVIDENCE_LEN} 字符）："
                        f"item {item.id!r}。"
                    )
        if in_progress > 1:
            return f"至多一个 in_progress 项，当前有 {in_progress} 个。"

        if len(self.amendments) > MAX_AMENDMENTS:
            return f"补充条目过多：{len(self.amendments)} 条，至多 {MAX_AMENDMENTS} 条。"
        for amendment in self.amendments:
            if len(amendment) > MAX_AMENDMENT_LEN:
                return (
                    f"补充条目过长（>{MAX_AMENDMENT_LEN} 字符）："
                    f"{amendment[:20]!r}…"
                )

        if len(self.facts) > MAX_FACTS:
            return f"关键事实过多：{len(self.facts)} 条，至多 {MAX_FACTS} 条。"
        for fact in self.facts:
            if len(fact) > MAX_FACT_LEN:
                return f"关键事实过长（>{MAX_FACT_LEN} 字符）：{fact[:20]!r}…"

        if previous is not None:
            transition_error = self._validate_transitions(previous)
            if transition_error is not None:
                return transition_error
        return None

    def _validate_transitions(self, previous: "TaskDoc") -> str | None:
        """Enforce the A4 transition discipline against the pre-write board.

        * ``pending`` → ``completed`` must pass through ``in_progress``; batch
          pending→completed marking is rejected (see ``validate``).
        * A previously-``pending`` id that vanished is a silent deletion of a
          route item — rejected.
        * A brand-new id that first appears as ``completed`` skips its entry
          transition — rejected.
        """

        prior_status = {item.id: item.status for item in previous.items}
        current_status = {item.id: item.status for item in self.items}

        vanished = [
            item_id
            for item_id, status in prior_status.items()
            if status == "pending" and item_id not in current_status
        ]
        if vanished:
            ids = "、".join(repr(i) for i in sorted(vanished))
            return (
                f"不允许删除 pending 路线项：{ids}。"
                "路线项只转状态不删除：不做了请标 blocked（附 reason），"
                "完成请标 completed（附 evidence_note）。"
            )

        jumped: list[str] = []
        for item in self.items:
            if item.status != "completed":
                continue
            # Only items that existed before are subject to the jump discipline.
            if prior_status.get(item.id) == "pending":
                jumped.append(item.id)

        if len(jumped) >= 2:
            ids = "、".join(repr(i) for i in jumped)
            return (
                f"不允许一次把多项 pending 直接标 completed（批量补标）：{ids}。"
                "请逐项推进：先把当前项标 in_progress、完成后再标 completed（附 evidence_note）。"
            )
        if len(jumped) == 1:
            return (
                f"不允许把 pending 项 {jumped[0]!r} 直接标 completed。"
                "请先标 in_progress（表示正在做该步），完成后再标 completed（附 evidence_note）。"
            )

        fresh_completed = [
            item.id
            for item in self.items
            if item.status == "completed" and item.id not in prior_status
        ]
        if fresh_completed:
            ids = "、".join(repr(i) for i in sorted(fresh_completed))
            return (
                f"新路线项不能直接以 completed 进入路线：{ids}。"
                "新项请先以 pending（或 in_progress）建立，完成后再标 completed"
                "（附 evidence_note）。"
            )
        return None

    # -- open-item queries (finish gate uses these) -----------------------

    def has_open_items(self) -> bool:
        """True if any item is ``pending`` or ``in_progress``."""

        return any(item.status in OPEN_STATUSES for item in self.items)

    def open_items_summary(self) -> str:
        """One-line summary of open items, for the finish rejection message."""

        return "；".join(
            f"{item.id}:{item.content}[{item.status}]"
            for item in self.items
            if item.status in OPEN_STATUSES
        )

    # -- rendering (pinned block) -----------------------------------------

    def render(self, lang: str = "cn") -> str:
        """Render as a pinned block. Empty doc -> ``""``; empty sections omitted.

        A doc is *empty* only when it has no ``goal_base``, no amendments, no
        items, and no facts. Any populated section is rendered under its header;
        blank sections (including 路线/关键事实) are skipped.

        The output is kept within :data:`MAX_RENDER_CHARS`: when it would grow
        beyond the budget, the truncatable tail (amendments, then facts) is
        dropped entry by entry — whole lines, earliest entries kept — and a
        visible ``…(已截断)`` marker is appended.  ``goal_base`` and route
        items are **never** truncated, so the budget holds whenever the
        protected content alone fits.  Pure and deterministic.
        """

        if not (self.goal_base.strip() or self.amendments or self.items or self.facts):
            return ""

        cn = _is_cn(lang)
        lines: list[str] = []
        # ``truncatable[i]`` marks the entries the render budget may drop:
        # amendment and fact lines.  Headers, ``goal_base`` and route items
        # are protected.
        truncatable: list[bool] = []

        if self.goal_base.strip() or self.amendments:
            lines.append("## 目标" if cn else "## Goal")
            truncatable.append(False)
            if self.goal_base.strip():
                lines.append(f"base: {self.goal_base}")
                truncatable.append(False)
            if self.amendments:
                lines.append("补充：" if cn else "Amendments:")
                truncatable.append(False)
                for amendment in self.amendments:
                    lines.append(f"- {amendment}")
                    truncatable.append(True)

        if self.items:
            lines.append("## 路线" if cn else "## Plan")
            truncatable.append(False)
            for item in self.items:
                suffix = ""
                if item.status == "blocked" and item.reason:
                    suffix = (
                        f"（原因：{item.reason}）" if cn else f" (reason: {item.reason})"
                    )
                elif item.status == "completed" and (item.evidence_note or "").strip():
                    suffix = (
                        f"（证据：{item.evidence_note}）"
                        if cn
                        else f" (evidence: {item.evidence_note})"
                    )
                lines.append(f"- [{item.status}] {item.id}: {item.content}{suffix}")
                truncatable.append(False)

        if self.facts:
            lines.append("## 关键事实" if cn else "## Key Facts")
            truncatable.append(False)
            for fact in self.facts:
                lines.append(f"- {fact}")
                truncatable.append(True)

        rendered = "\n".join(lines)
        if len(rendered) <= MAX_RENDER_CHARS:
            return rendered
        return self._render_bounded(lines, truncatable)

    def _render_bounded(self, lines: list[str], truncatable: list[bool]) -> str:
        """Drop truncatable tail entries until the render (plus marker) fits.

        Lines are removed back-to-front; protected lines (headers, goal_base,
        route items) are never removed.  If the protected content alone
        exceeds :data:`MAX_RENDER_CHARS`, the output may still exceed it —
        protected content is never cut.
        """

        budget = MAX_RENDER_CHARS - len(_RENDER_TRUNCATION_MARKER) - 1

        def total(candidate: list[str]) -> int:
            return sum(len(line) + 1 for line in candidate) - 1

        dropped = False
        # Back-to-front: popping at ``index`` never shifts lower indices, so
        # the walk is safe while lines shrink.
        for index in range(len(lines) - 1, -1, -1):
            if total(lines) <= budget:
                break
            if truncatable[index]:
                lines.pop(index)
                dropped = True

        result = "\n".join(lines)
        if dropped:
            result = f"{result}\n{_RENDER_TRUNCATION_MARKER}"
        return result
