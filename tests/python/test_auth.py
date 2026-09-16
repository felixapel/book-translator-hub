"""Authentication boundary contracts: fail closed, bounded, and tenant-safe."""

from __future__ import annotations

import threading
import time
import os
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests

from auth import (
    AuthConfigError,
    AuthRejected,
    AuthUnavailable,
    CwaSessionBinding,
    RequestAuthenticator,
)
from tests.python.test_cwa_strong_fixture import create_cwa_strong_app, session_cookie_from

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import server


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        content_type="application/json",
        body=b"[]",
        content_length=None,
    ):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class AuthenticationConfigTests(unittest.TestCase):
    def test_default_mode_is_fail_closed_token_auth(self):
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({})

    def test_disabled_mode_must_be_explicit(self):
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({"BT_AUTH_MODE": "disabled"})
        auth = RequestAuthenticator.from_environment({
            "BT_AUTH_MODE": "disabled",
            "BT_ALLOW_INSECURE_AUTH": "true",
        })
        identity = auth.authenticate({}, "127.0.0.1")
        self.assertEqual(identity.subject, "legacy-anonymous")
        self.assertEqual(auth.mode, "disabled")

    def test_invalid_mode_and_cwa_url_are_rejected_at_startup(self):
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({"BT_AUTH_MODE": "magic"})
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({
                "BT_AUTH_MODE": "cwa_session",
                "BT_CWA_AUTH_URL": "file:///etc/passwd",
            })
        for invalid_url in (
            "http://calibre-web:bad/ajax/emailstat",
            "http://calibre-web:8083/ping",
            "http://calibre-web:8083/ajax/emailstat?public=1",
        ):
            with self.subTest(invalid_url=invalid_url), self.assertRaises(AuthConfigError):
                RequestAuthenticator.from_environment({
                    "BT_AUTH_MODE": "cwa_session",
                    "BT_CWA_AUTH_URL": invalid_url,
                })
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({
                "BT_AUTH_MODE": "cwa_session",
                "BT_CWA_AUTH_URL": "http://calibre-web:8083/ajax/emailstat",
                "BT_CWA_AUTH_MAX_RESPONSE_BYTES": "0",
            })
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({
                "BT_AUTH_MODE": "cwa_session",
                "BT_CWA_AUTH_URL": "http://calibre-web:8083/ajax/emailstat",
                "BT_API_TOKEN": "unsafe operator token",
            })
        with self.assertRaises(AuthConfigError):
            RequestAuthenticator.from_environment({
                "BT_AUTH_MODE": "forwarded",
                "BT_IDENTITY_TRUSTED_PROXIES": "",
            })

    def test_cors_configuration_accepts_only_exact_origins(self):
        self.assertEqual(
            server._validate_cors_origin("https://books.example.test:8443"),
            "https://books.example.test:8443",
        )
        for origin in (
            "*",
            "https://books.example.test/path",
            "https://user@books.example.test",
            "https://books.example.test?query=1",
            "https://books.example.test:bad",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                server._validate_cors_origin(origin)


class TokenAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.auth = RequestAuthenticator(mode="token", api_token="correct-horse")

    def test_missing_and_wrong_tokens_are_rejected(self):
        with self.assertRaises(AuthRejected):
            self.auth.authenticate({}, "127.0.0.1")
        with self.assertRaises(AuthRejected):
            self.auth.authenticate({"X-BT-Token": "wrong"}, "127.0.0.1")
        with self.assertRaises(AuthRejected):
            self.auth.authenticate({"X-BT-Token": "snowman-☃"}, "127.0.0.1")

    def test_configured_token_must_be_an_ascii_header_value(self):
        for invalid in ("contains space", "snowman-☃", "line\nbreak"):
            with self.subTest(invalid=invalid), self.assertRaises(AuthConfigError):
                RequestAuthenticator(mode="token", api_token=invalid)
        self.assertFalse(server._token_matches("snowman-☃", "operator-token"))

    def test_token_identity_is_opaque(self):
        identity = self.auth.authenticate(
            {"X-BT-Token": "correct-horse"}, "127.0.0.1"
        )
        self.assertTrue(identity.subject.startswith("token:"))
        self.assertNotIn("correct-horse", identity.subject)
        self.assertEqual(identity.roles, frozenset({"operator"}))


class ForwardedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.auth = RequestAuthenticator(
            mode="forwarded",
            identity_trusted_proxies=("10.42.8.9/32", "127.0.0.1/32"),
        )

    def test_forwarded_mode_requires_exact_single_peer_addresses(self):
        for peer in ("10.42.0.0/16", "2001:db8::/64"):
            with self.subTest(peer=peer), self.assertRaises(AuthConfigError):
                RequestAuthenticator(
                    mode="forwarded",
                    identity_trusted_proxies=(peer,),
                )

    def test_direct_client_cannot_forge_identity_headers(self):
        with self.assertRaises(AuthRejected):
            self.auth.authenticate(
                {"X-BT-Subject": "admin", "X-BT-Roles": "admin"},
                "192.168.1.77",
            )

    def test_trusted_proxy_sets_opaque_subject_and_bounded_roles(self):
        identity = self.auth.authenticate(
            {"X-BT-Subject": "user@example.test", "X-BT-Roles": "reader, admin"},
            "10.42.8.9",
        )
        self.assertTrue(identity.subject.startswith("forwarded:"))
        self.assertNotIn("user@example.test", identity.subject)
        self.assertEqual(identity.roles, frozenset({"reader", "admin"}))

    def test_configured_identity_header_is_used_and_roles_can_be_disabled(self):
        auth = RequestAuthenticator.from_environment({
            "BT_AUTH_MODE": "forwarded",
            "BT_IDENTITY_TRUSTED_PROXIES": "10.42.8.9/32",
            "BT_FORWARDED_SUBJECT_HEADER": "X-authentik-uid",
            "BT_FORWARDED_ROLES_HEADER": "",
        })

        identity = auth.authenticate(
            {
                "X-authentik-uid": "stable-user-id",
                "X-authentik-groups": "admin",
            },
            "10.42.8.9",
        )

        self.assertTrue(identity.subject.startswith("forwarded:"))
        self.assertEqual(identity.roles, frozenset())

    def test_forwarded_request_must_arrive_without_browser_cookies(self):
        with self.assertRaises(AuthRejected):
            self.auth.authenticate(
                {
                    "X-BT-Subject": "user@example.test",
                    "Cookie": "authentik_session=must-be-stripped-at-edge",
                },
                "10.42.8.9",
            )

    def test_missing_or_malformed_subject_and_roles_are_rejected(self):
        for headers in (
            {},
            {"X-BT-Subject": "x\nspoof"},
            {"X-BT-Subject": "user", "X-BT-Roles": "reader,not a role"},
        ):
            with self.subTest(headers=headers), self.assertRaises(AuthRejected):
                self.auth.authenticate(headers, "127.0.0.1")


class CWASessionAuthenticationTests(unittest.TestCase):
    def make_auth(self, get, **overrides):
        return RequestAuthenticator(
            mode="cwa_session",
            cwa_auth_url="http://calibre-web:8083/ajax/emailstat",
            cwa_cookie_names=("session", "remember_token"),
            cwa_timeout_seconds=overrides.get("cwa_timeout_seconds", 0.5),
            cwa_cache_ttl_seconds=10,
            cwa_cache_max_entries=2,
            cwa_max_inflight=4,
            cwa_max_response_bytes=overrides.get("cwa_max_response_bytes", 1024),
            http_get=get,
        )

    @staticmethod
    def authenticate(
        authenticator,
        headers,
        remote_addr="127.0.0.1",
        *,
        cwa_remote_addr=None,
        user_agent="Test-Browser/1.0",
    ):
        return authenticator.authenticate(
            headers,
            remote_addr,
            cwa_binding=CwaSessionBinding(
                cwa_remote_addr=cwa_remote_addr or remote_addr,
                user_agent=user_agent,
            ),
        )

    @staticmethod
    def fixture_transport(client, *, remote_addr="172.30.39.4"):
        def get(_url, *, headers, **_kwargs):
            response = client.get(
                "/ajax/emailstat",
                headers=headers,
                environ_base={"REMOTE_ADDR": remote_addr},
            )
            return FakeResponse(
                status_code=response.status_code,
                content_type=response.content_type,
                body=response.data,
            )

        return get

    def test_cwa_v406_strong_fixture_requires_address_and_user_agent(self):
        client = create_cwa_strong_app().test_client(use_cookies=False)
        browser_user_agent = "Regression-Browser/1.0"
        observed_peer = "198.51.100.7"
        login = client.get(
            "/fixture/login",
            headers={
                "User-Agent": browser_user_agent,
                "X-Forwarded-For": observed_peer,
            },
            environ_base={"REMOTE_ADDR": "172.30.39.3"},
        )
        cookie = session_cookie_from(login)

        no_context = client.get(
            "/ajax/emailstat",
            headers={"Cookie": cookie},
            environ_base={"REMOTE_ADDR": "172.30.39.4"},
        )
        matching = client.get(
            "/ajax/emailstat",
            headers={
                "Cookie": cookie,
                "User-Agent": browser_user_agent,
                "X-Forwarded-For": observed_peer,
            },
            environ_base={"REMOTE_ADDR": "172.30.39.4"},
        )
        user_agent_only = client.get(
            "/ajax/emailstat",
            headers={"Cookie": cookie, "User-Agent": browser_user_agent},
            environ_base={"REMOTE_ADDR": "172.30.39.4"},
        )

        self.assertEqual(no_context.content_type, "text/html; charset=utf-8")
        self.assertEqual(matching.content_type, "application/json")
        self.assertEqual(user_agent_only.content_type, "text/html; charset=utf-8")

    def test_authenticator_replays_the_strong_session_binding(self):
        client = create_cwa_strong_app().test_client(use_cookies=False)
        browser_user_agent = "Regression-Browser/1.0"
        observed_peer = "198.51.100.7"
        login = client.get(
            "/fixture/login",
            headers={
                "User-Agent": browser_user_agent,
                "X-Forwarded-For": observed_peer,
            },
            environ_base={"REMOTE_ADDR": "172.30.39.3"},
        )
        cookie = session_cookie_from(login)
        auth = self.make_auth(self.fixture_transport(client))

        identity = self.authenticate(
            auth,
            {"Cookie": cookie},
            "172.30.39.3",
            cwa_remote_addr=observed_peer,
            user_agent=browser_user_agent,
        )

        self.assertTrue(identity.subject.startswith("cwa-session:"))

    def test_valid_session_forwards_only_selected_cookies_without_redirects(self):
        response = FakeResponse()
        get = mock.Mock(return_value=response)
        auth = self.make_auth(get)

        identity = self.authenticate(
            auth,
            {"Cookie": "ignored=private; session=session-secret; remember_token=remember"},
            "172.18.0.4",
        )

        self.assertTrue(identity.subject.startswith("cwa-session:"))
        self.assertNotIn("session-secret", identity.subject)
        get.assert_called_once_with(
            "http://calibre-web:8083/ajax/emailstat",
            headers={
                "Accept": "application/json",
                "Cookie": "session=session-secret; remember_token=remember",
                "X-Forwarded-For": "172.18.0.4",
                "User-Agent": "Test-Browser/1.0",
            },
            timeout=0.5,
            allow_redirects=False,
            stream=True,
        )
        self.assertTrue(response.closed)

    def test_success_must_match_the_cwa_task_list_contract(self):
        invalid_responses = (
            FakeResponse(body=b'{"authenticated": true}'),
            FakeResponse(body=b'["not-a-task-object"]'),
            FakeResponse(body=b"not-json"),
            FakeResponse(content_type="text/html", body=b"[]"),
        )
        for response in invalid_responses:
            with self.subTest(body=response.body, content_type=response.headers["Content-Type"]):
                auth = self.make_auth(mock.Mock(return_value=response))
                with self.assertRaises(AuthRejected):
                    self.authenticate(auth, {"Cookie": "session=wrong-endpoint"})

    def test_success_body_is_bounded_before_json_parsing(self):
        response = FakeResponse(body=b"[{}]", content_length=2048)
        auth = self.make_auth(
            mock.Mock(return_value=response), cwa_max_response_bytes=1024
        )
        with self.assertRaises(AuthUnavailable):
            self.authenticate(auth, {"Cookie": "session=oversized"})
        self.assertTrue(response.closed)

        response = FakeResponse(body=b"[" + (b" " * 1024) + b"]")
        auth = self.make_auth(
            mock.Mock(return_value=response), cwa_max_response_bytes=1024
        )
        with self.assertRaises(AuthUnavailable):
            self.authenticate(auth, {"Cookie": "session=streamed-oversized"})
        self.assertTrue(response.closed)

    def test_streamed_body_has_an_absolute_deadline(self):
        class SlowDripResponse(FakeResponse):
            def iter_content(self, chunk_size=8192):
                del chunk_size
                yield b"["
                time.sleep(0.05)
                yield b"]"

        response = SlowDripResponse()
        auth = self.make_auth(
            mock.Mock(return_value=response), cwa_timeout_seconds=0.01
        )

        with self.assertRaises(AuthUnavailable):
            self.authenticate(auth, {"Cookie": "session=slow-drip"})

        self.assertTrue(response.closed)

    def test_missing_cookie_and_login_redirect_are_unauthorized(self):
        get = mock.Mock(return_value=FakeResponse(status_code=302, content_type="text/html"))
        auth = self.make_auth(get)
        with self.assertRaises(AuthRejected):
            self.authenticate(auth, {})
        get.assert_not_called()
        with self.assertRaises(AuthRejected):
            self.authenticate(auth, {"Cookie": "session=expired"})

    def test_upstream_failures_are_sanitized_as_unavailable(self):
        def failed(*_args, **_kwargs):
            raise requests.exceptions.ConnectionError("private upstream detail")

        auth = self.make_auth(failed)
        with self.assertRaises(AuthUnavailable) as captured:
            self.authenticate(auth, {"Cookie": "session=secret"})
        self.assertNotIn("private upstream detail", str(captured.exception))

        auth = self.make_auth(lambda *_args, **_kwargs: FakeResponse(status_code=500))
        with self.assertRaises(AuthUnavailable):
            self.authenticate(auth, {"Cookie": "session=secret"})

    def test_validation_is_cached_but_distinct_sessions_stay_isolated(self):
        get = mock.Mock(return_value=FakeResponse())
        auth = self.make_auth(get)
        first = self.authenticate(auth, {"Cookie": "session=one"})
        repeat = self.authenticate(auth, {"Cookie": "session=one"})
        second = self.authenticate(auth, {"Cookie": "session=two"})
        self.assertEqual(first.subject, repeat.subject)
        self.assertNotEqual(first.subject, second.subject)
        self.assertEqual(get.call_count, 2)

        self.authenticate(auth, {"Cookie": "session=three"})
        self.assertLessEqual(auth.cache_entries, 2)

    def test_validation_cache_is_scoped_to_the_strong_session_context(self):
        def context_sensitive_get(_url, *, headers, **_kwargs):
            if (
                headers.get("User-Agent") == "Browser-A/1.0"
                and headers.get("X-Forwarded-For") == "198.51.100.7"
            ):
                return FakeResponse()
            return FakeResponse(status_code=302, content_type="text/html")

        expected_subject = None
        rejected_contexts = (
            ("198.51.100.7", "Browser-B/1.0"),
            ("198.51.100.8", "Browser-A/1.0"),
        )
        for rejected_addr, rejected_user_agent in rejected_contexts:
            with self.subTest(
                rejected_addr=rejected_addr,
                rejected_user_agent=rejected_user_agent,
            ):
                positive_first = mock.Mock(side_effect=context_sensitive_get)
                auth = self.make_auth(positive_first)
                accepted = self.authenticate(
                    auth,
                    {"Cookie": "session=shared"},
                    cwa_remote_addr="198.51.100.7",
                    user_agent="Browser-A/1.0",
                )
                with self.assertRaises(AuthRejected):
                    self.authenticate(
                        auth,
                        {"Cookie": "session=shared"},
                        cwa_remote_addr=rejected_addr,
                        user_agent=rejected_user_agent,
                    )
                self.assertEqual(positive_first.call_count, 2)

                negative_first = mock.Mock(side_effect=context_sensitive_get)
                auth = self.make_auth(negative_first)
                with self.assertRaises(AuthRejected):
                    self.authenticate(
                        auth,
                        {"Cookie": "session=shared"},
                        cwa_remote_addr=rejected_addr,
                        user_agent=rejected_user_agent,
                    )
                recovered = self.authenticate(
                    auth,
                    {"Cookie": "session=shared"},
                    cwa_remote_addr="198.51.100.7",
                    user_agent="Browser-A/1.0",
                )
                self.assertEqual(negative_first.call_count, 2)
                expected_subject = expected_subject or accepted.subject
                self.assertEqual(accepted.subject, expected_subject)
                self.assertEqual(recovered.subject, expected_subject)

    def test_absent_and_empty_user_agents_are_distinct_on_the_wire(self):
        observed_user_agents = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed_user_agents.append(self.headers.get("User-Agent"))
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        authority = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=authority.serve_forever, daemon=True)
        thread.start()
        try:
            auth = RequestAuthenticator(
                mode="cwa_session",
                cwa_auth_url=(
                    f"http://127.0.0.1:{authority.server_port}/ajax/emailstat"
                ),
                cwa_cache_ttl_seconds=10,
                http_get=None,
            )
            absent = self.authenticate(
                auth,
                {"Cookie": "session=shared"},
                user_agent=None,
            )
            empty = self.authenticate(
                auth,
                {"Cookie": "session=shared"},
                user_agent="",
            )
        finally:
            authority.shutdown()
            authority.server_close()
            thread.join(2)

        self.assertEqual(absent.subject, empty.subject)
        self.assertEqual(observed_user_agents, [None, ""])

    def test_singleflight_coalesces_only_identical_strong_session_contexts(self):
        lock = threading.Lock()
        two_contexts_entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_get(_url, *, headers, **_kwargs):
            with lock:
                calls.append(
                    (headers.get("X-Forwarded-For"), headers.get("User-Agent"))
                )
                if len(calls) == 2:
                    two_contexts_entered.set()
            release.wait(2)
            return FakeResponse()

        auth = self.make_auth(slow_get)
        identities = []
        errors = []

        def validate(remote_addr, user_agent):
            try:
                identities.append(
                    self.authenticate(
                        auth,
                        {"Cookie": "session=shared"},
                        cwa_remote_addr=remote_addr,
                        user_agent=user_agent,
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion evidence
                errors.append(exc)

        threads = [
            threading.Thread(target=validate, args=("198.51.100.7", "Browser-A/1.0")),
            threading.Thread(target=validate, args=("198.51.100.8", "Browser-B/1.0")),
        ]
        for thread in threads:
            thread.start()
        both_entered = two_contexts_entered.wait(1)
        release.set()
        for thread in threads:
            thread.join(2)

        self.assertTrue(both_entered)
        self.assertFalse(errors)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({identity.subject for identity in identities}), 1)

    def test_invalid_strong_session_bindings_fail_before_the_probe(self):
        get = mock.Mock(return_value=FakeResponse())
        auth = self.make_auth(get)
        invalid_bindings = (
            None,
            CwaSessionBinding("198.51.100.7, 203.0.113.8", "Browser/1.0"),
            CwaSessionBinding(" 198.51.100.7", "Browser/1.0"),
            CwaSessionBinding("not-an-ip", "Browser/1.0"),
            CwaSessionBinding("198.51.100.7", "Browser\rInjected"),
            CwaSessionBinding("198.51.100.7", "Browser-☃"),
            CwaSessionBinding("198.51.100.7", "A" * 4097),
        )

        for binding in invalid_bindings:
            with self.subTest(binding=binding), self.assertRaises(AuthRejected):
                auth.authenticate(
                    {"Cookie": "session=secret"},
                    "127.0.0.1",
                    cwa_binding=binding,
                )

        get.assert_not_called()

    def test_default_transport_ignores_environment_proxies_and_closes_session(self):
        response = FakeResponse()
        fake_session = mock.Mock()
        fake_session.trust_env = True
        fake_session.get.return_value = response
        auth = self.make_auth(None)

        with mock.patch.object(requests, "Session", return_value=fake_session):
            identity = self.authenticate(auth, {"Cookie": "session=direct-only"})

        self.assertTrue(identity.subject.startswith("cwa-session:"))
        self.assertFalse(fake_session.trust_env)
        fake_session.get.assert_called_once()
        fake_session.close.assert_called_once()
        self.assertTrue(response.closed)

    def test_concurrent_validation_is_coalesced(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_get(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            release.wait(2)
            return FakeResponse()

        auth = self.make_auth(slow_get)
        identities = []
        errors = []

        def validate():
            try:
                identities.append(
                    self.authenticate(auth, {"Cookie": "session=same"})
                )
            except Exception as exc:  # pragma: no cover - assertion evidence
                errors.append(exc)

        threads = [threading.Thread(target=validate) for _ in range(8)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(2)

        self.assertFalse(errors)
        self.assertEqual(len(identities), 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({identity.subject for identity in identities}), 1)


class ServerAuthenticationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_authenticator = server.AUTHENTICATOR
        self.original_auth_limit = server.BT_AUTH_RATE_LIMIT_PER_MINUTE
        self.original_auth_inflight_limit = server.BT_AUTH_MAX_INFLIGHT_PER_CLIENT
        self.original_api_rate_limit = server.RATE_LIMIT_MAX
        self.original_rate_client_cap = server.BT_RATE_LIMIT_MAX_CLIENTS
        self.original_origins = server.ALLOWED_ORIGINS
        self.original_public_origin = server.BT_PUBLIC_ORIGIN
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"
        server._auth_rate_limit_store.clear()
        server._auth_inflight_store.clear()
        server._rate_limit_store.clear()
        self.client = server.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = server.BT_PUBLIC_ORIGIN

    def tearDown(self):
        server.AUTHENTICATOR = self.original_authenticator
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = self.original_auth_limit
        server.BT_AUTH_MAX_INFLIGHT_PER_CLIENT = self.original_auth_inflight_limit
        server.RATE_LIMIT_MAX = self.original_api_rate_limit
        server.BT_RATE_LIMIT_MAX_CLIENTS = self.original_rate_client_cap
        server.ALLOWED_ORIGINS = self.original_origins
        server.BT_PUBLIC_ORIGIN = self.original_public_origin
        server._auth_rate_limit_store.clear()
        server._auth_inflight_store.clear()
        server._rate_limit_store.clear()

    def test_protected_route_rejects_before_translation_or_cache_work(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="integration-secret"
        )
        with mock.patch.object(server, "_cache_lookup") as cache_lookup, mock.patch.object(
            server, "translate_text"
        ) as translate_text:
            response = self.client.post("/translate", json={"text": "private text"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")
        cache_lookup.assert_not_called()
        translate_text.assert_not_called()

    def test_authenticated_subject_owns_cache_namespace(self):
        authenticator = RequestAuthenticator(
            mode="token", api_token="integration-secret"
        )
        server.AUTHENTICATOR = authenticator
        expected_subject = authenticator.authenticate(
            {"X-BT-Token": "integration-secret"}, "127.0.0.1"
        ).subject

        with mock.patch.object(server, "_cache_lookup", return_value="hola") as lookup:
            response = self.client.post(
                "/translate",
                headers={"X-BT-Token": "integration-secret"},
                json={
                    "text": "hello",
                    "book_id": "book-1",
                    "chapter_id": "chapter-2",
                    "tenant": "client-spoof",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(lookup.call_args.kwargs["tenant"], expected_subject)
        self.assertNotEqual(lookup.call_args.kwargs["tenant"], "client-spoof")

    def test_shallow_health_is_independent_of_auth_authority(self):
        unavailable = mock.Mock(mode="cwa_session")
        unavailable.authenticate.side_effect = AuthUnavailable(
            "private authority detail"
        )
        server.AUTHENTICATOR = unavailable

        self.assertEqual(self.client.get("/health").status_code, 200)
        protected = self.client.get("/metrics")
        self.assertEqual(protected.status_code, 503)
        self.assertEqual(protected.get_json()["error"], "authentication_unavailable")
        self.assertNotIn("private authority detail", protected.get_data(as_text=True))

    def test_authentication_attempts_have_a_separate_rate_limit(self):
        authenticator = mock.Mock(mode="token")
        authenticator.authenticate.side_effect = AuthRejected("invalid")
        server.AUTHENTICATOR = authenticator
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = 1

        first = self.client.get("/metrics", environ_base={"REMOTE_ADDR": "198.51.100.9"})
        second = self.client.get("/metrics", environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json()["error"], "rate_limited")
        self.assertEqual(authenticator.authenticate.call_count, 1)

    def test_rejected_proxy_client_cannot_exhaust_an_authenticated_subject(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="integration-secret"
        )
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = 2
        peer = {"REMOTE_ADDR": "172.30.39.3"}

        rejected = self.client.get("/metrics", environ_base=peer)
        accepted = self.client.get(
            "/metrics",
            headers={"X-BT-Token": "integration-secret"},
            environ_base=peer,
        )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_distinct_authenticated_subjects_behind_one_proxy_have_separate_budgets(self):
        authenticator = mock.Mock(mode="forwarded")

        def authenticate(headers, _peer):
            return mock.Mock(
                subject=f"forwarded:{headers['X-Test-Subject']}",
                roles=frozenset(),
            )

        authenticator.authenticate.side_effect = authenticate
        server.AUTHENTICATOR = authenticator
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = 10
        server.RATE_LIMIT_MAX = 1
        peer = {"REMOTE_ADDR": "172.30.39.3"}
        payload = {
            "text": "hello",
            "source_lang": "English",
            "target_lang": "English",
            "provider_policy": {
                **server.provider_policy(),
                "generation": server.PROVIDER_POLICY_GENERATION,
            },
        }

        alice = self.client.post(
            "/translate",
            headers={"X-Test-Subject": "alice"},
            json=payload,
            environ_base=peer,
        )
        bob = self.client.post(
            "/translate",
            headers={"X-Test-Subject": "bob"},
            json=payload,
            environ_base=peer,
        )
        alice_again = self.client.post(
            "/translate",
            headers={"X-Test-Subject": "alice"},
            json=payload,
            environ_base=peer,
        )

        self.assertEqual(alice.status_code, 200)
        self.assertEqual(bob.status_code, 200)
        self.assertEqual(alice_again.status_code, 429)

    def test_auth_rate_limit_identity_map_is_bounded_fail_closed(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="integration-secret"
        )
        server.BT_RATE_LIMIT_MAX_CLIENTS = 1

        first = self.client.get("/metrics", environ_base={"REMOTE_ADDR": "198.51.100.10"})
        new_identity = self.client.get(
            "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.11"}
        )
        self.assertEqual(first.status_code, 401)
        self.assertEqual(new_identity.status_code, 429)
        self.assertLessEqual(len(server._auth_rate_limit_store), 1)

    def test_authentication_inflight_is_bounded_before_authority_work(self):
        entered = threading.Event()
        release = threading.Event()
        authenticator = mock.Mock(mode="cwa_session")

        def authenticate(_headers, _peer, **_kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release authentication")
            return mock.Mock(subject="cwa-session:test", roles=frozenset())

        authenticator.authenticate.side_effect = authenticate
        server.AUTHENTICATOR = authenticator
        server.BT_AUTH_MAX_INFLIGHT_PER_CLIENT = 1
        first_result = {}

        def first_request():
            client = server.app.test_client()
            first_result["response"] = client.get(
                "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.12"}
            )

        thread = threading.Thread(target=first_request)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            second = self.client.get(
                "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.12"}
            )
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.get_json()["error"], "rate_limited")
            self.assertEqual(authenticator.authenticate.call_count, 1)
        finally:
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result["response"].status_code, 200)
        self.assertEqual(server._auth_inflight_store, {})

    def test_managed_proxy_alias_is_the_only_dns_peer_allowed_to_supply_xff(self):
        trusted = server.ipaddress.ip_address("172.30.39.3")
        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", "translator-proxy"), \
             mock.patch.object(server, "BT_TRUSTED_PROXIES", ""), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", ()), \
             mock.patch.object(
                 server, "_resolved_trusted_proxy_addresses", return_value=frozenset({trusted})
             ):
            with server.app.test_request_context(
                "/metrics",
                headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.7"},
                environ_base={"REMOTE_ADDR": "172.30.39.3"},
            ):
                self.assertEqual(server._client_ip(), "198.51.100.7")
            with server.app.test_request_context(
                "/metrics",
                headers={"X-Forwarded-For": "198.51.100.8"},
                environ_base={"REMOTE_ADDR": "172.30.39.4"},
            ):
                self.assertEqual(server._client_ip(), "172.30.39.4")

    @staticmethod
    def successful_cwa_authenticator():
        authenticator = mock.Mock(mode="cwa_session")
        authenticator.authenticate.return_value = mock.Mock(
            subject="cwa-session:test",
            roles=frozenset(),
        )
        return authenticator

    def test_cwa_session_binding_uses_the_managed_proxy_observed_peer(self):
        authenticator = self.successful_cwa_authenticator()
        server.AUTHENTICATOR = authenticator
        trusted_peer = server.ipaddress.ip_address("172.30.39.3")

        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", "translator-proxy"), \
             mock.patch.object(server, "BT_TRUSTED_PROXIES", frozenset()), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", ()), \
             mock.patch.object(
                 server,
                 "_resolved_trusted_proxy_addresses",
                 return_value=frozenset({trusted_peer}),
             ):
            response = self.client.get(
                "/metrics",
                headers={
                    "Cookie": "session=secret",
                    "User-Agent": "Regression-Browser/1.0",
                    "X-Forwarded-For": "198.51.100.7",
                },
                environ_base={"REMOTE_ADDR": "172.30.39.3"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            authenticator.authenticate.call_args.kwargs["cwa_binding"],
            CwaSessionBinding(
                cwa_remote_addr="198.51.100.7",
                user_agent="Regression-Browser/1.0",
            ),
        )

    def test_cwa_session_binding_allows_an_exact_trusted_proxy_address(self):
        authenticator = self.successful_cwa_authenticator()
        server.AUTHENTICATOR = authenticator
        exact_network = server.ipaddress.ip_network("172.30.39.3/32")

        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", ""), \
             mock.patch.object(
                 server, "BT_TRUSTED_PROXIES", frozenset({"172.30.39.3/32"})
             ), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", (exact_network,)):
            response = self.client.get(
                "/metrics",
                headers={
                    "User-Agent": "Regression-Browser/1.0",
                    "X-Forwarded-For": "198.51.100.7",
                },
                environ_base={"REMOTE_ADDR": "172.30.39.3"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            authenticator.authenticate.call_args.kwargs["cwa_binding"].cwa_remote_addr,
            "198.51.100.7",
        )

    def test_cwa_session_direct_binding_rejects_spoofed_xff_and_legacy_trust(self):
        authenticator = self.successful_cwa_authenticator()
        server.AUTHENTICATOR = authenticator

        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", ""), \
             mock.patch.object(server, "BT_TRUSTED_PROXIES", frozenset()), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", ()), \
             mock.patch.object(server, "BT_TRUST_PROXY", True):
            response = self.client.get(
                "/metrics",
                headers={"X-Forwarded-For": "203.0.113.99"},
                environ_base={"REMOTE_ADDR": "198.51.100.55"},
            )

        self.assertEqual(response.status_code, 401)
        authenticator.authenticate.assert_not_called()

    def test_cwa_session_rejects_a_broad_rate_limit_cidr_as_auth_authority(self):
        authenticator = self.successful_cwa_authenticator()
        server.AUTHENTICATOR = authenticator
        broad_network = server.ipaddress.ip_network("172.30.0.0/16")

        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", ""), \
             mock.patch.object(
                 server, "BT_TRUSTED_PROXIES", frozenset({"172.30.0.0/16"})
             ), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", (broad_network,)):
            response = self.client.get(
                "/metrics",
                headers={"X-Forwarded-For": "198.51.100.7"},
                environ_base={"REMOTE_ADDR": "172.30.39.3"},
            )

        self.assertEqual(response.status_code, 401)
        authenticator.authenticate.assert_not_called()

    def test_cwa_session_direct_binding_uses_the_socket_peer_without_xff(self):
        authenticator = self.successful_cwa_authenticator()
        server.AUTHENTICATOR = authenticator

        with mock.patch.object(server, "BT_TRUSTED_PROXY_HOST", ""), \
             mock.patch.object(server, "BT_TRUSTED_PROXIES", frozenset()), \
             mock.patch.object(server, "_TRUSTED_PROXY_NETS", ()), \
             mock.patch.object(server, "BT_TRUST_PROXY", True):
            response = self.client.get(
                "/metrics",
                headers={"User-Agent": "Direct-Browser/1.0"},
                environ_base={"REMOTE_ADDR": "198.51.100.55"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            authenticator.authenticate.call_args.kwargs["cwa_binding"],
            CwaSessionBinding(
                cwa_remote_addr="198.51.100.55",
                user_agent="Direct-Browser/1.0",
            ),
        )

    def test_cwa_session_managed_proxy_path_fails_closed_without_clean_xff(self):
        trusted_peer = server.ipaddress.ip_address("172.30.39.3")
        for xff in (None, "198.51.100.7, 203.0.113.8", "not-an-ip"):
            with self.subTest(xff=xff):
                authenticator = self.successful_cwa_authenticator()
                server.AUTHENTICATOR = authenticator
                headers = {"User-Agent": "Regression-Browser/1.0"}
                if xff is not None:
                    headers["X-Forwarded-For"] = xff
                with mock.patch.object(
                    server, "BT_TRUSTED_PROXY_HOST", "translator-proxy"
                ), mock.patch.object(
                    server, "BT_TRUSTED_PROXIES", frozenset()
                ), mock.patch.object(
                    server, "_TRUSTED_PROXY_NETS", ()
                ), mock.patch.object(
                    server,
                    "_resolved_trusted_proxy_addresses",
                    return_value=frozenset({trusted_peer}),
                ):
                    response = self.client.get(
                        "/metrics",
                        headers=headers,
                        environ_base={"REMOTE_ADDR": "172.30.39.3"},
                    )

                self.assertEqual(response.status_code, 401)
                authenticator.authenticate.assert_not_called()

    def test_cwa_cookie_cors_requires_an_exact_origin(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="cwa_session",
            cwa_auth_url="http://calibre-web:8083/ajax/emailstat",
            http_get=lambda *_args, **_kwargs: FakeResponse(),
        )
        server.ALLOWED_ORIGINS = frozenset({"https://books.example.test"})

        exact = self.client.get(
            "/ping", headers={"Origin": "https://books.example.test"}
        )
        broad_private = self.client.get(
            "/ping", headers={"Origin": "http://192.168.1.10:8083"}
        )
        self.assertEqual(
            exact.headers.get("Access-Control-Allow-Origin"),
            "https://books.example.test",
        )
        self.assertEqual(exact.headers.get("Access-Control-Allow-Credentials"), "true")
        self.assertIn("Origin", exact.headers.get("Vary", ""))
        self.assertIsNone(broad_private.headers.get("Access-Control-Allow-Origin"))

    def test_cookie_authenticated_writes_require_the_exact_public_origin(self):
        authenticator = self.successful_cwa_authenticator()
        authenticator.mode = "reader_session"
        server.AUTHENTICATOR = authenticator
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"
        before_rejections = server._metrics["outcomes"]["csrf_rejected"]

        missing = server.app.test_client().post("/feedback", json={})
        wrong = self.client.post(
            "/feedback",
            json={},
            headers={"Origin": "https://evil.example.test"},
        )
        exact = self.client.post(
            "/feedback",
            json={},
            headers={"Origin": "https://books.example.test"},
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(exact.status_code, 400)
        self.assertEqual(authenticator.authenticate.call_count, 1)
        self.assertEqual(
            server._metrics["outcomes"]["csrf_rejected"], before_rejections + 2
        )

    def test_cookie_authenticated_write_rejects_malformed_non_ascii_origin(self):
        authenticator = self.successful_cwa_authenticator()
        authenticator.mode = "reader_session"
        server.AUTHENTICATOR = authenticator
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"

        response = self.client.post(
            "/feedback",
            json={},
            headers={"Origin": "https://books.example.t\u00e9st"},
        )

        self.assertEqual(response.status_code, 403)
        authenticator.authenticate.assert_not_called()

    def test_cookie_authenticated_delete_requires_the_exact_public_origin(self):
        authenticator = self.successful_cwa_authenticator()
        authenticator.mode = "reader_session"
        server.AUTHENTICATOR = authenticator
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"

        rejected = self.client.delete(
            "/glossary",
            json={"source": "term"},
            headers={"Origin": "https://evil.example.test"},
        )

        self.assertEqual(rejected.status_code, 403)
        authenticator.authenticate.assert_not_called()

    def test_token_authenticated_write_keeps_non_browser_client_compatibility(self):
        authenticator = mock.Mock(mode="token")
        authenticator.authenticate.return_value = mock.Mock(
            subject="token:test", roles=frozenset({"operator"})
        )
        server.AUTHENTICATOR = authenticator

        response = self.client.post("/feedback", json={})

        self.assertEqual(response.status_code, 400)
        authenticator.authenticate.assert_called_once()

    def test_cookie_preflight_advertises_delete_only_for_an_exact_origin(self):
        server.AUTHENTICATOR = mock.Mock(mode="reader_session")
        server.ALLOWED_ORIGINS = frozenset({"https://books.example.test"})

        exact = self.client.options(
            "/glossary",
            headers={
                "Origin": "https://books.example.test",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        other = self.client.options(
            "/glossary",
            headers={
                "Origin": "https://evil.example.test",
                "Access-Control-Request-Method": "DELETE",
            },
        )

        self.assertEqual(exact.status_code, 200)
        self.assertIn("DELETE", exact.headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(exact.headers.get("Access-Control-Allow-Credentials"), "true")
        self.assertIn("Origin", exact.headers.get("Vary", ""))
        self.assertIsNone(other.headers.get("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()
