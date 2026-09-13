"""Case model for live diagnosis: goal + preconditions + acceptance + safety.

A **Case** is the coding agent's (Pi / Codex / …) executable test intent. It is
plain data — never a workflow — and is deliberately independent of the run's
free-text goal:

* ``goal`` is what the run is asked to do (also the text handed to the agent);
* ``preconditions`` are the world facts assumed before the run (unlock state,
  language, connectivity); a violation is a *harness setup* problem, not an
  actor failure;
* ``acceptance`` is the list of checkpoints that decide whether the Case really
  passed. Acceptance is evaluated **only** against recorded evidence; a
  checkpoint with no evidence is ``unknown`` — never silently ``met``;
* ``safety_boundaries`` are what the run must not do. They are reported for the
  human reviewer; the actual enforcement remains the runtime safety layer
  (``V2Config.safety_mode``, default ``wary``).

Case files are JSON (``references/cases/*.json``). This module also carries the
built-in synthetic case used by the offline smoke test — no device, no model,
no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Acceptance status vocabulary. ``unknown`` is a first-class result: the run may
# have finished without producing evidence for this checkpoint.
STATUS_MET = "met"
STATUS_UNMET = "unmet"
STATUS_UNKNOWN = "unknown"

VALID_STATUSES = (STATUS_MET, STATUS_UNMET, STATUS_UNKNOWN)

# Case-level rollup.
CASE_PASS = "pass"
CASE_FAIL = "fail"
CASE_INCOMPLETE = "incomplete"
CASE_UNKNOWN = "unknown"

# Harness reason that means "a human/console requested stop", not a real
# takeover. Kept as a constant so state derivation and the report agree.
STOP_TAKEOVER_REASON = "用户从 Web 控制台停止"


@dataclass(frozen=True)
class Acceptance:
    """One acceptance checkpoint.

    ``match`` / ``contradict`` are literal substrings the evaluator searches for
    in the run's recorded evidence. Both default to empty:

    * a match found anywhere -> ``met`` (with the matching evidence refs);
    * only a contradict found -> ``unmet`` (positive counter-evidence);
    * neither -> ``unknown`` — absence of evidence is **not** failure.

    Matching is intentionally dumb and auditable. It never guesses, and it never
    converts "the run finished" into "this checkpoint passed".
    """

    id: str
    description: str
    match: tuple[str, ...] = ()
    contradict: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Acceptance":
        if not isinstance(payload, dict):
            raise ValueError("acceptance item must be an object")
        ident = str(payload.get("id") or "").strip()
        description = str(payload.get("description") or "").strip()
        if not ident:
            raise ValueError("acceptance item needs an id")
        if not description:
            raise ValueError(f"acceptance item {ident!r} needs a description")
        return cls(
            id=ident,
            description=description,
            match=tuple(_string_list(payload.get("match"))),
            contradict=tuple(_string_list(payload.get("contradict"))),
        )


@dataclass(frozen=True)
class Case:
    """A child Case: goal + preconditions + acceptance checkpoints + boundaries."""

    id: str
    title: str
    goal: str
    preconditions: tuple[str, ...] = ()
    acceptance: tuple[Acceptance, ...] = ()
    safety_boundaries: tuple[str, ...] = ()
    preconditions_confirmed: bool = False
    notes: str = ""
    source_path: str | None = None

    def task_text(self) -> str:
        """Compose the text handed to the actor: goal + constraints.

        This is not a new workflow or safety layer: it only makes the Case's
        preconditions and safety boundaries **visible to the actor** in the task
        string. Preconditions are labelled as unverified assumptions unless
        explicitly confirmed; boundaries are stated as constraints the actor
        must respect. The runtime safety layer is independent and does NOT read
        these Case-specific boundaries.
        """

        parts = [self.goal]
        if self.preconditions:
            label = "已人工确认" if self.preconditions_confirmed else "未经验证的假设"
            parts.append(f"前置条件（{label}）：" + "；".join(self.preconditions))
        if self.safety_boundaries:
            parts.append(
                "安全边界（任务约束，必须主动遵守；运行期 safety 层不会自动执行本条）："
                + "；".join(self.safety_boundaries)
            )
        return "\n".join(parts)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, source_path: str | None = None) -> "Case":
        if not isinstance(payload, dict):
            raise ValueError("case must be a JSON object")
        ident = str(payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise ValueError("case needs a non-empty goal")
        if not ident:
            ident = _slug(title or goal)
        if not title:
            title = goal
        acceptance = tuple(
            Acceptance.from_dict(item) for item in payload.get("acceptance") or []
        )
        return cls(
            id=ident,
            title=title,
            goal=goal,
            preconditions=tuple(_string_list(payload.get("preconditions"))),
            acceptance=acceptance,
            safety_boundaries=tuple(_string_list(payload.get("safety_boundaries"))),
            preconditions_confirmed=bool(payload.get("preconditions_confirmed", False)),
            notes=str(payload.get("notes") or ""),
            source_path=source_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "preconditions": list(self.preconditions),
            "acceptance": [
                {
                    "id": a.id,
                    "description": a.description,
                    "match": list(a.match),
                    "contradict": list(a.contradict),
                }
                for a in self.acceptance
            ],
            "safety_boundaries": list(self.safety_boundaries),
            "preconditions_confirmed": self.preconditions_confirmed,
            "notes": self.notes,
            "source_path": self.source_path,
        }


def load_case(path: str | Path) -> Case:
    """Load a Case from a JSON file (``.json``)."""

    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    return Case.from_dict(payload, source_path=str(p))


def case_from_target(target: str) -> Case:
    """Build an ad-hoc Case from a bare natural-language target.

    Used by the backward-compatible ``run_diagnosis.py "<target>"`` invocation:
    the target is the goal, with no preconditions / checkpoints. The report then
    honestly shows ``case_acceptance`` as ``unknown`` rather than pretending the
    run's own finish implies acceptance.
    """

    goal = str(target or "").strip()
    if not goal:
        raise ValueError("target must be non-empty")
    return Case(id=_slug(goal), title=goal, goal=goal, notes="ad-hoc Case（仅目标，无验收检查点）")


def synthetic_case() -> Case:
    """The offline smoke Case. Matches ``synthetic_run``'s scripted evidence."""

    return Case(
        id="synthetic-settings",
        title="合成冒烟：打开设置",
        goal="打开系统设置并确认设置页可见（合成离线用例，不接触设备）",
        preconditions=["合成会话，无真实设备", "脚本化模型，无网关访问"],
        acceptance=(
            Acceptance(
                id="A1",
                description="观测到设置应用在前台（objective OBS）",
                match=("com.android.settings",),
            ),
            Acceptance(
                id="A2",
                description="观测到设置页 mark（objective OBS）",
                match=("设置",),
            ),
        ),
        safety_boundaries=("不得点击恢复出厂设置", "不得修改网络凭据"),
        notes="该用例只用于离线管线冒烟，结果不代表任何真机能力。",
    )


