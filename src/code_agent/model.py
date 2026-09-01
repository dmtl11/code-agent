from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import LLMConfig, load_llm_config


class ModelError(RuntimeError):
    pass


class ChatModel:
    """Small provider adapter that keeps the agent's internal message format stable."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        config = config or load_llm_config()
        self.provider = config.provider
        self.protocol = config.protocol
        self.base_url = config.base_url
        self.model = config.model
        self.api_key = config.api_key
        self.api_key_env_names = config.api_key_env_names
        self.env_file = config.env_file
        self.last_usage: dict[str, int] = {}
        self.last_usage_source = "unavailable"

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.api_key:
            names = ", ".join(self.api_key_env_names)
            raise ModelError(
                f"Missing API key for provider '{self.provider}'. "
                f"Fill {names} or CODE_AGENT_API_KEY in {self.env_file}."
            )

        if self.protocol == "anthropic":
            body, headers, endpoint = self._anthropic_request(messages, tools)
        else:
            body, headers, endpoint = self._openai_request(messages, tools)

        payload = self._post_json(endpoint, body, headers)
        self.last_usage = self._normalize_usage(payload.get("usage"))
        self.last_usage_source = "actual" if self.last_usage else "unavailable"
        if self.protocol == "anthropic":
            return self._normalize_anthropic_response(payload)
        return self._normalize_openai_response(payload)

    def _openai_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        return (
            body,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            f"{self.base_url}/chat/completions",
        )

    def _anthropic_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, str], str]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                content = message.get("content")
                if content:
                    system_parts.append(str(content))
                continue

            if role == "tool":
                content = message.get("content") or ""
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id") or message.get("name") or "tool-call",
                        "content": str(content),
                    }
                ]
                self._append_anthropic_message(converted, "user", blocks)
                continue

            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                content = message.get("content")
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id") or function.get("name") or "tool-call",
                            "name": function.get("name", ""),
                            "input": _parse_tool_input(function.get("arguments")),
                        }
                    )
                if blocks:
                    self._append_anthropic_message(converted, "assistant", blocks)
                continue

            if role == "user":
                content = message.get("content") or ""
                self._append_anthropic_message(converted, "user", [{"type": "text", "text": str(content)}])

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": converted,
            "tools": [_to_anthropic_tool(tool) for tool in tools],
            "temperature": 0.2,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        return (
            body,
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            f"{self.base_url}/messages",
        )

    @staticmethod
    def _append_anthropic_message(
        messages: list[dict[str, Any]], role: str, content: list[dict[str, Any]]
    ) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})

    def _post_json(self, endpoint: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelError(f"Model HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ModelError("Model returned invalid JSON.") from exc

    @staticmethod
    def _normalize_openai_response(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Unexpected model response: {payload}") from exc

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        total = usage.get("total_tokens", (prompt or 0) + (completion or 0))
        try:
            normalized = {
                "prompt_tokens": max(0, int(prompt or 0)),
                "completion_tokens": max(0, int(completion or 0)),
                "total_tokens": max(0, int(total or 0)),
            }
        except (TypeError, ValueError):
            return {}
        for source, target in (("prompt_cache_hit_tokens", "prompt_cache_hit_tokens"), ("prompt_cache_miss_tokens", "prompt_cache_miss_tokens")):
            if source in usage:
                try:
                    normalized[target] = max(0, int(usage[source] or 0))
                except (TypeError, ValueError):
                    pass
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
            try:
                normalized["prompt_cache_hit_tokens"] = max(0, int(prompt_details["cached_tokens"] or 0))
            except (TypeError, ValueError):
                pass
        return normalized

    @staticmethod
    def _normalize_anthropic_response(payload: dict[str, Any]) -> dict[str, Any]:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ModelError(f"Unexpected Claude response: {payload}")

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )

        return {
            "role": "assistant",
            "content": "\n".join(text_parts) or None,
            "tool_calls": tool_calls,
        }


def _parse_tool_input(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or {}
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get(
            "parameters", {"type": "object", "properties": {}}
        ),
    }
