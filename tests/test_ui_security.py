"""Console trust boundary: Host allow-list (DNS rebinding), Sec-Fetch-Site, X-JARVIS on POST."""

import http.client
import json
import threading
import unittest
from unittest.mock import MagicMock
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from jarvis.ui import Handler


class FakeConsole(MagicMock):
    """do_GET builds its whole route table eagerly, so every attribute must exist; no models, no state."""



class ConsoleSecurity(unittest.TestCase):
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

    def raw(self, method, path, headers=None, body=None, host="default"):
        """host: 'default' sends the real 127.0.0.1:port, None omits Host, else the literal."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host == "default":
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            elif host is not None:
                conn.putheader("Host", host)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            data = json.dumps(body).encode() if body is not None else b""
            if method == "POST":
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            res = conn.getresponse()
            return res.status, res.read()
        finally:
            conn.close()

    # --- Host allow-list -------------------------------------------------
    def test_allowed_hosts_pass_get(self):
        for host in (f"127.0.0.1:{self.port}", "127.0.0.1", "localhost", f"localhost:{self.port}",
                     "LOCALHOST", "LocalHost:8080"):
            with self.subTest(host=host):
                self.assertEqual(self.raw("GET", "/api/confirm", host=host)[0], 200)

    def test_foreign_hosts_rejected_get(self):
        for host in ("evil.example", "evil.example:80", "127.0.0.1.evil.com", "127.0.0.1.evil.com:8080",
                     "localhost.evil.com", "evil.com:127.0.0.1", "0.0.0.0", "192.168.1.5", "",
                     " ", "127.0.0.2", "foo.localhost"):
            with self.subTest(host=host):
                status, body = self.raw("GET", "/api/confirm", host=host)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Unexpected Host header."})

    def test_missing_host_rejected(self):
        status, body = self.raw("GET", "/api/confirm", host=None)
        self.assertEqual(status, 403)
        self.assertIn("Host", json.loads(body)["error"])

    def test_foreign_host_cannot_read_static_or_api(self):
        for path in ("/", "/api/overview", "/api/events", "/api/settings"):
            with self.subTest(path=path):
                self.assertEqual(self.raw("GET", path, host="evil.example")[0], 403)

    def test_userinfo_trick_in_host_rejected(self):
        # 'localhost' as userinfo or suffix must not satisfy the check.
        for host in ("localhost@evil.com", "evil.com#localhost", "localhost.:80x.evil.com"):
            with self.subTest(host=host):
                self.assertEqual(self.raw("GET", "/api/confirm", host=host)[0], 403)

    def test_bracketed_ipv6_host_is_refused_by_current_code(self):
        # Behavior pin: "[::1]:port" splits on ':' to "[" -> "" so IPv6 literals never match,
        # even though "::1" is in the allow-list. Fails closed (safe), just unusable.
        self.assertEqual(self.raw("GET", "/api/confirm", host=f"[::1]:{self.port}")[0], 403)

    # --- Sec-Fetch-Site --------------------------------------------------
    def test_cross_site_fetch_metadata_rejected_even_with_good_host(self):
        for site in ("cross-site", "same-site", "bogus", ""):
            with self.subTest(site=site):
                status, body = self.raw("GET", "/api/confirm", {"Sec-Fetch-Site": site})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Requests from other sites are refused."})

    def test_same_origin_and_none_fetch_metadata_pass(self):
        for site in ("same-origin", "none"):
            with self.subTest(site=site):
                self.assertEqual(self.raw("GET", "/api/confirm", {"Sec-Fetch-Site": site})[0], 200)

    def test_cross_site_post_rejected_even_with_header(self):
        status, _ = self.raw("POST", "/api/confirm", {"X-JARVIS": "1", "Sec-Fetch-Site": "cross-site"},
                             {"id": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(self.console.confirmed, [])

    # --- X-JARVIS on POST ------------------------------------------------
    def test_post_without_xjarvis_rejected(self):
        status, body = self.raw("POST", "/api/confirm", body={"id": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "Refused."})
        self.assertEqual(self.console.confirmed, [], "handler must not run")

    def test_post_with_wrong_xjarvis_value_rejected(self):
        for value in ("0", "true", "", "1 ", "yes"):
            with self.subTest(value=value):
                self.assertEqual(self.raw("POST", "/api/confirm", {"X-JARVIS": value}, {"id": "x"})[0], 403)
        self.assertEqual(self.console.confirmed, [])

    def test_post_with_xjarvis_accepted(self):
        status, body = self.raw("POST", "/api/confirm", {"X-JARVIS": "1"}, {"id": "x"})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.console.confirmed, [{"id": "x"}])

    def test_post_with_xjarvis_but_foreign_or_missing_host_rejected(self):
        for host in ("evil.example", "127.0.0.1.evil.com", None):
            with self.subTest(host=host):
                self.assertEqual(self.raw("POST", "/api/confirm", {"X-JARVIS": "1"}, {"id": "x"}, host=host)[0], 403)
        self.assertEqual(self.console.confirmed, [])

    def test_every_post_route_needs_xjarvis(self):
        for path in ("/api/ask", "/api/settings", "/api/memory", "/api/fs/copy", "/api/send",
                     "/api/kb/upload", "/api/listen", "/api/voice", "/api/credits/manual",
                     "/api/agent/ask", "/api/grokbot", "/api/overlay", "/api/nonexistent"):
            with self.subTest(path=path):
                self.assertEqual(self.raw("POST", path, body={"text": "x"})[0], 403)

    def test_xjarvis_does_not_unlock_get_to_foreign_host(self):
        self.assertEqual(self.raw("GET", "/api/confirm", {"X-JARVIS": "1"}, host="evil.example")[0], 403)

    def test_get_does_not_require_xjarvis(self):
        self.assertEqual(self.raw("GET", "/api/confirm")[0], 200)

    def test_bad_content_length_with_header_is_400_not_crash(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/confirm", skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{self.port}")
            conn.putheader("X-JARVIS", "1")
            conn.putheader("Content-Length", "abc")
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()

    def test_non_object_json_body_rejected(self):
        status, _ = self.raw("POST", "/api/confirm", {"X-JARVIS": "1"}, [1, 2])
        self.assertEqual(status, 400)
        self.assertEqual(self.console.confirmed, [])

    def test_empty_hosts_tuple_rejects_everything(self):
        # A bare Handler (hosts=()) is deny-all, never allow-all.
        h = SimpleNamespace(headers={"Host": "127.0.0.1"}, hosts=Handler.hosts)
        self.assertFalse(Handler._allowed(h))


if __name__ == "__main__":
    unittest.main()
