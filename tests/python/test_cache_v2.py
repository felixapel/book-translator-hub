"""Deterministic contracts for the namespaced, bounded cache schema v2."""

from __future__ import annotations

import gc
import os
import sqlite3
import stat
import tempfile
import unittest
import warnings
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cache import CACHE_SCHEMA_VERSION, CacheScope, CacheStore

ROOT = Path(__file__).resolve().parents[2]


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


def scope(**overrides: str) -> CacheScope:
    values = {
        "tenant": "user:42",
        "book_id": "book:7",
        "chapter_id": "chapter:3",
        "context_hash": "ctx-a",
        "provider": "local",
        "model": "gemma4-12b",
        "prompt_hash": "prompt-a",
        "protocol_version": "cwa-translate-segments/v1",
    }
    values.update(overrides)
    return CacheScope(**values)


def create_v1_table(
    conn: sqlite3.Connection,
    table: str = "translations",
    *,
    primary_key: bool = True,
) -> None:
    if table not in {"translations", "translations_v1"}:
        raise ValueError("unsupported v1 fixture table")
    key_constraint = "PRIMARY KEY" if primary_key else "NOT NULL"
    conn.execute(
        f"""CREATE TABLE {table} (
            cache_key TEXT {key_constraint},
            source_text TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 1
        )"""
    )


