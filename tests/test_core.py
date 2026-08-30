from __future__ import annotations

import shutil
import unittest
from pathlib import Path
import sys
import json


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.context import ContextManager
from code_agent.agent import CodingAgent
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
