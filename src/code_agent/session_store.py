from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .memory import normalize_memory, update_memory


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / ".code_agent" / "sessions.sqlite3"


class SessionStore:
    """Durable sessions, checkpoints, memory, events, and tool-call state."""

    _TOOL_TRANSITIONS = {
        "pending": {"pending", "running", "interrupted"},
        "running": {"running", "completed", "failed", "interrupted"},
        "completed": {"completed"},
        "failed": {"failed"},
        "interrupted": {"interrupted"},
    }

    def __init__(self, db_path: str | Path = DEFAULT_DB) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS messages_session_idx ON messages(session_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS events_session_idx ON events(session_id, id);
                CREATE TABLE IF NOT EXISTS memories (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tail TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS checkpoints_session_idx ON checkpoints(session_id, id);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, call_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS tool_calls_session_idx ON tool_calls(session_id, id);
                CREATE TABLE IF NOT EXISTS review_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    before_content TEXT NOT NULL,
                    after_content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, run_id, path),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS review_changes_session_idx ON review_changes(session_id, id);
                CREATE TABLE IF NOT EXISTS metric_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    ok INTEGER NOT NULL DEFAULT 1,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_source TEXT NOT NULL DEFAULT 'unavailable',
                    error TEXT NOT NULL DEFAULT '',
                    compacted_blocks INTEGER NOT NULL DEFAULT 0,
                    truncated_tool_results INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS metric_records_session_idx ON metric_records(session_id, id);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_session(self, workspace: str, provider: str = "", model: str = "") -> str:
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        now = self._now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id, workspace, created_at, updated_at, provider, model) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, str(Path(workspace).resolve()), now, now, provider, model),
            )
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str, provider: str = "", model: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE sessions SET updated_at = ?, provider = ?, model = ? WHERE id = ?",
                (self._now(), provider, model, session_id),
            )

    def clear_session(self, session_id: str) -> None:
        with self._lock, self._connect() as db:
            for table in ("messages", "events", "memories", "checkpoints", "tool_calls", "review_changes", "metric_records"):
                db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (self._now(), session_id))

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        role = str(message.get("role") or "unknown")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO messages(session_id, role, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, json.dumps(message, ensure_ascii=False), self._now()),
            )
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (self._now(), session_id))

    def load_messages(self, session_id: str, limit: int = 80) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, max(1, min(limit, 500))),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                value = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.append(value)
        return messages

    def append_event(self, session_id: str, event: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO events(session_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, str(event.get("type") or "unknown"), json.dumps(event, ensure_ascii=False), self._now()),
            )

    def get_memory(self, session_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM memories WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return normalize_memory(None)
        try:
            return normalize_memory(json.loads(row["payload"]))
        except json.JSONDecodeError:
            return normalize_memory(None)

    def update_memory(self, session_id: str, task: str, final: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        memory = update_memory(self.get_memory(session_id), task, final, messages)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO memories(session_id, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (session_id, json.dumps(memory, ensure_ascii=False), self._now()),
            )
        return memory

    def save_checkpoint(self, session_id: str, summary: str, tail: list[dict[str, Any]], estimated_tokens: int) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO checkpoints(session_id, summary, tail, estimated_tokens, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, summary, json.dumps(tail, ensure_ascii=False), estimated_tokens, self._now()),
            )

    def latest_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT summary, tail, estimated_tokens, created_at FROM checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        try:
            tail = json.loads(row["tail"])
        except json.JSONDecodeError:
            tail = []
        return {"summary": row["summary"], "tail": tail, "estimated_tokens": row["estimated_tokens"], "created_at": row["created_at"]}

    def create_tool_call(self, session_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        now = self._now()
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT attempt FROM tool_calls WHERE session_id = ? AND call_id = ?",
                (session_id, call_id),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE tool_calls SET name = ?, arguments = ?, status = 'pending', result = '', "
                    "attempt = ?, updated_at = ? WHERE session_id = ? AND call_id = ?",
                    (name, json.dumps(arguments, ensure_ascii=False), existing["attempt"] + 1, now, session_id, call_id),
                )
            else:
                db.execute(
                    "INSERT INTO tool_calls(session_id, call_id, name, arguments, status, result, attempt, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', '', 1, ?, ?)",
                    (session_id, call_id, name, json.dumps(arguments, ensure_ascii=False), now, now),
                )

    def update_tool_call(self, session_id: str, call_id: str, status: str, result: str = "") -> None:
        allowed = {"pending", "running", "completed", "failed", "interrupted"}
        if status not in allowed:
            raise ValueError(f"Unknown tool call status: {status}")
        with self._lock, self._connect() as db:
            current = db.execute(
                "SELECT status FROM tool_calls WHERE session_id = ? AND call_id = ?",
                (session_id, call_id),
            ).fetchone()
            if not current:
                raise ValueError(f"Unknown tool call: {call_id}")
            if status not in self._TOOL_TRANSITIONS[current["status"]]:
                raise ValueError(f"Invalid tool call transition: {current['status']} -> {status}")
            db.execute(
                "UPDATE tool_calls SET status = ?, result = ?, updated_at = ? WHERE session_id = ? AND call_id = ?",
                (status, result, self._now(), session_id, call_id),
            )

    def recover_interrupted_tools(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT call_id, name, arguments, status FROM tool_calls WHERE session_id = ? AND status IN ('pending', 'running')",
                (session_id,),
            ).fetchall()
            db.execute(
                "UPDATE tool_calls SET status = 'interrupted', updated_at = ? WHERE session_id = ? AND status IN ('pending', 'running')",
                (self._now(), session_id),
            )
        return [dict(row) for row in rows]

    def interrupted_tool_summary(self, session_id: str) -> str:
        with self._connect() as db:
            rows = db.execute(
                "SELECT call_id, name, arguments FROM tool_calls WHERE session_id = ? AND status = 'interrupted' ORDER BY id DESC LIMIT 12",
                (session_id,),
            ).fetchall()
        return "\n".join(f"- {row['call_id']} {row['name']}({row['arguments']})" for row in rows)

    def add_review_change(
        self,
        session_id: str,
        run_id: str,
        path: str,
        before_content: str,
        after_content: str,
    ) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO review_changes(session_id, run_id, path, before_content, after_content, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (session_id, run_id, path, before_content, after_content, self._now()),
            )
            return int(cursor.lastrowid)

    def list_review_changes(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, run_id, path, before_content, after_content, status, created_at "
                "FROM review_changes WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, max(1, min(limit, 200))),
            ).fetchall()
        changes: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["before_preview"] = item.pop("before_content")[:12000]
            item["after_preview"] = item.pop("after_content")[:12000]
            changes.append(item)
        return changes

    def get_review_changes(self, session_id: str, change_ids: list[int]) -> list[dict[str, Any]]:
        if not change_ids:
            return []
        placeholders = ",".join("?" for _ in change_ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, run_id, path, before_content, after_content, status, created_at "
                f"FROM review_changes WHERE session_id = ? AND id IN ({placeholders}) ORDER BY id",
                [session_id, *change_ids],
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_review_changes_merged(self, session_id: str, change_ids: list[int]) -> None:
        if not change_ids:
            return
        placeholders = ",".join("?" for _ in change_ids)
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE review_changes SET status = 'merged' WHERE session_id = ? AND id IN ({placeholders})",
                [session_id, *change_ids],
            )

    def record_metric(self, session_id: str, metric: dict[str, Any]) -> None:
        usage = metric.get("usage") if isinstance(metric.get("usage"), dict) else {}
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO metric_records(" 
                "session_id, run_id, kind, provider, model, tool_name, ok, latency_ms, "
                "prompt_tokens, completion_tokens, total_tokens, usage_source, error, "
                "prompt_cache_hit_tokens, prompt_cache_miss_tokens, compacted_blocks, "
                "truncated_tool_results, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    str(metric.get("run_id") or ""),
                    str(metric.get("kind") or metric.get("type") or "unknown"),
                    str(metric.get("provider") or ""),
                    str(metric.get("model") or ""),
                    str(metric.get("tool_name") or metric.get("name") or ""),
                    1 if metric.get("ok", True) else 0,
                    float(metric.get("latency_ms") or 0),
                    int(usage.get("prompt_tokens") or metric.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or metric.get("completion_tokens") or 0),
                    int(usage.get("total_tokens") or metric.get("total_tokens") or 0),
                    str(metric.get("usage_source") or "unavailable"),
                    str(metric.get("error") or "")[:2000],
                    int(usage.get("prompt_cache_hit_tokens") or metric.get("prompt_cache_hit_tokens") or 0),
                    int(usage.get("prompt_cache_miss_tokens") or metric.get("prompt_cache_miss_tokens") or 0),
                    int(metric.get("compacted_blocks") or 0),
                    int(metric.get("truncated_tool_results") or 0),
                    self._now(),
                ),
            )

    def monitoring_summary(
        self,
        session_id: str | None = None,
        workspace: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("m.session_id = ?")
            params.append(session_id)
        if workspace:
            clauses.append("s.workspace = ?")
            params.append(str(Path(workspace).resolve()))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.* FROM metric_records m JOIN sessions s ON s.id = m.session_id"
                + where
                + " ORDER BY m.id DESC LIMIT ?",
                [*params, max(1, min(limit, 20000))],
            ).fetchall()
        records = [dict(row) for row in reversed(rows)]
        llm = [row for row in records if row["kind"] in {"llm_call", "llm_error"}]
        tools = [row for row in records if row["kind"] == "tool_result"]
        runs = [row for row in records if row["kind"] == "run"]
        contexts = [row for row in records if row["kind"] == "context"]
        llm_success = sum(row["ok"] for row in llm)
        llm_errors = len(llm) - llm_success
        tool_success = sum(row["ok"] for row in tools)
        tool_errors = len(tools) - tool_success
        run_success = sum(row["ok"] for row in runs)
        run_errors = len(runs) - run_success
        latencies = [float(row["latency_ms"]) for row in llm if row["latency_ms"] > 0]
        total_tokens = sum(row["total_tokens"] for row in llm)
        prompt_tokens = sum(row["prompt_tokens"] for row in llm)
        completion_tokens = sum(row["completion_tokens"] for row in llm)
        cache_hit_tokens = sum(row["prompt_cache_hit_tokens"] for row in llm)
        cache_miss_tokens = sum(row["prompt_cache_miss_tokens"] for row in llm)
        actual_calls = sum(row["usage_source"] == "actual" for row in llm)
        estimated_calls = sum(row["usage_source"] == "estimated" for row in llm)

        def rate(errors: int, total: int) -> float:
            return round(errors / total * 100, 2) if total else 0.0

        providers = self._aggregate_monitoring(llm, "model")
        tool_breakdown = self._aggregate_monitoring(tools, "tool_name")
        error_records = [
            {
                "kind": row["kind"],
                "provider": row["provider"],
                "model": row["model"],
                "tool_name": row["tool_name"],
                "error": row["error"] or "Tool returned an error",
                "created_at": row["created_at"],
            }
            for row in reversed(records)
            if not row["ok"]
        ][:20]
        return {
            "summary": {
                "llm_requests": len(llm),
                "llm_successes": llm_success,
                "llm_errors": llm_errors,
                "llm_error_rate": rate(llm_errors, len(llm)),
                "runs": len(runs),
                "completed_runs": run_success,
                "failed_runs": run_errors,
                "run_error_rate": rate(run_errors, len(runs)),
                "tool_calls": len(tools),
                "tool_successes": tool_success,
                "tool_errors": tool_errors,
                "tool_error_rate": rate(tool_errors, len(tools)),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "prompt_cache_hit_tokens": cache_hit_tokens,
                "prompt_cache_miss_tokens": cache_miss_tokens,
                "prompt_cache_hit_rate": round(cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens) * 100, 2)
                if cache_hit_tokens + cache_miss_tokens
                else 0.0,
                "actual_usage_calls": actual_calls,
                "estimated_usage_calls": estimated_calls,
                "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
                "p95_latency_ms": self._percentile(latencies, 95),
                "compactions": sum(row["compacted_blocks"] for row in contexts),
                "truncated_tool_results": sum(row["truncated_tool_results"] for row in contexts),
                "sessions": len({row["session_id"] for row in records}),
                "active_sessions": self._active_session_count(workspace, session_id),
            },
            "providers": providers,
            "tools": tool_breakdown,
            "recent_errors": error_records,
            "last_updated": self._now(),
        }

    def _active_session_count(self, workspace: str | None, session_id: str | None) -> int:
        clauses = ["updated_at >= datetime('now', '-30 minutes')"]
        params: list[Any] = []
        if session_id:
            clauses.append("id = ?")
            params.append(session_id)
        if workspace:
            clauses.append("workspace = ?")
            params.append(str(Path(workspace).resolve()))
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM sessions WHERE " + " AND ".join(clauses), params
            ).fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float:
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((percentile / 100) * len(ordered) + 0.5) - 1))
        return round(ordered[index], 2)

    @staticmethod
    def _aggregate_monitoring(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in records:
            name = str(row.get(key) or "unknown")
            grouped.setdefault(name, []).append(row)
        result: list[dict[str, Any]] = []
        for name, items in grouped.items():
            errors = sum(not item["ok"] for item in items)
            result.append(
                {
                    "name": name,
                    "requests": len(items),
                    "errors": errors,
                    "error_rate": round(errors / len(items) * 100, 2) if items else 0,
                    "total_tokens": sum(item["total_tokens"] for item in items),
                    "avg_latency_ms": round(
                        sum(item["latency_ms"] for item in items if item["latency_ms"] > 0)
                        / max(1, sum(item["latency_ms"] > 0 for item in items)),
                        2,
                    ),
                }
            )
        return sorted(result, key=lambda item: (-item["requests"], item["name"]))
