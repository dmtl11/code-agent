from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
import ast
import re
import locale
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .code_registry import CodeRegistry, IGNORED_PARTS
from .patching import PatchEngine
from .repo_map import RepoMap
from .services import get_service_manager


class ToolError(RuntimeError):
    """Raised when a local tool call cannot be completed safely."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str

    def to_message(self) -> str:
        return json.dumps({"ok": self.ok, "output": self.output}, ensure_ascii=False)


class LocalTools:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        mode: str = "code",
        repo_map_chars: int = 6000,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.repo_map_chars = repo_map_chars
        self.registry = CodeRegistry(self.workspace)
        self.patch_engine = PatchEngine(self.workspace, self._validate_candidate)

    @property
    def allowed_tools(self) -> set[str]:
        read_only = {"repo_map", "list_files", "read_file", "search_files", "service_status", "service_logs", "finish"}
        if self.mode in {"ask", "architect"}:
            return read_only
        return read_only | {
            "lint_file",
            "replace_in_file",
            "write_file",
            "apply_patch",
            "rollback_patch",
            "run_command",
            "start_service",
            "stop_service",
        }

    def schema(self) -> list[dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "repo_map",
                    "description": "Return a compact repository map with files and important symbols.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Task terms used to prioritize relevant files."}
                        },
                    },
                },
            },
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
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1, "description": "1-based line to start at."},
                            "line_count": {"type": "integer", "minimum": 1, "maximum": 300},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Search text across workspace files and return concise file:line matches.",
                    "parameters": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string"},
                            "path": {"type": "string", "description": "Relative directory to search, default '.'"},
                            "max_matches": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lint_file",
                    "description": "Run a lightweight syntax/lint check before or after editing a source file.",
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
                    "name": "replace_in_file",
                    "description": "Make a focused edit by replacing one exact, unique text block in an existing file.",
                    "parameters": {
                        "type": "object",
                        "required": ["path", "old_text", "new_text"],
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string", "description": "Exact text currently present once in the file."},
                            "new_text": {"type": "string", "description": "Replacement text."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create a new UTF-8 file or replace a small file. Prefer replace_in_file for existing files.",
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
                    "name": "apply_patch",
                    "description": (
                        "Apply an exact-context, multi-file patch atomically. Use dry_run first for risky changes; "
                        "pass base_hashes from read_file to detect stale files."
                    ),
                    "parameters": {
                        "type": "object",
                        "required": ["patch"],
                        "properties": {
                            "patch": {
                                "type": "string",
                                "description": (
                                    "Patch enclosed by *** Begin Patch / *** End Patch with Add, Update, or Delete File sections."
                                ),
                            },
                            "dry_run": {"type": "boolean"},
                            "base_hashes": {
                                "type": "object",
                                "description": "Optional relative-path to SHA-256 prefix map returned by read_file.",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rollback_patch",
                    "description": "Rollback a previously applied patch if its files have not changed since application.",
                    "parameters": {
                        "type": "object",
                        "required": ["transaction_id"],
                        "properties": {"transaction_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a finite shell command and return stdout/stderr. For persistent servers use start_service.",
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
                    "name": "start_service",
                    "description": (
                        "Start a managed, persistent local service without blocking the conversation. "
                        "Use executable/argument arrays, not shell background syntax. Bind dev servers to 127.0.0.1. "
                        "Set port and health_path to verify HTTP readiness. Returns a service ID for status/logs/stop. "
                        "Services survive chat turns, but stop when the Code Agent host exits."
                    ),
                    "parameters": {
                        "type": "object", "required": ["command"],
                        "properties": {
                            "command": {
                                "type": "array", "minItems": 1, "items": {"type": "string"},
                                "description": "Example: [\"python\", \"app.py\", \"--port\", \"8000\"]. No shell operators.",
                            },
                            "cwd": {"type": "string", "description": "Workspace-relative working directory, default '.'"},
                            "name": {"type": "string", "maxLength": 80},
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "health_path": {"type": "string", "description": "Local HTTP path such as /health; requires port. No redirects."},
                            "startup_timeout": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "service_status",
                    "description": "List this workspace's managed services, or inspect one service's process and readiness.",
                    "parameters": {
                        "type": "object", "properties": {"service_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "service_logs",
                    "description": "Read a bounded tail of a managed service's combined stdout/stderr without waiting for exit.",
                    "parameters": {
                        "type": "object", "required": ["service_id"],
                        "properties": {
                            "service_id": {"type": "string"},
                            "lines": {"type": "integer", "minimum": 1, "maximum": 300},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stop_service",
                    "description": "Stop a service and its child processes using its registered service ID, never an arbitrary PID.",
                    "parameters": {
                        "type": "object", "required": ["service_id"],
                        "properties": {"service_id": {"type": "string"}},
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
        return [tool for tool in tools if tool["function"]["name"] in self.allowed_tools]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name not in self.allowed_tools:
                raise ToolError(f"Tool {name} is unavailable in {self.mode} mode")
            if name == "repo_map":
                query = str(arguments.get("query", ""))
                return ToolResult(True, RepoMap(self.workspace, self.registry).build(query, self.repo_map_chars))
            if name == "list_files":
                return self._list_files(arguments.get("path", "."))
            if name == "read_file":
                return self._read_file(
                    arguments["path"],
                    arguments.get("start_line"),
                    arguments.get("line_count"),
                )
            if name == "search_files":
                return self._search_files(
                    arguments["query"],
                    arguments.get("path", "."),
                    int(arguments.get("max_matches", 50)),
                )
            if name == "lint_file":
                return self._lint_file(arguments["path"])
            if name == "replace_in_file":
                return self._replace_in_file(
                    arguments["path"],
                    arguments["old_text"],
                    arguments["new_text"],
                )
            if name == "write_file":
                return self._write_file(arguments["path"], arguments["content"])
            if name == "apply_patch":
                return self._apply_patch(
                    arguments["patch"],
                    bool(arguments.get("dry_run", False)),
                    arguments.get("base_hashes"),
                )
            if name == "rollback_patch":
                return self._rollback_patch(arguments["transaction_id"])
            if name == "run_command":
                return self._run_command(arguments["command"], int(arguments.get("timeout", 20)))
            if name == "start_service":
                command = arguments["command"]
                if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
                    raise ToolError("command must be an executable/arguments array, not a shell string")
                self._guard_command(subprocess.list2cmdline(command))
                result = get_service_manager(self.workspace).start(
                    command, arguments.get("cwd", "."), arguments.get("name", ""),
                    arguments.get("port"), arguments.get("health_path"), arguments.get("startup_timeout", 10),
                )
                ok = result["state"] in {"starting", "running"} and (result["ready"] or result["port"] is None)
                return ToolResult(ok, json.dumps(result, ensure_ascii=False))
            if name == "service_status":
                result = get_service_manager(self.workspace).status(arguments.get("service_id"))
                return ToolResult(True, json.dumps(result, ensure_ascii=False))
            if name == "service_logs":
                result = get_service_manager(self.workspace).logs(arguments["service_id"], arguments.get("lines", 80))
                return ToolResult(True, json.dumps(result, ensure_ascii=False))
            if name == "stop_service":
                result = get_service_manager(self.workspace).stop(arguments["service_id"])
                return ToolResult(True, json.dumps(result, ensure_ascii=False))
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
            if any(part in IGNORED_PARTS for part in rel.parts):
                continue
            suffix = "/" if path.is_dir() else ""
            rows.append(f"{rel.as_posix()}{suffix}")
            if len(rows) >= 200:
                rows.append("... truncated")
                break
        return ToolResult(True, "\n".join(rows) or "(empty)")

    def _read_file(self, raw_path: str, start_line: Any = None, line_count: Any = None) -> ToolResult:
        path = self._resolve(raw_path)
        if not path.is_file():
            raise ToolError(f"Not a file: {raw_path}")
        text = path.read_text(encoding="utf-8")
        self.registry.update_file(path)
        content_hash = self.registry.file_hash(raw_path, refresh=False)
        lines = text.splitlines()
        if not lines:
            return ToolResult(True, f"{raw_path} is empty (0 lines, sha256 {content_hash[:12]}).")
        start = max(1, int(start_line or 1))
        count = max(1, min(int(line_count or 120), 300))
        selected = lines[start - 1 : start - 1 + count]
        numbered = [f"{idx:>4}: {line}" for idx, line in enumerate(selected, start=start)]
        end = start + len(selected) - 1
        header = f"{raw_path} lines {start}-{end} of {len(lines)} (sha256 {content_hash[:12]})"
        if end < len(lines):
            header += f" (pass start_line={end + 1} to continue)"
        return ToolResult(True, "\n".join([header, *numbered]))

    def _search_files(self, query: str, raw_path: str, max_matches: int) -> ToolResult:
        root = self._resolve(raw_path)
        if not root.exists():
            raise ToolError(f"Path does not exist: {raw_path}")
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[str] = []
        needle = query.lower()
        for path in files:
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace)
            if any(part in IGNORED_PARTS for part in rel.parts):
                continue
            if path.suffix.lower() not in {".py", ".cpp", ".cc", ".cxx", ".js", ".html", ".css", ".json", ".md", ".txt"}:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for idx, line in enumerate(lines, start=1):
                if needle in line.lower():
                    snippet = line.strip()
                    if len(snippet) > 140:
                        snippet = snippet[:137] + "..."
                    matches.append(f"{rel.as_posix()}:{idx}: {snippet}")
                    if len(matches) >= max(1, min(max_matches, 100)):
                        return ToolResult(True, "\n".join(matches))
        return ToolResult(True, "\n".join(matches) or "No matches found.")

    def _lint_file(self, raw_path: str) -> ToolResult:
        path = self._resolve(raw_path)
        if not path.is_file():
            raise ToolError(f"Not a file: {raw_path}")

        suffix = path.suffix.lower()
        if suffix == ".py":
            completed = subprocess.run(
                ["python", "-m", "py_compile", str(path.relative_to(self.workspace))],
                cwd=self.workspace,
                text=True,
                capture_output=True,
                timeout=20,
            )
            return self._process_result(completed, "Python syntax check")

        if suffix in {".cpp", ".cc", ".cxx"}:
            compiler = shutil.which("g++") or shutil.which("clang++")
            if not compiler:
                return ToolResult(False, "No g++ or clang++ compiler found for C++ syntax check.")
            completed = subprocess.run(
                [compiler, "-std=c++17", "-fsyntax-only", str(path.relative_to(self.workspace))],
                cwd=self.workspace,
                text=True,
                capture_output=True,
                timeout=20,
            )
            return self._process_result(completed, "C++ syntax check")

        if suffix in {".js", ".mjs", ".cjs"}:
            node = shutil.which("node")
            if not node:
                return ToolResult(False, "No Node.js executable found for JavaScript syntax check.")
            completed = subprocess.run(
                [node, "--check", str(path.relative_to(self.workspace))],
                cwd=self.workspace,
                text=True,
                capture_output=True,
                timeout=20,
            )
            return self._process_result(completed, "JavaScript syntax check")

        return ToolResult(True, f"No linter configured for {suffix or 'extensionless'} files.")

    def _write_file(self, raw_path: str, content: str) -> ToolResult:
        path = self._resolve(raw_path)
        self._validate_candidate(raw_path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.registry.update_file(path)
        return ToolResult(True, f"Wrote {path.relative_to(self.workspace).as_posix()}")

    def _replace_in_file(self, raw_path: str, old_text: str, new_text: str) -> ToolResult:
        path = self._resolve(raw_path)
        if not path.is_file():
            raise ToolError(f"Not a file: {raw_path}")
        if not old_text:
            raise ToolError("old_text must not be empty")
        content = path.read_text(encoding="utf-8")
        matches = content.count(old_text)
        if matches != 1:
            raise ToolError(
                f"Expected old_text to match exactly once in {raw_path}, found {matches}. "
                "Read a smaller line window and provide a unique block."
            )
        line = content[: content.index(old_text)].count("\n") + 1
        candidate = content.replace(old_text, new_text, 1)
        self._validate_candidate(raw_path, candidate)
        path.write_text(candidate, encoding="utf-8")
        self.registry.update_file(path)
        return ToolResult(
            True,
            f"Replaced one block in {path.relative_to(self.workspace).as_posix()} starting at line {line}.",
        )

    def _apply_patch(
        self,
        patch_text: str,
        dry_run: bool,
        base_hashes: Any,
    ) -> ToolResult:
        if base_hashes is not None and not isinstance(base_hashes, dict):
            raise ToolError("base_hashes must be an object mapping relative paths to SHA-256 values")
        result = self.patch_engine.apply(patch_text, dry_run=dry_run, base_hashes=base_hashes)
        if not dry_run:
            for raw_path in result.paths:
                self.registry.update_file(self._resolve(raw_path))
        return ToolResult(True, result.message()[:20000])

    def _rollback_patch(self, transaction_id: str) -> ToolResult:
        result = self.patch_engine.rollback(str(transaction_id))
        for raw_path in result.paths:
            self.registry.update_file(self._resolve(raw_path))
        return ToolResult(
            True,
            f"Rolled back patch {result.transaction_id} for: {', '.join(result.paths)}\n\n{result.diff}"[:20000],
        )

    def _validate_candidate(self, raw_path: str, content: str) -> None:
        if Path(raw_path).suffix.lower() != ".py":
            return
        try:
            ast.parse(content, filename=raw_path)
        except SyntaxError as exc:
            raise ToolError(f"Python syntax error in {raw_path}:{exc.lineno}: {exc.msg}")

    def _run_command(self, command: str, timeout: int) -> ToolResult:
        self._guard_command(command)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        bounded_timeout = max(1, min(timeout, 60))
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                shell=True,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=bounded_timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                stdout = self._read_capture(stdout_file)
                stderr = self._read_capture(stderr_file)
                output = "\n".join(
                    part
                    for part in [
                        f"Command timed out after {bounded_timeout}s; its process tree was terminated.",
                        stdout.strip(),
                        stderr.strip(),
                    ]
                    if part
                )
                return ToolResult(False, output[:12000])
            stdout = self._read_capture(stdout_file)
            stderr = self._read_capture(stderr_file)
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        return self._process_result(completed, "Command")

    @staticmethod
    def _read_capture(stream: Any) -> str:
        stream.flush()
        stream.seek(0)
        return stream.read().decode(locale.getpreferredencoding(False), errors="replace")

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=1)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.poll() is None:
                process.kill()
            return
        try:
            os.killpg(process.pid, 9)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.kill()

    def _process_result(self, completed: subprocess.CompletedProcess[str], success_label: str) -> ToolResult:
        output = "\n".join(
            part
            for part in [
                f"exit_code={completed.returncode}",
                completed.stdout.strip(),
                completed.stderr.strip(),
            ]
            if part
        )
        if output == f"exit_code={completed.returncode}" and completed.returncode == 0:
            output += f"\n{success_label} ran successfully and produced no output."
        return ToolResult(completed.returncode == 0, output[:12000])

    def _guard_command(self, command: str) -> None:
        normalized = command.lower().replace("\\", "/")
        blocked = ["rm -rf /", "del /s", "format ", "shutdown", "powershell -enc"]
        if any(token in normalized for token in blocked):
            raise ToolError(f"Blocked potentially destructive command: {command}")
        background = re.search(r"(?<!&)&(?!&)", command) or "start /b" in normalized or "start-process" in normalized
        if background:
            raise ToolError(
                "Background shell syntax is not supported. Use start_service with an executable/arguments array "
                "for persistent services, then service_status, service_logs, and stop_service to manage them."
            )
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            parts = command.split()
        if parts and parts[0].lower() in {"rm", "rmdir"} and any(flag in parts for flag in ["-rf", "/s"]):
            raise ToolError(f"Blocked recursive delete command: {command}")
