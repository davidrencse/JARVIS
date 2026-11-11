"""The paired phone's fleet route: read-only status and onboarding guidance, never admin.
Real PhoneServer over loopback with the same fakes as test_phone (no tailscale, no hub I/O)."""

import json
import time
from unittest import mock

from jarvis import cluster_setup, fleet_management
from jarvis.cluster_store import Broker
from tests.mobile import test_phone as base


class PhoneFleet(base.PhoneBase):
    def setUp(self):
        super().setUp()
        self.token, _ = self.pair()
        Broker(self.state).enroll("dell", ["inventory"], invite_ttl=3600)  # a pending invite exists
        import sqlite3  # the fake clock is far from real time: pin the invite to it
        db = sqlite3.connect(self.state / "cluster.sqlite3")
        db.execute("UPDATE nodes SET invite_expires=? WHERE id='dell'", (self.clock() + 3600,))
        db.commit()
        db.close()
        self.calls = []
        for name in ("invite", "revoke"):
            patch = mock.patch.object(cluster_setup, name, side_effect=lambda *a, **k: self.calls.append(a))
            patch.start()
            self.addCleanup(patch.stop)

    def get(self, path="/phone/api/fleet", **kw):
        return self.request("GET", path, cookie=kw.pop("cookie", self.token), **kw)

    def test_paired_phone_reads_a_safe_projection(self):
        status, body, res = self.get()
        self.assertEqual(status, 200, body)
        self.assertIn("no-store", res.getheader("Cache-Control"))
        self.assertEqual(body["audience"], "phone")
        self.assertFalse(body["can_write"])
        self.assertEqual([d["id"] for d in body["devices"]], ["zenbook", "dell"])
        text = json.dumps(body)
        for leaked in ("172.1.0.20", "WinLaptop", "archmain", "invite_expires", "hostname", "address",
                       FALLBACK_IP if (FALLBACK_IP := base.FALLBACK_IP) else "", "jarvis-invite", "token"):
            self.assertNotIn(leaked, text)
        dell = body["devices"][1]
        self.assertEqual(dell["status"], "invite_pending")

    def test_every_write_action_is_blocked_for_the_phone(self):
        _, body, _ = self.get()
        support = {a["id"]: a["support"] for a in body["actions"]}
        for action in ("invite", "revoke", "capabilities", "main_transfer"):
            self.assertEqual(support[action], "blocked", action)
        self.assertNotIn("preview", support)
        for action in body["actions"]:
            self.assertNotIn("path", action)  # nothing the phone could call

    def test_there_is_no_phone_write_route(self):
        for path in ("/phone/api/fleet", "/phone/api/fleet/invite", "/phone/api/fleet/revoke",
                     "/phone/api/fleet/capabilities", "/phone/api/fleet/main-transfer", "/api/fleet/invite",
                     "/phone/api/fleet/preview"):
            for body in ({"node": "oracle1", "request_id": "phone-0001"}, {}):
                status, _, _ = self.request("POST", path, cookie=self.token, body=body)
                self.assertIn(status, (404, 405), path)
        self.assertEqual(self.calls, [])

    def test_unauthenticated_and_wrong_identity_are_refused(self):
        self.assertEqual(self.request("GET", "/phone/api/fleet", cookie=None)[0], 401)
        self.assertEqual(self.request("GET", "/phone/api/fleet", cookie="nonsense")[0], 401)
        self.assertEqual(self.get(login="mallory@example.com")[0], 403)
        self.assertEqual(self.get(ip="192.168.1.5")[0], 401)
        self.assertEqual(self.get(host="evil.example")[0], 403)
        self.assertEqual(self.get(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_path_variants_do_not_reach_the_route(self):
        for path in ("/phone/api/fleet/", "/phone/api/fleet/../me/x", "/phone/api/FLEET", "/phone/api/fleet/vault"):
            self.assertIn(self.get(path)[0], (404, 400), path)

    def test_the_route_makes_no_subprocess_or_network_call(self):
        with mock.patch.object(fleet_management.subprocess, "run", side_effect=AssertionError("no subprocess")), \
             mock.patch.object(cluster_setup, "_sshd_up", side_effect=AssertionError("no probe")):
            status, _, _ = self.get()
        self.assertEqual(status, 200)

    def test_a_failing_projection_is_a_generic_error(self):
        with mock.patch.object(fleet_management, "inventory", side_effect=RuntimeError("/home/jdoe/secret")):
            status, body, _ = self.get()
        self.assertGreaterEqual(status, 500)
        self.assertNotIn("jdoe", json.dumps(body) if not isinstance(body, bytes) else body.decode())
