"""Provider-side contracts for single-text SSE streaming."""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import requests

import translator
from work_budget import WorkBudget, WorkBudgetExceeded


def budget() -> WorkBudget:
    return WorkBudget(
        max_attempts=20,
        max_input_bytes=1_000_000,
        max_output_tokens=100_000,
        deadline_seconds=30,
    )


def sse(*deltas: str, done: bool = True) -> list[bytes]:
    lines = [
        b"data: " + json.dumps(
            {"choices": [{"delta": {"content": delta}}]}
        ).encode("utf-8") + b"\n\n"
        for delta in deltas
    ]
    if done:
        lines.append(b"data: [DONE]\n\n")
    return lines


class StreamResponse:
    """Streaming double that can fail after emitting some chunks."""

    def __init__(
        self, chunks, *, fail_after=None, failure=None, status_code=200, headers=None
    ):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.failure = failure or requests.exceptions.ChunkedEncodingError(
            "connection reset"
        )
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=self
            )

    def iter_content(self, chunk_size=1):
        for index, chunk in enumerate(self.chunks):
            if self.fail_after is not None and index == self.fail_after:
                raise self.failure
            yield chunk

    def close(self):
        self.closed = True


class JsonResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


def custom_provider() -> translator._Provider:
    return translator._Provider(
        "openai-compatible",
        "custom-model",
        "custom-key",
        spec=translator.ProviderSpec(
            provider_id="openai-compatible",
            endpoint="https://llm.example.test/v1/chat/completions",
            protocol="openai",
            locality="remote",
            cache_namespace="openai-compatible:test",
        ),
    )


