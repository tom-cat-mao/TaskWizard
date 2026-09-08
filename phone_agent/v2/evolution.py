"""Offline lesson distillation and human-governed promotion.

The mutation path is deliberately outside the actor hot path: it reads full
episode outcomes and emits proposal/review events.  The sole runtime bridge is a
bounded, read-only selector for approved lessons; actor message construction
remains owned by :mod:`phone_agent.v2.agent`.
events.jsonl is authoritative; lessons.json is a rebuildable current-version
view.

WP-DISTILL-AUTO (distillation end-to-end without mechanical anchoring): the
harness is a fact-checker, fact supplier, and bookkeeper — never the semantic
judge.  One batch costs two model calls (both charged to the ``distill`` role):

* call 1 proposes candidates from episode cards and their ``struggle_markers``;
  the model emits semantics only (text/kind/evidence/scope/task_keys and the
  procedure extras) and every bookkeeping field is filled here;
* call 2 self-grades **every** candidate (rules and procedure cards alike) over
  a harness-computed fact sheet.  The only two verdicts are ``auto_approved``
  and ``needs_review``; transport, budget, and malformed-reply failures are
  fail-open and land candidates at ``needs_review`` rather than dropping them.

The sole remaining human gate is the CLI (approve/revoke/supersede) plus dream's
evidence-loss demotion; contradictions are surfaced to the grader through
``conflicts_with_approved`` instead of blocking anything mechanically.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from phone_agent.v2.middleware._tokens import (
    estimate_context_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
)
from phone_agent.v2.usage import UsageLedger

LESSON_EVENT_TYPES = frozenset(
    {
        "lesson_proposed",
        "lesson_approved",
        "lesson_revoked",
        "lesson_demoted",
        "lesson_superseded",
    }
)
LESSON_KINDS = frozenset({"rule", "procedure"})
LESSON_STATUSES = frozenset(
    {
        "proposed",
        "needs_review",
        "auto_approved",
        "approved",
        "revoked",
        "superseded",
    }
)
# States a distiller may file a new proposal in.  ``auto_approved`` is reached
# only by a graded procedure card (WP-WF1) and is the sole proposal state that
# is injectable without a human decision.
_PROPOSAL_STATUSES = frozenset({"proposed", "needs_review", "auto_approved"})
_APPROVABLE_STATUSES = frozenset({"proposed", "needs_review"})
_DEMOTABLE_STATUSES = frozenset({"approved", "auto_approved"})
_GRADES = frozenset({"auto_approved", "needs_review"})
GENERAL_APP_SCOPE = "general"
LESSON_FIELDS = (
    "lesson_id",
    "schema_v",
    "version",
    "status",
    "text",
    "scope",
    "evidence",
    "support_count",
    "task_keys",
    "conflicts",
    "created_ts",
    "source",
)
# WP-WF1 procedure-card fields.  They stay optional so every pre-WF1 lesson and
# event object validates unchanged (missing means rule/empty/None/None).
OPTIONAL_LESSON_FIELDS = frozenset({"kind", "steps", "pitfalls", "app_scope"})
_SCOPE_FIELDS = frozenset({"device", "app", "app_version"})
_EVIDENCE_FIELDS = frozenset({"run_id", "note"})
_LESSON_ID = re.compile(r"^les_[0-9a-f]{12,64}$")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")

_TASK_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("search_flight", ("机票", "航班", "flight")),
    ("search_hotel", ("酒店", "hotel")),
    ("open_app", ("打开", "启动", "open", "launch")),
    ("search", ("查询", "搜索", "查找", "search", "find")),
    ("send_message", ("发送", "消息", "send", "message")),
    ("change_setting", ("设置", "wifi", "wlan", "蓝牙", "setting")),
)
_NEGATIONS = (
    "不要",
    "禁止",
    "避免",
    "不得",
    "不能",
    "不应该",
    "不应",
    "切勿",
    "never",
    "do not",
    "don't",
    "should not",
    "must not",
)
_MODALS = ("应该", "应当", "必须", "应", "always", "should", "must")


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _nullable_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"scope.{field} must be a non-empty string or null")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != _single_line(item):
            raise ValueError(f"{field} values must be non-empty normalized strings")
        if item in result:
            raise ValueError(f"{field} values must be unique")
        result.append(item)
    return result


def _nullable_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string or null")
    return value.strip()


def _step_list(value: Any) -> list[str]:
    """Validate ordered procedure steps; repeated steps stay verbatim."""

    if not isinstance(value, list):
        raise ValueError("steps must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != _single_line(item):
            raise ValueError("steps values must be non-empty normalized strings")
        result.append(item)
    return result


@dataclass(frozen=True)
class LessonCandidate:
    """Strict, versioned lesson proposal schema.

    Two artifact shapes share this record (WP-WF1): ``kind="rule"`` is the
    original single-sentence behavioural rule, ``kind="procedure"`` is a
    multi-step process card whose steps are purely semantic (no coordinates,
    mark ids, or verbatim tool arguments).  The procedure fields are optional
    so every pre-WF1 record keeps validating.
    """

    lesson_id: str
    schema_v: int
    version: int
    status: str
    text: str
    scope: dict[str, str | None]
    evidence: list[dict[str, str]]
    support_count: int
    task_keys: list[str]
    conflicts: list[str]
    created_ts: float
    source: str
    kind: str = "rule"
    steps: list[str] = field(default_factory=list)
    pitfalls: str | None = None
    app_scope: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LessonCandidate":
        """Validate an exact schema object without accepting extra fields."""

        if not isinstance(payload, Mapping):
            raise TypeError("lesson candidate must be an object")
        missing = sorted(set(LESSON_FIELDS) - set(payload))
        extra = sorted(set(payload) - set(LESSON_FIELDS) - OPTIONAL_LESSON_FIELDS)
        if missing or extra:
            raise ValueError(f"lesson fields mismatch: missing={missing}, extra={extra}")

        kind = payload.get("kind", "rule")
        if not isinstance(kind, str) or kind not in LESSON_KINDS:
            raise ValueError(f"kind must be one of {sorted(LESSON_KINDS)!r}")
        raw_steps = payload.get("steps")
        steps = _step_list(raw_steps) if raw_steps is not None else []
        raw_pitfalls = payload.get("pitfalls")
        if isinstance(raw_pitfalls, list):
            # Models naturally emit pitfalls as an array; join into the
            # schema's single-string form instead of dropping the card.
            raw_pitfalls = "；".join(
                s for s in (str(i).strip() for i in raw_pitfalls) if s
            ) or None
        pitfalls = _nullable_str(raw_pitfalls, "pitfalls")
        app_scope = _nullable_str(payload.get("app_scope"), "app_scope")
        if kind == "procedure":
            if not steps:
                raise ValueError("procedure steps must be a non-empty array")
            if app_scope is None:
                raise ValueError(
                    f"procedure app_scope must be a non-empty string or"
                    f" '{GENERAL_APP_SCOPE}'"
                )
        else:
            if steps:
                raise ValueError("rule lessons must not carry steps")
            if app_scope is not None:
                raise ValueError("rule lessons must not carry app_scope")

        lesson_id = payload["lesson_id"]
        if not isinstance(lesson_id, str) or not _LESSON_ID.fullmatch(lesson_id):
            raise ValueError("lesson_id must be les_<12-64 lowercase hex chars>")
        schema_v = payload["schema_v"]
        if not isinstance(schema_v, int) or isinstance(schema_v, bool) or schema_v != 1:
            raise ValueError("schema_v must be 1")
        version = payload["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("version must be a positive integer")
        status = payload["status"]
        if not isinstance(status, str) or status not in LESSON_STATUSES:
            raise ValueError(f"status must be one of {sorted(LESSON_STATUSES)!r}")
        text = payload["text"]
        if not isinstance(text, str) or not text.strip() or text != _single_line(text):
            raise ValueError("text must be one non-empty normalized line")

        raw_scope = payload["scope"]
        if not isinstance(raw_scope, Mapping) or set(raw_scope) != _SCOPE_FIELDS:
            raise ValueError("scope must contain exactly device/app/app_version")
        scope = {
            "device": _nullable_text(raw_scope["device"], "device"),
            "app": _nullable_text(raw_scope["app"], "app"),
            "app_version": _nullable_text(
                raw_scope["app_version"], "app_version"
            ),
        }
        if scope["app"] is not None and not _PACKAGE.fullmatch(scope["app"]):
            raise ValueError("scope.app must be a package name or null")

        raw_evidence = payload["evidence"]
        if not isinstance(raw_evidence, list):
            raise ValueError("evidence must be an array")
        evidence: list[dict[str, str]] = []
        seen_runs: set[str] = set()
        for item in raw_evidence:
            # The model may cite a bare run_id string; the canonical evidence
            # rebuild overwrites notes from episode outcomes anyway, so accept
            # both shapes instead of dropping an otherwise-valid candidate.
            if isinstance(item, str):
                item = {"run_id": item, "note": "cited by distill"}
            if not isinstance(item, Mapping) or set(item) != _EVIDENCE_FIELDS:
                raise ValueError("each evidence item must contain run_id and note")
            run_id = item["run_id"]
            note = item["note"]
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("evidence.run_id must be a non-empty string")
            if run_id in seen_runs:
                raise ValueError("evidence.run_id values must be unique")
            if (
                not isinstance(note, str)
                or not note.strip()
                or note != _single_line(note)
            ):
                raise ValueError("evidence.note must be a non-empty normalized line")
            seen_runs.add(run_id)
            evidence.append({"run_id": run_id, "note": note})

        support_count = payload["support_count"]
        if (
            not isinstance(support_count, int)
            or isinstance(support_count, bool)
            or support_count < 0
        ):
            raise ValueError("support_count must be a non-negative integer")
        if support_count != len(evidence):
            raise ValueError("support_count must equal the unique evidence count")

        task_keys = _string_list(payload["task_keys"], "task_keys")
        conflicts = _string_list(payload["conflicts"], "conflicts")
        if isinstance(payload["created_ts"], bool):
            raise ValueError("created_ts must be a finite non-negative number")
        try:
            created_ts = float(payload["created_ts"])
        except (TypeError, ValueError) as exc:
            raise ValueError("created_ts must be a finite non-negative number") from exc
        if not math.isfinite(created_ts) or created_ts < 0:
            raise ValueError("created_ts must be a finite non-negative number")
        if payload["source"] != "distill":
            raise ValueError("source must be 'distill'")

        return cls(
            lesson_id=lesson_id,
            schema_v=1,
            version=version,
            status=status,
            text=text,
            scope=scope,
            evidence=evidence,
            support_count=support_count,
            task_keys=task_keys,
            conflicts=conflicts,
            created_ts=created_ts,
            source="distill",
            kind=kind,
            steps=steps,
            pitfalls=pitfalls,
            app_scope=app_scope,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible object in schema field order."""

        return {
            "lesson_id": self.lesson_id,
            "schema_v": self.schema_v,
            "version": self.version,
            "status": self.status,
            "text": self.text,
            "scope": dict(self.scope),
            "evidence": [dict(item) for item in self.evidence],
            "support_count": self.support_count,
            "task_keys": list(self.task_keys),
            "conflicts": list(self.conflicts),
            "created_ts": self.created_ts,
            "source": self.source,
            "kind": self.kind,
            "steps": list(self.steps),
            "pitfalls": self.pitfalls,
            "app_scope": self.app_scope,
        }


