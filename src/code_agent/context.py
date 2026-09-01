from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextStats:
    estimated_tokens: int
    max_tokens: int
    compacted_blocks: int = 0
    truncated_tool_results: int = 0

    def event(self) -> dict[str, Any]:
        return {
            "type": "context",
            "estimated_tokens": self.estimated_tokens,
            "max_tokens": self.max_tokens,
            "compacted_blocks": self.compacted_blocks,
            "truncated_tool_results": self.truncated_tool_results,
        }


class ContextManager:
    """Keep model requests inside a predictable local context budget."""

    def __init__(self, max_tokens: int = 16000, reserve_tokens: int = 2500) -> None:
        self.max_tokens = max(4000, max_tokens)
        self.reserve_tokens = min(max(1000, reserve_tokens), self.max_tokens // 2)
        self.target_tokens = self.max_tokens - self.reserve_tokens

    def prepare_history(
        self,
        history: list[dict[str, Any]] | None,
        max_tokens: int = 2500,
    ) -> tuple[list[dict[str, Any]], str]:
        clean: list[dict[str, Any]] = []
        for message in history or []:
            role = message.get("role")
            content = message.get("content")
            if role in {"user", "assistant"}:
                tool_calls = message.get("tool_calls")
                if not (isinstance(content, str) and content.strip()) and not tool_calls:
                    continue
                item: dict[str, Any] = {"role": role, "content": content[:8000] if isinstance(content, str) else content}
                if tool_calls:
                    item["tool_calls"] = tool_calls
                clean.append(item)
            elif role == "tool" and isinstance(content, str) and content.strip():
                clean.append(
                    {
                        "role": role,
                        "content": content[:8000],
                        "tool_call_id": message.get("tool_call_id", ""),
                        "name": message.get("name", ""),
                    }
                )

        kept: list[dict[str, Any]] = []
        used = 0
        for message in reversed(clean):
            cost = self.estimate_tokens(message)
            if kept and used + cost > max_tokens:
                break
            kept.append(message)
            used += cost
        kept.reverse()
        omitted = len(clean) - len(kept)
        summary = f"{omitted} older chat messages were omitted by the history budget." if omitted else ""
        return kept, summary

    def compact_working_set(
        self,
        messages: list[dict[str, Any]],
        base_count: int,
        context_note_index: int,
        prior_summary: list[str],
        preserve_from: int | None = None,
    ) -> tuple[list[dict[str, Any]], ContextStats]:
        truncated = 0
        for message in messages[base_count:]:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content", ""))
            if len(content) > 4000:
                message["content"] = content[:3600] + "\n... tool result truncated by context budget"
                truncated += 1

        compacted = 0
        protected_index = None
        if preserve_from is not None:
            protected_index = max(base_count, min(preserve_from, len(messages) - 1))

        while self.estimate_tokens(messages) > self.target_tokens:
            if protected_index is None:
                start = base_count
            elif protected_index > base_count:
                start = base_count
            elif protected_index + 1 < len(messages):
                start = protected_index + 1
            else:
                break

            limit = protected_index if protected_index is not None and protected_index > start else None
            end = self._next_block_end(messages, start, limit)
            if end <= start:
                break
            block = messages[start:end]
            prior_summary.append(self._summarize_block(block))
            del messages[start:end]
            if protected_index is not None and start < protected_index:
                protected_index -= end - start
            compacted += 1

        if prior_summary:
            messages[context_note_index]["content"] = (
                "Compacted earlier agent activity:\n" + "\n".join(prior_summary[-12:])
            )
        stats = ContextStats(
            estimated_tokens=self.estimate_tokens(messages),
            max_tokens=self.max_tokens,
            compacted_blocks=compacted,
            truncated_tool_results=truncated,
        )
        return messages, stats

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        # This conservative approximation works for mixed Chinese, English, and code.
        ascii_count = sum(1 for char in text if ord(char) < 128)
        non_ascii_count = len(text) - ascii_count
        return max(1, ascii_count // 4 + non_ascii_count)

    def _summarize_block(self, block: list[dict[str, Any]]) -> str:
        if not block:
            return "- empty agent step"
        assistant = block[0]
        content = str(assistant.get("content") or "").replace("\n", " ").strip()
        calls = [
            call.get("function", {}).get("name", "tool")
            for call in assistant.get("tool_calls", [])
            if isinstance(call, dict)
        ]
        details = content[:180] if content else "tool-only step"
        if calls:
            details += f"; tools: {', '.join(calls)}"
        tool_outputs = [
            str(message.get("content") or "").replace("\n", " ").strip()[:120]
            for message in block
            if message.get("role") == "tool" and message.get("content")
        ]
        if tool_outputs:
            details += "; results: " + " | ".join(tool_outputs[:3])
        return f"- {details}"

    @staticmethod
    def _next_block_end(messages: list[dict[str, Any]], start: int, limit: int | None = None) -> int:
        end = min(start + 1, limit) if limit is not None else start + 1
        if end <= start:
            return start
        if messages[start].get("role") in {"assistant", "user"}:
            while end < len(messages) and (limit is None or end < limit) and messages[end].get("role") == "tool":
                end += 1
            if messages[start].get("role") == "user" and end < len(messages) and (limit is None or end < limit):
                if messages[end].get("role") == "assistant":
                    end += 1
                    while end < len(messages) and (limit is None or end < limit) and messages[end].get("role") == "tool":
                        end += 1
        elif messages[start].get("role") == "tool":
            while end < len(messages) and (limit is None or end < limit) and messages[end].get("role") == "tool":
                end += 1
        return end
