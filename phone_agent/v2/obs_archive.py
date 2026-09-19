"""Text-only observation archive + rebuildable FTS5 recall index (WP-A).

One :class:`ObsArchive` owns exactly one run's evidence: every *committed
successful* observation appends one JSONL record to
``<obs_archive_dir>/<run_id>.jsonl`` — the model-facing ``[OBS]`` text (app,
``screen_seq``, marks digest with window/``op=`` annotations) plus the structured
metadata (``run_id`` / ``epoch`` / ``screen_seq`` / foreground ``package`` /
``ts``).  **Text only**: screenshot bytes and base64 never enter this plane
(P0 #6 is absolute for trace and this store is its own local-private plane).

The single observation producer remains ``PhoneSession.observe()`` (P0 #15): the
capability-installed ``session.obs_archive_sink`` is invoked by
``_commit_observation`` — no second observation path exists and no tool here
ever touches the device.  The write path is fail-open: archive/index failures
are swallowed (with a trace-only ``obs_archive_error`` name) and can never
alter observation semantics.

``page`` / ``search`` are the read side of the two model-facing tools
(``recall_screen`` / ``search_screens``).  They are read-only evidence: every
historical mark id is rendered non-addressable by :func:`invalidate_marks`
(``ax_3@e12`` -> ``历史:ax_3@e12（已失效）``) so recall can never bypass
marks-first (P0 #2).  Read failures raise :class:`ObsArchiveError` so the tools
can return honest error text (P0 #5).

Layout and index:

* ``<run_id>.jsonl`` is the archive of record (append-only, local-private);
* ``<run_id>.db`` is a derived SQLite **FTS5** index (same technical family as
  ``v2/recall.py``'s keyword pass, no embeddings) over the archived text.  It is
  rebuildable from the JSONL at any time — the meta table stores the JSONL byte
  size the index was built from, and a size mismatch (or a missing/damaged db)
  triggers one rebuild before the query runs.
* Retention keeps the newest ``keep_runs`` runs (default 20) by JSONL mtime; the
  current run is always kept.

``build_obs_archive`` is the capability factory half: it returns ``None``
unless ``PHONE_AGENT_OBS_ARCHIVE=on``, so the default deployment pays nothing
and writes nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_V = 1
ARCHIVE_SUFFIX = ".jsonl"
INDEX_SUFFIX = ".db"
DEFAULT_KEEP_RUNS = 20
PAGE_LINES_DEFAULT = 40
PAGE_LINES_MAX = 80
SEARCH_LIMIT_DEFAULT = 5
SEARCH_LIMIT_MAX = 10
SNIPPET_RADIUS = 60
# Suffix appended to the OBS-marks fold placeholder only while this capability
# is mounted (middleware/images.py renders it verbatim; the default is "").
RECALL_HINT = "[可 recall_screen]"
_INDEX_RAW_SUFFIXES = ("", "-wal", "-shm")

# Word/CJK-bigram tokenizer mirroring v2/recall.py's FTS5 keyword pass so the
# two indexes behave the same for Chinese and latin queries.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+|[\u3400-\u9fff]+")

# Badged mark ids the model ever sees are ``<provider_id>[#<seq>]@e<epoch>``
# (``ax_3@e9``, ``la_1#2@e11``).  The lookbehinds keep the rewrite idempotent
# (``历史:<id>（已失效）`` is never wrapped twice) and avoid touching ids that
# are substrings of a larger token.
_MARK_ID_RE = re.compile(
    r"(?<!历史:)(?<![A-Za-z0-9_#@])([A-Za-z][A-Za-z0-9_]*(?:#\d+)?@e\d+)(?!\w)"
)


class ObsArchiveError(RuntimeError):
    """Raised when an archive read fails; tools turn this into error text."""


def invalidate_marks(text: str) -> str:
    """Render every historical mark id in *text* as clearly non-addressable.

    ``ax_3@e12`` becomes ``历史:ax_3@e12（已失效）``.  The raw id stays visible
    as provenance but the prefix/suffix mean a model copying it verbatim can
    never resolve it in ``PhoneSession.resolve_mark`` (P0 #2 — recall is
    evidence, never an action target).  Pure and idempotent.
    """

    return _MARK_ID_RE.sub(lambda match: f"历史:{match.group(1)}（已失效）", text)


def _fts_terms(query: str) -> list[str]:
    """Return stable word/CJK-bigram terms for one FTS5 MATCH query."""

    terms: list[str] = []
    for match in _TOKEN_RE.finditer(str(query or "")):
        value = match.group(0)
        terms.append(value)
        if any("\u3400" <= char <= "\u9fff" for char in value):
            terms.extend(value[index : index + 2] for index in range(len(value) - 1))
    return list(dict.fromkeys(term for term in terms if term))


def _fts_query(query: str) -> str:
    """Build the quoted OR-of-terms MATCH expression for *query*."""

    return " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in _fts_terms(query)
    )


def _snippet(text: str, terms: Sequence[str]) -> str:
    """Return a one-line snippet around the first literally occurring term."""

    flat = " ".join(str(text or "").split())
    folded = flat.casefold()
    start = -1
    for term in terms:
        index = folded.find(term.casefold())
        if index >= 0:
            start = index
            break
    if start < 0:
        return flat[: SNIPPET_RADIUS * 2]
    begin = max(0, start - SNIPPET_RADIUS)
    end = min(len(flat), start + SNIPPET_RADIUS)
    prefix = "…" if begin > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[begin:end]}{suffix}"


@dataclass(frozen=True)
class ObsPage:
    """One page of an archived frame's text (already mark-id invalidated)."""

    screen_seq: int
    app: str
    package: str
    epoch: int
    ts: float
    total_lines: int
    offset: int
    text: str
    next_offset: int | None


