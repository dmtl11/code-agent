from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import load_llm_config


class ModelError(RuntimeError):
    pass


class ChatModel:
    def __init__(self) -> None:
        config = load_llm_config()
        self.base_url = config.base_url
        self.model = config.model
        self.api_key = config.api_key
        self.env_file = config.env_file

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.api_key:
            raise ModelError(f"Missing API key. Fill CODE_AGENT_API_KEY in {self.env_file}.")

        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelError(f"Model HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc

        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Unexpected model response: {payload}") from exc
