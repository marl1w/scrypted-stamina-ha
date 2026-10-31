"""The event index: SQLite in Home Assistant's config directory, metadata only.

What it holds (architecture §3.6): every event's facts, context, scores, reasons, decision
state and cloud paths; per destination, which files of each event are where (one row per
file, appended as it is uploaded); a table of pending actions (a delete or a commit that was
decided but not yet done); a 320 px thumbnail per event; and the stream position.

No video is ever stored here. Thumbnails go after a month (a week for dropped events).

The pending-actions table is what makes a decision survive a restart: a DROPPING decision
is written down *before* anything is deleted, and removed only once the delete has
succeeded. At startup the manager replays whatever is left.

Rows come in two tiers. A *fresh* row (its first `FRESH_DAYS`) keeps the full record. After
that it is *slimmed*: the record keeps what the list, the media browser, the event screen's
summary and the model read, and drops the observations, per-subject detail, context
snapshots and late-evidence detail (`slim_record`). Only a settled row is slimmed: final,
with no action pending and no copy still being written.

Rows are deleted once older than the history the model learns from — but never a row whose
files are still on a destination, because the quota is counted from these rows. Over the
size cap, ordinary dropped history is *thinned* rather than cut (`async_thin`): one row in
`THIN_EVERY` survives and carries the others' weight, so the model's rates stay the same.

Every call runs on the index's **own single thread** (a one-worker executor): writes land
in the order they were made, the connection is only ever touched from that thread (no lock
needed), and Home Assistant's shared executor is never held by a slow disk. The database is
in WAL mode. `read_learning_rows` is the one synchronous entry point: it runs in the
model-rebuild thread on a separate read-only connection, so a long read never blocks a
write.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sqlite3
import sys
from typing import Any

from homeassistant.core import HomeAssistant

from .const import UPLOADS_CLASSES, UPLOADS_UNUSUAL
from .decider import EventRecord, EventState
from .protocol import EventFacts

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# Columns added after the first schema, for databases created before them.
_ADDED_COLUMNS = (
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("area", "TEXT NOT NULL DEFAULT ''"),
    ("duration", "REAL"),
    ("features", "TEXT"),
    ("weight", "INTEGER NOT NULL DEFAULT 1"),
    ("slim", "INTEGER NOT NULL DEFAULT 0"),
    ("thin_kind", "TEXT"),
)

# Indexes on added columns: created after the columns exist, on old databases too.
_ADDED_INDEXES = """
CREATE INDEX IF NOT EXISTS events_fresh ON events (started) WHERE slim = 0;
CREATE INDEX IF NOT EXISTS events_thin ON events (started, id) WHERE thin_kind IS NOT NULL;
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    camera TEXT NOT NULL,
    started REAL NOT NULL,
    ended REAL,
    state TEXT NOT NULL,
    unusual INTEGER NOT NULL DEFAULT 0,
    score REAL,
    finalized INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    duration REAL,
    features TEXT,
    -- How many events this row stands for: more than one once thinning removed its peers.
    weight INTEGER NOT NULL DEFAULT 1,
    slim INTEGER NOT NULL DEFAULT 0,
    -- The kind an ordinary dropped row is thinned under; NULL for a row never thinned.
    thin_kind TEXT,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_camera_started ON events (camera, started);
