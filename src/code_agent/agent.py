from __future__ import annotations

import json
from time import perf_counter
from pathlib import Path
from typing import Any, Callable

from .config import load_llm_config
from .context import ContextManager
from .memory import format_memory
from .model import ChatModel
from .model_router import AutoRoutingModel
from .repo_map import RepoMap
from .session_store import SessionStore
from .tools import LocalTools


BASE_PROMPT = """You are a small coding agent running on the user's local machine.
Never access paths outside the workspace. Use the compact repository map to choose relevant files,
then inspect exact code with search_files and small read_file windows. Do not expose private chain-of-thought;
give short progress notes and concise conclusions."""

MODE_PROMPTS = {
    "code": """Mode: CODE. Complete the task iteratively:
1. inspect relevant files before editing and run lint_file on relevant Python/C++ files when useful,
2. use apply_patch for coordinated or multi-file edits and dry_run it when risk is non-trivial;
   use replace_in_file for one small unique block and write_file mainly for new or small files,
3. make minimal focused changes,
4. run lint_file and a relevant verification command after editing, then react to failures,
5. call finish with a concise summary and verification result.
On Windows, run_command uses cmd.exe. Do not use Bash-only syntax such as sleep, pkill, or a bare '&'.
run_command is for finite commands only. For persistent servers, use start_service with an executable/arguments
array and workspace-relative cwd; never use start /b, Start-Process, shell '&', or detach the service yourself.
Before starting, call service_status to reuse existing services. Bind development servers to 127.0.0.1.
Supply the actual port and a valid health_path to start_service. A running process alone is not proof the app works.
Use service_logs to diagnose startup errors, fix the cause, and retry. Report success only after readiness checks;
give the verified URL and service ID. Use stop_service when asked to stop or restart an owned service.
Services survive conversation turns and model switches while this host runs; they stop when the host exits.
Do not ask the user to do work that you can do with tools.""",
    "ask": """Mode: ASK. Answer questions about the repository. You may inspect files, but you must not
edit files or execute commands. Cite file paths and line numbers from read_file when useful, then call finish.""",
    "architect": """Mode: ARCHITECT. Inspect the repository and produce an implementation plan with affected
files, interfaces, risks, and verification steps. Do not edit files or execute commands. Call finish with the plan.""",
}

PROVIDER_IDENTITIES = {
    "auto": "Auto Router",
    "deepseek": "DeepSeek",
    "openai": "ChatGPT (through CloseAI)",
    "claude": "Claude (through CloseAI)",
    "qwen": "Qwen",
}

VALID_MODES = {"code", "ask", "architect", "context"}
EventSink = Callable[[dict[str, Any]], None]


