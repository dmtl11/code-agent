from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.config import load_llm_config
from code_agent.model import ModelError
from code_agent.model_router import AutoRoutingModel
from code_agent.session_store import SessionStore


class FakeModel:
    def __init__(self, name: str, error: str = "") -> None:
        self.model = name
        self.error = error
        self.calls = 0
        self.last_usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        self.last_usage_source = "actual"

    def complete(self, messages, tools):
        self.calls += 1
        if self.error:
            raise ModelError(self.error)
        return {"role": "assistant", "content": self.model, "tool_calls": []}


class AutoRouterTests(unittest.TestCase):
    def test_auto_config_is_available_without_its_own_api_key(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write("CODE_AGENT_PROVIDER=auto\nQWEN_API_KEY=qwen-test\n")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with patch.dict(os.environ, {}, clear=True):
            config = load_llm_config(handle.name)
        self.assertEqual(config.provider, "auto")
        self.assertEqual(config.model, "auto-cascade")

    def test_short_turn_prefers_qwen(self) -> None:
        qwen = FakeModel("qwen-plus")
        deepseek = FakeModel("deepseek-chat")
        router = AutoRoutingModel({"qwen": qwen, "deepseek": deepseek})

        response = router.complete([{"role": "user", "content": "写一个排序函数"}], [])

        self.assertEqual(response["content"], "qwen-plus")
        self.assertEqual(router.last_route["stage"], "efficient")
        self.assertEqual(router.last_provider, "qwen")
        self.assertEqual(qwen.calls, 1)
        self.assertEqual(deepseek.calls, 0)

    def test_complex_architecture_turn_uses_capable_tier(self) -> None:
        models = {
            "qwen": FakeModel("qwen-plus"),
            "deepseek": FakeModel("deepseek-chat"),
            "openai": FakeModel("gpt-4o"),
            "claude": FakeModel("claude-haiku"),
        }
        router = AutoRoutingModel(models)
        messages = [
            {"role": "system", "content": "Mode: ARCHITECT. Inspect the repository."},
            {
                "role": "user",
                "content": "重构前后端架构，处理并发、安全和多文件迁移，并给出完整 implementation plan。",
            },
        ]

        router.complete(messages, [])

        self.assertEqual(router.last_route["stage"], "capable")
        self.assertEqual(router.last_provider, "claude")

    def test_failed_qwen_cascades_to_deepseek_and_copies_usage(self) -> None:
        qwen = FakeModel("qwen-plus", error="temporary failure")
        deepseek = FakeModel("deepseek-chat")
        router = AutoRoutingModel({"qwen": qwen, "deepseek": deepseek})

        router.complete([{"role": "user", "content": "修复一个小错误"}], [])

        self.assertEqual(router.last_provider, "deepseek")
        self.assertEqual(router.last_route["preferred_provider"], "qwen")
        self.assertEqual(router.last_route["fallback_count"], 1)
        self.assertEqual(router.last_usage["total_tokens"], 15)

    def test_recent_tool_failures_escalate_routine_turn(self) -> None:
        router = AutoRoutingModel(
            {"qwen": FakeModel("qwen-plus"), "deepseek": FakeModel("deepseek-chat")}
        )
        messages = [
            {"role": "user", "content": "继续修复"},
            {"role": "tool", "content": '{"ok":false,"output":"lint failed"}'},
            {"role": "tool", "content": '{"ok":false,"output":"test failed"}'},
            {"role": "tool", "content": '{"ok":false,"output":"test failed again"}'},
        ]

        router.complete(messages, [])

        self.assertEqual(router.last_provider, "deepseek")
        self.assertEqual(router.last_route["stage"], "balanced")

    def test_route_metrics_report_fallbacks(self) -> None:
        tmp = Path(__file__).resolve().parent / "_work" / self._testMethodName
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = SessionStore(tmp / "sessions.sqlite3")
        session_id = store.create_session(str(tmp))
        store.record_metric(
            session_id,
            {
                "kind": "route",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "ok": True,
                "error": "qwen: timeout",
            },
        )
        summary = store.monitoring_summary(session_id=session_id)

        self.assertEqual(summary["summary"]["route_decisions"], 1)
        self.assertEqual(summary["summary"]["route_fallbacks"], 1)
        self.assertEqual(summary["routes"][0]["error_rate"], 100.0)


if __name__ == "__main__":
    unittest.main()
