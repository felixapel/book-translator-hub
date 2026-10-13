"""Provider registry, privacy policy, and failover contracts."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import translator
from work_budget import WorkBudget, WorkBudgetExceeded


def _budget() -> WorkBudget:
    return WorkBudget(
        max_attempts=20,
        max_input_bytes=1_000_000,
        max_output_tokens=100_000,
        deadline_seconds=5,
    )


class ProviderConfigurationTests(unittest.TestCase):
    def test_health_probe_allows_a_text_answer_after_model_thinking(self):
        provider = translator._Provider(
            "gemini", "gemini-3.5-flash-lite", "gemini-key"
        )
        with mock.patch.object(
            translator, "_call_provider", return_value="OK"
        ) as call_provider:
            health = translator._probe(provider, _budget())

        self.assertEqual(health["status"], "ok")
        self.assertEqual(call_provider.call_args.kwargs["max_tokens"], 32)

    def test_expired_budget_never_consumes_dns_thread_capacity(self):
        original_slots = translator._DNS_RESOLVER_SLOTS
        slots = threading.BoundedSemaphore(1)
        translator._DNS_RESOLVER_SLOTS = slots
        expired_budget = mock.Mock()
        expired_budget.ensure_active.side_effect = WorkBudgetExceeded("deadline")
        try:
            with (
                mock.patch.object(translator._socket, "getaddrinfo") as resolve,
                self.assertRaises(WorkBudgetExceeded),
            ):
                translator._bounded_getaddrinfo(
                    "expired.example.test", 443, budget=expired_budget
                )
            resolve.assert_not_called()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
        finally:
            translator._DNS_RESOLVER_SLOTS = original_slots

    def test_hung_custom_dns_resolution_has_bounded_thread_capacity(self):
        release = threading.Event()
        started = threading.Event()
        calls = 0

        def hung_resolution(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            release.wait(1)
            return []

        original_slots = translator._DNS_RESOLVER_SLOTS
        translator._DNS_RESOLVER_SLOTS = threading.BoundedSemaphore(2)
        try:
            with mock.patch.object(
                translator._socket, "getaddrinfo", side_effect=hung_resolution
            ):
                for _ in range(2):
                    with self.assertRaises((
                        translator._CustomEndpointDNSFailure,
                        WorkBudgetExceeded,
                    )):
                        translator._bounded_getaddrinfo(
                            "hung.example.test", 443,
                            budget=WorkBudget(
                                max_attempts=1,
                                max_input_bytes=1024,
                                max_output_tokens=1024,
                                deadline_seconds=0.02,
                            ),
                        )
                with self.assertRaisesRegex(
                    translator._CustomEndpointDNSFailure, "capacity"
                ):
                    translator._bounded_getaddrinfo("hung.example.test", 443)
                self.assertEqual(calls, 2)
        finally:
            release.set()
            self.assertTrue(translator._DNS_RESOLVER_SLOTS.acquire(timeout=1))
            self.assertTrue(translator._DNS_RESOLVER_SLOTS.acquire(timeout=1))
            translator._DNS_RESOLVER_SLOTS.release()
            translator._DNS_RESOLVER_SLOTS.release()
            translator._DNS_RESOLVER_SLOTS = original_slots

    def test_gemini_request_omits_deprecated_sampling_parameters(self):
        provider = translator._Provider(
            "gemini", "gemini-3.5-flash-lite", "gemini-key"
        )
        captured = {}

        class Response:
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "translated"}}]}

            def close(self):
                return None

        def provider_post(url, **kwargs):
            captured.update(
                {
                    "url": url,
                    "headers": kwargs["headers"],
                    "payload": kwargs["json"],
                }
            )
            return Response()

        with mock.patch.object(
            translator, "_provider_post", side_effect=provider_post
        ):
            result = translator._translate_openai(
                provider,
                "source text",
                "translate faithfully",
                timeout=5,
                max_tokens=256,
                budget=_budget(),
            )

        self.assertEqual(result, "translated")
        self.assertEqual(
            captured["url"],
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer gemini-key")
        self.assertEqual(
            captured["payload"],
            {
                "model": "gemini-3.5-flash-lite",
                "max_tokens": 256,
                "messages": [
                    {"role": "system", "content": "translate faithfully"},
                    {"role": "user", "content": "source text"},
                ],
            },
        )

    def test_non_gemini_openai_request_keeps_sampling_parameter(self):
        provider = translator._Provider("openai", "gpt-test", "openai-key")
        captured = {}

        class Response:
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "translated"}}]}

            def close(self):
                return None

        def provider_post(_url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

        with mock.patch.object(
            translator, "_provider_post", side_effect=provider_post
        ):
            result = translator._translate_openai(
                provider,
                "source text",
                "translate faithfully",
                timeout=5,
                max_tokens=256,
                budget=_budget(),
            )

        self.assertEqual(result, "translated")
        self.assertEqual(captured["temperature"], 0.3)

    def test_named_gemini_endpoint_is_fixed_and_remote(self):
        provider = translator._provider_from_config(
            name="gemini",
            model="gemini-3.5-flash-lite",
            api_key="gemini-key",
            custom_endpoint="",
            custom_api_key="",
        )

        self.assertEqual(
            provider.url,
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertEqual(provider.locality, "remote")
        self.assertEqual(provider.cache_namespace, "gemini")

    def test_openai_compatible_uses_dedicated_endpoint_key_and_cache_identity(self):
        public_dns = [
            (translator._socket.AF_INET, translator._socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        with mock.patch.object(translator._socket, "getaddrinfo", return_value=public_dns):
            first = translator._provider_from_config(
                name="openai-compatible",
                model="translation-model",
                api_key="",
                custom_endpoint="https://one.example.test/v1/chat/completions",
                custom_api_key="first-private-key",
            )
            second = translator._provider_from_config(
                name="openai-compatible",
                model="translation-model",
                api_key="",
                custom_endpoint="https://two.example.test/v1/chat/completions",
                custom_api_key="second-private-key",
            )

        self.assertEqual(first.url, "https://one.example.test/v1/chat/completions")
        self.assertEqual(first.api_key, "first-private-key")
        self.assertEqual(first.locality, "remote")
        self.assertNotEqual(first.cache_namespace, second.cache_namespace)
        self.assertNotIn(first.url, first.cache_namespace)

    def test_custom_endpoint_requires_https_exact_path_and_dedicated_key(self):
        invalid = (
            ("http://remote.example.test/v1/chat/completions", "key"),
            ("https://remote.example.test/v1/chat/completions/", "key"),
            ("https://remote.example.test/v1/messages", "key"),
            ("https://remote.example.test/v1/chat/completions", ""),
        )
        for endpoint, key in invalid:
            with self.subTest(endpoint=endpoint, has_key=bool(key)):
                with self.assertRaises(ValueError):
                    translator._provider_from_config(
                        name="openai-compatible",
                        model="translation-model",
                        api_key="",
                        custom_endpoint=endpoint,
                        custom_api_key=key,
                    )

        private_dns = [
            (translator._socket.AF_INET, translator._socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with (
            mock.patch.object(translator._socket, "getaddrinfo", return_value=private_dns),
            self.assertRaises(ValueError),
        ):
            translator._provider_from_config(
                name="openai-compatible",
                model="translation-model",
                api_key="",
                custom_endpoint="https://private.example.test/v1/chat/completions",
                custom_api_key="key",
            )

    def test_custom_transport_connects_to_vetted_ip_not_second_dns_answer(self):
        public_dns = [
            (translator._socket.AF_INET, translator._socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ]
        private_dns = [
            (translator._socket.AF_INET, translator._socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with mock.patch.object(
            translator._socket,
            "getaddrinfo",
            side_effect=[public_dns, private_dns],
        ) as resolver:
            _endpoint, hostname, addresses = translator._resolve_custom_endpoint(
                "https://rebind.example.test/v1/chat/completions"
            )
            translator._HTTP_CALL_CONTEXT.pinned_host = hostname
            translator._HTTP_CALL_CONTEXT.pinned_addresses = addresses
            sentinel_socket = object()
            try:
                connection = translator._DeadlineHTTPSConnection(
                    hostname, 443, timeout=1
                )
                with mock.patch.object(
                    translator._urllib3_connection,
                    "create_connection",
                    return_value=sentinel_socket,
                ) as connect:
                    result = connection._new_conn()
            finally:
                translator._HTTP_CALL_CONTEXT.__dict__.pop("pinned_host", None)
                translator._HTTP_CALL_CONTEXT.__dict__.pop(
                    "pinned_addresses", None
                )

        self.assertIs(result, sentinel_socket)
        self.assertEqual(connect.call_args.args[0], ("8.8.8.8", 443))
        self.assertEqual(resolver.call_count, 1)

    def test_transient_custom_dns_failure_uses_local_fallback(self):
        primary = translator._Provider(
            "openai-compatible",
            "custom-model",
            "custom-key",
            spec=translator.ProviderSpec(
                provider_id="openai-compatible",
                endpoint="https://unstable.example.test/v1/chat/completions",
                protocol="openai",
                locality="remote",
                cache_namespace="openai-compatible:test",
            ),
        )
        fallback = translator._Provider("local", "gemma4-12b", "")

        class LocalResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "local result"}}]}

            def close(self):
                return None

        calls = []

        def local_only(provider_url, **_kwargs):
            calls.append(provider_url)
            return LocalResponse()

        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
            mock.patch.object(
                translator,
                "_bounded_getaddrinfo",
                side_effect=translator._CustomEndpointDNSFailure("synthetic"),
            ),
            mock.patch.object(translator, "_provider_post", side_effect=local_only),
            mock.patch.object(translator.time, "sleep", return_value=None),
        ):
            translated, backend = translator._complete(
                "private text", "system", max_retries=1, budget=_budget()
            )

        self.assertEqual((translated, backend), ("local result", "local"))
        self.assertEqual(calls, [translator.PROVIDER_ENDPOINTS["local"][0]])

    def test_custom_dns_private_answer_is_terminal_without_fallback(self):
        primary = translator._Provider(
            "openai-compatible",
            "custom-model",
            "custom-key",
            spec=translator.ProviderSpec(
                provider_id="openai-compatible",
                endpoint="https://rebind.example.test/v1/chat/completions",
                protocol="openai",
                locality="remote",
                cache_namespace="openai-compatible:test",
            ),
        )
        fallback = translator._Provider("local", "gemma4-12b", "")
        private_dns = [
            (translator._socket.AF_INET, translator._socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        ]
        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
            mock.patch.object(
                translator._socket, "getaddrinfo", return_value=private_dns
            ),
            mock.patch.object(translator, "_provider_post") as provider_post,
            self.assertRaises(translator.ProviderUnavailableError),
        ):
            translator._complete(
                "private text", "system", max_retries=1, budget=_budget()
            )

        provider_post.assert_not_called()

    def test_terminal_primary_4xx_never_fails_over(self):
        primary = translator._Provider("gemini", "gemini-3.5-flash-lite", "key")
        fallback = translator._Provider("local", "gemma4-12b", "")
        calls = []

        def fail(provider, *_args, **_kwargs):
            calls.append(provider.name)
            raise translator._ProviderCallError(provider.name, 401, "HTTPError")

        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
            mock.patch.object(translator, "_call_provider", side_effect=fail),
            self.assertRaises(translator.ProviderUnavailableError),
        ):
            translator._complete("private text", "system", budget=_budget())

        self.assertEqual(calls, ["gemini"])

    def test_transient_primary_failure_uses_local_fallback_without_consent(self):
        primary = translator._Provider("gemini", "gemini-3.5-flash-lite", "key")
        fallback = translator._Provider("local", "gemma4-12b", "")
        calls = []

        def complete(provider, *_args, **_kwargs):
            calls.append(provider.name)
            if provider.name == "gemini":
                raise translator._ProviderCallError(provider.name, 503, "HTTPError")
            return "translated"

        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
            mock.patch.object(translator, "_call_provider", side_effect=complete),
        ):
            translated, backend = translator._complete(
                "private text", "system", budget=_budget()
            )

        self.assertEqual((translated, backend), ("translated", "local"))
        self.assertEqual(calls, ["gemini", "local"])

    def test_provider_policy_exposes_only_locality(self):
        primary = translator._Provider("gemini", "gemini-3.5-flash-lite", "key")
        fallback = translator._Provider("local", "gemma4-12b", "")
        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
        ):
            policy = translator.provider_policy()

        self.assertEqual(policy, {"primary": "remote", "fallback": "local"})
        encoded = repr(policy)
        self.assertNotIn("gemini", encoded)
        self.assertNotIn("gemma", encoded)
        self.assertNotIn("http", encoded)
        self.assertNotIn("key", encoded)


def _local(endpoint: str) -> translator._Provider:
    return translator._Provider(
        "local",
        "local-model",
        "",
        spec=translator.ProviderSpec(
            provider_id="local",
            endpoint=endpoint,
            protocol="openai",
            locality="local",
            cache_namespace="local",
        ),
    )


def _answers(*addresses: str) -> list[tuple]:
    return [
        (0, translator._socket.SOCK_STREAM, 6, "", (address, 8000))
        for address in addresses
    ]


class LocalProviderLocalityTests(unittest.TestCase):
    def initialize(self, primary, fallback=None, answers=None):
        resolve = mock.Mock(return_value=answers or [])
        with (
            mock.patch.object(translator, "_get_primary", return_value=primary),
            mock.patch.object(translator, "_get_fallback", return_value=fallback),
            mock.patch.object(translator._socket, "getaddrinfo", resolve),
        ):
            translator.initialize_provider_configuration()
        return resolve

    def test_public_local_address_logs_a_privacy_warning(self):
        with self.assertLogs("book-translator.translator", "WARNING") as logs:
            self.initialize(_local("http://8.8.8.8:8000/v1/chat/completions"))
        self.assertIn("8.8.8.8", logs.output[0])
        self.assertIn("public", logs.output[0])

    def test_local_host_resolving_only_to_public_addresses_warns(self):
        with self.assertLogs("book-translator.translator", "WARNING") as logs:
            self.initialize(
                translator._Provider("gemini", "gemini-3.5-flash-lite", "key"),
                fallback=_local("https://llm.example.test/v1/chat/completions"),
                answers=_answers("1.1.1.1", "2606:4700:4700::1111"),
            )
        self.assertIn("llm.example.test", logs.output[0])

    def test_private_or_mixed_local_answers_stay_quiet(self):
        for answers in (
            _answers("192.168.0.122"),
            _answers("100.101.102.103"),
            _answers("fd12:3456::1"),
            _answers("192.168.0.122", "2a01:4f8::1"),
        ):
            with (
                self.subTest(answers=answers),
                self.assertNoLogs("book-translator.translator", "WARNING"),
            ):
                self.initialize(
                    _local("http://llm.lan:8000/v1/chat/completions"),
                    answers=answers,
                )

    def test_unresolvable_local_host_does_not_block_startup(self):
        with (
            mock.patch.object(translator._socket, "getaddrinfo",
                              side_effect=OSError("no DNS yet")),
            mock.patch.object(translator, "_get_primary",
                              return_value=_local(
                                  "http://llm.lan:8000/v1/chat/completions")),
            mock.patch.object(translator, "_get_fallback", return_value=None),
            self.assertNoLogs("book-translator.translator", "WARNING"),
        ):
            translator.initialize_provider_configuration()

    def test_remote_providers_are_not_resolved(self):
        resolve = self.initialize(
            translator._Provider("gemini", "gemini-3.5-flash-lite", "key")
        )
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
