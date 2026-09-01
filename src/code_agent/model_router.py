from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import load_llm_config
from .model import ChatModel, ModelError


ROUTABLE_PROVIDERS = ("qwen", "deepseek", "openai", "claude")
DEFAULT_CASCADES = {
    "qwen": ("qwen", "deepseek", "openai", "claude"),
    "deepseek": ("deepseek", "openai", "claude", "qwen"),
    "openai": ("openai", "claude", "deepseek", "qwen"),
    "claude": ("claude", "openai", "deepseek", "qwen"),
}

COMPLEX_TERMS = (
    "architecture",
    "architect",
    "refactor",
    "migration",
    "concurrency",
    "security",
    "full stack",
    "multi-file",
    "debug",
    "架构",
    "重构",
    "迁移",
    "并发",
    "安全",
    "前后端",
    "多文件",
    "排查",
)


@dataclass(frozen=True)
class RouteDecision:
    preferred: str
    stage: str
    score: int
    reasons: tuple[str, ...]
    candidates: tuple[str, ...]


class AutoRoutingModel:
    """Route each model turn while preserving the agent's common message format."""

    provider = "auto"
    model = "auto-cascade"

    def __init__(
        self,
        models: dict[str, Any] | None = None,
        env_file: str | os.PathLike[str] | None = None,
    ) -> None:
        if models is None:
            models = {}
            for provider in ROUTABLE_PROVIDERS:
                config = load_llm_config(env_file=env_file, provider=provider)
                if config.api_key:
                    models[provider] = ChatModel(config)
        self.models = {name: value for name, value in models.items() if name in ROUTABLE_PROVIDERS}
        self.failure_counts = {provider: 0 for provider in ROUTABLE_PROVIDERS}
        self.retry_after = {provider: 0 for provider in ROUTABLE_PROVIDERS}
        self.call_count = 0
        self.last_usage: dict[str, int] = {}
        self.last_usage_source = "unavailable"
        self.last_provider = "auto"
        self.last_model = self.model
        self.last_route: dict[str, Any] = {}

    def route(self, messages: list[dict[str, Any]]) -> RouteDecision:
        score = 0
        reasons: list[str] = []
        user_text = self._last_user_text(messages)
        lowered = user_text.lower()
        context_chars = sum(len(str(message.get("content") or "")) for message in messages)
        tool_failures = self._recent_tool_failures(messages)
        tool_messages = sum(message.get("role") == "tool" for message in messages)
        architect_mode = any(
            message.get("role") == "system" and "Mode: ARCHITECT" in str(message.get("content") or "")
            for message in messages[:4]
        )

        if len(user_text) > 600:
            score += 1
            reasons.append("long request")
        if len(user_text) > 1800:
            score += 1
            reasons.append("very long request")
        matched = [term for term in COMPLEX_TERMS if term in lowered]
        if matched:
            score += min(3, 1 + len(matched) // 2)
            reasons.append("complex task signals")
        if architect_mode:
            score += 3
            reasons.append("architect mode")
        if tool_failures:
            score += min(3, tool_failures)
            reasons.append(f"{tool_failures} recent tool failure(s)")
        if tool_messages >= 6:
            score += 1
            reasons.append("multi-step agent run")
        if context_chars // 4 > 16000:
            score += 2
            reasons.append("long context")
        elif context_chars // 4 > 8000:
            score += 1
            reasons.append("medium context")

        qwen_max = self._env_int("CODE_AGENT_AUTO_QWEN_MAX_SCORE", 2)
        deepseek_max = self._env_int("CODE_AGENT_AUTO_DEEPSEEK_MAX_SCORE", 5)
        if score <= qwen_max:
            preferred, stage = "qwen", "efficient"
        elif score <= deepseek_max:
            preferred, stage = "deepseek", "balanced"
        elif architect_mode or context_chars // 4 > 16000:
            preferred, stage = "claude", "capable"
        else:
            preferred, stage = "openai", "capable"

        configured = [provider for provider in DEFAULT_CASCADES[preferred] if provider in self.models]
        configured.sort(
            key=lambda provider: (
                self.retry_after.get(provider, 0) > self.call_count,
                self.failure_counts.get(provider, 0),
            )
        )
        if not reasons:
            reasons.append("short routine coding turn")
        return RouteDecision(preferred, stage, score, tuple(reasons), tuple(configured))

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.call_count += 1
        decision = self.route(messages)
        attempts: list[dict[str, str]] = []
        if not decision.candidates:
            self.last_route = self._route_payload(decision, "", "", attempts)
            raise ModelError(
                "Auto router found no configured model. Fill QWEN_API_KEY, DEEPSEEK_API_KEY, "
                "or CLOSEAI_API_KEY in config/llm.env."
            )

        for provider in decision.candidates:
            model = self.models[provider]
            try:
                response = model.complete(messages, tools)
            except Exception as exc:
                self.failure_counts[provider] += 1
                self.retry_after[provider] = self.call_count + 2
                attempts.append(
                    {
                        "provider": provider,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue

            self.failure_counts[provider] = max(0, self.failure_counts[provider] - 1)
            self.retry_after[provider] = 0
            self.last_usage = dict(getattr(model, "last_usage", {}) or {})
            self.last_usage_source = getattr(model, "last_usage_source", "unavailable")
            self.last_provider = provider
            self.last_model = str(getattr(model, "model", provider))
            self.last_route = self._route_payload(
                decision,
                self.last_provider,
                self.last_model,
                attempts,
            )
            return response

        self.last_provider = attempts[-1]["provider"] if attempts else "auto"
        self.last_model = str(getattr(self.models.get(self.last_provider), "model", self.model))
        self.last_route = self._route_payload(
            decision,
            self.last_provider,
            self.last_model,
            attempts,
        )
        detail = "; ".join(f"{item['provider']}: {item['error']}" for item in attempts)
        raise ModelError(f"Auto router exhausted all configured providers. {detail}")

    @staticmethod
    def _last_user_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _recent_tool_failures(messages: list[dict[str, Any]]) -> int:
        failures = 0
        for message in messages[-16:]:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            try:
                payload = json.loads(content) if isinstance(content, str) else content
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("ok") is False:
                failures += 1
        return failures

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    @staticmethod
    def _route_payload(
        decision: RouteDecision,
        selected_provider: str,
        selected_model: str,
        attempts: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "requested_provider": "auto",
            "preferred_provider": decision.preferred,
            "selected_provider": selected_provider,
            "selected_model": selected_model,
            "stage": decision.stage,
            "score": decision.score,
            "reasons": list(decision.reasons),
            "candidates": list(decision.candidates),
            "attempts": list(attempts),
            "fallback_count": len(attempts),
        }
