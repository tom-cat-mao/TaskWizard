"""Rebuildable semantic recall for experience episodes, App-KB aliases, and
procedure cards.

The runtime contract is deliberately observe-only in ``shadow`` mode: this
module can retrieve and evaluate candidates, but it never constructs model
messages or mutates the actor context.  Episode JSONL is consumed directly so
the shared WP-I1 schema remains owned by the experience plane.

WP-WF2a adds a third namespace, ``procedure``, over the injectable procedure
cards produced by the WP-WF1 lesson pipeline (see ``WORKFLOW-MEMORY-DESIGN.md``
§4/§5).  Matching is deterministic hard filters (app package equality, device
scope) followed by a single embedding top-1; the selector only *selects* — the
two injection points and the prompt wiring land in WP-WF3.

Recall hardening keeps that fail-open contract but makes it observable: the
shared embedder is warmed on a daemon thread at capability mount (``on``/
``shadow`` only), and every selection-path exception is recorded — one
``recall_selection_error`` trace event plus a per-namespace counter in
``recall_stats.json`` — before the caller's fail-open return.  Neither changes
a selection result, a run outcome, or the meaning of an existing stats field.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any, Protocol


DEFAULT_EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
_NAMESPACES = frozenset({"episode", "app_alias", "procedure"})
# Device scope value that matches every device; mirrors the App-KB alias usage.
_GLOBAL_DEVICE_SCOPE = "global"
# WP-WF2a procedure-recall knobs.  The threshold is a module constant (not a
# config key) because WP-WF2b scans it offline in replay; the injection budget
# is deliberately separate from the rule budget (3 items / 800 tokens).
PROCEDURE_MIN_SCORE = 0.30  # calibrated on real episodes (20 runs, 2 cards) via
# `replay --channel procedure --sweep --calibrate`: knee at highest coverage
# (0.30) whose relevance clears 0.60 (0.667); tau>=0.45 reaches relevance 1.0
# but coverage drops to 0.20.
PROCEDURE_MAX_ITEMS = 1
PROCEDURE_MAX_TOKENS = 300
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]+")
_LAUNCH_RE = re.compile(
    r"\blaunched\s+.*\(([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\)\s*$",
    re.IGNORECASE,
)
# The recall side plane must fail open *visibly*.  A selection-path exception
# is recorded (one trace event plus a per-namespace counter in the shadow
# scorecard) before the caller's fail-open return, and the embedder loads its
# model off the run's critical path at capability mount time.  Neither the
# warm-up nor the recording may change a selection result or block a run.
WARMUP_TEXT = "warmup"
RECALL_SELECTION_ERROR = "recall_selection_error"
# One observe-only counter per namespace; existing stats keys are never
# rewritten (the schema-v2 scorecard is only extended).
SELECTION_ERROR_KEYS = {
    "episode": "episode_errors",
    "app_alias": "app_alias_errors",
    "procedure": "procedure_errors",
}
_STATS_LOCK = threading.Lock()
_INDEX_LOCK = threading.RLock()
_SELECTION_OBSERVER_LOCK = threading.Lock()
# Observers receive ``(namespace, error_type)`` — never a stack trace, message,
# or any caller-controlled string, so the audit plane stays redaction-safe.
_selection_error_observers: list[Callable[[str, str], None]] = []


class Embedder(Protocol):
    """Minimal embedding boundary used by the index and fake-only tests."""

    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one normalized vector per input text."""


def _normalize(vector: Sequence[float], *, dimension: int) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != dimension:
        raise ValueError(
            f"embedding dimension mismatch: expected {dimension}, got {len(values)}"
        )
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("embedding must have a finite, non-zero norm")
    return [value / norm for value in values]


def _semantic_tokens(text: str) -> list[str]:
    """Return stable word/CJK n-gram features for the deterministic fake."""

    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(str(text or "").casefold()):
        value = match.group(0)
        tokens.append(value)
        if any("\u3400" <= char <= "\u9fff" for char in value):
            tokens.extend(value)
            tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
    return tokens or [str(text or "").casefold()]


class HashEmbedder:
    """Deterministic dependency-free embedder for tests and local checks."""

    def __init__(self, dimension: int = 64, model_id: str = "hash-v1") -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = int(dimension)
        self.model_id = str(model_id)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in _semantic_tokens(str(text)):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "little") % self.dimension
                sign = 1.0 if digest[8] & 1 else -1.0
                vector[index] += sign
            vectors.append(_normalize(vector, dimension=self.dimension))
        return vectors


