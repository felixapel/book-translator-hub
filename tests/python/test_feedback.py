"""Regression tests for per-paragraph translation feedback.

Covers storage upsert/summary and tenant/book isolation, input validation,
and the /feedback + /feedback/summary HTTP endpoints.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BT_AUTH_MODE", "disabled")
os.environ.setdefault("BT_ALLOW_INSECURE_AUTH", "true")

import feedback
import server


def _temp_db():
    tmp = tempfile.TemporaryDirectory()
    patcher = mock.patch.dict(
        os.environ, {"DB_PATH": os.path.join(tmp.name, "feedback-test.db")}
    )
    patcher.start()
    return tmp, patcher


class FeedbackStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self._env = _temp_db()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_record_and_summary_roundtrip(self):
        self.assertEqual(
            feedback.record_feedback("t", "b", "para-1", 1),
            {"para_key": "para-1", "rating": 1},
        )
        feedback.record_feedback("t", "b", "para-2", -1)
        self.assertEqual(
            feedback.get_summary("t", "b"),
            {"up": 1, "down": 1, "total": 2, "score": 0},
        )

    def test_rating_strings_accepted(self):
        feedback.record_feedback("t", "b", "p1", "up")
        feedback.record_feedback("t", "b", "p2", "down")
        self.assertEqual(feedback.get_summary("t", "b")["total"], 2)

    def test_revote_replaces_rating(self):
        feedback.record_feedback("t", "b", "p", 1)
        feedback.record_feedback("t", "b", "p", -1)
        self.assertEqual(
            feedback.get_summary("t", "b"),
            {"up": 0, "down": 1, "total": 1, "score": -1},
        )

    def test_empty_summary_is_zeroed(self):
        self.assertEqual(
            feedback.get_summary("t", "unrated"),
            {"up": 0, "down": 0, "total": 0, "score": 0},
        )

    def test_entries_are_scoped_per_book_and_tenant(self):
        feedback.record_feedback("t1", "book-1", "p", 1)
        self.assertEqual(
            feedback.get_summary("t1", "book-2")["total"], 0)
        self.assertEqual(
            feedback.get_summary("t2", "book-1")["total"], 0)

    def test_raw_identifiers_are_not_stored(self):
        feedback.record_feedback("tenant-secret", "book-secret", "p", 1)
        with open(os.environ["DB_PATH"], "rb") as handle:
            blob = handle.read()
        self.assertNotIn(b"tenant-secret", blob)
        self.assertNotIn(b"book-secret", blob)

    def test_validation_rejects_bad_input(self):
        for key, rating in [
            ("", 1),
            ("   ", 1),
            ("k" * 129, 1),
            ("has\x00control", 1),
            (123, 1),
            ("ok", 0),
            ("ok", 2),
            ("ok", "maybe"),
            ("ok", None),
        ]:
            with self.assertRaises(ValueError, msg=f"{key!r}/{rating!r}"):
                feedback.record_feedback("t", "b", key, rating)


class FeedbackEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self._env = _temp_db()
        self.app = server.app.test_client()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_record_and_summary_roundtrip(self):
        scope = {"book_id": "book-f", "chapter_id": "ch-1"}
        posted = self.app.post(
            "/feedback",
            json={"para_key": "abc123", "rating": 1, **scope},
        )
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(
            posted.get_json()["feedback"],
            {"para_key": "abc123", "rating": 1},
        )
        summary = self.app.get(
            "/feedback/summary", query_string={"book_id": "book-f"}
        )
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(
            summary.get_json()["summary"],
            {"up": 1, "down": 0, "total": 1, "score": 1},
        )

    def test_revote_updates_summary(self):
        scope = {"book_id": "book-r", "chapter_id": "ch-1"}
        self.app.post(
            "/feedback", json={"para_key": "p", "rating": "up", **scope})
        self.app.post(
            "/feedback", json={"para_key": "p", "rating": "down", **scope})
        summary = self.app.get(
            "/feedback/summary", query_string={"book_id": "book-r"})
        self.assertEqual(
            summary.get_json()["summary"],
            {"up": 0, "down": 1, "total": 1, "score": -1},
        )

    def test_validation_errors_are_400(self):
        scope = {"book_id": "b", "chapter_id": "c"}
        cases = [
            {"rating": 1, **scope},  # missing para_key
            {"para_key": "p", **scope},  # missing rating
            {"para_key": "p", "rating": 0, **scope},
            {"para_key": "", "rating": 1, **scope},
            "not-a-dict",
        ]
        for body in cases:
            resp = self.app.post("/feedback", json=body)
            self.assertEqual(resp.status_code, 400, msg=f"{body!r}")

    def test_summary_scoped_per_book(self):
        self.app.post("/feedback", json={
            "para_key": "p", "rating": 1,
            "book_id": "book-a", "chapter_id": "c"})
        other = self.app.get(
            "/feedback/summary", query_string={"book_id": "book-b"})
        self.assertEqual(other.get_json()["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
