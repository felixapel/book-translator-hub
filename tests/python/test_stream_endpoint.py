import os
import unittest
import json
from unittest.mock import patch

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import server


class StreamEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = server.app.test_client()

    def test_stream_rejects_non_json(self):
        resp = self.app.post("/translate/stream", data="not json", content_type="text/plain")
        self.assertEqual(resp.status_code, 400)

    def test_stream_rejects_missing_text(self):
        resp = self.app.post("/translate/stream", json={"source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 400)

    def test_stream_empty_text_returns_sse(self):
        resp = self.app.post("/translate/stream", json={"text": "", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        data = resp.get_data(as_text=True)
        self.assertIn("event: done", data)

    def test_stream_source_equals_target_returns_echo(self):
        resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "English"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        data = resp.get_data(as_text=True)
        self.assertIn("Hello world", data)
        self.assertIn("source==target", data)

    @patch("server._cache_lookup")
    def test_stream_cached_hit(self, mock_lookup):
        mock_lookup.return_value = "Hola mundo en cache"
        resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        data = resp.get_data(as_text=True)
        self.assertIn("Hola mundo en cache", data)
        self.assertIn('"cached": true', data)

    @patch("server._cache_lookup", return_value=None)
    @patch("server.translate_text_stream")
    @patch("server.put_cache")
    def test_stream_fresh_generator(self, mock_put_cache, mock_stream, mock_lookup):
        mock_stream.return_value = [("Hola", "local"), (" mundo", "local")]
        resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        data = resp.get_data(as_text=True)
        self.assertIn("Hola", data)
        self.assertIn(" mundo", data)
        self.assertIn("event: done", data)
        self.assertTrue(mock_put_cache.called)

    @patch("server._cache_lookup", return_value=None)
    @patch("server.translate_text_stream")
    def test_stream_preserves_provider_rate_limit_code_and_retry(self, mock_stream, mock_lookup):
        from translator import ProviderUnavailableError
        mock_stream.side_effect = ProviderUnavailableError(
            "limited",
            error_code="provider_rate_limited",
            retry_after_seconds=7,
        )
        resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_data(as_text=True)
        self.assertIn("event: error", data)
        self.assertIn("provider_rate_limited", data)
        self.assertIn("7", data)

    @patch("server._cache_lookup", return_value=None)
    @patch("server.translate_text_stream")
    def test_stream_preserves_work_budget_rejection(self, mock_stream, mock_lookup):
        from work_budget import WorkBudgetExceeded
        mock_stream.side_effect = WorkBudgetExceeded("queue")
        resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_data(as_text=True)
        self.assertIn("event: error", data)
        self.assertIn("work_budget_exhausted", data)

    @patch("server._cache_lookup", return_value=None)
    @patch("server.translate_text_stream")
    @patch("server.put_cache")
    def test_stream_enforces_response_byte_cap(self, mock_put_cache, mock_stream, mock_lookup):
        mock_stream.return_value = [("Hola mundo excesivo", "local")]
        with patch.object(server, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 4):
            resp = self.app.post("/translate/stream", json={"text": "Hello world", "source_lang": "English", "target_lang": "Spanish"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_data(as_text=True)
        self.assertIn("event: error", data)
        self.assertIn("response_too_large", data)
        self.assertNotIn("event: done", data)
        mock_put_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