CREATE INDEX IF NOT EXISTS events_state ON events (state);
DROP INDEX IF EXISTS events_started;
CREATE INDEX IF NOT EXISTS events_page ON events (started DESC, id DESC);
CREATE TABLE IF NOT EXISTS copies (
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    copy TEXT NOT NULL,
    PRIMARY KEY (event_id, destination)
);
CREATE TABLE IF NOT EXISTS files (
    event_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (event_id, destination, name)
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS thumbnails (event_id TEXT PRIMARY KEY, data BLOB NOT NULL);
"""

# Rows removed per transaction when pruning, so a large prune never holds the writer long.
PRUNE_CHUNK = 200
# The WAL file is truncated back to this after it grows (bytes).
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
# Thinning keeps one row in this many per camera and kind, and its weight counts for them.
THIN_EVERY = 4
# A kind is common on a camera (so thinnable) from this many of its dropped events on.
# Fewer, and every instance is kept: a rare class is exactly what the model must not forget.
THIN_COMMON_EVENTS = 500
# Passes over the history, each allowing survivors up to `THIN_EVERY ** pass` events.
THIN_PASSES = 3
# Rows read per query while thinning (reads only; the writes go in `PRUNE_CHUNK`s).
THIN_READ = 1000

# Pending actions: what was decided and must still be done after a restart.
ACTION_DROP = "drop"
ACTION_COMMIT = "commit"


@dataclass(slots=True)
class Copy:
    """One event's files on one destination."""

    # The pending folder segments are uploaded into while the event lasts.
    folder: str
    # Every file uploaded for the event (init and segments), with its size. A committed
    # video is these files joined, so their total is what the copy occupies either way.
    files: dict[str, int] = field(default_factory=dict)
    committed: bool = False
    deleted: bool = False
    # Segments this destination could not take (quota full, provider refused); -1 is init.
    failed: list[int] = field(default_factory=list)
    # The committed video's path, and where each file joined into it lies: `[offset, length]`.
    video: str | None = None
    ranges: dict[str, list[int]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """Return the bytes this copy occupies."""
        return 0 if self.deleted else sum(self.files.values())

    @property
    def video_size(self) -> int:
        """Return the length of the committed video, as recorded."""
        return max((offset + length for offset, length in self.ranges.values()), default=0)

    @property
    def playable(self) -> bool:
        """Whether a committed video of this copy is on its destination."""
        return self.committed and not self.deleted and self.video is not None

    def header(self) -> dict[str, Any]:
        """Serialise everything but the files, which are rows of their own."""
        return {
            "folder": self.folder,
            "committed": self.committed,
            "deleted": self.deleted,
            "failed": self.failed,
            "video": self.video,
            "ranges": self.ranges,
        }

    @classmethod
    def from_header(cls, data: dict[str, Any], files: dict[str, int]) -> Copy:
        """Read back what `header` wrote, with the file rows."""
        video = data.get("video")
        return cls(
            folder=str(data["folder"]),
            files=files,
            committed=bool(data.get("committed")),
            deleted=bool(data.get("deleted")),
            failed=[int(n) for n in data.get("failed") or ()],
            video=str(video) if video else None,
            ranges={
                str(name): [int(offset), int(length)]
                for name, (offset, length) in (data.get("ranges") or {}).items()
            },
        )


_RECORD_FIELDS = (
    "target",
    "admitted",
    "upload_mode",
    "area",
    "area_key",
    "camera_name",
    "folder",
    "live_score",
    "provisional_score",
    "final_score",
    "unusual_live",
    "unusual_provisional",
    "unusual_final",
    "reasons",
    "reason",
    "keep_reason",
    "context_start",
    "context_end",
    "late",
    "follow_up_until",
    "finalized",
    "gaps",
    "notes",
    "latency",
    "dropped_at",
)


def record_to_dict(record: EventRecord) -> dict[str, Any]:
    """Serialise a record for the index."""
    data = {name: getattr(record, name) for name in _RECORD_FIELDS}
    data["facts"] = record.facts.as_dict()
    data["state"] = str(record.state)
    return data


def record_from_dict(data: dict[str, Any]) -> EventRecord:
    """Read back what `record_to_dict` wrote, tolerating fields added since."""
    record = EventRecord(facts=EventFacts.restore(data["facts"]), state=EventState(data["state"]))
    for name in _RECORD_FIELDS:
        if name in data:
            setattr(record, name, data[name])
    if "upload_mode" not in data:
        # From before the select: the switch's admission is the nearest position.
        record.upload_mode = UPLOADS_CLASSES if record.admitted else UPLOADS_UNUSUAL
    return record


# What a slim row's record keeps: the decision, the scores and the sentence, the path pieces
# (`cloud_path`), and the notes (`orphan_complete` decides retention). Context snapshots,
# late evidence, latency and gaps go.
_SLIM_FIELDS = (
    "target",
    "admitted",
    "upload_mode",
    "area",
    "area_key",
    "camera_name",
    "folder",
    "live_score",
    "provisional_score",
    "final_score",
    "unusual_live",
    "unusual_provisional",
    "unusual_final",
    "reasons",
    "reason",
    "keep_reason",
    "finalized",
    "notes",
    "dropped_at",
)
# Of the facts: what the event was, when and how it ended. Per-segment detail and hop stamps go.
_SLIM_FACTS = (
    "id",
    "camera",
    "camera_name",
    "started",
    "ended",
    "segments",
    "init",
    "segments_final",
    "signals",
    "camera_offline",
    "end_reason",
    "trigger",
)
# Of each subject: what it was and the verdict (the classes and `verdict_summary`).
_SLIM_SUBJECT = ("id", "class", "verdict")


def slim_record(data: dict[str, Any]) -> dict[str, Any]:
    """Return the slim tier of a record dict: still a record `record_from_dict` reads."""
    facts = data.get("facts") or {}
    slim = {name: data[name] for name in _SLIM_FIELDS if name in data}
    slim["state"] = data["state"]
    slim["facts"] = {name: facts[name] for name in _SLIM_FACTS if name in facts}
    slim["facts"]["subjects"] = [
        {name: subject[name] for name in _SLIM_SUBJECT if name in subject}
        for subject in facts.get("subjects") or ()
    ]
    slim["slim"] = True
    return slim


# Weather the model must keep every instance of: snow, storms and the exceptional.
_RARE_WEATHER = frozenset(
    {
        "exceptional",
        "hail",
        "lightning",
        "lightning-rainy",
        "pouring",
        "snowy",
        "snowy-rainy",
        "windy",
        "windy-variant",
    }
)


def thin_kind(state: str, unusual: bool, features: str | None) -> str | None:
    """Return the kind a slim row may be thinned under, or None when it never is.

    Only ordinary dropped events are thinned. Never: an event kept, committed or orphaned;
    one flagged unusual; or one with a rare context — tamper, an alarm armed or triggered,
    snow, a storm or exceptional weather, or any late evidence. Whether its kind is common
    on its camera is decided when thinning (`THIN_COMMON_EVENTS`).
    """
    if state != str(EventState.DROPPED) or unusual or not features:
        return None
    try:
        data = json.loads(features)
        context = {str(name): str(value) for name, value in data.get("context") or ()}
        kind = str(data["kind"])
    except (ValueError, KeyError, TypeError):
        return None
    for name, value in context.items():
        if name == "tamper" and value == "on":
            return None
        if name == "weather" and value in _RARE_WEATHER:
            return None
        if name.startswith("alarm_control_panel.") and value.startswith(("armed", "triggered")):
            return None
        if name.startswith("late:") and value != "none":
            return None
    return kind


def summary(record: EventRecord) -> str:
    """Return what the media browser lists for an event, so it never parses the record."""
    return ", ".join(sorted({s.cls for s in record.facts.subjects.values()})) or "motion"


@dataclass(slots=True, frozen=True)
class PageRow:
    """One event in a page of the panel's list: summary columns only."""

    id: str
    camera: str
    area: str
    started: float
    duration: float | None
    unusual: bool
    score: float | None
    classes: list[str]
    state: str
    thumbnail: bool


@dataclass(slots=True, frozen=True)
class BrowseRow:
    """The few columns the media browser needs."""

    id: str
    started: float
    state: str
    unusual: bool
    summary: str
    thumbnail: bool = False
    # A committed video is still on some destination.
    offsite: bool = False


@dataclass(slots=True, frozen=True)
class PruneResult:
    """What one nightly prune did."""

    deleted: int = 0
    slimmed: int = 0
    thumbnails: int = 0


# Whether an event still has a committed video somewhere (evicted copies are `deleted`).
_OFFSITE = (
    "EXISTS (SELECT 1 FROM copies c "
    "WHERE c.event_id = events.id AND json_extract(c.copy, '$.committed') "
    "AND json_extract(c.copy, '$.video') IS NOT NULL "
    "AND NOT COALESCE(json_extract(c.copy, '$.deleted'), 0))"
)
_THUMBNAIL = "EXISTS (SELECT 1 FROM thumbnails t WHERE t.event_id = events.id)"
_BROWSE = f"SELECT id, started, state, unusual, summary, {_THUMBNAIL}, {_OFFSITE} FROM events"


def _browse_row(row: tuple) -> BrowseRow:
    event_id, started, state, unusual, text, thumbnail, offsite = row
    return BrowseRow(
        str(event_id),
        float(started),
        str(state),
        bool(unusual),
        str(text),
        bool(thumbnail),
        bool(offsite),
    )


_UPSERT = (
    "INSERT INTO events (id, camera, started, ended, state, unusual, score, finalized, "
    "summary, area, duration, features, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET ended=excluded.ended, state=excluded.state, "
    "unusual=excluded.unusual, score=excluded.score, finalized=excluded.finalized, "
    "summary=excluded.summary, area=excluded.area, duration=excluded.duration, "
    "features=COALESCE(excluded.features, features), record=excluded.record"
)

_UNFINISHED = (
    str(EventState.PENDING),
    str(EventState.KEEPING),
    str(EventState.FOLLOW_UP),
    str(EventState.DROPPING),
)


class EventIndex:
    """The SQLite index. Construct, `async_open`, use, `async_close`."""

    def __init__(self, hass: HomeAssistant, path: str | Path) -> None:
        """Prepare an index at a path; nothing is opened yet."""
        self._hass = hass
        self._path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._executor: ThreadPoolExecutor | None = None
        # The cap could not be met by thinning: said once per run, not every night.
        self._cap_warned = False

    @property
    def path(self) -> Path:
        """Return the database file."""
        return self._path

    # ---------------------------------------------------------------- plumbing

    def _in_transaction[T](self, call: Callable[[sqlite3.Connection], T]) -> T:
        if self._db is None:
            raise RuntimeError("the index is closed")
        with self._db:
            return call(self._db)

    async def _run[T](self, call: Callable[[sqlite3.Connection], T]) -> T:
        """Run one call, in one transaction, on the index's own thread."""
        if self._executor is None:
            raise RuntimeError("the index is closed")
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._in_transaction, call
        )

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
        if sys.platform == "darwin":
            # The system SQLite on macOS shrinks its purgeable page cache on every statement
            # reset, and with a large cache that can spin for minutes (seen in the tests
            # after big prunes). A small cache keeps it short; Linux is not affected.
            db.execute("PRAGMA cache_size=-256")
        db.executescript(_SCHEMA)
        present = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        for column, definition in _ADDED_COLUMNS:
            if column not in present:
                db.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
        db.executescript(_ADDED_INDEXES)
        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
        )
        db.commit()
        self._db = db

    async def async_open(self) -> None:
        """Open (and create) the database on the index's own thread."""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrypted_stamina_db")
        await asyncio.get_running_loop().run_in_executor(self._executor, self._open)

    async def async_close(self) -> None:
        """Close the database and let its thread go."""
        executor, self._executor = self._executor, None
        if executor is None:
            return

        def _close() -> None:
            if self._db is not None:
                self._db.close()
                self._db = None

        await asyncio.get_running_loop().run_in_executor(executor, _close)
        # Waited for (off the loop): the index's thread is gone once this returns.
        await self._hass.async_add_executor_job(executor.shutdown)

    async def async_delete(self) -> None:
        """Remove the database files (the integration is being removed)."""
        await self.async_close()

        def _unlink() -> None:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self._path}{suffix}").unlink(missing_ok=True)

        await self._hass.async_add_executor_job(_unlink)

    # ---------------------------------------------------------------- meta

    async def async_get_meta(self, key: str) -> str | None:
        """Return one stored value."""
        row = await self._run(
            lambda db: db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        )
        return None if row is None else str(row[0])

    async def async_set_meta(self, values: dict[str, str]) -> None:
        """Store several values."""
        await self._run(
            lambda db: db.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(values.items())
            )
        )

    # ---------------------------------------------------------------- events

    async def async_save_many(self, records: list[tuple[EventRecord, str | None]]) -> None:
        """Insert or replace several events' rows (with their features, when final)."""
        rows = self._event_rows(records)
        await self._run(lambda db: db.executemany(_UPSERT, rows))

    @staticmethod
    def _event_rows(records: list[tuple[EventRecord, str | None]]) -> list[tuple]:
        return [
            (
                record.id,
                record.camera,
                record.facts.started,
                record.facts.ended,
                str(record.state),
                int(record.unusual),
                record.score,
                int(record.finalized),
                summary(record),
                record.area,
                record.facts.duration,
                features,
                json.dumps(record_to_dict(record)),
            )
            for record, features in records
        ]

    async def async_save_with_position(
        self, records: list[tuple[EventRecord, str | None]], position: dict[str, str]
    ) -> None:
        """Save records and the stream position in one transaction.

        A restart then never resumes the stream past a message whose effect on a record was
        not written: either both land, or neither does.
        """
        rows = self._event_rows(records)

        def _save(db: sqlite3.Connection) -> None:
            if rows:
                db.executemany(_UPSERT, rows)
            db.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(position.items())
            )

        await self._run(_save)

    async def async_save(self, record: EventRecord, features: str | None = None) -> None:
        """Insert or replace one event's row."""
        await self.async_save_many([(record, features)])

    async def async_get(self, event_id: str) -> EventRecord | None:
        """Return one event, or None (read and parsed in the executor)."""

        def _read(db: sqlite3.Connection) -> EventRecord | None:
            row = db.execute("SELECT record FROM events WHERE id = ?", (event_id,)).fetchone()
            return None if row is None else record_from_dict(json.loads(row[0]))

        return await self._run(_read)

    async def async_unfinished(self) -> list[EventRecord]:
        """Return every event that still needs attention after a restart.

        Those whose files are not final yet, orphans whose upload may still be completing,
        and those still inside their follow-up window.
        """
        found = await self._run(
            lambda db: [
                record_from_dict(json.loads(row[0]))
                for row in db.execute(
                    "SELECT record FROM events WHERE state IN (?, ?, ?, ?, ?) OR finalized = 0 "
                    "ORDER BY started",
                    (*_UNFINISHED, str(EventState.ORPHANED)),
                )
            ]
        )
        # An orphan whose upload has finished is done; only the others need resuming.
        return [
            record
            for record in found
            if record.state is not EventState.ORPHANED
            or not record.finalized
            or "orphan_complete" not in record.notes
        ]

    async def async_events(
        self,
        *,
        camera: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[EventRecord]:
        """Return events in a time window, optionally for one camera."""
        where, params = self._window(camera, since, until, include_dropped=True)
        order = "DESC" if newest_first else "ASC"
        tail = f" LIMIT {int(limit)}" if limit else ""
        sql = f"SELECT record FROM events {where} ORDER BY started {order}{tail}"
        return await self._run(
            lambda db: [record_from_dict(json.loads(row[0])) for row in db.execute(sql, params)]
        )

    async def async_page(
        self,
        *,
        unusual: bool | None = None,
        include_dropped: bool = False,
        cameras: list[str] | None = None,
        cls: str | None = None,
        before: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[PageRow], tuple[float, str] | None]:
        """Return a page of the panel's event list, newest first, and the next cursor.

        Every filter is applied in SQL; the cursor is `(started, id)` of the last row, so
        events that share a start time are neither repeated nor skipped.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if unusual is not None:
            clauses.append("unusual = ?")
            params.append(int(unusual))
        if not include_dropped:
            clauses.append("state != ?")
            params.append(str(EventState.DROPPED))
        if cameras is not None:
            if not cameras:
                return [], None
            clauses.append(f"camera IN ({','.join('?' * len(cameras))})")
            params.extend(cameras)
        if cls:
            clauses.append("(', ' || summary || ', ') LIKE ?")
            params.append(f"%, {cls.replace('%', '').replace('_', '')}, %")
        if before is not None:
            clauses.append("(started < ? OR (started = ? AND id < ?))")
            params.extend([before[0], before[0], before[1]])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        size = max(1, min(int(limit), 500))
        sql = (
            "SELECT id, camera, area, started, duration, unusual, score, summary, state, "
            f"{_THUMBNAIL} FROM events {where} ORDER BY started DESC, id DESC LIMIT {size}"
        )

        def _read(db: sqlite3.Connection) -> list[PageRow]:
            return [
                PageRow(
                    id=str(row[0]),
                    camera=str(row[1]),
                    area=str(row[2]),
                    started=float(row[3]),
                    duration=None if row[4] is None else float(row[4]),
                    unusual=bool(row[5]),
                    score=None if row[6] is None else float(row[6]),
                    classes=[item for item in str(row[7]).split(", ") if item],
                    state=str(row[8]),
                    thumbnail=bool(row[9]),
                )
                for row in db.execute(sql, params)
            ]

        rows = await self._run(_read)
        following = (rows[-1].started, rows[-1].id) if len(rows) == size else None
        return rows, following

    @staticmethod
    def _window(
        camera: str | None, since: float | None, until: float | None, *, include_dropped: bool
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if camera is not None:
            clauses.append("camera = ?")
            params.append(camera)
        if since is not None:
            clauses.append("started >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started < ?")
            params.append(until)
        if not include_dropped:
            clauses.append("state != ?")
            params.append(str(EventState.DROPPED))
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    async def async_browse(
        self, camera: str, since: float, until: float, limit: int
    ) -> list[BrowseRow]:
        """Return the media browser's rows for one camera and window, newest first."""
        where, params = self._window(camera, since, until, include_dropped=False)
        sql = f"{_BROWSE} {where} ORDER BY started DESC LIMIT {int(limit)}"
        rows = await self._run(lambda db: db.execute(sql, params).fetchall())
        return [_browse_row(row) for row in rows]

    async def async_browse_one(self, event_id: str) -> BrowseRow | None:
        """Return the media browser's row for one event, or None."""
        row = await self._run(
            lambda db: db.execute(f"{_BROWSE} WHERE id = ?", (event_id,)).fetchone()
        )
        return None if row is None else _browse_row(row)

    async def async_days(self, camera: str) -> list[float]:
        """Return the start of every kept event of a camera, for the media browser's days."""
        rows = await self._run(
            lambda db: db.execute(
                "SELECT started FROM events WHERE camera = ? AND state != ? ORDER BY started DESC",
                (camera, str(EventState.DROPPED)),
            ).fetchall()
        )
        return [float(row[0]) for row in rows]

    async def async_cameras(self) -> list[str]:
        """Return every camera with at least one event."""
        rows = await self._run(
            lambda db: db.execute("SELECT DISTINCT camera FROM events").fetchall()
        )
        return [str(row[0]) for row in rows]

    async def async_count(self) -> int:
        """Return how many events the index holds."""
        row = await self._run(lambda db: db.execute("SELECT COUNT(*) FROM events").fetchone())
        return int(row[0])

    def read_learning_rows(self, since: float) -> list[tuple[str, bool, str | None, str, int]]:
        """Return `(id, finalized, features, record, weight)` of every event since a time.

        Oldest first. Synchronous: called from the executor thread the model is rebuilt in,
        on a read-only connection of its own, so the read, the parsing and the feature
        extraction stay off the event loop and never wait for (or block) the index's writer.
        The record JSON is only read for rows without stored features. The weight is how
        many events the row stands for after thinning.
        """
        db = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = db.execute(
                "SELECT id, finalized, features, "
                "CASE WHEN features IS NULL THEN record ELSE '' END, weight "
                "FROM events WHERE started >= ? ORDER BY started",
                (since,),
            )
            return [
                (str(row[0]), bool(row[1]), row[2], row[3], max(1, int(row[4] or 1)))
                for row in cursor
            ]
        finally:
            db.close()

    async def async_set_features(self, features: list[tuple[str, str]]) -> None:
        """Store computed features for rows that had none (a one-off backfill), in chunks."""
        for start in range(0, len(features), PRUNE_CHUNK):
            chunk = [(value, event_id) for event_id, value in features[start : start + PRUNE_CHUNK]]
            await self._run(
                lambda db, chunk=chunk: db.executemany(
                    "UPDATE events SET features = ? WHERE id = ? AND features IS NULL", chunk
                )
            )
            await asyncio.sleep(0)

    # ---------------------------------------------------------------- copies

    async def async_save_copy(self, event_id: str, destination: str, copy: Copy) -> None:
        """Write one copy's header; files are rows of their own (`async_add_file`)."""
        payload = json.dumps(copy.header())

        def _save(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT OR REPLACE INTO copies (event_id, destination, copy) VALUES (?, ?, ?)",
                (event_id, destination, payload),
            )
            if copy.deleted:
                db.execute(
                    "DELETE FROM files WHERE event_id = ? AND destination = ?",
                    (event_id, destination),
                )
            else:
                db.executemany(
                    "INSERT OR IGNORE INTO files (event_id, destination, name, size) "
                    "VALUES (?, ?, ?, ?)",
                    [(event_id, destination, name, size) for name, size in copy.files.items()],
                )

        await self._run(_save)

    async def async_save_copy_header(self, event_id: str, destination: str, copy: Copy) -> None:
        """Write one copy's header only: its files are rows already."""
        payload = json.dumps(copy.header())
        await self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO copies (event_id, destination, copy) VALUES (?, ?, ?)",
                (event_id, destination, payload),
            )
        )

    async def async_add_file(self, event_id: str, destination: str, name: str, size: int) -> None:
        """Record one uploaded file: an append, not a rewrite of the copy."""
        await self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO files (event_id, destination, name, size) "
                "VALUES (?, ?, ?, ?)",
                (event_id, destination, name, size),
            )
        )

    async def async_copies(self, event_id: str) -> dict[str, Copy]:
        """Return one event's copies, by destination."""

        def _read(db: sqlite3.Connection) -> dict[str, Copy]:
            files: dict[str, dict[str, int]] = {}
            for dest, name, size in db.execute(
                "SELECT destination, name, size FROM files WHERE event_id = ?", (event_id,)
            ):
                files.setdefault(str(dest), {})[str(name)] = int(size)
            return {
                str(dest): Copy.from_header(json.loads(raw), files.get(str(dest), {}))
                for dest, raw in db.execute(
                    "SELECT destination, copy FROM copies WHERE event_id = ?", (event_id,)
                )
            }

        return await self._run(_read)

    async def async_video_taken(self, destination: str, path: str, event_id: str) -> bool:
        """Return whether another event's live copy on a destination is the video at `path`."""
        return await self._run(
            lambda db: (
                db.execute(
                    "SELECT 1 FROM copies WHERE destination = ? AND event_id != ? "
                    "AND json_extract(copy, '$.video') = ? "
                    "AND NOT COALESCE(json_extract(copy, '$.deleted'), 0) LIMIT 1",
                    (destination, event_id, path),
                ).fetchone()
                is not None
            )
        )

    async def async_destination_usage(self, destination: str) -> list[tuple[str, float, str, int]]:
        """Return `(event id, start, state, bytes)` for every live copy on a destination."""

        def _read(db: sqlite3.Connection) -> list[tuple[str, float, str, int]]:
            sizes = {
                str(event_id): int(total)
                for event_id, total in db.execute(
                    "SELECT event_id, SUM(size) FROM files WHERE destination = ? GROUP BY event_id",
                    (destination,),
                )
            }
            found = []
            for event_id, started, state, raw in db.execute(
                "SELECT c.event_id, e.started, e.state, c.copy FROM copies c "
                "JOIN events e ON e.id = c.event_id WHERE c.destination = ?",
                (destination,),
            ):
                if not json.loads(raw).get("deleted"):
                    found.append(
                        (str(event_id), float(started), str(state), sizes.get(event_id, 0))
                    )
            return found

        return await self._run(_read)

    async def async_copy_areas(self) -> dict[str, tuple[str, str]]:
        """Return `(area key, area folder)` of every event with a copy on some destination.

        The key is empty for an event recorded before records kept it.
        """
        return await self._run(
            lambda db: {
                str(event_id): (str(key or ""), str(area))
                for event_id, key, area in db.execute(
                    "SELECT DISTINCT e.id, json_extract(e.record, '$.area_key'), e.area "
                    "FROM copies c JOIN events e ON e.id = c.event_id"
                )
            }
        )

    # ---------------------------------------------------------------- actions

    async def async_add_action(self, event_id: str, action: str, created: float) -> None:
        """Write a decided action down before it is carried out."""
        await self._run(
            lambda db: db.execute(
                "INSERT INTO actions (event_id, action, created) VALUES (?, ?, ?)",
                (event_id, action, created),
            )
        )

    async def async_done_action(self, event_id: str, action: str) -> None:
        """Remove an action once it has been carried out."""
        await self._run(
            lambda db: db.execute(
                "DELETE FROM actions WHERE event_id = ? AND action = ?", (event_id, action)
            )
        )

    async def async_actions(self) -> list[tuple[str, str]]:
        """Return every action still to do, oldest first."""
        rows = await self._run(
            lambda db: db.execute("SELECT event_id, action FROM actions ORDER BY id").fetchall()
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    # ---------------------------------------------------------------- thumbnails

    async def async_save_thumbnail(self, event_id: str, data: bytes) -> None:
        """Store an event's thumbnail."""
        await self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO thumbnails (event_id, data) VALUES (?, ?)",
                (event_id, data),
            )
        )

    async def async_thumbnail(self, event_id: str) -> bytes | None:
        """Return an event's thumbnail, if one was stored."""
        row = await self._run(
            lambda db: db.execute(
                "SELECT data FROM thumbnails WHERE event_id = ?", (event_id,)
            ).fetchone()
        )
        return None if row is None else bytes(row[0])

    async def async_has_thumbnail(self, event_id: str) -> bool:
        """Return whether an event still has its thumbnail (they go after a while)."""
        row = await self._run(
            lambda db: db.execute(
                "SELECT 1 FROM thumbnails WHERE event_id = ?", (event_id,)
            ).fetchone()
        )
        return row is not None

    # ---------------------------------------------------------------- pruning

    async def async_prune(
        self,
        *,
        history_before: float,
        fresh_before: float,
        thumbnails_before: float,
        dropped_thumbnails_before: float,
    ) -> PruneResult:
        """Delete rows past the history, slim rows past the fresh tier, drop old thumbnails.

        Never deletes a row with files still out there, and never slims one that is not
        settled. A chunk of `PRUNE_CHUNK` rows per transaction, yielding between them, so a
        large prune never holds the writer (or the event loop) for long; the WAL is
        checkpointed afterwards (passively: it never waits for a reader, such as the model
        rebuild, and `journal_size_limit` trims the file once it is reset).
        """

        def _doomed(db: sqlite3.Connection) -> list[str]:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT id FROM events WHERE finalized = 1 AND state NOT IN (?, ?, ?, ?) "
                    "AND started < ? AND id NOT IN (SELECT DISTINCT event_id FROM files)",
                    (*_UNFINISHED, history_before),
                )
            ]

        def _fresh(db: sqlite3.Connection) -> list[str]:
            # Settled: final, nothing pending, features stored (the model's only source once
            # the record is slim), and no copy still being written.
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT id FROM events WHERE slim = 0 AND started < ? AND finalized = 1 "
                    "AND state NOT IN (?, ?, ?, ?) AND features IS NOT NULL "
                    "AND id NOT IN (SELECT event_id FROM actions) "
                    "AND NOT EXISTS (SELECT 1 FROM copies c WHERE c.event_id = events.id "
                    "AND NOT COALESCE(json_extract(c.copy, '$.committed'), 0) "
                    "AND NOT COALESCE(json_extract(c.copy, '$.deleted'), 0))",
                    (fresh_before, *_UNFINISHED),
                )
            ]

        def _old_thumbnails(db: sqlite3.Connection) -> list[str]:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT t.event_id FROM thumbnails t JOIN events e ON e.id = t.event_id "
                    "WHERE e.started < ? OR (e.state = ? AND e.started < ?)",
                    (thumbnails_before, str(EventState.DROPPED), dropped_thumbnails_before),
                )
            ]

        doomed = await self._run(_doomed)
        for ids in _chunks(doomed):
            await self._run(lambda db, ids=ids: _delete_events(db, ids))
            await asyncio.sleep(0)

        fresh = await self._run(_fresh)
        slimmed = 0
        for ids in _chunks(fresh):
            slimmed += await self._run(lambda db, ids=ids: _slim(db, ids))
            await asyncio.sleep(0)

        thumbnails = await self._run(_old_thumbnails)
        for ids in _chunks(thumbnails):
            await self._run(
                lambda db, ids=ids: db.execute(
                    f"DELETE FROM thumbnails WHERE event_id IN {_IDS}", (json.dumps(ids),)
                )
            )
            await asyncio.sleep(0)

        if doomed or slimmed or thumbnails:
            await self._run(lambda db: db.execute("PRAGMA wal_checkpoint(PASSIVE)"))
        return PruneResult(deleted=len(doomed), slimmed=slimmed, thumbnails=len(thumbnails))

    async def async_size(self) -> int:
        """Return the bytes the index's data takes: its pages in use, free pages excluded.

        Cheap (three pragmas) and immediate: pages a delete frees count at once, even
        though the file itself keeps its size until SQLite reuses them.
        """
        return await self._run(_used_bytes)

    async def async_thin(self, cap_bytes: int) -> int:
        """Thin ordinary dropped history, oldest first, until the index fits under a cap.

        Per camera and common kind, runs of `THIN_EVERY` consecutive thinnable rows keep
        one (the lowest id, which is random, so no time of day is favoured); it takes the
        others' weight, and the model counts it that many times. Only slim rows marked by
        `thin_kind` qualify, so the fresh tier and every protected row are never touched.
        If the cap still cannot be met, that is logged once and nothing more is removed.
        Returns how many rows went.
        """
        if await self.async_size() <= cap_bytes:
            return 0
        removed = 0
        for level in range(1, THIN_PASSES + 1):
            removed += await self._async_thin_pass(cap_bytes, THIN_EVERY**level)
            if await self.async_size() <= cap_bytes:
                break
        else:
            if not self._cap_warned:
                self._cap_warned = True
                _LOGGER.warning(
                    "The event history is over its size cap (%s MB) even after thinning; "
                    "nothing it protects is removed. Raise the cap in the options",
                    cap_bytes // (1024 * 1024),
                )
        if removed:
            await self._run(lambda db: db.execute("PRAGMA wal_checkpoint(PASSIVE)"))
        return removed

    async def _async_thin_pass(self, cap_bytes: int, below: int) -> int:
        """One oldest-first pass over rows weighing less than `below`."""

        def _common(db: sqlite3.Connection) -> set[tuple[str, str]]:
            return {
                (str(camera), str(kind))
                for camera, kind in db.execute(
                    "SELECT camera, thin_kind FROM events WHERE thin_kind IS NOT NULL "
                    "GROUP BY camera, thin_kind HAVING SUM(weight) >= ?",
                    (THIN_COMMON_EVENTS,),
                )
            }

        common = await self._run(_common)
        if not common:
            return 0
        runs: dict[tuple[str, str], list[tuple[str, int]]] = {}
        # Whole runs only, so a pass that stops early never leaves a survivor counting
        # peers that are still there.
        batch: list[tuple[str, int, list[str]]] = []
        removed = 0
        after: tuple[float, str] = (-1.0, "")
        while True:
            rows = await self._run(
                lambda db, after=after: db.execute(
                    "SELECT id, camera, thin_kind, weight, started FROM events "
                    "WHERE thin_kind IS NOT NULL AND weight < ? "
                    "AND (started > ? OR (started = ? AND id > ?)) "
                    "AND id NOT IN (SELECT event_id FROM files) "
                    f"ORDER BY started, id LIMIT {THIN_READ}",
                    (below, after[0], after[0], after[1]),
                ).fetchall()
            )
            last = len(rows) < THIN_READ
            if rows:
                after = (float(rows[-1][4]), str(rows[-1][0]))
            for event_id, camera, kind, weight, _started in rows:
                key = (str(camera), str(kind))
                if key not in common:
                    continue
                run = runs.setdefault(key, [])
                run.append((str(event_id), int(weight)))
                if len(run) < THIN_EVERY:
                    continue
                keep = min(run)[0]
                batch.append(
                    (keep, sum(w for _, w in run), [item for item, _ in run if item != keep])
                )
                run.clear()
                if sum(len(others) for _, _, others in batch) < PRUNE_CHUNK:
                    continue
                removed += await self._async_apply_thinning(batch)
                batch = []
                if await self.async_size() <= cap_bytes:
                    return removed
            if last:
                break
        if batch:
            removed += await self._async_apply_thinning(batch)
        return removed

    async def _async_apply_thinning(self, batch: list[tuple[str, int, list[str]]]) -> int:
        """Remove thinned rows and give their weight to the ones that stand for them."""
        survivors = [(weight, keep) for keep, weight, _ in batch]
        gone = [event_id for _, _, others in batch for event_id in others]

        def _apply(db: sqlite3.Connection) -> None:
            db.executemany("UPDATE events SET weight = ? WHERE id = ?", survivors)
            _delete_events(db, gone)

        await self._run(_apply)
        await asyncio.sleep(0)
        return len(gone)


