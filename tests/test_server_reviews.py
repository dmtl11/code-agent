from __future__ import annotations

import json
import shutil
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.server import DemoHandler
from code_agent.session_store import SessionStore
import code_agent.server as server_module
from http.server import ThreadingHTTPServer


class ReviewEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(__file__).resolve().parent / "_work" / self._testMethodName
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True)
        self.store = SessionStore(self.workspace / "sessions.sqlite3")
        self.session_id = self.store.create_session(str(self.workspace))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store_factory = lambda: self.store
        self.store_patch = patch.object(server_module, "SessionStore", self.store_factory)
        self.store_patch.start()

    def tearDown(self) -> None:
        self.store_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.workspace.exists():
            shutil.rmtree(self.workspace)

    def test_merge_endpoint_combines_two_sequential_changes(self) -> None:
        path = self.workspace / "app.py"
        path.write_text("print(3)\n", encoding="utf-8")
        first_id = self.store.add_review_change(self.session_id, "run-1", "app.py", "print(1)\n", "print(2)\n")
        second_id = self.store.add_review_change(self.session_id, "run-2", "app.py", "print(2)\n", "print(3)\n")

        url = f"http://127.0.0.1:{self.server.server_address[1]}/api/reviews/merge"
        request = urllib.request.Request(
            url,
            data=json.dumps({"session_id": self.session_id, "change_ids": [first_id, second_id]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), "print(3)\n")
        self.assertTrue(all(item["status"] == "merged" for item in self.store.list_review_changes(self.session_id)))

    def test_monitoring_endpoint_returns_project_metrics(self) -> None:
        self.store.record_metric(
            self.session_id,
            {
                "kind": "llm_call",
                "model": "deepseek-chat",
                "provider": "deepseek",
                "ok": True,
                "latency_ms": 42,
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                "usage_source": "actual",
            },
        )
        url = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
            f"/api/monitoring?workspace={urllib.parse.quote(str(self.workspace))}"
        )
        with urllib.request.urlopen(url) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total_tokens"], 11)
        self.assertEqual(payload["providers"][0]["name"], "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
