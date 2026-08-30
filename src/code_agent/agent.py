from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import load_llm_config
from .context import ContextManager
from .model import ChatModel
from .repo_map import RepoMap
from .tools import LocalTools


BASE_PROMPT = """You are a small coding agent running on the user's local machine.
Never access paths outside the workspace. Use the compact repository map to choose relevant files,
then inspect exact code with search_files and small read_file windows. Do not expose private chain-of-thought;
give short progress notes and concise conclusions."""

MODE_PROMPTS = {
    "code": """Mode: CODE. Complete the task iteratively:
1. inspect relevant files before editing and run lint_file on relevant Python/C++ files when useful,
2. prefer replace_in_file for focused changes; use write_file mainly for new or small files,
3. make minimal focused changes,
4. run lint_file and a relevant verification command after editing, then react to failures,
5. call finish with a concise summary and verification result.
Do not ask the user to do work that you can do with tools.""",
    "ask": """Mode: ASK. Answer questions about the repository. You may inspect files, but you must not
edit files or execute commands. Cite file paths and line numbers from read_file when useful, then call finish.""",
    "architect": """Mode: ARCHITECT. Inspect the repository and produce an implementation plan with affected
files, interfaces, risks, and verification steps. Do not edit files or execute commands. Call finish with the plan.""",
}

VALID_MODES = {"code", "ask", "architect", "context"}
EventSink = Callable[[dict[str, Any]], None]


class CodingAgent:
    def __init__(
        self,
        workspace: str | Path,
        max_turns: int = 12,
        sink: EventSink | None = None,
        mode: str = "code",
        context_tokens: int | None = None,
        model: Any | None = None,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown mode: {mode}")
        config = load_llm_config()
        self.workspace = Path(workspace).resolve()
        self.max_turns = max_turns
        self.mode = mode
        self.tools = LocalTools(self.workspace, mode=mode, repo_map_chars=config.repo_map_chars)
        self.model = model or ChatModel()
        self.context = ContextManager(max_tokens=context_tokens or config.context_tokens)
        self.repo_map_chars = config.repo_map_chars
        self.sink = sink or (lambda event: None)
        self.last_exchange: list[dict[str, str]] = []

    def run(self, task: str, history: list[dict[str, Any]] | None = None) -> str:
        repo_map = RepoMap(self.workspace).build(task, self.repo_map_chars)
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
            return self._finish(task, f"{repo_map}\n\nEstimated retained context: {estimated} tokens.")

        context_note = history_note or "No earlier chat messages were omitted or compacted."
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{BASE_PROMPT}\n\n{MODE_PROMPTS[self.mode]}"},
            {"role": "system", "content": repo_map},
            {"role": "system", "content": context_note},
            *clean_history,
            {"role": "user", "content": task},
        ]
        context_note_index = 2
        base_count = len(messages)
        compacted_summary: list[str] = []

        for step in range(1, self.max_turns + 1):
            messages, stats = self.context.compact_working_set(
                messages,
                base_count,
                context_note_index,
                compacted_summary,
            )
            self.sink(stats.event())
            self.sink({"type": "step", "step": step, "message": "asking model"})
            assistant = self.model.complete(messages, self.tools.schema())
            messages.append(assistant)

            content = assistant.get("content")
            if content:
                self.sink({"type": "assistant", "content": content})

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return self._finish(task, content or "Model stopped without a final message.")

            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                raw_args = tool_call.get("function", {}).get("arguments") or "{}"
                args = self._parse_args(raw_args)
                self.sink({"type": "tool_call", "name": name, "arguments": args})
                result = self.tools.call(name, args)
                self.sink({"type": "tool_result", "name": name, "ok": result.ok, "output": result.output})

                if name == "finish":
                    return self._finish(task, result.output)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", name),
                        "name": name,
                        "content": result.to_message(),
                    }
                )

        return self._finish(
            task,
            f"Stopped after max_turns={self.max_turns}. Increase --max-turns if the task is larger.",
        )

    def _finish(self, task: str, final: str) -> str:
        self.last_exchange = [
            {"role": "user", "content": task},
            {"role": "assistant", "content": final},
        ]
        return final

    def _parse_args(self, raw_args: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