class StreamProviderTests(unittest.TestCase):
    def providers(self, primary, fallback=None):
        return (
            mock.patch.object(translator, "_primary_provider", primary),
            mock.patch.object(translator, "_fallback_provider", fallback),
        )

    def run_stream(self, **kwargs):
        return list(translator.translate_text_stream(
            "The house is red.", "English", "Spanish",
            budget=budget(), **kwargs,
        ))

    def test_non_streaming_provider_needs_only_one_upstream_slot(self):
        primary = translator._Provider("anthropic", "claude-test", "key")
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(
                translator, "_UPSTREAM_SEM", threading.BoundedSemaphore(1)
            ),
            mock.patch.object(translator, "BT_UPSTREAM_QUEUE_TIMEOUT", 0.2),
            mock.patch.object(
                translator,
                "_provider_post",
                return_value=JsonResponse(
                    {"content": [{"type": "text", "text": "La casa es roja."}]}
                ),
            ),
        ):
            result = self.run_stream()
        self.assertEqual(result, [("La casa es roja.", "anthropic")])

    def test_failure_after_partial_output_never_appends_a_fallback(self):
        primary = translator._Provider("local", "local-model", "")
        fallback = translator._Provider("openai", "gpt-test", "key")
        posted = []

        def post(url, **_kwargs):
            posted.append(url)
            return StreamResponse(sse("La casa ", "es ", "roja"), fail_after=2)

        patch_primary, patch_fallback = self.providers(primary, fallback)
        received = []
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", side_effect=post),
        ):
            with self.assertRaises(translator.ProviderUnavailableError) as raised:
                for delta, provider in translator.translate_text_stream(
                    "The house is red.", "English", "Spanish",
                    budget=budget(), allow_cloud_fallback=True,
                ):
                    received.append((delta, provider))
        self.assertEqual(raised.exception.error_code, "provider_stream_interrupted")
        self.assertEqual(received, [("La casa ", "local"), ("es ", "local")])
        self.assertEqual(posted, [primary.url])

    def test_failure_before_output_still_uses_an_eligible_fallback(self):
        primary = translator._Provider("local", "local-model", "")
        fallback = translator._Provider("openai", "gpt-test", "key")

        def post(url, **_kwargs):
            if url == primary.url:
                return StreamResponse(
                    sse("ignored"),
                    fail_after=0,
                    failure=requests.exceptions.ConnectionError("refused"),
                )
            return StreamResponse(sse("La casa es roja."))

        patch_primary, patch_fallback = self.providers(primary, fallback)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", side_effect=post),
        ):
            result = self.run_stream(allow_cloud_fallback=True)
        self.assertEqual(result, [("La casa es roja.", "openai")])

    def test_stream_response_is_closed_after_completion(self):
        primary = translator._Provider("local", "local-model", "")
        response = StreamResponse(sse("Hola"))
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", return_value=response),
        ):
            self.assertEqual(self.run_stream(), [("Hola", "local")])
        self.assertTrue(response.closed)

    def test_oversized_sse_line_is_rejected_without_buffering_it(self):
        primary = translator._Provider("local", "local-model", "")
        response = StreamResponse([b"data: " + b"x" * 64, b"y" * 64])
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 32),
            mock.patch.object(translator, "_provider_post", return_value=response),
        ):
            with self.assertRaises(translator.ProviderUnavailableError):
                self.run_stream()
        self.assertTrue(response.closed)

    def test_complete_oversized_sse_line_is_rejected(self):
        primary = translator._Provider("local", "local-model", "")
        response = StreamResponse(sse("x" * 64))
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 32),
            mock.patch.object(translator, "_provider_post", return_value=response),
        ):
            with self.assertRaises(translator.ProviderUnavailableError):
                self.run_stream()
        self.assertTrue(response.closed)

    def test_deadline_stops_events_buffered_in_one_chunk(self):
        primary = translator._Provider("local", "local-model", "")
        request_budget = budget()
        response = StreamResponse([b"".join(sse("Hola", " mundo"))])
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", return_value=response),
        ):
            stream = translator.translate_text_stream(
                "Hello world", "English", "Spanish", budget=request_budget
            )
            self.assertEqual(next(stream), ("Hola", "local"))
            request_budget.cancel("deadline")
            with self.assertRaises(WorkBudgetExceeded):
                next(stream)
        self.assertTrue(response.closed)

    def test_custom_endpoint_stream_connects_only_to_vetted_addresses(self):
        primary = custom_provider()
        seen = {}

        def post(url, **_kwargs):
            seen["host"] = getattr(translator._HTTP_CALL_CONTEXT, "pinned_host", None)
            seen["addresses"] = getattr(
                translator._HTTP_CALL_CONTEXT, "pinned_addresses", None
            )
            return StreamResponse(sse("Hola"))

        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(
                translator,
                "_resolve_custom_endpoint",
                return_value=(primary.url, "llm.example.test", ("8.8.8.8",)),
            ),
            mock.patch.object(translator, "_provider_post", side_effect=post),
        ):
            self.assertEqual(
                self.run_stream(), [("Hola", "openai-compatible:test")]
            )
        self.assertEqual(seen, {"host": "llm.example.test", "addresses": ("8.8.8.8",)})
        self.assertIsNone(
            getattr(translator._HTTP_CALL_CONTEXT, "pinned_host", None)
        )

    def test_provider_rate_limit_is_reported_without_paid_fallback(self):
        primary = translator._Provider("openai", "gpt-test", "key")
        fallback = translator._Provider("anthropic", "claude-test", "key")
        posted = []

        def post(url, **_kwargs):
            posted.append(url)
            return StreamResponse(
                [], status_code=429, headers={"Retry-After": "7"}
            )

        patch_primary, patch_fallback = self.providers(primary, fallback)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", side_effect=post),
        ):
            with self.assertRaises(translator.ProviderUnavailableError) as raised:
                self.run_stream(allow_cloud_fallback=True)
        self.assertEqual(raised.exception.error_code, "provider_rate_limited")
        self.assertEqual(raised.exception.retry_after_seconds, 7)
        self.assertEqual(posted, [primary.url])

    def test_deadline_mid_stream_remains_request_fatal(self):
        primary = translator._Provider("local", "local-model", "")
        request_budget = budget()
        response = StreamResponse(sse("Hola", " mundo"))
        patch_primary, patch_fallback = self.providers(primary)
        with (
            patch_primary,
            patch_fallback,
            mock.patch.object(translator, "_provider_post", return_value=response),
        ):
            stream = translator.translate_text_stream(
                "Hello world", "English", "Spanish", budget=request_budget
            )
            self.assertEqual(next(stream), ("Hola", "local"))
            request_budget.cancel("deadline")
            with self.assertRaises(WorkBudgetExceeded):
                next(stream)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