class CacheV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "private-cache"
        self.db_path = self.cache_dir / "translations.db"
        self.clock = MutableClock()
        self.store = CacheStore(
            self.db_path,
            ttl_days=30,
            max_entries=3,
            hit_flush_threshold=100,
            now=self.clock,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_scope_dimensions_and_content_are_all_part_of_key(self) -> None:
        baseline = self.store.compute_key("same text", "English", "Spanish", scope())
        variants = [
            scope(tenant="user:99"),
            scope(book_id="book:8"),
            scope(chapter_id="chapter:4"),
            scope(context_hash="ctx-b"),
            scope(provider="minimax"),
            scope(model="MiniMax-M3"),
            scope(prompt_hash="prompt-b"),
            scope(protocol_version="cwa-translate-segments/v2"),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    baseline,
                    self.store.compute_key(
                        "same text", "English", "Spanish", variant
                    ),
                )

        self.assertNotEqual(
            baseline,
            self.store.compute_key("other text", "English", "Spanish", scope()),
        )
        self.assertNotEqual(
            baseline,
            self.store.compute_key("same text", "French", "Spanish", scope()),
        )

    def test_ttl_is_enforced_on_read_and_stale_rows_are_removed(self) -> None:
        self.store.put("hello", "English", "Spanish", "hola", scope())
        self.assertEqual(
            self.store.get("hello", "English", "Spanish", scope()), "hola"
        )

        self.clock.advance(days=31)
        self.assertIsNone(
            self.store.get("hello", "English", "Spanish", scope())
        )
        remaining = self.store.connection().execute(
            "SELECT COUNT(*) FROM translations_v2"
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_get_many_aligns_results_and_hashes_duplicates_once(self) -> None:
        self.store.put("hello", "English", "Spanish", "hola", scope())
        self.store.put("bye", "English", "Spanish", "adiós", scope())
        entries = [
            ("hello", "English", "Spanish", scope()),
            ("missing", "English", "Spanish", scope()),
            ("hello", "English", "Spanish", scope()),
            ("bye", "English", "Spanish", scope()),
        ]
        with mock.patch.object(
            self.store, "compute_key", wraps=self.store.compute_key
        ) as keys:
            results = self.store.get_many(entries)
        self.assertEqual(results, ["hola", None, "hola", "adiós"])
        self.assertEqual(keys.call_count, 3)
        self.assertEqual(self.store.get_many([]), [])

    def test_get_many_reads_expired_rows_as_misses(self) -> None:
        self.store.put("hello", "English", "Spanish", "hola", scope())
        self.clock.advance(days=31)
        self.assertEqual(
            self.store.get_many([("hello", "English", "Spanish", scope())]),
            [None],
        )
        remaining = self.store.connection().execute(
            "SELECT COUNT(*) FROM translations_v2"
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_hard_cap_is_never_exceeded(self) -> None:
        for index in range(6):
            self.clock.advance(seconds=1)
            self.store.put(
                f"source-{index}",
                "English",
                "Spanish",
                f"target-{index}",
                scope(context_hash=f"ctx-{index}"),
            )

        stats = self.store.stats()
        self.assertEqual(stats["total_entries"], 3)
        self.assertIsNone(
            self.store.get(
                "source-0", "English", "Spanish", scope(context_hash="ctx-0")
            )
        )
        self.assertEqual(
            self.store.get(
                "source-5", "English", "Spanish", scope(context_hash="ctx-5")
            ),
            "target-5",
        )

    def test_put_many_uses_one_transaction_for_one_translation_batch(self) -> None:
        statements: list[str] = []
        connection = self.store.connection()
        connection.set_trace_callback(statements.append)
        try:
            self.store.put_many([
                (
                    f"source-{index}", "English", "Spanish",
                    f"target-{index}", scope(context_hash=f"ctx-{index}"),
                )
                for index in range(5)
            ])
        finally:
            connection.set_trace_callback(None)

        self.assertEqual(
            sum(statement == "COMMIT" for statement in statements), 1
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM translations_v2").fetchone()[0],
            self.store.max_entries,
        )

    def test_cache_hit_does_not_write_until_counters_are_flushed(self) -> None:
        self.store.put("hello", "English", "Spanish", "hola", scope())
        connection = self.store.connection()
        changes_before = connection.total_changes

        self.assertEqual(
            self.store.get("hello", "English", "Spanish", scope()), "hola"
        )
        self.assertEqual(connection.total_changes, changes_before)

        stats = self.store.stats()
        self.assertEqual(stats["total_hits"], 1)
        self.assertGreater(connection.total_changes, changes_before)

    def test_thread_local_connections_support_concurrent_reads_and_writes(self) -> None:
        concurrent_store = CacheStore(
            Path(self.tmp.name) / "concurrent-cache" / "translations.db",
            ttl_days=30,
            max_entries=200,
            hit_flush_threshold=8,
            now=self.clock,
        )

        def worker(worker_id: int) -> None:
            for offset in range(20):
                source = f"source-{worker_id}-{offset}"
                translated = f"target-{worker_id}-{offset}"
                concurrent_store.put(
                    source, "English", "Spanish", translated, scope()
                )
                self.assertEqual(
                    concurrent_store.get(
                        source, "English", "Spanish", scope()
                    ),
                    translated,
                )

        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(worker, range(8)))
            stats = concurrent_store.stats()
            self.assertEqual(stats["total_entries"], 160)
            self.assertEqual(stats["total_hits"], 160)
        finally:
            concurrent_store.close()

    def test_source_text_is_not_persisted_and_identifiers_are_hashed(self) -> None:
        private_source = "private source text that must not be stored"
        private_scope = scope(
            tenant="private-user@example.invalid",
            book_id="private-book-title",
            chapter_id="private-chapter-title",
        )
        self.store.put(
            private_source, "English", "Spanish", "texto traducido", private_scope
        )
        self.store.checkpoint()

        columns = {
            row[1]
            for row in self.store.connection().execute(
                "PRAGMA table_info(translations_v2)"
            )
        }
        self.assertNotIn("source_text", columns)
        db_bytes = self.db_path.read_bytes()
        self.assertNotIn(private_source.encode(), db_bytes)
        self.assertNotIn(private_scope.tenant.encode(), db_bytes)
        self.assertNotIn(private_scope.book_id.encode(), db_bytes)
        self.assertNotIn(private_scope.chapter_id.encode(), db_bytes)

    def test_new_cache_paths_are_private(self) -> None:
        self.assertEqual(
            stat.S_IMODE(self.cache_dir.stat().st_mode), 0o700
        )
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_v1_table_is_preserved_but_never_served_as_v2(self) -> None:
        self.store.close()
        self.tmp.cleanup()

        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "migration-cache"
        self.cache_dir.mkdir(mode=0o700)
        self.db_path = self.cache_dir / "translations.db"
        conn = sqlite3.connect(self.db_path)
        create_v1_table(conn)
        conn.execute(
            "INSERT INTO translations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-key",
                "legacy source",
                "English",
                "Spanish",
                "legacy target",
                "legacy-model",
                self.clock().isoformat(),
                7,
            ),
        )
        conn.commit()
        conn.close()

        self.store = CacheStore(
            self.db_path,
            ttl_days=30,
            max_entries=3,
            now=self.clock,
        )
        names = {
            row[0]
            for row in self.store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("translations", names)
        self.assertIn("translations_v2", names)
        self.assertNotIn("translations_v1", names)
        legacy_count = self.store.connection().execute(
            "SELECT COUNT(*) FROM translations"
        ).fetchone()[0]
        self.assertEqual(legacy_count, 1)
        self.assertIsNone(
            self.store.get(
                "legacy source", "English", "Spanish", scope(model="legacy-model")
            )
        )
        self.store.put(
            "v2 source", "English", "Spanish", "v2 target", scope()
        )
        legacy_conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                legacy_conn.execute(
                    "SELECT translated_text FROM translations WHERE cache_key = ?",
                    ("legacy-key",),
                ).fetchone()[0],
                "legacy target",
            )
            legacy_conn.execute(
                "INSERT INTO translations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-key-2",
                    "second legacy source",
                    "English",
                    "French",
                    "second legacy target",
                    "legacy-model",
                    self.clock().isoformat(),
                    1,
                ),
            )
            legacy_conn.commit()
            self.assertEqual(
                legacy_conn.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            legacy_conn.close()
        self.assertEqual(
            self.store.get("v2 source", "English", "Spanish", scope()),
            "v2 target",
        )
        self.assertEqual(
            self.store.connection().execute("PRAGMA user_version").fetchone()[0],
            CACHE_SCHEMA_VERSION,
        )

    def test_unreleased_draft_layout_is_normalized_atomically(self) -> None:
        self.store.put(
            "draft v2 source", "English", "Spanish", "draft v2 target", scope()
        )
        self.store.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE translations_v2 RENAME TO translations")
        create_v1_table(conn, "translations_v1")
        conn.execute(
            "INSERT INTO translations_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "draft-v1-key",
                "draft v1 source",
                "English",
                "Spanish",
                "draft v1 target",
                "legacy-model",
                self.clock().isoformat(),
                2,
            ),
        )
        conn.commit()
        conn.close()

        self.store = CacheStore(
            self.db_path,
            ttl_days=30,
            max_entries=3,
            now=self.clock,
        )
        names = {
            row[0]
            for row in self.store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("translations", names)
        self.assertIn("translations_v2", names)
        self.assertNotIn("translations_v1", names)
        self.assertEqual(
            self.store.connection().execute(
                "SELECT translated_text FROM translations WHERE cache_key = ?",
                ("draft-v1-key",),
            ).fetchone()[0],
            "draft v1 target",
        )
        self.assertEqual(
            self.store.get("draft v2 source", "English", "Spanish", scope()),
            "draft v2 target",
        )
        self.assertEqual(
            self.store.connection().execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )

    def test_draft_normalization_rolls_back_if_second_rename_fails(self) -> None:
        self.store.put(
            "draft v2 source", "English", "Spanish", "draft v2 target", scope()
        )
        self.store.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE translations_v2 RENAME TO translations")
        create_v1_table(conn, "translations_v1")
        conn.commit()
        conn.close()

        class FailingSecondRenameStore(CacheStore):
            def _new_connection(self) -> sqlite3.Connection:
                connection = super()._new_connection()

                def deny_second_rename(
                    action: int,
                    _database: str | None,
                    table: str | None,
                    _source: str | None,
                    _trigger: str | None,
                ) -> int:
                    if (
                        action == sqlite3.SQLITE_ALTER_TABLE
                        and table == "translations_v1"
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(deny_second_rename)
                return connection

        with self.assertRaises(sqlite3.DatabaseError):
            FailingSecondRenameStore(
                self.db_path,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(names, {"translations", "translations_v1"})
            self.assertIn(
                "tenant_hash",
                {row[1] for row in conn.execute("PRAGMA table_info(translations)")},
            )
            self.assertIn(
                "source_text",
                {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(translations_v1)")
                },
            )
        finally:
            conn.close()

    def test_incomplete_existing_schemas_fail_closed(self) -> None:
        self.store.close()

        malformed_v1 = Path(self.tmp.name) / "malformed-v1" / "translations.db"
        malformed_v1.parent.mkdir()
        conn = sqlite3.connect(malformed_v1)
        conn.execute("CREATE TABLE translations (source_text TEXT)")
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "unsupported translations"):
            CacheStore(
                malformed_v1,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

        malformed_v2 = Path(self.tmp.name) / "malformed-v2" / "translations.db"
        malformed_v2.parent.mkdir()
        conn = sqlite3.connect(malformed_v2)
        conn.execute(
            """CREATE TABLE translations_v2 (
                cache_key TEXT PRIMARY KEY,
                tenant_hash TEXT,
                source_lang TEXT,
                target_lang TEXT,
                created_at TEXT,
                last_accessed_at TEXT
            )"""
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "translations_v2"):
            CacheStore(
                malformed_v2,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

    def test_corrupt_database_closes_connection_before_pragma_failure(self) -> None:
        self.store.close()
        corrupt_dir = Path(self.tmp.name) / "corrupt"
        corrupt_dir.mkdir(mode=0o700)
        corrupt_db = corrupt_dir / "translations.db"
        corrupt_db.write_bytes(b"not a sqlite database")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(sqlite3.DatabaseError):
                CacheStore(
                    corrupt_db,
                    ttl_days=30,
                    max_entries=3,
                    now=self.clock,
                )
            gc.collect()

        self.assertFalse(
            [warning for warning in caught if warning.category is ResourceWarning]
        )

    def test_existing_schemas_without_primary_keys_fail_closed(self) -> None:
        self.store.close()

        malformed_v1 = Path(self.tmp.name) / "unkeyed-v1" / "translations.db"
        malformed_v1.parent.mkdir()
        conn = sqlite3.connect(malformed_v1)
        create_v1_table(conn, primary_key=False)
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "unsupported translations"):
            CacheStore(
                malformed_v1,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

        malformed_v2 = Path(self.tmp.name) / "unkeyed-v2" / "translations.db"
        malformed_v2.parent.mkdir()
        conn = sqlite3.connect(malformed_v2)
        conn.execute(
            """CREATE TABLE translations_v2 (
                cache_key TEXT NOT NULL,
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
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "translations_v2"):
            CacheStore(
                malformed_v2,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

    def test_v2_schema_with_source_text_fails_closed(self) -> None:
        self.store.put("private source", "English", "Spanish", "privado", scope())
        self.store.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE translations_v2 ADD COLUMN source_text TEXT")
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "translations_v2"):
            CacheStore(
                self.db_path,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

    def test_v2_schema_with_composite_primary_key_fails_closed(self) -> None:
        self.store.put("private source", "English", "Spanish", "privado", scope())
        self.store.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE translations_v2 RENAME TO valid_v2")
        definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='valid_v2'"
        ).fetchone()[0]
        columns = definition[definition.index("(") + 1 : definition.rindex(")")]
        columns = columns.replace("cache_key TEXT PRIMARY KEY", "cache_key TEXT")
        conn.execute(
            f"CREATE TABLE translations_v2 ({columns}, extra_pk TEXT, "
            "PRIMARY KEY (cache_key, extra_pk))"
        )
        conn.execute("DROP TABLE valid_v2")
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "translations_v2"):
            CacheStore(
                self.db_path,
                ttl_days=30,
                max_entries=3,
                now=self.clock,
            )

    def test_invalid_retention_configuration_fails_closed(self) -> None:
        bad_db = Path(self.tmp.name) / "bad" / "cache.db"
        for ttl_days, max_entries in ((0, 1), (-1, 1), (1, 0), (1, -1)):
            with self.subTest(ttl_days=ttl_days, max_entries=max_entries):
                with self.assertRaises(ValueError):
                    CacheStore(
                        bad_db,
                        ttl_days=ttl_days,
                        max_entries=max_entries,
                        now=self.clock,
                    )

    def test_operator_group_access_keeps_directory_setgid_and_cache_files_group_readable(self) -> None:
        group_dir = Path(self.tmp.name) / "operator-group-cache"
        store = CacheStore(
            group_dir / "cache.db",
            ttl_days=30,
            max_entries=3,
            now=self.clock,
            harden_existing_directory=True,
            operator_group_access=True,
        )
        try:
            self.assertEqual(group_dir.stat().st_mode & 0o7777, 0o2750)
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(group_dir / "cache.db") + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        finally:
            store.close()


class CacheDeploymentContractTests(unittest.TestCase):
    def test_container_enforces_private_cache_directory(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("chmod 700 /app/data", dockerfile)
        self.assertIn('ENV BT_CACHE_HARDEN_EXISTING_DIR="true"', dockerfile)
        self.assertIn("umask 027", entrypoint)
        self.assertIn("stat -c %a /app/data", entrypoint)
        self.assertNotIn("chmod 700 /app/data", entrypoint)

    def test_deployment_defaults_are_bounded(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()
        configuration = (
            ROOT / "docs" / "reference" / "configuration.md"
        ).read_text()
        self.assertIn("BT_CACHE_TTL_DAYS=90", compose)
        self.assertIn("BT_CACHE_MAX_ENTRIES=100000", compose)
        self.assertIn("| `BT_CACHE_TTL_DAYS` | `90` |", configuration)
        self.assertIn("| `BT_CACHE_MAX_ENTRIES` | `100000` |", configuration)
        self.assertNotIn("`0` = unlimited", configuration)


if __name__ == "__main__":
    unittest.main(verbosity=2)
