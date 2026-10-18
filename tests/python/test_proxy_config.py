"""Fail-closed rendering contracts for the nginx injection proxy."""

from __future__ import annotations

import contextlib
import io
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "proxy" / "render_config.py"
TEMPLATE = ROOT / "proxy" / "nginx.conf.template"


class ProxyConfigRendererTests(unittest.TestCase):
    def render(self, overrides: dict[str, str | None] | None = None, *, existing=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "proxy.conf"
        browser_output = Path(temporary.name) / "browser-config.json"
        if existing:
            output.write_text("retained nginx configuration", encoding="utf-8")
            browser_output.write_text("retained browser configuration", encoding="utf-8")
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

    def test_empty_edge_trust_keeps_socket_peer_forwarding(self):
        for value in (None, ""):
            with self.subTest(value=value):
                result, output, _ = self.render({"BT_EDGE_TRUSTED_PROXIES": value})
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered = output.read_text()
                self.assertNotIn("set_real_ip_from", rendered)
                self.assertNotIn("real_ip_header", rendered)
                self.assertEqual(
                    rendered.count("proxy_set_header X-Forwarded-For $remote_addr;"),
                    3,
                )

    def test_exact_edge_peers_are_canonicalized_without_changing_api_authority(self):
        result, output, _ = self.render({
            "BT_EDGE_TRUSTED_PROXIES":
                "198.51.100.2/32, 192.0.2.8/32,192.0.2.8",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = output.read_text()
        self.assertIn("set_real_ip_from 192.0.2.8;", rendered)
        self.assertIn("set_real_ip_from 198.51.100.2;", rendered)
        self.assertEqual(rendered.count("set_real_ip_from"), 2)
        self.assertIn("real_ip_header X-Forwarded-For;", rendered)
        self.assertIn("real_ip_recursive off;", rendered)
        self.assertEqual(rendered.count("proxy_set_header X-Real-IP $remote_addr;"), 3)
        self.assertEqual(rendered.count("proxy_set_header User-Agent $http_user_agent;"), 3)
        self.assertEqual(rendered.count('proxy_set_header Forwarded "";'), 3)

    def test_edge_trust_rejects_ambiguous_or_unbounded_authority_before_writes(self):
        invalid = (
            "0.0.0.0/0", "192.0.2.0/24", "2001:db8::/64", "*",
            "edge.example.test", "unix:/tmp/edge.sock", "fe80::1%eth0",
            "192.0.2.1,", ",192.0.2.1", "192.0.2.1,,192.0.2.2",
            "192.0.2.1\ninclude /tmp/evil;", "192.0.2.1\t", "192.0.2.1\x7f",
            "192.0.2.1; include /tmp/evil;", "192.0.2.1:8080", " 192.0.2.1",
            # Never an immediate TCP peer of the IPv4-only listener.
            "0.0.0.0", "0.0.0.0/32", "::", "224.0.0.1", "ff02::1",
            "::ffff:192.0.2.1", "2001:db8::8", "2001:db8::8/128", "::1",
            ",".join(f"192.0.2.{i}" for i in range(1, 34)),
            "192.0.2.1," * 500,
        )
        for value in invalid:
            with self.subTest(value=value):
                result, output, browser_output = self.render({
                    "BT_EDGE_TRUSTED_PROXIES": value,
                })
                self.assertEqual(result.returncode, 78)
                self.assertIn("BT_EDGE_TRUSTED_PROXIES", result.stderr)
                self.assertNotIn(value, result.stderr)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())

    def test_whitespace_edge_trust_is_not_the_empty_default(self):
        result, output, browser_output = self.render({"BT_EDGE_TRUSTED_PROXIES": " "})
        self.assertEqual(result.returncode, 78)
        self.assertFalse(output.exists())
        self.assertFalse(browser_output.exists())

    def test_forwarded_identity_is_stripped_on_all_three_upstream_routes(self):
        result, output, _ = self.render({"BT_CWA_IDENTITY_HEADER": "X-Reader-Login"})
        self.assertEqual(result.returncode, 0, result.stderr)
        for header in (
            "X-Reader-Login", "X-BT-Subject", "X-BT-Roles",
            "X-authentik-uid", "X-authentik-groups",
        ):
            self.assertEqual(output.read_text().count(f'proxy_set_header {header} "";'), 3)

    def test_identity_header_cannot_collide_with_managed_context_or_credentials(self):
        for header in (
            "User-Agent", "uSeR-aGeNt", "Host", "Cookie", "Authorization",
            "X-Forwarded-For", "X-Real-IP", "X-BT-Subject", "Connection",
        ):
            with self.subTest(header=header):
                result, output, browser_output = self.render({"BT_CWA_IDENTITY_HEADER": header})
                self.assertEqual(result.returncode, 78)
                self.assertIn("BT_CWA_IDENTITY_HEADER", result.stderr)
                self.assertFalse(output.exists())
                self.assertFalse(browser_output.exists())

    def test_maximum_exact_edge_set_accepts_duplicate_host_forms(self):
        peers = [f"192.0.2.{index}" for index in range(1, 33)]
        result, output, _ = self.render({
            "BT_EDGE_TRUSTED_PROXIES": ",".join(peers + [peers[0] + "/32"]),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text().count("set_real_ip_from"), 32)

    def test_invalid_security_settings_preserve_existing_outputs(self):
        for overrides in (
            {"BT_EDGE_TRUSTED_PROXIES": "192.0.2.0/24"},
            {"BT_CWA_IDENTITY_HEADER": "User-Agent"},
        ):
            with self.subTest(overrides=overrides):
                result, output, browser_output = self.render(overrides, existing=True)
                self.assertEqual(result.returncode, 78)
                self.assertEqual(output.read_text(), "retained nginx configuration")
                self.assertEqual(browser_output.read_text(), "retained browser configuration")

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
        # The reader upstream is re-resolved through the container's own DNS
        # so a recreated reader container (new IP) never leaves nginx on a
        # stale address. The exact servers come from /etc/resolv.conf.
        self.assertRegex(rendered, r"\n        resolver \S+(?: \S+)* valid=10s ipv6=off;\n")
        self.assertNotIn("resolver 127.0.0.11 valid", TEMPLATE.read_text())
        self.assertIn("set $bt_reader_upstream http://calibre-web:8083;", rendered)
        self.assertIn("proxy_pass $bt_reader_upstream;", rendered)
        self.assertNotIn("proxy_pass http://calibre-web:8083;", rendered)
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
        self.assertIn("set $bt_reader_upstream http://kavita:5000;", rendered)
        self.assertIn("proxy_pass $bt_reader_upstream;", rendered)
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


class ReaderUpstreamRoutingTests(unittest.TestCase):
    """nginx's runtime resolver ignores /etc/hosts and needs a reachable DNS."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        from proxy import render_config

        self.render_config = render_config
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def routing(self, upstream, *, resolv="nameserver 127.0.0.11\n", hosts=""):
        resolv_conf = self.directory / "resolv.conf"
        hosts_file = self.directory / "hosts"
        resolv_conf.write_text(resolv)
        hosts_file.write_text(hosts)
        return self.render_config._reader_upstream_routing(
            upstream, "_cwa", resolv_conf=resolv_conf, hosts_file=hosts_file
        )

    def test_container_names_re_resolve_through_the_configured_dns(self):
        self.assertEqual(
            self.routing("http://calibre-web:8083"),
            "resolver 127.0.0.11 valid=10s ipv6=off;\n"
            "        set $bt_cwa_reader_upstream http://calibre-web:8083;\n"
            "        proxy_pass $bt_cwa_reader_upstream;",
        )

    def test_default_bridge_uses_the_host_nameservers(self):
        routing = self.routing(
            "https://books.example.test",
            resolv=(
                "# generated by Docker\nsearch lan\n"
                "nameserver 192.168.0.1 ; router\n"
                "nameserver 2001:db8::53\n"
                "nameserver fe80::1%eth0\n"
                "nameserver not-an-address\n"
                "options ndots:0\n"
            ),
        )
        self.assertTrue(
            routing.startswith("resolver 192.168.0.1 [2001:db8::53] valid=10s")
        )

    def test_hosts_file_names_and_literals_keep_a_static_proxy_pass(self):
        hosts = (
            "127.0.0.1\tlocalhost\n"
            "172.17.0.1 host.docker.internal # extra_hosts\n"
            "# 10.0.0.9 commented.example\n"
        )
        for upstream in (
            "http://host.docker.internal:8083",
            "http://HOST.docker.internal:8083",
            "http://172.17.0.1:8083",
            "http://[2001:db8::7]:8083",
        ):
            with self.subTest(upstream=upstream):
                self.assertEqual(
                    self.routing(upstream, hosts=hosts),
                    f"proxy_pass {upstream};",
                )
        self.assertTrue(
            self.routing("http://commented.example", hosts=hosts).startswith(
                "resolver "
            )
        )

    def test_missing_or_unusable_dns_falls_back_to_startup_resolution(self):
        self.assertEqual(
            self.routing("http://kavita:5000", resolv="search lan\n"),
            "proxy_pass http://kavita:5000;",
        )
        resolv_conf = self.directory / "absent-resolv.conf"
        self.assertEqual(
            self.render_config._reader_upstream_routing(
                "http://kavita:5000",
                "_kavita",
                resolv_conf=resolv_conf,
                hosts_file=self.directory / "absent-hosts",
            ),
            "proxy_pass http://kavita:5000;",
        )


class EdgeGatewayTrustTests(unittest.TestCase):
    """Trusting the network gateway trusts every connection Docker relays."""

    # /proc/net/route stores addresses as little-endian hex: 172.25.0.1.
    ROUTES = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010019AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000019AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        from proxy import render_config

        self.render_config = render_config
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.routes = self.directory / "route"
        self.routes.write_text(self.ROUTES)

    def test_trusting_the_default_gateway_is_reported_without_the_value(self):
        warning = self.render_config._edge_gateway_warning(
            ("192.0.2.8", "172.25.0.1"), route_file=self.routes
        )
        self.assertIn("BT_EDGE_TRUSTED_PROXIES", warning)
        self.assertIn("gateway", warning)
        self.assertNotIn("172.25.0.1", warning)

    def test_other_peers_and_unreadable_routes_stay_quiet(self):
        self.assertIsNone(self.render_config._edge_gateway_warning(
            ("172.25.0.5", "2001:db8::1"), route_file=self.routes
        ))
        self.assertIsNone(self.render_config._edge_gateway_warning(
            ("172.25.0.1",), route_file=self.directory / "missing"
        ))

    def test_route_table_without_a_default_route_has_no_gateway(self):
        header_only = self.directory / "header-only"
        header_only.write_text(self.ROUTES.splitlines(keepends=True)[0])
        self.assertEqual(self.render_config._default_gateways(header_only), set())

    def test_render_warns_but_still_writes_the_configuration(self):
        output = self.directory / "proxy.conf"
        env = {
            "CWA_UPSTREAM": "http://127.0.0.1:8083",
            "BT_API_UPSTREAM": "http://127.0.0.1:8390",
            "BT_PROXY_PORT": "8080",
            "BT_UI_VERSION": "2.4.2",
            "BT_PUBLIC_ORIGIN": "https://books.example.test",
            "BT_CWA_MAX_BODY_SIZE": "2g",
            "BT_CWA_IDENTITY_HEADER": "Remote-User",
            "BT_BROWSER_AUTH_MODE": "cwa_session",
            "BT_BROWSER_CREDENTIALS": "same-origin",
            "BT_EDGE_TRUSTED_PROXIES": "172.25.0.1",
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(self.render_config, "ROUTE_FILE", self.routes),
            contextlib.redirect_stderr(stderr),
        ):
            self.render_config.render(TEMPLATE, output, env)
        self.assertIn("set_real_ip_from 172.25.0.1;", output.read_text())
        self.assertIn("[proxy-config] WARNING:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