def evaluate_acceptance(case: Case, evidence: "AcceptanceEvidence") -> list[dict[str, Any]]:
    """Evaluate every checkpoint against recorded evidence (never self-claimed).

    Only **objective observations** can prove a checkpoint ``met`` or supply a
    positive contradiction (``unmet``). Candidate sources (the TaskDoc evidence
    notes / facts and the actor's own finish claims) may only surface as
    ``unknown`` with their refs shown — they never auto-pass.

    If a checkpoint has both an objective match and an objective contradiction
    the result is ``unknown`` (a conflict must not be resolved in favour of
    pass). No objective evidence at all -> ``unknown``.

    Any structural failure yields ``unknown`` for the affected checkpoint
    rather than aborting the whole evaluation.
    """

    results: list[dict[str, Any]] = []
    objective = list(evidence.objective_pairs())
    candidate = list(evidence.candidate_pairs())
    for checkpoint in case.acceptance:
        status = STATUS_UNKNOWN
        refs: list[str] = []
        note = ""
        try:
            obj_matches = [
                ref for ref, text in objective if _contains_any(text, checkpoint.match)
            ]
            obj_contra = [
                ref
                for ref, text in objective
                if _contains_any(text, checkpoint.contradict)
            ]
            cand_matches = [
                ref for ref, text in candidate if _contains_any(text, checkpoint.match)
            ]
            if obj_matches and obj_contra:
                status = STATUS_UNKNOWN
                refs = (obj_matches + obj_contra)[:8]
                note = "客观证据中命中与反证并存，冲突不判通过"
            elif obj_matches:
                status = STATUS_MET
                refs = obj_matches[:8]
            elif obj_contra:
                status = STATUS_UNMET
                refs = obj_contra[:8]
            elif cand_matches:
                status = STATUS_UNKNOWN
                refs = cand_matches[:8]
                note = "仅有自述/任务板证据（候选），不足以客观判通过"
        except Exception:  # noqa: BLE001 - one bad checkpoint never aborts the rollup
            status = STATUS_UNKNOWN
            refs = []
        item = {
            "id": checkpoint.id,
            "description": checkpoint.description,
            "status": status,
            "evidence": refs,
        }
        if note:
            item["note"] = note
        results.append(item)
    return results


