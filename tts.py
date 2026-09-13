"""Text-to-Speech (TTS) integration for Book Translator Hub.

Supports local Speaches server with Kokoro-82M-v1.0-ONNX (OpenAI-compatible /v1/audio/speech API).
Includes in-memory LRU audio caching and language-to-voice resolution.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import threading
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("book-translator.tts")

BT_TTS_BACKEND = os.environ.get("BT_TTS_BACKEND", "speaches").strip().lower()
BT_TTS_URL = os.environ.get("BT_TTS_URL", "http://192.168.0.122:6521/v1").rstrip("/")
BT_TTS_MODEL = os.environ.get("BT_TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX")
BT_TTS_DEFAULT_VOICE = os.environ.get("BT_TTS_DEFAULT_VOICE", "af_heart")
BT_TTS_TIMEOUT = float(os.environ.get("BT_TTS_TIMEOUT", "15.0"))
BT_TTS_CACHE_CAPACITY = int(os.environ.get("BT_TTS_CACHE_CAPACITY", "128"))

# Language to Kokoro voice resolution
KOKORO_VOICES: dict[str, str] = {
    # Spanish
    "es": "ef_dora",
    "spanish": "ef_dora",
    "español": "ef_dora",
    "es-es": "ef_dora",
    "es-mx": "ef_dora",
    # English
    "en": "af_heart",
    "english": "af_heart",
    "en-us": "af_heart",
    "en-gb": "bf_alice",
    # French
    "fr": "ff_siwis",
    "french": "ff_siwis",
    "français": "ff_siwis",
    "fr-fr": "ff_siwis",
    # Italian
    "it": "if_sara",
    "italian": "if_sara",
    "italiano": "if_sara",
    "it-it": "if_sara",
    # Portuguese
    "pt": "pf_dora",
    "portuguese": "pf_dora",
    "português": "pf_dora",
    "pt-br": "pf_dora",
    "pt-pt": "pf_dora",
    # Japanese
    "ja": "jf_alpha",
    "japanese": "jf_alpha",
    "日本語": "jf_alpha",
    # Chinese
    "zh": "zf_xiaobei",
    "chinese": "zf_xiaobei",
    "中文": "zf_xiaobei",
    "zh-cn": "zf_xiaobei",
    # Hindi
    "hi": "hf_alpha",
    "hindi": "hf_alpha",
}


def resolve_kokoro_voice(lang: str | None = None, requested_voice: str | None = None) -> str:
    """Resolve the optimal Kokoro voice for the requested language or override."""
    if requested_voice and requested_voice.strip():
        return requested_voice.strip()
    if not lang:
        return BT_TTS_DEFAULT_VOICE
    norm = lang.strip().lower()
    return KOKORO_VOICES.get(norm, BT_TTS_DEFAULT_VOICE)


class TtsService:
    """Service client for local Speaches TTS with Kokoro."""

    def __init__(
        self,
        backend: str = BT_TTS_BACKEND,
        url: str = BT_TTS_URL,
        model: str = BT_TTS_MODEL,
        default_voice: str = BT_TTS_DEFAULT_VOICE,
        timeout: float = BT_TTS_TIMEOUT,
        cache_capacity: int = BT_TTS_CACHE_CAPACITY,
    ) -> None:
        self.backend = backend
        self.url = url
        self.model = model
        self.default_voice = default_voice
        self.timeout = timeout
        self.cache_capacity = max(16, cache_capacity)
        self._cache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._cache_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> TtsService:
        return cls()

    @property
    def is_enabled(self) -> bool:
        return self.backend not in ("disabled", "off", "none", "false", "") and bool(self.url)

    def check_health(self) -> dict[str, Any]:
        """Check connection to Speaches upstream."""
        if not self.is_enabled:
            return {"enabled": False, "backend": "disabled"}
        models_url = f"{self.url}/models"
        req = urllib.request.Request(models_url, headers={"User-Agent": "BookTranslatorHub-TTS"})
        try:
            with urllib.request.urlopen(req, timeout=min(3.0, self.timeout)) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    model_ids = [m.get("id") for m in data.get("data", [])]
                    has_model = self.model in model_ids
                    return {
                        "enabled": True,
                        "backend": "speaches",
                        "status": "ready" if has_model else "model_missing",
                        "url": self.url,
                        "model": self.model,
                        "available_models": model_ids,
                    }
        except Exception as exc:
            log.warning("Speaches TTS healthcheck failed: %s", exc)
            return {
                "enabled": True,
                "backend": "speaches",
                "status": "offline",
                "error": str(exc),
                "url": self.url,
            }
        return {"enabled": True, "backend": "speaches", "status": "unknown"}

    def synthesize(
        self,
        text: str,
        lang: str | None = None,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> tuple[bytes, str]:
        """Synthesize text into MP3 audio via Speaches Kokoro.

        Returns (audio_bytes, content_type).
        """
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("empty text")
        if len(clean_text) > 5000:
            raise ValueError("text exceeds maximum length of 5000 characters")

        chosen_voice = resolve_kokoro_voice(lang, voice)
        speed = max(0.5, min(2.0, speed))

        cache_key = hashlib.sha256(
            f"{self.model}:{chosen_voice}:{speed:.2f}:{clean_text}".encode("utf-8")
        ).hexdigest()

        with self._cache_lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                return self._cache[cache_key], "audio/mp3"

        speech_url = f"{self.url}/audio/speech"
        payload = {
            "model": self.model,
            "input": clean_text,
            "voice": chosen_voice,
            "speed": speed,
            "response_format": "mp3",
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            speech_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "BookTranslatorHub-TTS"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                audio_bytes = resp.read()
                content_type = resp.headers.get("Content-Type", "audio/mp3").split(";")[0].strip()
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            log.error("Speaches TTS HTTP %d: %s", err.code, err_body)
            raise RuntimeError(f"Speaches TTS HTTP {err.code}: {err_body}") from err
        except Exception as exc:
            log.error("Speaches TTS request failed: %s", exc)
            raise RuntimeError(f"Speaches TTS error: {exc}") from exc

        # Cache in LRU (thread-safe: avoid premature eviction if key exists)
        with self._cache_lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
            else:
                if len(self._cache) >= self.cache_capacity:
                    self._cache.popitem(last=False)
            self._cache[cache_key] = audio_bytes

        return audio_bytes, content_type or "audio/mp3"


TTS_SERVICE = TtsService.from_env()