class MlxEmbedder:
    """Lazy ``mlx-embeddings`` adapter for normalized Qwen3 embeddings."""

    def __init__(
        self,
        model_id: str = DEFAULT_EMBED_MODEL,
        dimension: int = 1024,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.model_id = str(model_id)
        self.dimension = int(dimension)
        self._model: Any | None = None
        self._processor: Any | None = None
        self._generate: Any | None = None
        # Reentrant: ``embed`` holds it across load + generate so a background
        # warm-up and the run's own call can never load MLX twice or run two
        # generate calls at once (that race segfaults the process).
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            # Importing mlx-embeddings pulls in the MLX/transformers stack, so it
            # belongs here rather than at module import or object construction.
            from mlx_embeddings import generate, load

            model, processor = load(self.model_id, lazy=True)
            self._model = model
            self._processor = processor
            self._generate = generate

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        inputs = [str(text) for text in texts]
        if not inputs:
            return []
        with self._lock:
            self._ensure_loaded()
            output = self._generate(self._model, self._processor, texts=inputs)
            embeddings = getattr(output, "text_embeds", output)
            rows = embeddings.tolist()
            if rows and isinstance(rows[0], (int, float)):
                rows = [rows]
            if len(rows) != len(inputs):
                raise ValueError(
                    f"embedder returned {len(rows)} vectors for {len(inputs)} texts"
                )
        return [_normalize(row, dimension=self.dimension) for row in rows]


def embedder_needs_warmup(embedder: Any) -> bool:
    """Whether a background warm-up embed is worth running for ``embedder``.

    The deterministic test/office embedder has nothing to load, and an adapter
    that reports itself loaded is already warm; only a lazy real model
    benefits from paying the load cost off the run's critical path.
    """

    if isinstance(embedder, HashEmbedder):
        return False
    return getattr(embedder, "loaded", None) is not True


def resolve_embedder_factory(candidate: Any) -> Callable[[], Any] | None:
    """Duck-type the run's shared embedder factory out of a mounted object.

    The recall capability mounts objects it does not own (currently the WP-WF3
    procedure injector), so it reads the factory through a public attribute
    when one exists and falls back to the private name rather than adding a
    new harness service.  Warm-up is skipped when no factory is reachable.
    """

    for name in ("embedder_factory", "_embedder_factory"):
        attribute = getattr(candidate, name, None)
        if callable(attribute):
            return attribute
    return None


def warmup_embedder(
    embedder_factory: Callable[[], Any] | None,
    *,
    text: str = WARMUP_TEXT,
) -> threading.Thread | None:
    """Run one trivial embed on a daemon thread (fail-open, fire-and-forget).

    The MLX stack loads its model on the first ``embed()``; doing that at
    capability mount keeps the load off the first recall/selection of the run.
    A failure is recorded like any other selection-path failure and never
    reaches the caller.
    """

    if not callable(embedder_factory):
        return None

    def run() -> None:
        try:
            embedder = embedder_factory()
            if embedder is None or not embedder_needs_warmup(embedder):
                return
            embedder.embed([text])
        except Exception as exc:  # noqa: BLE001 - warm-up is fail-open
            note_selection_error("embedder", exc)

    thread = threading.Thread(
        target=run, name="recall-embedder-warmup", daemon=True
    )
    thread.start()
    return thread


def read_episode_events(events_path: str | Path) -> list[dict[str, Any]]:
    """Read valid WP-I1 ``episode_outcome`` schema-v1 records directly."""

    path = Path(events_path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(event, dict)
                and event.get("type") == "episode_outcome"
                and event.get("schema_v") == 1
                and str(event.get("run_id", "")).strip()
                and str(event.get("goal_text", "")).strip()
                and isinstance(event.get("apps"), list)
            ):
                events.append(event)
    return events


def _episode_text(event: Mapping[str, Any]) -> str:
    apps = " ".join(str(app) for app in event.get("apps", []) if app)
    outcome = "success" if event.get("success") is True else "failure"
    return (
        f"goal: {event.get('goal_text', '')}\n"
        f"apps: {apps}\n"
        f"outcome: {outcome}; reason: {event.get('reason', '')}; "
        f"verifier: {event.get('verifier', 'skipped')}"
    )


def episode_is_indexable(
    event: Mapping[str, Any], *, min_steps: int = 2
) -> bool:
    """Apply the shared write-time quality gate for semantic episodes."""

    if min_steps < 0:
        raise ValueError("min_steps must be non-negative")
    goal = str(event.get("goal_text", "")).strip()
    try:
        steps = int(event.get("steps", 0))
    except (TypeError, ValueError):
        return False
    return bool(goal) and steps >= min_steps


def _parse_timestamp(value: Any, *, default: float | None = None) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return datetime.now(timezone.utc).timestamp() if default is None else default


def _alias_ref_id(entry: Mapping[str, Any]) -> str:
    identity = "\0".join(
        str(entry.get(key, "")) for key in ("term", "package", "kind", "scope")
    )
    return "alias:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(value or ""))


def _unique_text(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value_text = str(value or "").strip()
        key = " ".join(value_text.split()).casefold()
        if value_text and key not in seen:
            seen.add(key)
            result.append(value_text)
    return result


def _prefer_chinese_names(values: Iterable[Any]) -> list[str]:
    """Keep stable order while preferring names without Latin scaffolding."""

    unique = _unique_text(values)
    return sorted(
        unique,
        key=lambda value: (
            bool(re.search(r"[A-Za-z]", value)),
            unique.index(value),
        ),
    )


def _static_identity(package: str, registry: Any | None = None) -> Any | None:
    if registry is None:
        from phone_agent.config.apps import DEFAULT_APP_REGISTRY

        registry = DEFAULT_APP_REGISTRY
    resolution = registry.resolve_package(package)
    return resolution.identity if resolution.status == "resolved" else None


def _alias_document(
    entry: Mapping[str, Any],
    *,
    all_entries: Sequence[Mapping[str, Any]],
    registry: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a package-aware alias document and deterministic mention terms.

    Chinese naming follows the required provenance chain: learned/user App-KB
    names first, then static registry canonical/display/aliases, then package-only.
    Device labels remain the display-name field but cannot displace a verified
    learned/user Chinese name as the canonical semantic name.
    """

    package = str(entry.get("package", "")).strip()
    related = [
        item
        for item in all_entries
        if str(item.get("package", "")).strip() == package
        and not bool(item.get("stale", False))
        and str(item.get("scope", "")).strip()
        in {"global", str(entry.get("scope", "")).strip()}
    ]
    learned = sorted(
        (item for item in related if item.get("kind") in {"learned", "user"}),
        key=lambda item: (
            0 if item.get("kind") == "learned" else 1,
            -int(item.get("success_count", 0)),
            str(item.get("term", "")),
        ),
    )
    learned_chinese = _prefer_chinese_names(
        value
        for item in learned
        for value in (item.get("label"), item.get("term"))
        if _contains_cjk(str(value or ""))
    )

    identity = _static_identity(package, registry)
    static_values: list[str] = []
    if identity is not None:
        static_values = [
            str(identity.canonical_id),
            str(identity.display_name),
            *sorted(str(value) for value in identity.aliases),
        ]
    static_chinese = _prefer_chinese_names(
        value for value in static_values if _contains_cjk(value)
    )
    chinese_names = _unique_text([*learned_chinese, *static_chinese])
    canonical_name = chinese_names[0] if chinese_names else package
    chinese_aliases = chinese_names[1:]

    label = str(entry.get("label", "")).strip()
    display_name = label
    if (not display_name or display_name == package) and identity is not None:
        display_name = str(identity.display_name).strip()
    display_name = display_name or package

    mention_terms = _unique_text(
        [
            *(
                value
                for item in related
                for value in (item.get("term"), item.get("label"))
            ),
            *static_values,
            package,
        ]
    )
    term = str(entry.get("term", "")).strip()
    pure_package = term == label == package and not chinese_names
    name_source = (
        "app_kb"
        if learned_chinese
        else "static_registry"
        if static_chinese
        else "package"
    )
    document = (
        " | ".join(
            (canonical_name, " ".join(chinese_aliases), display_name, package)
        )
        if chinese_names
        else package
    )
    return document, {
        "canonical_name": canonical_name,
        "chinese_aliases": chinese_aliases,
        "display_name": display_name,
        "mention_terms": mention_terms,
        "name_source": name_source,
        "semantic_eligible": not pure_package,
    }


def alias_snapshot(entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Return stable fingerprints used to isolate aliases changed this run."""

    snapshot: dict[str, str] = {}
    for entry in entries:
        try:
            ref_id = _alias_ref_id(entry)
            payload = json.dumps(dict(entry), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            continue
        snapshot[ref_id] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return snapshot


@contextmanager
def _index_file_lock(db_path: str | Path):
    """Serialize derived-index writers across threads and processes."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with _INDEX_LOCK, lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fts_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(str(query or "")):
        value = match.group(0)
        terms.append(value)
        if any("\u3400" <= char <= "\u9fff" for char in value):
            terms.extend(value[index : index + 2] for index in range(len(value) - 1))
    return list(dict.fromkeys(term for term in terms if term))


def _fts_query(query: str) -> str:
    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _fts_terms(query)
    )


def _keyword_score(query: str, text: str, *, fts_hit: bool) -> float:
    if not fts_hit:
        return 0.0
    normalized_query = "".join(str(query).casefold().split())
    normalized_text = "".join(str(text).casefold().split())
    if normalized_query and normalized_query in normalized_text:
        return 1.0
    query_terms = set(_fts_terms(query))
    text_terms = set(_fts_terms(text))
    overlap = len(query_terms & text_terms) / max(1, len(query_terms))
    return min(1.0, overlap)


def _exact_lexical_match(query: str, text: str) -> bool:
    normalized_query = "".join(str(query).casefold().split())
    normalized_text = "".join(str(text).casefold().split())
    return bool(normalized_query) and normalized_query in normalized_text


def _mention_occurs(term: str, query: str) -> bool:
    from phone_agent.v2.names import mention_occurs

    return mention_occurs(term, query)


def _procedure_device_scopes(device_id: str | None) -> tuple[str, ...]:
    """Return the device_scope values a procedure row may carry to match."""

    serial = " ".join(str(device_id or "").split()).removeprefix("device:")
    if not serial or serial == "unknown":
        return (_GLOBAL_DEVICE_SCOPE,)
    return (serial, _GLOBAL_DEVICE_SCOPE)


def _procedure_app_matches(app_scope: str | None, app_package: str | None) -> bool:
    """Deterministic app gate: package equality, or general cards with no app.

    App matching never uses embeddings, so a wrong injection is bounded to
    "the wrong card inside the right app".
    """

    if app_package:
        return str(app_scope or "") == app_package
    from phone_agent.v2.evolution import GENERAL_APP_SCOPE

    return str(app_scope or "") == GENERAL_APP_SCOPE


def _procedure_document(lesson: Any) -> str:
    """Return the retrieval document: card title plus a steps summary."""

    title = str(getattr(lesson, "text", "") or "").strip()
    steps = [str(step).strip() for step in getattr(lesson, "steps", ()) or ()]
    steps = [step for step in steps if step]
    return " | ".join([title, *steps]) if steps else title


def _procedure_source_hash(lesson: Any) -> str:
    payload = {
        "lesson_id": str(getattr(lesson, "lesson_id", "")),
        "version": int(getattr(lesson, "version", 1)),
        "status": str(getattr(lesson, "status", "")),
        "kind": str(getattr(lesson, "kind", "")),
        "app_scope": getattr(lesson, "app_scope", None),
        "device_scope": (getattr(lesson, "scope", {}) or {}).get("device"),
        "document": _procedure_document(lesson),
        "pitfalls": getattr(lesson, "pitfalls", None),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_procedure_lessons(
    lessons_dir: str | Path = "memory/lessons",
) -> list[Any]:
    """Return the injectable procedure cards of the materialized lesson view.

    Only the current-version view is read (never rebuilt) and only cards that
    pass :func:`phone_agent.v2.evolution.lesson_injectable` are returned, so a
    revoked or demoted card simply disappears from the recall namespace.  A
    missing or damaged view fails open to no cards.
    """

    from phone_agent.v2.evolution import LessonCandidate, lesson_injectable

    path = Path(lessons_dir) / "lessons.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    cards: list[Any] = []
    for item in payload:
        try:
            lesson = LessonCandidate.from_dict(item)
        except (TypeError, ValueError):
            continue
        if lesson.kind == "procedure" and lesson_injectable(lesson):
            cards.append(lesson)
    return cards


@dataclass(frozen=True)
class ProcedureSelection:
    """Structured outcome of one procedure-card recall attempt.

    ``candidates`` counts the rows surviving revocation plus the device gate and
    ``filtered`` the rows additionally surviving the app gate, so a zero-recall
    run stays a visible, attributable neutral state instead of silent absence.
    """

    lesson_id: str | None = None
    title: str = ""
    steps: tuple[str, ...] = ()
    pitfalls: str | None = None
    app_scope: str | None = None
    device_scope: str | None = None
    score: float = 0.0
    candidates: int = 0
    filtered: int = 0
    reason: str = "no_cards"

    @property
    def selected(self) -> bool:
        return bool(self.lesson_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "title": self.title,
            "steps": list(self.steps),
            "pitfalls": self.pitfalls,
            "app_scope": self.app_scope,
            "device_scope": self.device_scope,
            "score": round(float(self.score), 6),
            "candidates": int(self.candidates),
            "filtered": int(self.filtered),
            "reason": self.reason,
            "selected": self.selected,
        }


def format_procedure_block(
    selection: ProcedureSelection | Mapping[str, Any] | None,
    *,
    max_tokens: int = PROCEDURE_MAX_TOKENS,
) -> str | None:
    """Render at most one procedure card inside its own token budget.

    The card is a non-binding reference: the block says so explicitly and names
    the source lesson id.  Steps are appended in order and dropped (never
    rewritten) once the budget is spent.  Pure function — no I/O, no index.
    """

    payload = (
        selection.to_dict()
        if isinstance(selection, ProcedureSelection)
        else dict(selection or {})
    )
    lesson_id = str(payload.get("lesson_id") or "").strip()
    if not lesson_id:
        return None
    from phone_agent.v2.evolution import GENERAL_APP_SCOPE
    from phone_agent.v2.middleware._tokens import estimate_text_tokens

    budget = int(max_tokens)
    if budget <= 0:
        return None
    header = "[过程参考]（历史过程卡，仅供参考，不是规则；与当前世界状态冲突时以观测为准）"
    title = str(payload.get("title") or "").strip()
    scope = str(payload.get("app_scope") or GENERAL_APP_SCOPE)
    lines = [header, f"{title}（来源 {lesson_id} · app {scope}）"]
    truncated = False
    for index, step in enumerate(payload.get("steps") or (), start=1):
        candidate = f"{index}. {step}"
        if estimate_text_tokens("\n".join([*lines, candidate])) > budget:
            truncated = True
            break
        lines.append(candidate)
    pitfalls = str(payload.get("pitfalls") or "").strip()
    if pitfalls:
        candidate = f"注意：{pitfalls}"
        if estimate_text_tokens("\n".join([*lines, candidate])) <= budget:
            lines.append(candidate)
        else:
            truncated = True
    if truncated:
        marker = "…（过程卡已按预算截断）"
        if estimate_text_tokens("\n".join([*lines, marker])) <= budget:
            lines.append(marker)
    return "\n".join(lines)


@contextmanager
def _selection_scope(namespace: str):
    """Record one selection phase's failure and re-raise it unchanged.

    The fail-open return belongs to the caller (the shadow run-start path and
    the procedure injector); the scope only makes the failure visible in the
    trace and the scorecard before control leaves this module.
    """

    try:
        yield
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        note_selection_error(namespace, exc)
        raise


class VecIndex:
    """Single-file sqlite-vec + FTS5 hybrid index."""

    def __init__(
        self,
        db_path: str | Path = "memory/vec.db",
        *,
        embedder: Embedder,
    ) -> None:
        self.db_path = Path(db_path)
        self.embedder = embedder
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self._load_extension()
        self._initialize()

    def _load_extension(self) -> None:
        import sqlite_vec

        self.connection.enable_load_extension(True)
        try:
            sqlite_vec.load(self.connection)
        finally:
            self.connection.enable_load_extension(False)

    def _initialize(self) -> None:
        dimension = int(self.embedder.dimension)
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS recall_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recall_items (
                id INTEGER PRIMARY KEY,
                namespace TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                text TEXT NOT NULL,
                embed_model TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                app_package TEXT,
                device_scope TEXT NOT NULL,
                ts REAL NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,
                revoked INTEGER NOT NULL DEFAULT 0,
                UNIQUE(namespace, ref_id, embed_model)
            );
            CREATE INDEX IF NOT EXISTS recall_items_filter_idx
                ON recall_items(embed_model, device_scope, revoked, namespace);
            CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(
                ref_id UNINDEXED, text, tokenize='unicode61'
            );
            """
        )
        stored = self.connection.execute(
            "SELECT value FROM recall_index_meta WHERE key = 'embed_dim'"
        ).fetchone()
        if stored is not None and int(stored[0]) != dimension:
            self.connection.close()
            raise ValueError(
                f"vector DB dimension is {stored[0]}, configured dimension is {dimension}; "
                "run --rebuild-vec"
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO recall_index_meta(key, value) VALUES ('embed_dim', ?)",
            (str(dimension),),
        )
        self.connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS recall_vectors "
            f"USING vec0(embedding float[{dimension}])"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "VecIndex":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def count(self, *, embed_model: str | None = None) -> int:
        if embed_model is None:
            row = self.connection.execute("SELECT count(*) FROM recall_items").fetchone()
        else:
            row = self.connection.execute(
                "SELECT count(*) FROM recall_items WHERE embed_model = ?",
                (embed_model,),
            ).fetchone()
        return int(row[0])

    def upsert(
        self,
        *,
        namespace: str,
        ref_id: str,
        text: str,
        metadata: Mapping[str, Any],
    ) -> None:
        if namespace not in _NAMESPACES:
            raise ValueError(f"unknown namespace: {namespace!r}")
        ref_id = str(ref_id).strip()
        text = str(text).strip()
        if not ref_id or not text:
            raise ValueError("ref_id and text must not be empty")
        vector = self.embedder.embed([text])[0]
        from sqlite_vec import serialize_float32

        payload = dict(metadata)
        device_scope = str(payload.get("device_scope", "")).strip()
        if not device_scope:
            raise ValueError("metadata.device_scope must not be empty")
        apps = payload.get("apps", [])
        app_package = str(payload.get("app_package", "") or "").strip()
        if not app_package and isinstance(apps, list) and apps:
            app_package = str(apps[0])
        timestamp = _parse_timestamp(payload.get("ts"))
        generation = int(payload.get("generation", 1))
        revoked = int(bool(payload.get("revoked", False)))
        payload.update(
            {
                "app_package": app_package or None,
                "device_scope": device_scope,
                "ts": timestamp,
                "generation": generation,
                "revoked": bool(revoked),
            }
        )
        metadata_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self.connection:
            existing = self.connection.execute(
                "SELECT id FROM recall_items "
                "WHERE namespace = ? AND ref_id = ? AND embed_model = ?",
                (namespace, ref_id, self.embedder.model_id),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO recall_items(
                        namespace, ref_id, text, embed_model, metadata_json,
                        app_package, device_scope, ts, generation, revoked
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        namespace,
                        ref_id,
                        text,
                        self.embedder.model_id,
                        metadata_json,
                        app_package or None,
                        device_scope,
                        timestamp,
                        generation,
                        revoked,
                    ),
                )
                row_id = int(cursor.lastrowid)
            else:
                row_id = int(existing[0])
                self.connection.execute("DELETE FROM recall_vectors WHERE rowid = ?", (row_id,))
                self.connection.execute("DELETE FROM recall_fts WHERE rowid = ?", (row_id,))
                self.connection.execute(
                    """
                    UPDATE recall_items SET text = ?, metadata_json = ?, app_package = ?,
                        device_scope = ?, ts = ?, generation = ?, revoked = ?
                    WHERE id = ?
                    """,
                    (
                        text,
                        metadata_json,
                        app_package or None,
                        device_scope,
                        timestamp,
                        generation,
                        revoked,
                        row_id,
                    ),
                )
            self.connection.execute(
                "INSERT INTO recall_vectors(rowid, embedding) VALUES (?, ?)",
                (row_id, serialize_float32(vector)),
            )
            self.connection.execute(
                "INSERT INTO recall_fts(rowid, ref_id, text) VALUES (?, ?, ?)",
                (row_id, ref_id, text + "\n" + " ".join(_fts_terms(text))),
            )

    def delete_missing(self, namespace: str, ref_ids: set[str]) -> int:
        """Delete derived rows absent from the authoritative JSON source."""

        if namespace not in _NAMESPACES:
            raise ValueError(f"unknown namespace: {namespace!r}")
        rows = self.connection.execute(
            "SELECT id, ref_id, embed_model FROM recall_items "
            "WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        deleted = [
            row
            for row in rows
            if str(row["ref_id"]) not in ref_ids
            or str(row["embed_model"]) != self.embedder.model_id
        ]
        with self.connection:
            for row in deleted:
                row_id = int(row["id"])
                self.connection.execute(
                    "DELETE FROM recall_vectors WHERE rowid = ?", (row_id,)
                )
                self.connection.execute(
                    "DELETE FROM recall_fts WHERE rowid = ?", (row_id,)
                )
                self.connection.execute("DELETE FROM recall_items WHERE id = ?", (row_id,))
        return len(deleted)

    def source_hash(self, namespace: str, ref_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT i.metadata_json FROM recall_items AS i "
            "JOIN recall_vectors AS v ON v.rowid = i.id "
            "JOIN recall_fts AS f ON f.rowid = i.id "
            "WHERE i.namespace = ? AND i.ref_id = ? AND i.embed_model = ?",
            (namespace, ref_id, self.embedder.model_id),
        ).fetchone()
        if row is None:
            return None
        try:
            return str(json.loads(row[0]).get("source_hash", "")) or None
        except (TypeError, json.JSONDecodeError):
            return None

    def index_episode(self, event: Mapping[str, Any], *, min_steps: int = 2) -> bool:
        if not episode_is_indexable(event, min_steps=min_steps):
            return False
        apps = [str(app) for app in event.get("apps", []) if str(app).strip()]
        source_hash = hashlib.sha256(
            json.dumps(dict(event), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.source_hash("episode", str(event["run_id"])) == source_hash:
            return True
        self.upsert(
            namespace="episode",
            ref_id=str(event["run_id"]),
            text=_episode_text(event),
            metadata={
                "app_package": apps[0] if apps else None,
                "apps": apps,
                "device_scope": str(event.get("device_scope", "")),
                "ts": event.get("ts_end", event.get("ts_start", 0.0)),
                "generation": int(event.get("schema_v", 1)),
                "revoked": bool(event.get("revoked", False)),
                "success": event.get("success") is True,
                "reason": str(event.get("reason", "")),
                "verifier": str(event.get("verifier", "skipped")),
                "source_hash": source_hash,
            },
        )
        return True

    def index_episodes(
        self, events_path: str | Path, *, min_steps: int = 2
    ) -> dict[str, int]:
        indexed = 0
        for event in read_episode_events(events_path):
            indexed += int(self.index_episode(event, min_steps=min_steps))
        return {"episodes": indexed}

    def index_alias_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        all_entries: Sequence[Mapping[str, Any]] | None = None,
        registry: Any | None = None,
    ) -> int:
        indexed = 0
        context = list(all_entries if all_entries is not None else entries)
        for entry in entries:
            term = str(entry.get("term", "")).strip()
            label = str(entry.get("label", "")).strip()
            package = str(entry.get("package", "")).strip()
            scope = str(entry.get("scope", "")).strip()
            if (
                not term
                or not label
                or not package
                or not scope
                or bool(entry.get("stale", False))
            ):
                continue
            text, derived = _alias_document(
                entry, all_entries=context, registry=registry
            )
            source_hash = hashlib.sha256(
                json.dumps(
                    {"entry": dict(entry), "derived": derived},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            ref_id = _alias_ref_id(entry)
            if self.source_hash("app_alias", ref_id) == source_hash:
                indexed += 1
                continue
            self.upsert(
                namespace="app_alias",
                ref_id=ref_id,
                text=text,
                metadata={
                    "app_package": package,
                    "device_scope": scope,
                    # last_seen advances on device inventory refresh. Only a
                    # confirmed successful use makes an alias a recency winner.
                    "ts": (
                        _parse_timestamp(entry.get("last_success"), default=0.0)
                        if entry.get("last_success")
                        else 0.0
                    ),
                    "generation": int(entry.get("success_count", 0)) + 1,
                    "revoked": False,
                    "term": term,
                    "label": label,
                    "kind": str(entry.get("kind", "")),
                    "success_count": int(entry.get("success_count", 0)),
                    "last_success": entry.get("last_success"),
                    "source_hash": source_hash,
                    **derived,
                },
            )
            indexed += 1
        return indexed

    def index_app_aliases(
        self,
        *,
        memory_dir: str | Path = "memory",
        store: Any | None = None,
    ) -> dict[str, int]:
        if store is None:
            from phone_agent.v2.appkb import AppKnowledgeStore

            store = AppKnowledgeStore(str(memory_dir))
        entries = store.entries(include_stale=True)
        indexed = self.index_alias_entries(entries, all_entries=entries)
        return {"app_aliases": indexed}

    def index_procedure_lessons(self, lessons: Sequence[Any]) -> int:
        """Upsert injectable procedure cards into the ``procedure`` namespace."""

        indexed = 0
        for lesson in lessons:
            document = _procedure_document(lesson)
            if not document:
                continue
            source_hash = _procedure_source_hash(lesson)
            ref_id = str(getattr(lesson, "lesson_id", "")).strip()
            if not ref_id:
                continue
            if self.source_hash("procedure", ref_id) == source_hash:
                indexed += 1
                continue
            device_scope = (getattr(lesson, "scope", {}) or {}).get("device")
            self.upsert(
                namespace="procedure",
                ref_id=ref_id,
                text=document,
                metadata={
                    "device_scope": str(device_scope or "").strip()
                    or _GLOBAL_DEVICE_SCOPE,
                    "ts": getattr(lesson, "created_ts", 0.0),
                    "generation": int(getattr(lesson, "version", 1)),
                    "revoked": False,
                    "lesson_id": ref_id,
                    "status": str(getattr(lesson, "status", "")),
                    "kind": str(getattr(lesson, "kind", "procedure")),
                    # Belt-and-braces for the selector: the namespace is also
                    # pruned on state change, but a stale row must never inject.
                    "injectable": True,
                    "title": str(getattr(lesson, "text", "")),
                    "steps": list(getattr(lesson, "steps", ()) or ()),
                    "pitfalls": getattr(lesson, "pitfalls", None),
                    "app_scope": getattr(lesson, "app_scope", None),
                    "source_hash": source_hash,
                },
            )
            indexed += 1
        return indexed

    def sync_procedure_lessons(self, lessons: Sequence[Any]) -> dict[str, int]:
        """Make the ``procedure`` namespace match the current lesson view.

        Promotion to an injectable status upserts; revocation, demotion, or any
        other loss of injectability removes the row because the source view no
        longer lists the card.
        """

        indexed = self.index_procedure_lessons(lessons)
        removed = self.delete_missing(
            "procedure",
            {str(getattr(lesson, "lesson_id", "")) for lesson in lessons},
        )
        return {"procedures": indexed, "removed_procedures": removed}

    def select_procedure(
        self,
        goal_text: str,
        *,
        app_package: str | None = None,
        device_id: str | None = None,
        min_score: float = PROCEDURE_MIN_SCORE,
    ) -> ProcedureSelection:
        """Hard-filter then embedding top-1 one procedure card for a goal.

        Hard filters: device scope (a card scoped to another device is never a
        candidate) and exact ``app_scope`` equality — with no app known yet only
        general cards survive.  The remaining pool is ranked by cosine against
        the card document and the single best card is returned when it clears
        ``min_score``; the channel quota is :data:`PROCEDURE_MAX_ITEMS` card
        inside :data:`PROCEDURE_MAX_TOKENS` tokens, separate from rules.
        """

        query = str(goal_text or "").strip()
        if not query:
            return ProcedureSelection(reason="empty_query")
        if not 0.0 <= float(min_score) <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        with _selection_scope("procedure"):
            return self._select_procedure(
                query,
                app_package=app_package,
                device_id=device_id,
                min_score=min_score,
            )

    def _select_procedure(
        self,
        query: str,
        *,
        app_package: str | None,
        device_id: str | None,
        min_score: float,
    ) -> ProcedureSelection:
        from sqlite_vec import serialize_float32

        query_vector = serialize_float32(self.embedder.embed([query])[0])
        rows = self.connection.execute(
            """
            SELECT i.ref_id, i.device_scope, i.metadata_json,
                   vec_distance_cosine(v.embedding, ?) AS vector_distance
            FROM recall_items AS i
            JOIN recall_vectors AS v ON v.rowid = i.id
            WHERE i.namespace = 'procedure' AND i.embed_model = ? AND i.revoked = 0
            """,
            (query_vector, self.embedder.model_id),
        ).fetchall()
        if not rows:
            return ProcedureSelection(reason="no_cards")

        scopes = _procedure_device_scopes(device_id)
        wanted_app = str(app_package or "").strip() or None
        in_device = [row for row in rows if str(row["device_scope"]) in scopes]
        if not in_device:
            return ProcedureSelection(reason="device_scope_mismatch")

        scored: list[tuple[float, str, Mapping[str, Any]]] = []
        for row in in_device:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if metadata.get("injectable") is not True:
                continue
            if not _procedure_app_matches(metadata.get("app_scope"), wanted_app):
                continue
            score = max(0.0, min(1.0, 1.0 - float(row["vector_distance"])))
            scored.append((score, str(row["ref_id"]), metadata))
        if not scored:
            return ProcedureSelection(
                candidates=len(in_device), reason="app_scope_mismatch"
            )

        score, ref_id, metadata = min(scored, key=lambda item: (-item[0], item[1]))
        if score < min_score:
            return ProcedureSelection(
                candidates=len(in_device),
                filtered=len(scored),
                score=round(score, 6),
                reason="below_threshold",
            )
        return ProcedureSelection(
            lesson_id=ref_id,
            title=str(metadata.get("title", "")),
            steps=tuple(str(step) for step in metadata.get("steps", ()) or ()),
            pitfalls=metadata.get("pitfalls"),
            app_scope=metadata.get("app_scope"),
            device_scope=str(metadata.get("device_scope") or "") or None,
            score=round(score, 6),
            candidates=len(in_device),
            filtered=len(scored),
            reason="hit",
        )

    def recall(
        self,
        query: str,
        *,
        device_scope: str,
        top_k: int = 1,
        min_score: float = 0.50,
        decay_lambda: float = 0.02,
        now: float | None = None,
        namespaces: Sequence[str] = ("episode", "app_alias"),
    ) -> list[dict[str, Any]]:
        """Return deterministic app mentions plus semantic episode winners.

        top_k is the episode quota. App mentions never consume that quota.
        """

        query = str(query).strip()
        device_scope = str(device_scope).strip()
        selected = tuple(dict.fromkeys(str(value) for value in namespaces))
        if not query or not device_scope or top_k <= 0 or not selected:
            return []
        if any(namespace not in _NAMESPACES for namespace in selected):
            raise ValueError("namespaces must contain only episode/app_alias/procedure")
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        if decay_lambda < 0.0:
            raise ValueError("decay_lambda must be non-negative")
        app_candidates: list[dict[str, Any]] = []
        if "app_alias" in selected:
            with _selection_scope("app_alias"):
                app_candidates = self._mention_candidates(
                    query, device_scope=device_scope
                )
        if "episode" not in selected:
            return app_candidates
        with _selection_scope("episode"):
            episodes = self._episode_candidates(
                query,
                device_scope=device_scope,
                top_k=top_k,
                min_score=min_score,
                decay_lambda=decay_lambda,
                now=now,
            )
        return [*app_candidates, *episodes]

    def _episode_candidates(
        self,
        query: str,
        *,
        device_scope: str,
        top_k: int,
        min_score: float,
        decay_lambda: float,
        now: float | None,
    ) -> list[dict[str, Any]]:
        """Rank the semantic episode namespace for one already-validated query."""

        episode_count = self.connection.execute(
            "SELECT count(*) FROM recall_items "
            "WHERE embed_model = ? AND namespace = 'episode'",
            (self.embedder.model_id,),
        ).fetchone()
        if not episode_count or int(episode_count[0]) == 0:
            return []

        from sqlite_vec import serialize_float32

        query_vector = serialize_float32(self.embedder.embed([query])[0])
        filter_sql = (
            "i.embed_model = ? AND i.revoked = 0 "
            "AND i.namespace = 'episode' AND i.device_scope = ?"
        )
        params: list[Any] = [self.embedder.model_id, device_scope]
        rows = self.connection.execute(
            f"""
            SELECT i.*, vec_distance_cosine(v.embedding, ?) AS vector_distance
            FROM recall_items AS i
            JOIN recall_vectors AS v ON v.rowid = i.id
            WHERE {filter_sql}
            """,
            [query_vector, *params],
        ).fetchall()

        fts_hits: set[int] = set()
        fts_query = _fts_query(query)
        if rows and fts_query:
            fts_hits = {
                int(row[0])
                for row in self.connection.execute(
                    f"""
                    SELECT i.id
                    FROM recall_fts
                    JOIN recall_items AS i ON i.id = recall_fts.rowid
                    WHERE recall_fts MATCH ? AND {filter_sql}
                    """,
                    [fts_query, *params],
                ).fetchall()
            }

        current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        episode_candidates: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            vector_score = max(0.0, min(1.0, 1.0 - float(row["vector_distance"])))
            keyword_score = _keyword_score(
                query, row["text"], fts_hit=int(row["id"]) in fts_hits
            )
            exact_lexical = _exact_lexical_match(query, row["text"])
            vector_gate = min_score if exact_lexical else min(1.0, min_score + 0.10)
            if vector_score < vector_gate:
                continue
            age_days = max(0.0, (current - float(row["ts"])) / 86_400.0)
            time_score = math.exp(-decay_lambda * age_days)
            score = 0.75 * vector_score + 0.25 * keyword_score
            if score < min_score:
                continue
            reasons = [f"vector={vector_score:.3f}"]
            if keyword_score > 0.0:
                reasons.append(f"lexical={keyword_score:.3f}")
            reasons.append(f"recency_tiebreak={time_score:.3f}")
            episode_candidates.append(
                {
                    "namespace": "episode",
                    "ref_id": row["ref_id"],
                    "score": round(score, 6),
                    "vector_score": round(vector_score, 6),
                    "keyword_score": round(keyword_score, 6),
                    "time_score": round(time_score, 6),
                    "match_reasons": reasons,
                    "metadata": metadata,
                }
            )
        episode_candidates.sort(
            key=lambda item: (-item["score"], -item["time_score"], item["ref_id"])
        )
        return episode_candidates[:top_k]

    def app_name_vector_candidates(
        self, query: str, *, device_scope: str, top_k: int = 20
    ) -> list[dict[str, Any]]:
        """Return App-KB vector neighbours for the names.py embedding route."""

        clean_query = str(query or "").strip()
        clean_scope = str(device_scope or "").strip()
        if not clean_query or not clean_scope or top_k <= 0:
            return []
        count = self.connection.execute(
            "SELECT count(*) FROM recall_items "
            "WHERE namespace = 'app_alias' AND embed_model = ? "
            "AND revoked = 0 AND (device_scope = ? OR device_scope = 'global')",
            (self.embedder.model_id, clean_scope),
        ).fetchone()
        if not count or int(count[0]) == 0:
            return []

        from sqlite_vec import serialize_float32

        query_vector = serialize_float32(self.embedder.embed([clean_query])[0])
        rows = self.connection.execute(
            """
            SELECT i.ref_id, i.metadata_json,
                   vec_distance_cosine(v.embedding, ?) AS vector_distance
            FROM recall_items AS i
            JOIN recall_vectors AS v ON v.rowid = i.id
            WHERE i.namespace = 'app_alias' AND i.embed_model = ?
              AND i.revoked = 0
              AND (i.device_scope = ? OR i.device_scope = 'global')
            ORDER BY vector_distance ASC, i.ref_id ASC
            LIMIT ?
            """,
            (query_vector, self.embedder.model_id, clean_scope, int(top_k)),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if metadata.get("semantic_eligible") is False:
                continue
            package = str(metadata.get("app_package", "")).strip()
            if not package:
                continue
            candidates.append(
                {
                    "package": package,
                    "term": str(metadata.get("term", metadata.get("label", package))),
                    "label": str(metadata.get("label", "")),
                    "kind": str(metadata.get("kind", "alias")),
                    "success_count": int(metadata.get("success_count", 0) or 0),
                    "last_success": metadata.get("last_success"),
                    "ref_id": str(row["ref_id"]),
                    "sim": max(
                        0.0, min(1.0, 1.0 - float(row["vector_distance"]))
                    ),
                    "metadata": metadata,
                }
            )
        return candidates

    def _mention_candidates(
        self, query: str, *, device_scope: str
    ) -> list[dict[str, Any]]:
        """Resolve static and learned app mentions without vector competition."""

        records: list[tuple[str, str, str, dict[str, Any]]] = []
        eligible: list[dict[str, Any]] = []

        from phone_agent.config.apps import DEFAULT_APP_REGISTRY

        for identity in DEFAULT_APP_REGISTRY.identities:
            if identity.observation_only:
                continue
            # aliases_for exposes only terms owned by exactly one identity;
            # ambiguous static vocabulary therefore fails closed.
            terms = (
                term
                for term in DEFAULT_APP_REGISTRY.aliases_for(identity)
                if len(term) >= 2
            )
            for term in terms:
                records.append(
                    (
                        identity.primary_package,
                        term,
                        f"registry:{identity.canonical_id}",
                        {"mention_term": term, "name_source": "static_registry"},
                    )
                )

        rows = self.connection.execute(
            "SELECT ref_id, metadata_json FROM recall_items "
            "WHERE namespace = 'app_alias' AND embed_model = ? AND revoked = 0 "
            "AND (device_scope = ? OR device_scope = 'global')",
            (self.embedder.model_id, device_scope),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            terms = metadata.get("mention_terms", [])
            if not isinstance(terms, list):
                terms = []
            package = str(metadata.get("app_package", ""))
            for term in terms:
                records.append(
                    (
                        package,
                        str(term),
                        str(row["ref_id"]),
                        metadata,
                    )
                )

        owners_by_term: dict[str, set[str]] = {}
        for package, term, _ref_id, _metadata in records:
            normalized = " ".join(term.split()).casefold()
            if normalized and package:
                owners_by_term.setdefault(normalized, set()).add(package)
        for package, term, ref_id, metadata in records:
            normalized = " ".join(term.split()).casefold()
            if len(owners_by_term.get(normalized, ())) != 1:
                continue
            if not _mention_occurs(term, query):
                continue
            eligible.append(
                {
                    **dict(metadata),
                    "term": term,
                    "label": str(metadata.get("label", term)),
                    "package": package,
                    "kind": str(metadata.get("kind", "registry")),
                    "ref_id": ref_id,
                }
            )

        from phone_agent.v2.names import ResolverSettings, generate_candidates

        ranked = generate_candidates(
            query,
            registry=(),
            kb_entries=eligible,
            settings=ResolverSettings(
                min_score=0.0,
                margin=0.0,
                top_k=max(1, len(eligible)),
                lexical=True,
                pinyin=False,
                embed=False,
            ),
        )
        return [
            {
                "namespace": "app_alias",
                "ref_id": candidate.source_ref or f"package:{candidate.package}",
                "score": round(candidate.score, 6),
                "vector_score": None,
                "keyword_score": round(candidate.sim, 6),
                "time_score": None,
                "match_reasons": [
                    f"mention={candidate.matched_term}",
                    f"route={candidate.source_route}",
                ],
                "metadata": {
                    **dict(candidate.source_entry or {}),
                    "app_package": candidate.package,
                },
            }
            for candidate in ranked
        ]


def evaluate_recall(
    recalled: Sequence[Mapping[str, Any]],
    actual_apps: Sequence[str] | set[str],
    intent_apps: Sequence[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Evaluate one run at candidate and package level with explicit denominators."""

    recalled_apps: set[str] = set()
    top_candidate_apps: set[str] = set()
    inferred_intent_apps: set[str] = set()
    for position, candidate in enumerate(recalled):
        metadata = candidate.get("metadata", {})
        if not isinstance(metadata, Mapping):
            continue
        candidate_apps: set[str] = set()
        package = str(metadata.get("app_package", "") or "").strip()
        if package:
            candidate_apps.add(package)
        apps = metadata.get("apps", [])
        if isinstance(apps, list):
            candidate_apps.update(str(app).strip() for app in apps if str(app).strip())
        if position == 0:
            top_candidate_apps = set(candidate_apps)
        recalled_apps.update(candidate_apps)
        if candidate.get("namespace") == "app_alias":
            inferred_intent_apps.update(candidate_apps)
    actual = {str(app).strip() for app in actual_apps if str(app).strip()}
    intent = (
        inferred_intent_apps
        if intent_apps is None
        else {str(app).strip() for app in intent_apps if str(app).strip()}
    )
    matched = recalled_apps & actual
    hit = bool(matched)
    contaminated = bool(recalled_apps - actual)
    return {
        "hit": hit,
        "hit_at_1": bool(top_candidate_apps & actual),
        "conditional_hit": hit if recalled_apps else False,
        "contaminated_run": contaminated,
        # Compatibility keys retained for the existing web reader.
        "false_hit": contaminated,
        "recalled_apps": sorted(recalled_apps),
        "actual_apps": sorted(actual),
        "intent_apps": sorted(intent),
        "matched_apps": sorted(matched),
        "package_true_positives": len(matched),
        "package_predictions": len(recalled_apps),
        "package_actuals": len(actual),
        "package_precision": round(len(matched) / max(1, len(recalled_apps)), 6),
        "package_recall": round(len(matched) / max(1, len(actual)), 6),
        "precision_at_k": round(len(matched) / max(1, len(recalled_apps)), 6),
        "recall_at_k": round(len(matched) / max(1, len(actual)), 6),
    }


def extract_launched_apps(content: Any) -> set[str]:
    """Extract packages only from successful ``launch_app`` receipts."""

    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
    packages: set[str] = set()
    for text in texts:
        if not text.lstrip().startswith("OK."):
            continue
        packages.update(match.strip() for match in _LAUNCH_RE.findall(text) if match.strip())
    return packages


def _accumulate_stats(
    stats_path: str | Path,
    accumulate: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Atomically read-modify-write the shared shadow stats file."""

    path = Path(stats_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _STATS_LOCK, lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            # The v1 counters used incompatible ranking and false-hit
            # denominators. Start the schema-v2 scorecard clean instead of
            # blending irrecoverable historical meanings into the new rates.
            if current.get("schema_v") != 2:
                current = {}
            updated = accumulate(dict(current))
            updated["schema_v"] = 2
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(updated, stream, ensure_ascii=False, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return updated
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def selection_error_key(namespace: str) -> str | None:
    """Return the scorecard counter key for ``namespace`` (``None`` = untracked).

    Only the three recall namespaces carry a counter; the embedder warm-up
    reports a namespace of its own and stays trace-only.
    """

    return SELECTION_ERROR_KEYS.get(str(namespace).strip())


def add_selection_error_observer(
    observer: Callable[[str, str], None],
) -> Callable[[], None]:
    """Register one ``(namespace, error_type)`` observer, returning a disposer."""

    if not callable(observer):
        raise TypeError("selection error observer must be callable")
    with _SELECTION_OBSERVER_LOCK:
        _selection_error_observers.append(observer)

    def dispose() -> None:
        with _SELECTION_OBSERVER_LOCK:
            try:
                _selection_error_observers.remove(observer)
            except ValueError:
                return

    return dispose


def note_selection_error(namespace: str, error: BaseException | str) -> None:
    """Notify observers of one selection-path failure; never raises.

    Only the exception *type* (or a literal string) is published, so a failure
    leaves no stack trace, message, or caller-controlled text in the trace.
    """

    error_type = error if isinstance(error, str) else type(error).__name__
    for observer in tuple(_selection_error_observers):
        try:
            observer(str(namespace), str(error_type))
        except Exception:  # noqa: BLE001 - observation is fail-open
            continue


def update_selection_error_stats(
    stats_path: str | Path, namespace: str
) -> dict[str, Any]:
    """Increment the per-namespace error counter in ``recall_stats.json``.

    Observe-only: existing fields are preserved and an untracked namespace
    (e.g. the embedder warm-up) is a no-op.
    """

    key = selection_error_key(namespace)
    if key is None:
        return {}

    def accumulate(current: dict[str, Any]) -> dict[str, Any]:
        updated = dict(current)
        updated[key] = int(current.get(key, 0)) + 1
        return updated

    return _accumulate_stats(stats_path, accumulate)


def update_recall_stats(
    stats_path: str | Path,
    evaluation: Mapping[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Atomically accumulate shadow metrics with stable, explicit denominators."""

    def accumulate(current: dict[str, Any]) -> dict[str, Any]:
        evaluations = int(current.get("evaluations", 0)) + 1
        hits = int(current.get("hits", 0)) + int(bool(evaluation.get("hit")))
        hit_at_1_count = int(current.get("hit_at_1_count", 0)) + int(
            bool(evaluation.get("hit_at_1"))
        )
        contaminated_runs = int(
            current.get("contaminated_runs", current.get("false_hits", 0))
        ) + int(
            bool(
                evaluation.get(
                    "contaminated_run", evaluation.get("false_hit")
                )
            )
        )
        recall_runs = int(current.get("recall_runs", 0)) + int(
            bool(evaluation.get("recalled_apps"))
        )
        package_true_positives = int(
            current.get("package_true_positives", 0)
        ) + int(evaluation.get("package_true_positives", 0))
        package_predictions = int(current.get("package_predictions", 0)) + int(
            evaluation.get("package_predictions", 0)
        )
        package_actuals = int(current.get("package_actuals", 0)) + int(
            evaluation.get("package_actuals", 0)
        )
        updated = dict(current)
        updated.update(
            {
                "evaluations": evaluations,
                "recall_runs": recall_runs,
                "hits": hits,
                "hit_at_1_count": hit_at_1_count,
                "contaminated_runs": contaminated_runs,
                "package_true_positives": package_true_positives,
                "package_predictions": package_predictions,
                "package_actuals": package_actuals,
                "hit_rate": round(hits / evaluations, 6),
                "hit_at_1": round(hit_at_1_count / evaluations, 6),
                "conditional_hit_rate": round(hits / max(1, recall_runs), 6),
                "contaminated_run_rate": round(
                    contaminated_runs / evaluations, 6
                ),
                "package_precision": round(
                    package_true_positives / max(1, package_predictions), 6
                ),
                "package_recall": round(
                    package_true_positives / max(1, package_actuals), 6
                ),
                "precision_at_k": round(
                    package_true_positives / max(1, package_predictions), 6
                ),
                "recall_at_k": round(
                    package_true_positives / max(1, package_actuals), 6
                ),
                # Compatibility aliases: web/app.py keeps rendering these
                # counts over evaluations while UI adoption is handled later.
                "false_hits": contaminated_runs,
                "false_hit_rate": round(contaminated_runs / evaluations, 6),
                "latest": {"run_id": run_id, **dict(evaluation)},
            }
        )
        return updated

    return _accumulate_stats(stats_path, accumulate)


def update_procedure_recall_stats(
    stats_path: str | Path,
    selection: "ProcedureSelection | Mapping[str, Any] | None",
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Accumulate one run's procedure-card recall outcome (candidates/hit/reason).

    Zero recall is a visible neutral state, not a missing record: the candidate
    and post-filter counts separate "too few cards" from "threshold too tight",
    and the reason histogram keeps every run attributable.
    """

    payload = (
        selection.to_dict()
        if isinstance(selection, ProcedureSelection)
        else dict(selection or {})
    )
    reason = str(payload.get("reason") or "unknown")

    def accumulate(current: dict[str, Any]) -> dict[str, Any]:
        runs = int(current.get("procedure_runs", 0)) + 1
        hits = int(current.get("procedure_hits", 0)) + int(
            bool(payload.get("lesson_id"))
        )
        candidates = int(current.get("procedure_candidates", 0)) + int(
            payload.get("candidates", 0) or 0
        )
        filtered = int(current.get("procedure_filtered", 0)) + int(
            payload.get("filtered", 0) or 0
        )
        reasons = dict(current.get("procedure_reasons", {}) or {})
        reasons[reason] = int(reasons.get(reason, 0)) + 1
        updated = dict(current)
        updated.update(
            {
                "procedure_runs": runs,
                "procedure_hits": hits,
                "procedure_candidates": candidates,
                "procedure_filtered": filtered,
                "procedure_hit_rate": round(hits / runs, 6),
                "procedure_reasons": reasons,
                "latest_procedure": {"run_id": run_id, **payload},
            }
        )
        return updated

    return _accumulate_stats(stats_path, accumulate)


def _sync_procedure_namespace(index: "VecIndex", lessons_dir: Any) -> dict[str, Any]:
    """Sync the derived procedure namespace from the lesson view (fail-open).

    A config without a lessons directory has no lesson pipeline at all, so the
    report stays exactly as it was.
    """

    if not lessons_dir:
        return {}
    try:
        return {
            **index.sync_procedure_lessons(load_procedure_lessons(lessons_dir)),
            "procedures_status": "updated",
        }
    except Exception as exc:  # noqa: BLE001 - derived index is fail-open
        return {
            "procedures": 0,
            "removed_procedures": 0,
            "procedures_status": "error",
            "procedures_error": type(exc).__name__,
        }


def incremental_upsert(
    config: Any,
    *,
    episode: Mapping[str, Any] | None,
    alias_entries: Sequence[Mapping[str, Any]] = (),
    all_alias_entries: Sequence[Mapping[str, Any]] | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Upsert one completed run and its changed aliases under a process lock."""

    active_embedder = embedder or MlxEmbedder(
        getattr(config, "embed_model", DEFAULT_EMBED_MODEL),
        getattr(config, "embed_dim", 1024),
    )
    db_path = Path(getattr(config, "vec_db", "memory/vec.db"))
    min_steps = int(getattr(config, "index_min_steps", 2))
    with _index_file_lock(db_path):
        with VecIndex(db_path, embedder=active_embedder) as index:
            episode_indexed = bool(
                episode is not None
                and index.index_episode(episode, min_steps=min_steps)
            )
            aliases = index.index_alias_entries(
                alias_entries,
                all_entries=all_alias_entries,
            )
            procedures = _sync_procedure_namespace(
                index, getattr(config, "lessons_dir", None)
            )
    return {
        "status": "updated",
        "episode": int(episode_indexed),
        "episode_skipped": int(episode is not None and not episode_indexed),
        "app_aliases": aliases,
        **procedures,
    }


def reconcile_index(
    config: Any,
    *,
    embedder: Embedder | None = None,
    app_store: Any | None = None,
) -> dict[str, Any]:
    """Make the current model's vec rows match episode/App-KB JSON views."""

    from phone_agent.v2.appkb import AppKnowledgeStore
    from phone_agent.v2.experience import load_episodes

    active_embedder = embedder or MlxEmbedder(
        getattr(config, "embed_model", DEFAULT_EMBED_MODEL),
        getattr(config, "embed_dim", 1024),
    )
    db_path = Path(getattr(config, "vec_db", "memory/vec.db"))
    memory_dir = Path(getattr(config, "memory_dir", "memory"))
    min_steps = int(getattr(config, "index_min_steps", 2))
    experience_dir = getattr(config, "experience_dir", None)
    if not experience_dir:
        experience_dir = memory_dir / "experience"
    episodes = [
        item
        for item in load_episodes(experience_dir).values()
        if item.get("type") == "episode_outcome"
        and episode_is_indexable(item, min_steps=min_steps)
        and not bool(item.get("revoked", False))
    ]
    store = app_store or AppKnowledgeStore(str(memory_dir))
    aliases = [
        entry
        for entry in store.entries(include_stale=True)
        if not bool(entry.get("stale", False))
    ]

    with _index_file_lock(db_path):
        with VecIndex(db_path, embedder=active_embedder) as index:
            removed_episodes = index.delete_missing(
                "episode", {str(item["run_id"]) for item in episodes}
            )
            removed_aliases = index.delete_missing(
                "app_alias", {_alias_ref_id(item) for item in aliases}
            )
            indexed_episodes = sum(
                int(index.index_episode(item, min_steps=min_steps))
                for item in episodes
            )
            indexed_aliases = index.index_alias_entries(
                aliases, all_entries=aliases
            )
            procedures = _sync_procedure_namespace(
                index, getattr(config, "lessons_dir", None)
            )
            total = index.count(embed_model=active_embedder.model_id)
    return {
        "status": "reconciled",
        "episodes": indexed_episodes,
        "app_aliases": indexed_aliases,
        "removed_episodes": removed_episodes,
        "removed_app_aliases": removed_aliases,
        **procedures,
        "total": total,
    }


def rebuild_index(
    config: Any,
    *,
    embedder: Embedder | None = None,
    events_path: str | Path | None = None,
    app_store: Any | None = None,
) -> dict[str, Any]:
    """Delete the derived DB and rebuild it from episode/App-KB sources."""

    db_path = Path(getattr(config, "vec_db", "memory/vec.db"))
    active_embedder = embedder or MlxEmbedder(
        getattr(config, "embed_model", DEFAULT_EMBED_MODEL),
        getattr(config, "embed_dim", 1024),
    )
    memory_dir = Path(getattr(config, "memory_dir", "memory"))
    min_steps = int(getattr(config, "index_min_steps", 2))
    with _index_file_lock(db_path):
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(db_path) + suffix)
            if target.exists():
                target.unlink()
        with VecIndex(db_path, embedder=active_embedder) as index:
            if events_path is None:
                from phone_agent.v2.experience import load_episodes

                experience_dir = getattr(config, "experience_dir", None)
                if not experience_dir:
                    experience_dir = memory_dir / "experience"
                episode_records = [
                    item
                    for item in load_episodes(experience_dir).values()
                    if item.get("type") == "episode_outcome"
                ]
                episodes = sum(
                    int(index.index_episode(item, min_steps=min_steps))
                    for item in episode_records
                )
            else:
                source = Path(events_path)
                episodes = index.index_episodes(
                    source, min_steps=min_steps
                )["episodes"]
            aliases = index.index_app_aliases(memory_dir=memory_dir, store=app_store)[
                "app_aliases"
            ]
            procedures = _sync_procedure_namespace(
                index, getattr(config, "lessons_dir", None)
            )
            total = index.count(embed_model=active_embedder.model_id)
    return {
        "status": "rebuilt",
        "db_path": str(db_path),
        "embed_model": active_embedder.model_id,
        "embed_dim": active_embedder.dimension,
        "episodes": episodes,
        "app_aliases": aliases,
        **procedures,
        "total": total,
    }


def index_episodes(
    events_path: str | Path,
    *,
    db_path: str | Path = "memory/vec.db",
    embedder: Embedder | None = None,
    embed_model: str = DEFAULT_EMBED_MODEL,
    embed_dim: int = 1024,
    min_steps: int = 2,
) -> dict[str, int]:
    """Index episode JSONL through the default or caller-supplied embedder."""

    active = embedder or MlxEmbedder(embed_model, embed_dim)
    with _index_file_lock(db_path):
        with VecIndex(db_path, embedder=active) as index:
            return index.index_episodes(events_path, min_steps=min_steps)


def index_app_aliases(
    *,
    memory_dir: str | Path = "memory",
    db_path: str | Path = "memory/vec.db",
    embedder: Embedder | None = None,
    embed_model: str = DEFAULT_EMBED_MODEL,
    embed_dim: int = 1024,
    store: Any | None = None,
) -> dict[str, int]:
    """Mirror App-KB terms, labels, and packages into semantic recall."""

    active = embedder or MlxEmbedder(embed_model, embed_dim)
    with _index_file_lock(db_path):
        with VecIndex(db_path, embedder=active) as index:
            return index.index_app_aliases(memory_dir=memory_dir, store=store)


def select_procedure(
    goal_text: str,
    *,
    app_package: str | None = None,
    device_id: str | None = None,
    db_path: str | Path = "memory/vec.db",
    embedder: Embedder | None = None,
    embed_model: str = DEFAULT_EMBED_MODEL,
    embed_dim: int = 1024,
    min_score: float = PROCEDURE_MIN_SCORE,
) -> ProcedureSelection:
    """Select one procedure card from a configured index (WP-WF3 entry point).

    ``app_package`` is the hard app gate: when it is known only cards scoped to
    exactly that package qualify, otherwise only general cards do.
    """

    active = embedder or MlxEmbedder(embed_model, embed_dim)
    with VecIndex(db_path, embedder=active) as index:
        return index.select_procedure(
            goal_text,
            app_package=app_package,
            device_id=device_id,
            min_score=min_score,
        )


def recall(
    query: str,
    *,
    device_scope: str,
    db_path: str | Path = "memory/vec.db",
    embedder: Embedder | None = None,
    embed_model: str = DEFAULT_EMBED_MODEL,
    embed_dim: int = 1024,
    top_k: int = 1,
    min_score: float = 0.50,
    decay_lambda: float = 0.02,
    now: float | None = None,
    namespaces: Sequence[str] = ("episode", "app_alias"),
) -> list[dict[str, Any]]:
    """Retrieve candidates from a configured single-file vector index."""

    active = embedder or MlxEmbedder(embed_model, embed_dim)
    with VecIndex(db_path, embedder=active) as index:
        return index.recall(
            query,
            device_scope=device_scope,
            top_k=top_k,
            min_score=min_score,
            decay_lambda=decay_lambda,
            now=now,
            namespaces=namespaces,
        )


__all__ = [
    "DEFAULT_EMBED_MODEL",
    "Embedder",
    "HashEmbedder",
    "MlxEmbedder",
    "PROCEDURE_MAX_ITEMS",
    "PROCEDURE_MAX_TOKENS",
    "PROCEDURE_MIN_SCORE",
    "ProcedureSelection",
    "RECALL_SELECTION_ERROR",
    "VecIndex",
    "add_selection_error_observer",
    "alias_snapshot",
    "embedder_needs_warmup",
    "episode_is_indexable",
    "evaluate_recall",
    "extract_launched_apps",
    "format_procedure_block",
    "index_app_aliases",
    "index_episodes",
    "incremental_upsert",
    "load_procedure_lessons",
    "note_selection_error",
    "read_episode_events",
    "recall",
    "reconcile_index",
    "rebuild_index",
    "resolve_embedder_factory",
    "select_procedure",
    "selection_error_key",
    "update_procedure_recall_stats",
    "update_recall_stats",
    "update_selection_error_stats",
    "warmup_embedder",
]
