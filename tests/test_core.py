from __future__ import annotations

import shutil
import unittest
from pathlib import Path
import sys
import json
from time import perf_counter


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.context import ContextManager
from code_agent.agent import CodingAgent
from code_agent.code_registry import CodeRegistry
from code_agent.repo_map import RepoMap
from code_agent.tools import LocalTools


class CoreFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(__file__).resolve().parent / "_work" / self._testMethodName
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace)

    def test_repo_map_extracts_python_symbols(self) -> None:
        (self.workspace / "service.py").write_text(
            "class Service:\n    def run(self):\n        return 1\n\ndef build():\n    return Service()\n",
            encoding="utf-8",
        )
        result = RepoMap(self.workspace).build("service run")
        self.assertIn("service.py", result)
        self.assertIn("class Service", result)
        self.assertIn("method Service.run", result)
        self.assertIn("function build", result)

    def test_code_registry_persists_and_updates_symbols_incrementally(self) -> None:
        path = self.workspace / "service.py"
        path.write_text("def old_name():\n    return 1\n", encoding="utf-8")
        registry = CodeRegistry(self.workspace)
        first = registry.sync()
        self.assertEqual(first.updated, 1)
        self.assertTrue((self.workspace / ".code_agent" / "code_registry.sqlite3").is_file())
        self.assertIn("old_name", registry.build_map("old_name"))

        path.write_text("def new_name():\n    return 2\n", encoding="utf-8")
        self.assertTrue(registry.update_file(path))
        reopened = CodeRegistry(self.workspace)
        self.assertIn("new_name", reopened.build_map("new_name"))
        self.assertNotIn("old_name", reopened.build_map("new_name"))

    def test_apply_patch_supports_dry_run_registry_update_and_rollback(self) -> None:
        path = self.workspace / "app.py"
        path.write_text("def old_name():\n    return 1\n", encoding="utf-8")
        tools = LocalTools(self.workspace)
        base_hash = tools.registry.file_hash("app.py")
        patch_text = """*** Begin Patch
*** Update File: app.py
@@
-def old_name():
+def new_name():
     return 1
*** Add File: helper.py
+def helper():
+    return 2
*** End Patch"""

        dry_run = tools.call(
            "apply_patch",
            {"patch": patch_text, "dry_run": True, "base_hashes": {"app.py": base_hash[:12]}},
        )
        self.assertTrue(dry_run.ok)
        self.assertIn("Dry run succeeded", dry_run.output)
        self.assertIn("old_name", path.read_text(encoding="utf-8"))
        self.assertFalse((self.workspace / "helper.py").exists())

        applied = tools.call(
            "apply_patch",
            {"patch": patch_text, "base_hashes": {"app.py": base_hash[:12]}},
        )
        self.assertTrue(applied.ok, applied.output)
        transaction_id = applied.output.split()[2]
        self.assertIn("new_name", tools.call("repo_map", {"query": "new_name"}).output)
        self.assertTrue((self.workspace / "helper.py").is_file())

        rolled_back = tools.call("rollback_patch", {"transaction_id": transaction_id})
        self.assertTrue(rolled_back.ok, rolled_back.output)
        self.assertIn("old_name", path.read_text(encoding="utf-8"))
        self.assertFalse((self.workspace / "helper.py").exists())

    def test_apply_patch_rejects_stale_hash_and_invalid_python_atomically(self) -> None:
        first = self.workspace / "first.py"
        second = self.workspace / "second.py"
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 2\n", encoding="utf-8")
        tools = LocalTools(self.workspace)
        stale_hash = tools.registry.file_hash("first.py")
        first.write_text("value = 9\n", encoding="utf-8")
        stale = tools.call(
            "apply_patch",
            {
                "patch": """*** Begin Patch
*** Update File: first.py
@@
-value = 9
+value = 10
*** End Patch""",
                "base_hashes": {"first.py": stale_hash},
            },
        )
        self.assertFalse(stale.ok)
        self.assertIn("Conflict", stale.output)
        self.assertEqual(first.read_text(encoding="utf-8"), "value = 9\n")

        invalid = tools.call(
            "apply_patch",
            {
                "patch": """*** Begin Patch
*** Update File: first.py
@@
-value = 9
+value = 10
*** Update File: second.py
@@
-value = 2
+def broken(:
*** End Patch"""
            },
        )
        self.assertFalse(invalid.ok)
        self.assertEqual(first.read_text(encoding="utf-8"), "value = 9\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "value = 2\n")

    def test_replace_in_file_requires_one_exact_match(self) -> None:
        path = self.workspace / "sample.py"
        path.write_text("value = 1\nprint(value)\n", encoding="utf-8")
        tools = LocalTools(self.workspace)

        result = tools.call(
            "replace_in_file",
            {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
        )
        self.assertTrue(result.ok)
        self.assertIn("value = 2", path.read_text(encoding="utf-8"))

        missing = tools.call(
            "replace_in_file",
            {"path": "sample.py", "old_text": "absent", "new_text": "x"},
        )
        self.assertFalse(missing.ok)
        self.assertIn("found 0", missing.output)

    def test_read_only_modes_reject_mutating_tools(self) -> None:
        tools = LocalTools(self.workspace, mode="ask")
        result = tools.call("write_file", {"path": "blocked.py", "content": "pass\n"})
        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "blocked.py").exists())

    def test_run_command_rejects_background_services_and_enforces_timeout(self) -> None:
        tools = LocalTools(self.workspace)
        background = tools.call("run_command", {"command": "start /b node server.js", "timeout": 5})
        self.assertFalse(background.ok)
        self.assertIn("Background shell syntax is not supported", background.output)
        self.assertIn("start_service", background.output)

        started = perf_counter()
        timed_out = tools.call(
            "run_command",
            {"command": 'python -c "import time; time.sleep(5)"', "timeout": 1},
        )
        self.assertFalse(timed_out.ok)
        self.assertIn("timed out after 1s", timed_out.output)
        self.assertLess(perf_counter() - started, 4)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_javascript_lint_reports_syntax_errors(self) -> None:
        (self.workspace / "broken.js").write_text("const value = ;\n", encoding="utf-8")
        result = LocalTools(self.workspace).call("lint_file", {"path": "broken.js"})
        self.assertFalse(result.ok)
        self.assertIn("SyntaxError", result.output)

    def test_context_manager_compacts_old_agent_blocks(self) -> None:
        manager = ContextManager(max_tokens=4000, reserve_tokens=1000)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "system", "content": "map"},
            {"role": "system", "content": "note"},
            {"role": "user", "content": "task"},
        ]
        for index in range(6):
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": f"read_{index}"}}],
                }
            )
            messages.append({"role": "tool", "content": "x" * 6000})

        compacted, stats = manager.compact_working_set(messages, 4, 2, [])
        self.assertGreater(stats.compacted_blocks, 0)
        self.assertLessEqual(stats.estimated_tokens, manager.target_tokens)
        self.assertIn("Compacted earlier agent activity", compacted[2]["content"])

    def test_default_context_budget_is_32k_with_output_reserve(self) -> None:
        manager = ContextManager()
        self.assertEqual(manager.max_tokens, 32000)
        self.assertEqual(manager.reserve_tokens, 8000)
        self.assertEqual(manager.target_tokens, 24000)

    def test_history_budget_keeps_tool_call_and_results_together(self) -> None:
        manager = ContextManager()
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"app.py"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-read",
                "name": "read_file",
                "content": "x" * 4000,
            },
            {"role": "user", "content": "How do I run the project?"},
        ]

        prepared, note = manager.prepare_history(history, max_tokens=50)

        self.assertEqual(prepared, [{"role": "user", "content": "How do I run the project?"}])
        self.assertIn("omitted", note)
        self.assertFalse(any(message.get("role") == "tool" for message in prepared))

    def test_history_cleanup_removes_orphaned_and_incomplete_tool_messages(self) -> None:
        manager = ContextManager()
        history = [
            {"role": "tool", "tool_call_id": "missing", "content": "orphan"},
            {
                "role": "assistant",
                "content": "I started checking the project.",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call-b",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "partial"},
            {"role": "user", "content": "Continue."},
        ]

        prepared, note = manager.prepare_history(history)

        self.assertEqual(
            prepared,
            [
                {"role": "assistant", "content": "I started checking the project."},
                {"role": "user", "content": "Continue."},
            ],
        )
        self.assertIn("incomplete", note)

    def test_history_cleanup_preserves_complete_multi_tool_block(self) -> None:
        manager = ContextManager()
        assistant = {
            "role": "assistant",
            "content": "Inspect both files.",
            "tool_calls": [
                {"id": "call-a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "call-b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        }
        history = [
            assistant,
            {"role": "tool", "tool_call_id": "call-b", "content": "B"},
            {"role": "tool", "tool_call_id": "call-a", "content": "A"},
        ]

        prepared, note = manager.prepare_history(history)

        self.assertEqual(prepared[0], assistant)
        self.assertEqual([message["tool_call_id"] for message in prepared[1:]], ["call-a", "call-b"])
        self.assertEqual(note, "")

    def test_agent_compacts_real_file_tool_results(self) -> None:
        for index in range(5):
            content = "\n".join(f"line {line}: " + "x" * 72 for line in range(150))
            (self.workspace / f"large_{index}.txt").write_text(content, encoding="utf-8")

        class ScriptedModel:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "role": "assistant",
                        "content": "Inspect the files.",
                        "tool_calls": [
                            {
                                "id": f"read-{index}",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": f"large_{index}.txt"}),
                                },
                            }
                            for index in range(5)
                        ],
                    }
                return {
                    "role": "assistant",
                    "content": "Inspection complete.",
                    "tool_calls": [
                        {
                            "id": "finish-1",
                            "type": "function",
                            "function": {
                                "name": "finish",
                                "arguments": json.dumps({"summary": "Local context probe passed."}),
                            },
                        }
                    ],
                }

        events = []
        final = CodingAgent(
            self.workspace,
            mode="ask",
            context_tokens=4000,
            model=ScriptedModel(),
            sink=events.append,
        ).run("Inspect every large file.")

        self.assertEqual(final, "Local context probe passed.")
        compacted = [event for event in events if event.get("type") == "context"]
        self.assertTrue(any(event.get("compacted_blocks", 0) > 0 for event in compacted))
        self.assertEqual(
            sum(1 for event in events if event.get("type") == "tool_call" and event.get("name") == "read_file"),
            5,
        )


if __name__ == "__main__":
    unittest.main()
