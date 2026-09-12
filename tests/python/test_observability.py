"""Bounded observability contracts for every production failure boundary."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

from auth import AuthUnavailable, RequestAuthenticator
import server
import translator
from translator import BatchRecoveryTracker, ProviderUnavailableError
from work_budget import WorkBudgetExceeded


def all_cache_miss(entries, *, record_hit=False):
    """Batch-probe stub: every entry misses, aligned with the request."""
    return [None] * len(entries)


class ObservabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.original_authenticator = server.AUTHENTICATOR
        self.original_auth_limit = server.BT_AUTH_RATE_LIMIT_PER_MINUTE
        self.original_rate_limit = server.RATE_LIMIT_MAX
        server.AUTHENTICATOR = RequestAuthenticator(mode="disabled")
        server._auth_rate_limit_store.clear()
        server._rate_limit_store.clear()
        server._reset_metrics_for_tests()
        self.client = server.app.test_client()

    def tearDown(self):
        server.AUTHENTICATOR = self.original_authenticator
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = self.original_auth_limit
        server.RATE_LIMIT_MAX = self.original_rate_limit
        server._auth_rate_limit_store.clear()
        server._rate_limit_store.clear()
        server._reset_metrics_for_tests()

    def metrics(self, *, remote_addr: str = "127.0.0.1") -> dict:
        response = self.client.get(
            "/metrics",
            headers={"X-BT-Token": "observability-secret"},
            environ_base={"REMOTE_ADDR": remote_addr},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_auth_rejection_and_auth_rate_limit_are_counted(self):
        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="observability-secret"
        )
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = 1

        first = self.client.get(
            "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.10"}
        )
        second = self.client.get(
            "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.10"}
        )

        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 429)
        snapshot = self.metrics(remote_addr="198.51.100.11")
        self.assertEqual(snapshot["http_responses"]["4xx"], 2)
        self.assertEqual(snapshot["outcomes"]["auth_rejected"], 1)
        self.assertEqual(snapshot["outcomes"]["auth_rate_limited"], 1)

    def test_auth_authority_outage_is_counted(self):
        unavailable = mock.Mock(mode="cwa_session")
        unavailable.authenticate.side_effect = AuthUnavailable("private detail")
        server.AUTHENTICATOR = unavailable

        failed = self.client.get(
            "/metrics", environ_base={"REMOTE_ADDR": "198.51.100.12"}
        )
        self.assertEqual(failed.status_code, 503)

        server.AUTHENTICATOR = RequestAuthenticator(
            mode="token", api_token="observability-secret"
        )
        snapshot = self.metrics(remote_addr="198.51.100.13")
        self.assertEqual(snapshot["http_responses"]["5xx"], 1)
        self.assertEqual(snapshot["outcomes"]["auth_unavailable"], 1)

    def test_api_rate_limit_and_validation_failure_are_counted(self):
        server.RATE_LIMIT_MAX = 1

        accepted = self.client.post(
            "/translate",
            json={"text": "hello", "source_lang": "English", "target_lang": "English"},
            environ_base={"REMOTE_ADDR": "198.51.100.20"},
        )
        limited = self.client.post(
            "/translate",
            json={"text": "again", "source_lang": "English", "target_lang": "English"},
            environ_base={"REMOTE_ADDR": "198.51.100.20"},
        )
        # Disabled development auth intentionally has one shared subject.
        # Start a fresh API window so this assertion exercises validation,
        # not the already-proven subject quota above.
        server._rate_limit_store.clear()
        invalid = self.client.post(
            "/translate",
            json={"text": 17},
            environ_base={"REMOTE_ADDR": "198.51.100.21"},
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(invalid.status_code, 400)
        snapshot = self.metrics(remote_addr="198.51.100.22")
        self.assertEqual(snapshot["http_responses"]["2xx"], 1)
        self.assertEqual(snapshot["http_responses"]["4xx"], 2)
        self.assertEqual(snapshot["outcomes"]["api_rate_limited"], 1)

    def test_deadline_provider_and_partial_batch_failures_are_counted(self):
        with (
            mock.patch.object(server, "_cache_lookup", return_value=None),
            mock.patch.object(
                server,
                "translate_text",
                side_effect=WorkBudgetExceeded("deadline"),
            ),
        ):
            deadline = self.client.post("/translate", json={"text": "deadline"})

        with (
            mock.patch.object(server, "_cache_lookup", return_value=None),
            mock.patch.object(
                server,
                "translate_text",
                side_effect=ProviderUnavailableError("private detail"),
            ),
        ):
            provider = self.client.post("/translate", json={"text": "provider"})

        partial_result = {
            "translations": [
                "translated",
                "[TRANSLATION ERROR: provider_unavailable]",
                "[TRANSLATION ERROR: translation_failed]",
            ],
            "backends": ["local", "unknown", "unknown"],
            "cached": [False, False, False],
            "cached_count": 0,
            "fresh_count": 1,
            "total_elapsed_ms": 7,
        }
        with mock.patch.object(
            server, "_translate_paragraphs", return_value=partial_result
        ):
            partial = self.client.post(
                "/translate/batch", json={"paragraphs": ["one", "two", "three"]}
            )

        self.assertEqual(deadline.status_code, 503)
        self.assertEqual(provider.status_code, 502)
        self.assertEqual(partial.status_code, 200)
        snapshot = self.metrics(remote_addr="198.51.100.30")
        self.assertEqual(snapshot["http_responses"]["5xx"], 2)
        self.assertEqual(snapshot["outcomes"]["work_budget_exhausted"], 1)
        self.assertEqual(snapshot["work_budget_reasons"]["deadline"], 1)
        self.assertEqual(snapshot["outcomes"]["provider_unavailable"], 1)
        self.assertEqual(snapshot["outcomes"]["batch_partial_failure_requests"], 1)
        self.assertEqual(snapshot["batch_partial_failure_segments"], 2)

    def test_segment_recovery_metrics_distinguish_retry_and_fallback(self):
        tracker = BatchRecoveryTracker()
        tracker.record("envelope_retry_groups", 2)
        tracker.record("envelope_retry_recovered_groups")
        tracker.record("paragraph_fallback_groups")
        tracker.record("paragraph_fallback_recovered_segments")
        tracker.record("paragraph_fallback_failed_segments")

        server._record_segment_recovery(tracker.snapshot())

        recovery = self.metrics()["segment_recovery"]
        self.assertEqual(recovery, {
            "envelope_retry_groups": 2,
            "envelope_retry_recovered_groups": 1,
            "paragraph_fallback_groups": 1,
            "paragraph_fallback_recovered_segments": 1,
            "paragraph_fallback_failed_segments": 1,
        })

    def test_batch_plan_metrics_use_only_fixed_buckets(self):
        paragraphs = ["a", "bb", "日本語" * 400]

        server._record_batch_plan(paragraphs, [[0, 1], [2]])

        snapshot = self.metrics()
        self.assertEqual(snapshot["batch_groups_total"], 2)
        self.assertEqual(snapshot["batch_paragraphs_total"], 3)
        self.assertEqual(snapshot["batch_group_size_buckets"]["single"], 1)
        self.assertEqual(snapshot["batch_group_size_buckets"]["2_4"], 1)
        self.assertEqual(
            snapshot["batch_group_source_token_buckets"]["up_to_128"], 1
        )
        self.assertEqual(
            snapshot["batch_group_source_token_buckets"]["over_600"], 1
        )

    def test_provider_attempt_metrics_distinguish_success_429_and_failure(self):
        translator._record_provider_call("attempt")
        translator._record_provider_call("success")
        translator._record_provider_call("attempt")
        translator._record_provider_call("rate_limited")
        translator._record_provider_call("failure")

        self.assertEqual(self.metrics()["provider_calls"], {
            "attempts": 2,
            "successes": 1,
            "rate_limited": 1,
            "failures": 1,
        })

    def test_stats_snapshot_is_cached_then_invalidated_by_writes(self):
        try:
            server._invalidate_stats_cache()
            first = self.client.get("/stats")
            self.assertEqual(first.status_code, 200)
            with mock.patch.object(
                server, "get_cache_stats", return_value={"total_entries": 999}
            ) as cache_stats:
                second = self.client.get("/stats")
                cache_stats.assert_not_called()
                self.assertEqual(second.get_json(), first.get_json())
            server._invalidate_stats_cache()
            with mock.patch.object(
                server, "get_cache_stats", return_value={"total_entries": 1}
            ) as cache_stats:
                third = self.client.get("/stats")
                cache_stats.assert_called_once()
                self.assertEqual(third.get_json(), {"total_entries": 1})
        finally:
            server._invalidate_stats_cache()

    def test_cache_lookup_failure_fails_open_to_provider(self):
        with (
            mock.patch.object(
                server, "get_cached", side_effect=RuntimeError("db locked")
            ),
            mock.patch.object(server, "translate_text", return_value=("hola", "local")),
            mock.patch.object(server, "put_cache"),
        ):
            response = self.client.post("/translate", json={"text": "hola"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["translated"], "hola")

    def test_recovery_metrics_survive_work_budget_failure(self):
        captured = {}

        def exhaust_after_recovery(*_args, recovery_tracker=None, **_kwargs):
            self.assertIsNotNone(recovery_tracker)
            captured["tracker"] = recovery_tracker
            recovery_tracker.record("envelope_retry_groups")
            raise WorkBudgetExceeded("attempts")

        with (
            mock.patch.object(server, "get_cached_many", side_effect=all_cache_miss),
            mock.patch.object(
                server, "translate_batch", side_effect=exhaust_after_recovery
            ),
        ):
            failed = self.client.post(
                "/translate/batch", json={"paragraphs": ["one", "two"]}
            )

        self.assertEqual(failed.status_code, 503)
        # A worker already beyond provider I/O may finish its CPU-only
        # bookkeeping after the coordinator has returned the bounded 503.
        captured["tracker"].record("paragraph_fallback_groups")
        recovery = self.metrics()["segment_recovery"]
        self.assertEqual(recovery["envelope_retry_groups"], 1)
        self.assertEqual(recovery["paragraph_fallback_groups"], 1)
        self.assertEqual(recovery["envelope_retry_recovered_groups"], 0)
        self.assertEqual(recovery["paragraph_fallback_recovered_segments"], 0)
        self.assertEqual(recovery["paragraph_fallback_failed_segments"], 0)

    def test_recovery_metric_failure_never_masks_work_budget_response(self):
        def exhaust_after_recovery(*_args, recovery_tracker=None, **_kwargs):
            self.assertIsNotNone(recovery_tracker)
            recovery_tracker.record("envelope_retry_groups")
            raise WorkBudgetExceeded("attempts")

        with (
            mock.patch.object(server, "get_cached_many", side_effect=all_cache_miss),
            mock.patch.object(
                server, "translate_batch", side_effect=exhaust_after_recovery
            ),
            mock.patch.object(
                server,
                "_record_segment_recovery",
                side_effect=RuntimeError("synthetic metric failure"),
            ),
        ):
            failed = self.client.post(
                "/translate/batch", json={"paragraphs": ["one", "two"]}
            )

        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.get_json()["error"], "work_budget_exhausted")
        self.assertEqual(failed.get_json()["reason"], "attempts")

    def test_metric_dimensions_are_fixed_and_reject_dynamic_labels(self):
        for latency_ms in (100, 300, 900, 1_700, 3_500, 7_000):
            server._record_metric(latency_ms)
        snapshot = self.metrics()

        self.assertEqual(
            set(snapshot["http_responses"]), {"2xx", "3xx", "4xx", "5xx"}
        )
        self.assertEqual(
            set(snapshot["work_budget_reasons"]),
            {"attempts", "input_bytes", "output_tokens", "deadline", "queue", "cancelled", "unknown"},
        )
        self.assertEqual(
            set(snapshot["outcomes"]),
            {
                "auth_rejected",
                "auth_unavailable",
                "auth_rate_limited",
                "api_rate_limited",
                "work_budget_exhausted",
                "provider_unavailable",
                "invalid_provider_response",
                "translation_failed",
                "internal_error",
                "batch_partial_failure_requests",
            },
        )
        self.assertEqual(
            set(snapshot["segment_recovery"]),
            {
                "envelope_retry_groups",
                "envelope_retry_recovered_groups",
                "paragraph_fallback_groups",
                "paragraph_fallback_recovered_segments",
                "paragraph_fallback_failed_segments",
            },
        )
        self.assertEqual(
            set(snapshot["batch_group_size_buckets"]),
            {"single", "2_4", "5_8", "9_10", "over_10"},
        )
        self.assertEqual(
            set(snapshot["batch_group_source_token_buckets"]),
            {"up_to_128", "up_to_256", "up_to_450", "up_to_600", "over_600"},
        )
        self.assertEqual(
            snapshot["translation_latency_ms_buckets"],
            {
                "up_to_250": 1,
                "up_to_500": 1,
                "up_to_1000": 1,
                "up_to_2500": 1,
                "up_to_5000": 1,
                "over_5000": 1,
            },
        )
        self.assertEqual(
            set(snapshot["provider_calls"]),
            {"attempts", "successes", "rate_limited", "failures"},
        )
        with self.assertRaises(ValueError):
            server._record_outcome("book-title-controlled-label")


if __name__ == "__main__":
    unittest.main(verbosity=2)
