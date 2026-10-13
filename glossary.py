"""Per-book glossary: exact source -> target term mappings.

Stored in the shared SQLite database (``DB_PATH``) in table ``glossary_v1``.
Raw tenant/book identifiers are hashed one-way before storage, matching the
privacy contract of ``cache.py``. Traffic is low (user edits), so each call
uses a short-lived connection instead of a pooled store.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from envconfig import env_int

log = logging.getLogger("book-translator.glossary")

GLOSSARY_TABLE = "glossary_v1"
GLOSSARY_MAX_ENTRIES = env_int("BT_GLOSSARY_MAX_ENTRIES", 200)
GLOSSARY_MAX_TERM_CHARS = env_int("BT_GLOSSARY_MAX_TERM_CHARS", 200)
# Book ids are client metadata, so the per-book cap alone would let one
# reader grow the table without bound by naming new books.
GLOSSARY_MAX_TENANT_ENTRIES = env_int("BT_GLOSSARY_MAX_TENANT_ENTRIES", 5000)

_init_lock = threading.Lock()


def _db_path() -> Path:
    return Path(os.getenv("DB_PATH", "translations.db"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_term(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"glossary {field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"glossary {field} must be a non-empty string")
    if len(value) > GLOSSARY_MAX_TERM_CHARS:
        raise ValueError(
            f"glossary {field} exceeds the "
            f"{GLOSSARY_MAX_TERM_CHARS}-character limit"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"glossary {field} contains control characters")
    return value


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {GLOSSARY_TABLE} (
            tenant_hash TEXT NOT NULL,
            book_hash TEXT NOT NULL,
            source_norm TEXT NOT NULL,
            source_text TEXT NOT NULL,
            target_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_hash, book_hash, source_norm)
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


def list_entries(tenant: str, book_id: str) -> list[dict[str, str]]:
    """Return stored glossary entries for one tenant/book, ordered by term."""
    _ensure_init()
    conn = _connect()
    try:
        rows = conn.execute(
            f"""SELECT source_text, target_text FROM {GLOSSARY_TABLE}
                WHERE tenant_hash = ? AND book_hash = ?
                ORDER BY source_norm ASC""",
            (_hash(tenant), _hash(book_id)),
        ).fetchall()
    finally:
        conn.close()
    return [{"source": source, "target": target} for source, target in rows]


def put_entry(tenant: str, book_id: str, source: str, target: str) -> dict[str, str]:
    """Insert or replace one glossary term. Returns the stored entry."""
    source_text = _normalize_term(source, "source")
    target_text = _normalize_term(target, "target")
    _ensure_init()
    conn = _connect()
    try:
        # Count and write in one write transaction so concurrent requests
        # cannot both pass the caps.
        conn.execute("BEGIN IMMEDIATE")
        tenant_hash, book_hash = _hash(tenant), _hash(book_id)
        source_norm = source_text.casefold()
        count = conn.execute(
            f"""SELECT COUNT(*) FROM {GLOSSARY_TABLE}
                WHERE tenant_hash = ? AND book_hash = ?
                AND source_norm != ?""",
            (tenant_hash, book_hash, source_norm),
        ).fetchone()[0]
        if count >= GLOSSARY_MAX_ENTRIES:
            raise ValueError(
                f"glossary exceeds the {GLOSSARY_MAX_ENTRIES}-entry limit"
            )
        tenant_count = conn.execute(
            f"""SELECT COUNT(*) FROM {GLOSSARY_TABLE}
                WHERE tenant_hash = ?
                AND NOT (book_hash = ? AND source_norm = ?)""",
            (tenant_hash, book_hash, source_norm),
        ).fetchone()[0]
        if tenant_count >= GLOSSARY_MAX_TENANT_ENTRIES:
            raise ValueError(
                "glossary exceeds the "
                f"{GLOSSARY_MAX_TENANT_ENTRIES}-entry limit across all books"
            )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"""INSERT INTO {GLOSSARY_TABLE}
                (tenant_hash, book_hash, source_norm, source_text,
                 target_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_hash, book_hash, source_norm)
                DO UPDATE SET source_text = excluded.source_text,
                              target_text = excluded.target_text,
                              updated_at = excluded.updated_at""",
            (
                tenant_hash,
                book_hash,
                source_norm,
                source_text,
                target_text,
                now,
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"source": source_text, "target": target_text}


def delete_entry(tenant: str, book_id: str, source: str) -> bool:
    """Delete one glossary term. Returns True when a row was removed."""
    source_text = _normalize_term(source, "source")
    _ensure_init()
    conn = _connect()
    try:
        cursor = conn.execute(
            f"""DELETE FROM {GLOSSARY_TABLE}
                WHERE tenant_hash = ? AND book_hash = ?
                AND source_norm = ?""",
            (_hash(tenant), _hash(book_id), source_text.casefold()),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
