"""Unit tests for the echo server using only the Python standard library."""

import json
import threading
import unittest
import urllib.request

import server


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _request(self, path, method="GET", body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, data = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "ok"})

    def test_echo(self):
        status, data = self._request("/api/echo", method="POST", body={"message": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"echo": {"message": "hi"}})

    def test_echo_invalid_json(self):
        url = f"http://127.0.0.1:{self.port}/api/echo"
        req = urllib.request.Request(
            url, data=b"not json", headers={"Content-Type": "application/json"}, method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_route(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_index_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
        self.assertIn("Echo App", body)

    def test_static_with_query_string(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/style.css?v=1") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/css")
            body = resp.read().decode("utf-8")
        self.assertIn("body", body)


if __name__ == "__main__":
    unittest.main()
