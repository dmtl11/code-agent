from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.config import load_llm_config
from code_agent.model import ChatModel


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class ProviderTests(unittest.TestCase):
    def _env_file(self, content: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write(content)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_provider_alias_and_provider_key_are_resolved(self) -> None:
        path = self._env_file("CODE_AGENT_PROVIDER=chatgpt\nCLOSEAI_API_KEY=closeai-test\n")
        with patch.dict(os.environ, {}, clear=True):
            config = load_llm_config(path)

        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.protocol, "openai")
        self.assertEqual(config.base_url, "https://api.openai-proxy.org/v1")
        self.assertEqual(config.api_key, "closeai-test")

    def test_closeai_relay_overrides_old_common_values(self) -> None:
        path = self._env_file(
            "CODE_AGENT_PROVIDER=claude\n"
            "CODE_AGENT_PROTOCOL=openai\n"
            "CODE_AGENT_BASE_URL=https://old-deepseek.example/v1\n"
            "CODE_AGENT_API_KEY=old-key\n"
            "CLOSEAI_BASE_URL=https://relay.example/v1\n"
            "CLOSEAI_CLAUDE_MODEL=claude-relay-model\n"
            "CLOSEAI_API_KEY=relay-key\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            config = load_llm_config(path)

        self.assertEqual(config.provider, "claude")
        self.assertEqual(config.protocol, "openai")
        self.assertEqual(config.base_url, "https://relay.example/v1")
        self.assertEqual(config.model, "claude-relay-model")
        self.assertEqual(config.api_key, "relay-key")

    def test_shared_closeai_relay_can_select_two_models(self) -> None:
        path = self._env_file(
            "CODE_AGENT_PROVIDER=deepseek\n"
            "CLOSEAI_API_KEY=relay-key\n"
            "CLOSEAI_BASE_URL=https://relay.example/v1\n"
            "CLOSEAI_OPENAI_MODEL=chatgpt-relay-model\n"
            "CLOSEAI_CLAUDE_MODEL=claude-relay-model\n"
            "CLOSEAI_OPENAI_PROTOCOL=openai\n"
            "CLOSEAI_CLAUDE_PROTOCOL=openai\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            openai_config = load_llm_config(path, provider="openai")
            claude_config = load_llm_config(path, provider="claude")

        self.assertEqual(openai_config.model, "chatgpt-relay-model")
        self.assertEqual(claude_config.model, "claude-relay-model")
        self.assertEqual(openai_config.api_key, "relay-key")
        self.assertEqual(claude_config.api_key, "relay-key")
        self.assertEqual(openai_config.protocol, "openai")
        self.assertEqual(claude_config.protocol, "openai")

    def test_request_provider_does_not_inherit_another_provider_defaults(self) -> None:
        path = self._env_file(
            "CODE_AGENT_PROVIDER=deepseek\n"
            "CODE_AGENT_BASE_URL=https://api.deepseek.com/v1\n"
            "CODE_AGENT_MODEL=deepseek-chat\n"
            "CODE_AGENT_API_KEY=deepseek-key\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            config = load_llm_config(path, provider="claude")

        self.assertEqual(config.provider, "claude")
        self.assertEqual(config.base_url, "https://api.openai-proxy.org/v1")
        self.assertEqual(config.model, "claude-haiku-4-5")
        self.assertEqual(config.protocol, "openai")
        self.assertEqual(config.api_key, "")

    def test_claude_request_and_tool_response_are_normalized(self) -> None:
        path = self._env_file(
            "CODE_AGENT_PROVIDER=claude\n"
            "CLOSEAI_API_KEY=claude-test\n"
            "CLOSEAI_BASE_URL=https://relay.example/anthropic\n"
            "CLOSEAI_CLAUDE_PROTOCOL=anthropic\n"
            "CODE_AGENT_MODEL=claude-test-model\n"
        )
        model_messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Inspect the project."},
            {
                "role": "assistant",
                "content": "I will inspect it.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "list_files",
                            "arguments": '{"path":"."}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "list_files",
                "content": '{"ok":true,"output":"app.py"}',
            },
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        with patch.dict(os.environ, {"CODE_AGENT_ENV_FILE": str(path)}, clear=True):
            model = ChatModel()
            with patch(
                "code_agent.model.urllib.request.urlopen",
                return_value=_FakeResponse(
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-2",
                                "name": "finish",
                                "input": {"summary": "Done"},
                            }
                        ]
                    }
                ),
            ) as urlopen:
                result = model.complete(model_messages, tools)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertTrue(request.full_url.endswith("/anthropic/messages"))
        self.assertEqual(request.headers["X-api-key"], "claude-test")
        self.assertEqual(body["system"], "Be concise.")
        self.assertEqual(body["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(body["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "finish")
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"summary": "Done"})
        self.assertEqual(model.last_usage, {})

    def test_usage_is_normalized_for_monitoring(self) -> None:
        usage = ChatModel._normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        )
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 60)


if __name__ == "__main__":
    unittest.main()
