"""Reader-session broker contracts for CWA and pinned Kavita."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from auth import CwaSessionBinding, RequestAuthenticator
from reader_session import (
    BrokerConfigError,
    BrokerRejected,
    BrokerUnavailable,
    ReaderSessionBroker,
    load_or_create_session_key,
)

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")


class FakeResponse:
    def __init__(self, body, *, status=200, content_type="application/json"):
        self.status_code = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Set-Cookie": "must-not-be-forwarded=1",
        }
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class ReaderSessionBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.tokens = iter(("A" * 43, "B" * 43, "C" * 43))

    def transport(self, body, *, status=200, content_type="application/json"):
        def get(url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse(body, status=status, content_type=content_type)

        return get

    def broker(
        self,
        *,
        reader_type="kavita",
        reader_version="0.9.0.2",
        transport=None,
        cookie_name=None,
    ):
        return ReaderSessionBroker(
            reader_type=reader_type,
            auth_url=(
                "http://kavita:5000/api/Account"
                if reader_type == "kavita"
                else "http://calibre-web:8083/ajax/emailstat"
            ),
            reader_version=reader_version if reader_type == "kavita" else "4.0.6",
            connector_id="01234567-89ab-4cde-8123-0123456789ab",
            public_origin="https://books.example.test",
            cookie_name=cookie_name,
            secret_key=b"s" * 32,
            http_get=transport
            or self.transport(json.dumps({"id": 7, "kavitaVersion": reader_version}).encode()),
            clock=self.clock,
            token_factory=lambda: next(self.tokens),
        )

    def test_reader_specific_cookie_name_is_used_for_issue_and_authentication(self):
        broker = self.broker(cookie_name="__Host-bt-kavita-session")

        issue = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
            },
            self.binding(),
        )

        self.assertIn("__Host-bt-kavita-session=", issue.set_cookie)
        self.assertEqual(
            broker.authenticate(
                {"Cookie": f"__Host-bt-kavita-session={issue.token}"},
                self.binding(),
            ),
            issue.subject,
        )
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={issue.token}"}, self.binding()
            )

    def test_secure_reader_cookie_name_must_keep_host_prefix(self):
        with self.assertRaisesRegex(BrokerConfigError, "BT_SESSION_COOKIE_NAME"):
            self.broker(cookie_name="bt-kavita-session")

    @staticmethod
    def binding():
        return CwaSessionBinding("203.0.113.9", "Reader/1.0")

    def test_native_kavita_exchange_forwards_only_access_jwt(self):
        broker = self.broker()

        issue = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
                "Cookie": "__Host-bt-session=old-token",
            },
            self.binding(),
        )

        self.assertEqual(issue.token, "A" * 43)
        self.assertIn("__Host-bt-session=", issue.set_cookie)
        self.assertIn("Secure", issue.set_cookie)
        self.assertIn("HttpOnly", issue.set_cookie)
        self.assertIn("SameSite=Strict", issue.set_cookie)
        _, kwargs = self.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access-token")
        self.assertNotIn("Cookie", kwargs["headers"])
        self.assertNotIn("refresh", repr(self.calls).lower())
        self.assertFalse(kwargs["allow_redirects"])

    def test_native_kavita_0_9_1_4_bearer_exchange_uses_its_exact_contract(self):
        broker = self.broker(reader_version="0.9.1.4")

        issue = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
            },
            self.binding(),
        )

        self.assertEqual(issue.token, "A" * 43)
        self.assertEqual(
            self.calls[0][1]["headers"]["Authorization"], "Bearer access-token"
        )

    def test_oidc_chunks_are_exact_bounded_and_never_mixed_with_jwt(self):
        broker = self.broker()
        cookie = (
            ".AspNetCore.Cookies=chunks-2; "
            ".AspNetCore.CookiesC1=first; .AspNetCore.CookiesC2=second"
        )

        broker.exchange(
            {"Origin": "https://books.example.test", "Cookie": cookie},
            self.binding(),
        )

        self.assertEqual(self.calls[0][1]["headers"]["Cookie"], cookie)
        for invalid in (
            cookie + "; .AspNetCore.CookiesC3=extra",
            ".AspNetCore.Cookies=chunks-2; .AspNetCore.CookiesC1=missing-two",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(BrokerRejected):
                self.broker().exchange(
                    {"Origin": "https://books.example.test", "Cookie": invalid},
                    self.binding(),
                )
        with self.assertRaises(BrokerRejected):
            self.broker().exchange(
                {
                    "Origin": "https://books.example.test",
                    "Authorization": "Bearer access-token",
                    "Cookie": ".AspNetCore.Cookies=value",
                },
                self.binding(),
            )

    def test_kavita_exchange_discards_unrelated_ambient_cookies(self):
        single_oidc_broker = self.broker()
        single_oidc_broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Cookie": "theme=dark; .AspNetCore.Cookies=single; stale-plugin=value",
            },
            self.binding(),
        )
        self.assertEqual(
            self.calls[0][1]["headers"]["Cookie"],
            ".AspNetCore.Cookies=single",
        )

        chunked_oidc_broker = self.broker()
        chunked_oidc_broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Cookie": (
                    "theme=dark; .AspNetCore.Cookies=chunks-2; "
                    ".AspNetCore.CookiesC1=first; "
                    ".AspNetCore.CookiesC2=second; stale-plugin=value"
                ),
            },
            self.binding(),
        )

        self.assertEqual(
            self.calls[1][1]["headers"]["Cookie"],
            ".AspNetCore.Cookies=chunks-2; "
            ".AspNetCore.CookiesC1=first; .AspNetCore.CookiesC2=second",
        )

        native_broker = self.broker()
        native_broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
                "Cookie": "theme=dark; stale-plugin=value",
            },
            self.binding(),
        )

        self.assertEqual(
            self.calls[2][1]["headers"],
            {
                "Accept": "application/json",
                "Authorization": "Bearer access-token",
                "X-Forwarded-For": "203.0.113.9",
                "User-Agent": "Reader/1.0",
            },
        )

        calls_before_rejections = len(self.calls)
        for headers in (
            {
                "Origin": "https://books.example.test",
                "Cookie": "theme=dark; stale-plugin=value",
            },
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
                "Cookie": ".AspNetCore.CookiesC1=orphan; theme=dark",
            },
        ):
            with self.subTest(headers=headers), self.assertRaises(BrokerRejected):
                self.broker().exchange(headers, self.binding())
        self.assertEqual(len(self.calls), calls_before_rejections)

    def test_normal_requests_accept_only_bound_short_lived_plugin_cookie(self):
        broker = self.broker()
        issue = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
            },
            self.binding(),
        )

        subject = broker.authenticate(
            {"Cookie": f"__Host-bt-session={issue.token}"}, self.binding()
        )

        self.assertTrue(subject.startswith("reader-session:kavita:"))
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {
                    "Authorization": "Bearer leaked",
                    "Cookie": f"__Host-bt-session={issue.token}",
                },
                self.binding(),
            )
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={issue.token}; other=value"},
                self.binding(),
            )
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={issue.token}"},
                CwaSessionBinding("203.0.113.10", "Reader/1.0"),
            )
        self.clock.value += 301
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={issue.token}"}, self.binding()
            )

    def test_rotation_revokes_old_token_and_delete_is_idempotent(self):
        broker = self.broker()
        first = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
            },
            self.binding(),
        )
        second = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
                "Cookie": f"__Host-bt-session={first.token}",
            },
            self.binding(),
        )

        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={first.token}"}, self.binding()
            )
        broker.revoke(
            {"Cookie": f"__Host-bt-session={second.token}"}, self.binding()
        )
        broker.revoke({}, self.binding())
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={second.token}"}, self.binding()
            )

    def test_exact_origin_version_and_response_shape_fail_closed(self):
        for origin in ("", "null", "https://other.example.test"):
            with self.subTest(origin=origin), self.assertRaises(BrokerRejected):
                self.broker().exchange(
                    {"Origin": origin, "Authorization": "Bearer access-token"},
                    self.binding(),
                )
        cases = (
            (b'{"id":7,"kavitaVersion":"0.9.0.1"}', BrokerRejected),
            (b'{"id":0,"kavitaVersion":"0.9.0.2"}', BrokerRejected),
            (b'not-json', BrokerRejected),
        )
        for body, error in cases:
            with self.subTest(body=body), self.assertRaises(error):
                self.broker(transport=self.transport(body)).exchange(
                    {
                        "Origin": "https://books.example.test",
                        "Authorization": "Bearer access-token",
                    },
                    self.binding(),
                )
        with self.assertRaises(BrokerRejected):
            self.broker(
                transport=self.transport(b"{}", status=302)
            ).exchange(
                {
                    "Origin": "https://books.example.test",
                    "Authorization": "Bearer access-token",
                },
                self.binding(),
            )
        with self.assertRaises(BrokerUnavailable):
            self.broker(
                transport=self.transport(b"x" * 300_000)
            ).exchange(
                {
                    "Origin": "https://books.example.test",
                    "Authorization": "Bearer access-token",
                },
                self.binding(),
            )

    def test_cwa_exchange_forwards_only_allowlisted_cookies(self):
        broker = self.broker(
            reader_type="cwa", transport=self.transport(b"[]")
        )

        issue = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Cookie": "cf_clearance=cloudflare; remember_token=two; session=one",
            },
            self.binding(),
        )

        self.assertEqual(issue.token, "A" * 43)
        self.assertEqual(
            self.calls[0][1]["headers"]["Cookie"],
            "session=one; remember_token=two",
        )
        calls_before_rejections = len(self.calls)
        with self.assertRaises(BrokerRejected):
            broker.exchange(
                {
                    "Origin": "https://books.example.test",
                    "Cookie": "cf_clearance=cloudflare",
                },
                self.binding(),
            )
        with self.assertRaises(BrokerRejected):
            broker.exchange(
                {
                    "Origin": "https://books.example.test",
                    "Authorization": "Bearer leaked",
                    "Cookie": "session=one",
                },
                self.binding(),
            )
        with self.assertRaises(BrokerRejected):
            broker.exchange(
                {
                    "Origin": "https://books.example.test",
                    "Cookie": "__Host-bt-kavita-session=foreign",
                },
                self.binding(),
            )
        self.assertEqual(len(self.calls), calls_before_rejections)

        rotated = broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Cookie": (
                    f"__Host-bt-session={issue.token}; "
                    "__Host-bt-kavita-session=foreign; session=three"
                ),
            },
            self.binding(),
        )

        self.assertEqual(rotated.token, "B" * 43)
        self.assertEqual(self.calls[1][1]["headers"]["Cookie"], "session=three")
        with self.assertRaises(BrokerRejected):
            broker.authenticate(
                {"Cookie": f"__Host-bt-session={issue.token}"}, self.binding()
            )

    def test_configuration_enforces_https_except_loopback_development(self):
        common = dict(
            reader_type="kavita",
            auth_url="http://kavita:5000/api/Account",
            reader_version="0.9.0.2",
            connector_id="01234567-89ab-4cde-8123-0123456789ab",
            secret_key=b"s" * 32,
        )
        with self.assertRaises(BrokerConfigError):
            ReaderSessionBroker(
                **common, public_origin="http://books.example.test"
            )
        local = ReaderSessionBroker(
            **common, public_origin="http://127.0.0.1:8385"
        )
        self.assertEqual(local.cookie_name, "bt-session")

    def test_persistent_session_key_is_private_stable_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reader_session_key"

            first = load_or_create_session_key(path)
            second = load_or_create_session_key(path)

            self.assertEqual(first, second)
            self.assertEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
            with self.assertRaises(BrokerConfigError):
                load_or_create_session_key(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


class ReaderSessionEndpointTests(unittest.TestCase):
    def setUp(self):
        import server

        self.server = server
        self.original_authenticator = server.AUTHENTICATOR
        self.original_public_origin = server.BT_PUBLIC_ORIGIN
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"
        with server._rate_limit_lock:
            server._rate_limit_store.clear()
            server._auth_rate_limit_store.clear()
            server._auth_inflight_store.clear()

    def tearDown(self):
        self.server.AUTHENTICATOR = self.original_authenticator
        self.server.BT_PUBLIC_ORIGIN = self.original_public_origin

    def test_exchange_then_translation_uses_only_broker_identity(self):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(b'{"id":7,"kavitaVersion":"0.9.0.2"}')

        broker = ReaderSessionBroker(
            reader_type="kavita",
            auth_url="http://kavita:5000/api/Account",
            reader_version="0.9.0.2",
            connector_id="01234567-89ab-4cde-8123-0123456789ab",
            public_origin="https://books.example.test",
            secret_key=b"s" * 32,
            http_get=get,
            token_factory=lambda: "A" * 43,
        )
        self.server.AUTHENTICATOR = RequestAuthenticator(
            mode="reader_session", reader_session_broker=broker
        )
        client = self.server.app.test_client()
        client.environ_base["HTTP_ORIGIN"] = "https://books.example.test"

        exchange = client.post(
            "/session",
            headers={
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
                "User-Agent": "Reader/1.0",
            },
        )
        translated = client.post(
            "/translate",
            json={
                "text": "same",
                "source_lang": "English",
                "target_lang": "English",
                "provider_policy": {
                    **self.server.provider_policy(),
                    "generation": self.server.PROVIDER_POLICY_GENERATION,
                },
            },
            headers={"User-Agent": "Reader/1.0"},
        )

        self.assertEqual(exchange.status_code, 200, exchange.get_json())
        self.assertEqual(exchange.get_json()["expires_in"], 300)
        self.assertIn("__Host-bt-session=", exchange.headers["Set-Cookie"])
        self.assertEqual(translated.status_code, 200, translated.get_json())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer access-token")

    def test_exchange_rejects_body_and_wrong_origin_without_upstream_call(self):
        calls = []

        def get(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse(b'{}')

        broker = ReaderSessionBroker(
            reader_type="kavita",
            auth_url="http://kavita:5000/api/Account",
            reader_version="0.9.0.2",
            connector_id="01234567-89ab-4cde-8123-0123456789ab",
            public_origin="https://books.example.test",
            secret_key=b"s" * 32,
            http_get=get,
        )
        self.server.AUTHENTICATOR = RequestAuthenticator(
            mode="reader_session", reader_session_broker=broker
        )
        client = self.server.app.test_client()

        body = client.post(
            "/session",
            data=b"{}",
            headers={
                "Origin": "https://books.example.test",
                "Authorization": "Bearer access-token",
            },
        )
        wrong_origin = client.post(
            "/session",
            headers={
                "Origin": "https://evil.example.test",
                "Authorization": "Bearer access-token",
            },
        )

        self.assertEqual(body.status_code, 400)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