# A list of ids as one JSON parameter: the statement's text never changes with the chunk's
# length, so SQLite's statement cache is not churned by every chunk. (Churning it made the
# system SQLite on macOS spin in its page cache while finalizing; seen in the tests.)
_IDS = "(SELECT value FROM json_each(?))"


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[start : start + PRUNE_CHUNK] for start in range(0, len(ids), PRUNE_CHUNK)]


def _delete_events(db: sqlite3.Connection, ids: list[str]) -> None:
    listed = (json.dumps(ids),)
    for table, column in (
        ("events", "id"),
        ("copies", "event_id"),
        ("thumbnails", "event_id"),
        ("actions", "event_id"),
    ):
        db.execute(f"DELETE FROM {table} WHERE {column} IN {_IDS}", listed)


def _slim(db: sqlite3.Connection, ids: list[str]) -> int:
    """Slim one chunk of settled rows; an orphan whose upload may still resume is left."""
    updates = []
    for event_id, state, unusual, features, raw in db.execute(
        f"SELECT id, state, unusual, features, record FROM events WHERE id IN {_IDS}",
        (json.dumps(ids),),
    ):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if state == str(EventState.ORPHANED) and "orphan_complete" not in (data.get("notes") or ()):
            continue
        updates.append(
            (
                json.dumps(slim_record(data), separators=(",", ":")),
                thin_kind(str(state), bool(unusual), features),
                event_id,
            )
        )
    db.executemany(
        "UPDATE events SET record = ?, slim = 1, thin_kind = ? WHERE id = ? AND slim = 0", updates
    )
    return len(updates)


def _used_bytes(db: sqlite3.Connection) -> int:
    pages = int(db.execute("PRAGMA page_count").fetchone()[0])
    free = int(db.execute("PRAGMA freelist_count").fetchone()[0])
    size = int(db.execute("PRAGMA page_size").fetchone()[0])
    return (pages - free) * size
