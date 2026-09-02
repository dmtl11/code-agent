#!/usr/bin/env python3
"""Snake game web server using only the Python standard library.

Serves the static frontend and exposes a small JSON API backed by SQLite:
  GET  /api/scores            -> top 10 leaderboard
  POST /api/scores            -> save a score {name, score}
  GET  /api/health            -> health check
"""

import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
DB_PATH = os.path.join(BASE_DIR, "snake.db")

MAX_NAME_LEN = 20
MAX_SCORE = 1000000
LEADERBOARD_LIMIT = 10

# Content types for static files.
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff -]{1,%d}$" % MAX_NAME_LEN)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the scores table if it does not exist."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                score INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_leaderboard(limit=LEADERBOARD_LIMIT):
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT name, score, created_at
            FROM scores
            ORDER BY score DESC, created_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_score(name, score):
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO scores (name, score) VALUES (?, ?)", (name, score)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def validate_score(payload):
    """Return (name, score) or raise ValueError with a message."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    name = payload.get("name")
    score = payload.get("score")
    if not isinstance(name, str):
        raise ValueError("name 必须是字符串")
    name = name.strip()
    if not name:
        raise ValueError("昵称不能为空")
    if not _NAME_RE.match(name):
        raise ValueError(
            "昵称只能包含字母、数字、下划线、中文、空格和连字符，长度不超过 %d" % MAX_NAME_LEN
        )
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError("score 必须是整数")
    if score < 0 or score > MAX_SCORE:
        raise ValueError("score 必须在 0 到 %d 之间" % MAX_SCORE)
    return name, score


class SnakeHandler(BaseHTTPRequestHandler):
    server_version = "SnakeServer/1.0"

    # ---- helpers ---------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json({"error": message}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _serve_static(self, path):
        # Normalize and prevent path traversal.
        rel = path.lstrip("/")
        if not rel:
            rel = "index.html"
        full = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        if not full.startswith(FRONTEND_DIR):
            self._send_error_json(403, "Forbidden")
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self._send_error_json(404, "Not Found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_error_json(500, "读取文件失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---- HTTP verbs ------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._send_json({"status": "ok"})
        elif path == "/api/scores":
            self._send_json({"scores": get_leaderboard()})
        else:
            self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/scores":
            self._send_error_json(404, "Not Found")
            return
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_error_json(400, "无效的 JSON")
            return
        try:
            name, score = validate_score(payload)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        add_score(name, score)
        self._send_json({"ok": True, "saved": {"name": name, "score": score}}, 201)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), SnakeHandler)
    print("Snake server running at http://%s:%d  (frontend: %s)" % (host, port, FRONTEND_DIR))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
