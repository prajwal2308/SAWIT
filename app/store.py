"""SQLite persistence, with FTS5 for the search you actually came here for."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import ReelNote

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    api_key       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx ON users(lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS users_key_idx ON users(api_key);

CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,
    -- Whose note this is. Every read and write in this module filters on it;
    -- see the class docstring for why that is enforced here and not upstream.
    user_id       TEXT NOT NULL DEFAULT '',
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
    source_mid    TEXT,
    -- float32 vector of the note's meaning, for search that is not keyword
    -- matching. Null is normal: embedding is best-effort and a note without
    -- one is still findable through FTS.
    embedding     BLOB
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
    """Notes, scoped to one account.

    SQLite has no row-level security, so the isolation lives here instead: a
    Store is bound to a user id and every note query filters on it inside this
    module. Endpoints cannot forget the filter because they never write one —
    asking an unbound Store for notes raises rather than returning somebody
    else's. That is the difference between isolation that holds and isolation
    that holds until the next endpoint is added in a hurry.
    """

    def __init__(self, path: str, user_id: str | None = None) -> None:
        self.path = path
        self.user_id = user_id
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS leaves a database made by an older
            # version untouched, so a column added later has to be added here.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
            if "embedding" not in existing:
                conn.execute("ALTER TABLE notes ADD COLUMN embedding BLOB")
            if "user_id" not in existing:
                conn.execute("ALTER TABLE notes ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS notes_user_idx ON notes(user_id, created_at DESC)"
            )

    def for_user(self, user_id: str) -> Store:
        """A view of the same database that can only see one account's notes."""
        view = object.__new__(Store)
        view.path = self.path
        view.user_id = user_id
        return view

    @property
    def _uid(self) -> str:
        if not self.user_id:
            raise RuntimeError(
                "This Store is not bound to an account. Call for_user() before "
                "reading or writing notes."
            )
        return self.user_id

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

    # ---- accounts. Deliberately unscoped: these are how a scope is obtained. ----

    def create_user(self, email: str, password_hash: str, api_key: str) -> str | None:
        """None when the email is already taken."""
        user_id = uuid.uuid4().hex[:12]
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO users (id, email, password_hash, api_key, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (user_id, email, password_hash, api_key, _now()),
                )
        except sqlite3.IntegrityError:
            return None
        return user_id

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(email)=lower(?)", (email,)
            ).fetchone()
        return dict(row) if row else None

    def user_by_api_key(self, api_key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE api_key=?", (api_key,)).fetchone()
        return dict(row) if row else None

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def set_credentials(self, user_id: str, email: str, password_hash: str) -> bool:
        """Give an account an email and password it can sign in with.

        The account bootstrapped from SAWIT_API_KEY owns the notes but has a
        random password nobody knows, so without this its library is reachable
        only by key — and signing up properly would strand it. False when the
        email belongs to someone else.
        """
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET email=?, password_hash=? WHERE id=?",
                    (email, password_hash, user_id),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def adopt_orphan_notes(self, user_id: str) -> int:
        """Hand notes written before accounts existed to their owner.

        Run once, when the first account is created on a database that already
        has notes in it. Without this the existing library becomes invisible:
        every row carries user_id '' and no account can ever match it.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE notes SET user_id=? WHERE user_id=''", (user_id,)
            )
        return cursor.rowcount

    # ---- notes. Every one of these filters on the bound account. ----

    def create_pending(self, url: str, *, mid: str | None = None) -> str | None:
        """Returns None when `mid` was already accepted — a retried webhook."""
        note_id = uuid.uuid4().hex[:12]
        ts = _now()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO notes "
                    "(id, user_id, url, status, created_at, updated_at, source_mid) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (note_id, self._uid, url, "pending", ts, ts, mid),
                )
        except sqlite3.IntegrityError:
            return None
        return note_id

    def set_embedding(self, note_id: str, blob: bytes) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE notes SET embedding=? WHERE id=? AND user_id=?",
                         (blob, note_id, self._uid))

    def embeddings(self, category: str | None = None) -> list[tuple[str, bytes]]:
        """Every stored vector, for a similarity sweep in Python.

        Loading them all is the right shape here: a few thousand notes is a few
        megabytes and a few milliseconds, and it needs no index to maintain and
        no extra service to run. Revisit when the count makes it show in a
        profile, not before.
        """
        clause, params = _category_clause(category, prefix="", conjunction="AND")
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, embedding FROM notes "
                f"WHERE user_id=? AND embedding IS NOT NULL AND status='ready' {clause}",
                (self._uid, *params),
            ).fetchall()
        return [(r["id"], r["embedding"]) for r in rows]

    def awaiting_embedding(self, limit: int = 200) -> list[dict[str, Any]]:
        """Notes written before embedding existed, or whose embedding failed."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes "
                "WHERE user_id=? AND embedding IS NULL AND status='ready' "
                "ORDER BY created_at DESC LIMIT ?",
                (self._uid, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Hydrate notes and hand them back in the order asked for.

        SQL will not preserve the ranking, and the ranking is the whole result.
        """
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes "
                f"WHERE user_id=? AND id IN ({marks})",
                (self._uid, *ids),
            ).fetchall()
        found = {r["id"]: _row_to_dict(r) for r in rows}
        return [found[i] for i in ids if i in found]

    def find_written(self, url: str) -> str | None:
        """The id of a note already written for this URL, if there is one.

        Re-sharing a reel is the normal way to find it again, not a request to
        transcribe it a second time — and each repeat costs a download, a
        Whisper run and a model call. A failed or in-flight note is not a hit:
        the first should be retried and the second is already on its way.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM notes WHERE user_id=? AND url=? AND status='ready' "
                "ORDER BY created_at DESC LIMIT 1",
                (self._uid, url),
            ).fetchone()
        return row["id"] if row else None

    def mark_failed(self, note_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notes SET status='failed', error=?, updated_at=? "
                "WHERE id=? AND user_id=?",
                (error[:2000], _now(), note_id, self._uid),
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
                WHERE id=? AND user_id=?
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
                    self._uid,
                ),
            )
            # FTS rows are keyed by the notes rowid; delete-then-insert keeps a
            # re-processed note from matching twice.
            row = conn.execute("SELECT rowid FROM notes WHERE id=? AND user_id=?",
                               (note_id, self._uid)).fetchone()
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
                "SELECT status, COUNT(*) AS n FROM notes WHERE user_id=? GROUP BY status",
                (self._uid,),
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
                "UPDATE notes SET status='pending', error=NULL, updated_at=? "
                "WHERE id=? AND user_id=?",
                (_now(), note_id, self._uid),
            )
        return cursor.rowcount > 0

    def delete(self, note_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT rowid FROM notes WHERE id=? AND user_id=?",
                               (note_id, self._uid)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (row["rowid"],))
            conn.execute("DELETE FROM notes WHERE id=? AND user_id=?",
                         (note_id, self._uid))
        return True

    def get(self, note_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes "
                "WHERE id=? AND user_id=?",
                (note_id, self._uid),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_thumbnail(self, note_id: str) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute("SELECT thumbnail FROM notes WHERE id=? AND user_id=?",
                               (note_id, self._uid)).fetchone()
        return row["thumbnail"] if row else None

    def recent(
        self, limit: int = 50, category: str | None = None
    ) -> list[dict[str, Any]]:
        clause, params = _category_clause(category, prefix="", conjunction="AND")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *, thumbnail IS NOT NULL AS has_thumbnail FROM notes
                WHERE user_id=? {clause}
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (self._uid, *params, limit),
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
                WHERE notes_fts MATCH ? AND n.user_id=? {clause}
                ORDER BY n.created_at DESC, n.rowid DESC
                LIMIT ?
                """,
                (cleaned, self._uid, *params, limit),
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
                WHERE user_id=? AND category IS NOT NULL
                GROUP BY category
                ORDER BY n DESC, category ASC
                """,
                (self._uid,),
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
    # Both blobs are dropped: they have their own accessors, and a raw one here
    # would ride SELECT * straight into a JSON response and fail to serialise.
    data = {k: row[k] for k in row.keys() if k not in ("thumbnail", "embedding")}
    for field in ("takeaways", "key_facts", "steps", "caveats", "tags"):
        raw = data.get(field)
        data[field] = json.loads(raw) if raw else []
    data["has_thumbnail"] = bool(data.get("has_thumbnail"))
    return data
