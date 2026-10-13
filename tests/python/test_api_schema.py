"""Boundary-schema tests that must reject invalid data before side effects."""
import os
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import server


class APISchemaTests(unittest.TestCase):
    def setUp(self):
        server._rate_limit_store.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server._rate_limit_store.clear()

    def test_unpaired_unicode_surrogate_never_reaches_provider(self):
        with mock.patch.object(server, "translate_text") as translate_text:
            response = self.client.post(
                "/translate",
                data='{"text":"\\ud800"}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIsInstance(response.get_json().get("error"), str)
        translate_text.assert_not_called()

    def test_batch_unpaired_unicode_surrogate_never_reaches_provider(self):
        with mock.patch.object(server, "translate_batch") as translate_batch:
            response = self.client.post(
                "/translate/batch",
                data='{"paragraphs":["valid","\\udfff"]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIsInstance(response.get_json().get("error"), str)
        translate_batch.assert_not_called()

    def test_invalid_book_or_chapter_scope_never_reaches_provider(self):
        invalid_payloads = [
            {"text": "hello", "book_id": 7},
            {"text": "hello", "chapter_id": ""},
            {"text": "hello", "book_id": "book\nheader"},
            {"text": "hello", "chapter_id": "x" * 513},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), mock.patch.object(
                server, "translate_text"
            ) as translate_text:
                response = self.client.post("/translate", json=payload)
                self.assertEqual(response.status_code, 400)
                translate_text.assert_not_called()

    def test_cache_scope_metadata_is_forwarded_but_client_tenant_is_ignored(self):
        with mock.patch.object(
            server, "_cache_lookup", return_value="cached"
        ) as cache_lookup:
            response = self.client.post(
                "/translate",
                json={
                    "text": "hello",
                    "book_id": "book-7",
                    "chapter_id": "chapter-3",
                    "tenant": "attacker-controlled",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["translated"], "cached")
        self.assertEqual(cache_lookup.call_args.kwargs, {
            "tenant": "legacy-anonymous",
            "book_id": "book-7",
            "chapter_id": "chapter-3",
            "allow_cloud_fallback": False,
            "glossary": [],
        })

    def test_cloud_fallback_consent_must_be_a_json_boolean(self):
        cases = (
            ("/translate", {"text": "hello"}),
            ("/translate/batch", {"paragraphs": ["hello"]}),
        )
        for endpoint, base in cases:
            for invalid in (None, 0, 1, "true", [], {}):
                with (
                    self.subTest(endpoint=endpoint, invalid=invalid),
                    mock.patch.object(server, "translate_text") as translate_text,
                    mock.patch.object(server, "translate_batch") as translate_batch,
                ):
                    response = self.client.post(
                        endpoint,
                        json={**base, "allow_cloud_fallback": invalid},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.get_json().get("error"),
                        "'allow_cloud_fallback' must be a boolean",
                    )
                    translate_text.assert_not_called()
                    translate_batch.assert_not_called()

    def test_explicit_cloud_fallback_consent_reaches_translation_boundary(self):
        with (
            mock.patch.object(server, "_cache_lookup", return_value=None),
            mock.patch.object(
                server, "translate_text", return_value=("hola", "local")
            ) as translate_text,
            mock.patch.object(server, "put_cache"),
        ):
            single = self.client.post(
                "/translate",
                json={"text": "hello", "allow_cloud_fallback": True},
            )
        self.assertEqual(single.status_code, 200)
        self.assertIs(
            translate_text.call_args.kwargs["allow_cloud_fallback"], True
        )

        batch_result = {
            "translations": ["hola"],
            "backends": ["local"],
            "cached": [False],
            "cached_count": 0,
            "fresh_count": 1,
            "total_elapsed_ms": 1,
        }
        with mock.patch.object(
            server, "_translate_paragraphs", return_value=batch_result
        ) as translate_paragraphs:
            batch = self.client.post(
                "/translate/batch",
                json={
                    "paragraphs": ["hello"],
                    "allow_cloud_fallback": True,
                },
            )
        self.assertEqual(batch.status_code, 200)
        self.assertIs(
            translate_paragraphs.call_args.kwargs["allow_cloud_fallback"], True
        )

    def test_provider_policy_returns_locality_and_opaque_generation(self):
        with mock.patch.object(
            server,
            "provider_policy",
            return_value={"primary": "remote", "fallback": "local"},
        ):
            response = self.client.get("/provider-policy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {
                "primary": "remote",
                "fallback": "local",
                "generation": server.PROVIDER_POLICY_GENERATION,
            }
        )
        encoded = response.get_data(as_text=True)
        for forbidden in ("model", "provider", "endpoint", "url", "key"):
            self.assertNotIn(forbidden, encoded.casefold())

    def test_stale_browser_provider_generation_rejects_before_translation(self):
        stale = {
            "primary": "local",
            "fallback": None,
            "generation": "0" * 32,
        }
        current = {"primary": "remote", "fallback": "local"}
        cases = (
            ("/translate", {"text": "private book text"}),
            ("/translate/batch", {"paragraphs": ["private book text"]}),
        )
        for endpoint, body in cases:
            with (
                self.subTest(endpoint=endpoint),
                mock.patch.object(server, "provider_policy", return_value=current),
                mock.patch.object(server, "_cache_lookup") as cache_lookup,
                mock.patch.object(server, "translate_text") as translate_text,
                mock.patch.object(server, "_translate_paragraphs") as translate_batch,
            ):
                response = self.client.post(
                    endpoint,
                    json={**body, "provider_policy": stale},
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.get_json()["error"], "provider_policy_changed"
            )
            cache_lookup.assert_not_called()
            translate_text.assert_not_called()
            translate_batch.assert_not_called()

    def test_browser_provider_policy_binding_is_exact(self):
        invalid = {
            "primary": "local",
            "fallback": None,
            "generation": "not-a-generation",
        }
        with mock.patch.object(server, "translate_text") as translate_text:
            response = self.client.post(
                "/translate",
                json={"text": "private book text", "provider_policy": invalid},
            )

        self.assertEqual(response.status_code, 400)
        translate_text.assert_not_called()

    def test_managed_reader_without_policy_binding_is_stale(self):
        with mock.patch.object(server.AUTHENTICATOR, "mode", "reader_session"):
            self.assertTrue(server._stale_provider_policy({}))

        with mock.patch.object(server.AUTHENTICATOR, "mode", "token"):
            self.assertFalse(server._stale_provider_policy({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
