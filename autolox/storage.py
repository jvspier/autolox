"""SQLite storage for enrolment sessions and bindings.

Shared by the CLI and the web app. The DB is authoritative; a CSV export
is available for the "paste back into a spreadsheet" workflow but is
never read back.

Schema is created on first open; the module is safe to import against an
existing DB or a fresh path. See docs/phase-3-spec.md for the schema.

Thread-safety note: SQLite connections aren't safe to share across
threads by default. This module uses `check_same_thread=False` and
serialises writes with an application-level lock — fine for our single-
process single-operator model. If you ever host multiple concurrent
enrolment sessions on one process, revisit.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_DB_PATH = os.environ.get("AUTOLOX_DB", "./autolox.db")
CSV_EXPORT_PATH = os.environ.get("AUTOLOX_CSV_EXPORT")  # None disables export


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    reader_uuid  TEXT NOT NULL,
    reader_name  TEXT,
    user_uuid    TEXT NOT NULL,
    user_name    TEXT,
    roster_size  INTEGER NOT NULL,
    roster_json  TEXT,
    status       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bindings (
    id            INTEGER PRIMARY KEY,
    timestamp     TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    user_uuid     TEXT NOT NULL,
    roster_name   TEXT NOT NULL,
    loxone_name   TEXT NOT NULL,
    tag_id        TEXT NOT NULL,
    tag_name      TEXT,
    status        TEXT NOT NULL,
    skip_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_bindings_user_status ON bindings(user_uuid, status);
CREATE INDEX IF NOT EXISTS idx_bindings_session     ON bindings(session_id);
"""

