"""Workspace-scoped services, shared across agent turns in one host process."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from http.client import HTTPException
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
from threading import RLock, Thread
import time
from typing import Any
from urllib import error, request
from uuid import uuid4

from .process_job import WindowsJob


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass
class Service:
    service_id: str
    name: str
    command: list[str]
    cwd: str
    port: int | None
    health_path: str | None
    process: subprocess.Popen
    log_path: Path
    job: WindowsJob | None
    started_at: float = field(default_factory=time.time)
    state: str = "starting"
    failure: str = ""


class ServiceManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.records: dict[str, Service] = {}
        self.lock = RLock()
        self.closed = False

    def _owned_path(self, raw_path: str) -> Path:
        path = (self.workspace / raw_path).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError(f"Path escapes workspace: {raw_path}")
        return path

    def start(
        self, command: list[str], cwd: str = ".", name: str = "",
        port: int | None = None, health_path: str | None = None, startup_timeout: int = 10,
    ) -> dict[str, Any]:
        if not isinstance(command, list) or not command or any(
            not isinstance(arg, str) or "\x00" in arg for arg in command
        ) or not command[0].strip():
            raise ValueError("command must be a non-empty executable/arguments array, not a shell string")
        command = list(command)
        if command[0].lower() in {"python", "python3", "python.exe"}:
            command[0] = sys.executable
        elif os.name == "nt":
            executable = Path(shutil.which(command[0]) or command[0])
            if executable.suffix.lower() in {".cmd", ".bat"}:
                # Bypass npm's cmd wrapper so quoting remains an argv operation.
                cli = executable.parent / "node_modules" / "npm" / "bin" / f"{executable.stem.lower()}-cli.js"
                node = executable.parent / "node.exe"
                if executable.stem.lower() not in {"npm", "npx"} or not cli.is_file():
                    raise ValueError("Batch launchers are not supported; use the underlying python/node executable")
                command = [str(node) if node.is_file() else shutil.which("node") or "node", str(cli), *command[1:]]
        directory = self._owned_path(cwd)
        if not directory.is_dir():
            raise ValueError(f"Not a directory: {cwd}")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("port must be an integer between 1 and 65535")
        if health_path is not None and (
            port is None or not isinstance(health_path, str) or not health_path.startswith("/")
            or health_path.startswith("//") or any(char.isspace() or ord(char) < 32 for char in health_path)
        ):
            raise ValueError("health_path must be a local path such as /health, with port specified")
        if type(startup_timeout) is not int or not 1 <= startup_timeout <= 30:
            raise ValueError("startup_timeout must be between 1 and 30 seconds")
        if not isinstance(name, str) or len(name) > 80:
            raise ValueError("name must be at most 80 characters")
        with self.lock:
            if self.closed:
                raise ValueError("Service manager is shutting down")
            for record in self.records.values():
                self._refresh(record)
                if record.state not in {"starting", "running"}:
                    continue
                if (record.command, record.cwd, record.port, record.health_path) == (
                    command, str(directory), port, health_path,
                ):
                    return {**self._snapshot(record), "reused": True}
                if name and record.name == name:
                    raise ValueError("An active service already has that name; stop it before replacing it")
            if sum(record.state in {"starting", "running"} for record in self.records.values()) >= 8:
                raise ValueError("At most 8 active services are allowed per workspace")
            if port is not None:
                with socket.socket() as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError as exc:
                        raise ValueError(f"Local port {port} is unavailable; choose another port") from exc
            log_dir = self._owned_path(".code_agent/services")
            log_dir.mkdir(parents=True, exist_ok=True)
            service_id = f"svc_{uuid4().hex}"
            log_path = log_dir / f"{service_id}.log"
            job = WindowsJob() if os.name == "nt" else None
            process = None
            try:
                with log_path.open("xb", buffering=0) as log:
                    process = subprocess.Popen(
                        [sys.executable, "-u", str(Path(__file__).with_name("service_worker.py")), json.dumps(command)],
                        cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                        start_new_session=os.name != "nt",
                    )
                    if job:
                        job.assign(process)
                    process.stdin.write(b"1")
                    process.stdin.close()
            except Exception:
                if job:
                    job.close()
                if process:
                    process.kill()
                    process.wait(timeout=5)
                    if process.stdin:
                        process.stdin.close()
                raise
            record = Service(service_id, name or command[0], command, str(directory), port, health_path, process, log_path, job)
            self.records[service_id] = record
            Thread(target=self._watch, args=(record,), daemon=True, name=f"service-{service_id}").start()

        deadline = time.monotonic() + startup_timeout
        while True:
            with self.lock:
                snapshot = self._snapshot(record)
                if record.state not in {"starting", "running"}:
                    return {**snapshot, "log_tail": self.logs(service_id)["output"]}
                if snapshot["ready"] or (port is None and time.time() - record.started_at >= 0.3):
                    record.state = "running"
                    return {**snapshot, "state": "running"}
                if time.monotonic() >= deadline:
                    record.failure = f"Service did not become ready within {startup_timeout}s; process tree stopped"
                    self._terminate(record)
                    record.state = "failed"
                    return {**self._snapshot(record), "log_tail": self.logs(service_id)["output"]}
            time.sleep(0.1)

    def _watch(self, record: Service) -> None:
        record.process.wait()
        with self.lock:
            self._refresh(record)

    def _refresh(self, record: Service) -> None:
        if record.state in {"starting", "running"} and record.process.poll() is not None:
            record.failure = f"Service process exited with code {record.process.returncode}"
            self._terminate(record)
            record.state = "failed" if record.state == "starting" else "exited"

    def _probe(self, record: Service) -> tuple[bool, str]:
        if record.port is None:
            return False, "No port configured; process liveness only, application readiness is unverified"
        try:
            if record.health_path is None:
                with socket.create_connection(("127.0.0.1", record.port), timeout=0.3):
                    return True, "Local TCP port is reachable; HTTP endpoints were not verified"
            # Health checks must not use machine proxies or follow redirects off localhost.
            opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
            with opener.open(f"http://127.0.0.1:{record.port}{record.health_path}", timeout=0.5) as response:
                return 200 <= response.status < 300, f"HTTP {response.status}"
        except (OSError, error.URLError, HTTPException, ValueError) as exc:
            return False, str(exc)

    def _snapshot(self, record: Service) -> dict[str, Any]:
        self._refresh(record)
        active = record.state in {"starting", "running"}
        ready, check = self._probe(record) if active else (False, "Service is not running")
        return {
            "service_id": record.service_id, "name": record.name, "state": record.state,
            "pid": record.process.pid, "pid_kind": "managed launcher", "command": record.command,
            "cwd": str(Path(record.cwd).relative_to(self.workspace)), "port": record.port,
            "url": f"http://127.0.0.1:{record.port}/" if record.port else None,
            "ready": ready, "readiness_check": check, "error": record.failure,
            "exit_code": record.process.returncode,
            "started_at": record.started_at,
            "log_path": str(record.log_path.relative_to(self.workspace)),
        }

    def _get(self, service_id: str) -> Service:
        if service_id not in self.records:
            raise ValueError("Unknown service ID in this workspace/host lifetime; call service_status without an ID")
        return self.records[service_id]

    def status(self, service_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            if service_id:
                return self._snapshot(self._get(service_id))
            return {"services": [self._snapshot(record) for record in self.records.values()]}

    def logs(self, service_id: str, lines: int = 80) -> dict[str, Any]:
        if type(lines) is not int or not 1 <= lines <= 300:
            raise ValueError("lines must be between 1 and 300")
        with self.lock:
            record = self._get(service_id)
            # Read only the tail, even if a service has emitted a very large log.
            with record.log_path.open("rb") as log:
                size = log.seek(0, 2)
                log.seek(max(0, size - 16000))
                data = log.read(16000).decode("utf-8", errors="replace")
            rows = data.splitlines()
            return {
                "service_id": service_id, "output": "\n".join(rows[-lines:]) or "Service has produced no output yet.",
                "truncated": size > 16000 or len(rows) > lines,
            }

    def _terminate(self, record: Service) -> None:
        if record.job:
            record.job.close()
        elif os.name != "nt":
            try:
                os.killpg(record.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        record.process.wait(timeout=5)

    def stop(self, service_id: str) -> dict[str, Any]:
        with self.lock:
            record = self._get(service_id)
            self._refresh(record)
            if record.state in {"starting", "running"}:
                self._terminate(record)
                record.state = "stopped"
            return self._snapshot(record)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for record in self.records.values():
                self.stop(record.service_id)


_managers: dict[Path, ServiceManager] = {}
_managers_lock = RLock()


def get_service_manager(workspace: Path) -> ServiceManager:
    workspace = workspace.resolve()
    with _managers_lock:
        if workspace not in _managers or _managers[workspace].closed:
            _managers[workspace] = ServiceManager(workspace)
        return _managers[workspace]


def shutdown_services() -> None:
    with _managers_lock:
        for manager in _managers.values():
            manager.close()
        _managers.clear()


atexit.register(shutdown_services)