@dataclass(frozen=True)
class ObsHit:
    """One FTS search hit over this run's archived frames."""

    screen_seq: int
    app: str
    epoch: int
    ts: float
    snippet: str


def read_archive_records(path: str | Path) -> list[dict[str, Any]]:
    """Read valid archive records from a JSONL file (malformed lines skipped).

    Validation, not transformation: a record must be an object carrying
    ``schema_v=1``, an integer ``screen_seq`` and a string ``text``.  Anything
    else is dropped so one damaged line can never poison a rebuild.
    """

    archive_path = Path(path)
    if not archive_path.exists():
        return []
    records: list[dict[str, Any]] = []
    with archive_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            seq = record.get("screen_seq")
            text = record.get("text")
            if (
                record.get("schema_v") != SCHEMA_V
                or not isinstance(seq, int)
                or not isinstance(text, str)
            ):
                continue
            records.append(record)
    return records


def prune_obs_archive_runs(
    root_dir: str | Path, *, keep: int, current_run_id: str
) -> int:
    """Delete the oldest per-run archive+index pairs beyond *keep* runs.

    Runs are ranked by the JSONL file's mtime; the current run is never
    deleted.  Returns the number of removed JSONL files; every failure is
    swallowed (retention is housekeeping, never a run blocker).
    """

    root = Path(root_dir)
    keep = max(1, int(keep))
    try:
        candidates = sorted(
            (
                path
                for path in root.glob(f"*{ARCHIVE_SUFFIX}")
                if path.is_file() and path.stem != str(current_run_id)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for path in candidates[max(0, keep - 1) :]:
        index_path = path.with_suffix(INDEX_SUFFIX)
        targets = (path, *(Path(f"{index_path}{suffix}") for suffix in _INDEX_RAW_SUFFIXES))
        for target in targets:
            try:
                target.unlink()
            except OSError:
                continue
        removed += 1
    return removed


class ObsArchive:
    """One run's text-only observation archive + derived FTS5 index.

    Single run, single thread: the run's tool execution is serialized
    (P0 #15 / single-batch execution), so no cross-thread locking is needed
    beyond SQLite's own file locking.  Writers append to the JSONL first and
    then to the index; a failed index write marks the index dirty and the next
    read rebuilds it from the JSONL (the archive of record).
    """

    def __init__(
        self,
        root_dir: str | Path,
        run_id: str,
        *,
        keep_runs: int = DEFAULT_KEEP_RUNS,
    ) -> None:
        run = str(run_id or "").strip()
        if not run:
            raise ValueError("obs archive requires a non-empty run_id")
        self.root_dir = Path(root_dir)
        self.run_id = run
        self.keep_runs = max(1, int(keep_runs))
        self.jsonl_path = self.root_dir / f"{run}{ARCHIVE_SUFFIX}"
        self.db_path = self.root_dir / f"{run}{INDEX_SUFFIX}"
        self._connection: sqlite3.Connection | None = None
        self._prepared = False
        self._index_broken = False
        self._index_checked = False
        self._records: dict[int, dict[str, Any]] = {}

    # -- write side (invoked by PhoneSession._commit_observation) ----------

    def on_committed_observation(
        self, session: Any, observation: Any, package: str | None = None
    ) -> None:
        """Append one committed observation; never raises (fail-open sink).

        The observation object is the one the session just committed, so this
        renders the exact model-facing ``[OBS]`` text with the shared renderer —
        including the windowed/``op=`` digest annotations.
        """

        try:
            self._append(session, observation, package)
        except Exception as exc:  # noqa: BLE001 - archive is an observe-only side plane
            self._note_failure(session, exc)

    def _append(self, session: Any, observation: Any, package: str | None) -> None:
        record = {
            "schema_v": SCHEMA_V,
            "run_id": self.run_id,
            "epoch": int(getattr(observation, "epoch", 0) or 0),
            "screen_seq": int(getattr(observation, "screen_seq", 0) or 0),
            "app": str(getattr(observation, "current_app", "") or ""),
            "package": str(package or ""),
            "ts": time.time(),
            "text": _render_obs_text(session, observation),
        }
        self._prepare()
        with self.jsonl_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
        self._records[int(record["screen_seq"])] = record
        try:
            self._index_record(record)
        except Exception as exc:  # noqa: BLE001 - derived index is rebuildable
            self._index_broken = True
            self._note_failure(session, exc)

    def _prepare(self) -> None:
        """Create the archive root once and enforce run retention once."""

        if self._prepared:
            return
        self.root_dir.mkdir(parents=True, exist_ok=True)
        prune_obs_archive_runs(
            self.root_dir, keep=self.keep_runs, current_run_id=self.run_id
        )
        self._prepared = True

    @staticmethod
    def _note_failure(session: Any, exc: BaseException) -> None:
        """Record a trace-only diagnostic (never the message, never text)."""

        recorder = getattr(session, "resolution_trace_recorder", None)
        if not callable(recorder):
            return
        try:
            recorder("obs_archive_error", error=type(exc).__name__)
        except Exception:  # noqa: BLE001 - diagnostics cannot change semantics
            return

    # -- read side (recall_screen / search_screens) ------------------------

    def page(
        self,
        screen_seq: int,
        *,
        offset: int = 0,
        limit: int = PAGE_LINES_DEFAULT,
    ) -> ObsPage:
        """Return one line-paged slice of an archived frame (marks invalidated).

        Raises :class:`ObsArchiveError` when the frame is not archived; the
        error text names the archived range so the model can self-correct.
        """

        seq = int(screen_seq)
        record = self._record_for(seq)
        if record is None:
            known = self._known_seqs()
            range_text = f"screen#{known[0]}–screen#{known[-1]}" if known else "无"
            raise ObsArchiveError(
                f"未找到 screen#{seq} 的存档帧（本 run 已存档 {len(known)} 帧：{range_text}）"
            )
        text = invalidate_marks(str(record.get("text", "")))
        lines = text.splitlines()
        total = len(lines)
        start = max(0, int(offset))
        page_size = max(1, min(PAGE_LINES_MAX, int(limit)))
        page_lines = lines[start : start + page_size]
        end = start + len(page_lines)
        return ObsPage(
            screen_seq=seq,
            app=str(record.get("app", "") or ""),
            package=str(record.get("package", "") or ""),
            epoch=int(record.get("epoch", 0) or 0),
            ts=float(record.get("ts", 0.0) or 0.0),
            total_lines=total,
            offset=start,
            text="\n".join(page_lines),
            next_offset=end if end < total else None,
        )

    def search(
        self, query: str, *, limit: int = SEARCH_LIMIT_DEFAULT
    ) -> list[ObsHit]:
        """FTS-search this run's archived frames; snippets are invalidated.

        Returns ``[]`` for an empty query or an empty archive.  A broken or
        stale index is rebuilt once from the JSONL; if the query still fails an
        :class:`ObsArchiveError` is raised for the tool to render honestly.
        """

        terms = _fts_terms(query)
        match = _fts_query(query)
        if not terms or not match:
            return []
        if not self.jsonl_path.exists():
            return []
        rows = self._search_rows(match, limit)
        hits: list[ObsHit] = []
        for row in rows:
            hits.append(
                ObsHit(
                    screen_seq=int(row["screen_seq"]),
                    app=str(row["app"] or ""),
                    epoch=int(row["epoch"] or 0),
                    ts=float(row["ts"] or 0.0),
                    snippet=invalidate_marks(_snippet(str(row["text"]), terms)),
                )
            )
        return hits

    def _search_rows(self, match: str, limit: int) -> list[sqlite3.Row]:
        top = max(1, min(SEARCH_LIMIT_MAX, int(limit)))
        sql = (
            "SELECT obs_fts.screen_seq AS screen_seq, obs_fts.text AS text, "
            "obs_records.app AS app, obs_records.epoch AS epoch, "
            "obs_records.ts AS ts, bm25(obs_fts) AS score "
            "FROM obs_fts JOIN obs_records "
            "ON obs_records.screen_seq = obs_fts.screen_seq "
            "WHERE obs_fts MATCH ? ORDER BY score ASC, screen_seq ASC LIMIT ?"
        )
        connection = self._ensure_index()
        try:
            return connection.execute(sql, (match, top)).fetchall()
        except sqlite3.Error:
            self._index_broken = True
            try:
                self._rebuild_index()
                return self._open_index().execute(sql, (match, top)).fetchall()
            except sqlite3.Error as retry_exc:
                raise ObsArchiveError(
                    f"观测存档索引不可用（{type(retry_exc).__name__}）"
                ) from retry_exc

    def _record_for(self, screen_seq: int) -> dict[str, Any] | None:
        if screen_seq not in self._records:
            for record in read_archive_records(self.jsonl_path):
                self._records.setdefault(int(record["screen_seq"]), record)
        return self._records.get(screen_seq)

    def _known_seqs(self) -> list[int]:
        for record in read_archive_records(self.jsonl_path):
            self._records.setdefault(int(record["screen_seq"]), record)
        return sorted(self._records)

    # -- derived index -----------------------------------------------------

    def close(self) -> None:
        """Close the derived-index connection (idempotent).

        Resets the once-per-process verification state so a later read against
        a rebuilt/rotated db file verifies (and rebuilds) again.
        """

        connection, self._connection = self._connection, None
        self._index_checked = False
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _open_index(self) -> sqlite3.Connection:
        if self._connection is None:
            self._prepare()
            connection = sqlite3.connect(str(self.db_path))
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA busy_timeout=5000;
                    CREATE TABLE IF NOT EXISTS obs_archive_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS obs_records (
                        screen_seq INTEGER PRIMARY KEY,
                        epoch INTEGER NOT NULL DEFAULT 0,
                        app TEXT NOT NULL DEFAULT '',
                        package TEXT NOT NULL DEFAULT '',
                        ts REAL NOT NULL DEFAULT 0
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
                        text, screen_seq UNINDEXED, tokenize='unicode61'
                    );
                    """
                )
                connection.commit()
            except sqlite3.Error:
                connection.close()
                self._connection = None
                raise
            self._connection = connection
        assert self._connection is not None
        return self._connection

    def _ensure_index(self) -> sqlite3.Connection:
        """Open the derived index and verify it once per process.

        The meta table records the JSONL byte size the index was built from; a
        mismatch (a crashed writer, an externally extended archive, a damaged
        db) triggers exactly one rebuild before the caller proceeds.  Within
        the process, incremental writes keep the index fresh and the check is
        not repeated.
        """

        connection = self._open_index()
        if self._index_broken or not self._index_checked:
            self._index_checked = True
            if self._index_broken or self._index_stale():
                self._rebuild_index()
                connection = self._open_index()
        return connection

    def _index_stale(self) -> bool:
        connection = self._connection
        if connection is None:
            return True
        try:
            row = connection.execute(
                "SELECT value FROM obs_archive_meta WHERE key = 'source_bytes'"
            ).fetchone()
        except sqlite3.Error:
            return True
        try:
            size = self.jsonl_path.stat().st_size
        except OSError:
            size = 0
        return row is None or str(row[0]) != str(size)

    def _rebuild_index(self) -> None:
        """Rebuild the derived index from the JSONL archive of record.

        The index is derived state: this runs after a crash, a damaged db, or
        an out-of-process append, and always ends with the meta byte size
        matching the JSONL (so the next process's single verification passes).
        """

        connection = self._open_index()
        records = read_archive_records(self.jsonl_path)
        try:
            size = self.jsonl_path.stat().st_size
        except OSError:
            size = 0
        try:
            with connection:
                connection.execute("DELETE FROM obs_fts")
                connection.execute("DELETE FROM obs_records")
                for record in records:
                    self._insert_index_row(connection, record)
                connection.execute(
                    "INSERT OR REPLACE INTO obs_archive_meta(key, value) "
                    "VALUES ('source_bytes', ?)",
                    (str(size),),
                )
        except sqlite3.Error as exc:
            self._index_broken = True
            raise ObsArchiveError(
                f"观测存档索引重建失败（{type(exc).__name__}）"
            ) from exc
        self._index_broken = False

    def _index_record(self, record: dict[str, Any]) -> None:
        connection = self._ensure_index()
        try:
            size = self.jsonl_path.stat().st_size
        except OSError:
            size = 0
        with connection:
            self._insert_index_row(connection, record)
            connection.execute(
                "INSERT OR REPLACE INTO obs_archive_meta(key, value) "
                "VALUES ('source_bytes', ?)",
                (str(size),),
            )

    @staticmethod
    def _insert_index_row(
        connection: sqlite3.Connection, record: dict[str, Any]
    ) -> None:
        seq = int(record.get("screen_seq", 0) or 0)
        connection.execute(
            "INSERT OR REPLACE INTO obs_records(screen_seq, epoch, app, package, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                seq,
                int(record.get("epoch", 0) or 0),
                str(record.get("app", "") or ""),
                str(record.get("package", "") or ""),
                float(record.get("ts", 0.0) or 0.0),
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO obs_fts(rowid, text, screen_seq) VALUES (?, ?, ?)",
            (seq, str(record.get("text", "") or ""), seq),
        )


def _render_obs_text(session: Any, observation: Any) -> str:
    """Render the model-facing ``[OBS]`` text with the shared tool renderer.

    Imported lazily: this module is also imported during capability assembly,
    where the model-facing tools package must not be imported yet.
    """

    from phone_agent.v2.tools._obs import format_observation_text

    return format_observation_text(session, observation)


def build_obs_archive(
    config: Any, run_id: str, *, keep_default: int = DEFAULT_KEEP_RUNS
) -> ObsArchive | None:
    """Build the run's archive, or ``None`` when the capability is off.

    ``PHONE_AGENT_OBS_ARCHIVE`` is ``off`` (default) or ``on``; directory and
    retention come from ``obs_archive_dir`` / ``obs_archive_keep_runs``.  The
    default directory lives under the git-ignored local ``memory/`` root, so a
    fresh clone never commits or serves archived screens.
    """

    mode = str(getattr(config, "obs_archive", "off") or "off").strip().lower()
    if mode != "on":
        return None
    run = str(run_id or "").strip()
    if not run:
        return None
    try:
        keep = int(getattr(config, "obs_archive_keep_runs", keep_default))
    except (TypeError, ValueError):
        keep = keep_default
    return ObsArchive(
        getattr(config, "obs_archive_dir", "memory/obs_archive"),
        run,
        keep_runs=keep,
    )


__all__ = [
    "DEFAULT_KEEP_RUNS",
    "PAGE_LINES_DEFAULT",
    "PAGE_LINES_MAX",
    "RECALL_HINT",
    "SCHEMA_V",
    "SEARCH_LIMIT_DEFAULT",
    "SEARCH_LIMIT_MAX",
    "ObsArchive",
    "ObsArchiveError",
    "ObsHit",
    "ObsPage",
    "build_obs_archive",
    "invalidate_marks",
    "prune_obs_archive_runs",
    "read_archive_records",
]
