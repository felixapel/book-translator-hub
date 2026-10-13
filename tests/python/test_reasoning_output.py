"""Reasoning-model output: a leading <think> block is never a translation."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import translator
from tests.python import test_stream_provider as helpers


def openai_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class SingleResponseReasoningTests(unittest.TestCase):
    def call(self, content: str, source: str = "The house is red.") -> str:
        provider = translator._Provider("local", "local-model", "")
        with mock.patch.object(
            translator,
            "_provider_post",
            return_value=helpers.JsonResponse(openai_body(content)),
        ):
            return translator._call_provider(
                provider, source, "system", 1, 5, 64, helpers.budget(),
            )

    def test_leading_reasoning_block_is_removed(self):
        self.assertEqual(
            self.call("<think>\nThe user wants Spanish.\n</think>\n\nLa casa es roja."),
            "La casa es roja.",
        )

    def test_unclosed_reasoning_block_is_rejected(self):
        with self.assertRaises(translator._ProviderCallError) as raised:
            self.call("<think>\nThe user wants Spanish. The house")
        self.assertEqual(raised.exception.error_type, "ValueError")

    def test_reasoning_with_no_translation_is_rejected(self):
        with self.assertRaises(translator._ProviderCallError):
            self.call("<think>The user wants Spanish.</think>\n")

    def test_tag_inside_the_translation_is_kept(self):
        self.assertEqual(
            self.call("Escribió <think> en la pizarra."),
            "Escribió <think> en la pizarra.",
        )

    def test_literal_tag_at_the_start_of_the_source_is_translated(self):
        self.assertEqual(
            self.call(
                "<think>Recuérdame.</think> Cerró el cuaderno.",
                source="<think>Remember me.</think> She closed the notebook.",
            ),
            "<think>Recuérdame.</think> Cerró el cuaderno.",
        )

    def test_reasoning_before_a_segment_envelope_is_removed(self):
        envelope = (
            '{"protocol": "%s", "translations": [{"id": "s0", "text": "Hola"}]}'
            % translator.SEGMENT_PROTOCOL
        )
        output = self.call("<think>Two segments? One.</think>" + envelope)
        self.assertEqual(
            translator._parse_segment_envelope(output, ["s0"]), ["Hola"]
        )


class StreamReasoningTests(unittest.TestCase):
    def run_stream(
        self, primary_chunks, fallback_chunks=None, text="The house is red."
    ):
        primary = translator._Provider("local", "local-model", "")
        fallback = translator._Provider("openai", "gpt-test", "key")

        def post(url, **_kwargs):
            if url == primary.url:
                return helpers.StreamResponse(primary_chunks)
            return helpers.StreamResponse(fallback_chunks)

        with (
            mock.patch.object(translator, "_primary_provider", primary),
            mock.patch.object(
                translator,
                "_fallback_provider",
                fallback if fallback_chunks is not None else None,
            ),
            mock.patch.object(translator, "_provider_post", side_effect=post),
        ):
            return list(translator.translate_text_stream(
                text, "English", "Spanish",
                budget=helpers.budget(),
                allow_cloud_fallback=fallback_chunks is not None,
            ))

    def test_reasoning_split_across_deltas_is_never_streamed(self):
        result = self.run_stream(helpers.sse(
            "<thi", "nk>The user wants ", "Spanish.</th", "ink>\n\nLa casa", " es roja."
        ))
        self.assertEqual(result, [("La casa", "local"), (" es roja.", "local")])

    def test_text_without_reasoning_streams_unchanged(self):
        result = self.run_stream(helpers.sse("<", "b>La casa", " es roja."))
        self.assertEqual(
            result, [("<b>La casa", "local"), (" es roja.", "local")]
        )

    def test_unclosed_reasoning_falls_back_before_any_output(self):
        result = self.run_stream(
            helpers.sse("<think>", "The user wants"),
            fallback_chunks=helpers.sse("La casa es roja."),
        )
        self.assertEqual(result, [("La casa es roja.", "openai")])

    def test_unclosed_reasoning_without_fallback_is_unavailable(self):
        with self.assertRaises(translator.ProviderUnavailableError):
            self.run_stream(helpers.sse("<think>", "The user wants"))

    def test_opening_tag_alone_is_never_streamed(self):
        result = self.run_stream(
            helpers.sse("<think>"),
            fallback_chunks=helpers.sse("La casa es roja."),
        )
        self.assertEqual(result, [("La casa es roja.", "openai")])

    def test_literal_tag_in_the_source_streams_unchanged(self):
        result = self.run_stream(
            helpers.sse("<think>Recuérdame.</think>", " Cerró el cuaderno."),
            text="<think>Remember me.</think> She closed the notebook.",
        )
        self.assertEqual(
            "".join(delta for delta, _ in result),
            "<think>Recuérdame.</think> Cerró el cuaderno.",
        )

    def test_held_whitespace_counts_against_the_response_cap(self):
        chunks = helpers.sse(*([" " * 20] * 8), "<think>x</think>Hola")
        with mock.patch.object(translator, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 128):
            with self.assertRaises(translator.ProviderUnavailableError):
                self.run_stream(chunks)

    def test_reasoning_closed_in_the_delta_that_crosses_the_cap_is_rejected(self):
        chunks = helpers.sse("<think>" + "a" * 60, "b" * 60 + "</think>Hola")
        with mock.patch.object(translator, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 128):
            with self.assertRaises(translator.ProviderUnavailableError):
                self.run_stream(chunks)

    def test_reasoning_is_held_only_up_to_the_response_cap(self):
        chunks = helpers.sse(
            "<think>", "a" * 40, "b" * 40, "c" * 40, "d" * 40, "</think>Hola"
        )
        with mock.patch.object(translator, "BT_MAX_UPSTREAM_RESPONSE_BYTES", 128):
            with self.assertRaises(translator.ProviderUnavailableError):
                self.run_stream(chunks)


if __name__ == "__main__":
    unittest.main()
