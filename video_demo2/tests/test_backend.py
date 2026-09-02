#!/usr/bin/env python3
"""Tests for the snake backend (score saving + leaderboard API).

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib import request, error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import server  # noqa: E402


class ScoreLogicTests(unittest.TestCase):
    def setUp(self):
        # Use a temp DB so tests never touch the real one.
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db = server.DB_PATH
        server.DB_PATH = self.db_path
        server.init_db()

    def tearDown(self):
        server.DB_PATH = self._orig_db
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_add_and_leaderboard_order(self):
        server.add_score("Alice", 30)
        server.add_score("Bob", 100)
        server.add_score("Carol", 50)
        rows = server.get_leaderboard()
        self.assertEqual([r["name"] for r in rows], ["Bob", "Carol", "Alice"])
        self.assertEqual([r["score"] for r in rows], [100, 50, 30])

    def test_leaderboard_limit(self):
        for i in range(15):
            server.add_score("P%d" % i, i)
        rows = server.get_leaderboard()
        self.assertEqual(len(rows), 10)
        # highest scores first
        self.assertEqual(rows[0]["score"], 14)

    def test_validate_score_ok(self):
        self.assertEqual(server.validate_score({"name": "小明", "score": 120}), ("小明", 120))
        self.assertEqual(server.validate_score({"name": "  Alice  ", "score": 0}), ("Alice", 0))

    def test_validate_score_rejects(self):
        bad = [
            {"name": "", "score": 10},
            {"name": "   ", "score": 10},
            {"name": "a" * 30, "score": 10},
            {"name": "bad<tag>", "score": 10},
            {"name": "Alice", "score": -1},
            {"name": "Alice", "score": server.MAX_SCORE + 1},
            {"name": "Alice", "score": "10"},
            {"name": "Alice", "score": True},
            {"name": 123, "score": 10},
            {"name": "Alice"},  # missing score
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    server.validate_score(payload)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._orig_db = server.DB_PATH
        server.DB_PATH = cls.db_path
        server.init_db()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.SnakeHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.DB_PATH = cls._orig_db
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base + "/api/scores",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, json.loads(body)

    def _get(self, path):
        with request.urlopen(self.base + path) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, data = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_save_and_list(self):
        status, data = self._post({"name": "Tester", "score": 250})
        self.assertEqual(status, 201)
        self.assertTrue(data["ok"])

        status, data = self._get("/api/scores")
        self.assertEqual(status, 200)
        names = [s["name"] for s in data["scores"]]
        self.assertIn("Tester", names)
        entry = next(s for s in data["scores"] if s["name"] == "Tester")
        self.assertEqual(entry["score"], 250)

    def test_invalid_payload_rejected(self):
        status, data = self._post({"name": "X", "score": -5})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        status, data = self._post({"name": "", "score": 5})
        self.assertEqual(status, 400)

        status, data = self._post("not json")
        self.assertEqual(status, 400)

    def test_static_index_served(self):
        with request.urlopen(self.base + "/") as resp:
            body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("贪吃蛇", body)

    def test_unknown_api_404(self):
        try:
            request.urlopen(self.base + "/api/nope")
            self.fail("expected 404")
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


if __name__ == "__main__":
    unittest.main()
