"""Reader-session broker contracts for CWA and pinned Kavita."""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import unittest
import zlib
from pathlib import Path

from auth import CwaSessionBinding, RequestAuthenticator
from reader_session import (
    BrokerConfigError,
    BrokerRejected,
    BrokerUnavailable,
    ReaderSessionBroker,
    broker_from_environment,
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

    def test_cwa_exchange_handles_ambient_cookies_with_trailing_semicolons_and_whitespace(self):
        cwa_broker = self.broker(
            reader_type="cwa",
            transport=self.transport(b'[{"status": 1}]'),
        )
        cwa_broker.exchange(
            {
                "Origin": "https://books.example.test",
                "Cookie": (
                    "ambient_flag; empty=; theme=dark; "
                    "session=\"session-secret\"; "
                    "remember_token=remember; "
                ),
            },
            self.binding(),
        )
        self.assertEqual(
            self.calls[-1][1]["headers"]["Cookie"],
            "session=session-secret; remember_token=remember",
        )

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
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                path.chmod(0o644)
                with self.assertRaises(BrokerConfigError):
                    load_or_create_session_key(path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


class TenantKeyTests(unittest.TestCase):
    """Cache and glossary tenants must survive reinstall/upgrade."""

    ENV = {
        "BT_READER_TYPE": "kavita",
        "BT_READER_AUTH_URL": "http://kavita:5000/api/Account",
        "BT_READER_VERSION": "0.9.1.4",
        "BT_READER_CONNECTOR_ID": "01234567-89ab-4cde-8123-0123456789ab",
        "BT_PUBLIC_ORIGIN": "https://books.example.test",
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.session_key = self.data / "reader_session_key"
        self.tenant_key = self.data / "tenant_key"

    def broker(self):
        return broker_from_environment(
            {**self.ENV, "BT_SESSION_KEY_PATH": str(self.session_key)}
        )

    def test_uninstall_regenerating_the_session_key_keeps_tenants(self):
        first = self.broker()
        session_before = self.session_key.read_bytes()
        self.session_key.unlink()  # btctl uninstall removes only this file

        second = self.broker()

        self.assertNotEqual(self.session_key.read_bytes(), session_before)
        self.assertEqual(first._subject("7"), second._subject("7"))
        self.assertNotEqual(first._subject("7"), first._subject("8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.tenant_key.stat().st_mode), 0o600)

    def test_existing_install_keeps_its_current_tenants_on_first_start(self):
        legacy_secret = load_or_create_session_key(self.session_key)
        legacy = ReaderSessionBroker(
            reader_type="kavita",
            auth_url=self.ENV["BT_READER_AUTH_URL"],
            reader_version="0.9.1.4",
            connector_id=self.ENV["BT_READER_CONNECTOR_ID"],
            public_origin=self.ENV["BT_PUBLIC_ORIGIN"],
            secret_key=legacy_secret,
        )

        upgraded = self.broker()

        self.assertEqual(self.tenant_key.read_bytes(), legacy_secret)
        self.assertEqual(upgraded._subject("7"), legacy._subject("7"))

    def test_session_tokens_still_use_the_ephemeral_session_key(self):
        self.tenant_key.write_bytes(b"t" * 32)
        self.tenant_key.chmod(0o600)

        broker = self.broker()

        self.assertNotEqual(self.session_key.read_bytes(), b"t" * 32)
        self.assertEqual(broker._secret, self.session_key.read_bytes())
        self.assertNotEqual(broker._token_digest("A" * 43), hmac_digest(b"t" * 32))

    def test_unsafe_tenant_key_fails_closed(self):
        self.tenant_key.write_bytes(b"short")
        self.tenant_key.chmod(0o600)

        with self.assertRaises(BrokerConfigError):
            self.broker()


def hmac_digest(key):
    import hmac as _hmac

    return _hmac.digest(key, b"token\x00" + b"A" * 43, "sha256")


def _signed_cookie(payload, *, stamp="ZZ0001", compressed=False):
    """Build an itsdangerous-shaped cookie; the signature is opaque here."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if compressed:
        raw = zlib.compress(raw)
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return ("." if compressed else "") + f"{body}.{stamp}.signature-{stamp}"


class CwaStableIdentityTests(unittest.TestCase):
    """CWA re-signs its session cookie on every request; tenants must not."""

    ORIGIN = "https://books.example.test"

    def setUp(self):
        self.calls = []
        self.tokens = (f"{letter}" * 43 for letter in "ABCDEFGH")

    def broker(self, accepts):
        def get(url, **kwargs):
            cookie = kwargs["headers"]["Cookie"]
            self.calls.append(cookie)
            if accepts(cookie):
                return FakeResponse(b"[]")
            return FakeResponse(b"", status=401)

        return ReaderSessionBroker(
            reader_type="cwa",
            auth_url="http://calibre-web:8083/ajax/emailstat",
            reader_version="4.0.6",
            connector_id="01234567-89ab-4cde-8123-0123456789ab",
            public_origin=self.ORIGIN,
            secret_key=b"s" * 32,
            http_get=get,
            clock=Clock(),
            token_factory=lambda: next(self.tokens),
        )

    def exchange(self, broker, cookie):
        issue = broker.exchange(
            {"Origin": self.ORIGIN, "Cookie": cookie},
            CwaSessionBinding("203.0.113.9", "Reader/1.0"),
        )
        return issue.subject

    def test_rotated_session_cookies_of_one_user_share_one_tenant(self):
        broker = self.broker(lambda cookie: True)
        first = _signed_cookie({"_user_id": "3", "_random": "r1"}, stamp="AAAA")
        rotated = _signed_cookie(
            {"_user_id": "3", "_random": "r1", "magic_shelf_counts": {}},
            stamp="AAAB",
            compressed=True,
        )
        other_user = _signed_cookie({"_user_id": "4"}, stamp="AAAA")

        subjects = [
            self.exchange(broker, f"session={first}"),
            self.exchange(broker, f"session={rotated}; remember_token=opaque"),
            self.exchange(broker, f"session={other_user}"),
        ]

        self.assertEqual(subjects[0], subjects[1])
        self.assertNotEqual(subjects[0], subjects[2])
        # The probe carries exactly the credential the user id was read from.
        self.assertEqual(
            self.calls,
            [f"session={first}", f"session={rotated}", f"session={other_user}"],
        )

    def test_rejected_session_falls_back_to_a_remember_token_only_probe(self):
        session = _signed_cookie({"_user_id": "3"}, stamp="AAAA")
        remember = _signed_cookie({"user": 3, "random": "r1"}, stamp="BBBB")
        broker = self.broker(lambda cookie: cookie.startswith("remember_token="))

        via_remember = self.exchange(
            broker, f"session={session}; remember_token={remember}"
        )
        via_session = self.exchange(
            self.broker(lambda cookie: True), f"session={session}"
        )

        self.assertEqual(
            self.calls,
            [f"session={session}", f"remember_token={remember}", f"session={session}"],
        )
        self.assertEqual(via_remember, via_session)

    def test_single_cookie_probes_keep_the_browser_binding(self):
        headers = []
        broker = self.broker(lambda cookie: True)
        original = broker._http_get
        broker._http_get = lambda url, **kwargs: (
            headers.append(kwargs["headers"]) or original(url, **kwargs)
        )

        self.exchange(broker, f"session={_signed_cookie({'_user_id': '3'})}")

        self.assertEqual(headers[0]["X-Forwarded-For"], "203.0.113.9")
        self.assertEqual(headers[0]["User-Agent"], "Reader/1.0")

    def test_both_identifying_cookies_rejected_stops_after_two_probes(self):
        session = _signed_cookie({"_user_id": "3"})
        remember = _signed_cookie({"user": 3, "random": "r1"})

        def redirect_to_login(url, **kwargs):
            self.calls.append(kwargs["headers"]["Cookie"])
            return FakeResponse(b"", status=302, content_type="text/html")

        broker = self.broker(lambda cookie: False)
        broker._http_get = redirect_to_login
        with self.assertRaises(BrokerRejected):
            self.exchange(broker, f"session={session}; remember_token={remember}")
        self.assertEqual(
            self.calls, [f"session={session}", f"remember_token={remember}"]
        )

    def test_unavailable_authority_fails_closed_without_fallback(self):
        session = _signed_cookie({"_user_id": "3"})
        remember = _signed_cookie({"user": 3, "random": "r1"})
        broker = self.broker(lambda cookie: False)
        broker._http_get = lambda url, **kwargs: (
            self.calls.append(kwargs["headers"]["Cookie"])
            or FakeResponse(b"", status=503)
        )

        with self.assertRaises(BrokerUnavailable):
            self.exchange(broker, f"session={session}; remember_token={remember}")
        self.assertEqual(self.calls, [f"session={session}"])

    def test_large_compressed_session_still_identifies_its_user(self):
        shelves = {str(index): [index, "x" * 40] for index in range(400)}
        session = _signed_cookie(
            {"_user_id": "3", "magic_shelf_counts": shelves}, compressed=True
        )
        broker = self.broker(lambda cookie: True)

        self.assertEqual(
            self.exchange(broker, f"session={session}"),
            self.exchange(broker, f"session={_signed_cookie({'_user_id': '3'})}"),
        )

    def test_rejected_identifying_cookies_never_fall_back_to_a_wider_probe(self):
        session = _signed_cookie({"_user_id": "3"})
        broker = self.broker(lambda cookie: False)

        with self.assertRaises(BrokerRejected):
            self.exchange(broker, f"session={session}")
        self.assertEqual(self.calls, [f"session={session}"])

    def test_undecodable_cookie_still_gets_the_combined_legacy_probe(self):
        session = _signed_cookie({"_user_id": "3"})
        broker = self.broker(lambda cookie: "remember_token=" in cookie)

        subject = self.exchange(broker, f"session={session}; remember_token=opaque")

        self.assertEqual(
            self.calls,
            [f"session={session}", f"session={session}; remember_token=opaque"],
        )
        self.assertTrue(subject.startswith("reader-session:cwa:"))

    def test_malformed_or_hostile_payloads_are_not_identities(self):
        bomb = zlib.compress(b"[" + b" " * 5_000_000 + b"]")
        hostile = [
            "." + base64.urlsafe_b64encode(bomb).rstrip(b"=").decode() + ".t.s",
            _signed_cookie({"_user_id": "0"}),
            _signed_cookie({"_user_id": "3x"}),
            _signed_cookie({"_user_id": True}),
            _signed_cookie({"_user_id": ["3"]}),
            _signed_cookie(["_user_id", "3"]),
            "not-base64!.t.s",
        ]
        for value in hostile:
            with self.subTest(value=value[:24]):
                self.calls.clear()
                broker = self.broker(lambda cookie: True)
                self.exchange(broker, f"session={value}")
                # No user id was extracted, so only the legacy probe ran.
                self.assertEqual(self.calls, [f"session={value}"])


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
