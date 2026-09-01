from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def empty_memory() -> dict[str, Any]:
    return {
        "goal": "",
        "project_type": "",
        "decisions": [],
        "completed_tasks": [],
        "active_tasks": [],
        "known_errors": [],
        "files": [],
        "user_preferences": [],
        "last_updated": "",
    }


def normalize_memory(value: dict[str, Any] | None) -> dict[str, Any]:
    memory = empty_memory()
    if isinstance(value, dict):
        for key in memory:
            if key in value:
                memory[key] = value[key]
    for key in ("decisions", "completed_tasks", "active_tasks", "known_errors", "files", "user_preferences"):
        if not isinstance(memory[key], list):
            memory[key] = []
    return memory


def update_memory(
    previous: dict[str, Any] | None,
    task: str,
    final: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Update durable project memory without exposing hidden model reasoning."""
    memory = normalize_memory(previous)
    if task.strip():
        memory["goal"] = task.strip()[:500]
        _append_unique(memory["active_tasks"], task.strip()[:300], limit=12)
    if final.strip():
        _append_unique(memory["completed_tasks"], final.strip()[:500], limit=20)
        for line in final.splitlines():
            if "error" in line.lower() or "failed" in line.lower():
                _append_unique(memory["known_errors"], line.strip()[:300], limit=20)

    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_content = str(message.get("content") or "")
        if '"ok": false' in tool_content.lower() or "error" in tool_content.lower():
            _append_unique(memory["known_errors"], tool_content[:300], limit=20)

    paths = re.findall(r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.(?:py|cpp|cc|cxx|h|hpp|js|jsx|ts|tsx|html|css|json|md)",
                       "\n".join(str(message.get("content") or "") for message in messages))
    for path in paths:
        _append_unique(memory["files"], path.replace("\\", "/"), limit=100)
    memory["last_updated"] = datetime.now(timezone.utc).isoformat()
    return memory


def format_memory(memory: dict[str, Any] | None, checkpoint: str = "", interrupted_tools: str = "") -> str:
    memory = normalize_memory(memory)
    lines = ["Durable project memory:"]
    if memory["goal"]:
        lines.append(f"- goal: {memory['goal']}")
    if memory["project_type"]:
        lines.append(f"- project_type: {memory['project_type']}")
    for key, label in (
        ("decisions", "decisions"),
        ("completed_tasks", "completed_tasks"),
        ("active_tasks", "active_tasks"),
        ("known_errors", "known_errors"),
        ("files", "important_files"),
        ("user_preferences", "user_preferences"),
    ):
        values = memory[key]
        if values:
            lines.append(f"- {label}: " + "; ".join(str(item) for item in values[-12:]))
    if checkpoint:
        lines.append("\nLatest context checkpoint:\n" + checkpoint[:6000])
    if interrupted_tools:
        lines.append("\nInterrupted tool calls that must be inspected before retrying:\n" + interrupted_tools[:3000])
    return "\n".join(lines)


def _append_unique(values: list[Any], value: Any, limit: int) -> None:
    if not value or value in values:
        return
    values.append(value)
    del values[:-limit]
