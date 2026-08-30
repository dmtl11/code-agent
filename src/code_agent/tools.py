from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ToolError(RuntimeError):
    """Raised when a local tool call cannot be completed safely."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str

    def to_message(self) -> str:
        return json.dumps({"ok": self.ok, "output": self.output}, ensure_ascii=False)


class LocalTools:
    def __init__(self, workspace: str | os.PathLike[str]) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files under the workspace or a subdirectory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative path, default '.'"}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a UTF-8 text file from the workspace.",
                    "parameters": {
                        "type": "object",
                        "required": ["path"],
                        "properties": {"path": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a UTF-8 text file inside the workspace.",
                    "parameters": {
                        "type": "object",
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command in the workspace and return stdout/stderr.",
                    "parameters": {
                        "type": "object",
                        "required": ["command"],
                        "properties": {
                            "command": {"type": "string"},
                            "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "description": "Finish the task and report the final answer to the user.",
                    "parameters": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                    },
                },
            },
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "list_files":
                return self._list_files(arguments.get("path", "."))
            if name == "read_file":
                return self._read_file(arguments["path"])
            if name == "write_file":
                return self._write_file(arguments["path"], arguments["content"])
            if name == "run_command":
                return self._run_command(arguments["command"], int(arguments.get("timeout", 20)))
            if name == "finish":
                return ToolResult(True, str(arguments["summary"]))
            raise ToolError(f"Unknown tool: {name}")
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    def _resolve(self, raw_path: str) -> Path:
        path = (self.workspace / raw_path).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ToolError(f"Path escapes workspace: {raw_path}")
        return path

    def _list_files(self, raw_path: str) -> ToolResult:
        root = self._resolve(raw_path)
        if not root.exists():
            raise ToolError(f"Path does not exist: {raw_path}")
        if root.is_file():
            return ToolResult(True, str(root.relative_to(self.workspace)))

        rows: list[str] = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(self.workspace)
            if any(part in {".git", "__pycache__", ".venv"} for part in rel.parts):
                continue
            suffix = "/" if path.is_dir() else ""
            rows.append(f"{rel.as_posix()}{suffix}")
            if len(rows) >= 200:
                rows.append("... truncated")
                break
        return ToolResult(True, "\n".join(rows) or "(empty)")

    def _read_file(self, raw_path: str) -> ToolResult:
        path = self._resolve(raw_path)
        if not path.is_file():
            raise ToolError(f"Not a file: {raw_path}")
        return ToolResult(True, path.read_text(encoding="utf-8"))

    def _write_file(self, raw_path: str, content: str) -> ToolResult:
        path = self._resolve(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(True, f"Wrote {path.relative_to(self.workspace).as_posix()}")

    def _run_command(self, command: str, timeout: int) -> ToolResult:
        self._guard_command(command)
        completed = subprocess.run(
            command,
            cwd=self.workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=max(1, min(timeout, 60)),
        )
        output = "\n".join(
            part
            for part in [
                f"exit_code={completed.returncode}",
                completed.stdout.strip(),
                completed.stderr.strip(),
            ]
            if part
        )
        return ToolResult(completed.returncode == 0, output[:12000])

    def _guard_command(self, command: str) -> None:
        normalized = command.lower().replace("\\", "/")
        blocked = ["rm -rf /", "del /s", "format ", "shutdown", "powershell -enc"]
        if any(token in normalized for token in blocked):
            raise ToolError(f"Blocked potentially destructive command: {command}")
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            parts = command.split()
        if parts and parts[0].lower() in {"rm", "rmdir"} and any(flag in parts for flag in ["-rf", "/s"]):
            raise ToolError(f"Blocked recursive delete command: {command}")
