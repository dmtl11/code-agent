"""Minimal HTTP server using only the Python standard library.

Endpoints:
    GET  /api/health  -> {"status": "ok"}
    POST /api/echo    -> echoes back the JSON body sent by the client
    GET  /            -> serves the frontend (index.html)
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path):
        # Prevent path traversal.
        safe_path = os.path.normpath(path).lstrip("/\\")
        if not safe_path:
            safe_path = "index.html"
        full_path = os.path.join(STATIC_DIR, safe_path)
        if not full_path.startswith(STATIC_DIR) or not os.path.isfile(full_path):
            self.send_error(404, "Not Found")
            return

        content_type = "text/html"
        if full_path.endswith(".css"):
            content_type = "text/css"
        elif full_path.endswith(".js"):
            content_type = "application/javascript"

        with open(full_path, "rb") as fh:
            body = fh.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Ignore any query string so static files can be requested with
        # cache-busting parameters (e.g. /style.css?v=1).
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send_json(200, {"status": "ok"})
        elif path in ("/", "/index.html"):
            self._send_static("/index.html")
        else:
            self._send_static(path)

    def do_POST(self):
        if self.path != "/api/echo":
            self._send_json(404, {"error": "Not Found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        self._send_json(200, {"echo": data})

    def log_message(self, fmt, *args):
        # Keep console output quiet during tests.
        pass


def run(host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    run()
