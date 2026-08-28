"""SQLite state: message dedupe, lifecycle, and the repo-verdict cache.

The lifecycle column is the source of truth for what may be deleted from the
mail server. A message is only ever deletable once it reached ``committed``,
which happens after the .eml and every attachment are durably in ``done/``.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STATE_INGESTED = "ingested"
STATE_PROCESSING = "processing"
STATE_COMMITTED = "committed"
STATE_DELETED = "deleted"
STATE_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    uid          TEXT,
    from_addr    TEXT,
    subject      TEXT,
    date_header  TEXT,
    state        TEXT NOT NULL,
    error        TEXT,
    ingested_at  REAL NOT NULL,
    committed_at REAL,
    deleted_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state);

-- Repo-investigation results, keyed so that re-rendering a PDF never re-burns
-- a `claude` invocation. Invalidated by a new repo HEAD or a prompt change.
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key      TEXT PRIMARY KEY,
    message_id     TEXT NOT NULL,
    issue_key      TEXT NOT NULL,
    repo_head      TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_message ON analysis_cache(message_id);

-- Chat-message level dedupe, one row per WhatsApp line ever seen. Selections
-- shared to email overlap in practice (you re-select a message for context),
-- and this is what stops an already-triaged bug being re-analysed and re-billed.
CREATE TABLE IF NOT EXISTS seen_chat_messages (
    fingerprint   TEXT PRIMARY KEY,
    email_id      TEXT NOT NULL,
    first_seen_at REAL NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def is_known(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def record_ingested(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    slug: str,
    uid: str | None,
    from_addr: str,
    subject: str,
    date_header: str,
) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (message_id, slug, uid, from_addr, subject, date_header, state, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO NOTHING
        """,
        (
            message_id,
            slug,
            uid,
            from_addr,
            subject,
            date_header,
            STATE_INGESTED,
            time.time(),
        ),
    )


def set_state(
    conn: sqlite3.Connection,
    message_id: str,
    state: str,
    *,
    error: str | None = None,
) -> None:
    stamp_column = {
        STATE_COMMITTED: "committed_at",
        STATE_DELETED: "deleted_at",
    }.get(state)
    if stamp_column:
        conn.execute(
            f"UPDATE messages SET state = ?, error = ?, {stamp_column} = ? "
            "WHERE message_id = ?",
            (state, error, time.time(), message_id),
        )
    else:
        conn.execute(
            "UPDATE messages SET state = ?, error = ? WHERE message_id = ?",
            (state, error, message_id),
        )


def get(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()


def list_by_state(conn: sqlite3.Connection, *states: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in states)
    return list(
        conn.execute(
            f"SELECT * FROM messages WHERE state IN ({placeholders}) "
            "ORDER BY ingested_at",
            states,
        )
    )


def count_unprocessed(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE state IN (?, ?, ?)",
        (STATE_INGESTED, STATE_PROCESSING, STATE_FAILED),
    ).fetchone()
    return int(row["n"])


def fingerprint_chat_message(
    timestamp: str, sender: str, body: str, attachment: str = ""
) -> str:
    """Stable identity for one WhatsApp line, independent of the email carrying it."""
    import hashlib

    payload = "\x1f".join((timestamp.strip(), sender.strip(), body, attachment))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def filter_unseen(
    conn: sqlite3.Connection, fingerprints: list[str]
) -> tuple[list[str], list[str]]:
    """Split fingerprints into (unseen, already_seen), preserving order."""
    if not fingerprints:
        return [], []
    placeholders = ",".join("?" for _ in fingerprints)
    rows = conn.execute(
        f"SELECT fingerprint FROM seen_chat_messages WHERE fingerprint IN ({placeholders})",
        fingerprints,
    ).fetchall()
    known = {row["fingerprint"] for row in rows}
    unseen = [fp for fp in fingerprints if fp not in known]
    seen = [fp for fp in fingerprints if fp in known]
    return unseen, seen


def mark_seen(conn: sqlite3.Connection, fingerprints: list[str], email_id: str) -> None:
    now = time.time()
    conn.executemany(
        "INSERT INTO seen_chat_messages (fingerprint, email_id, first_seen_at) "
        "VALUES (?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
        [(fp, email_id, now) for fp in fingerprints],
    )


def cache_get(conn: sqlite3.Connection, cache_key: str) -> str | None:
    row = conn.execute(
        "SELECT payload_json FROM analysis_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return row["payload_json"] if row else None


def cache_put(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
    message_id: str,
    issue_key: str,
    repo_head: str,
    prompt_version: str,
    payload_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analysis_cache
            (cache_key, message_id, issue_key, repo_head, prompt_version,
             payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            created_at = excluded.created_at
        """,
        (
            cache_key,
            message_id,
            issue_key,
            repo_head,
            prompt_version,
            payload_json,
            time.time(),
        ),
    )
