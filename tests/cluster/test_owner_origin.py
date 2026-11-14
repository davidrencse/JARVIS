"""The owner credential only reaches this PC's own hub on the caller's numeric-loopback origin.

A verified continuity instruction (effective_config) may name a successor route,
often a public HTTPS proxy. It decides who the main is; it must never decide where
the owner bearer token is sent. Fake loopback HTTP servers stand in for hubs; no
real credentials, services or vault are touched.
"""
import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jarvis import cluster
from jarvis.cluster_store import OWNER_TOKEN
from jarvis.continuity import hub_control

OWNER = "owner-token-" + "x" * 32
NODE = "node-token-" + "y" * 32


class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size) if size else b""
        self.server.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "main": self.headers.get("X-JARVIS-Main"),
                                 "body": json.loads(body) if body else None})
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps({"ok": True, "result": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer


def _server(case, redirect=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.seen, server.redirect = [], redirect
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    return server


def _config(main, url, local="dell"):
    return {"cluster": {"enabled": True, "local_device": local, "main_device": main,
                        "main_url": url},
            "devices": {"zenbook": {}, "dell": {}}}


def _rewrite(main, url):
    """Stand-in for a verified continuity instruction naming a successor route."""
    def effective(config, state):
        result = copy.deepcopy(config)
        result["cluster"].update(main_device=main, main_url=url)
        return result
    return patch("jarvis.continuity.repoint.effective_config", side_effect=effective)


class OwnerOriginTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.state.mkdir()
        (self.state / OWNER_TOKEN).write_text(OWNER, encoding="ascii")
        self.hub = _server(self)
        self.local = f"http://127.0.0.1:{self.hub.server_address[1]}"
        # Reachable through a DNS alias: a stand-in for a successor route that a
        # proxy (or another host) serves. It must never see the owner token.
        self.route = _server(self)
        self.route_url = f"http://localhost:{self.route.server_address[1]}"

    def assert_never_reached_route(self):
        self.assertEqual(self.route.seen, [])

    def test_successor_route_cannot_redirect_the_owner_token(self):
        with _rewrite("dell", self.route_url):
            client = cluster.Client(_config("dell", self.local), self.state, owner=True)
            self.assertEqual(client.url, self.local)
            self.assertEqual(client.main, "dell")  # role still comes from the verified instruction
            client.request("/v1/status")
        self.assertEqual(self.hub.seen[0]["auth"], f"Bearer {OWNER}")
        self.assertEqual(self.hub.seen[0]["main"], "dell")
        self.assert_never_reached_route()

    def test_public_https_route_is_never_a_fallback(self):
        with _rewrite("dell", "https://dell.example.ts.net:8443"):
            client = cluster.Client(_config("dell", self.local), self.state, owner=True)
        self.assertEqual(client.url, self.local)

    def test_owner_refuses_a_remote_or_dns_caller_origin(self):
        for origin in ("https://dell.example.ts.net:8443", self.route_url, "http://localhost:7781"):
            with self.subTest(origin=origin), _rewrite("dell", origin):
                with self.assertRaisesRegex(ValueError, "numeric loopback"):
                    cluster.Client(_config("dell", origin), self.state, owner=True)
        self.assert_never_reached_route()

    def test_caller_origin_for_another_main_is_refused(self):
        # A raw node config's loopback main_url may be an SSH tunnel to the old main.
        with _rewrite("dell", self.route_url):
            with self.assertRaisesRegex(ValueError, "numeric loopback"):
                cluster.Client(_config("zenbook", self.local), self.state, owner=True)
        self.assertEqual(self.hub.seen, [])
        self.assert_never_reached_route()

    def test_owner_credential_stays_off_a_node(self):
        with _rewrite("zenbook", self.local):
            with self.assertRaisesRegex(ValueError, "Only the configured main PC"):
                cluster.Client(_config("dell", self.local), self.state, owner=True)

    def test_ipv6_loopback_is_accepted(self):
        client = cluster.Client(_config("dell", "http://[::1]:7781"), self.state, owner=True)
        self.assertEqual(client.url, "http://[::1]:7781")

    def test_redirect_never_carries_the_owner_token(self):
        bouncer = _server(self, redirect=self.route_url)
        url = f"http://127.0.0.1:{bouncer.server_address[1]}"
        client = cluster.Client(_config("dell", url), self.state, owner=True)
        with self.assertRaises(Exception):
            client.request("/v1/status")
        self.assertEqual(len(bouncer.seen), 1)
        self.assert_never_reached_route()

    def test_open_rechecks_a_changed_url(self):
        client = cluster.Client(_config("dell", self.local), self.state, owner=True)
        client.url = self.route_url
        with self.assertRaisesRegex(ValueError, "numeric loopback"):
            client.request("/v1/status")
        self.assert_never_reached_route()

    def test_node_client_still_follows_the_verified_route(self):
        (self.state / "cluster-node.json").write_text(json.dumps(
            {"node_id": "dell", "main_device": "zenbook", "token": NODE}), encoding="utf-8")
        with _rewrite("zenbook", self.route_url):
            client = cluster.Client(_config("dell", self.local), self.state)
            client.request("/v1/status")
        self.assertEqual(client.url, self.route_url)
        self.assertEqual(self.route.seen[0]["auth"], f"Bearer {NODE}")
        self.assertEqual(self.hub.seen, [])

    def test_dispatch_uses_the_pinned_loopback_hub(self):
        with _rewrite("dell", self.route_url), patch.dict(cluster._HUBS, clear=True):
            cluster.dispatch(_config("dell", self.local), self.state, "zenbook", "transfer",
                             {"op": "probe"}, timeout=5)
        self.assertEqual(self.hub.seen[0]["path"], "/v1/action")
        self.assertEqual(self.hub.seen[0]["body"]["node"], "zenbook")
        self.assertEqual(self.hub.seen[0]["auth"], f"Bearer {OWNER}")
        self.assert_never_reached_route()

    def test_request_main_and_recover_post_to_the_pinned_hub(self):
        with _rewrite("dell", self.route_url):
            hub_control.request_main(_config("dell", self.local), self.state, "zenbook")
            hub_control.request_recover(_config("dell", self.local), self.state, "dell", "req-1")
        self.assertEqual([(row["path"], row["body"]) for row in self.hub.seen], [
            ("/v1/continuity/main", {"device": "zenbook"}),
            ("/v1/continuity/recover", {"device": "dell", "request_id": "req-1"})])
        self.assertTrue(all(row["auth"] == f"Bearer {OWNER}" for row in self.hub.seen))
        self.assert_never_reached_route()

    def test_control_checks_the_client_url_after_construction(self):
        class Moved:
            owner, url = True, "https://dell.example.ts.net:8443"

            def request(self, *args, **kwargs):
                raise AssertionError("posted")
        with patch("jarvis.cluster.Client", return_value=Moved()):
            with self.assertRaisesRegex(ValueError, "MAIN control"):
                hub_control.request_main(_config("dell", self.local), self.state, "zenbook")
            with self.assertRaisesRegex(ValueError, "Recovery control"):
                hub_control.request_recover(_config("dell", self.local), self.state, "dell", "req-1")

    def test_switch_main_refuses_a_moved_origin_before_any_receipt(self):
        with _rewrite("dell", self.route_url):
            with self.assertRaisesRegex(ValueError, "numeric loopback"):
                hub_control.switch_main(_config("zenbook", self.local), self.state, "zenbook")
        self.assertFalse((self.state / "continuity-main-receipt.json").exists())
        self.assertEqual(self.hub.seen, [])
        self.assert_never_reached_route()

    def test_switch_main_posts_once_to_the_pinned_hub(self):
        with _rewrite("dell", self.route_url):
            hub_control.switch_main(_config("dell", self.local), self.state, "zenbook")
        self.assertEqual([row["path"] for row in self.hub.seen], ["/v1/continuity/main"])
        self.assertTrue((self.state / "continuity-main-receipt.json").exists())
        self.assert_never_reached_route()


if __name__ == "__main__":
    unittest.main()
