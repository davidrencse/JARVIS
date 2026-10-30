"""cluster_proxy.ProxyHandler: a node's loopback console forwarding to the main."""

import http.client
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

from jarvis.cluster_proxy import _Body, proxy_handler


class FakeMain(BaseHTTPRequestHandler):
    """Records what reaches the 'main' and answers by path."""
    seen = []

    def log_message(self, *_):
        pass

    def _record(self, body=b""):
        type(self).seen.append({"method": self.command, "path": self.path,
                                "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

    def _reply(self):
        if self.path.startswith("/api/missing"):
            data, status = b'{"error": "nope"}', 404
        elif self.path.startswith("/api/big"):
            data, status = b"x" * 200000, 200
        else:
            data, status = b'{"ok": true}', 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "session=secret")
        self.send_header("Server-Secret", "leak")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._record()
        self._reply()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(n))
        self._reply()


class MainClient:
    """Stands in for cluster.Client: adds the node's bearer token like the real one does."""

    def __init__(self, port, error=None):
        self.port, self.error = port, error
        self.calls = []

    def open(self, path, body=None, timeout=45, headers=None, method=None):
        self.calls.append({"path": path, "headers": dict(headers or {}), "method": method})
        if self.error:
            raise self.error
        data = None
        if body is not None:  # the real Client streams this; drain it the same way (raises on short upload)
            data = b""
            while True:
                block = body.read(65536)
                if not block:
                    break
                data += block
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={**(headers or {}), "Authorization": "Bearer NODE-TOKEN-0123456789"})
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10)