class CodingAgent:
    def __init__(
        self,
        workspace: str | Path,
        max_turns: int = 24,
        sink: EventSink | None = None,
        mode: str = "code",
        context_tokens: int | None = None,
        model: Any | None = None,
        provider: str | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown mode: {mode}")
        config = load_llm_config(provider=provider)
        self.workspace = Path(workspace).resolve()
        self.max_turns = max_turns
        self.mode = mode
        self.provider = config.provider
        self.model_name = config.model
        self.session_store = session_store
        self.session_id = session_id
        self.tools = LocalTools(self.workspace, mode=mode, repo_map_chars=config.repo_map_chars)
        if model is not None:
            self.model = model
        elif config.provider == "auto":
            self.model = AutoRoutingModel(env_file=config.env_file)
        else:
            self.model = ChatModel(config=config)
        self.context = ContextManager(max_tokens=context_tokens or config.context_tokens)
        self.repo_map_chars = config.repo_map_chars
        self.sink = sink or (lambda event: None)
        self.last_exchange: list[dict[str, str]] = []

    def run(self, task: str, history: list[dict[str, Any]] | None = None) -> str:
        repo_map = RepoMap(self.workspace, self.tools.registry).build(task, self.repo_map_chars)
        checkpoint = None
        interrupted_tools = ""
        if self.session_store and self.session_id:
            self.session_store.recover_interrupted_tools(self.session_id)
            self.session_store.append_message(self.session_id, {"role": "user", "content": task})
            checkpoint = self.session_store.latest_checkpoint(self.session_id)
            interrupted_tools = self.session_store.interrupted_tool_summary(self.session_id)
        clean_history, history_note = self.context.prepare_history(history)

        if self.mode == "context":
            estimated = self.context.estimate_tokens(clean_history) + self.context.estimate_tokens(repo_map)
            self.sink(
                {
                    "type": "context",
                    "estimated_tokens": estimated,
                    "max_tokens": self.context.max_tokens,
                    "compacted_blocks": 0,
                    "truncated_tool_results": 0,
                }
            )
            return self._finish(task, f"{repo_map}\n\nEstimated retained context: {estimated} tokens.", clean_history)

        context_note = history_note or "No earlier chat messages were omitted or compacted."
        identity = PROVIDER_IDENTITIES.get(self.provider, self.provider)
        durable_memory = format_memory(
            self.session_store.get_memory(self.session_id) if self.session_store and self.session_id else None,
            checkpoint.get("summary", "") if checkpoint else "",
            interrupted_tools,
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"{BASE_PROMPT}\n\n{MODE_PROMPTS[self.mode]}\n\n"
                    f"Current model identity: {identity}. Configured model name: {self.model_name}. "
                    f"If the user asks which model is active, answer with {identity} and the configured model name. "
                    "Do not claim to be Claude, ChatGPT, DeepSeek, or Qwen unless it matches the current model identity."
                ),
            },
            {"role": "system", "content": repo_map},
            {"role": "system", "content": context_note},
            {"role": "system", "content": durable_memory},
            *clean_history,
            {"role": "user", "content": task},
        ]
        context_note_index = 2
        base_count = 4
        preserve_from = base_count + len(clean_history)
        compacted_summary: list[str] = []

        for step in range(1, self.max_turns + 1):
            messages, stats = self.context.compact_working_set(
                messages,
                base_count,
                context_note_index,
                compacted_summary,
                preserve_from=preserve_from,
            )
            self.sink(stats.event())
            if stats.compacted_blocks and self.session_store and self.session_id:
                self.session_store.save_checkpoint(
                    self.session_id,
                    "\n".join(compacted_summary[-12:]),
                    messages[base_count:],
                    stats.estimated_tokens,
                )
            self.sink({"type": "step", "step": step, "message": "asking model"})
            model_started = perf_counter()
            try:
                assistant = self.model.complete(messages, self.tools.schema())
            except Exception as exc:
                self._emit_route(step)
                actual_provider, actual_model = self._active_model_identity()
                self.sink(
                    {
                        "type": "llm_error",
                        "provider": actual_provider,
                        "model": actual_model,
                        "step": step,
                        "ok": False,
                        "latency_ms": round((perf_counter() - model_started) * 1000, 2),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                raise
            latency_ms = round((perf_counter() - model_started) * 1000, 2)
            self._emit_route(step)
            actual_provider, actual_model = self._active_model_identity()
            usage = dict(getattr(self.model, "last_usage", {}) or {})
            usage_source = getattr(self.model, "last_usage_source", "unavailable")
            if not usage:
                usage = {
                    "prompt_tokens": self.context.estimate_tokens(messages),
                    "completion_tokens": self.context.estimate_tokens(assistant),
                }
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                usage_source = "estimated"
            self.sink(
                {
                    "type": "llm_call",
                    "provider": actual_provider,
                    "model": actual_model,
                    "step": step,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "usage": usage,
                    "usage_source": usage_source,
                }
            )
            messages.append(assistant)
            if self.session_store and self.session_id:
                self.session_store.append_message(self.session_id, assistant)

            content = assistant.get("content")
            if content:
                self.sink({"type": "assistant", "content": content})

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return self._finish(task, content or "Model stopped without a final message.", messages)

            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                raw_args = tool_call.get("function", {}).get("arguments") or "{}"
                args = self._parse_args(raw_args)
                self.sink({"type": "tool_call", "name": name, "arguments": args})
                call_id = str(tool_call.get("id") or f"{name}-{step}")
                if self.session_store and self.session_id:
                    self.session_store.create_tool_call(self.session_id, call_id, name, args)
                    self.session_store.update_tool_call(self.session_id, call_id, "running")
                tool_started = perf_counter()
                result = self.tools.call(name, args)
                self.sink(
                    {
                        "type": "tool_result",
                        "name": name,
                        "call_id": call_id,
                        "ok": result.ok,
                        "latency_ms": round((perf_counter() - tool_started) * 1000, 2),
                        "output": result.output,
                    }
                )
                if self.session_store and self.session_id:
                    self.session_store.update_tool_call(
                        self.session_id,
                        call_id,
                        "completed" if result.ok else "failed",
                        result.output,
                    )

                if name == "finish":
                    finish_result = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result.to_message(),
                    }
                    messages.append(finish_result)
                    if self.session_store and self.session_id:
                        self.session_store.append_message(self.session_id, finish_result)
                        self.session_store.append_message(
                            self.session_id,
                            {"role": "assistant", "content": result.output},
                        )
                        self.session_store.update_memory(self.session_id, task, result.output, messages)
                    return self._finish(task, result.output)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result.to_message(),
                    }
                )
                if self.session_store and self.session_id:
                    self.session_store.append_message(self.session_id, messages[-1])

        final = (
            f"Stopped after max_turns={self.max_turns}. Increase --max-turns if the task is larger."
        )
        if self.session_store and self.session_id:
            self.session_store.update_memory(self.session_id, task, final, messages)
        return self._finish(
            task,
            final,
        )

    def _finish(self, task: str, final: str, messages: list[dict[str, Any]] | None = None) -> str:
        self.last_exchange = [
            {"role": "user", "content": task},
            {"role": "assistant", "content": final},
        ]
        if self.session_store and self.session_id:
            if messages is not None:
                self.session_store.update_memory(self.session_id, task, final, messages)
            self.session_store.touch_session(self.session_id, self.provider, self.model_name)
        return final

    def _emit_route(self, step: int) -> None:
        route = dict(getattr(self.model, "last_route", {}) or {})
        if not route:
            return
        attempts = route.get("attempts") or []
        self.sink(
            {
                "type": "route",
                **route,
                "provider": route.get("selected_provider") or "auto",
                "model": route.get("selected_model") or self.model_name,
                "step": step,
                "ok": bool(route.get("selected_provider")),
                "error": "; ".join(
                    f"{item.get('provider')}: {item.get('error')}" for item in attempts
                ),
            }
        )

    def _active_model_identity(self) -> tuple[str, str]:
        return (
            str(getattr(self.model, "last_provider", self.provider)),
            str(getattr(self.model, "last_model", self.model_name)),
        )

    def _parse_args(self, raw_args: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
