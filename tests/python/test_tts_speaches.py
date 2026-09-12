"""Unit tests for Speaches Kokoro Text-to-Speech (tts.py)."""

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import tts


class TestTtsSpeaches(unittest.TestCase):
    def setUp(self):
        self.service = tts.TtsService(
            backend="speaches",
            url="http://127.0.0.1:6521/v1",
            model="speaches-ai/Kokoro-82M-v1.0-ONNX",
            default_voice="af_heart",
            timeout=5.0,
            cache_capacity=16,
        )

    def test_voice_resolution(self):
        # Spanish
        self.assertEqual(tts.resolve_kokoro_voice("Spanish"), "ef_dora")
        self.assertEqual(tts.resolve_kokoro_voice("es"), "ef_dora")
        self.assertEqual(tts.resolve_kokoro_voice("es-ES"), "ef_dora")
        # English
        self.assertEqual(tts.resolve_kokoro_voice("English"), "af_heart")
        self.assertEqual(tts.resolve_kokoro_voice("en"), "af_heart")
        self.assertEqual(tts.resolve_kokoro_voice("en-GB"), "bf_alice")
        # French
        self.assertEqual(tts.resolve_kokoro_voice("French"), "ff_siwis")
        # Italian
        self.assertEqual(tts.resolve_kokoro_voice("Italian"), "if_sara")
        # Japanese
        self.assertEqual(tts.resolve_kokoro_voice("Japanese"), "jf_alpha")
        # Explicit override
        self.assertEqual(tts.resolve_kokoro_voice("Spanish", "am_adam"), "am_adam")
        # Unknown fallback
        self.assertEqual(tts.resolve_kokoro_voice("Klingon"), "af_heart")

    def test_is_enabled(self):
        self.assertTrue(self.service.is_enabled)
        disabled_svc = tts.TtsService(backend="disabled", url="http://127.0.0.1:6521/v1")
        self.assertFalse(disabled_svc.is_enabled)
        off_svc = tts.TtsService(backend="off", url="http://127.0.0.1:6521/v1")
        self.assertFalse(off_svc.is_enabled)

    def test_check_health_disabled(self):
        disabled_svc = tts.TtsService(backend="disabled")
        health = disabled_svc.check_health()
        self.assertFalse(health["enabled"])

    @patch("urllib.request.urlopen")
    def test_check_health_ready(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "data": [
                {"id": "speaches-ai/Kokoro-82M-v1.0-ONNX"},
                {"id": "other-model"},
            ]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        health = self.service.check_health()
        self.assertTrue(health["enabled"])
        self.assertEqual(health["status"], "ready")
        self.assertIn("speaches-ai/Kokoro-82M-v1.0-ONNX", health["available_models"])

    @patch("urllib.request.urlopen")
    def test_synthesize_and_lru_cache(self, mock_urlopen):
        fake_audio = b"\xff\xfb\x90\x44" + b"\x00" * 100
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = fake_audio
        mock_resp.headers = {"Content-Type": "audio/mp3"}
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # First call -> hits upstream
        audio1, ct1 = self.service.synthesize("Hola mundo", lang="Spanish")
        self.assertEqual(audio1, fake_audio)
        self.assertEqual(ct1, "audio/mp3")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call -> cache hit, no upstream call
        audio2, ct2 = self.service.synthesize("Hola mundo", lang="Spanish")
        self.assertEqual(audio2, fake_audio)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Different text -> cache miss, hits upstream
        audio3, _ = self.service.synthesize("Otro texto", lang="Spanish")
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_synthesize_validation(self):
        with self.assertRaises(ValueError):
            self.service.synthesize("   ")
        with self.assertRaises(ValueError):
            self.service.synthesize("x" * 5001)


if __name__ == "__main__":
    unittest.main()

