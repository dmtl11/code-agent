from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.agent import CodingAgent
from code_agent.session_store import SessionStore


class SessionFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(__file__).resolve().parent / "_work" / self._testMethodName
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True)
        self.db_path = self.workspace / "sessions.sqlite3"

    def tearDown(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace)

    def test_session_persists_memory_checkpoint_and_recovers_tool_state(self) -> None:
        store = SessionStore(self.db_path)
        session_id = store.create_session(str(self.workspace), "openai", "gpt-4o")
        store.append_message(session_id, {"role": "user", "content": "Inspect app.py"})
        store.append_message(session_id, {"role": "assistant", "content": "I inspected app.py."})
        store.update_memory(
            session_id,
            "Inspect app.py",
            "Inspection complete.",
            [{"role": "tool", "content": "read_file app.py"}],
        )
        store.save_checkpoint(
            session_id,
            "Earlier inspection was compacted.",
            [{"role": "assistant", "content": "tail"}],
            1234,
        )
        store.create_tool_call(session_id, "call-1", "read_file", {"path": "app.py"})
        store.update_tool_call(session_id, "call-1", "running")

        reopened = SessionStore(self.db_path)
        self.assertEqual(len(reopened.load_messages(session_id)), 2)
        self.assertEqual(reopened.get_memory(session_id)["goal"], "Inspect app.py")
        self.assertEqual(reopened.latest_checkpoint(session_id)["estimated_tokens"], 1234)
        recovered = reopened.recover_interrupted_tools(session_id)
        self.assertEqual(recovered[0]["call_id"], "call-1")
        self.assertIn("call-1 read_file", reopened.interrupted_tool_summary(session_id))

    def test_review_changes_can_be_listed_and_merged(self) -> None:
        store = SessionStore(self.db_path)
        session_id = store.create_session(str(self.workspace))
        first_id = store.add_review_change(session_id, "run-1", "app.py", "print(1)\n", "print(2)\n")
        second_id = store.add_review_change(session_id, "run-2", "app.py", "print(2)\n", "print(3)\n")

        reviews = store.list_review_changes(session_id)
        self.assertEqual({review["id"] for review in reviews}, {first_id, second_id})
        self.assertEqual(reviews[0]["after_preview"], "print(3)\n")
        self.assertIn("--- a/app.py", reviews[0]["diff"])
        self.assertIn("+print(3)", reviews[0]["diff"])
        store.mark_review_changes_merged(session_id, [first_id, second_id])
        self.assertTrue(all(review["status"] == "merged" for review in store.list_review_changes(session_id)))

    def test_monitoring_summary_aggregates_usage_errors_and_latency(self) -> None:
        store = SessionStore(self.db_path)
        session_id = store.create_session(str(self.workspace))
        store.record_metric(
            session_id,
            {
                "kind": "llm_call",
                "provider": "openai",
                "model": "gpt-4o",
                "ok": True,
                "latency_ms": 100,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "usage_source": "actual",
            },
        )
        store.record_metric(
            session_id,
            {
                "kind": "llm_error",
                "provider": "openai",
                "model": "gpt-4o",
                "ok": False,
                "latency_ms": 200,
                "error": "timeout",
            },
        )
        store.record_metric(session_id, {"kind": "tool_result", "name": "read_file", "ok": True})
        store.record_metric(session_id, {"kind": "context", "ok": True, "compacted_blocks": 2})
        summary = store.monitoring_summary(session_id=session_id)

        self.assertEqual(summary["summary"]["llm_requests"], 2)
        self.assertEqual(summary["summary"]["llm_error_rate"], 50.0)
        self.assertEqual(summary["summary"]["total_tokens"], 15)
        self.assertEqual(summary["summary"]["compactions"], 2)
        self.assertEqual(summary["providers"][0]["name"], "gpt-4o")

    def test_different_models_share_one_session_memory(self) -> None:
        store = SessionStore(self.db_path)
        session_id = store.create_session(str(self.workspace))

        class FinishModel:
            def __init__(self, label: str) -> None:
                self.label = label
                self.last_messages = []

            def complete(self, messages, tools):
                self.last_messages = messages
                return {
                    "role": "assistant",
                    "content": f"{self.label} is ready.",
                    "tool_calls": [
                        {
                            "id": f"finish-{self.label}",
                            "type": "function",
                            "function": {
                                "name": "finish",
                                "arguments": json.dumps({"summary": f"{self.label} finished."}),
                            },
                        }
                    ],
                }

        first_model = FinishModel("ChatGPT")
        first = CodingAgent(
            self.workspace,
            provider="openai",
            model=first_model,
            session_store=store,
            session_id=session_id,
        )
        self.assertEqual(first.run("Create the API skeleton."), "ChatGPT finished.")

        second_model = FinishModel("Claude")
        second = CodingAgent(
            self.workspace,
            provider="claude",
            model=second_model,
            session_store=SessionStore(self.db_path),
            session_id=session_id,
        )
        history = store.load_messages(session_id)
        self.assertEqual(second.run("Review the API skeleton.", history=history), "Claude finished.")
        durable_memory = second_model.last_messages[3]["content"]
        self.assertIn("Create the API skeleton.", durable_memory)
        self.assertIn("ChatGPT finished.", durable_memory)
        self.assertEqual(len(SessionStore(self.db_path).load_messages(session_id)), 8)


if __name__ == "__main__":
    unittest.main()