# Columns added after 0.1.0. Applied on open via ALTER TABLE ADD COLUMN,
# guarded by PRAGMA table_info so it's idempotent. Kept in sync with
# _SCHEMA above — new DBs get them from CREATE, existing DBs from ALTER.
_ADD_COLUMNS_IF_MISSING: list[tuple[str, str, str]] = [
    ("sessions", "reader_name", "TEXT"),
    ("sessions", "user_name",   "TEXT"),
    ("sessions", "roster_json", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Session:
    id: str
    started_at: str
    ended_at: str | None
    reader_uuid: str
    user_uuid: str
    roster_size: int
    status: str  # 'active' | 'completed' | 'stopped'
    reader_name: str | None = None
    user_name: str | None = None
    # Roster as recorded at session creation time. List of
    # {roster, loxone, warnings} dicts (from autolox.naming.transform_all).
    # Kept as-is so the history-audit view can render "intended" rows
    # even when they never got a tap. May be None for sessions predating
    # the roster_json column.
    roster: list[dict] | None = None


@dataclass(frozen=True)
class Binding:
    id: int
    timestamp: str
    session_id: str
    user_uuid: str
    roster_name: str
    loxone_name: str
    tag_id: str
    tag_name: str | None
    status: str  # 'bound' | 'dry-run' | 'skipped' | 'error'
    skip_reason: str | None


class Store:
    """Thin wrapper around a SQLite connection.

    Instantiate once per process. Callers must be async-friendly at their
    boundary but all storage calls here are synchronous — the DB writes
    are fast enough that we don't need aiosqlite for iteration 1.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB_PATH):
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._apply_migrations()
        self._write_lock = threading.Lock()

    def _apply_migrations(self) -> None:
        """Add columns that were introduced after 0.1.0 to older DBs."""
        for table, col, coltype in _ADD_COLUMNS_IF_MISSING:
            existing = {
                row[1] for row in self._conn.execute(
                    f"PRAGMA table_info({table})")
            }
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")

    def close(self) -> None:
        self._conn.close()

    # ---- sessions ---------------------------------------------------

    def create_session(self, *, reader_uuid: str, user_uuid: str,
                       roster_size: int,
                       reader_name: str | None = None,
                       user_name: str | None = None,
                       roster: list[dict] | None = None) -> Session:
        sid = str(uuid.uuid4())
        started = _now()
        roster_json_str = (
            json.dumps(roster, ensure_ascii=False) if roster is not None
            else None
        )
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO sessions (id, started_at, ended_at, reader_uuid,"
                " reader_name, user_uuid, user_name, roster_size,"
                " roster_json, status)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'active')",
                (sid, started, reader_uuid, reader_name, user_uuid,
                 user_name, roster_size, roster_json_str),
            )
        return Session(sid, started, None, reader_uuid, user_uuid,
                       roster_size, "active",
                       reader_name=reader_name, user_name=user_name,
                       roster=roster)

    def end_session(self, session_id: str, *, status: str) -> None:
        if status not in ("completed", "stopped"):
            raise ValueError(f"invalid end status {status!r}")
        with self._write_lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, status = ? WHERE id = ?",
                (_now(), status, session_id),
            )

    def get_session(self, session_id: str) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,),
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, *, user_uuid: str | None = None,
                       limit: int = 50) -> list[Session]:
        if user_uuid:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE user_uuid = ?"
                " ORDER BY started_at DESC LIMIT ?",
                (user_uuid, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    # ---- bindings ---------------------------------------------------

    def record_binding(self, *, session_id: str, user_uuid: str,
                        roster_name: str, loxone_name: str,
                        tag_id: str, tag_name: str | None,
                        status: str, skip_reason: str | None = None) -> Binding:
        if status not in ("bound", "dry-run", "skipped", "error"):
            raise ValueError(f"invalid binding status {status!r}")
        ts = _now()
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT INTO bindings (timestamp, session_id, user_uuid,"
                " roster_name, loxone_name, tag_id, tag_name, status, skip_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, session_id, user_uuid, roster_name, loxone_name,
                 tag_id, tag_name, status, skip_reason),
            )
            binding_id = cur.lastrowid
        _optionally_export_csv(ts, session_id, user_uuid, roster_name,
                                loxone_name, tag_id, tag_name, status,
                                skip_reason)
        return Binding(binding_id, ts, session_id, user_uuid, roster_name,
                       loxone_name, tag_id, tag_name, status, skip_reason)

    def get_bound_tags(self, user_uuid: str) -> set[str]:
        """Return the set of tag IDs successfully bound to this user.

        Used by the pre-load in the enroller: any tag in this set that
        gets tapped again should be flagged as already-assigned rather
        than consuming a roster slot.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT tag_id FROM bindings"
            " WHERE user_uuid = ? AND status = 'bound'",
            (user_uuid,),
        ).fetchall()
        return {row["tag_id"] for row in rows}

    def backfill_display_names(
        self, *,
        user_names: dict[str, str],
        reader_names: dict[str, str],
    ) -> None:
        """Fill in NULL `user_name` / `reader_name` on session rows using
        the provided uuid→name maps.

        Idempotent — only touches rows where the column is currently NULL.
        Rows whose uuid isn't in the map stay NULL (user or reader has
        since been deleted, and we don't have a name to record). Used
        after the schema was extended with these columns post-0.1.0."""
        with self._write_lock:
            for uuid_, name in user_names.items():
                self._conn.execute(
                    "UPDATE sessions SET user_name = ? "
                    "WHERE user_uuid = ? AND user_name IS NULL",
                    (name, uuid_),
                )
            for uuid_, name in reader_names.items():
                self._conn.execute(
                    "UPDATE sessions SET reader_name = ? "
                    "WHERE reader_uuid = ? AND reader_name IS NULL",
                    (name, uuid_),
                )

    def bindings_for_session(self, session_id: str) -> list[Binding]:
        rows = self._conn.execute(
            "SELECT * FROM bindings WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [_row_to_binding(r) for r in rows]


def _row_to_session(row: sqlite3.Row) -> Session:
    # `reader_name` and `user_name` are added post-0.1.0 columns and may
    # be missing on rows written before migration; use .get-style access
    # via keys() to stay compatible.
    keys = row.keys()
    roster: list[dict] | None = None
    if "roster_json" in keys and row["roster_json"]:
        try:
            roster = json.loads(row["roster_json"])
        except (ValueError, TypeError):
            roster = None
    return Session(
        id=row["id"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        reader_uuid=row["reader_uuid"],
        user_uuid=row["user_uuid"],
        roster_size=row["roster_size"],
        status=row["status"],
        reader_name=row["reader_name"] if "reader_name" in keys else None,
        user_name=row["user_name"] if "user_name" in keys else None,
        roster=roster,
    )


def _row_to_binding(row: sqlite3.Row) -> Binding:
    return Binding(
        id=row["id"],
        timestamp=row["timestamp"],
        session_id=row["session_id"],
        user_uuid=row["user_uuid"],
        roster_name=row["roster_name"],
        loxone_name=row["loxone_name"],
        tag_id=row["tag_id"],
        tag_name=row["tag_name"],
        status=row["status"],
        skip_reason=row["skip_reason"],
    )


def _optionally_export_csv(ts, session_id, user_uuid, roster_name,
                            loxone_name, tag_id, tag_name, status,
                            skip_reason) -> None:
    """Append a row to the CSV export if AUTOLOX_CSV_EXPORT is set.

    The export is a view of the DB, not a source of truth. If it fails,
    log and continue — DB write already succeeded."""
    if not CSV_EXPORT_PATH:
        return
    path = Path(CSV_EXPORT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    try:
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(["timestamp", "session_id", "user_uuid",
                            "roster_name", "loxone_name", "tag_id",
                            "tag_name", "status", "skip_reason"])
            w.writerow([ts, session_id, user_uuid, roster_name, loxone_name,
                        tag_id, tag_name or "", status, skip_reason or ""])
    except OSError:
        pass  # export is best-effort; DB is authoritative


@contextmanager
def open_store(path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> Iterator[Store]:
    """Context manager for a Store — closes on exit."""
    s = Store(path)
    try:
        yield s
    finally:
        s.close()
