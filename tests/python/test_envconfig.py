"""Numeric settings fail startup with the variable name, never silently."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from envconfig import env_float, env_int


ROOT = Path(__file__).resolve().parents[2]


class EnvConfigTests(unittest.TestCase):
    def test_parses_defaults_and_values(self):
        with mock.patch.dict(os.environ, {"BT_X": " 7 "}, clear=False):
            self.assertEqual(env_int("BT_X", 3), 7)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env_int("BT_X", 3), 3)
            self.assertEqual(env_int("BT_X", 0, minimum=0), 0)
            self.assertEqual(env_float("BT_X", 2.5), 2.5)

    def test_rejects_malformed_and_out_of_range_values(self):
        cases = [
            (env_int, "abc", {}),
            (env_int, "1.5", {}),
            (env_int, "0", {}),
            (env_int, "-1", {"minimum": 0}),
            (env_float, "nan", {}),
            (env_float, "inf", {}),
            (env_float, "0", {}),
            (env_float, "", {}),
        ]
        for parse, raw, options in cases:
            with self.subTest(parse=parse.__name__, raw=raw):
                with mock.patch.dict(os.environ, {"BT_X": raw}):
                    with self.assertRaisesRegex(ValueError, "^BT_X must be"):
                        parse("BT_X", 1, **options)

    def test_server_startup_names_the_bad_setting(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BT_", "LLM_", "DB_PATH"))
        }
        env.update({
            "BT_AUTH_MODE": "disabled",
            "BT_ALLOW_INSECURE_AUTH": "true",
            "LLM_PROVIDER": "local",
            "LLM_MODEL": "envconfig-test-model",
            "BT_LOCAL_URL": "http://127.0.0.1:1234/v1/chat/completions",
            "BT_MAX_CONCURRENT": "two",
        })
        result = subprocess.run(
            [sys.executable, "-c", "import server"],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BT_MAX_CONCURRENT must be an integer", result.stderr)
