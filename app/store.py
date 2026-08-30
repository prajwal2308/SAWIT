"""SQLite persistence, with FTS5 for the search you actually came here for."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .schemas import ReelNote

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    status        TEXT NOT NULL,          -- pending | ready | failed
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    title         TEXT,
    category      TEXT,
    one_liner     TEXT,
    takeaways     TEXT,                   -- json array
    key_facts     TEXT,                   -- json array of {label, value}
    steps         TEXT,                   -- json array
    caveats       TEXT,                   -- json array
    tags          TEXT,                   -- json array
    transcript    TEXT,
    source_title  TEXT,
    uploader      TEXT,
    duration      REAL,
    thumbnail     BLOB,
    error         TEXT,
    -- Meta's message id. Webhooks are retried, so this is what stops one
    -- share from being downloaded, transcribed and billed twice.
    source_mid    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS notes_mid_idx ON notes(source_mid)
    WHERE source_mid IS NOT NULL;

CREATE INDEX IF NOT EXISTS notes_created_idx ON notes(created_at DESC);

-- A plain (not contentless) FTS5 table: it keeps its own copy of the text,
-- which costs a little space and buys ordinary DELETE/UPDATE so a reprocessed
-- note can replace its old index row.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, one_liner, body, tags,
    tokenize='porter unicode61'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Two reels shared at once means concurrent writers. WAL lets a reader
        # proceed during a write, and busy_timeout waits for the lock instead
        # of failing the note outright.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create_pending(self, url: str, *, mid: str | None = None) -> str | None:
        """Returns None when `mid` was already accepted — a retried webhook."""
        note_id = uuid.uuid4().hex[:12]
        ts = _now()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO notes (id, url, status, created_at, updated_at, source_mid) "
                    "VALUES (?,?,?,?,?,?)",
                    (note_id, url, "pending", ts, ts, mid),
                )
        except sqlite3.IntegrityError:
            return None
        return note_id

    def mark_failed(self, note_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notes SET status='failed', error=?, updated_at=? WHERE id=?",
                (error[:2000], _now(), note_id),
            )

    def save_note(
        self,
        note_id: str,
        note: ReelNote,
        *,
        transcript: str,
        source_title: str | None = None,
        uploader: str | None = None,
        duration: float | None = None,
        thumbnail: bytes | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE notes SET
                    status='ready', error=NULL, updated_at=?,
                    title=?, category=?, one_liner=?,
                    takeaways=?, key_facts=?, steps=?, caveats=?, tags=?,
                    transcript=?, source_title=?, uploader=?, duration=?, thumbnail=?
                WHERE id=?
                """,
                (
                    _now(),
                    note.title,
                    note.category,
                    note.one_liner,
                    json.dumps(note.takeaways),
                    json.dumps([f.model_dump() for f in note.key_facts]),
                    json.dumps(note.steps),
                    json.dumps(note.caveats),
                    json.dumps(note.tags),
                    transcript,
                    source_title,
                    uploader,
                    duration,
                    thumbnail,
                    note_id,
                ),
            )
            # FTS rows are keyed by the notes rowid; delete-then-insert keeps a
            # re-processed note from matching twice.
            row = conn.execute("SELECT rowid FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                return
            rowid = row["rowid"]
            body = "\n".join(
                [
                    *note.takeaways,
                    *note.steps,
                    *(f"{f.label}: {f.value}" for f in note.key_facts),
                    transcript,
                ]
            )
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
            conn.execute(
                "INSERT INTO notes_fts (rowid, title, one_liner, body, tags) VALUES (?,?,?,?,?)",
                (rowid, note.title, note.one_liner, body, " ".join(note.tags)),
            )

    def status_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM notes GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def recover_orphans(self) -> int:
        """Fail any note left mid-flight by a restart.

        Work runs in-process, so a redeploy or a crash kills whatever was in
        flight. Anything still `pending` at startup is therefore orphaned, and
        would otherwise sit in the list saying "working…" forever. Marking it
        failed makes it visible and retryable. Assumes one process — which the
        Dockerfile's single uvicorn guarantees.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE notes SET status='failed', error=?, updated_at=? "
                "WHERE status='pending'",
                ("Interrupted by a restart. Retry it.", _now()),
            )
        return cursor.rowcount

    def reset_to_pending(self, note_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE notes SET status='pending', error=NULL, updated_at=? WHERE id=?",
                (_now(), note_id),
            )
        return cursor.rowcount > 0

    def delete(self, note_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT rowid FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (row["rowid"],))
            conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return True

    def get(self, note_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes WHERE id=?",
                (note_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_thumbnail(self, note_id: str) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute("SELECT thumbnail FROM notes WHERE id=?", (note_id,)).fetchone()
        return row["thumbnail"] if row else None

    def recent(
        self, limit: int = 50, category: str | None = None
    ) -> list[dict[str, Any]]:
        clause, params = _category_clause(category, prefix="")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes
                {clause}
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def search(
        self, query: str, limit: int = 50, category: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first full-text search — the thing Instagram saves don't do."""
        cleaned = _fts_query(query)
        if not cleaned:
            return []
        clause, params = _category_clause(category, prefix="n.", conjunction="AND")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT n.*, n.thumbnail IS NOT NULL AS has_thumbnail
                FROM notes_fts f
                JOIN notes n ON n.rowid = f.rowid
                WHERE notes_fts MATCH ? {clause}
                ORDER BY n.created_at DESC, n.rowid DESC
                LIMIT ?
                """,
                (cleaned, *params, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def category_counts(self) -> list[tuple[str, int]]:
        """Categories that actually have notes, biggest first.

        Only categories in use are returned — an empty `fitness` chip is a
        dead end, and the point of the row is to narrow, not to enumerate.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT category, COUNT(*) AS n FROM notes
                WHERE category IS NOT NULL
                GROUP BY category
                ORDER BY n DESC, category ASC
                """
            ).fetchall()
        return [(r["category"], r["n"]) for r in rows]


def _category_clause(
    category: str | None, *, prefix: str, conjunction: str = "WHERE"
) -> tuple[str, tuple[str, ...]]:
    """Build the optional category filter. The value is always parameterized."""
    if not category:
        return "", ()
    return f"{conjunction} {prefix}category = ?", (category,)


def _fts_query(query: str) -> str:
    """Quote each term so user input can't be read as FTS5 syntax."""
    terms = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k != "thumbnail"}
    for field in ("takeaways", "key_facts", "steps", "caveats", "tags"):
        raw = data.get(field)
        data[field] = json.loads(raw) if raw else []
    data["has_thumbnail"] = bool(data.get("has_thumbnail"))
    return data
