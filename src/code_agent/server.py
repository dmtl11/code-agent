from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
from time import perf_counter
import urllib.parse
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent import CodingAgent
from .code_registry import CodeRegistry, IGNORED_PARTS, TEXT_SUFFIXES
from .config import PROVIDER_DEFAULTS, load_llm_config
from .session_store import SessionStore


ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/clear"):
            session_id = parsed.path.split("/")[3]
            store = SessionStore()
            if not store.get_session(session_id):
                self._json({"ok": False, "error": "Session not found"}, status=404)
                return
            store.clear_session(session_id)
            self._json({"ok": True, "session_id": session_id})
            return

        if parsed.path == "/api/run-stream":
            self._run_agent_stream()
            return

        if parsed.path == "/api/reviews/merge":
            self._merge_reviews()
            return

        if parsed.path == "/api/reviews/rollback":
            self._rollback_reviews()
            return

        if parsed.path == "/api/file":
            self._save_file()
            return

        if parsed.path == "/api/run-file":
            self._run_file()
            return

        if parsed.path == "/api/run":
            self._json(
                {
                    "ok": False,
                    "events": [
                        {
                            "type": "error",
                            "message": "The page is using an old script. Refresh with Ctrl+F5.",
                        }
                    ],
                    "error": "The page is using an old script. Refresh with Ctrl+F5.",
                },
                status=409,
            )
            return

        if parsed.path != "/api/run-stream":
            self.send_error(404)
            return

    def _run_agent_stream(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return

        task = str(payload.get("task") or "Create a hello world Python script and run it.")
        mode = str(payload.get("mode") or "code")
        provider = str(payload.get("provider") or "").strip() or None
        try:
            workspace = self._resolve_workspace(str(payload.get("workspace") or "workspace"))
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return

        store = SessionStore()
        session_id = str(payload.get("session_id") or "").strip()
        session = store.get_session(session_id) if session_id else None
        if session_id and not session:
            self._json({"ok": False, "error": f"Session not found: {session_id}"}, status=404)
            return
        if session and Path(session["workspace"]).resolve() != workspace.resolve():
            self._json({"ok": False, "error": "Session belongs to a different workspace."}, status=400)
            return
        if not session:
            session_id = store.create_session(str(workspace), provider or "", "")
        history = store.load_messages(session_id)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        before_snapshot = self._snapshot_workspace(workspace)
        run_started = perf_counter()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: dict[str, Any]) -> None:
            persisted_event = {**event, "run_id": run_id}
            store.append_event(session_id, persisted_event)
            if event.get("type") in {"route", "llm_call", "llm_error", "tool_result", "context"}:
                store.record_metric(session_id, persisted_event)
            self._write_ndjson(persisted_event)

        try:
            self._write_ndjson({"type": "session", "session_id": session_id})
            agent = CodingAgent(
                workspace,
                sink=emit,
                mode=mode,
                provider=provider,
                session_store=store,
                session_id=session_id,
            )
            final = agent.run(task, history=history)
            review_changes = self._record_review_changes(store, session_id, run_id, before_snapshot, workspace)
            store.record_metric(
                session_id,
                {
                    "kind": "run",
                    "run_id": run_id,
                    "provider": agent.provider,
                    "model": agent.model_name,
                    "ok": True,
                    "latency_ms": round((perf_counter() - run_started) * 1000, 2),
                },
            )
            self._write_ndjson(
                {
                    "type": "final",
                    "ok": True,
                    "content": final,
                    "workspace": str(workspace),
                    "session_id": session_id,
                    "memory": store.get_memory(session_id),
                    "exchange": agent.last_exchange,
                    "review_changes": review_changes,
                }
            )
        except Exception as exc:
            review_changes = self._record_review_changes(store, session_id, run_id, before_snapshot, workspace)
            store.record_metric(
                session_id,
                {
                    "kind": "run",
                    "run_id": run_id,
                    "provider": provider or "",
                    "model": "",
                    "ok": False,
                    "latency_ms": round((perf_counter() - run_started) * 1000, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            self._write_ndjson({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            self._write_ndjson(
                {
                    "type": "final",
                    "ok": False,
                    "content": str(exc),
                    "workspace": str(workspace),
                    "session_id": session_id,
                    "review_changes": review_changes,
                }
            )

    def _merge_reviews(self) -> None:
        try:
            payload = self._read_json_body()
            session_id = str(payload.get("session_id") or "").strip()
            raw_ids = payload.get("change_ids")
            if not session_id or not isinstance(raw_ids, list) or len(raw_ids) < 2:
                raise ValueError("Select at least two review changes to merge.")
            change_ids = sorted({int(value) for value in raw_ids})
            store = SessionStore()
            session = store.get_session(session_id)
            if not session:
                self._json({"ok": False, "error": "Session not found"}, status=404)
                return
            workspace = Path(session["workspace"]).resolve()
            changes = store.get_review_changes(session_id, change_ids)
            if len(changes) != len(change_ids):
                raise ValueError("One or more review changes do not belong to this session.")
            if any(change["status"] != "pending" for change in changes):
                raise ValueError("Only pending review changes can be merged.")

            merged: dict[str, tuple[bool, str]] = {}
            grouped: dict[str, list[dict[str, Any]]] = {}
            for change in changes:
                grouped.setdefault(change["path"], []).append(change)
            for raw_path, path_changes in grouped.items():
                path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
                path_changes.sort(key=lambda item: item["id"])
                latest = path_changes[-1]
                current_exists = path.is_file()
                current = path.read_text(encoding="utf-8") if current_exists else ""
                matches_before = current_exists == latest["before_exists"] and current == latest["before_content"]
                matches_after = current_exists == latest["after_exists"] and current == latest["after_content"]
                if not (matches_before or matches_after):
                    raise ValueError(
                        f"Conflict in {raw_path}: the workspace changed after this review. Refresh the review first."
                    )
                merged[raw_path] = (latest["after_exists"], latest["after_content"])

            for raw_path, (exists, content) in merged.items():
                path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
                if exists and path.suffix.lower() == ".py":
                    ast.parse(content, filename=raw_path)

            self._write_review_states(workspace, merged)
            store.mark_review_changes_merged(session_id, change_ids)
            self._json({"ok": True, "session_id": session_id, "change_ids": change_ids, "paths": list(merged)})
        except (ValueError, TypeError, SyntaxError, OSError) as exc:
            self._json({"ok": False, "error": str(exc)}, status=409)

    def _rollback_reviews(self) -> None:
        try:
            payload = self._read_json_body()
            session_id = str(payload.get("session_id") or "").strip()
            raw_ids = payload.get("change_ids")
            if not session_id or not isinstance(raw_ids, list) or not raw_ids:
                raise ValueError("Select at least one review change to roll back.")
            change_ids = sorted({int(value) for value in raw_ids})
            store = SessionStore()
            session = store.get_session(session_id)
            if not session:
                self._json({"ok": False, "error": "Session not found"}, status=404)
                return
            workspace = Path(session["workspace"]).resolve()
            changes = store.get_review_changes(session_id, change_ids)
            if len(changes) != len(change_ids):
                raise ValueError("One or more review changes do not belong to this session.")
            if any(change["status"] != "pending" for change in changes):
                raise ValueError("Only pending review changes can be rolled back.")

            targets: dict[str, tuple[bool, str]] = {}
            grouped: dict[str, list[dict[str, Any]]] = {}
            for change in changes:
                grouped.setdefault(change["path"], []).append(change)
            for raw_path, path_changes in grouped.items():
                path_changes.sort(key=lambda item: item["id"])
                earliest = path_changes[0]
                latest = path_changes[-1]
                path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
                current_exists = path.is_file()
                current = path.read_text(encoding="utf-8") if current_exists else ""
                if current_exists != latest["after_exists"] or current != latest["after_content"]:
                    raise ValueError(
                        f"Rollback conflict in {raw_path}: the file changed after the selected review."
                    )
                targets[raw_path] = (earliest["before_exists"], earliest["before_content"])

            for raw_path, (exists, content) in targets.items():
                if exists and Path(raw_path).suffix.lower() == ".py":
                    ast.parse(content, filename=raw_path)
            self._write_review_states(workspace, targets)
            store.mark_review_changes_rolled_back(session_id, change_ids)
            self._json({"ok": True, "session_id": session_id, "change_ids": change_ids, "paths": list(targets)})
        except (ValueError, TypeError, SyntaxError, OSError) as exc:
            self._json({"ok": False, "error": str(exc)}, status=409)

    def _write_review_states(self, workspace: Path, targets: dict[str, tuple[bool, str]]) -> None:
        originals: dict[str, tuple[bool, str]] = {}
        for raw_path in targets:
            path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
            originals[raw_path] = (path.is_file(), path.read_text(encoding="utf-8") if path.is_file() else "")
        try:
            for raw_path, (exists, content) in targets.items():
                path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
                if exists:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                elif path.exists():
                    path.unlink()
        except Exception:
            for raw_path, (exists, content) in originals.items():
                path = self._resolve_workspace_path(workspace, raw_path, require_file=False)
                if exists:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                elif path.exists():
                    path.unlink()
            raise
        registry = CodeRegistry(workspace)
        for raw_path in targets:
            registry.update_file(self._resolve_workspace_path(workspace, raw_path, require_file=False))

    def _record_review_changes(
        self,
        store: SessionStore,
        session_id: str,
        run_id: str,
        before: dict[str, str],
        workspace: Path,
    ) -> list[dict[str, Any]]:
        after = self._snapshot_workspace(workspace)
        changes: list[dict[str, Any]] = []
        for raw_path in sorted(set(before) | set(after)):
            before_content = before.get(raw_path, "")
            after_content = after.get(raw_path, "")
            if before_content == after_content and (raw_path in before) == (raw_path in after):
                continue
            change_id = store.add_review_change(
                session_id,
                run_id,
                raw_path,
                before_content,
                after_content,
                before_exists=raw_path in before,
                after_exists=raw_path in after,
            )
            changes.append({"id": change_id, "run_id": run_id, "path": raw_path, "status": "pending"})
        return changes

    def _snapshot_workspace(self, workspace: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        if not workspace.exists():
            return snapshot
        for path in workspace.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            rel = path.relative_to(workspace)
            if any(part in {".git", "__pycache__", ".venv", ".code_agent_build", ".code_agent"} for part in rel.parts):
                continue
            try:
                snapshot[rel.as_posix()] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return snapshot

    def _save_file(self) -> None:
        try:
            payload = self._read_json_body()
            workspace = self._resolve_workspace(str(payload.get("workspace") or "workspace"))
            raw_path = str(payload["path"])
            content = str(payload.get("content", ""))
            root = workspace.resolve()
            path = (root / raw_path).resolve()
            if path != root and root not in path.parents:
                raise ValueError(f"Path escapes workspace: {raw_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            CodeRegistry(workspace).update_file(path)
            self._json({"ok": True, "path": raw_path, "size": path.stat().st_size})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def _run_file(self) -> None:
        try:
            payload = self._read_json_body()
            workspace = self._resolve_workspace(str(payload.get("workspace") or "workspace"))
            raw_path = str(payload["path"])
            path = self._resolve_workspace_path(workspace, raw_path)
            self._json(self._execute_source_file(workspace.resolve(), path))
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/sessions/"):
            session_id = parsed.path.split("/")[3]
            store = SessionStore()
            session = store.get_session(session_id)
            if not session:
                self._json({"ok": False, "error": "Session not found"}, status=404)
                return
            self._json(
                {
                    "ok": True,
                    "session": session,
                    "messages": store.load_messages(session_id),
                    "memory": store.get_memory(session_id),
                    "checkpoint": store.latest_checkpoint(session_id),
                    "reviews": store.list_review_changes(session_id),
                }
            )
            return

        if parsed.path == "/api/reviews":
            params = urllib.parse.parse_qs(parsed.query)
            session_id = str(params.get("session_id", [""])[0]).strip()
            if not session_id:
                self._json({"ok": False, "error": "session_id is required"}, status=400)
                return
            store = SessionStore()
            if not store.get_session(session_id):
                self._json({"ok": False, "error": "Session not found"}, status=404)
                return
            self._json({"ok": True, "reviews": store.list_review_changes(session_id)})
            return

        if parsed.path == "/api/providers":
            labels = {
                "auto": "Auto",
                "deepseek": "DeepSeek",
                "openai": "ChatGPT (CloseAI)",
                "claude": "Claude (CloseAI)",
                "qwen": "Qwen",
            }
            self._json(
                {
                    "ok": True,
                    "providers": [
                        {
                            "id": provider,
                            "label": labels[provider],
                            "protocol": "router" if provider == "auto" else load_llm_config(provider=provider).protocol,
                            "default_model": load_llm_config(provider=provider).model,
                            "context_tokens": load_llm_config(provider=provider).context_tokens,
                            "routing": "task-aware" if provider == "auto" else "fixed",
                        }
                        for provider in labels
                    ],
                }
            )
            return

        if parsed.path == "/api/monitoring":
            params = urllib.parse.parse_qs(parsed.query)
            workspace_name = str(params.get("workspace", [""])[0]).strip()
            session_id = str(params.get("session_id", [""])[0]).strip() or None
            try:
                workspace = self._resolve_workspace(workspace_name) if workspace_name else None
                store = SessionStore()
                if session_id and not store.get_session(session_id):
                    self._json({"ok": False, "error": "Session not found"}, status=404)
                    return
                summary = store.monitoring_summary(
                    session_id=session_id,
                    workspace=str(workspace) if workspace else None,
                )
                self._json({"ok": True, "scope": "session" if session_id else "workspace", **summary})
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
            return

        if parsed.path == "/api/files":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                workspace = self._resolve_workspace(str(params.get("workspace", ["workspace"])[0]))
                self._json({"ok": True, "files": self._list_workspace_files(workspace)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=500)
            return

        if parsed.path == "/api/file":
            params = urllib.parse.parse_qs(parsed.query)
            raw_path = str(params.get("path", [""])[0])
            try:
                workspace = self._resolve_workspace(str(params.get("workspace", ["workspace"])[0]))
                path = self._resolve_workspace_path(workspace, raw_path)
                self._json({"ok": True, "path": raw_path, "content": path.read_text(encoding="utf-8")})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=500)
            return

        return super().do_GET()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _write_ndjson(self, event: dict[str, Any]) -> None:
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()

    def _execute_source_file(self, workspace: Path, path: Path) -> dict[str, Any]:
        suffix = path.suffix.lower()
        rel_path = path.relative_to(workspace)
        if suffix == ".py":
            return self._run_process(["python", str(rel_path)], workspace, "python")
        if suffix in {".cpp", ".cc", ".cxx"}:
            compiler = shutil.which("g++") or shutil.which("clang++") or shutil.which("cl")
            if not compiler:
                return {
                    "ok": False,
                    "output": "No C++ compiler found. Install g++, clang++, or MSVC cl and try again.",
                }

            build_dir = workspace / ".code_agent_build"
            build_dir.mkdir(parents=True, exist_ok=True)
            exe_name = path.stem + (".exe" if os.name == "nt" else "")
            exe_path = build_dir / exe_name
            if Path(compiler).name.lower() == "cl.exe":
                compile_cmd = [compiler, "/nologo", "/EHsc", str(rel_path), f"/Fe:{exe_path}"]
            else:
                compile_cmd = [
                    compiler,
                    "-std=c++17",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    str(rel_path),
                    "-o",
                    str(exe_path),
                ]

            compile_result = self._run_process(compile_cmd, workspace, "compile")
            if not compile_result["ok"]:
                return compile_result
            run_result = self._run_process([str(exe_path)], workspace, "run")
            run_result["output"] = compile_result["output"] + "\n\n" + run_result["output"]
            return run_result
        return {"ok": False, "output": f"Run File supports Python and C++ only, not {suffix or 'extensionless'} files."}

    def _run_process(self, command: list[str], cwd: Path, label: str) -> dict[str, Any]:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=20,
        )
        command_text = " ".join(command)
        output = "\n".join(
            part
            for part in [
                f"$ {command_text}",
                f"exit_code={completed.returncode}",
                completed.stdout.strip(),
                completed.stderr.strip(),
            ]
            if part
        )
        if completed.returncode == 0 and output == f"$ {command_text}\nexit_code=0":
            output += f"\n{label} succeeded and produced no output."
        return {"ok": completed.returncode == 0, "output": output}

    def _resolve_workspace_path(self, workspace: Path, raw_path: str, require_file: bool = True) -> Path:
        root = workspace.resolve()
        path = (root / raw_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"Path escapes workspace: {raw_path}")
        if require_file and not path.is_file():
            raise ValueError(f"Not a file: {raw_path}")
        return path

    def _resolve_workspace(self, raw_path: str) -> Path:
        root = ROOT.resolve()
        workspace = (root / raw_path).resolve()
        if workspace != root and root not in workspace.parents:
            raise ValueError(f"Workspace escapes project root: {raw_path}")
        return workspace

    def _list_workspace_files(self, workspace: Path) -> list[dict[str, Any]]:
        root = workspace.resolve()
        if not root.exists():
            return []

        files: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in IGNORED_PARTS for part in rel.parts):
                continue
            if path.is_file():
                files.append(
                    {
                        "path": rel.as_posix(),
                        "name": rel.name,
                        "size": path.stat().st_size,
                        "language": self._language_for(path),
                    }
                )
            if len(files) >= 200:
                break
        return files

    def _language_for(self, path: Path) -> str:
        return {
            ".py": "Python",
            ".cpp": "C++",
            ".cc": "C++",
            ".cxx": "C++",
            ".js": "JavaScript",
            ".html": "HTML",
            ".css": "CSS",
            ".json": "JSON",
            ".md": "Markdown",
            ".txt": "Text",
        }.get(path.suffix.lower(), "Text")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the coding-agent web demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print(f"Code Agent Harness demo: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