def make_lesson_id(text: str, scope: Mapping[str, Any]) -> str:
    """Return a stable semantic id for a lesson text and scope."""

    payload = json.dumps(
        {"text": _single_line(text).casefold(), "scope": dict(scope)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "les_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _demoted_status(candidate: LessonCandidate) -> str:
    """Return the landing status of a demotion.

    A rule goes back to ``proposed`` (human re-approval required); a procedure
    card lands in ``needs_review`` so a graded card never silently re-enters the
    review queue as a plain proposal.
    """

    return "needs_review" if candidate.kind == "procedure" else "proposed"


def _replay_lesson_events(events_path: Path) -> dict[str, LessonCandidate]:
    current: dict[str, LessonCandidate] = {}
    if not events_path.exists():
        return current
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                kind = event["type"]
                if kind not in LESSON_EVENT_TYPES or event.get("schema_v") != 1:
                    continue
                if kind in {"lesson_proposed", "lesson_superseded"}:
                    candidate = LessonCandidate.from_dict(event["lesson"])
                    if candidate.status not in _PROPOSAL_STATUSES:
                        continue
                    if kind == "lesson_proposed":
                        if candidate.lesson_id in current or candidate.version != 1:
                            continue
                    else:
                        prior = current.get(candidate.lesson_id)
                        if (
                            prior is None
                            or event.get("lesson_id") != candidate.lesson_id
                            or event.get("from_version") != prior.version
                            or candidate.version != prior.version + 1
                        ):
                            continue
                    current[candidate.lesson_id] = candidate
                    continue
                lesson_id = str(event["lesson_id"])
                candidate = current.get(lesson_id)
                if candidate is None or event.get("version") != candidate.version:
                    continue
                if kind == "lesson_approved" and candidate.status in _APPROVABLE_STATUSES:
                    current[lesson_id] = replace(candidate, status="approved")
                elif kind == "lesson_revoked" and candidate.status != "revoked":
                    current[lesson_id] = replace(candidate, status="revoked")
                elif (
                    kind == "lesson_demoted"
                    and candidate.status in _DEMOTABLE_STATUSES
                ):
                    # Withdrawn at the same version; there is no reinstatement
                    # path, so a revoked lesson stays revoked.
                    current[lesson_id] = replace(
                        candidate, status=_demoted_status(candidate)
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return current


class LessonStore:
    """Append lesson lifecycle events and maintain their current-version view."""

    def __init__(self, lessons_dir: str | os.PathLike[str] = "memory/lessons") -> None:
        self.root = Path(lessons_dir)
        self.events_path = self.root / "events.jsonl"
        self.lessons_path = self.root / "lessons.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)
        self._lessons = _replay_lesson_events(self.events_path)
        self._write_view()

    def lessons(self, *, status: str | None = None) -> list[LessonCandidate]:
        with self._lock:
            values = [
                candidate
                for candidate in self._lessons.values()
                if status is None or candidate.status == status
            ]
        return sorted(values, key=lambda item: (item.created_ts, item.lesson_id))

    def get(self, lesson_id: str) -> LessonCandidate | None:
        with self._lock:
            return self._lessons.get(lesson_id)

    def propose(
        self,
        candidate: LessonCandidate,
        *,
        grade: str | None = None,
        grade_basis: str | None = None,
        fact_sheet: Mapping[str, Any] | None = None,
    ) -> LessonCandidate:
        """Append one proposal, optionally with its WP-WF1 grading audit trail.

        ``grade``/``grade_basis``/``fact_sheet`` are event-only provenance: they
        never enter the lesson schema, but they stay in ``events.jsonl`` so a
        later audit can see why a procedure card was auto-approved.
        """

        candidate = LessonCandidate.from_dict(candidate.to_dict())
        if candidate.status not in _PROPOSAL_STATUSES:
            raise ValueError("new lesson candidates must be proposed")
        with self._lock:
            prior = self._lessons.get(candidate.lesson_id)
            if prior is None:
                if candidate.version != 1:
                    raise ValueError("a new lesson must start at version 1")
                self._append(
                    self._proposal_event(
                        "lesson_proposed",
                        candidate,
                        grade=grade,
                        grade_basis=grade_basis,
                        fact_sheet=fact_sheet,
                    )
                )
                saved = candidate
            elif _proposal_payload(prior) == _proposal_payload(candidate):
                return prior
            else:
                # A changed evidence/conflict payload is a new proposal version,
                # even when v1 was approved; it still requires another human
                # approval and can never inherit approved status automatically.
                # A freshly graded procedure card keeps the verdict it was filed
                # with (proposed, needs_review, or auto_approved).
                saved = replace(candidate, version=prior.version + 1)
                self._append(
                    self._proposal_event(
                        "lesson_superseded",
                        saved,
                        lesson_id=prior.lesson_id,
                        from_version=prior.version,
                        grade=grade,
                        grade_basis=grade_basis,
                        fact_sheet=fact_sheet,
                    )
                )
            self._lessons[saved.lesson_id] = saved
            self._write_view()
            return saved

    def approve(self, lesson_id: str) -> LessonCandidate:
        """Promote a proposed or very-uncertain (``needs_review``) proposal."""

        with self._lock:
            candidate = self._require(lesson_id)
            if candidate.status not in _APPROVABLE_STATUSES:
                raise ValueError("only a proposed or needs_review lesson can be approved")
            approved = replace(candidate, status="approved")
            self._append(
                {
                    "type": "lesson_approved",
                    "schema_v": 1,
                    "ts": time.time(),
                    "lesson_id": lesson_id,
                    "version": candidate.version,
                }
            )
            self._lessons[lesson_id] = approved
            self._write_view()
            return approved

    def revoke(self, lesson_id: str, reason: str) -> LessonCandidate:
        clean_reason = _single_line(reason)
        if not clean_reason:
            raise ValueError("revoke reason must not be empty")
        with self._lock:
            candidate = self._require(lesson_id)
            if candidate.status == "revoked":
                raise ValueError("lesson is already revoked")
            revoked = replace(candidate, status="revoked")
            self._append(
                {
                    "type": "lesson_revoked",
                    "schema_v": 1,
                    "ts": time.time(),
                    "lesson_id": lesson_id,
                    "version": candidate.version,
                    "reason": clean_reason,
                }
            )
            self._lessons[lesson_id] = revoked
            self._write_view()
            return revoked

    def demote(self, lesson_id: str, reason: str) -> LessonCandidate:
        """Withdraw an injectable lesson, keeping its version.

        This is the evidence-loss counterpart of :meth:`approve`: a lesson whose
        cited episodes no longer satisfy Rule-of-3 stops being injectable and
        needs another human approval — including an ``auto_approved`` procedure
        card, which lands in ``needs_review`` instead of ``proposed``.
        Revoked lessons are never reinstated.
        """

        clean_reason = _single_line(reason)
        if not clean_reason:
            raise ValueError("demote reason must not be empty")
        with self._lock:
            candidate = self._require(lesson_id)
            if candidate.status not in _DEMOTABLE_STATUSES:
                raise ValueError(
                    "only an approved lesson or an auto_approved procedure card"
                    " can be demoted"
                )
            demoted = replace(candidate, status=_demoted_status(candidate))
            self._append(
                {
                    "type": "lesson_demoted",
                    "schema_v": 1,
                    "ts": time.time(),
                    "lesson_id": lesson_id,
                    "version": candidate.version,
                    "reason": clean_reason,
                }
            )
            self._lessons[lesson_id] = demoted
            self._write_view()
            return demoted

    def supersede(self, lesson_id: str, text: str) -> LessonCandidate:
        """Create a proposed revision while retaining the stable lesson id."""

        clean_text = _single_line(text)
        if not clean_text:
            raise ValueError("replacement text must not be empty")
        with self._lock:
            prior = self._require(lesson_id)
            revision = replace(
                prior,
                version=prior.version + 1,
                status="proposed",
                text=clean_text,
                conflicts=[],
                created_ts=time.time(),
            )
            self._append(
                {
                    "type": "lesson_superseded",
                    "schema_v": 1,
                    "ts": time.time(),
                    "lesson_id": lesson_id,
                    "from_version": prior.version,
                    "lesson": revision.to_dict(),
                }
            )
            self._lessons[lesson_id] = revision
            self._write_view()
            return revision

    def _require(self, lesson_id: str) -> LessonCandidate:
        candidate = self._lessons.get(lesson_id)
        if candidate is None:
            raise KeyError(f"unknown lesson: {lesson_id}")
        return candidate

    @staticmethod
    def _proposal_event(
        event_type: str,
        candidate: LessonCandidate,
        *,
        lesson_id: str | None = None,
        from_version: int | None = None,
        grade: str | None = None,
        grade_basis: str | None = None,
        fact_sheet: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": event_type,
            "schema_v": 1,
            "ts": time.time(),
            "lesson": candidate.to_dict(),
        }
        if lesson_id is not None:
            event["lesson_id"] = lesson_id
        if from_version is not None:
            event["from_version"] = from_version
        if grade is not None:
            event["grade"] = grade
        if grade_basis:
            event["grade_basis"] = grade_basis
        if fact_sheet:
            event["fact_sheet"] = dict(fact_sheet)
        return event

    def _append(self, event: Mapping[str, Any]) -> None:
        needs_newline = False
        if self.events_path.stat().st_size:
            with self.events_path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                needs_newline = stream.read(1) != b"\n"
        with self.events_path.open("a", encoding="utf-8") as stream:
            if needs_newline:
                stream.write("\n")
            stream.write(
                json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def _write_view(self) -> None:
        payload = [
            candidate.to_dict()
            for candidate in sorted(
                self._lessons.values(),
                key=lambda item: (item.created_ts, item.lesson_id),
            )
        ]
        _atomic_write(self.lessons_path, payload)


def _proposal_payload(candidate: LessonCandidate) -> dict[str, Any]:
    payload = candidate.to_dict()
    for key in ("version", "status", "created_ts"):
        payload.pop(key)
    return payload


def load_lessons(
    lessons_dir: str | os.PathLike[str] = "memory/lessons",
) -> list[LessonCandidate]:
    """Rebuild and return the current lesson view from authoritative events."""

    return LessonStore(lessons_dir).lessons()


def lesson_injectable(lesson: LessonCandidate) -> bool:
    """Return whether a lesson may cross the run-start injection gate.

    ``approved`` injects for either artifact kind — that is always a human
    decision.  ``auto_approved`` is the distiller's own confident verdict in the
    second self-grading call and injects for either kind too (WP-DISTILL-AUTO):
    a wrong card is corrected afterwards by human CLI or dream demotion rather
    than by a prior restraint.  ``needs_review`` behaves exactly like
    ``proposed`` and never injects.
    """

    return lesson.status in {"approved", "auto_approved"}


def proposal_metadata(
    lessons_dir: str | os.PathLike[str],
) -> dict[str, dict[str, Any]]:
    """Return the newest grading audit trail per lesson id (fail-open).

    The grade, its basis, and the fact sheet are event-only provenance: they are
    recorded next to the proposal in ``events.jsonl`` and never enter the lesson
    schema.  A missing or damaged log yields an empty mapping.
    """

    metadata: dict[str, dict[str, Any]] = {}
    events_path = Path(lessons_dir) / "events.jsonl"
    if not events_path.exists():
        return metadata
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(event, Mapping)
                or event.get("type") not in {"lesson_proposed", "lesson_superseded"}
                or not isinstance(event.get("lesson"), Mapping)
            ):
                continue
            lesson_id = str(event["lesson"].get("lesson_id") or "")
            if not lesson_id:
                continue
            entry: dict[str, Any] = {}
            for key in ("grade", "grade_basis", "fact_sheet"):
                if key in event:
                    entry[key] = event[key]
            if entry:
                metadata[lesson_id] = entry
    return metadata


def _read_lessons_snapshot(lessons_dir: str | os.PathLike[str]) -> list[LessonCandidate]:
    """Read ``lessons.json`` read-only; a damaged view fails open to empty.

    Intentionally does not construct :class:`LessonStore`: opening a runtime
    run must never create or rebuild lesson state.
    """

    path = Path(lessons_dir) / "lessons.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        lessons = [LessonCandidate.from_dict(item) for item in payload]
        if len({lesson.lesson_id for lesson in lessons}) != len(lessons):
            return []
        return lessons
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def select_lessons_for_injection(
    lessons_dir: str | os.PathLike[str],
    *,
    device_scope: str | None,
    max_items: int,
    max_tokens: int,
) -> list[LessonCandidate]:
    """Read a bounded approved-only run-start snapshot from ``lessons.json``.

    This path intentionally does not construct :class:`LessonStore`: opening a
    runtime run must never create or rebuild lesson state.  A missing or damaged
    materialized view fails open to no injection.  App-scoped lessons are
    excluded because the foreground app is not yet known at run start; a future
    event-triggered injector may resolve that narrower scope.
    """

    try:
        item_limit = int(max_items)
        token_limit = int(max_tokens)
    except (TypeError, ValueError):
        return []
    if item_limit <= 0 or token_limit <= 0:
        return []

    lessons = _read_lessons_snapshot(lessons_dir)

    local_device = _single_line(device_scope).removeprefix("device:")
    if local_device == "unknown":
        local_device = ""
    eligible = [
        lesson
        for lesson in lessons
        if lesson_injectable(lesson)
        # Procedures inject through the procedure card channel only (their own
        # budget and format); the rule mirror must not leak them as one-liners.
        and lesson.kind == "rule"
        and lesson.scope["device"] in {None, local_device or None}
        # App and app-version scope cannot be established at run start.
        and lesson.scope["app"] is None
        and lesson.scope["app_version"] is None
    ]
    selected = sorted(
        eligible,
        key=lambda lesson: (-lesson.version, -lesson.created_ts, lesson.lesson_id),
    )[:item_limit]

    while selected and sum(estimate_text_tokens(item.text) for item in selected) > token_limit:
        selected.pop()
    return selected


# WP-WF4-C: the app-entrance / mention-prefetch rule mirror keeps its own
# quota — separate from the run-start 3-item/800-token mirror and from the
# 1-card/300-token procedure-card budget.
APP_RULES_MAX_ITEMS = 2
APP_RULES_MAX_TOKENS = 200


def select_app_rules_for_injection(
    lessons_dir: str | os.PathLike[str],
    *,
    app_package: str,
    device_scope: str | None,
    max_items: int = APP_RULES_MAX_ITEMS,
    max_tokens: int = APP_RULES_MAX_TOKENS,
) -> list[LessonCandidate]:
    """Read a bounded approved-only app-rule snapshot from ``lessons.json``.

    WP-WF4-C delivery selector for rules scoped to exactly one app: the
    entrance (``app/launched``) and mention-prefetch channels hand them to the
    model beside that app's procedure card inside the same one-shot message.
    Same read-only discipline as :func:`select_lessons_for_injection` — a
    runtime run must never construct a :class:`LessonStore` — and the same
    fail-open: a missing or damaged view yields no injection.  The app scope
    must equal ``app_package`` exactly; the device scope follows the run-start
    rule (global matches anywhere, a pinned device only on that device); and
    version-scoped rules never inject because the running app version is
    unknowable here.
    """

    package = str(app_package or "").strip()
    if not package:
        return []
    try:
        item_limit = int(max_items)
        token_limit = int(max_tokens)
    except (TypeError, ValueError):
        return []
    if item_limit <= 0 or token_limit <= 0:
        return []

    lessons = _read_lessons_snapshot(lessons_dir)

    local_device = _single_line(device_scope).removeprefix("device:")
    if local_device == "unknown":
        local_device = ""
    eligible = [
        lesson
        for lesson in lessons
        if lesson_injectable(lesson)
        and lesson.kind == "rule"
        and lesson.scope["app"] == package
        and lesson.scope["device"] in {None, local_device or None}
        and lesson.scope["app_version"] is None
    ]
    selected = sorted(
        eligible,
        key=lambda lesson: (-lesson.version, -lesson.created_ts, lesson.lesson_id),
    )[:item_limit]

    while selected and sum(estimate_text_tokens(item.text) for item in selected) > token_limit:
        selected.pop()
    return selected


def emergency_revoke_lesson(
    lessons_dir: str | os.PathLike[str], lesson_id: str
) -> bool:
    """Persist a runner-originated revocation; unknown/corrupt state is a no-op."""

    try:
        store = LessonStore(lessons_dir)
        current = store.get(lesson_id)
        if current is None:
            return False
        if current.status == "revoked":
            return True
        store.revoke(lesson_id, "runner emergency revoke")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


@dataclass(frozen=True)
class PromotionEvaluation:
    eligible: bool
    candidate: LessonCandidate
    reasons: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.eligible


def _semantic_signature(text: str) -> tuple[int, str]:
    lowered = _single_line(text).casefold()
    polarity = -1 if any(term in lowered for term in _NEGATIONS) else 1
    for term in (*_NEGATIONS, *_MODALS):
        lowered = lowered.replace(term, "")
    base = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)
    return polarity, base


def _opposes(left: LessonCandidate, right: LessonCandidate) -> bool:
    if left.scope != right.scope:
        return False
    left_polarity, left_base = _semantic_signature(left.text)
    right_polarity, right_base = _semantic_signature(right.text)
    return bool(
        left_base
        and left_base == right_base
        and left_polarity != right_polarity
    )


def _approved_conflicts(
    candidate: LessonCandidate,
    approved_lessons: Sequence[LessonCandidate],
) -> list[str]:
    """Return same-scope injectable lessons that state the opposite.

    The fact-sheet counterpart of :func:`evaluate_promotion`'s contradiction
    check: nothing here blocks a proposal, the list only reaches the grader.
    """

    return [
        f"{approved.lesson_id}@v{approved.version}"
        for approved in approved_lessons
        if approved.lesson_id != candidate.lesson_id and _opposes(candidate, approved)
    ]


def evaluate_promotion(
    candidate: LessonCandidate,
    episodes: Sequence[Mapping[str, Any]],
    *,
    approved_lessons: Sequence[LessonCandidate] = (),
) -> PromotionEvaluation:
    """Apply Rule-of-3 and conservative same-scope contradiction detection.

    This is the **human** gate only (``--approve-lesson`` and dream's
    evidence-loss demotion).  Distillation never routes through it any more: a
    batch's status comes from the self-grading call, and the numbers below are
    reported to that grader through the fact sheet instead of blocking.
    """

    reasons = [
        item
        for item in candidate.conflicts
        if not item.startswith(("rule:", "approved_conflict:"))
    ]
    episode_ids = {
        str(item.get("run_id"))
        for item in episodes
        if item.get("type") == "episode_outcome" and item.get("run_id")
    }
    evidence_ids = {item["run_id"] for item in candidate.evidence}
    verified_support = len(evidence_ids & episode_ids)
    cited_episodes = [
        item for item in episodes if str(item.get("run_id")) in evidence_ids
    ]
    verified_task_keys = {_task_key(item) for item in cited_episodes}
    if candidate.support_count < 3:
        reasons.append(f"rule:support_count={candidate.support_count}<3")
    if verified_support < candidate.support_count:
        reasons.append(
            f"rule:verified_evidence={verified_support}<{candidate.support_count}"
        )
    supported_task_keys = set(candidate.task_keys) & verified_task_keys
    if len(supported_task_keys) < 2:
        reasons.append(f"rule:task_keys={len(supported_task_keys)}<2")
    if not set(candidate.task_keys) <= verified_task_keys:
        reasons.append("rule:task_keys_not_supported_by_evidence")
    for approved in approved_lessons:
        if approved.lesson_id == candidate.lesson_id or not lesson_injectable(approved):
            continue
        if _opposes(candidate, approved):
            reasons.append(f"approved_conflict:{approved.lesson_id}@v{approved.version}")
    reasons = list(dict.fromkeys(reasons))
    evaluated = replace(candidate, conflicts=reasons, status="proposed")
    return PromotionEvaluation(not reasons, evaluated, tuple(reasons))


def _task_key(episode: Mapping[str, Any]) -> str:
    """Derive a stable, readable task key from the episode goal text."""

    goal = _single_line(episode.get("goal_text")).casefold()
    for key, terms in _TASK_TERMS:
        if any(term in goal for term in terms):
            return key
    return "task:" + goal[:48]


def _episode_rows(events_path: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not events_path.exists():
        return []
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                isinstance(event, dict)
                and event.get("type") == "episode_outcome"
                and event.get("run_id")
            ):
                rows[str(event["run_id"])] = event
    return sorted(
        rows.values(),
        key=lambda item: (float(item.get("ts_end", 0.0)), str(item["run_id"])),
    )


def _episode_sort_key(item: Mapping[str, Any]) -> tuple[float, str]:
    return float(item.get("ts_end", 0.0)), str(item.get("run_id", ""))


def _ledger_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _single_line(value) or None


def _tool_ledgers(events_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Collect per-run step ledgers from the append-only experience log."""

    ledgers: dict[str, list[dict[str, Any]]] = {}
    if not events_path.exists():
        return ledgers
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("type") != "experience_event"
                ):
                    continue
                run_id = str(event.get("run_id") or "")
                if not run_id:
                    continue
                ledgers.setdefault(run_id, []).append(
                    {
                        "step": max(0, int(event.get("step", 0) or 0)),
                        "tool": _ledger_text(event.get("tool")) or "unknown",
                        "result_class": _ledger_text(event.get("result_class"))
                        or "error",
                        "app_package": _ledger_text(event.get("app_package")),
                        "intent": _ledger_text(event.get("intent")),
                        "note": _ledger_text(event.get("note")),
                    }
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    for entries in ledgers.values():
        entries.sort(key=lambda item: item["step"])
    return ledgers


def _scope_values(group: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    devices = {
        str(item.get("device_scope", "")).removeprefix("device:")
        for item in group
        if str(item.get("device_scope", "")).strip()
    }
    apps = {str(app) for item in group for app in item.get("apps", []) if app}
    return {"devices": devices, "apps": apps}


def _prompt_rows(
    batch: Sequence[Mapping[str, Any]],
    ledgers: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Render complete episode cards: goal, outcome, and per-step ledger."""

    rows: list[dict[str, Any]] = []
    for item in batch:
        run_id = str(item.get("run_id", ""))
        row: dict[str, Any] = {
            "run_id": run_id,
            "goal": str(item.get("goal_text", "") or ""),
            "apps": sorted({str(app) for app in item.get("apps", []) if app}),
            "success": bool(item.get("success")),
            "reason": _single_line(item.get("reason"))[:120],
            "verifier": _single_line(item.get("verifier")) or "skipped",
            "steps": max(0, int(item.get("steps", 0) or 0)),
            "tokens_total": max(0, int(item.get("tokens_total", 0) or 0)),
            "warnings": max(0, int(item.get("warnings", 0) or 0)),
            "takeover": _single_line(item.get("takeover")) or None,
            "device": _single_line(item.get("device_scope")).removeprefix("device:")[
                :120
            ]
            or None,
            "task_key": _task_key(item),
            "ts_end": float(item.get("ts_end", 0.0) or 0.0),
        }
        steps_ledger = [dict(entry) for entry in ledgers.get(run_id, ())]
        if steps_ledger:
            row["steps_ledger"] = steps_ledger
        # Mechanical struggle signals for the same run; always present so every
        # episode card keeps one shape (empty when the run went smoothly).
        row["struggle_markers"] = _struggle_markers(steps_ledger)
        rows.append(row)
    return rows


def _build_distill_messages(
    batch: Sequence[Mapping[str, Any]],
    ledgers: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    system = SystemMessage(
        content=(
            "你是离线经验提炼器：你会看到若干完整的任务过程（目标、逐步 intent/note 与"
            "执行结果账本、最终结局），请从中提炼两类可复用经验：单条行为规则（rule）与"
            "多步过程卡（procedure）。"
            "每条 episode 除账本外还有一个 struggle_markers 段，它是 harness 机械算出的"
            "挣扎信号：error_steps 是报错回执的位置与工具，redo_loops 是 back 之后又重走"
            "过的操作串，finish_rejections 是 finish 被驳回的次数，steps 是总步数。"
            "请优先从这四个镜头找经验："
            "① 错误恢复——某步报错之后，actor 用什么动作回到正轨；"
            "② 弯路重走——redo_loops 暴露重复劳动，说明前面少了哪一步或没确认哪个状态；"
            "③ 跨任务复现子流程——同一串语义子过程在 ≥2 个不同 task_key 的任务里出现；"
            "④ finish 驳回史——finish_rejections 说明这类任务的收尾还缺什么。"
            "只输出严格 JSON 对象 {\"rules\": [...], \"procedures\": [...]}，"
            "不要 Markdown、解释或代码围栏；两个数组都可以为空。"
            "候选元素只写语义字段：text、kind、evidence、scope、task_keys，"
            "以及可选字段 conflicts、steps、pitfalls、app_scope。"
            "簿记字段全由 harness 自填（lesson_id、schema_v、version、status、source、"
            "created_ts、support_count、scope.app_version），你不要写。"
            "evidence 的元素是 run_id 字符串或 {\"run_id\", \"note\"}，"
            "run_id 只能引用输入里真实存在的 run（编造即丢弃），note 可选一句话支撑点；"
            "scope 只有 device 与 app 两个键，取值只能逐字选自输入，没有就填 null；"
            "task_keys 只能选输入里出现过的 task_key。"
            "若一条课描述的是 App 之间切换/交接的行为（例如切走前先记录数据），"
            "偏好按实际涉及的每个 App 各产出一条候选，scope.app 各挂对应包名；"
            "这是偏好不是闸门，判不准时仍可只出一条通用课。"
            "rules（单条行为规则）：text 写一句行为规则加适用条件，kind 必须是 rule，"
            "不得携带 steps/app_scope。"
            "参考这句金例的写法："
            "「B站搜索切换排序方式后筛选面板不收起，此时按返回键会连同结果页一起退出；"
            "应先收起面板再继续后续操作」—— 说清触发条件、错在哪、正确做什么。"
            "两个负例不要提："
            "照抄型，如「在 run-3 第 7 步点坐标 500,800 后失败」——绑死单屏与坐标，"
            "UI 一变就失效；空谈型，如「操作时要小心一点」——读不出下一步该做什么。"
            "宁可输出空数组也不要凑数：证据不足就留空，"
            "一条错误的经验会在未来所有同类任务里误导 actor。"
            "procedures（多步过程卡）：kind 必须填 procedure；text 写卡名（一句短语）；"
            "app_scope 填应用包名，跨应用通用的卡填 \"general\"；steps 写有序语义步；"
            "pitfalls 写观察到的坑位（失败后恢复、弹窗/权限框如何处理），没有观察到就填 null"
            "（字符串或 null，不要数组）。"
            "只在单一任务里出现过的流程通常不足以下结论（会在第二步自评里被降级），"
            "但请按你的判断决定，harness 不会替你拦截。"
            "steps 必须是语义步：描述意图与目标，例如「搜索框输入目标」「选店进入」"
            "「到结算页停手问人」；禁止出现坐标（500,800）、mark id（ax_3、ax_3@e7）、"
            "具体工具名与工具参数（tap(ax_3)、type_text(上海)）——照抄工具参数的候选会被丢弃。"
            "太通用的流程（例如「打开 app 后点搜索」这类常识）不要提案。"
            "没有足够证据支撑的经验时对应的数组输出 []。"
        )
    )
    human = HumanMessage(
        content=(
            "从以下完整任务过程提炼候选：\n"
            + json.dumps(
                _prompt_rows(batch, ledgers), ensure_ascii=False, sort_keys=True
            )
        )
    )
    return [system, human]


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, Mapping) else str(block)
            for block in content
        )
    return str(content)


def _strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


_REDO_WINDOW_SIZES = (4, 3)


def _back_redo_loops(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return every ``back`` step whose following tool window repeats earlier.

    Purely mechanical and deliberately conservative: only an exact contiguous
    repeat of a 4- (else 3-) tool window counts, at most one record per ``back``,
    and every tool including wait/read_screen participates, so an interleaved
    observation breaks the match rather than fabricating a loop.
    """

    tools = [str(entry.get("tool", "unknown")) for entry in entries]
    loops: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if tool != "back":
            continue
        earlier = tools[:index]
        for size in _REDO_WINDOW_SIZES:
            window = tools[index + 1 : index + 1 + size]
            if len(window) < size:
                continue
            if not any(
                earlier[offset : offset + size] == window
                for offset in range(len(earlier) - size + 1)
            ):
                continue
            loops.append(
                {
                    "after_step": max(0, int(entries[index].get("step", 0) or 0)),
                    "repeat_tools": list(window),
                }
            )
            break
    return loops


def _struggle_markers(ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Distill mechanical struggle signals out of one run's step ledger.

    Only fields the experience plane already carries are used — ledger entries
    have no error text and never will (P0 #16), so an error is a receipt class,
    not a message.  Every key is always present; no signal means empty.
    """

    entries = sorted(ledger, key=lambda item: item.get("step", 0))
    return {
        "error_steps": [
            {
                "step": max(0, int(entry.get("step", 0) or 0)),
                "tool": str(entry.get("tool", "unknown")),
            }
            for entry in entries
            if str(entry.get("result_class", "")) == "error"
        ],
        "redo_loops": _back_redo_loops(entries),
        "finish_rejections": sum(
            1
            for entry in entries
            if str(entry.get("tool", "")) == "finish"
            and str(entry.get("result_class", "")) != "ok"
        ),
        "steps": len(entries),
    }


_MECHANICAL_STEP = re.compile(
    r"(?:ax_\d+|@e\d+|\b\d{1,4}\s*[,，]\s*\d{1,4}\b"
    r"|\b(?:tap|long_press|type_text|swipe|launch_app|read_screen|wait|locate"
    r"|update_task_doc|finish|ask_user|take_over|write_document)\s*\()",
    re.IGNORECASE,
)


def _is_semantic_steps(steps: Sequence[str]) -> bool:
    """Reject procedure steps that are mechanically bound to one screen."""

    return all(not _MECHANICAL_STEP.search(step) for step in steps)


def _split_distill_payload(payload: Any) -> tuple[list[Any], list[Any]]:
    """Split the distiller output into rule and procedure candidates."""

    if isinstance(payload, list):
        # Pre-WP-WF1 prompt shape: a bare array is read as rules only.
        return payload, []
    if isinstance(payload, Mapping):
        if not {"rules", "procedures"} & set(payload):
            raise ValueError("distill output object must carry rules/procedures")
        rules = payload.get("rules", [])
        procedures = payload.get("procedures", [])
        if isinstance(rules, list) and isinstance(procedures, list):
            return rules, procedures
    raise ValueError(
        'distill output must be {"rules": [...], "procedures": [...]} or a JSON array'
    )


_MODEL_CANDIDATE_FIELDS = frozenset(
    {
        "text",
        "kind",
        "evidence",
        "scope",
        "task_keys",
        "conflicts",
        "steps",
        "pitfalls",
        "app_scope",
    }
)
# Bookkeeping the harness owns.  A model still emitting the pre-A2 full payload
# is tolerated — the fields are read and discarded — while any *unknown* key
# still fails closed.
_IGNORED_MODEL_FIELDS = frozenset(
    {"lesson_id", "schema_v", "version", "status", "source", "created_ts", "support_count"}
)
_REQUIRED_MODEL_FIELDS = ("text", "evidence", "scope", "task_keys")


def _model_evidence(raw: Any) -> list[dict[str, str]]:
    """Normalize `evidence[{run_id, note?}]`; the note is optional."""

    if not isinstance(raw, list):
        raise ValueError("evidence must be an array")
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"run_id": item}
        if (
            not isinstance(item, Mapping)
            or not set(item) <= _EVIDENCE_FIELDS
            or "run_id" not in item
        ):
            raise ValueError(
                "each evidence item must be a run_id string or {run_id, note?}"
            )
        run_id = item["run_id"]
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("evidence.run_id must be a non-empty string")
        run_id = run_id.strip()
        note = item.get("note")
        if note is None or (isinstance(note, str) and not note.strip()):
            note = "cited by distill"
        if not isinstance(note, str) or note != _single_line(note):
            raise ValueError("evidence.note must be a non-empty normalized line")
        if run_id in seen:
            # Same run cited twice still counts once.
            continue
        seen.add(run_id)
        evidence.append({"run_id": run_id, "note": note})
    return evidence


def _model_scope(raw: Any) -> dict[str, str | None]:
    """Normalize `scope{device, app}`; ``app_version`` is always None."""

    if not isinstance(raw, Mapping) or not set(raw) <= {"device", "app", "app_version"}:
        raise ValueError("scope must contain only device/app")
    return {
        "device": _nullable_text(raw.get("device"), "device"),
        "app": _nullable_text(raw.get("app"), "app"),
        "app_version": None,
    }


def _candidate_from_model_dict(payload: Any) -> LessonCandidate:
    """Build a lesson from the distiller's slim, semantic-only element.

    The model emits meaning; the harness emits bookkeeping: ``lesson_id``
    (text+scope hash, later re-based on the identity scope), ``schema_v``,
    ``version``, ``status=proposed``, ``source=distill``, ``created_ts``,
    ``scope.app_version=None``, and ``support_count`` = deduplicated evidence
    count.  Validation of every shape the harness did not produce stays strict
    through :meth:`LessonCandidate.from_dict`, which remains the storage entry.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("lesson candidate must be an object")
    extra = sorted(set(payload) - _MODEL_CANDIDATE_FIELDS - _IGNORED_MODEL_FIELDS)
    if extra:
        raise ValueError(f"unexpected candidate fields: {extra}")
    for key in _REQUIRED_MODEL_FIELDS:
        if key not in payload:
            raise ValueError(f"missing candidate field: {key}")

    kind = payload.get("kind", "rule")
    if not isinstance(kind, str) or kind not in LESSON_KINDS:
        raise ValueError(f"kind must be one of {sorted(LESSON_KINDS)!r}")
    text = payload["text"]
    if not isinstance(text, str) or not text.strip() or text != _single_line(text):
        raise ValueError("text must be one non-empty normalized line")
    evidence = _model_evidence(payload["evidence"])
    scope = _model_scope(payload["scope"])
    task_keys = _string_list(payload["task_keys"], "task_keys")
    raw_conflicts = payload.get("conflicts")
    conflicts = _string_list(raw_conflicts, "conflicts") if raw_conflicts is not None else []
    raw_steps = payload.get("steps")
    steps = _step_list(raw_steps) if raw_steps is not None else []
    raw_pitfalls = payload.get("pitfalls")
    if isinstance(raw_pitfalls, list):
        raw_pitfalls = "；".join(
            s for s in (str(i).strip() for i in raw_pitfalls) if s
        ) or None
    pitfalls = _nullable_str(raw_pitfalls, "pitfalls")
    app_scope = _nullable_str(payload.get("app_scope"), "app_scope")

    return LessonCandidate.from_dict(
        {
            "lesson_id": make_lesson_id(text, scope),
            "schema_v": 1,
            "version": 1,
            "status": "proposed",
            "text": text,
            "scope": scope,
            "evidence": evidence,
            "support_count": len(evidence),
            "task_keys": task_keys,
            "conflicts": conflicts,
            "created_ts": time.time(),
            "source": "distill",
            "kind": kind,
            "steps": steps,
            "pitfalls": pitfalls,
            "app_scope": app_scope,
        }
    )


def _validate_model_candidates(
    response: Any,
    group: Sequence[Mapping[str, Any]],
) -> list[LessonCandidate]:
    payload = _strict_json_loads(_response_text(response))
    raw_rules, raw_procedures = _split_distill_payload(payload)
    allowed_runs = {str(item["run_id"]) for item in group}
    episode_by_id = {str(item["run_id"]): item for item in group}
    allowed_tasks = {_task_key(item) for item in group}
    scope_values = _scope_values(group)
    candidates: list[LessonCandidate] = []
    for raw in [*raw_rules, *raw_procedures]:
        try:
            candidate = _candidate_from_model_dict(raw)
            evidence_ids = {item["run_id"] for item in candidate.evidence}
            if not evidence_ids or not evidence_ids <= allowed_runs:
                continue
            cited = [item for item in group if str(item["run_id"]) in evidence_ids]
            cited_tasks = {_task_key(item) for item in cited}
            if not set(candidate.task_keys) <= allowed_tasks:
                continue
            if not set(candidate.task_keys) <= cited_tasks:
                continue
            if candidate.kind == "procedure" and not _is_semantic_steps(
                candidate.steps
            ):
                # The only mechanical procedure check left: steps must survive
                # a UI redesign. How often the sub-process recurred is a fact
                # for the grader (``recurs_in_tasks``), never a harness gate.
                continue
            if candidate.scope["device"] not in {None, *scope_values["devices"]}:
                continue
            if candidate.scope["app"] not in {None, *scope_values["apps"]}:
                continue
            if candidate.scope["app_version"] is not None:
                continue
            canonical_evidence = []
            for item in candidate.evidence:
                episode = episode_by_id[item["run_id"]]
                outcome = "success" if bool(episode.get("success")) else "failure"
                reason = _single_line(episode.get("reason"))[:120]
                canonical_evidence.append(
                    {
                        "run_id": item["run_id"],
                        "note": f"{outcome}:{reason}" if reason else outcome,
                    }
                )
            identity_scope = dict(candidate.scope)
            if candidate.kind == "procedure":
                identity_scope["app_scope"] = candidate.app_scope
            candidate = replace(
                candidate,
                lesson_id=make_lesson_id(candidate.text, identity_scope),
                evidence=sorted(canonical_evidence, key=lambda item: item["run_id"]),
                task_keys=sorted(candidate.task_keys),
                conflicts=sorted(candidate.conflicts),
                created_ts=time.time(),
            )
            candidates.append(candidate)
        except (TypeError, ValueError):
            continue
    return candidates


def _verified_appkb_packages(
    appkb_dir: str | os.PathLike[str] | None,
) -> frozenset[str]:
    """Return App-KB packages with at least one verified launch (fail-open)."""

    if appkb_dir is None:
        return frozenset()
    try:
        payload = json.loads(
            (Path(appkb_dir) / "app_kb" / "kb.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, list):
        return frozenset()
    verified: set[str] = set()
    for entry in payload:
        if not isinstance(entry, Mapping) or entry.get("stale"):
            continue
        package = str(entry.get("package") or "").strip()
        if not package:
            continue
        try:
            success_count = int(entry.get("success_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        if success_count > 0:
            verified.add(package)
    return frozenset(verified)


def _procedure_proposal_counts(events_path: Path) -> Counter[tuple[str, str]]:
    """Count earlier procedure proposals keyed by normalized title+app_scope."""

    counts: Counter[tuple[str, str]] = Counter()
    if not events_path.exists():
        return counts
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(event, Mapping)
                or event.get("type") not in {"lesson_proposed", "lesson_superseded"}
                or not isinstance(event.get("lesson"), Mapping)
                or event["lesson"].get("kind") != "procedure"
            ):
                continue
            key = (
                _single_line(event["lesson"].get("text")).casefold(),
                _single_line(event["lesson"].get("app_scope")).casefold(),
            )
            counts[key] += 1
    return counts


def _procedure_fact_sheet(
    candidate: LessonCandidate,
    group: Sequence[Mapping[str, Any]],
    *,
    verified_packages: frozenset[str],
    prior_counts: Mapping[tuple[str, str], int],
) -> dict[str, Any]:
    """Build the harness-side fact sheet a grader sees for one procedure card."""

    run_ids = sorted(item["run_id"] for item in candidate.evidence)
    cited = [
        item for item in group if str(item.get("run_id")) in set(run_ids)
    ]
    outcomes = [bool(item.get("success")) for item in cited]
    successes = sum(1 for item in outcomes if item)
    failures = len(outcomes) - successes
    if not outcomes:
        outcome = "unknown"
    elif all(outcomes):
        outcome = "success"
    elif not any(outcomes):
        outcome = "failure"
    else:
        outcome = "mixed"
    task_keys = sorted({_task_key(item) for item in cited})
    app_scope = candidate.app_scope
    prior = int(
        prior_counts.get(
            (
                _single_line(candidate.text).casefold(),
                _single_line(app_scope).casefold(),
            ),
            0,
        )
    )
    return {
        "run_ids": run_ids,
        "run_count": len(run_ids),
        "task_count": len(task_keys),
        "task_keys": task_keys,
        "outcome_success": successes,
        "outcome_failure": failures,
        "outcome": outcome,
        "outcome_consistent": outcome in {"success", "failure"},
        "app_scope": app_scope,
        "app_scope_verified": app_scope in verified_packages,
        "prior_proposals": prior,
        "previously_proposed": prior > 0,
    }


def _recurrence_counts(events_path: Path) -> Counter[str]:
    """Count prior proposals per stable lesson id (text+scope identical)."""

    counts: Counter[str] = Counter()
    if not events_path.exists():
        return counts
    with events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not isinstance(event, Mapping)
                or event.get("type") not in {"lesson_proposed", "lesson_superseded"}
                or not isinstance(event.get("lesson"), Mapping)
            ):
                continue
            lesson_id = str(event["lesson"].get("lesson_id") or "").strip()
            if lesson_id:
                counts[lesson_id] += 1
    return counts


def _candidate_fact_sheet(
    candidate: LessonCandidate,
    group: Sequence[Mapping[str, Any]],
    *,
    ledgers: Mapping[str, Sequence[Mapping[str, Any]]],
    verified_packages: frozenset[str],
    prior_counts: Mapping[tuple[str, str], int],
    recurrence: Mapping[str, int],
    approved_lessons: Sequence[LessonCandidate],
) -> dict[str, Any]:
    """Build the unified fact sheet a grader sees for one candidate.

    Rules and procedure cards share the same evidence-is-real core (outcomes,
    error load, cross-task recurrence, past proposals, conflicts); a procedure
    card additionally carries its card-specific block (identity, step shape,
    App-KB verification).
    """

    run_ids = sorted({item["run_id"] for item in candidate.evidence})
    episode_by_id = {str(item.get("run_id")): item for item in group}
    cited = [episode_by_id[run_id] for run_id in run_ids if run_id in episode_by_id]
    sheet: dict[str, Any] = {
        "kind": candidate.kind,
        "support_count_verified": len(cited),
        "evidence_outcomes": [
            {
                "run_id": run_id,
                "success": bool(episode_by_id[run_id].get("success")),
                "reason": _single_line(episode_by_id[run_id].get("reason"))[:120],
            }
            for run_id in run_ids
            if run_id in episode_by_id
        ],
        "error_receipts_per_run": {
            run_id: sum(
                1
                for entry in ledgers.get(run_id, ())
                if str(entry.get("result_class", "")) == "error"
            )
            for run_id in run_ids
        },
        "recurs_in_tasks": len({_task_key(item) for item in cited}),
        # Earlier identical proposals, excluding this one — recurrence across
        # batches is corroboration, novelty is not disqualification.
        "recurrence_history": int(recurrence.get(candidate.lesson_id, 0)),
        "conflicts_with_approved": _approved_conflicts(candidate, approved_lessons),
    }
    if candidate.kind == "procedure":
        sheet.update(
            _procedure_fact_sheet(
                candidate,
                group,
                verified_packages=verified_packages,
                prior_counts=prior_counts,
            )
        )
    return sheet


def _build_grading_messages(
    candidates: Sequence[LessonCandidate],
    fact_sheets: Mapping[str, Mapping[str, Any]],
) -> list[Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    system = SystemMessage(
        content=(
            "你是离线经验分级器：你会看到若干候选（单条规则 rule 或多步过程卡 procedure），"
            "以及 harness 为每条候选统计的事实单（引用的 run 与结局、每个 run 的报错回执数、"
            "跨多少个不同任务复现、历史上被独立提炼过多少次、是否与已注入的经验在同一 scope 下"
            "表达相反主张；过程卡另有 app_scope 是否在 App-KB 验证过等卡片事实）。"
            "harness 只做事实核验与簿记，最终判断权在你。"
            "只输出严格 JSON 对象 {\"grades\": [{\"lesson_id\": ..., \"grade\": ..., "
            "\"basis\": ...}]}，不要 Markdown、解释或代码围栏。"
            "grade 只能是 auto_approved 或 needs_review。"
            "请注意两个方向的风险并不对称：错误注入一条伪经验，会在未来所有同类任务里误导"
            "actor 的行为，代价远高于把一条好经验留给人审（needs_review 不阻塞任何事，"
            "只是进人工队列）。因此只有同时满足以下三条才填 auto_approved："
            "一是事实单显示的证据真实且无矛盾（引用的 run 确实存在、结局支持这条表述、"
            "conflicts_with_approved 为空）；"
            "二是表述可执行（读了就知道下一步该做什么，而不是空泛的提醒）；"
            "三是即使判断错了也不危险（照做最多绕点路，不会漏改关键信息或误导其他任务）。"
            "任何一条不满足，或你只是拿不准，一律填 needs_review。"
            "basis 写一句判断依据（中文），缺失或非法的 grade 一律按 needs_review 处理。"
        )
    )
    human = HumanMessage(
        content=(
            "为以下候选分级：\n"
            + json.dumps(
                {
                    "candidates": [item.to_dict() for item in candidates],
                    "fact_sheets": {key: dict(value) for key, value in fact_sheets.items()},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    )
    return [system, human]


def _parse_grades(response: Any) -> dict[str, tuple[str, str]]:
    """Map lesson_id → (grade, basis); anything unusable falls back per item."""

    payload = _strict_json_loads(_response_text(response))
    items = payload.get("grades") if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        raise ValueError("grading output must be a JSON array or {grades: [...]}")
    grades: dict[str, tuple[str, str]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        lesson_id = _single_line(raw.get("lesson_id"))
        if not lesson_id:
            continue
        grade = raw.get("grade")
        if grade not in _GRADES:
            grade = "needs_review"
        grades[lesson_id] = (grade, _single_line(raw.get("basis"))[:200])
    return grades


def _grading_records(
    candidates: Sequence[LessonCandidate],
    fact_sheets: Mapping[str, Mapping[str, Any]],
    grades: Mapping[str, tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        grade, basis = grades.get(candidate.lesson_id, ("needs_review", ""))
        if grade not in _GRADES:
            grade = "needs_review"
        records[candidate.lesson_id] = {
            "grade": grade,
            "grade_basis": basis,
            "fact_sheet": dict(fact_sheets[candidate.lesson_id]),
        }
    return records


def _apply_self_grading(
    candidates: Sequence[LessonCandidate],
    group: Sequence[Mapping[str, Any]],
    *,
    model: Any,
    ledger: UsageLedger,
    store: "LessonStore",
    token_budget: int | None,
    appkb_dir: str | os.PathLike[str] | None,
    ledgers: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Grade every candidate with a second model call over fact sheets.

    Rules and procedure cards travel the same path (WP-DISTILL-AUTO): the
    harness only supplies facts, the model owns the verdict.  Both calls are
    charged to the ``distill`` ledger role.  Any failure (budget, transport,
    malformed grades) is fail-open: every candidate stays at ``needs_review``
    rather than being dropped.
    """

    if not candidates:
        return {}
    verified_packages = _verified_appkb_packages(appkb_dir)
    prior_counts = _procedure_proposal_counts(store.events_path)
    recurrence = _recurrence_counts(store.events_path)
    approved_lessons = [
        item for item in store.lessons() if lesson_injectable(item)
    ]
    active_ledgers = ledgers or {}
    fact_sheets = {
        item.lesson_id: _candidate_fact_sheet(
            item,
            group,
            ledgers=active_ledgers,
            verified_packages=verified_packages,
            prior_counts=prior_counts,
            recurrence=recurrence,
            approved_lessons=approved_lessons,
        )
        for item in candidates
    }
    grades: dict[str, tuple[str, str]] = {}
    messages = _build_grading_messages(candidates, fact_sheets)
    request_estimate = estimate_context_tokens(messages)
    if token_budget is not None and ledger.total + request_estimate > token_budget:
        # Out of budget: keep every candidate at needs_review, drop nothing.
        return _grading_records(candidates, fact_sheets, grades)
    try:
        response = model.invoke(messages)
    except Exception:  # noqa: BLE001 - grading is advisory, never fatal
        ledger.record("distill", estimate_tokens=request_estimate)
    else:
        ledger.record(
            "distill",
            response,
            estimate_tokens=request_estimate + estimate_message_tokens(response),
        )
        try:
            grades = _parse_grades(response)
        except (TypeError, ValueError, json.JSONDecodeError):
            grades = {}
    return _grading_records(candidates, fact_sheets, grades)


@dataclass(frozen=True)
class DistillResult:
    groups_considered: int
    groups_rejected: int
    proposed: tuple[LessonCandidate, ...]
    tokens_total: int
    tokens_by_role: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups_considered": self.groups_considered,
            "groups_rejected": self.groups_rejected,
            "proposed": [item.to_dict() for item in self.proposed],
            "tokens_total": self.tokens_total,
            "tokens_by_role": dict(self.tokens_by_role),
        }


_DISTILL_BATCH_MAX = 40
_DISTILL_STATE_FILE = "distill_state.json"


def _distill_state_path(lessons_dir: str | os.PathLike[str]) -> Path:
    return Path(lessons_dir) / _DISTILL_STATE_FILE


def _read_distill_watermark(lessons_dir: str | os.PathLike[str]) -> float:
    """Return the last processed ts_end; missing or corrupt state means 0.0."""

    try:
        payload = json.loads(
            _distill_state_path(lessons_dir).read_text(encoding="utf-8")
        )
        value = float(payload["last_ts_end"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _write_distill_watermark(lessons_dir: str | os.PathLike[str], value: float) -> None:
    _atomic_write(_distill_state_path(lessons_dir), {"last_ts_end": float(value)})


def _distill_batch(
    batch: Sequence[Mapping[str, Any]],
    *,
    messages: Sequence[Any],
    request_estimate: int,
    model: Any,
    ledger: UsageLedger,
    store: LessonStore,
    token_budget: int | None,
    ledgers: Mapping[str, Sequence[Mapping[str, Any]]],
    appkb_dir: str | os.PathLike[str] | None,
) -> tuple[int, list[LessonCandidate]]:
    """Run one batch through the model and persist whatever it supports."""

    try:
        response = model.invoke(messages)
    except Exception:  # noqa: BLE001 - reject this offline batch, keep the watermark
        ledger.record("distill", estimate_tokens=request_estimate)
        return 1, []
    ledger.record(
        "distill",
        response,
        estimate_tokens=request_estimate + estimate_message_tokens(response),
    )
    if token_budget is not None and ledger.total > token_budget:
        return 1, []
    try:
        candidates = _validate_model_candidates(response, batch)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1, []
    if not candidates:
        return 0, []
    # Second call: the harness supplies per-card facts, the model self-grades
    # every candidate — rules and procedure cards alike.
    grading = _apply_self_grading(
        candidates,
        batch,
        model=model,
        ledger=ledger,
        store=store,
        token_budget=token_budget,
        appkb_dir=appkb_dir,
        ledgers=ledgers,
    )
    proposed: list[LessonCandidate] = []
    for candidate in candidates:
        metadata = grading.get(candidate.lesson_id)
        # The verdict is the grader's; there is no harness-evaluated status any
        # more, only fail-open ``needs_review`` when the grade is unusable.
        graded = (
            candidate
            if metadata is None or metadata["grade"] == candidate.status
            else replace(candidate, status=metadata["grade"])
        )
        prior = store.get(graded.lesson_id)
        saved = store.propose(
            graded,
            grade=None if metadata is None else metadata["grade"],
            grade_basis=None if metadata is None else metadata["grade_basis"],
            fact_sheet=None if metadata is None else metadata["fact_sheet"],
        )
        if prior is None or saved.version > prior.version:
            proposed.append(saved)
    return 0, proposed


def distill_lessons(
    experience_events: str | os.PathLike[str],
    lessons_dir: str | os.PathLike[str],
    *,
    model: Any,
    ledger: UsageLedger | None = None,
    token_budget: int | None = None,
    appkb_dir: str | os.PathLike[str] | None = None,
) -> DistillResult:
    """Distill one watermarked batch of episodes into proposed lessons only.

    Every episode newer than the persisted ``last_ts_end`` watermark is distilled
    ungrouped in a single model call, so the distiller sees complete task
    processes instead of per-app cohorts.  The watermark advances once the batch
    has been processed, including when the batch was rejected, so processed
    episodes are never replayed.

    A second model call self-grades every surviving candidate: the harness
    supplies a fact sheet, the model answers ``auto_approved`` or
    ``needs_review``, and that verdict becomes the lesson's status (see
    :func:`_apply_self_grading`).  ``appkb_dir`` is the App-KB root used to
    verify ``app_scope`` packages.
    """

    events_path = Path(experience_events)
    episodes = _episode_rows(events_path)
    watermark = _read_distill_watermark(lessons_dir)
    batch = sorted(
        (
            item
            for item in episodes
            if float(item.get("ts_end", 0.0) or 0.0) > watermark
        ),
        key=_episode_sort_key,
    )[:_DISTILL_BATCH_MAX]
    active_ledger = ledger or UsageLedger()
    if not batch:
        return DistillResult(0, 0, (), active_ledger.total, active_ledger.by_role())

    store = LessonStore(lessons_dir)
    ledgers = _tool_ledgers(events_path)
    rejected = 0
    proposed: list[LessonCandidate] = []
    try:
        if token_budget is not None and active_ledger.total >= token_budget:
            rejected = 1
        else:
            messages = _build_distill_messages(batch, ledgers)
            request_estimate = estimate_context_tokens(messages)
            if (
                token_budget is not None
                and active_ledger.total + request_estimate > token_budget
            ):
                rejected = 1
            else:
                rejected, proposed = _distill_batch(
                    batch,
                    messages=messages,
                    request_estimate=request_estimate,
                    model=model,
                    ledger=active_ledger,
                    store=store,
                    token_budget=token_budget,
                    ledgers=ledgers,
                    appkb_dir=appkb_dir,
                )
    finally:
        _write_distill_watermark(
            lessons_dir,
            max(float(item.get("ts_end", 0.0) or 0.0) for item in batch),
        )
    return DistillResult(
        groups_considered=1,
        groups_rejected=rejected,
        proposed=tuple(proposed),
        tokens_total=active_ledger.total,
        tokens_by_role=active_ledger.by_role(),
    )


def build_distill_model(config: Any) -> Any:
    """Build memory_model when configured, otherwise the main model."""

    from phone_agent.v2.model import build_chat_model

    name = getattr(config, "memory_model", None)
    active_config = replace(config, model_name=name) if name else config
    return build_chat_model(active_config)


def approve_if_eligible(
    store: LessonStore,
    lesson_id: str,
    episodes: Sequence[Mapping[str, Any]],
) -> LessonCandidate:
    """Approve one human-selected lesson unconditionally.

    Post-DISTILL-AUTO the human CLI is a correction channel, not a gate:
    Rule-of-3 lives only in the distill fact sheet, so promotion never
    hard-blocks on support counts. `episodes` is accepted for signature
    compatibility and is unused.
    """

    candidate = store.get(lesson_id)
    if candidate is None:
        raise KeyError(f"unknown lesson: {lesson_id}")
    return store.approve(lesson_id)


def read_episode_outcomes(
    experience_dir: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    return _episode_rows(Path(experience_dir) / "events.jsonl")


__all__ = [
    "DistillResult",
    "GENERAL_APP_SCOPE",
    "LESSON_EVENT_TYPES",
    "LESSON_FIELDS",
    "LESSON_KINDS",
    "LESSON_STATUSES",
    "LessonCandidate",
    "LessonStore",
    "OPTIONAL_LESSON_FIELDS",
    "PromotionEvaluation",
    "approve_if_eligible",
    "build_distill_model",
    "distill_lessons",
    "evaluate_promotion",
    "lesson_injectable",
    "load_lessons",
    "make_lesson_id",
    "proposal_metadata",
    "read_episode_outcomes",
    "select_app_rules_for_injection",
    "select_lessons_for_injection",
]
