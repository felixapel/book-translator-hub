"""Private, bounded SQLite cache for translated paragraphs.

Schema v2 deliberately does not reuse v1 rows.  A translation is only valid
inside the exact tenant/book/chapter/provider/prompt/protocol/context scope that
produced it; treating a v1 row as equivalent would recreate the cross-context
cache poisoning this schema fixes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("book-translator.cache")

CACHE_SCHEMA_VERSION = 2
CACHE_KEY_VERSION = "cwa-translate-cache/v2"
CACHE_TABLE = "translations_v2"
V1_REQUIRED_SCHEMA = {
    "cache_key": ("TEXT", False, True),
    "source_text": ("TEXT", True, False),
    "source_lang": ("TEXT", True, False),
    "target_lang": ("TEXT", True, False),
    "translated_text": ("TEXT", True, False),
    "model": ("TEXT", True, False),
    "created_at": ("TEXT", True, False),
    "hit_count": ("INTEGER", True, False),
}
V2_REQUIRED_SCHEMA = {
    "cache_key": ("TEXT", False, True),
    "tenant_hash": ("TEXT", True, False),
    "book_hash": ("TEXT", True, False),
    "chapter_hash": ("TEXT", True, False),
    "context_hash": ("TEXT", True, False),
    "source_lang": ("TEXT", True, False),
    "target_lang": ("TEXT", True, False),
    "translated_text": ("TEXT", True, False),
    "provider": ("TEXT", True, False),
    "model": ("TEXT", True, False),
    "prompt_hash": ("TEXT", True, False),
    "protocol_version": ("TEXT", True, False),
    "created_at": ("TEXT", True, False),
    "last_accessed_at": ("TEXT", True, False),
    "hit_count": ("INTEGER", True, False),
}
DB_PATH = Path(os.getenv("DB_PATH", "translations.db"))


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


CACHE_TTL_DAYS = _positive_int_env("BT_CACHE_TTL_DAYS", 90)
CACHE_MAX_ENTRIES = _positive_int_env("BT_CACHE_MAX_ENTRIES", 100_000)
CACHE_HIT_FLUSH_THRESHOLD = _positive_int_env("BT_CACHE_HIT_FLUSH_THRESHOLD", 100)


@dataclass(frozen=True, slots=True)
class CacheScope:
    """Every semantic dimension that can change a translation result.

    Raw tenant/book/chapter identifiers are used only while hashing the key.
    The database stores one-way hashes for those identifiers, not their values.
    """

    tenant: str
    book_id: str
    chapter_id: str
    context_hash: str
    provider: str
    model: str
    prompt_hash: str
    protocol_version: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"cache scope {field.name} must be a non-empty string")
            if len(value) > 1024:
                raise ValueError(f"cache scope {field.name} is too long")
            object.__setattr__(self, field.name, value.strip())

    @classmethod
    def legacy(cls, model: str, provider: str = "legacy") -> "CacheScope":
        """Compatibility scope for direct callers predating schema v2.

        The HTTP API never uses this scope; it supplies request metadata and a
        translation-contract fingerprint explicitly.  Keeping this adapter
        avoids an abrupt Python API break for local maintenance scripts.
        """
        if not isinstance(model, str) or not model.strip():
            raise ValueError("cache model must be a non-empty string")
        return cls(
            tenant="legacy",
            book_id="legacy",
            chapter_id="legacy",
            context_hash="legacy",
            provider=provider or "legacy",
            model=model,
            prompt_hash="legacy",
            protocol_version="legacy",
        )


class CacheStore:
    """Thread-safe cache facade with one SQLite connection per worker thread."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        ttl_days: int,
        max_entries: int,
        hit_flush_threshold: int = 100,
        now: Callable[[], datetime] | None = None,
        harden_existing_directory: bool = False,
        operator_group_access: bool = False,
    ) -> None:
        for name, value in (
            ("ttl_days", ttl_days),
            ("max_entries", max_entries),
            ("hit_flush_threshold", hit_flush_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        self.db_path = Path(db_path)
        self.ttl_days = ttl_days
        self.max_entries = max_entries
        self.hit_flush_threshold = hit_flush_threshold
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._harden_existing_directory = harden_existing_directory
        self._operator_group_access = operator_group_access
        self._thread_local = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: set[sqlite3.Connection] = set()
        self._pending_hits: Counter[str] = Counter()
        self._pending_hits_total = 0
        self._pending_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._writes_since_prune = 0
        self._prune_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._prepare_directory()
        try:
            self.init()
        except Exception:
            self.close()
            raise

    def _prepare_directory(self) -> None:
        parent = self.db_path.parent
        created = not parent.exists()
        directory_mode = 0o2750 if self._operator_group_access else 0o700
        parent.mkdir(mode=directory_mode, parents=True, exist_ok=True)
        if self._operator_group_access:
            if created:
                os.chmod(parent, directory_mode)
            mode = parent.stat().st_mode & 0o7777
            if mode != directory_mode:
                raise PermissionError(
                    "operator-group cache access requires a preflight-prepared "
                    "setgid directory with mode 2750"
                )
        elif created or self._harden_existing_directory:
            os.chmod(parent, 0o700)
        else:
            mode = parent.stat().st_mode & 0o777
            if mode & 0o077:
                log.warning(
                    "Cache directory %s is mode %03o; use a dedicated 0700 "
                    "directory or set BT_CACHE_HARDEN_EXISTING_DIR=true",
                    parent,
                    mode,
                )

    def _secure_files(self) -> None:
        file_mode = 0o640 if self._operator_group_access else 0o600
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            try:
                os.chmod(path, file_mode)
            except FileNotFoundError:
                continue

    def _new_connection(self) -> sqlite3.Connection:
        # A connection remains owned by one worker thread during normal use.
        # Disabling SQLite's Python-side thread check only lets shutdown close
        # every registered connection from the main thread without leaking
        # descriptors; query access is still thread-local.
        conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, check_same_thread=False
        )
        try:
            self._secure_files()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA cache_size=-64000")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._secure_files()
        except Exception:
            conn.close()
            raise
        with self._connections_lock:
            self._connections.add(conn)
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._thread_local.conn = conn
        return conn

    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self.connection()
            try:
                # sqlite3 does not start a transaction for DDL. Begin one
                # explicitly so the two draft-layout renames and all schema
                # initialization either commit together or leave the original
                # rollback-compatible layout untouched.
                conn.execute("BEGIN IMMEDIATE")
                self._normalize_cache_tables(conn)
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS translations_v2 (
                        cache_key TEXT PRIMARY KEY,
                        tenant_hash TEXT NOT NULL,
                        book_hash TEXT NOT NULL,
                        chapter_hash TEXT NOT NULL,
                        context_hash TEXT NOT NULL,
                        source_lang TEXT NOT NULL,
                        target_lang TEXT NOT NULL,
                        translated_text TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_hash TEXT NOT NULL,
                        protocol_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_accessed_at TEXT NOT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0
                    )"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_cache_v2_langs
                       ON translations_v2(tenant_hash, source_lang, target_lang)"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_cache_v2_created
                       ON translations_v2(created_at)"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_cache_v2_accessed
                       ON translations_v2(last_accessed_at, created_at)"""
                )
                conn.execute(f"PRAGMA user_version={CACHE_SCHEMA_VERSION}")
                self._delete_expired(conn)
                self._enforce_cap(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            self._secure_files()
            self._initialized = True
            log.info(
                "Translation cache v%d initialized at %s (ttl=%dd cap=%d)",
                CACHE_SCHEMA_VERSION,
                self.db_path,
                self.ttl_days,
                self.max_entries,
            )

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _table_matches_schema(
        conn: sqlite3.Connection,
        table: str,
        expected: dict[str, tuple[str, bool, bool]],
    ) -> bool:
        actual = {
            row[1]: (row[2].upper(), bool(row[3]), bool(row[5]))
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        return set(actual) == set(expected) and all(
            actual.get(column) == signature
            for column, signature in expected.items()
        )

    def _normalize_cache_tables(self, conn: sqlite3.Connection) -> None:
        """Keep the v1 table available for an in-place rollback.

        The unreleased draft schema renamed ``translations`` to
        ``translations_v1`` and reused the old name for v2. Normalize that
        layout atomically while it is still safe to do so before v2.2.0.
        """
        primary_columns = self._table_columns(conn, "translations")
        v2_columns = self._table_columns(conn, CACHE_TABLE)
        draft_v1_columns = self._table_columns(conn, "translations_v1")

        primary_is_v1 = self._table_matches_schema(
            conn, "translations", V1_REQUIRED_SCHEMA
        )
        primary_is_v2 = self._table_matches_schema(
            conn, "translations", V2_REQUIRED_SCHEMA
        )

        if primary_columns and (primary_is_v1 == primary_is_v2):
            raise RuntimeError("unsupported translations cache schema")
        if v2_columns and (
            not self._table_matches_schema(conn, CACHE_TABLE, V2_REQUIRED_SCHEMA)
            or "source_text" in v2_columns
        ):
            raise RuntimeError("translations_v2 does not contain the expected v2 schema")
        if draft_v1_columns and not self._table_matches_schema(
            conn, "translations_v1", V1_REQUIRED_SCHEMA
        ):
            raise RuntimeError("translations_v1 does not contain the expected v1 schema")

        if primary_is_v2:
            if v2_columns:
                raise RuntimeError("ambiguous duplicate v2 cache tables")
            conn.execute("ALTER TABLE translations RENAME TO translations_v2")
            if draft_v1_columns:
                conn.execute("ALTER TABLE translations_v1 RENAME TO translations")
                log.warning(
                    "Normalized draft cache layout; v1 remains available for rollback"
                )
            else:
                log.warning(
                    "Normalized draft v2 cache layout without a preserved v1 table"
                )
        elif not primary_columns and draft_v1_columns:
            conn.execute("ALTER TABLE translations_v1 RENAME TO translations")
            log.warning("Restored the preserved v1 table name for rollback")

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("cache clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _cutoff(self, days: int | None = None) -> str:
        return (self._utc_now() - timedelta(days=days or self.ttl_days)).isoformat()

    @staticmethod
    def _hash_identifier(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def compute_key(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        cache_scope: CacheScope,
    ) -> str:
        if not isinstance(text, str):
            raise ValueError("cache text must be a string")
        if not isinstance(source_lang, str) or not source_lang.strip():
            raise ValueError("source_lang must be a non-empty string")
        if not isinstance(target_lang, str) or not target_lang.strip():
            raise ValueError("target_lang must be a non-empty string")
        if not isinstance(cache_scope, CacheScope):
            raise ValueError("cache_scope must be a CacheScope")
        payload = [
            CACHE_KEY_VERSION,
            cache_scope.tenant,
            cache_scope.book_id,
            cache_scope.chapter_id,
            cache_scope.context_hash,
            cache_scope.provider,
            cache_scope.model,
            cache_scope.prompt_hash,
            cache_scope.protocol_version,
            source_lang.strip(),
            target_lang.strip(),
            text.strip(),
        ]
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        cache_scope: CacheScope,
        *,
        record_hit: bool = True,
    ) -> str | None:
        cache_key = self.compute_key(text, source_lang, target_lang, cache_scope)
        conn = self.connection()
        cutoff = self._cutoff()
        row = conn.execute(
            """SELECT translated_text, created_at FROM translations_v2
               WHERE cache_key = ?""",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        if row[1] < cutoff:
            try:
                conn.execute(
                    """DELETE FROM translations_v2
                       WHERE cache_key = ? AND created_at = ?""",
                    (cache_key, row[1]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return None
        if record_hit:
            self._queue_hit(cache_key)
        log.debug(
            "Cache HIT chars=%d langs=%s->%s provider=%s model=%s",
            len(text),
            source_lang,
            target_lang,
            cache_scope.provider,
            cache_scope.model,
        )
        return row[0]

    def get_many(
        self,
        entries: list[tuple[str, str, str, CacheScope]],
        *,
        record_hit: bool = False,
    ) -> list[str | None]:
        """Fetch many translations with one connection and one query.

        Results align with ``entries``. Identical (text, scope) pairs hash
        once and share the row; expired rows are evicted lazily and read as
        misses, matching :meth:`get`.
        """
        if not isinstance(entries, list):
            raise ValueError("cache entries must be a list")
        if not entries:
            return []
        memo: dict[tuple[str, str, str, CacheScope], str] = {}
        keys: list[str] = []
        for text, source_lang, target_lang, cache_scope in entries:
            memo_key = (text, source_lang, target_lang, cache_scope)
            key = memo.get(memo_key)
            if key is None:
                key = self.compute_key(
                    text, source_lang, target_lang, cache_scope
                )
                memo[memo_key] = key
            keys.append(key)
        conn = self.connection()
        cutoff = self._cutoff()
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            """SELECT cache_key, translated_text, created_at
               FROM translations_v2
               WHERE cache_key IN (%s)""" % placeholders,
            tuple(keys),
        ).fetchall()
        by_key: dict[str, tuple[str, str]] = {}
        for cache_key, translated_text, created_at in rows:
            by_key.setdefault(cache_key, (translated_text, created_at))
        expired = [
            (cache_key, created_at)
            for cache_key, (_, created_at) in by_key.items()
            if created_at < cutoff
        ]
        if expired:
            try:
                conn.executemany(
                    """DELETE FROM translations_v2
                       WHERE cache_key = ? AND created_at = ?""",
                    expired,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            for cache_key, _ in expired:
                del by_key[cache_key]
        results: list[str | None] = []
        for key in keys:
            row = by_key.get(key)
            if row is None:
                results.append(None)
                continue
            if record_hit:
                self._queue_hit(key)
            results.append(row[0])
        return results

    def record_hit(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        cache_scope: CacheScope,
    ) -> None:
        """Record a hit after a caller atomically accepts a complete group."""
        self._queue_hit(
            self.compute_key(text, source_lang, target_lang, cache_scope)
        )

    def _queue_hit(self, cache_key: str) -> None:
        should_flush = False
        with self._pending_lock:
            self._pending_hits[cache_key] += 1
            self._pending_hits_total += 1
            should_flush = self._pending_hits_total >= self.hit_flush_threshold
        if should_flush:
            self.flush_hits()

    def flush_hits(self) -> int:
        with self._flush_lock:
            with self._pending_lock:
                if not self._pending_hits:
                    return 0
                pending = self._pending_hits
                pending_total = self._pending_hits_total
                self._pending_hits = Counter()
                self._pending_hits_total = 0

            conn = self.connection()
            accessed_at = self._utc_now().isoformat()
            try:
                conn.executemany(
                    """UPDATE translations_v2
                       SET hit_count = hit_count + ?, last_accessed_at = ?
                       WHERE cache_key = ?""",
                    [
                        (count, accessed_at, cache_key)
                        for cache_key, count in pending.items()
                    ],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                with self._pending_lock:
                    self._pending_hits.update(pending)
                    self._pending_hits_total += pending_total
                raise
            return pending_total

    def put(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        translated_text: str,
        cache_scope: CacheScope,
    ) -> None:
        self.put_many([(
            text, source_lang, target_lang, translated_text, cache_scope
        )])

    def put_many(
        self,
        entries: list[tuple[str, str, str, str, CacheScope]],
    ) -> None:
        """Commit one batch of cacheable translations in one transaction."""
        if not isinstance(entries, list) or not entries:
            raise ValueError("cache entries must be one non-empty list")
        now = self._utc_now().isoformat()
        rows = []
        for text, source_lang, target_lang, translated_text, cache_scope in entries:
            if not isinstance(translated_text, str) or not translated_text.strip():
                raise ValueError("translated_text must be a non-empty string")
            cache_key = self.compute_key(
                text, source_lang, target_lang, cache_scope
            )
            rows.append((
                cache_key,
                self._hash_identifier(cache_scope.tenant),
                self._hash_identifier(cache_scope.book_id),
                self._hash_identifier(cache_scope.chapter_id),
                self._hash_identifier(cache_scope.context_hash),
                source_lang.strip(),
                target_lang.strip(),
                translated_text,
                cache_scope.provider,
                cache_scope.model,
                cache_scope.prompt_hash,
                cache_scope.protocol_version,
                now,
                now,
            ))
        conn = self.connection()
        try:
            conn.executemany(
                """INSERT INTO translations_v2 (
                       cache_key, tenant_hash, book_hash, chapter_hash,
                       context_hash, source_lang, target_lang, translated_text,
                       provider, model, prompt_hash, protocol_version,
                       created_at, last_accessed_at, hit_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       translated_text = excluded.translated_text,
                       provider = excluded.provider,
                       model = excluded.model,
                       prompt_hash = excluded.prompt_hash,
                       protocol_version = excluded.protocol_version,
                       created_at = excluded.created_at,
                       last_accessed_at = excluded.last_accessed_at""",
                rows,
            )
            # Retention (expiry + cap ORDER BY/OFFSET scan) runs on a
            # watermark, not on every write: the scan serializes the WAL
            # writer and adds p95 on every fresh batch once near capacity.
            # The watermark scales down for tiny test caps so cap behavior
            # stays observable in unit tests.
            with self._prune_lock:
                self._writes_since_prune += len(rows)
                threshold = min(1000, self.max_entries)
                due = self._writes_since_prune >= threshold
                if due:
                    self._writes_since_prune = 0
            if due:
                self._delete_expired(conn)
                self._enforce_cap(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _delete_expired(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "DELETE FROM translations_v2 WHERE created_at < ?", (self._cutoff(),)
        )
        return max(0, cursor.rowcount)

    def _enforce_cap(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """DELETE FROM translations_v2 WHERE cache_key IN (
                   SELECT cache_key FROM translations_v2
                   ORDER BY last_accessed_at DESC, created_at DESC, cache_key DESC
                   LIMIT -1 OFFSET ?
               )""",
            (self.max_entries,),
        )

    def cleanup(self, days: int | None = None) -> int:
        retention_days = self.ttl_days if days is None else days
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days <= 0
        ):
            raise ValueError("days must be a positive integer")
        self.flush_hits()
        conn = self.connection()
        try:
            cursor = conn.execute(
                "DELETE FROM translations_v2 WHERE created_at < ?",
                (self._cutoff(retention_days),),
            )
            deleted = max(0, cursor.rowcount)
            self._enforce_cap(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info(
            "Cache cleanup evicted %d entries older than %d days",
            deleted,
            retention_days,
        )
        return deleted

    def stats(self) -> dict:
        self.flush_hits()
        conn = self.connection()
        try:
            self._delete_expired(conn)
            self._enforce_cap(conn)
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM translations_v2").fetchone()[0]
            total_hits = conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM translations_v2"
            ).fetchone()[0]
            pairs = conn.execute(
                """SELECT source_lang, target_lang, COUNT(*)
                   FROM translations_v2 GROUP BY source_lang, target_lang"""
            ).fetchall()
            legacy_tables = int(
                "source_text" in self._table_columns(conn, "translations")
            ) + conn.execute(
                """SELECT COUNT(*) FROM sqlite_master
                   WHERE type='table' AND name LIKE 'translations_v1%'"""
            ).fetchone()[0]
        except Exception:
            conn.rollback()
            raise
        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "total_entries": total,
            "total_hits": total_hits,
            "db_size_mb": self.db_size_mb(),
            "ttl_days": self.ttl_days,
            "max_entries": self.max_entries,
            "legacy_tables_preserved": legacy_tables,
            "language_pairs": {f"{source}→{target}": count for source, target, count in pairs},
        }

    def db_size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(str(self.db_path) + suffix).stat().st_size
            except FileNotFoundError:
                continue
        return round(total / (1024 * 1024), 2)

    def checkpoint(self) -> None:
        self.flush_hits()
        self.connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._secure_files()

    def close(self) -> None:
        try:
            self.flush_hits()
        except (sqlite3.Error, OSError):
            log.exception("Failed to flush cache hit counters during close")
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                log.exception("Failed to close a cache connection")
        self._thread_local = threading.local()


_harden_existing = os.getenv(
    "BT_CACHE_HARDEN_EXISTING_DIR", "false"
).lower() in ("1", "true", "yes")
_operator_group_access = os.getenv(
    "BT_CACHE_OPERATOR_GROUP_ACCESS", "false"
).lower() in ("1", "true", "yes")

_default_store: CacheStore | None = None
_default_store_lock = threading.Lock()


def _store() -> CacheStore:
    """Build the process-wide store on first cache use, not module import.

    Lazy initialization keeps read-only tooling (schema inspection, CLI help,
    unit-test collection) from creating or migrating an operator database as a
    side effect of merely importing ``cache``.
    """
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = CacheStore(
                    DB_PATH,
                    ttl_days=CACHE_TTL_DAYS,
                    max_entries=CACHE_MAX_ENTRIES,
                    hit_flush_threshold=CACHE_HIT_FLUSH_THRESHOLD,
                    harden_existing_directory=_harden_existing,
                    operator_group_access=_operator_group_access,
                )
    return _default_store


def _scope_for(
    model: str,
    scope: CacheScope | None,
    provider: str = "legacy",
) -> CacheScope:
    if scope is None:
        return CacheScope.legacy(model, provider=provider)
    if not isinstance(scope, CacheScope):
        raise ValueError("scope must be a CacheScope")
    if model and model != scope.model:
        raise ValueError("model and scope.model must match")
    return scope


def compute_cache_key(
    text: str,
    source_lang: str,
    target_lang: str,
    model: str = "",
    *,
    scope: CacheScope | None = None,
    provider: str = "legacy",
) -> str:
    return _store().compute_key(
        text,
        source_lang,
        target_lang,
        _scope_for(model, scope, provider),
    )


def get_cached(
    text: str,
    source_lang: str,
    target_lang: str,
    model: str = "",
    *,
    scope: CacheScope | None = None,
    provider: str = "legacy",
    record_hit: bool = True,
) -> str | None:
    return _store().get(
        text,
        source_lang,
        target_lang,
        _scope_for(model, scope, provider),
        record_hit=record_hit,
    )


def get_cached_many(
    entries: list[tuple[str, str, str, CacheScope]],
    *,
    record_hit: bool = False,
) -> list[str | None]:
    return _store().get_many(entries, record_hit=record_hit)


def record_cache_hit(
    text: str,
    source_lang: str,
    target_lang: str,
    *,
    scope: CacheScope,
) -> None:
    _store().record_hit(text, source_lang, target_lang, scope)


def put_cache(
    text: str,
    source_lang: str,
    target_lang: str,
    translated_text: str,
    model: str = "",
    *,
    scope: CacheScope | None = None,
    provider: str = "legacy",
) -> None:
    _store().put(
        text,
        source_lang,
        target_lang,
        translated_text,
        _scope_for(model, scope, provider),
    )


def put_cache_many(
    entries: list[tuple[str, str, str, str, CacheScope]],
) -> None:
    _store().put_many(entries)


def get_cache_stats() -> dict:
    return _store().stats()


def cleanup_old_entries(days: int = CACHE_TTL_DAYS) -> int:
    return _store().cleanup(days)


def init_db() -> None:
    _store().init()


def _get_conn() -> sqlite3.Connection:
    """Backward-compatible maintenance hook; request code should use helpers."""
    return _store().connection()


def _db_size_mb() -> float:
    return _store().db_size_mb()
