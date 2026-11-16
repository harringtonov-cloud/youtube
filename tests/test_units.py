"""Юнит-тесты без браузера: python -m unittest discover -s tests"""

import tempfile
import time
import unittest
from pathlib import Path

from app.db import Database
from app.sandbox.store import COMMENT_LIMIT, MAX_COMMENT_LEN, SandboxStore
from app.urlutils import InvalidYouTubeUrl, extract_video_id

VID = "dQw4w9WgXcQ"


class UrlTests(unittest.TestCase):
    def test_valid_forms(self):
        for url in (
            f"https://www.youtube.com/watch?v={VID}",
            f"https://youtube.com/watch?v={VID}&t=10",
            f"https://m.youtube.com/watch?v={VID}",
            f"https://youtu.be/{VID}?si=abc",
            f"https://www.youtube.com/shorts/{VID}",
            f"https://www.youtube.com/embed/{VID}",
            f"youtube.com/watch?v={VID}",
        ):
            self.assertEqual(extract_video_id(url), VID, url)

    def test_invalid(self):
        for url in (
            "", "   ", "https://example.com/watch?v=" + VID,
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/", "ftp://youtube.com/watch?v=" + VID,
            "https://evil.com/?u=https://youtube.com/watch?v=" + VID,
        ):
            with self.assertRaises(InvalidYouTubeUrl, msg=url):
                extract_video_id(url)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = SandboxStore(view_threshold_ms=500)

    def test_view_rules(self):
        self.assertEqual(self.s.view(VID, "a")["reason"], "not_played")
        self.s.play(VID, "a")
        self.assertEqual(self.s.view(VID, "a")["reason"], "too_early")
        time.sleep(0.6)
        self.assertTrue(self.s.view(VID, "a")["counted"])
        self.assertEqual(self.s.view(VID, "a")["reason"], "already_counted")
        self.assertEqual(self.s.counters(VID)["views"], 1)

    def test_like_idempotent(self):
        self.s.set_like(VID, "a", True)
        self.s.set_like(VID, "a", True)
        self.assertEqual(self.s.counters(VID)["likes"], 1)
        self.s.set_like(VID, "b", True)
        self.assertEqual(self.s.counters(VID)["likes"], 2)
        self.s.set_like(VID, "a", False)
        self.assertEqual(self.s.counters(VID)["likes"], 1)

    def test_comments(self):
        self.assertEqual(self.s.add_comment(VID, "a", "  ")[0], 400)
        self.assertEqual(self.s.add_comment(VID, "a", "x" * (MAX_COMMENT_LEN + 1))[0], 400)
        self.assertEqual(self.s.add_comment(VID, "a", 123)[0], 400)
        for _ in range(COMMENT_LIMIT):
            self.assertEqual(self.s.add_comment(VID, "a", "hi")[0], 201)
        self.assertEqual(self.s.add_comment(VID, "a", "hi")[0], 429)
        self.assertEqual(self.s.add_comment(VID, "b", "hi")[0], 201)

    def test_flaky(self):
        self.s.arm_flaky(2)
        self.assertEqual([self.s.consume_flaky() for _ in range(3)], [True, True, False])
        self.assertEqual(self.s.flaky_hits, 2)


class DbTests(unittest.TestCase):
    def test_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.sqlite3")
            db.create_run("a" * 12, "url", VID, ["views"])
            db.add_log("a" * 12, "info", "views", "hello")
            db.add_scenario_result("a" * 12, "views", "passed", 1, 5, "ok")
            db.update_run("a" * 12, status="passed", summary={"passed": 1, "total": 1})
            run = db.get_run("a" * 12)
            self.assertEqual(run["status"], "passed")
            self.assertEqual(run["results"][0]["name"], "views")
            self.assertEqual(run["summary"]["passed"], 1)
            self.assertEqual(len(db.get_logs("a" * 12)), 1)
            self.assertEqual(db.clear_finished(), ["a" * 12])
            self.assertIsNone(db.get_run("a" * 12))
            db.close()


if __name__ == "__main__":
    unittest.main()
