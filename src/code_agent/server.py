from __future__ import annotations

import argparse
import json
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent import CodingAgent
from .tools import LocalTools


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
        if parsed.path == "/api/run-stream":
            self._run_agent_stream()
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
        workspace = ROOT / str(payload.get("workspace") or "workspace")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: dict[str, Any]) -> None:
            self._write_ndjson(event)

        try:
            final = CodingAgent(workspace, sink=emit).run(task)
            self._write_ndjson({"type": "final", "ok": True, "content": final, "workspace": str(workspace)})
        except Exception as exc:
            self._write_ndjson({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            self._write_ndjson({"type": "final", "ok": False, "content": str(exc), "workspace": str(workspace)})

    def _save_file(self) -> None:
        try:
            payload = self._read_json_body()
            workspace = ROOT / str(payload.get("workspace") or "workspace")
            raw_path = str(payload["path"])
            content = str(payload.get("content", ""))
            root = workspace.resolve()
            path = (root / raw_path).resolve()
            if path != root and root not in path.parents:
                raise ValueError(f"Path escapes workspace: {raw_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self._json({"ok": True, "path": raw_path, "size": path.stat().st_size})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def _run_file(self) -> None:
        try:
            payload = self._read_json_body()
            workspace = ROOT / str(payload.get("workspace") or "workspace")
            raw_path = str(payload["path"])
            self._resolve_workspace_path(workspace, raw_path)
            safe_path = raw_path.replace('"', '\\"')
            result = LocalTools(workspace).call("run_command", {"command": f'python "{safe_path}"', "timeout": 20})
            self._json({"ok": result.ok, "output": result.output})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/files":
            params = urllib.parse.parse_qs(parsed.query)
            workspace = ROOT / str(params.get("workspace", ["workspace"])[0])
            try:
                self._json({"ok": True, "files": self._list_workspace_files(workspace)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=500)
            return

        if parsed.path == "/api/file":
            params = urllib.parse.parse_qs(parsed.query)
            workspace = ROOT / str(params.get("workspace", ["workspace"])[0])
            raw_path = str(params.get("path", [""])[0])
            try:
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

    def _resolve_workspace_path(self, workspace: Path, raw_path: str) -> Path:
        root = workspace.resolve()
        path = (root / raw_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"Path escapes workspace: {raw_path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {raw_path}")
        return path

    def _list_workspace_files(self, workspace: Path) -> list[dict[str, Any]]:
        root = workspace.resolve()
        if not root.exists():
            return []

        files: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in {".git", "__pycache__", ".venv"} for part in rel.parts):
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
