"""Regression tests for minimal EPUB export (rebuild + endpoint + caps)."""
import io
import os
import unittest
import zipfile
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import server
from auth import RequestAuthenticator
from epub_export import build_epub, epub_filename


def _names(payload: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.namelist()


class EpubRebuildTests(unittest.TestCase):
    def test_valid_epub_layout_and_mimetype_first_uncompressed(self):
        payload = build_epub(
            "Chapter 1", ["Hola mundo", "Segunda línea"], target_lang="Spanish"
        )
        self.assertEqual(
            _names(payload),
            [
                "mimetype",
                "META-INF/container.xml",
                "OEBPS/content.opf",
                "OEBPS/toc.ncx",
                "OEBPS/chapter.xhtml",
            ],
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = archive.getinfo("mimetype")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn("<p>Hola mundo</p>", chapter)
        self.assertIn("<p>Segunda l\xednea</p>", chapter)
        self.assertIn("<dc:title>Chapter 1</dc:title>", opf)
        self.assertIn("<dc:language>es</dc:language>", opf)

    def test_text_is_xml_escaped(self):
        payload = build_epub("A&B <test>", ["x < y & \"z\""], target_lang="English")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn("A&amp;B &lt;test&gt;", chapter)
        self.assertIn("x &lt; y &amp; \"z\"", chapter)
        self.assertIn("<dc:title>A&amp;B &lt;test&gt;</dc:title>", opf)
        self.assertIn("<dc:language>en</dc:language>", opf)

    def test_blank_paragraphs_skipped_but_all_blank_rejected(self):
        payload = build_epub("T", ["  ", "kept", ""], target_lang="English")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
        self.assertIn("<p>kept</p>", chapter)
        self.assertNotIn("<p></p>", chapter)
        with self.assertRaises(ValueError):
            build_epub("T", ["  ", ""], target_lang="English")

    def test_structural_validation(self):
        for title, paragraphs in (
            ("", ["a"]),
            ("   ", ["a"]),
            ("T", []),
            ("T", "not-a-list"),
            ("T", ["ok", 7]),
        ):
            with self.subTest(title=title, paragraphs=paragraphs):
                with self.assertRaises(ValueError):
                    build_epub(title, paragraphs)

    def test_unknown_language_falls_back_to_english_code(self):
        payload = build_epub("T", ["a"], target_lang="Klingon")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn("<dc:language>en</dc:language>", opf)

    def test_filename_is_attachment_safe(self):
        self.assertEqual(epub_filename("Chapter 1: El inicio"), "chapter_1__el_inicio.epub")
        self.assertEqual(epub_filename("../../../etc/passwd"), "etc_passwd.epub")
        self.assertEqual(epub_filename('a"b\\c'), "a_b_c.epub")
        self.assertEqual(epub_filename("   "), "translation.epub")
        self.assertTrue(epub_filename("Capítulo ñ").isascii())


class ExportEndpointTests(unittest.TestCase):
    def setUp(self):
        self.original_authenticator = server.AUTHENTICATOR
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        server._auth_inflight_store.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.AUTHENTICATOR = self.original_authenticator
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        server._auth_inflight_store.clear()

    def _payload(self, **overrides):
        body = {
            "paragraphs": ["Hola mundo", "Segunda línea"],
            "title": "Chapter 1",
            "source_lang": "English",
            "target_lang": "Spanish",
        }
        body.update(overrides)
        return body

    def test_happy_path_returns_epub_attachment(self):
        response = self.client.post("/export/epub", json=self._payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/epub+zip")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(
            response.headers["Content-Disposition"].endswith('.epub"')
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.headers.get("X-Request-ID"))
        chapter = zipfile.ZipFile(io.BytesIO(response.get_data())) \
            .read("OEBPS/chapter.xhtml").decode("utf-8")
        self.assertIn("<p>Hola mundo</p>", chapter)

    def test_schema_rejections(self):
        cases = [
            ({"title": "T"}, 400),  # missing paragraphs
            ({"paragraphs": "nope", "title": "T"}, 400),
            ({"paragraphs": [], "title": "T"}, 400),
            ({"paragraphs": ["ok", 7], "title": "T"}, 400),
            ({"paragraphs": ["ok"], "title": "  "}, 400),
            ({"paragraphs": ["ok"], "title": "T", "target_lang": "Klingon"}, 400),
            ({"paragraphs": ["ok"], "title": "T", "book_id": 7}, 400),
        ]
        for body, status in cases:
            with self.subTest(body=body):
                response = self.client.post("/export/epub", json=body)
                self.assertEqual(response.status_code, status)
                self.assertIsInstance(response.get_json().get("error"), str)

    def test_non_json_body_rejected(self):
        response = self.client.post(
            "/export/epub", data="not json", content_type="text/plain"
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_unicode_rejected(self):
        response = self.client.post(
            "/export/epub",
            data='{"paragraphs":["\\ud800"],"title":"T"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_oversized_paragraph_rejected_with_413(self):
        response = self.client.post(
            "/export/epub",
            json=self._payload(paragraphs=["x" * (server.BT_MAX_PARAGRAPH_CHARS + 1)]),
        )
        self.assertEqual(response.status_code, 413)

    def test_paragraph_count_cap_rejected_with_413(self):
        with mock.patch.object(server, "BT_MAX_EXPORT_PARAGRAPHS", 2):
            response = self.client.post(
                "/export/epub", json=self._payload(paragraphs=["a", "b", "c"])
            )
        self.assertEqual(response.status_code, 413)

    def test_total_character_cap_rejected_with_413(self):
        with mock.patch.object(server, "BT_MAX_EXPORT_TOTAL_CHARS", 4):
            response = self.client.post(
                "/export/epub", json=self._payload(paragraphs=["ab", "cde"])
            )
        self.assertEqual(response.status_code, 413)

    def test_title_length_cap_rejected_with_413(self):
        response = self.client.post(
            "/export/epub",
            json=self._payload(title="t" * (server.BT_MAX_EXPORT_TITLE_CHARS + 1)),
        )
        self.assertEqual(response.status_code, 413)

    def test_token_auth_is_enforced(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="export-secret"
        )
        rejected = self.client.post("/export/epub", json=self._payload())
        self.assertEqual(rejected.status_code, 401)
        accepted = self.client.post(
            "/export/epub",
            json=self._payload(),
            headers={"X-BT-Token": "export-secret"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.mimetype, "application/epub+zip")

    def test_api_rate_limit_applies(self):
        with mock.patch.object(server, "_check_rate_limit", return_value=False):
            response = self.client.post("/export/epub", json=self._payload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"], "rate_limited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
