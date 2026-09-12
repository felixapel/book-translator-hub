"""Regression tests for the per-book glossary.

Covers storage CRUD and isolation, translator prompt injection and cache
contract separation, and the /glossary HTTP endpoints plus glossary
propagation into the translate endpoints.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import glossary
import server
import translator


def _temp_db():
    tmp = tempfile.TemporaryDirectory()
    patcher = mock.patch.dict(
        os.environ, {"DB_PATH": os.path.join(tmp.name, "glossary-test.db")}
    )
    patcher.start()
    return tmp, patcher


class GlossaryStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self._env = _temp_db()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_put_and_list_roundtrip(self):
        entry = glossary.put_entry("tenant-a", "book-1", "Starship", "Nave")
        self.assertEqual(entry, {"source": "Starship", "target": "Nave"})
        self.assertEqual(
            glossary.list_entries("tenant-a", "book-1"),
            [{"source": "Starship", "target": "Nave"}],
        )

    def test_upsert_replaces_target(self):
        glossary.put_entry("t", "b", "Warp", "Uno")
        glossary.put_entry("t", "b", "warp", "Dos")
        self.assertEqual(
            glossary.list_entries("t", "b"), [{"source": "warp", "target": "Dos"}]
        )

    def test_delete_removes_term(self):
        glossary.put_entry("t", "b", "Phaser", "Fáser")
        self.assertTrue(glossary.delete_entry("t", "b", "PHASER"))
        self.assertEqual(glossary.list_entries("t", "b"), [])

    def test_delete_missing_term_returns_false(self):
        self.assertFalse(glossary.delete_entry("t", "b", "absent"))

    def test_entries_are_scoped_per_book_and_tenant(self):
        glossary.put_entry("t1", "book-1", "Bridge", "Puente")
        self.assertEqual(glossary.list_entries("t1", "book-2"), [])
        self.assertEqual(glossary.list_entries("t2", "book-1"), [])

    def test_raw_identifiers_are_not_stored(self):
        glossary.put_entry("tenant-secret", "book-secret", "Hull", "Casco")
        with open(os.environ["DB_PATH"], "rb") as handle:
            blob = handle.read()
        self.assertNotIn(b"tenant-secret", blob)
        self.assertNotIn(b"book-secret", blob)

    def test_validation_rejects_bad_terms(self):
        for source, target in [
            ("", "x"),
            ("   ", "x"),
            ("x", ""),
            ("a" * 201, "x"),
            ("x", "b" * 201),
            ("has\x00control", "x"),
            (123, "x"),
            ("x", None),
        ]:
            with self.assertRaises(ValueError, msg=f"{source!r}/{target!r}"):
                glossary.put_entry("t", "b", source, target)


class GlossaryPromptTests(unittest.TestCase):
    def test_empty_glossary_renders_no_block(self):
        self.assertEqual(translator.format_glossary_block(None), "")
        self.assertEqual(translator.format_glossary_block([]), "")

    def test_block_lists_exact_mappings(self):
        block = translator.format_glossary_block(
            [("Starship", "Nave estelar")]
        )
        self.assertIn("Starship => Nave estelar", block)

    def test_contract_without_glossary_is_unchanged(self):
        self.assertEqual(
            translator.single_cache_contract("English", "Spanish"),
            translator.single_cache_contract("English", "Spanish", None),
        )
        self.assertEqual(
            translator.single_cache_contract("English", "Spanish"),
            translator.single_cache_contract("English", "Spanish", []),
        )

    def test_contract_separates_glossaries(self):
        base = translator.single_cache_contract("English", "Spanish")
        with_terms = translator.single_cache_contract(
            "English", "Spanish", [("Warp", "Curvatura")]
        )
        other_terms = translator.single_cache_contract(
            "English", "Spanish", [("Warp", "Distorsión")]
        )
        self.assertNotEqual(base.prompt_hash, with_terms.prompt_hash)
        self.assertNotEqual(with_terms.prompt_hash, other_terms.prompt_hash)
        # Ordering of the same mappings shares one contract.
        reordered = translator.single_cache_contract(
            "English", "Spanish",
            [("Shield", "Escudo"), ("Warp", "Curvatura")],
        )
        canonical = translator.single_cache_contract(
            "English", "Spanish",
            [("Warp", "Curvatura"), ("Shield", "Escudo")],
        )
        self.assertEqual(reordered.prompt_hash, canonical.prompt_hash)

    def test_batch_contract_separates_glossaries(self):
        texts = ["alpha segment", "beta segment"]
        base = translator.batch_cache_contract(texts, [0, 1], "English", "Spanish")
        with_terms = translator.batch_cache_contract(
            texts, [0, 1], "English", "Spanish", [("Warp", "Curvatura")]
        )
        self.assertNotEqual(base.prompt_hash, with_terms.prompt_hash)

    def test_single_translation_injects_glossary_into_system_prompt(self):
        captured = {}

        def fake_complete(user_content, system_prompt, *args, **kwargs):
            captured["system"] = system_prompt
            return "done", "local"

        with mock.patch.object(
            translator, "_complete", side_effect=fake_complete
        ):
            translator.translate_text(
                "hello",
                "English",
                "Spanish",
                budget=translator.create_work_budget(),
                glossary=[("Warp", "Curvatura")],
            )
        self.assertIn("Warp => Curvatura", captured["system"])

    def test_malformed_glossary_entries_are_rejected(self):
        with self.assertRaises(ValueError):
            translator.single_cache_contract(
                "English", "Spanish", [("only-source",)]
            )


class GlossaryEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self._env = _temp_db()
        self.app = server.app.test_client()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_crud_roundtrip(self):
        scope = {"book_id": "book-g", "chapter_id": "ch-1"}
        posted = self.app.post(
            "/glossary",
            json={"source": "Starship", "target": "Nave", **scope},
        )
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(
            posted.get_json()["entry"],
            {"source": "Starship", "target": "Nave"},
        )
        listed = self.app.get(
            "/glossary", query_string={"book_id": "book-g"}
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            listed.get_json()["entries"],
            [{"source": "Starship", "target": "Nave"}],
        )
        deleted = self.app.delete(
            "/glossary", json={"source": "starship", **scope}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["deleted"])
        relisted = self.app.get(
            "/glossary", query_string={"book_id": "book-g"}
        )
        self.assertEqual(relisted.get_json()["entries"], [])

    def test_delete_missing_term_is_404(self):
        resp = self.app.delete(
            "/glossary",
            json={"source": "absent", "book_id": "b", "chapter_id": "c"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_validation_errors_are_400(self):
        bad_posts = [
            {"target": "x"},
            {"source": "", "target": "x"},
            {"source": "x" * 201, "target": "y"},
        ]
        for body in bad_posts:
            resp = self.app.post("/glossary", json=body)
            self.assertEqual(resp.status_code, 400, msg=body)
        self.assertEqual(
            self.app.post("/glossary", data="nope").status_code, 400
        )
        self.assertEqual(
            self.app.delete("/glossary", json={"book_id": "b"}).status_code,
            400,
        )

    def test_entries_are_isolated_per_book(self):
        self.app.post(
            "/glossary",
            json={"source": "A", "target": "B", "book_id": "one"},
        )
        other = self.app.get("/glossary", query_string={"book_id": "two"})
        self.assertEqual(other.get_json()["entries"], [])

    @mock.patch("server.put_cache")
    @mock.patch("server._cache_lookup", return_value=None)
    @mock.patch("server.translate_text", return_value=("Hola", "local"))
    def test_translate_receives_stored_glossary(
        self, mock_translate, mock_lookup, mock_put
    ):
        self.app.post(
            "/glossary",
            json={
                "source": "Starship",
                "target": "Nave",
                "book_id": "book-g",
                "chapter_id": "ch-1",
            },
        )
        resp = self.app.post(
            "/translate",
            json={"text": "hello", "book_id": "book-g", "chapter_id": "ch-1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            mock_translate.call_args.kwargs["glossary"],
            [("Starship", "Nave")],
        )

    @mock.patch("server.put_cache")
    @mock.patch("server._cache_lookup", return_value=None)
    @mock.patch("server.translate_text", return_value=("Hola", "local"))
    def test_translate_without_terms_sends_empty_glossary(
        self, mock_translate, mock_lookup, mock_put
    ):
        resp = self.app.post(
            "/translate",
            json={"text": "hello", "book_id": "book-g", "chapter_id": "ch-1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_translate.call_args.kwargs["glossary"], [])

    def test_batch_receives_stored_glossary(self):
        from translator import BatchTranslationItem

        scope = {"book_id": "book-g", "chapter_id": "ch-1"}
        self.app.post(
            "/glossary",
            json={"source": "Warp", "target": "Curvatura", **scope},
        )
        with (
            mock.patch.object(
                server, "get_cached_many", return_value=[None]
            ),
            mock.patch.object(
                server,
                "translate_batch",
                return_value=[
                    BatchTranslationItem("Hola", "local", False, "direct")
                ],
            ) as mock_batch,
            mock.patch.object(server, "put_cache_many"),
        ):
            resp = self.app.post(
                "/translate/batch",
                json={"paragraphs": ["hello"], **scope},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            mock_batch.call_args.kwargs["glossary"], [("Warp", "Curvatura")]
        )


if __name__ == "__main__":
    unittest.main()
