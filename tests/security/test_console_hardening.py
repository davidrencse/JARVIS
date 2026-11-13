"""Loopback console hardening: Origin, browser content types, headers, static paths.

The server stays on 127.0.0.1 with an ephemeral port. Nothing here binds another
address, starts a daemon, or touches a real pointer.
"""

import contextlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import control
from jarvis.events import Bus
from jarvis.ui import Handler, _origin_allowed, _static_target


class FakeConsole(MagicMock):
    """do_GET builds its route table up front, so every attribute must exist."""


class ConsoleHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = FakeConsole()
        cls.console.confirmed = []
        cls.console.confirmations.return_value = {"pending": []}
        cls.console.confirmation.side_effect = lambda body: cls.console.confirmed.append(body) or {"ok": True}
        handler = type("H", (Handler,), {"console": cls.console, "hosts": ("127.0.0.1", "localhost", "::1")})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.console.confirmed.clear()
        self.console.overview.side_effect = None
        self.console.screen.side_effect = None
        self.console.fs_list.side_effect = None
        self.console.confirmation.side_effect = (
            lambda body: self.console.confirmed.append(body) or {"ok": True})

    def raw(self, method, path, headers=None, body=None, host="default", extra=()):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            elif host is not None:
                conn.putheader("Host", host)
            for key, value in list((headers or {}).items()) + list(extra):
                conn.putheader(key, value)
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            elif body is None:
                data = b""
            else:
                data = json.dumps(body).encode()
            if method in ("POST", "PUT", "PATCH", "DELETE") or data:
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            res = conn.getresponse()
            return res.status, res.read(), res
        finally:
            conn.close()

    def origin(self, host="127.0.0.1"):
        return f"http://{host}:{self.port}"

    def test_api_json_carries_isolation_headers(self):
        status, body, res = self.raw("GET", "/api/confirm")
        self.assertEqual((status, json.loads(body)), (200, {"pending": []}))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(res.getheader("Cross-Origin-Resource-Policy"), "same-origin")
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        self.assertIsNone(res.getheader("Access-Control-Allow-Private-Network"))
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        # Stamped once: a repeated header would be joined with a comma.
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")

    def test_html_csp_allows_the_skeleton_styles_only(self):
        status, body, res = self.raw("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"JARVIS", body)
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(res.getheader("Cache-Control"), "no-cache")

    def test_event_stream_is_stamped_and_ends_when_the_bus_closes(self):
        bus = Bus()
        previous = self.console.bus, self.console.brain
        self.console.bus = bus
        self.console.brain = lambda: "off"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("GET", "/api/events", skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"127.0.0.1:{self.port}")
            conn.endheaders()
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertTrue(res.getheader("Content-Type").startswith("text/event-stream"))
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(res.getheader("Cross-Origin-Resource-Policy"), "same-origin")
            self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
            self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
            self.assertTrue(bus.subs)
            with bus.lock:
                for sub in list(bus.subs):
                    sub.put_nowait(None)
            self.assertEqual(res.read(1), b"r")  # "retry: ..." — one byte, then drop the socket
        finally:
            with bus.lock:
                for sub in list(bus.subs):
                    sub.put_nowait(None)  # ends _stream_events; the queue is not full
            conn.close()
            self.console.bus, self.console.brain = previous

    def test_present_origin_must_be_this_server(self):
        bad = (
            "null",
            f"https://127.0.0.1:{self.port}",
            "http://127.0.0.1:1",
            "http://evil.example",
            self.origin() + "/stolen",
            f"http://user:pw@127.0.0.1:{self.port}",
            f"http://127.0.0.1:{self.port}?x=1",
            f"http://127.0.0.1:{self.port}#x",
        )
        for origin in bad:
            with self.subTest(origin=origin):
                status, body, _ = self.raw("GET", "/api/confirm", {"Origin": origin})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Requests from other sites are refused."})
        status, _, _ = self.raw("GET", "/api/confirm", extra=(
            ("Origin", self.origin()), ("Origin", self.origin())))
        self.assertEqual(status, 403)
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(self.raw("GET", "/api/confirm", {"Origin": self.origin(host)})[0], 200)
        self.assertEqual(self.raw("GET", "/api/confirm")[0], 200)  # no Origin: curl / urllib
        self.assertEqual(self.raw("GET", "/api/confirm", {
            "Origin": self.origin(), "Sec-Fetch-Site": "same-origin"})[0], 200)
        for site in ("same-site", "cross-site"):
            with self.subTest(site=site):
                self.assertEqual(self.raw("GET", "/api/confirm", {"Sec-Fetch-Site": site})[0], 403)

    def test_origin_without_a_bound_port_fails_closed(self):
        self.assertFalse(_origin_allowed({"Origin": "http://127.0.0.1:7777"}, ("127.0.0.1",), None))

    def test_sensitive_gets_reject_a_foreign_origin_before_the_handler(self):
        calls = []
        self.console.screen.side_effect = lambda *a, **k: calls.append("screen") or (b"\xff\xd8", "image/jpeg")
        self.console.fs_list.side_effect = lambda *a, **k: calls.append("fs") or {"entries": []}
        for path in ("/api/screen", "/api/fs", "/api/events"):
            with self.subTest(path=path):
                status, _, _ = self.raw("GET", path, {"Origin": "http://evil.example"})
                self.assertEqual(status, 403)
        self.assertEqual(calls, [])

    def test_browser_simple_content_types_cannot_post(self):
        good = {"X-JARVIS": "1", "Origin": self.origin(), "Sec-Fetch-Site": "same-origin"}
        for ctype in ("text/plain", "application/x-www-form-urlencoded",
                      "multipart/form-data", "text/plain;charset=UTF-8"):
            with self.subTest(ctype=ctype):
                status, _, _ = self.raw("POST", "/api/confirm", {**good, "Content-Type": ctype}, {"id": "x"})
                self.assertEqual(status, 403)
        status, _, _ = self.raw("POST", "/api/confirm", good, {"id": "x"})  # browser, no Content-Type
        self.assertEqual(status, 403)
        status, _, _ = self.raw("POST", "/api/confirm", good, {"id": "x"}, extra=(
            ("Content-Type", "application/json"), ("Content-Type", "text/plain")))
        self.assertEqual(status, 403)
        self.assertEqual(self.console.confirmed, [])
        for ctype in ("application/json", "application/json; charset=utf-8", "application/octet-stream"):
            with self.subTest(ctype=ctype):
                status, body, _ = self.raw("POST", "/api/confirm", {**good, "Content-Type": ctype}, {"id": ctype})
                self.assertEqual(status, 200, body)
        self.assertEqual([row["id"] for row in self.console.confirmed],
                         ["application/json", "application/json; charset=utf-8", "application/octet-stream"])

    def test_urllib_form_post_without_browser_headers_still_works(self):
        # urllib.request.Request defaults POST to application/x-www-form-urlencoded and sends no Origin.
        status, body, _ = self.raw(
            "POST", "/api/confirm",
            {"X-JARVIS": "1", "Content-Type": "application/x-www-form-urlencoded"},
            {"id": "cli"})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.console.confirmed, [{"id": "cli"}])

    def test_unsupported_methods_are_not_a_cors_preflight(self):
        status, body, res = self.raw("OPTIONS", "/api/confirm")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body), {"error": "Method not allowed."})
        self.assertEqual(res.getheader("Allow"), "GET, HEAD, POST")
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        self.assertIsNone(res.getheader("Access-Control-Allow-Private-Network"))
        status, _, res = self.raw("PUT", "/api/confirm", body=b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(res.getheader("Allow"), "GET, HEAD, POST")
        status, body, res = self.raw("OPTIONS", "/api/confirm", {"Origin": "http://evil.example"})
        self.assertEqual((status, json.loads(body)["error"]), (403, "Refused."))
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        status, body, res = self.raw("TRACE", "/api/confirm?secret=1")
        self.assertEqual(status, 501)
        self.assertNotIn(b"secret", body)
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))

    def test_duplicate_host_and_absolute_form_are_rejected(self):
        status, body, _ = self.raw("GET", "/api/confirm", host=None, extra=(
            ("Host", f"127.0.0.1:{self.port}"), ("Host", f"127.0.0.1:{self.port}")))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "Unexpected Host header.")
        status, body, _ = self.raw("GET", "http://evil.example/api/confirm")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "Requests from other sites are refused.")
        status, _, _ = self.raw("GET", "/api/confirm", extra=(
            ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")))
        self.assertEqual(status, 403)

    def test_static_traversal_stays_inside_the_web_tree(self):
        probes = (
            "/%2e%2e/jarvis.config.json",
            "/..%2f..%2fetc/passwd",
            "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/..\\..\\etc\\passwd",
            "/%5c..%5cetc%5cpasswd",
            "/etc/passwd",
            "/%00.js",
            "/%ff",
            "/%2fetc/passwd",
        )
        for path in probes:
            with self.subTest(path=path):
                status, body, _ = self.raw("GET", path)
                self.assertIn(status, (403, 404))
                self.assertNotIn(b"root:", body)
                self.assertNotIn(b"Traceback", body)
        # Python 3.14's http.server collapses a leading "//" to one slash before
        # the handler (gh-87389), so this is a missing file, not an open redirect.
        status, body, res = self.raw("GET", "//evil.example/x")
        self.assertEqual(status, 404)
        self.assertIsNone(res.getheader("Location"))
        self.assertNotIn(b"evil.example", body)

    def test_static_symlink_cannot_leave_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web"
            root.mkdir()
            (root / "ok.js").write_text("ok", encoding="utf-8")
            secret = Path(tmp) / "secret.txt"
            secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
            (root / "leak.txt").symlink_to(secret)
            (root / "sub").mkdir()
            (root / "sub" / "up.txt").symlink_to(secret)
            with patch("jarvis.ui.WEB", root):
                self.assertIsNone(_static_target("/leak.txt"))
                self.assertIsNone(_static_target("/sub/up.txt"))
                self.assertEqual(_static_target("/ok.js"), (root / "ok.js").resolve())
                self.assertIsNone(_static_target("/%2e%2e/secret.txt"))
                self.assertIsNone(_static_target("/%2e%2e%5csecret.txt"))
                self.assertIsNone(_static_target("/ok%00.js"))
                self.assertIsNone(_static_target("/..\\secret.txt"))

    def test_unexpected_errors_do_not_echo_the_exception(self):
        self.console.overview.side_effect = ZeroDivisionError("SECRET /tmp/leak")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            status, body, _ = self.raw("GET", "/api/overview")
        text = body.decode()
        self.assertEqual(status, 500)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("ZeroDivisionError", text)
        self.assertIn("JARVIS log", text)
        self.assertIn("Traceback", buf.getvalue())


class _Pointer:
    """Records calls. Never a real backend."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return (0.0, 0.0) if name == "cursor" else None
        return record


class ControlFailsafe(unittest.TestCase):
    def setUp(self):
        saved = dict(control._STATE)
        self.addCleanup(lambda: (control._STATE.clear(), control._STATE.update(saved)))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        control._STATE.update(map=None, left_at=None, paused_until=0.0, times=[],
                              announced=time.time(), typed="", shot_time=0.0)

    def test_stop_file_blocks_spec_that_tries_to_override_it(self):
        pointer = _Pointer()
        path = Path(self.tmp.name) / control.STOP_FILE
        path.write_text(str(time.time() + 3600), encoding="utf-8")
        ok, text = control.act(
            {"action": "type", "text": "nope", "paused_until": 0, "force": True, "skip_check": 1},
            state=self.tmp.name, impl=pointer)
        self.assertFalse(ok)
        self.assertIn("stopped", text.lower())
        self.assertEqual(pointer.calls, [])
        self.assertTrue(path.is_file())

    def test_pause_blocks_a_click_the_spec_marks_forced(self):
        pointer = _Pointer()
        control._STATE["paused_until"] = time.time() + 3600
        ok, text = control.act(
            {"action": "click", "x": 1, "y": 1, "paused_until": 0, "force": True},
            state=self.tmp.name, impl=pointer)
        self.assertFalse(ok)
        self.assertIn("paused", text.lower())
        self.assertEqual(pointer.calls, [])

    def test_model_cannot_resume_or_pass_minutes_to_stop(self):
        pointer = _Pointer()
        ok, text = control.act({"action": "resume"}, state=self.tmp.name, impl=pointer)
        self.assertFalse(ok)
        self.assertIn("action must be one of", text)
        self.assertEqual(pointer.calls, [])
        with patch("jarvis.control.stop", return_value=(True, "stopped")) as stop, \
                patch("jarvis.control.act") as act:
            ok, text = control.run({"action": "stop", "minutes": 0, "force": True, "skip_check": True})
        self.assertEqual((ok, text), (True, "stopped"))
        stop.assert_called_once_with()
        act.assert_not_called()


if __name__ == "__main__":
    unittest.main()
