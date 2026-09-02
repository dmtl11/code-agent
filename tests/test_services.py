from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_agent.agent import CodingAgent
from code_agent.model import _to_anthropic_tool
from code_agent.services import get_service_manager, shutdown_services
from code_agent.session_store import SessionStore
from code_agent.tools import LocalTools


SERVER = """from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path in ('/', '/health') else 404)
        self.end_headers()
        self.wfile.write(b'game-ready')
print('server started', flush=True)
print('stderr captured', file=sys.stderr, flush=True)
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parent / "_work"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="service-", dir=root)
        self.workspace = Path(self.temp.name)
        (self.workspace / "backend folder").mkdir()
        (self.workspace / "backend folder" / "app.py").write_text(SERVER, encoding="utf-8")
        self.tools = LocalTools(self.workspace)

    def tearDown(self) -> None:
        shutdown_services()
        self.temp.cleanup()

    def start_server(self, **overrides):
        port = unused_port()
        args = {
            "command": ["python", "app.py", str(port)], "cwd": "backend folder",
            "port": port, "health_path": "/health", "startup_timeout": 5,
        }
        args.update(overrides)
        result = self.tools.call("start_service", args)
        return result, json.loads(result.output), args

    def wait_closed(self, port: int) -> None:
        deadline = time.monotonic() + 4
        while port_open(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(port_open(port), f"Port {port} was left open")

    def test_lifecycle_across_tool_instances(self) -> None:
        result, started, _ = self.start_server()
        self.assertTrue(result.ok, result.output)
        self.assertTrue(started["ready"])
        self.assertEqual(started["state"], "running")
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(started["url"], timeout=2) as response:
            self.assertEqual(response.read(), b"game-ready")
        tools = LocalTools(self.workspace)
        services = json.loads(tools.call("service_status", {}).output)["services"]
        self.assertEqual(services[0]["service_id"], started["service_id"])
        log = tools.call("service_logs", {"service_id": started["service_id"]})
        self.assertTrue(log.ok)
        self.assertIn("server started", log.output)
        self.assertIn("stderr captured", log.output)
        for _ in range(2):
            stopped = tools.call("stop_service", {"service_id": started["service_id"]})
            self.assertTrue(stopped.ok, stopped.output)
            self.assertEqual(json.loads(stopped.output)["state"], "stopped")
        self.wait_closed(started["port"])

    def test_duplicate_start_reuses_service(self) -> None:
        _, first, args = self.start_server(name="game")
        second = self.tools.call("start_service", args)
        self.assertTrue(second.ok, second.output)
        self.assertEqual(json.loads(second.output)["service_id"], first["service_id"])
        self.assertTrue(json.loads(second.output)["reused"])
        conflict = self.tools.call("start_service", {"command": ["python", "other.py"], "name": "game"})
        self.assertFalse(conflict.ok)

    def test_port_conflict_does_not_kill_existing_process(self) -> None:
        with socket.socket() as external:
            external.bind(("127.0.0.1", 0))
            external.listen()
            port = external.getsockname()[1]
            result = self.tools.call("start_service", {"command": ["python", "app.py"], "port": port})
            self.assertFalse(result.ok)
            self.assertIn("unavailable", result.output)
            self.assertTrue(port_open(port))
            self.assertEqual(get_service_manager(self.workspace).status()["services"], [])

    def test_start_failure_reports_logs(self) -> None:
        result = self.tools.call("start_service", {
            "command": ["python", "-c", "import sys; print('startup broken', flush=True); sys.exit(7)"],
            "port": unused_port(), "startup_timeout": 2,
        })
        self.assertFalse(result.ok, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["exit_code"], 7)
        self.assertIn("startup broken", data["log_tail"])

    def test_readiness_timeout_stops_tree(self) -> None:
        result, data, _ = self.start_server(health_path="/missing", startup_timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(data["state"], "failed")
        self.assertIn("did not become ready", data["error"])
        self.assertIn("404", data["log_tail"])
        self.wait_closed(data["port"])

    def test_silent_process_and_unverified_readiness(self) -> None:
        result = self.tools.call("start_service", {"command": ["python", "-c", "import time; time.sleep(60)"]})
        self.assertTrue(result.ok, result.output)
        data = json.loads(result.output)
        self.assertFalse(data["ready"])
        self.assertIn("unverified", data["readiness_check"])
        log = self.tools.call("service_logs", {"service_id": data["service_id"]})
        self.assertIn("no output", log.output)

    def test_logs_are_bounded(self) -> None:
        result = self.tools.call("start_service", {
            "command": ["python", "-c", "import time; print('x' * 50000, flush=True); print('tail', flush=True); time.sleep(60)"],
        })
        self.assertTrue(result.ok, result.output)
        log = json.loads(self.tools.call("service_logs", {"service_id": json.loads(result.output)["service_id"], "lines": 1}).output)
        self.assertEqual(log["output"], "tail")
        self.assertTrue(log["truncated"])

    def test_read_only_modes_and_workspace_ownership(self) -> None:
        _, service, _ = self.start_server()
        for mode in ("ask", "architect"):
            tools = LocalTools(self.workspace, mode=mode)
            names = {item["function"]["name"] for item in tools.schema()}
            self.assertIn("service_status", names)
            self.assertIn("service_logs", names)
            self.assertNotIn("start_service", names)
            self.assertNotIn("stop_service", names)
            self.assertFalse(tools.call("stop_service", {"service_id": service["service_id"]}).ok)
            self.assertFalse(tools.call("start_service", {"command": ["python"]}).ok)
            self.assertTrue(tools.call("service_status", {}).ok)
        other = LocalTools(self.workspace / "other")
        for tool in ("service_status", "service_logs", "stop_service"):
            result = other.call(tool, {"service_id": service["service_id"]})
            self.assertFalse(result.ok)
        self.assertTrue(port_open(service["port"]))
        self.assertFalse(self.tools.call("stop_service", {"service_id": str(os.getpid())}).ok)

    def test_validation_and_shell_background_rejection(self) -> None:
        cases = [
            {"command": "python app.py"}, {"command": []}, {"command": ["python"], "cwd": ".."},
            {"command": ["python"], "port": 0}, {"command": ["python"], "port": True},
            {"command": ["python"], "health_path": "http://example.com"},
            {"command": ["python"], "port": 8000, "health_path": "//example.com"},
            {"command": ["python"], "port": 8000, "health_path": "/bad path"},
            {"command": ["python"], "startup_timeout": 100},
            {"command": ["cmd", "/c", "start /b python app.py"]},
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(self.tools.call("start_service", args).ok)
        self.assertFalse(self.tools.call("run_command", {"command": "start /b python app.py"}).ok)
        self.assertEqual(get_service_manager(self.workspace).status()["services"], [])

    def test_stop_cleans_grandchildren(self) -> None:
        port = unused_port()
        script = "import subprocess, sys, time; subprocess.Popen([sys.executable, 'app.py', sys.argv[1]]); time.sleep(60)"
        result = self.tools.call("start_service", {
            "command": ["python", "-c", script, str(port)], "cwd": "backend folder",
            "port": port, "health_path": "/health",
        })
        self.assertTrue(result.ok, result.output)
        self.tools.call("stop_service", {"service_id": json.loads(result.output)["service_id"]})
        self.wait_closed(port)

    def test_natural_exit_cleans_grandchildren_without_status_call(self) -> None:
        port = unused_port()
        script = "import subprocess, sys, time; subprocess.Popen([sys.executable, 'app.py', sys.argv[1]]); time.sleep(1.5)"
        result = self.tools.call("start_service", {
            "command": ["python", "-c", script, str(port)], "cwd": "backend folder",
            "port": port, "health_path": "/health",
        })
        self.assertTrue(result.ok, result.output)
        self.wait_closed(port)

    def test_shutdown_stops_services_and_forgets_old_ids(self) -> None:
        _, service, _ = self.start_server()
        shutdown_services()
        self.wait_closed(service["port"])
        self.assertFalse(self.tools.call("stop_service", {"service_id": service["service_id"]}).ok)
        self.assertEqual(json.loads(self.tools.call("service_status", {}).output)["services"], [])

    def test_schema_adapts_to_claude(self) -> None:
        schemas = self.tools.schema()
        original = next(item["function"] for item in schemas if item["function"]["name"] == "start_service")
        converted = next(_to_anthropic_tool(item) for item in schemas if item["function"]["name"] == "start_service")
        self.assertEqual(converted["input_schema"], original["parameters"])
        self.assertEqual(converted["input_schema"]["properties"]["command"]["items"]["type"], "string")

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "Node/npm is not installed")
    def test_node_and_npm_service_in_directory_with_spaces(self) -> None:
        directory = self.workspace / "backend folder"
        (directory / "app.js").write_text(
            "require('http').createServer((req, res) => res.end('ok')).listen(Number(process.argv[2]), '127.0.0.1');\n",
            encoding="utf-8",
        )
        (directory / "package.json").write_text(json.dumps({"scripts": {"start": "node app.js"}}), encoding="utf-8")
        for prefix in (["node", "app.js"], ["npm", "--offline", "--cache", str(directory / ".npm-cache"), "run", "start", "--"]):
            port = unused_port()
            result = self.tools.call("start_service", {
                "command": [*prefix, str(port)], "cwd": "backend folder", "port": port, "health_path": "/", "startup_timeout": 10,
            })
            self.assertTrue(result.ok, result.output)
            self.tools.call("stop_service", {"service_id": json.loads(result.output)["service_id"]})
            self.wait_closed(port)

    def test_host_exit_cleans_services(self) -> None:
        # Windows Job handles are closed by the OS even after a forced host exit.
        for forced in ([False, True] if os.name == "nt" else [False]):
            port = unused_port()
            args = {"command": ["python", "app.py", str(port)], "cwd": "backend folder", "port": port, "health_path": "/health"}
            script = (
                "import sys; from code_agent.tools import LocalTools; "
                f"result = LocalTools({str(self.workspace)!r}).call('start_service', {args!r}); "
                "assert result.ok, result.output; sys.stdin.read(1)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 6
                while not port_open(port) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(port_open(port))
                if forced:
                    process.kill()
                _, stderr = process.communicate(input=b"x" if not forced else None, timeout=6)
                if not forced:
                    self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                self.wait_closed(port)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_can_stop_while_start_waits(self) -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.tools.call, "start_service", {
                "command": ["python", "-c", "import time; time.sleep(60)"], "port": unused_port(), "startup_timeout": 10,
            })
            deadline = time.monotonic() + 3
            records = []
            while not records and time.monotonic() < deadline:
                records = get_service_manager(self.workspace).status()["services"]
                time.sleep(0.05)
            self.assertTrue(records)
            self.assertTrue(self.tools.call("stop_service", {"service_id": records[0]["service_id"]}).ok)
            result = pending.result(timeout=3)
            self.assertFalse(result.ok)
            self.assertEqual(json.loads(result.output)["state"], "stopped")

    def test_agent_dispatch_across_models_in_one_session(self) -> None:
        port = unused_port()
        calls = []

        class Model:
            def __init__(self, actions):
                self.actions = iter(actions)

            def complete(self, messages, tools):
                name, args = next(self.actions)
                self_names = {item["function"]["name"] for item in tools}
                if name not in self_names:
                    raise AssertionError(f"Missing schema: {name}")
                calls.append(name)
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"call-{len(calls)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}

        store = SessionStore(self.workspace / ".code_agent" / "test-sessions.sqlite3")
        session_id = store.create_session(str(self.workspace))
        events = []
        config = SimpleNamespace(provider="deepseek", model="test", repo_map_chars=1000, context_tokens=8000)
        with patch("code_agent.agent.load_llm_config", return_value=config):
            agent = CodingAgent(self.workspace, model=Model([
                ("start_service", {"command": ["python", "app.py", str(port)], "cwd": "backend folder", "port": port, "health_path": "/health"}),
                ("finish", {"summary": "Started"}),
            ]), sink=events.append, session_store=store, session_id=session_id)
            self.assertEqual(agent.run("Start game"), "Started")
            started = json.loads(next(event["output"] for event in events if event["type"] == "tool_result" and event["name"] == "start_service"))
            self.assertTrue(started["ready"])
            config.provider = "claude"
            second = CodingAgent(self.workspace, model=Model([
                ("service_status", {}), ("service_logs", {"service_id": started["service_id"]}),
                ("stop_service", {"service_id": started["service_id"]}), ("finish", {"summary": "Stopped"}),
            ]), sink=events.append, session_store=store, session_id=session_id)
            self.assertEqual(second.run("Stop game", history=store.load_messages(session_id)), "Stopped")
        self.assertTrue(all(event["ok"] for event in events if event["type"] == "tool_result"))
        history = store.load_messages(session_id)
        self.assertTrue(any(message.get("name") == "start_service" for message in history))
        self.assertTrue(any(message.get("name") == "stop_service" for message in history))
        self.wait_closed(port)


if __name__ == "__main__":
    unittest.main()