class ProxyBase(unittest.TestCase):
    overlay = None
    setup = None

    @classmethod
    def setUpClass(cls):
        FakeMain.seen = []
        cls.main = ThreadingHTTPServer(("127.0.0.1", 0), FakeMain)
        threading.Thread(target=cls.main.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.main.shutdown()
        cls.main.server_close()

    def start(self, client=None, overlay=None, setup=None):
        self.client = client or MainClient(self.main.server_address[1])
        handler = proxy_handler(self.client, overlay, setup)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        FakeMain.seen.clear()

    def raw(self, method, path, headers=None, body=b"", host="default", length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            elif host is not None:
                conn.putheader("Host", host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if method == "POST":
                conn.putheader("Content-Length", str(len(body) if length is None else length))
            conn.endheaders(body)
            res = conn.getresponse()
            return res.status, res.read(), res
        finally:
            conn.close()


class ProxyForwarding(ProxyBase):
    def setUp(self):
        self.start()

    def test_get_api_forwarded_with_only_xjarvis(self):
        status, body, res = self.raw("GET", "/api/overview?x=1")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        seen = FakeMain.seen[-1]
        self.assertEqual((seen["method"], seen["path"]), ("GET", "/api/overview?x=1"))
        self.assertEqual(self.client.calls[-1]["headers"], {"X-JARVIS": "1"})

    def test_post_forwards_body_and_content_headers(self):
        payload = json.dumps({"text": "hi"}).encode()
        status, _, _ = self.raw("POST", "/api/ask", {"X-JARVIS": "1", "Content-Type": "application/json"}, payload)
        self.assertEqual(status, 200)
        seen = FakeMain.seen[-1]
        self.assertEqual((seen["method"], seen["body"]), ("POST", payload))
        self.assertEqual(seen["headers"]["content-type"], "application/json")

    def test_untrusted_request_headers_are_stripped(self):
        self.raw("GET", "/api/overview", {
            "Authorization": "Bearer ATTACKER", "Cookie": "a=b", "X-JARVIS-Main": "evil",
            "X-Forwarded-For": "6.6.6.6", "Origin": "http://evil.example", "Accept": "text/html",
            "Last-Event-ID": "42"})
        sent = self.client.calls[-1]["headers"]
        self.assertEqual(sent, {"X-JARVIS": "1", "Last-Event-ID": "42"})
        up = FakeMain.seen[-1]["headers"]
        self.assertEqual(up["authorization"], "Bearer NODE-TOKEN-0123456789")  # the node's, not the caller's
        self.assertNotIn("cookie", up)
        self.assertNotIn("x-forwarded-for", up)
        self.assertNotIn("origin", up)
        self.assertNotIn("x-jarvis-main", up)

    def test_upstream_host_is_not_browser_host(self):
        self.raw("GET", "/api/overview")
        self.assertEqual(FakeMain.seen[-1]["headers"]["host"], f"127.0.0.1:{self.main.server_address[1]}")

    def test_response_headers_are_whitelisted(self):
        _, _, res = self.raw("GET", "/api/overview")
        names = {k.lower() for k, _ in res.getheaders()}
        self.assertNotIn("set-cookie", names)
        self.assertNotIn("server-secret", names)
        self.assertIn("cache-control", names)
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")

    def test_large_response_streams_intact(self):
        status, body, _ = self.raw("GET", "/api/big")
        self.assertEqual((status, len(body)), (200, 200000))

    def test_upstream_http_error_status_and_body_passed_through(self):
        status, body, _ = self.raw("GET", "/api/missing")
        self.assertEqual((status, json.loads(body)), (404, {"error": "nope"}))

    def test_upstream_connection_error_is_502(self):
        self.start(MainClient(1, error=OSError("refused")))
        status, body, _ = self.raw("GET", "/api/overview")
        self.assertEqual(status, 502)
        self.assertIn("Main PC unavailable", json.loads(body)["error"])
        self.assertEqual(FakeMain.seen, [])

    def test_upstream_runtime_error_is_502(self):
        self.start(MainClient(1, error=RuntimeError("hub refused")))
        self.assertEqual(self.raw("GET", "/api/overview")[0], 502)


class ProxyTrustBoundary(ProxyBase):
    def setUp(self):
        self.start()

    def test_foreign_host_rejected_get_and_nothing_forwarded(self):
        for host in ("evil.example", "127.0.0.1.evil.com", "192.168.0.9", None):
            with self.subTest(host=host):
                status, body, _ = self.raw("GET", "/api/overview", host=host)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Local console requests only."})
        self.assertEqual(self.client.calls, [])

    def test_cross_site_get_rejected(self):
        self.assertEqual(self.raw("GET", "/api/overview", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.client.calls, [])

    def test_post_needs_xjarvis_host_and_same_site(self):
        self.assertEqual(self.raw("POST", "/api/ask", body=b"{}")[0], 403)
        self.assertEqual(self.raw("POST", "/api/ask", {"X-JARVIS": "1"}, b"{}", host="evil.example")[0], 403)
        self.assertEqual(self.raw("POST", "/api/ask", {"X-JARVIS": "1", "Sec-Fetch-Site": "cross-site"}, b"{}")[0], 403)
        self.assertEqual(self.raw("POST", "/api/ask", {"X-JARVIS": "0"}, b"{}")[0], 403)
        self.assertEqual(self.client.calls, [])

    def test_non_api_post_is_404_and_not_forwarded(self):
        self.assertEqual(self.raw("POST", "/other", {"X-JARVIS": "1"}, b"{}")[0], 404)
        self.assertEqual(self.client.calls, [])

    def test_non_api_get_served_locally_not_forwarded(self):
        status, body, _ = self.raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"JARVIS", body)
        self.assertEqual(self.client.calls, [])

    def test_static_traversal_blocked(self):
        for path in ("/../jarvis.config.json", "/%2e%2e/jarvis.config.json"):
            self.assertEqual(self.raw("GET", path)[0], 404)
        self.assertEqual(self.client.calls, [])

    def test_api_prefix_lookalike_not_forwarded(self):
        self.assertEqual(self.raw("GET", "/apix/overview")[0], 404)
        self.assertEqual(self.client.calls, [])

    def test_oversized_declared_post_length_is_502_before_upstream(self):
        status, body, _ = self.raw("POST", "/api/send", {"X-JARVIS": "1"}, b"", length=3 * 1024 ** 3)
        self.assertEqual(status, 502)
        self.assertIn("Invalid proxy request length", json.loads(body)["error"])
        self.assertEqual(self.client.calls, [])

    def test_negative_or_garbage_length_rejected(self):
        for value in ("-5", "abc"):
            with self.subTest(value=value):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                try:
                    conn.putrequest("POST", "/api/ask", skip_host=True)
                    conn.putheader("Host", f"127.0.0.1:{self.port}")
                    conn.putheader("X-JARVIS", "1")
                    conn.putheader("Content-Length", value)
                    conn.endheaders()
                    self.assertEqual(conn.getresponse().status, 502)
                finally:
                    conn.close()
        self.assertEqual(self.client.calls, [])

    def test_truncated_upload_does_not_hang_and_is_not_success(self):
        # Declares 100 bytes, sends 5, then closes: the proxy must give up (ValueError path).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/ask", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("X-JARVIS", "1")
        conn.putheader("Content-Length", "100")
        conn.endheaders(b"12345")
        conn.sock.shutdown(1)
        try:
            res = conn.getresponse()
            self.assertEqual(res.status, 502)
        except (http.client.HTTPException, OSError):
            pass  # connection dropped without a success response is also acceptable
        finally:
            conn.close()
        self.assertNotIn("ok", [s["body"] for s in FakeMain.seen])

    def test_body_reader_limits_to_declared_length(self):
        import io
        body = _Body(io.BytesIO(b"abcdefghij"), 4)
        self.assertEqual(body.read(), b"abcd")
        self.assertEqual(body.read(), b"")
        short = _Body(io.BytesIO(b"ab"), 4)
        self.assertEqual(short.read(), b"ab")
        with self.assertRaises(ValueError):
            short.read()

    def test_client_open_refuses_scheme_relative_paths(self):
        # Defence in depth: the real Client the proxy uses won't let a path escape the main.
        from jarvis.cluster import Client
        c = Client.__new__(Client)
        for path in ("//evil.example/x", "http://evil.example/", "api/x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                c.open(path)


class ProxyLocalEndpoints(ProxyBase):
    def test_overlay_without_overlay_is_404(self):
        self.start()
        self.assertEqual(self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b'{"action":"hide"}')[0], 404)
        self.assertEqual(self.client.calls, [])

    def test_overlay_handled_locally(self):
        overlay = MagicMock()
        overlay.request.return_value = {"visible": False}
        self.start(overlay=overlay)
        status, body, _ = self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b'{"action":"hide"}')
        self.assertEqual((status, json.loads(body)), (200, {"visible": False}))
        overlay.request.assert_called_once_with("hide")
        self.assertEqual(self.client.calls, [])

    def test_overlay_bad_bodies_are_400(self):
        self.start(overlay=MagicMock())
        self.assertEqual(self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b"")[0], 400)
        self.assertEqual(self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b"{not json")[0], 400)
        self.assertEqual(self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b"[1]")[0], 400)
        self.assertEqual(self.raw("POST", "/api/overlay", {"X-JARVIS": "1"}, b"x" * 9000)[0], 400)
        self.assertEqual(self.client.calls, [])

    def test_voice_setup_answered_locally_never_forwarded(self):
        setup = MagicMock()
        setup.status.return_value = {"state": "idle"}
        setup.devices.return_value = ["mic0"]
        self.start(setup=setup)
        self.assertEqual(json.loads(self.raw("GET", "/api/voice/setup")[1]), {"state": "idle"})
        self.assertEqual(json.loads(self.raw("GET", "/api/voice/devices")[1]), {"devices": ["mic0"]})
        setup.start.return_value = {"started": True}
        status, body, _ = self.raw("POST", "/api/voice/setup", {"X-JARVIS": "1"},
                                   b'{"action":"start","device":"mic0"}')
        self.assertEqual((status, json.loads(body)), (200, {"started": True}))
        setup.start.assert_called_once_with("mic0")
        self.assertEqual(self.client.calls, [])

    def test_voice_setup_rejects_unknown_action_and_unauthenticated(self):
        setup = MagicMock()
        self.start(setup=setup)
        self.assertEqual(self.raw("POST", "/api/voice/setup", {"X-JARVIS": "1"}, b'{"action":"rm -rf"}')[0], 400)
        self.assertEqual(self.raw("POST", "/api/voice/setup", body=b'{"action":"start"}')[0], 403)
        self.assertEqual(self.raw("GET", "/api/voice/setup", host="evil.example")[0], 403)
        setup.start.assert_not_called()

    def test_voice_setup_error_is_500_on_get_and_400_on_post(self):
        setup = MagicMock()
        setup.status.side_effect = RuntimeError("boom")
        setup.start.side_effect = RuntimeError("busy")
        self.start(setup=setup)
        self.assertEqual(self.raw("GET", "/api/voice/setup")[0], 500)
        self.assertEqual(self.raw("POST", "/api/voice/setup", {"X-JARVIS": "1"}, b'{"action":"start"}')[0], 400)

    def test_without_setup_voice_setup_is_forwarded_to_main(self):
        self.start()
        self.raw("GET", "/api/voice/setup")
        self.assertEqual(self.client.calls[-1]["path"], "/api/voice/setup")


if __name__ == "__main__":
    unittest.main()
