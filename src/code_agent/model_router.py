from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from .config import (
    load_auto_route_limits,
    load_llm_config,
    load_qwen_auto_models,
    load_semantic_router_config,
)
from .model import ChatModel, ModelError
from .semantic_router import ArchRouterClient


MODEL_CASCADES = {
    "qwen-flash": (
        "qwen-flash", "qwen-plus", "qwen-max", "qwen", "deepseek", "openai", "claude"
    ),
    "qwen-coder": (
        "qwen-coder", "qwen-max", "qwen", "deepseek", "openai", "claude", "qwen-plus"
    ),
    "qwen-math": (
        "qwen-math", "qwen-plus", "qwen-max", "qwen", "deepseek", "openai", "claude"
    ),
    "qwen-plus": (
        "qwen-plus", "qwen-max", "qwen", "deepseek", "openai", "claude", "qwen-flash"
    ),
    "qwen-max": (
        "qwen-max", "qwen-coder", "qwen", "deepseek", "openai", "claude", "qwen-plus"
    ),
    "qwen": ("qwen", "deepseek", "openai", "claude"),
    "deepseek": (
        "deepseek", "qwen-max", "openai", "claude", "qwen-coder", "qwen-plus", "qwen"
    ),
    "openai": ("openai", "claude", "qwen-max", "deepseek", "qwen-coder", "qwen"),
    "claude": ("claude", "openai", "qwen-max", "deepseek", "qwen-coder", "qwen"),
}

COMPLEX_TERMS = (
    "architecture", "architect", "refactor", "migration", "concurrency", "security",
    "full stack", "multi-file", "debug", "架构", "重构", "迁移", "并发", "安全",
    "前后端", "多文件", "排查",
)
CODE_TERMS = (
    "code", "coding", "function", "class", "bug", "test", "api", "python", "javascript",
    "typescript", "react", "vue", "cpp", "c++", ".py", ".js", ".ts", ".tsx", ".cpp",
    "代码", "函数", "类", "接口", "修复", "报错", "测试", "文件", "项目", "编译",
    "运行", "前端", "后端", "仓库", "实现",
)
MATH_TERMS = (
    "equation", "integral", "derivative", "theorem", "probability", "matrix", "geometry",
    "calculate", "prove", "latex", "方程", "积分", "导数", "定理", "概率", "矩阵", "几何",
    "计算", "证明", "数学", "数列", "极限",
)

SEMANTIC_ROUTE_DECISIONS = {
    "simple_general": ("general", "efficient"),
    "complex_general": ("general", "balanced"),
    "routine_code": ("code", "efficient"),
    "complex_code": ("code", "capable"),
    "mathematics": ("math", "balanced"),
    "architecture": ("architecture", "capable"),
}
STAGE_RANK = {"efficient": 0, "balanced": 1, "capable": 2}


@dataclass(frozen=True)
class RouteDecision:
    preferred: str
    task_type: str
    stage: str
    score: int
    reasons: tuple[str, ...]
    candidates: tuple[str, ...]
    strategy: str
    semantic_route: str
    router_error: str


