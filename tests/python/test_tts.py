"""Static contract for the reader-overlay text-to-speech feature.

Covers the browser speechSynthesis queue over translated paragraphs, the
stop/pause bar controls and the per-target-language voice selection.

Manual browser checklist (Web Speech API needs a real browser; jsdom only
mocks it, so verify these by hand after deploy):
1. Open a book, translate a page, press Listen (the bar PLAY button):
   translated paragraphs are read aloud in reading order.
2. While speaking, press the button again: speech pauses and the button
   offers resume; a third press resumes.
3. Press the bar STOP button: speech stops immediately and Stop disables.
4. Switch target language mid-speech: queued speech stops (stale voice).
5. Turn a page mid-speech: queued speech stops (stale chapter).
6. Switch the target language and listen again: the voice accent follows
   the new target language.
7. In a browser without speechSynthesis the Listen/Stop buttons are hidden.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRANSLATOR_JS = ROOT / "static" / "translator.js"
TRANSLATOR_CSS = ROOT / "static" / "translator.css"

TOP_LANGUAGE_VOICES = {
    "English": "en-US",
    "Chinese": "zh-CN",
    "Hindi": "hi-IN",
    "Spanish": "es-ES",
    "French": "fr-FR",
    "Arabic": "ar-SA",
    "Bengali": "bn-BD",
    "Portuguese": "pt-PT",
    "Russian": "ru-RU",
    "Urdu": "ur-PK",
}


class TtsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = TRANSLATOR_JS.read_text(encoding="utf-8")
        cls.css = TRANSLATOR_CSS.read_text(encoding="utf-8")

    def test_speech_queue_and_transport_controls(self):
        self.assertIn("speechSynthesis", self.js)
        self.assertIn("SpeechSynthesisUtterance", self.js)
        self.assertRegex(self.js, r"\.speak\(utterance\)")
        self.assertRegex(self.js, r"\.cancel\(\)")
        self.assertRegex(self.js, r"speechSynthesis\.pause\(\)")
        self.assertRegex(self.js, r"speechSynthesis\.resume\(\)")

    def test_voice_per_target_language(self):
        self.assertRegex(self.js, r"const BT_TTS_LANG_CODES = \{")
        for language, locale in TOP_LANGUAGE_VOICES.items():
            self.assertIn(
                f"'{language}': '{locale}'",
                self.js,
                f"missing voice locale for {language}",
            )
        self.assertRegex(self.js, r"utterance\.lang = code")
        self.assertRegex(self.js, r"utterance\.voice = voice")
        self.assertRegex(self.js, r"function ttsPickVoice\(langCode\)")

    def test_stop_pause_bar_ui(self):
        self.assertIn('id="bt-speak"', self.js)
        self.assertIn('id="bt-stop"', self.js)
        self.assertIn("aria-pressed", self.js)
        self.assertIn("#bt-speak", self.css)
        self.assertIn("#bt-stop", self.css)

    def test_stale_speech_is_cancelled(self):
        self.assertRegex(
            self.js, r"function newGeneration\(\) \{[\s\S]{0,300}?ttsStop\(\)"
        )
        self.assertIn("addEventListener('pagehide', ttsStop)", self.js)

    def test_no_innerhtml_sink_in_tts_markup(self):
        tts_block = self.js[
            self.js.index("── Text-to-Speech"): self.js.index("── DOM Helpers")
        ]
        self.assertNotIn("innerHTML", tts_block)


if __name__ == "__main__":
    unittest.main()
