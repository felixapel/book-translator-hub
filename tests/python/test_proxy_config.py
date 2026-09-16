"""Fail-closed rendering contracts for the nginx injection proxy."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "proxy" / "render_config.py"
TEMPLATE = ROOT / "proxy" / "nginx.conf.template"


class ProxyConfigRendererTests(unittest.TestCase):
    def render(self, overrides: dict[str, str | None] | None = None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "proxy.conf"
        browser_output = Path(temporary.name) / "browser-config.json"
        env = os.environ.copy()
        env.update({
            "CWA_UPSTREAM": "http://calibre-web:8083",
            "BT_API_UPSTREAM": "http://book-translator-api:8390",
            "BT_PROXY_PORT": "8080",
            "BT_UI_VERSION": "2.1.4",
            "BT_PUBLIC_ORIGIN": "https://books.example.test:8443",
            "BT_CWA_MAX_BODY_SIZE": "2g",
            "BT_CWA_IDENTITY_HEADER": "Remote-User",
            "BT_BROWSER_AUTH_MODE": "cwa_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
            "BT_BATCH_SIZE": "5",
            "BT_CLIENT_PREFETCH_GAP_MS": "0",
        })
        for name, value in (overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        result = subprocess.run(
            [
                sys.executable,
                str(RENDERER),
                str(TEMPLATE),
                str(output),
                str(browser_output),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return result, output, browser_output

    def test_valid_contract_renders_only_validated_values(self):
        result, output, browser_output = self.render()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = output.read_text()
        self.assertEqual(rendered.count("proxy_set_header Host books.example.test:8443;"), 3)
        self.assertEqual(rendered.count("proxy_set_header X-Forwarded-Proto https;"), 3)
        self.assertEqual(rendered.count("proxy_set_header X-Forwarded-For $remote_addr;"), 3)
        self.assertEqual(rendered.count("proxy_set_header User-Agent $http_user_agent;"), 3)
        self.assertNotIn("$proxy_add_x_forwarded_for", rendered)
        self.assertNotIn("$http_x_forwarded_for", rendered)
        self.assertIn("client_max_body_size 2g;", rendered)
        self.assertIn("absolute_redirect off;", rendered)
        self.assertIn("listen 8080;", rendered)
        self.assertIn("proxy_pass http://calibre-web:8083;", rendered)
        self.assertIn("proxy_pass http://book-translator-api:8390/;", rendered)
        self.assertEqual(rendered.count("proxy_set_header Cookie $http_cookie;"), 1)
        self.assertIn(
            "proxy_set_header Cookie $bt_session_route_cookie;", rendered
        )
        self.assertNotIn("proxy_set_header Cookie $bt_session_cookie;", rendered)
        self.assertIn('proxy_set_header Remote-User "";', rendered)
        self.assertNotIn("$http_x_forwarded_proto", rendered)
        self.assertNotRegex(rendered, r"\$\{(?:BT_|CWA_)")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(browser_output.read_text()),
            {
                "apiUrl": "/bt-api",
                "authMode": "cwa_session",
                "credentials": "same-origin",
                "batchSize": 5,
                "prefetchGapMs": 0,
            },
        )
        self.assertEqual(browser_output.stat().st_mode & 0o777, 0o600)
        self.assertIn("location = /bt-config.json", rendered)
        self.assertIn('add_header Cache-Control "no-store" always;', rendered)
        for header in (
            "X-BT-Subject",
            "X-BT-Roles",
            "X-authentik-uid",
            "X-authentik-groups",
        ):
            self.assertIn(f'proxy_set_header {header} "";', rendered)

    def test_forwarded_browser_contract_sends_cookie_only_to_identity_edge(self):
        result, output, browser_output = self.render({
            "BT_BROWSER_AUTH_MODE": "forwarded",
            "BT_BROWSER_CREDENTIALS": "include",
        })

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = output.read_text(encoding="utf-8")
        self.assertNotIn("proxy_set_header Cookie $http_cookie;", rendered)
        self.assertIn("POST $http_cookie;", rendered)
        self.assertNotIn("default $http_cookie;", rendered)
        self.assertIn(
            "proxy_set_header Cookie $bt_session_route_cookie;", rendered
        )
        self.assertIn('proxy_set_header Cookie "";', rendered)
        self.assertEqual(
            json.loads(browser_output.read_text()),
            {
                "apiUrl": "/bt-api",
                "authMode": "forwarded",
                "credentials": "include",
                "batchSize": 5,
                "prefetchGapMs": 0,
            },
        )

    def test_reader_session_proxy_contains_raw_credentials_to_exchange_only(self):
        result, output, browser_output = self.render({
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "CWA_UPSTREAM": None,
            "BT_CWA_IDENTITY_HEADER": "Remote-User",
            "BT_READER_VERSION": "0.9.0.2",
            "BT_READER_CONTRACT_VERSION": "kavita-0.9.0.2-epub-v1",
            "BT_BROWSER_AUTH_MODE": "reader_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
        })

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = output.read_text(encoding="utf-8")
        self.assertIn("proxy_pass http://kavita:5000;", rendered)
        self.assertIn('default "";', rendered)
        self.assertIn("POST $http_cookie;", rendered)
        self.assertNotIn("default $http_cookie;", rendered)
        self.assertIn("DELETE $bt_session_cookie;", rendered)
        self.assertIn("POST $http_authorization;", rendered)
        self.assertNotIn("default $http_authorization;", rendered)
        self.assertIn(
            "proxy_set_header Cookie $bt_session_route_cookie;", rendered
        )
        self.assertIn(
            "proxy_set_header Authorization $bt_session_route_authorization;",
            rendered,
        )
        self.assertIn("proxy_set_header Cookie $bt_session_cookie;", rendered)
        self.assertIn('proxy_set_header Authorization "";', rendered)
        self.assertEqual(
            json.loads(browser_output.read_text()),
            {
                "apiUrl": "/bt-api",
                "authMode": "reader_session",
                "credentials": "same-origin",
                "readerType": "kavita",
                "readerVersion": "0.9.0.2",
                "readerContractVersion": "kavita-0.9.0.2-epub-v1",
                "batchSize": 5,
                "prefetchGapMs": 0,
            },
        )

    def test_kavita_0_9_1_4_requires_and_renders_its_exact_contract(self):
        result, _, browser_output = self.render({
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "CWA_UPSTREAM": None,
            "BT_READER_VERSION": "0.9.1.4",
            "BT_READER_CONTRACT_VERSION": "kavita-0.9.1.4-epub-v1",
            "BT_BROWSER_AUTH_MODE": "reader_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(browser_output.read_text())
        self.assertEqual(config["readerVersion"], "0.9.1.4")
        self.assertEqual(config["readerContractVersion"], "kavita-0.9.1.4-epub-v1")

        rejected, output, config_output = self.render({
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "CWA_UPSTREAM": None,
            "BT_READER_VERSION": "0.9.1.4",
            "BT_READER_CONTRACT_VERSION": "kavita-0.9.0.2-epub-v1",
            "BT_BROWSER_AUTH_MODE": "reader_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
        })
        self.assertEqual(rejected.returncode, 78)
        self.assertFalse(output.exists())
        self.assertFalse(config_output.exists())

    def test_browser_batch_controls_are_bounded_and_render_as_numbers(self):
        result, _, browser_output = self.render({
            "BT_BATCH_SIZE": "10",
            "BT_CLIENT_PREFETCH_GAP_MS": "1000",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(browser_output.read_text())
        self.assertEqual(config["batchSize"], 10)
        self.assertEqual(config["prefetchGapMs"], 1000)

        for name, value in (
            ("BT_BATCH_SIZE", "0"),
            ("BT_BATCH_SIZE", "51"),
            ("BT_BATCH_SIZE", "1.5"),
            ("BT_CLIENT_PREFETCH_GAP_MS", "-1"),
            ("BT_CLIENT_PREFETCH_GAP_MS", "10001"),
            ("BT_CLIENT_PREFETCH_GAP_MS", "1.5"),
        ):
            with self.subTest(name=name, value=value):
                failed, output, config_output = self.render({name: value})
                self.assertEqual(failed.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(config_output.exists())

    def test_reader_specific_session_cookie_is_rendered_without_cross_reader_alias(self):
        result, output, _ = self.render({
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "CWA_UPSTREAM": None,
            "BT_READER_VERSION": "0.9.0.2",
            "BT_READER_CONTRACT_VERSION": "kavita-0.9.0.2-epub-v1",
            "BT_BROWSER_AUTH_MODE": "reader_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
            "BT_SESSION_COOKIE_NAME": "__Host-bt-kavita-session",
            "BT_PROXY_NAMESPACE": "kavita",
            "BT_BROWSER_CONFIG_PATH": "/tmp/nginx/browser-config-kavita.json",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = output.read_text(encoding="utf-8")
        self.assertIn("__Host-bt-kavita-session=", rendered)
        self.assertNotIn("__Host-bt-session=", rendered)
        self.assertIn("$bt_kavita_session_cookie", rendered)
        self.assertIn(
            "proxy_set_header Cookie $bt_kavita_session_cookie;", rendered
        )
        self.assertNotIn("proxy_set_header Cookie $bt_session_cookie;", rendered)
        self.assertIn(
            "alias /tmp/nginx/browser-config-kavita.json;", rendered
        )

    def test_proxy_namespace_and_browser_config_path_reject_nginx_injection(self):
        for name, value in (
            ("BT_PROXY_NAMESPACE", "kavita; include /tmp/evil"),
            ("BT_BROWSER_CONFIG_PATH", "/etc/passwd"),
            ("BT_BROWSER_CONFIG_PATH", "/tmp/nginx/config.json; include /tmp/evil"),
        ):
            with self.subTest(name=name):
                result, output, browser_output = self.render({name: value})
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())
                self.assertIn(name, result.stderr)

    def test_reader_session_requires_exact_reader_contract(self):
        base = {
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "CWA_UPSTREAM": None,
            "BT_CWA_IDENTITY_HEADER": "Remote-User",
            "BT_READER_VERSION": "0.9.0.2",
            "BT_READER_CONTRACT_VERSION": "kavita-0.9.0.2-epub-v1",
            "BT_BROWSER_AUTH_MODE": "reader_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
        }
        for name, value in (
            ("BT_READER_TYPE", "unknown"),
            ("BT_READER_VERSION", "0.9.0.1"),
            ("BT_READER_CONTRACT_VERSION", "kavita-latest"),
        ):
            with self.subTest(name=name):
                result, output, browser_output = self.render(
                    {**base, name: value}
                )
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())

    def test_browser_auth_and_credentials_must_be_a_supported_pair(self):
        for auth_mode, credentials in (
            ("forwarded", "omit"),
            ("forwarded", "same-origin"),
            ("cwa_session", "include"),
            ("token", "omit"),
        ):
            with self.subTest(auth_mode=auth_mode, credentials=credentials):
                result, output, browser_output = self.render({
                    "BT_BROWSER_AUTH_MODE": auth_mode,
                    "BT_BROWSER_CREDENTIALS": credentials,
                })
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())
                self.assertIn("BT_BROWSER", result.stderr)

    def test_public_origin_is_required_and_must_be_an_exact_http_origin(self):
        for value in (
            None,
            "",
            "books.example.test",
            "file://books.example.test",
            "https://user:pass@books.example.test",
            "https://books.example.test/path",
            "https://books.example.test?query=1",
            "https://books.example.test#fragment",
            "https://books.example.test\nserver { listen 9000; }",
        ):
            with self.subTest(value=value):
                result, output, browser_output = self.render({"BT_PUBLIC_ORIGIN": value})
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())
                self.assertIn("BT_PUBLIC_ORIGIN", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_upstream_urls_reject_credentials_paths_and_non_http_schemes(self):
        cases = (
            ("CWA_UPSTREAM", "file:///etc/passwd"),
            ("CWA_UPSTREAM", "http://calibre-web:8083/admin"),
            ("CWA_UPSTREAM", "http://user:secret@calibre-web:8083"),
            ("BT_API_UPSTREAM", "http://api:8390/translate"),
            ("BT_API_UPSTREAM", "http://api:bad"),
            ("BT_API_UPSTREAM", "http://api:8390\ninclude /tmp/evil.conf"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                result, output, browser_output = self.render({name: value})
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())
                self.assertIn(name, result.stderr)
                self.assertNotIn("secret", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_port_size_and_ui_version_are_bounded_tokens(self):
        cases = (
            ("BT_PROXY_PORT", "0"),
            ("BT_PROXY_PORT", "65536"),
            ("BT_PROXY_PORT", "8080; include /tmp/evil"),
            ("BT_CWA_MAX_BODY_SIZE", "0"),
            ("BT_CWA_MAX_BODY_SIZE", "unlimited"),
            ("BT_CWA_MAX_BODY_SIZE", "2g; include /tmp/evil"),
            ("BT_UI_VERSION", "../../secret"),
            ("BT_UI_VERSION", "v1\nscript"),
            ("BT_CWA_IDENTITY_HEADER", "Remote_User"),
            ("BT_CWA_IDENTITY_HEADER", "Remote-User; include"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                result, output, browser_output = self.render({name: value})
                self.assertEqual(result.returncode, 78)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())
                self.assertIn(name, result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
