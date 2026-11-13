"""Phone and continuity-gateway session checks that the live listener was missing.

In-process servers bind an ephemeral loopback port. Nothing here calls the tailscale
CLI, opens a non-loopback socket, or spends a model. Run:
  JARVIS_NO_MODELS=1 python -m unittest tests.security.test_phone_hardening
"""

import http.client
import json
import re
import socket
import subprocess
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path

from jarvis import phone
from jarvis.continuity import ingress
from jarvis.continuity.gateway import ContinuityGateway
from tests.mobile.test_phone import DNS, LOGIN, PHONE_A, PHONE_B, PhoneBase

ROOT = Path(__file__).resolve().parents[2]


class Predicates(unittest.TestCase):
    def test_loopback_peer(self):
        for good in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "[::1]"):
            self.assertTrue(phone.loopback_peer((good, 9)), good)
        for bad in ("100.90.0.2", "10.1.2.3", "0.0.0.0", "localhost", "", None):
            self.assertFalse(phone.loopback_peer((bad, 9) if bad else bad), bad)

    def test_cookie_string_stays_host_prefixed_and_rejects_a_break(self):
        token = "A" * 43
        baked = phone.set_cookie(token)
        self.assertEqual(
            baked, f"__Host-jarvis_phone={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=7776000")
        self.assertNotIn("Domain=", baked)
        self.assertEqual(phone.set_cookie("", 0),
                         "__Host-jarvis_phone=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")
        for bad in ("bad\r\nSet-Cookie: x=1", "short", "has space", "a" * 200):
            with self.assertRaises(ValueError):
                phone.set_cookie(bad)

    def test_global_cap_and_locked_sources_survive_a_flood(self):
        lim = phone.Limiter(lambda: 1000.0)
        for _ in range(phone.BAD_CODES):
            lim.hit("ip:a")
            lim.hit_global()
        self.assertTrue(lim.blocked("ip:a"))
        self.assertFalse(lim.blocked("ip:b"))  # five is the per-source cap, not the global one
        locked = phone.Limiter(lambda: 1000.0)
        for _ in range(phone.BAD_CODES):
            locked.hit("ip:locked")
        for i in range(1100):
            locked.hit(f"ip:{i}")
        self.assertTrue(locked.blocked("ip:locked"))
        full = phone.Limiter(lambda: 1000.0)
        for _ in range(phone.GLOBAL_BAD):
            full.hit_global()
        for i in range(1100):
            full.hit(f"noise:{i}")
        self.assertTrue(full.blocked("ip:fresh"))
        self.assertEqual(len(full._global), phone.GLOBAL_BAD)

    def test_product_code_does_not_enable_funnel(self):
        self.assertNotIn("funnel", phone.SETUP.lower())
        self.assertNotIn("--funnel", phone.SETUP)
        for rel in ("jarvis/phone.py", "jarvis/continuity/gateway.py", "jarvis/continuity/ingress.py",
                    "jarvis/web/phone/sw.js"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("--funnel", text, rel)
            self.assertNotRegex(text, r"tailscale[^\n]{0,40}funnel", rel)

    def test_service_worker_does_not_cache_authenticated_responses(self):
        sw = (ROOT / "jarvis/web/phone/sw.js").read_text(encoding="utf-8")
        self.assertEqual(sw.count("__SHELL_VERSION__"), 2)
        self.assertIn('startsWith("/phone/")', sw)
        self.assertIn("Set-Cookie", sw)
        self.assertIn("no-store", sw)
        self.assertIn("private", sw)
        listed = set(re.findall(r'"(/[^"]*)"', re.search(r"const SHELL = \[(.*?)\];", sw, re.DOTALL).group(1)))
        self.assertEqual(listed, set(phone.SHELL) - {"/sw.js"})


class IngressLimit(unittest.TestCase):
    def _gate(self):
        gate = ingress.MobileIngress.__new__(ingress.MobileIngress)
        gate._clock = lambda: 1000.0
        gate._lock = threading.Lock()
        gate._hits = {}
        gate._global_hits = []
        return gate

    def test_duplicate_header_fails_closed_and_a_dict_still_reads(self):
        msg = Message()
        msg["Tailscale-User-Login"] = "one@example.com"
        msg["Tailscale-User-Login"] = "two@example.com"
        self.assertEqual(ingress._header(msg, "Tailscale-User-Login"), "")
        single = Message()
        single["Tailscale-User-Login"] = "one@example.com"
        self.assertEqual(ingress._header(single, "Tailscale-User-Login"), "one@example.com")
        self.assertEqual(ingress._header({"Tailscale-User-Login": "one@example.com"},
                                         "Tailscale-User-Login"), "one@example.com")

    def test_global_cap_is_once_per_attempt_and_survives_compaction(self):
        gate = self._gate()
        for _ in range(ingress.BAD_CODES):
            gate._hit("node:a")
            gate._hit_global()
        self.assertTrue(gate._blocked_key("node:a"))
        self.assertFalse(gate._blocked_key("node:b"))
        flooded = self._gate()
        for _ in range(ingress.BAD_CODES):
            flooded._hit("node:locked")
        for i in range(1100):
            flooded._hit(f"node:{i}")
        self.assertTrue(flooded._blocked_key("node:locked"))
        full = self._gate()
        for _ in range(ingress.GLOBAL_BAD):
            full._hit_global()
        for i in range(1100):
            full._hit(f"k{i}")
        self.assertTrue(full._blocked_key("node:fresh"))
        self.assertEqual(len(full._global_hits), ingress.GLOBAL_BAD)


class PhoneHttp(PhoneBase):
    def raw(self, method, path, headers, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            if method == "POST" and not any(name.lower() == "content-length" for name, _ in headers):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            res = conn.getresponse()
            payload = res.read()
            try:
                parsed = json.loads(payload) if payload and "json" in (res.getheader("Content-Type") or "") else payload
            except ValueError:
                parsed = payload
            return res.status, parsed, res
        finally:
            conn.close()

    def test_listener_is_loopback_and_a_foreign_peer_is_refused_first(self):
        self.assertEqual(self.server.httpd.socket.getsockname()[0], "127.0.0.1")
        handler = phone.PhoneHandler.__new__(phone.PhoneHandler)
        handler.client_address = ("10.1.2.3", 9)
        handler.path = "/phone/api/me"
        handler.headers = {}
        handler.phone = self.server
        handler.command = "GET"
        with self.assertRaises(phone.Reply) as raised:
            handler._route("GET")
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.body["error"], "Refused.")

    def test_origin_when_present_must_be_this_host(self):
        token, _ = self.pair()
        self.assertEqual(self.request("GET", "/phone/api/me", cookie=token)[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Origin": f"https://{DNS}"})[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        for origin in ("https://evil.example", "null", f"https://{DNS}/", "http://0.0.0.0:7787"):
            status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi"}, cookie=token,
                                           headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
            self.assertNotEqual(body.get("code"), "csrf", origin)
        self.assertEqual(self.console.submitted, [])
        status, body, _ = self.raw("GET", "/", [
            ("Host", DNS), ("Origin", f"https://{DNS}"), ("Origin", "https://evil.example")])
        self.assertEqual(status, 403, body)

    def test_duplicate_identity_headers_do_not_authenticate(self):
        token, _ = self.pair()
        common = [("Host", DNS), ("Cookie", f"{phone.COOKIE}={token}"),
                  ("X-CSRF-Token", self._csrf[token])]
        status, body, _ = self.raw("GET", "/phone/api/me", common + [
            ("X-Forwarded-For", PHONE_A), ("X-Forwarded-For", PHONE_B),
            ("Tailscale-User-Login", LOGIN)])
        self.assertNotEqual(status, 200)
        self.assertEqual(status, 401, body)
        status, body, _ = self.raw("POST", "/phone/api/ask", common + [
            ("X-Forwarded-For", PHONE_A), ("Tailscale-User-Login", LOGIN),
            ("Tailscale-User-Login", "mallory@example.com"),
            ("X-JARVIS-Phone", "1"), ("Content-Type", "application/json")],
            body=b'{"text": "hi"}')
        self.assertEqual(status, 403, body)
        self.assertNotEqual(body.get("code"), "csrf")
        self.assertEqual(self.console.submitted, [])

    def test_new_pair_requires_csrf_on_post_and_a_legacy_row_does_not(self):
        token, _ = self.pair()
        stored = (self.state / phone.STORE).read_text(encoding="utf-8")
        self.assertNotIn(self._csrf[token], stored)
        self.assertIn("csrf_sha256", stored)
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi"}, cookie=token, csrf=False)
        self.assertEqual((status, body.get("code")), (403, "csrf"))
        self.assertEqual(self.console.submitted, [])
        status, body, _ = self.request("GET", "/phone/api/me", cookie=token, csrf=False)
        self.assertEqual(status, 200, body)  # the frame and the reads do not send the token
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi"}, cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.console.submitted), 1)

        def drop(data):
            data["phones"][0].pop("csrf_sha256", None)
        self.server.store.update(drop)
        before = len(self.console.submitted)
        status, body, _ = self.request("POST", "/phone/api/ask", body={"text": "hi again"},
                                       cookie=token, csrf=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.console.submitted), before + 1)

    def test_api_json_is_unframed_and_unstored(self):
        token, _ = self.pair()
        status, _, res = self.request("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", res.getheader("Cache-Control"))
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        _, _, script = self.request("GET", "/phone.js")
        self.assertIsNone(script.getheader("Content-Security-Policy"))


class GatewayHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        path = Path(cls.keys.name) / "owner"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-phone-hardening", "-f", str(path)],
            check=True, capture_output=True, timeout=10)
        token = path.with_suffix(".pub").read_text(encoding="ascii").split()
        cls.pubkey = f"{token[0]} {token[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gw = ContinuityGateway.open(
            Path(self.tmp.name) / "authority.sqlite", "home", "oracle-auth", [self.pubkey])
        self.addCleanup(self.gw.close)
        import socketserver
        orig = socketserver.BaseServer.serve_forever

        def fast(self, poll_interval=0.5):
            return orig(self, min(poll_interval, 0.01))

        with unittest.mock.patch.object(socketserver.BaseServer, "serve_forever", fast):
            self.origin = self.gw.serve()
        self.port = int(self.origin.rsplit(":", 1)[1])

    def _send(self, raw):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall(raw)
            return sock.recv(2048)

    def test_replies_carry_the_security_headers_and_no_cors(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/v1/authority/status")
        res = conn.getresponse()
        try:
            self.assertEqual(res.status, 405)
            self.assertEqual(res.getheader("Content-Type"), "application/json")
            self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        finally:
            res.read()
            conn.close()

    def test_host_peer_and_origin(self):
        class Probe:
            pass
        probe = Probe()
        probe.client_address = ("10.1.2.3", 9)
        probe.headers = Message()
        self.assertFalse(self.gw._peer_ok(probe))
        probe.client_address = ("127.0.0.1", 9)
        self.assertTrue(self.gw._peer_ok(probe))
        probe.client_address = ("::ffff:127.0.0.1", 9)
        self.assertTrue(self.gw._peer_ok(probe))
        bare = Message()
        bare["Host"] = "127.0.0.1"
        probe.headers = bare
        self.assertTrue(self.gw._host_ok(probe))
        evil = Message()
        evil["Host"] = "evil.example"
        probe.headers = evil
        self.assertFalse(self.gw._host_ok(probe))
        for good in ("oracle-vm.tailnet-example.ts.net", "oracle-vm.tailnet-example.ts.net:443"):
            served = Message()  # tailscale serve forwards the caller's Host; peer is loopback
            served["Host"] = good
            probe.headers = served
            with self.subTest(host=good):
                self.assertTrue(self.gw._host_ok(probe))
        for bad in ("evil.ts.net.evil.example", ".ts.net", "x.ts.net:8080", "user@x.ts.net"):
            wrong = Message()
            wrong["Host"] = bad
            probe.headers = wrong
            with self.subTest(host=bad):
                self.assertFalse(self.gw._host_ok(probe))
        twice = Message()
        twice["Host"] = "127.0.0.1"
        twice["Host"] = "evil.example"
        probe.headers = twice
        self.assertFalse(self.gw._host_ok(probe))
        self.assertTrue(self.gw._origin_ok(probe))  # no Origin header
        foreign = Message()
        foreign["Origin"] = "https://evil.example"
        probe.headers = foreign
        self.assertFalse(self.gw._origin_ok(probe))
        allowed = Message()
        allowed["Origin"] = f"http://127.0.0.1:{self.port}"
        probe.headers = allowed
        self.assertTrue(self.gw._origin_ok(probe))

        refused = self._send(
            b"POST /v1/authority/challenge HTTP/1.1\r\nHost: evil.example\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        self.assertIn(b"403", refused.split(b"\r\n", 1)[0])
        oversized = self._send(
            b"POST /v1/authority/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 999999\r\nConnection: close\r\n\r\n")
        self.assertIn(b"400", oversized.split(b"\r\n", 1)[0])
        self.assertNotIn(b"sqlite", oversized)
        self.assertNotIn(b"Access-Control-Allow-Origin", oversized)


if __name__ == "__main__":
    unittest.main()
