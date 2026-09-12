"""Per-paragraph translation feedback: thumbs up/down quality signals.

Stored in the shared SQLite database (``DB_PATH``) in table ``feedback_v1``.
Paragraphs are identified by the opaque client paragraph key (a hash of the
source text plus book/chapter scope); raw book text is never stored. Tenant
and book identifiers are hashed one-way before storage, matching the privacy
contract of ``cache.py`` and ``glossary.py``. Traffic is low (user taps), so
each call uses a short-lived connection instead of a pooled store.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("book-translator.feedback")

FEEDBACK_TABLE = "feedback_v1"
FEEDBACK_MAX_ENTRIES = int(os.getenv("BT_FEEDBACK_MAX_ENTRIES", "5000"))
FEEDBACK_MAX_KEY_CHARS = int(os.getenv("BT_FEEDBACK_MAX_KEY_CHARS", "128"))

RATING_UP = 1
RATING_DOWN = -1

_init_lock = threading.Lock()


def _db_path() -> Path:
    return Path(os.getenv("DB_PATH", "translations.db"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("feedback paragraph key must be a string")
    value = value.strip()
    if not value:
        raise ValueError("feedback paragraph key must be a non-empty string")
    if len(value) > FEEDBACK_MAX_KEY_CHARS:
        raise ValueError(
            "feedback paragraph key exceeds the "
            f"{FEEDBACK_MAX_KEY_CHARS}-character limit"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("feedback paragraph key contains control characters")
    return value


def _normalize_rating(value: object) -> int:
    if value == RATING_UP or value == RATING_DOWN:
        return int(value)
    if value == "up":
        return RATING_UP
    if value == "down":
        return RATING_DOWN
    raise ValueError("feedback rating must be +1/-1 (or 'up'/'down')")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {FEEDBACK_TABLE} (
            tenant_hash TEXT NOT NULL,
            book_hash TEXT NOT NULL,
            para_key TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_hash, book_hash, para_key)
        )"""
    )


def _ensure_init() -> None:
    with _init_lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()


def record_feedback(
    tenant: str, book_id: str, para_key: str, rating: object
) -> dict[str, object]:
    """Insert or replace one paragraph rating. Returns the stored record."""
    key = _normalize_key(para_key)
    score = _normalize_rating(rating)
    _ensure_init()
    conn = _connect()
    try:
        count = conn.execute(
            f"""SELECT COUNT(*) FROM {FEEDBACK_TABLE}
                WHERE tenant_hash = ? AND book_hash = ?
                AND para_key != ?""",
            (_hash(tenant), _hash(book_id), key),
        ).fetchone()[0]
        if count >= FEEDBACK_MAX_ENTRIES:
            raise ValueError(
                f"feedback exceeds the {FEEDBACK_MAX_ENTRIES}-entry limit"
            )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"""INSERT INTO {FEEDBACK_TABLE}
                (tenant_hash, book_hash, para_key, rating,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_hash, book_hash, para_key)
                DO UPDATE SET rating = excluded.rating,
                              updated_at = excluded.updated_at""",
            (_hash(tenant), _hash(book_id), key, score, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"para_key": key, "rating": score}


def get_summary(tenant: str, book_id: str) -> dict[str, int]:
    """Return rating totals for one tenant/book: up/down/total/score."""
    _ensure_init()
    conn = _connect()
    try:
        rows = conn.execute(
            f"""SELECT rating, COUNT(*) FROM {FEEDBACK_TABLE}
                WHERE tenant_hash = ? AND book_hash = ?
                GROUP BY rating""",
            (_hash(tenant), _hash(book_id)),
        ).fetchall()
    finally:
        conn.close()
    up = sum(count for rating, count in rows if rating == RATING_UP)
    down = sum(count for rating, count in rows if rating == RATING_DOWN)
    return {"up": up, "down": down, "total": up + down, "score": up - down}
