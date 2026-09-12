"""Cache/context contracts at the translator and HTTP orchestration boundary."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import server
import translator
from work_budget import WorkBudget, WorkBudgetExceeded


def budget() -> WorkBudget:
    return WorkBudget(
        max_attempts=20,
        max_input_bytes=1_000_000,
        max_output_tokens=100_000,
        deadline_seconds=30,
    )


def all_cache_miss(entries, *, record_hit=False):
    """Batch-probe stub: every entry misses, aligned with the request."""
    return [None] * len(entries)


class TranslationContractTests(unittest.TestCase):
    def test_groups_are_stable_over_empty_slots(self) -> None:
        self.assertEqual(
            translator.translation_groups(
                ["a", "", "b", "c", " ", "d"], batch_size=2
            ),
            [[0, 2], [3, 5]],
        )

    def test_adaptive_groups_respect_count_and_source_token_limits(self) -> None:
        self.assertEqual(
            translator.translation_groups(
                ["abcdefg", "日本語", "abcdefg", "", "日本語"],
                batch_size=3,
                source_token_budget=4,
            ),
            [[0, 1], [2, 4]],
        )

    def test_adaptive_groups_keep_an_oversized_paragraph_as_a_singleton(self) -> None:
        self.assertEqual(
            translator.translation_groups(
                ["日本語日本語日本語", "short"],
                batch_size=10,
                source_token_budget=4,
            ),
            [[0], [1]],
        )

    def test_zero_source_token_budget_preserves_count_only_grouping(self) -> None:
        paragraphs = ["a", "b", "c", "d"]
        self.assertEqual(
            translator.translation_groups(
                paragraphs, batch_size=3, source_token_budget=0
            ),
            translator.translation_groups(paragraphs, batch_size=3),
        )

    def test_source_token_budget_rejects_invalid_values(self) -> None:
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    translator.translation_groups(
                        ["paragraph"],
                        batch_size=5,
                        source_token_budget=invalid,
                    )

    def test_selected_groups_cannot_bypass_the_source_token_budget(self) -> None:
        with (
            mock.patch.object(translator, "BT_BATCH_SIZE", 10),
            mock.patch.object(translator, "BT_BATCH_SOURCE_TOKEN_BUDGET", 4),
            mock.patch.object(translator, "_translate_group") as translate_group,
        ):
            with self.assertRaisesRegex(ValueError, "selected_groups"):
                translator.translate_batch_detailed(
                    ["日本語日本語", "abcdefg"],
                    selected_groups=[[0, 1]],
                    budget=budget(),
                )
        translate_group.assert_not_called()

    def test_single_item_batch_without_context_uses_single_contract(self) -> None:
        with mock.patch.object(translator, "BT_CONTEXT_WINDOW", 0):
            single = translator.single_cache_contract("English", "Spanish")
            grouped = translator.batch_cache_contract(
                ["hello"], [0], "English", "Spanish"
            )
        self.assertEqual(grouped, single)

    def test_contract_changes_with_prompt_language_protocol_and_context(self) -> None:
        with mock.patch.object(translator, "BT_CONTEXT_WINDOW", 1):
            baseline = translator.batch_cache_contract(
                ["before", "one", "two", "after"],
                [1, 2],
                "English",
                "Spanish",
            )
            changed_context = translator.batch_cache_contract(
                ["different", "one", "two", "after"],
                [1, 2],
                "English",
                "Spanish",
            )
            changed_language = translator.batch_cache_contract(
                ["before", "one", "two", "after"],
                [1, 2],
                "English",
                "French",
            )

        self.assertNotEqual(baseline.context_hash, changed_context.context_hash)
        self.assertNotEqual(baseline.prompt_hash, changed_language.prompt_hash)
        self.assertEqual(baseline.protocol_version, translator.SEGMENT_PROTOCOL)


class ServerGroupCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace = {
            "tenant": "subject-42",
            "book_id": "book-7",
            "chapter_id": "chapter-3",
        }

    def test_partial_group_hit_retranslates_the_whole_original_group(self) -> None:
        def partial_hit(entries, *, record_hit):
            self.assertFalse(record_hit)
            return [
                "old-a" if text == "a" and scope.provider == "local" else None
                for text, _source, _target, scope in entries
            ]

        fresh = [
            translator.BatchTranslationItem("new-a", "local", True, "direct"),
            translator.BatchTranslationItem("new-b", "local", True, "direct"),
        ]
        with (
            mock.patch.object(server, "get_cached_many", side_effect=partial_hit),
            mock.patch.object(server, "put_cache_many") as put_cache_many,
            mock.patch.object(server, "translate_batch", return_value=fresh) as translate,
            mock.patch.object(
                server,
                "cache_lookup_backends",
                return_value=[("local", "gemma4-12b")],
            ),
        ):
            result = server._translate_paragraphs(
                ["a", "b"],
                "English",
                "Spanish",
                budget(),
                **self.namespace,
            )

        self.assertEqual(result["translations"], ["new-a", "new-b"])
        self.assertEqual(result["cached_count"], 0)
        self.assertEqual(result["fresh_count"], 2)
        translate.assert_called_once()
        self.assertEqual(translate.call_args.kwargs["selected_groups"], [[0, 1]])
        put_cache_many.assert_called_once()
        entries = put_cache_many.call_args.args[0]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            cache_scope = entry[4]
            self.assertEqual(cache_scope.tenant, "subject-42")
            self.assertEqual(cache_scope.book_id, "book-7")
            self.assertEqual(cache_scope.chapter_id, "chapter-3")

    def test_batch_result_exposes_aligned_sanitized_error_metadata(self) -> None:
        fresh = [
            translator.BatchTranslationItem(
                "translated-a", "gemini", True, "direct"
            ),
            translator.BatchTranslationItem(
                "[TRANSLATION ERROR: provider_rate_limited]",
                "",
                False,
                "failed",
                error_code="provider_rate_limited",
                retry_after_seconds=7,
            ),
        ]
        with (
            mock.patch.object(server, "get_cached_many", side_effect=all_cache_miss),
            mock.patch.object(server, "put_cache_many"),
            mock.patch.object(server, "translate_batch", return_value=fresh),
            mock.patch.object(
                server,
                "cache_lookup_backends",
                return_value=[("gemini", "gemini-test")],
            ),
        ):
            result = server._translate_paragraphs(
                ["a", "b"],
                "English",
                "Spanish",
                budget(),
                **self.namespace,
            )

        self.assertEqual(
            result["error_codes"], [None, "provider_rate_limited"]
        )
        self.assertEqual(result["retry_after_seconds"], [None, 7])

    def test_complete_group_hit_avoids_provider_work(self) -> None:
        hits = {"a": "cached-a", "b": "cached-b"}

        def complete_hit(entries, *, record_hit):
            self.assertTrue(entries)
            for _text, _source, _target, scope in entries:
                self.assertEqual(scope.provider, "local")
            self.assertFalse(record_hit)
            return [hits[text] for text, _s, _t, _scope in entries]

        with (
            mock.patch.object(server, "get_cached_many", side_effect=complete_hit),
            mock.patch.object(server, "translate_batch") as translate,
            mock.patch.object(server, "put_cache_many") as put_cache_many,
            mock.patch.object(server, "record_cache_hit") as record_hit,
            mock.patch.object(
                server,
                "cache_lookup_backends",
                return_value=[("local", "gemma4-12b")],
            ),
        ):
            result = server._translate_paragraphs(
                ["a", "b"],
                "English",
                "Spanish",
                budget(),
                **self.namespace,
            )

        self.assertEqual(result["translations"], ["cached-a", "cached-b"])
        self.assertEqual(result["cached_count"], 2)
        self.assertEqual(result["fresh_count"], 0)
        translate.assert_not_called()
        put_cache_many.assert_not_called()
        self.assertEqual(record_hit.call_count, 2)

    def test_individual_recovery_is_not_cached_as_batch_but_siblings_are(self) -> None:
        fresh = [
            translator.BatchTranslationItem(
                "new-a", "local", True, "direct"),
            translator.BatchTranslationItem(
                "new-b", "local", True, "direct"),
            translator.BatchTranslationItem(
                "new-c", "local", False, "paragraph_fallback"),
            translator.BatchTranslationItem(
                "new-d", "local", False, "paragraph_fallback"),
        ]
        with (
            mock.patch.object(translator, "BT_BATCH_SIZE", 2),
            mock.patch.object(server, "get_cached_many", side_effect=all_cache_miss),
            mock.patch.object(server, "put_cache_many") as put_cache_many,
            mock.patch.object(
                server, "translate_batch", return_value=fresh
            ) as translate,
            mock.patch.object(
                server,
                "cache_lookup_backends",
                return_value=[("local", "gemma4-12b")],
            ),
        ):
            result = server._translate_paragraphs(
                ["a", "b", "c", "d"],
                "English",
                "Spanish",
                budget(),
                **self.namespace,
            )

        self.assertEqual(
            result["translations"], ["new-a", "new-b", "new-c", "new-d"]
        )
        self.assertEqual(result["fresh_count"], 4)
        self.assertEqual(
            translate.call_args.kwargs["selected_groups"], [[0, 1], [2, 3]]
        )
        self.assertEqual(
            [entry[0] for entry in put_cache_many.call_args.args[0]], ["a", "b"]
        )

    def test_mid_recovery_budget_exhaustion_writes_no_partial_cache(self) -> None:
        calls = []

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, output: str) -> None:
                self._body = {
                    "choices": [{"message": {"content": output}}],
                }

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._body

        def malformed_then_single(_url, **kwargs):
            payload = kwargs["json"]
            system = payload["messages"][0]["content"]
            text = payload["messages"][1]["content"]
            if translator.SEGMENT_PROTOCOL in system:
                calls.append("batch")
                return Response("not-json")
            calls.append(f"single:{text}")
            return Response(f"translated:{text}")

        constrained_budget = WorkBudget(
            max_attempts=3,
            max_input_bytes=1_000_000,
            max_output_tokens=100_000,
            deadline_seconds=30,
        )
        primary = translator._Provider("local", "fake-model", "")
        server._reset_metrics_for_tests()
        try:
            with (
                mock.patch.object(translator, "_primary_provider", primary),
                mock.patch.object(translator, "_fallback_provider", None),
                mock.patch.object(
                    translator, "_provider_post", side_effect=malformed_then_single
                ),
                mock.patch.object(translator, "BT_BATCH_SIZE", 3),
                mock.patch.object(server, "get_cached_many", side_effect=all_cache_miss),
                mock.patch.object(server, "put_cache_many") as put_cache_many,
                mock.patch.object(
                    server,
                    "cache_lookup_backends",
                    return_value=[("local", "fake-model")],
                ),
            ):
                with self.assertRaises(WorkBudgetExceeded) as raised:
                    server._translate_paragraphs(
                        ["one", "two", "three"],
                        "English",
                        "Spanish",
                        constrained_budget,
                        tenant="subject-mid-recovery",
                        book_id="book-mid-recovery",
                        chapter_id="chapter-mid-recovery",
                    )

            self.assertEqual(raised.exception.reason, "attempts")
            self.assertEqual(calls, ["batch", "batch", "single:one"])
            put_cache_many.assert_not_called()
            with server._metrics_lock:
                recovery = dict(server._metrics["segment_recovery"])
            self.assertEqual(recovery, {
                "envelope_retry_groups": 1,
                "envelope_retry_recovered_groups": 0,
                "paragraph_fallback_groups": 1,
                "paragraph_fallback_recovered_segments": 1,
                "paragraph_fallback_failed_segments": 0,
            })
        finally:
            server._reset_metrics_for_tests()


if __name__ == "__main__":
    unittest.main(verbosity=2)
