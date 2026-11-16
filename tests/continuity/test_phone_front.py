"""The phone front relays /phone to whichever PC holds MAIN right now. Upstreams are fake
loopback servers; nothing leaves 127.0.0.1."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis.continuity.gateway import (
    GatewayConfig, GatewayError, load_gateway_config, serve_gateway,
)
from jarvis.continuity.phone_front import PhoneFront
from tests.continuity.test_gateway import _keys


class Upstream:
    def __init__(self, name):
        self.name = name
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.seen.append({"method": self.command, "path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": body})
                if self.path.startswith("/phone/cookies"):
                    self.send_response(200)
                    self.send_header("Set-Cookie", "a=1; Path=/; HttpOnly")
                    self.send_header("Set-Cookie", "b=2; Path=/; Secure")
                    self.send_header("X-Secret-Internal", "no")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                if self.path.startswith("/phone/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/phone/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/phone/denied"):
                    raw = b'{"error":"no"}'
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path.startswith("/phone/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()  # no length: close-delimited
                    for part in (b"one-", b"two-", b"three"):
                        self.wfile.write(part)
                        self.wfile.flush()
                        time.sleep(0.02)
                    self.close_connection = True
                    return
                if self.path.startswith("/phone/frame"):
                    self.send_response(200)
                    self.send_header("X-Frame-Seq", "321")
                    self.send_header("X-Frame-Width", "1920")
                    self.send_header("X-Frame-Height", "1080")
                    self.send_header("Content-Length", "3")
                    self.end_headers()
                    self.wfile.write(b"jpg")
                    return
                if self.path.startswith("/phone/idle"):
                    self.send_response(204)
                    self.send_header("X-Node-Ago", "0.3")
                    self.end_headers()
                    return
                raw = json.dumps({"from": outer.name, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            do_GET = do_POST = do_HEAD = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def get_free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FrontCase(unittest.TestCase):
    def setUp(self):
        self.arch, self.dell = Upstream("arch"), Upstream("dell")
        self.addCleanup(self.arch.close)
        self.addCleanup(self.dell.close)
        self.status = {"main": "arch", "lease_valid": True, "main_ready": True}
        self.calls = 0

        def status_fn():
            self.calls += 1
            if isinstance(self.status, Exception):
                raise self.status
            return dict(self.status)

        self.front = PhoneFront(status_fn, {"arch": self.arch.origin, "dell": self.dell.origin},
                                bind_port=0, timeout=5)
        origin = self.front.start()
        self.addCleanup(self.front.close)
        self.port = int(origin.rsplit(":", 1)[1])

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response, response.read()
        finally:
            conn.close()


class RoutingTests(FrontCase):
    def test_forwards_to_current_main(self):
        status, response, body = self.request("GET", "/phone/state?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"from": "arch", "path": "/phone/state?x=1"})
        self.assertEqual(response.getheader("X-Jarvis-Main"), "arch")
        self.assertEqual(len(self.arch.seen), 1)
        self.assertEqual(self.dell.seen, [])
        seen = self.arch.seen[0]["headers"]
        self.assertEqual(seen["x-jarvis-front"], "1")
        self.assertEqual(seen["x-jarvis-front-main"], "arch")

    def test_switches_upstream_when_main_changes_between_requests(self):
        self.request("GET", "/phone/a")
        self.status = {"main": "dell", "lease_valid": True, "main_ready": True}
        status, response, body = self.request("GET", "/phone/b")
        self.assertEqual(json.loads(body)["from"], "dell")
        self.assertEqual(response.getheader("X-Jarvis-Main"), "dell")
        self.assertEqual(len(self.arch.seen), 1)
        self.assertEqual(len(self.dell.seen), 1)
        self.assertGreaterEqual(self.calls, 2)

    def test_ready_successor_serves_shell_and_api_with_original_main_off(self):
        self.request("GET", "/")
        self.arch.close()
        # The original server is physically closed for all subsequent requests,
        # not merely marked offline in the role snapshot.
        self.status = {"main": "dell", "lease_valid": True, "main_ready": True}
        credentials = {
            "Cookie": "__Host-jarvis_phone.arch=A; __Host-jarvis_phone.dell=D",
            "X-Jarvis-Csrf-arch": "csrf-arch", "X-Jarvis-Csrf-dell": "csrf-dell",
        }
        for path in ("/", "/phone.js", "/sw.js", "/manifest.webmanifest", "/phone/api/me"):
            status, response, body = self.request("GET", path, headers=credentials)
            self.assertEqual((status, json.loads(body)["from"]), (200, "dell"), path)
            self.assertEqual(response.getheader("X-Jarvis-Main"), "dell", path)
        status, _, _ = self.request("POST", "/phone/api/ask", body=b'{"q":"status"}',
                                    headers=credentials)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.arch.seen), 1)
        for seen in self.dell.seen:
            self.assertEqual(seen["headers"]["cookie"], "__Host-jarvis_phone=D")
            self.assertEqual(seen["headers"]["x-csrf-token"], "csrf-dell")

    def test_phone_bare_and_slash_forward(self):
        self.assertEqual(self.request("GET", "/phone")[0], 200)
        self.assertEqual(self.request("GET", "/phone/")[0], 200)

    def test_app_shell_at_root_forwards(self):
        # The PC serves the PWA shell at its root; through the front it must load too.
        for path in ("/", "/?v=1", "/phone.js", "/sw.js", "/manifest.webmanifest",
                     "/icons/icon-192.png", "/fonts/geist.woff2", "/icons.svg"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(json.loads(body)["path"], path)

    def test_other_paths_are_404_json_and_never_reach_upstream(self):
        for path in ("/api/state", "/phonex", "/phone/../secret", "/phone/%2e%2e/x", "/other/phone/",
                     "/icons/../phone.json", "/fonts/", "/index.html"):
            status, response, body = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body)["code"], "not_found")
        self.assertEqual(self.arch.seen, [])

    def test_only_get_head_post(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status, response, _ = self.request(method, "/phone/x")
            self.assertEqual(status, 405, method)
        self.assertEqual(self.arch.seen, [])
        self.assertEqual(self.request("HEAD", "/phone/x")[0], 200)
        status, _, _ = self.request("POST", "/phone/x", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual([s["method"] for s in self.arch.seen], ["HEAD", "POST"])


class UnavailableTests(FrontCase):
    def assert_no_main(self):
        status, response, body = self.request("GET", "/phone/x")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "No ready MAIN. Waiting to reconnect.",
                                            "code": "no_main", "wait": 2})
        self.assertEqual(response.getheader("Retry-After"), "2")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(self.arch.seen + self.dell.seen, [])

    def test_lease_invalid(self):
        self.status = {"main": "arch", "lease_valid": False}
        self.assert_no_main()

    def test_lease_valid_not_true(self):
        self.status = {"main": "arch", "lease_valid": "yes"}
        self.assert_no_main()

    def test_granted_successor_is_not_routed_until_ready(self):
        for ready in (False, None, "yes", 1):
            self.status = {"main": "dell", "lease_valid": True, "main_ready": ready}
            with self.subTest(main_ready=ready):
                self.assert_no_main()
        self.status = {"main": "dell", "lease_valid": True}
        self.assert_no_main()
        self.status["main_ready"] = True
        status, _, body = self.request("GET", "/phone/x")
        self.assertEqual((status, json.loads(body)["from"]), (200, "dell"))

    def test_main_unknown(self):
        self.status = {"main": "oracle", "lease_valid": True}
        self.assert_no_main()
        self.status = {"main": None, "lease_valid": True}
        self.assert_no_main()

    def test_status_raises(self):
        self.status = RuntimeError("db locked")
        self.assert_no_main()

    def test_upstream_down_is_502_no_relay(self):
        self.arch.close()
        status, response, body = self.request("GET", "/phone/x")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {
            "error": "The MAIN connection is unavailable. Waiting to reconnect.",
            "code": "no_relay", "wait": 2})
        self.assertEqual(response.getheader("Retry-After"), "2")
        self.arch.httpd = ThreadingHTTPServer.__new__(ThreadingHTTPServer)  # closed twice in cleanup
        self.arch.httpd.shutdown = lambda: None
        self.arch.httpd.server_close = lambda: None


class ForwardingTests(FrontCase):
    def test_request_headers_are_filtered(self):
        self.request("GET", "/phone/x", headers={
            "Cookie": "sid=abc", "Accept": "text/html", "Origin": "https://front.ts.net:8443",
            "Referer": "https://front.ts.net:8443/phone/", "User-Agent": "iPhone",
            "X-CSRF-Token": "tok", "X-Jarvis-Thing": "t", "If-None-Match": '"e"',
            "If-Modified-Since": "Wed, 01 Jan 2025 00:00:00 GMT",
            "Authorization": "Bearer nope", "X-Forwarded-For": "6.6.6.6", "X-Evil": "1",
            "X-Jarvis-Front": "spoof", "X-Jarvis-Front-Main": "spoof", "Accept-Encoding": "gzip",
            "Host": "front.example.ts.net:8443"})
        seen = self.arch.seen[0]["headers"]
        for name in ("cookie", "accept", "origin", "referer", "user-agent", "x-csrf-token",
                     "x-jarvis-thing", "if-none-match", "if-modified-since"):
            self.assertIn(name, seen, name)
        for name in ("authorization", "x-forwarded-for", "x-evil"):
            self.assertNotIn(name, seen, name)
        self.assertNotEqual(seen.get("accept-encoding"), "gzip")
        # Host is the upstream's own name: its `tailscale serve` routes by it.
        self.assertNotEqual(seen["host"], "front.example.ts.net:8443")
        self.assertEqual(seen["x-forwarded-host"], "front.example.ts.net:8443")
        self.assertEqual(seen["x-jarvis-front"], "1")
        self.assertEqual(seen["x-jarvis-front-main"], "arch")

    def test_phone_identity_comes_only_from_this_servers_serve(self):
        self.request("GET", "/phone/x", headers={
            "X-Forwarded-For": "100.64.0.14", "Tailscale-User-Login": "owner@example.com",
            "X-Jarvis-Front-For": "100.1.1.1", "X-Jarvis-Front-Login": "spoof@example.com"})
        seen = self.arch.seen[0]["headers"]
        self.assertEqual(seen["x-jarvis-front-for"], "100.64.0.14")
        self.assertEqual(seen["x-jarvis-front-login"], "owner@example.com")

    def test_client_front_identity_never_passes_alone(self):
        self.request("GET", "/phone/x", headers={
            "X-Jarvis-Front-For": "100.1.1.1", "X-Jarvis-Front-Login": "spoof@example.com"})
        seen = self.arch.seen[0]["headers"]
        self.assertNotIn("x-jarvis-front-for", seen)
        self.assertNotIn("x-jarvis-front-login", seen)

    def test_post_body_and_query_forwarded(self):
        self.request("POST", "/phone/api/ask?a=b", body=b'{"q":1}',
                     headers={"Content-Type": "application/json"})
        seen = self.arch.seen[0]
        self.assertEqual(seen["path"], "/phone/api/ask?a=b")
        self.assertEqual(seen["body"], b'{"q":1}')
        self.assertEqual(seen["headers"]["content-type"], "application/json")

    def test_multiple_set_cookie_pass_through_and_other_headers_dropped(self):
        status, response, body = self.request("GET", "/phone/cookies")
        self.assertEqual(body, b"ok")
        self.assertEqual(response.msg.get_all("Set-Cookie"),
                         ["a=1; Path=/; HttpOnly", "b=2; Path=/; Secure"])
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIsNone(response.getheader("X-Secret-Internal"))
        self.assertEqual(response.getheader("Content-Length"), "2")

    def test_upstream_errors_and_redirects_pass_through(self):
        status, response, body = self.request("GET", "/phone/denied")
        self.assertEqual((status, body), (403, b'{"error":"no"}'))
        status, response, _ = self.request("GET", "/phone/redirect")
        self.assertEqual(status, 302)
        self.assertEqual(response.getheader("Location"), "/phone/elsewhere")

    def test_streamed_body_without_length(self):
        status, response, body = self.request("GET", "/phone/stream")
        self.assertEqual((status, body), (200, b"one-two-three"))

    def test_frame_sequence_dimensions_and_idle_liveness_are_preserved(self):
        status, response, body = self.request("GET", "/phone/frame")
        self.assertEqual((status, body), (200, b"jpg"))
        for name, value in (("X-Frame-Seq", "321"), ("X-Frame-Width", "1920"),
                            ("X-Frame-Height", "1080")):
            self.assertEqual(response.getheader(name), value)
        status, response, body = self.request("GET", "/phone/idle")
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(response.getheader("X-Node-Ago"), "0.3")

    def test_big_body_is_413_and_never_forwarded(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", "/phone/x")
            conn.putheader("Content-Length", str((1 << 20) + 1))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
        finally:
            conn.close()
        self.assertEqual(self.arch.seen, [])

    def test_body_at_cap_is_forwarded(self):
        status, _, _ = self.request("POST", "/phone/x", body=b"x" * (1 << 20))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.arch.seen[0]["body"]), 1 << 20)


class ConfigTests(unittest.TestCase):
    def test_upstream_origin_validation(self):
        ok = {"a": "https://winlaptop.tailnet-example.ts.net", "b": "https://x.ts.net:8443",
              "c": "http://127.0.0.1:9000", "d": "http://localhost:9000"}
        PhoneFront(dict, ok, bind_port=0)
        for bad in ("http://x.ts.net", "https://example.com", "https://x.ts.net/", "https://x.ts.net/p",
                    "https://u@x.ts.net", "http://127.0.0.1", "http://10.0.0.1:80", "ftp://x.ts.net",
                    "https://x.ts.net.evil.com", "https://x.ts.net:99999", "", None, "https://.ts.net"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                PhoneFront(dict, {"a": bad}, bind_port=0)
        with self.assertRaises(ValueError):
            PhoneFront(dict, {"a": "http://127.0.0.1:9"}, bind_host="0.0.0.0")

    def test_close_is_idempotent_and_frees_port(self):
        front = PhoneFront(dict, {"a": "http://127.0.0.1:9"}, bind_port=0)
        origin = front.start()
        front.close()
        front.close()
        host, port = origin[len("http://"):].split(":")
        with self.assertRaises(OSError):
            http.client.HTTPConnection(host, int(port), timeout=1).request("GET", "/phone/")


class GatewayWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, **extra):
        data = {"state_path": str(self.dir / "authority.sqlite"), "fleet_id": "home",
                "authority_id": "oracle-auth", "owner_public_keys": [_keys()["owner"][0]], **extra}
        path = self.dir / "gateway.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_config_accepts_phone_front(self):
        config = load_gateway_config(self.write(phone_front={
            "bind_port": 8767,
            "upstreams": {"zenbook": "https://archmain.tailnet-example.ts.net",
                          "dell": "https://winlaptop.tailnet-example.ts.net"}}))
        self.assertEqual(config.phone_front["bind_port"], 8767)
        self.assertEqual(set(config.phone_front["upstreams"]), {"zenbook", "dell"})
        self.assertIsNone(load_gateway_config(self.write()).phone_front)

    def test_config_rejects_bad_phone_front(self):
        good = {"zenbook": "https://archmain.tailnet-example.ts.net"}
        for bad in (
            {"bind_port": 80, "upstreams": good}, {"bind_port": True, "upstreams": good},
            {"bind_port": "8767", "upstreams": good}, {"bind_port": 70000, "upstreams": good},
            {"bind_port": 8767}, {"upstreams": good},
            {"bind_port": 8767, "upstreams": {}}, {"bind_port": 8767, "upstreams": []},
            {"bind_port": 8767, "upstreams": {"zenbook": "https://evil.example.com"}},
            {"bind_port": 8767, "upstreams": {"bad id": "https://a.ts.net"}},
            {"bind_port": 8767, "upstreams": good, "bind_host": "0.0.0.0"},
            "x", [], 5,
        ):
            with self.assertRaises(GatewayError, msg=repr(bad)) as ctx:
                load_gateway_config(self.write(phone_front=bad))
            self.assertEqual(ctx.exception.code, "bad_config")

    def test_serve_gateway_starts_and_closes_front(self):
        port = get_free_port()
        config = load_gateway_config(self.write(phone_front={
            "bind_port": port, "upstreams": {"zenbook": "http://127.0.0.1:9"}}))
        gateway, _ = serve_gateway(config)
        try:
            front = gateway.phone_front
            self.assertIsNotNone(front)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/phone/")
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            # fresh authority has no MAIN lease: front answers no_main, it does not guess
            self.assertEqual((response.status, body["code"]), (503, "no_main"))
        finally:
            gateway.close()
        with self.assertRaises(OSError):
            http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/phone/")

    def test_serve_gateway_without_front_has_none(self):
        gateway, _ = serve_gateway(load_gateway_config(self.write()))
        try:
            self.assertIsNone(getattr(gateway, "phone_front", None))
        finally:
            gateway.close()

    def test_gatewayconfig_default_is_none(self):
        self.assertIsNone(load_gateway_config(self.write()).phone_front)
        self.assertIn("phone_front", GatewayConfig.__init__.__code__.co_varnames)


class PerMainSessionTests(FrontCase):
    """One origin, one pairing store per PC: each PC gets its own cookie and CSRF token."""

    def test_only_this_mains_cookie_and_csrf_reach_it(self):
        self.request("GET", "/phone/x", headers={
            "Cookie": "__Host-jarvis_phone.arch=A; __Host-jarvis_phone.dell=D; "
                      "__Host-jarvis_phone=OLD; theme=dark",
            "X-Jarvis-Csrf-arch": "csrf-arch", "X-Jarvis-Csrf-dell": "csrf-dell",
            "X-CSRF-Token": "stale"})
        seen = self.arch.seen[0]["headers"]
        self.assertEqual(seen["cookie"], "__Host-jarvis_phone=A; theme=dark")
        self.assertEqual(seen["x-csrf-token"], "csrf-arch")
        self.assertNotIn("x-jarvis-csrf-dell", seen)
        self.assertNotIn("x-jarvis-csrf-arch", seen)
        self.status = {"main": "dell", "lease_valid": True, "main_ready": True}
        self.request("GET", "/phone/x", headers={
            "Cookie": "__Host-jarvis_phone.arch=A; __Host-jarvis_phone.dell=D",
            "X-Jarvis-Csrf-arch": "csrf-arch", "X-Jarvis-Csrf-dell": "csrf-dell"})
        seen = self.dell.seen[0]["headers"]
        self.assertEqual(seen["cookie"], "__Host-jarvis_phone=D")
        self.assertEqual(seen["x-csrf-token"], "csrf-dell")

    def test_no_token_for_this_main_sends_none(self):
        self.request("GET", "/phone/x", headers={
            "Cookie": "__Host-jarvis_phone.dell=D", "X-Jarvis-Csrf-dell": "csrf-dell",
            "X-CSRF-Token": "stale"})
        seen = self.arch.seen[0]["headers"]
        self.assertNotIn("cookie", seen)
        self.assertNotIn("x-csrf-token", seen)

    def test_plain_csrf_passes_when_app_has_no_per_main_tokens(self):
        self.request("GET", "/phone/x", headers={"X-CSRF-Token": "plain"})
        self.assertEqual(self.arch.seen[0]["headers"]["x-csrf-token"], "plain")

    def test_set_cookie_is_renamed_for_the_answering_main(self):
        from jarvis.continuity.phone_front import _rename_cookie
        self.assertEqual(
            _rename_cookie("__Host-jarvis_phone=T; Path=/; Secure; HttpOnly; SameSite=Strict", "dell"),
            "__Host-jarvis_phone.dell=T; Path=/; Secure; HttpOnly; SameSite=Strict")
        self.assertEqual(_rename_cookie("a=1; Path=/", "dell"), "a=1; Path=/")


if __name__ == "__main__":
    unittest.main()
