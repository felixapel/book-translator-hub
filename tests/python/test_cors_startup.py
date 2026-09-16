"""CORS startup contracts run in fresh processes, not mutated module globals."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CorsStartupTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith(("BT_", "CWA_", "KAVITA_", "LLM_", "DB_PATH")):
                env.pop(name)
        env.update(
            {
                "BT_AUTH_MODE": "cwa_session",
                "BT_CWA_AUTH_URL": "http://calibre-web:8083/ajax/emailstat",
                "BT_PUBLIC_ORIGIN": "https://books.example.test",
                "LLM_PROVIDER": "local",
                "LLM_MODEL": "cors-startup-test-model",
                "BT_LOCAL_URL": "http://127.0.0.1:1234/v1/chat/completions",
            }
        )
        env.update(overrides)
        return env

    def _import_server(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", "import server; print(sorted(server.ALLOWED_ORIGINS))"],
            cwd=ROOT,
            env=self._environment(**overrides),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_origins_start_and_do_not_inherit_reader_upstreams(self):
        result = self._import_server(
            BT_ALLOWED_ORIGINS="https://books.example.test,https://admin.example.test:8443",
            CWA_UPSTREAM="http://reader-private:8083",
            BT_ALLOW_PRIVATE_LAN="true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://books.example.test", result.stdout)
        self.assertIn("https://admin.example.test:8443", result.stdout)
        self.assertNotIn("reader-private", result.stdout)

    def test_invalid_or_wildcard_origins_fail_startup(self):
        for value in (
            "*",
            "https://books.example.test/path",
            "https://books.example.test, https://admin.example.test",
            "https://books.example.test,,https://admin.example.test",
        ):
            with self.subTest(value=value):
                result = self._import_server(BT_ALLOWED_ORIGINS=value)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BT_ALLOWED_ORIGINS", result.stderr)

    def test_cookie_auth_mode_requires_an_exact_public_origin_for_writes(self):
        for value in ("", "https://books.example.test/path", "*"):
            with self.subTest(value=value):
                result = self._import_server(BT_PUBLIC_ORIGIN=value)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BT_PUBLIC_ORIGIN", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
