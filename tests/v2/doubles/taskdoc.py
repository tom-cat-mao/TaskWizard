"""The TaskDoc double: the spec §2.1 board surface (goal + route + facts).

One version for every test that needs a board — the five per-file copies had
drifted in three ways (an extra ``reason``/``evidence_note`` on the item, a
rendered ``## 关键事实`` section, and two different item line formats). The union
below keeps every consumer's observable output:

* ``render`` emits the ``- [{status}] {id}: {content}`` line used by the
  full-board renderer and by the flow-line section the middleware appends;
* ``open_items_summary`` keeps the ``1:打开设置[completed]`` shape the finish
  guard asserts on;
* a bare ``FakeTaskDoc()`` renders ``""``, so a session that only needs "a board
  object exists" does not silently start injecting a preview block.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeTaskItem:
    id: str
    content: str
    status: str = "pending"
    reason: str | None = None
    evidence_note: str | None = None


@dataclass
class FakeTaskDoc:
    goal_base: str = ""
    amendments: list[str] = field(default_factory=list)
    items: list[FakeTaskItem] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)

    def validate(self) -> str | None:
        return None

    def has_open_items(self) -> bool:
        return any(i.status in {"pending", "in_progress"} for i in self.items)

    def open_items_summary(self) -> str:
        return "; ".join(
            f"{i.id}:{i.content}[{i.status}]"
            for i in self.items
            if i.status in {"pending", "in_progress"}
        )

    def render(self, lang: str = "cn") -> str:  # noqa: ARG002
        if not (self.goal_base or self.items or self.facts):
            return ""
        lines = ["## 目标", f"base: {self.goal_base}"]
        if self.items:
            lines.append("## 路线")
            for item in self.items:
                lines.append(f"- [{item.status}] {item.id}: {item.content}")
        if self.facts:
            lines.append("## 关键事实")
            lines.extend(f"- {fact}" for fact in self.facts)
        return "\n".join(lines)


__all__ = ["FakeTaskDoc", "FakeTaskItem"]
