from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .model import ChatModel
from .tools import LocalTools


SYSTEM_PROMPT = """You are a small coding agent running on the user's local machine.
You can inspect files, edit files, and run commands through tools. Work iteratively:
1. inspect the workspace before editing,
2. make minimal focused changes,
3. run a relevant verification command,
4. call finish with a concise summary.
Never access paths outside the workspace. Do not ask the user to do work that you can do with tools."""


EventSink = Callable[[dict[str, Any]], None]


class CodingAgent:
    def __init__(self, workspace: str | Path, max_turns: int = 12, sink: EventSink | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.max_turns = max_turns
        self.tools = LocalTools(self.workspace)
        self.model = ChatModel()
        self.sink = sink or (lambda event: None)

    def run(self, task: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

        for step in range(1, self.max_turns + 1):
            self.sink({"type": "step", "step": step, "message": "asking model"})
            assistant = self.model.complete(messages, self.tools.schema())
            messages.append(assistant)

            content = assistant.get("content")
            if content:
                self.sink({"type": "assistant", "content": content})

            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return content or "Model stopped without a final message."

            for tool_call in tool_calls:
                name = tool_call.get("function", {}).get("name", "")
                raw_args = tool_call.get("function", {}).get("arguments") or "{}"
                args = self._parse_args(raw_args)
                self.sink({"type": "tool_call", "name": name, "arguments": args})
                result = self.tools.call(name, args)
                self.sink({"type": "tool_result", "name": name, "ok": result.ok, "output": result.output})

                if name == "finish":
                    return result.output

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", name),
                        "name": name,
                        "content": result.to_message(),
                    }
                )

        return f"Stopped after max_turns={self.max_turns}. Increase --max-turns if the task is larger."

    def _parse_args(self, raw_args: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {"raw": raw_args}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
