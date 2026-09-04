from __future__ import annotations

import ast
import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import SemanticRouterConfig


ROUTE_DEFINITIONS = (
    {
        "name": "simple_general",
        "description": (
            "Short, straightforward general questions, explanations, rewrites, or routine "
            "requests that need little reasoning and no specialized coding or mathematics."
        ),
    },
    {
        "name": "complex_general",
        "description": (
            "General tasks requiring multi-step reasoning, comparison, synthesis, planning, "
            "or careful analysis, but not primarily software engineering or mathematics."
        ),
    },
    {
        "name": "routine_code",
        "description": (
            "Focused software implementation, debugging, testing, code explanation, or small "
            "repository changes with a clear and bounded scope."
        ),
    },
    {
        "name": "complex_code",
        "description": (
            "Difficult software engineering involving many files, subtle bugs, broad refactors, "
            "performance, compatibility, or repeated failed implementation attempts."
        ),
    },
    {
        "name": "mathematics",
        "description": (
            "Mathematical calculation, proof, equations, probability, geometry, statistics, "
            "or symbolic reasoning where a math-specialized model is useful."
        ),
    },
    {
        "name": "architecture",
        "description": (
            "Software architecture, security design, concurrency, system migration, distributed "
            "systems, or high-impact technical planning requiring the most capable model."
        ),
    },
)

ROUTE_NAMES = frozenset(route["name"] for route in ROUTE_DEFINITIONS)

TASK_INSTRUCTION = """You are a routing model.
Select the route that best matches the user's latest intent.
The available route descriptions are inside <routes></routes>:
<routes>
{routes}
</routes>

The recent conversation is inside <conversation></conversation>:
<conversation>
{conversation}
</conversation>

Return only JSON in this exact shape: {{"route":"route_name"}}.
Use one exact route name from <routes>. If no route applies, return {{"route":"other"}}.
"""


class SemanticRouterError(RuntimeError):
    pass


class ArchRouterClient:
    """Small OpenAI-compatible client for the Arch-Router classification model."""

    def __init__(self, config: SemanticRouterConfig) -> None:
        self.base_url = config.base_url
        self.model = config.model
        self.api_key = config.api_key
        self.timeout_seconds = config.timeout_seconds

    def classify(self, messages: list[dict[str, Any]]) -> str:
        conversation = self._conversation(messages)
        prompt = TASK_INSTRUCTION.format(
            routes=json.dumps(ROUTE_DEFINITIONS, ensure_ascii=False, indent=2),
            conversation=json.dumps(conversation, ensure_ascii=False, indent=2),
        )
        payload = self._post(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 64,
            }
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SemanticRouterError(f"Unexpected router response: {payload}") from exc
        route = self._parse_route(str(content or ""))
        if route not in ROUTE_NAMES:
            raise SemanticRouterError(f"Router returned unsupported route: {route or content!r}")
        return route

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SemanticRouterError(f"Router HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise SemanticRouterError(f"Router request failed: {exc}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            raise SemanticRouterError(f"Router returned no valid response: {exc}") from exc

    @staticmethod
    def _conversation(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        conversation: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = str(message.get("content") or "").strip()
            if content:
                conversation.append({"role": role, "content": content[:1200]})
        return conversation[-6:]

    @staticmethod
    def _parse_route(content: str) -> str:
        stripped = content.strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(stripped)
            except (SyntaxError, ValueError):
                match = re.search(r'["\']route["\']\s*:\s*["\']([^"\']+)["\']', stripped)
                return match.group(1).strip() if match else ""
        return str(parsed.get("route") or "").strip() if isinstance(parsed, dict) else ""