def rollup_acceptance(
    results: Iterable[dict[str, Any]], *, harness_finished: bool
) -> str:
    """Roll checkpoint results into a Case-level verdict.

    * any ``unmet`` -> ``fail`` (positive counter-evidence);
    * all checkpoints ``met`` -> ``pass``;
    * otherwise the harness was not finished -> ``incomplete``;
    * otherwise -> ``unknown`` (finished but some checkpoints lack evidence).

    Crucially, ``harness_finished`` never promotes ``unknown`` to ``pass``.
    """

    items = list(results)
    if not items:
        return CASE_UNKNOWN
    statuses = [item.get("status") for item in items]
    if STATUS_UNMET in statuses:
        return CASE_FAIL
    if all(status == STATUS_MET for status in statuses):
        return CASE_PASS
    if not harness_finished:
        return CASE_INCOMPLETE
    return CASE_UNKNOWN


KIND_OBJECTIVE = "objective"
KIND_CANDIDATE = "candidate"


class AcceptanceEvidence:
    """Typed acceptance corpus. Only ``objective`` entries can prove a checkpoint.

    Entries are ``(ref, text, kind)`` where ``kind`` is
    :data:`KIND_OBJECTIVE` (a real observation from the device) or
    :data:`KIND_CANDIDATE` (the TaskDoc's own notes/facts or the actor's finish
    claim). A 2-tuple defaults to ``candidate`` — the safe direction: missing a
    kind must never upgrade a self-claim to objective evidence.
    """

    def __init__(
        self,
        texts: Iterable[tuple],
        *,
        harness_finished: bool = False,
    ) -> None:
        pairs: list[tuple[str, str, str]] = []
        for item in texts:
            if not item:
                continue
            ref = str(item[0])
            text = str(item[1]) if len(item) > 1 else ""
            if not text.strip():
                continue
            kind = str(item[2]) if len(item) > 2 else KIND_CANDIDATE
            if kind not in (KIND_OBJECTIVE, KIND_CANDIDATE):
                kind = KIND_CANDIDATE
            pairs.append((ref, text, kind))
        self._pairs = pairs
        self.harness_finished = bool(harness_finished)

    def corpus_texts(self) -> list[tuple[str, str]]:
        return [(ref, text) for ref, text, _ in self._pairs]

    def objective_pairs(self) -> list[tuple[str, str]]:
        return [(ref, text) for ref, text, kind in self._pairs if kind == KIND_OBJECTIVE]

    def candidate_pairs(self) -> list[tuple[str, str]]:
        return [(ref, text) for ref, text, kind in self._pairs if kind == KIND_CANDIDATE]


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    if not needles:
        return False
    haystack = str(text)
    return any(needle and needle in haystack for needle in needles)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _slug(value: str) -> str:
    import re

    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(value or "").strip()).strip("-")
    return text[:60] or "case"


__all__ = [
    "STATUS_MET",
    "STATUS_UNMET",
    "STATUS_UNKNOWN",
    "CASE_PASS",
    "CASE_FAIL",
    "CASE_INCOMPLETE",
    "CASE_UNKNOWN",
    "STOP_TAKEOVER_REASON",
    "KIND_OBJECTIVE",
    "KIND_CANDIDATE",
    "Acceptance",
    "Case",
    "AcceptanceEvidence",
    "load_case",
    "case_from_target",
    "synthetic_case",
    "evaluate_acceptance",
    "rollup_acceptance",
]