class AutoRoutingModel:
    """Classify the task, select a concrete model, then cascade on failures."""

    provider = "auto"
    model = "auto-cascade"

    def __init__(
        self,
        models: dict[str, Any] | None = None,
        env_file: str | os.PathLike[str] | None = None,
        semantic_router: Any | None = None,
    ) -> None:
        load_configured_router = models is None
        if models is None:
            models = self._load_models(env_file)
        self.models = {
            name: value for name, value in models.items() if name in MODEL_CASCADES
        }
        self.target_providers = {
            name: "qwen" if name.startswith("qwen") else name for name in self.models
        }
        self.failure_counts = {target: 0 for target in self.models}
        self.retry_after = {target: 0 for target in self.models}
        self.efficient_max, self.balanced_max = load_auto_route_limits(env_file)
        router_config = load_semantic_router_config(env_file)
        self.semantic_router = semantic_router
        if self.semantic_router is None and router_config.base_url and load_configured_router:
            self.semantic_router = ArchRouterClient(router_config)
        self._semantic_cache_key = ""
        self._semantic_cache_route = ""
        self._semantic_cache_error = ""
        self.call_count = 0
        self.last_usage: dict[str, int] = {}
        self.last_usage_source = "unavailable"
        self.last_provider = "auto"
        self.last_model = self.model
        self.last_route: dict[str, Any] = {}

    @staticmethod
    def _load_models(env_file: str | os.PathLike[str] | None) -> dict[str, Any]:
        models: dict[str, Any] = {}
        qwen_config = load_llm_config(env_file=env_file, provider="qwen")
        if qwen_config.api_key:
            for target, model_name in load_qwen_auto_models(env_file).items():
                models[target] = ChatModel(replace(qwen_config, model=model_name))
        for provider in ("deepseek", "openai", "claude"):
            config = load_llm_config(env_file=env_file, provider=provider)
            if config.api_key:
                models[provider] = ChatModel(config)
        return models

    def route(self, messages: list[dict[str, Any]]) -> RouteDecision:
        score = 0
        reasons: list[str] = []
        user_text = self._last_user_text(messages)
        lowered = user_text.lower()
        context_chars = sum(len(str(message.get("content") or "")) for message in messages)
        tool_failures = self._recent_tool_failures(messages)
        tool_messages = sum(message.get("role") == "tool" for message in messages)
        task_type = self._task_type(lowered, self._mode(messages))

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
        if task_type == "architecture":
            score += 3
            reasons.append("architecture task")
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

        if score <= self.efficient_max:
            stage = "efficient"
        elif score <= self.balanced_max:
            stage = "balanced"
        else:
            stage = "capable"

        semantic_route, router_error = self._semantic_classification(
            messages, user_text, self._mode(messages)
        )
        strategy = "heuristic"
        if semantic_route:
            task_type, stage = SEMANTIC_ROUTE_DECISIONS[semantic_route]
            reasons.insert(0, f"semantic route: {semantic_route}")
            strategy = "semantic+constraints"
        elif router_error:
            reasons.insert(0, "semantic router unavailable; used heuristic fallback")
            strategy = "heuristic-fallback"

        mode = self._mode(messages)
        if mode == "architect":
            task_type = "architecture"
            stage = "capable"
        if tool_failures >= 4:
            stage = "capable"
        elif tool_failures >= 2:
            stage = self._max_stage(stage, "balanced")
        if context_chars // 4 > 16000:
            stage = self._max_stage(stage, "balanced")
        preferred = self._preferred_target(task_type, stage)

        configured = [target for target in MODEL_CASCADES[preferred] if target in self.models]
        configured.sort(
            key=lambda target: (
                self.retry_after.get(target, 0) > self.call_count,
                self.failure_counts.get(target, 0),
            )
        )
        if not reasons:
            reasons.append(f"{task_type} task detected from user input")
        return RouteDecision(
            preferred,
            task_type,
            stage,
            score,
            tuple(reasons),
            tuple(configured),
            strategy,
            semantic_route,
            router_error,
        )

    def _semantic_classification(
        self,
        messages: list[dict[str, Any]],
        user_text: str,
        mode: str,
    ) -> tuple[str, str]:
        if self.semantic_router is None or mode == "architect":
            return "", ""
        cache_key = f"{mode}\n{user_text}"
        if cache_key == self._semantic_cache_key:
            return self._semantic_cache_route, self._semantic_cache_error
        route = ""
        error = ""
        try:
            candidate = str(self.semantic_router.classify(messages) or "")
            if candidate in SEMANTIC_ROUTE_DECISIONS:
                route = candidate
            else:
                error = f"unsupported route: {candidate!r}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
        self._semantic_cache_key = cache_key
        self._semantic_cache_route = route
        self._semantic_cache_error = error
        return route, error

    @staticmethod
    def _max_stage(left: str, right: str) -> str:
        return left if STAGE_RANK[left] >= STAGE_RANK[right] else right

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.call_count += 1
        decision = self.route(messages)
        attempts: list[dict[str, str]] = []
        if not decision.candidates:
            self.last_route = self._route_payload(decision, "", "", "", attempts)
            raise ModelError(
                "Auto router found no configured model. Fill QWEN_API_KEY, DEEPSEEK_API_KEY, "
                "or CLOSEAI_API_KEY in config/llm.env."
            )

        for target in decision.candidates:
            model = self.models[target]
            provider = self.target_providers[target]
            model_name = str(getattr(model, "model", target))
            try:
                response = model.complete(messages, tools)
            except Exception as exc:
                self.failure_counts[target] += 1
                self.retry_after[target] = self.call_count + 2
                attempts.append(
                    {
                        "target": target,
                        "provider": provider,
                        "model": model_name,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue

            self.failure_counts[target] = max(0, self.failure_counts[target] - 1)
            self.retry_after[target] = 0
            self.last_usage = dict(getattr(model, "last_usage", {}) or {})
            self.last_usage_source = getattr(model, "last_usage_source", "unavailable")
            self.last_provider = provider
            self.last_model = model_name
            self.last_route = self._route_payload(
                decision, target, self.last_provider, self.last_model, attempts
            )
            return response

        last_target = attempts[-1]["target"] if attempts else ""
        self.last_provider = self.target_providers.get(last_target, "auto")
        self.last_model = attempts[-1]["model"] if attempts else self.model
        self.last_route = self._route_payload(
            decision, last_target, self.last_provider, self.last_model, attempts
        )
        detail = "; ".join(f"{item['model']}: {item['error']}" for item in attempts)
        raise ModelError(f"Auto router exhausted all configured models. {detail}")

    @staticmethod
    def _mode(messages: list[dict[str, Any]]) -> str:
        system_text = "\n".join(
            str(message.get("content") or "")
            for message in messages[:4]
            if message.get("role") == "system"
        )
        if "Mode: ARCHITECT" in system_text:
            return "architect"
        if "Mode: ASK" in system_text:
            return "ask"
        return "code"

    @staticmethod
    def _task_type(lowered: str, mode: str) -> str:
        if mode == "architect" or any(
            term in lowered for term in ("architecture", "architect", "架构")
        ):
            return "architecture"
        math_signal = any(term in lowered for term in MATH_TERMS)
        code_signal = any(term in lowered for term in CODE_TERMS)
        if math_signal and not code_signal:
            return "math"
        if code_signal:
            return "code"
        return "general"

    @staticmethod
    def _preferred_target(task_type: str, stage: str) -> str:
        if task_type == "architecture":
            return "qwen-max"
        if task_type == "math":
            return "qwen-math"
        if task_type == "code":
            return "qwen-max" if stage == "capable" else "qwen-coder"
        if stage == "efficient":
            return "qwen-flash"
        if stage == "balanced":
            return "qwen-plus"
        return "qwen-max"

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

    def _route_payload(
        self,
        decision: RouteDecision,
        selected_target: str,
        selected_provider: str,
        selected_model: str,
        attempts: list[dict[str, str]],
    ) -> dict[str, Any]:
        preferred_model = str(
            getattr(self.models.get(decision.preferred), "model", decision.preferred)
        )
        return {
            "requested_provider": "auto",
            "task_type": decision.task_type,
            "preferred_target": decision.preferred,
            "preferred_provider": self.target_providers.get(decision.preferred, "qwen"),
            "preferred_model": preferred_model,
            "selected_target": selected_target,
            "selected_provider": selected_provider,
            "selected_model": selected_model,
            "stage": decision.stage,
            "score": decision.score,
            "reasons": list(decision.reasons),
            "candidates": [
                str(getattr(self.models[target], "model", target))
                for target in decision.candidates
            ],
            "attempts": list(attempts),
            "fallback_count": len(attempts),
            "routing_strategy": decision.strategy,
            "semantic_route": decision.semantic_route,
            "router_error": decision.router_error,
        }
