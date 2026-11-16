"""Real HTTP Hub -> PhoneHandler, with isolated authority and fake Tailscale/console.

No system Serve changes, real input, camera, model, or external network.
"""
import copy
import http.client
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest import mock

from jarvis import phone, phone_hub
from jarvis.cluster import Hub
from jarvis.continuity.authority import LEASE_SECONDS, SAFETY_MARGIN
from jarvis.continuity.integration import IsolatedFleet
from jarvis.continuity.phone_front import PhoneFront
from tests.continuity._fleet import Clock
from tests.mobile.test_phone import CONFIG, DNS, LOGIN, PHONE_A, FakeConsole, FakeTailscale
from tests.mobile.test_phone_front_origin import FRONT, ORACLE_IP, _whois

PUBLIC = f"https://{DNS}:8443"


class PhoneHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.fleet = IsolatedFleet(self.root / "fleet", self.clock)
        self.addCleanup(self.fleet.close)
        self.fleet.cutover()
        _, lease = self.fleet.promote("zenbook")
        self.node = self.fleet.nodes["zenbook"]
        self.node.runtime.start()
        self.node.runtime.accept_lease(lease)
        self.assertEqual(self.node.runtime.renew_once()["code"], "ok")
        self.config = copy.deepcopy(CONFIG)
        self.config["cluster"].update(main_url=PUBLIC, auto_update=False, auto_release=False)
        self.config["phone"] = {"public_origin": PUBLIC, "front_origin": FRONT}
        self.state = self.root / ".jarvis"
        self.hub = Hub(self.config, self.root, self.state).start(port=0)
        self.addCleanup(self.hub.close)
        self.hub.continuity_endpoint = PUBLIC
        self.console = FakeConsole()
        self.hub.console = self.console
        ts = FakeTailscale()
        ts.whois[ORACLE_IP] = _whois("oracle-vm.tailnet-example.ts.net", "nORACLE", "linux", LOGIN)
        self.backend = phone.PhoneServer(self.config, self.state, console=self.console,
                                         hub=self.hub, port=0, tailnet=phone.Tailnet(run=ts))
        self.backend.store.set_on(True)
        self.backend.poll = 0.02
        self.backend.start()
        self.addCleanup(self.backend.close)
        self.assertTrue(phone_hub.mount(self.hub, self.backend))
        participant = SimpleNamespace(runtime=self.node.runtime, client=self.node.client, device_id="zenbook")
        for patch in (mock.patch("jarvis.continuity.participant.lookup", return_value=participant),
                      mock.patch("jarvis.continuity.integration.production_binding",
                                 return_value={"runtime": self.node.runtime})):
            patch.start()
            self.addCleanup(patch.stop)
        self.seen = []
        original = phone.PhoneHandler._route
        seen = self.seen

        def track(handler, method):
            seen.append((handler.path, dict(handler.headers.items())))
            return original(handler, method)

        patch = mock.patch.object(phone.PhoneHandler, "_route", new=track)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, method, path, *, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.server.server_port, timeout=10)
        request_headers = {"Host": PUBLIC.removeprefix("https://"), "Origin": FRONT,
                           "X-Forwarded-For": ORACLE_IP, "Tailscale-User-Login": LOGIN,
                           "X-Jarvis-Front-For": PHONE_A, "X-Jarvis-Front-Login": LOGIN,
                           "X-JARVIS-Phone": "1", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        body = json.dumps(data).encode() if data is not None else None
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            raw = response.read()
            parsed = json.loads(raw) if raw and "json" in (response.getheader("Content-Type") or "") else raw
            return response.status, parsed, response
        finally:
            conn.close()

    def paired(self):
        code, code_id = self.backend.store.new_code("Pocket")
        status, _, _ = self.request("POST", "/phone/pair", data={"code": code, "name": "Pocket"})
        self.assertEqual(status, 202)
        self.backend.store.approve(code_id)
        status, result, response = self.request("POST", "/phone/pair/status", data={"code": code})
        self.assertEqual(status, 200, result)
        return {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0],
                "X-CSRF-Token": result["csrf_token"]}

    def test_shell_and_paired_ask_keep_real_phone_identity_and_csrf_gates(self):
        status, result, _ = self.request("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(result["name"], "JARVIS")
        credentials = self.paired()
        status, result, _ = self.request("POST", "/phone/api/ask", data={"text": "status"}, headers=credentials)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(self.console.submitted), 1)
        missing = {"Cookie": credentials["Cookie"]}
        self.assertEqual(self.request("POST", "/phone/api/ask", data={"text": "again"}, headers=missing)[0], 403)
        self.assertEqual(len(self.console.submitted), 1)
        self.assertEqual(self.seen[-1][1]["X-Jarvis-Front-For"], PHONE_A)
        self.assertEqual(self.request("GET", "/phone/api/me", headers={"X-Jarvis-Front-Login": "fake@example.com"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Origin": "https://other.ts.net"})[0], 403)
        self.assertEqual(self.request("GET", "/", headers={"Host": "other.ts.net:8443"})[0], 403)

    def test_owner_bearer_never_authenticates_or_reaches_phone_backend(self):
        status, result, _ = self.request("GET", "/phone/api/me", headers={
            "Authorization": "Bearer " + self.hub.owner, "X-JARVIS-Main": "zenbook"})
        self.assertEqual(status, 401)
        self.assertTrue(result["pair"])
        self.assertNotIn("Authorization", self.seen[-1][1])
        self.assertNotIn("X-JARVIS-Main", self.seen[-1][1])

    def test_frame_sequence_dimensions_and_idle_liveness_survive_hub(self):
        credentials = self.paired()
        with mock.patch.object(self.backend, "frame", return_value=(99, b"jpeg", 1600, 900)):
            status, result, response = self.request("GET", "/phone/api/session/frame?after=98", headers=credentials)
        self.assertEqual((status, result), (200, b"jpeg"))
        self.assertEqual([response.getheader(k) for k in ("X-Frame-Seq", "X-Frame-Width", "X-Frame-Height")],
                         ["99", "1600", "900"])
        with mock.patch.object(self.backend, "frame", return_value=None), \
                mock.patch.object(self.backend, "node_ago", return_value=0.1):
            status, _, response = self.request("GET", "/phone/api/session/frame?after=99", headers=credentials)
        self.assertEqual(status, 204)
        self.assertEqual(response.getheader("X-Node-Ago"), "0.1")

    def test_not_ready_or_wrong_tuple_refuses_without_losing_local_grant(self):
        snapshot = self.node.runtime.lease_snapshot()
        current = self.node.client.status()
        for update in ({"main_ready": False, "mode": "HANDOVER"}, {"epoch": current["epoch"] + 1},
                       {"incarnation": True}, {"main": "dell"}, {"transition": {"state": "REQUESTED"}}):
            with self.subTest(update=update), mock.patch.object(self.node.client, "status", return_value={**current, **update}):
                self.assertEqual(self.request("GET", "/")[0], 503)
                self.assertEqual(self.node.runtime.lease_snapshot(), snapshot)
        self.assertEqual(self.seen, [])

    def test_expired_authority_refuses_before_backend(self):
        self.clock.tick(LEASE_SECONDS + SAFETY_MARGIN + 1)
        self.assertEqual(self.request("GET", "/")[0], 503)
        self.assertEqual(self.seen, [])

    def test_missing_grant_and_local_pause_refuse_despite_live_ready_authority(self):
        with mock.patch.object(self.node.runtime, "lease_snapshot", return_value=None):
            self.assertEqual(self.request("GET", "/")[0], 503)
        snapshot = self.node.runtime.lease_snapshot()
        self.node.runtime.stop_new_admission("paused")
        self.assertTrue(self.node.client.status()["main_ready"])
        self.assertEqual(self.request("GET", "/")[0], 503)
        self.assertEqual(self.node.runtime.lease_snapshot(), snapshot)
        self.assertEqual(self.seen, [])

    def test_startup_mounts_only_runtime_view_and_deactivation_clears_pointer(self):
        from jarvis.continuity.startup import NodeMainServices
        config = copy.deepcopy(self.config)
        del config["phone"]["public_origin"]
        views = []

        def start(view, *_args):
            views.append(view)
            return self.backend

        self.hub.phone = None
        services = NodeMainServices(config, self.root, self.state,
                                    console_factory=lambda *_: self.console, phone_start=start)
        services.hub = self.hub
        self.assertTrue(services.activate())
        self.assertEqual(views[0]["phone"]["public_origin"], PUBLIC)
        self.assertNotIn("public_origin", config["phone"])
        self.assertIs(self.hub.phone, self.backend)
        services.deactivate()
        self.assertIsNone(self.hub.phone)

    def test_nonphone_paths_and_absolute_proxy_targets_are_never_intercepted(self):
        for path in ("/api/overview", "/v1/status", "/phone/../v1/status", "/phone/%2e%2e/api/state",
                     "http://127.0.0.1:1/", "//other.ts.net/phone/api/me"):
            self.request("GET", path)
        self.assertEqual(self.seen, [])

    def test_large_bodies_and_transfer_encoding_stop_before_backend(self):
        status, _, _ = self.request("POST", "/phone/api/ask", headers={"Content-Length": str(phone.MAX_BODY + 1)})
        self.assertEqual(status, 413)
        status, _, _ = self.request("POST", "/phone/api/ask", headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(self.seen, [])

    def test_backend_redirect_is_returned_without_following_it(self):
        credentials = self.paired()
        with mock.patch.object(self.backend, "me", side_effect=phone.Reply(302, {}, {"Location": "http://127.0.0.1:1/"})):
            status, _, response = self.request("GET", "/phone/api/me", headers=credentials)
        self.assertEqual(status, 302)
        self.assertEqual(response.getheader("Location"), "http://127.0.0.1:1/")

    def test_stable_front_pairs_and_relays_frames_through_existing_hub_mount(self):
        outer = self

        class LoopbackServe:
            # The TLS/Serve edge is simulated; both HTTP proxy hops and the phone
            # handler are real. Serve names the Oracle hop at the PC's listener.
            def open(self, request, timeout):
                conn = http.client.HTTPConnection("127.0.0.1", outer.hub.server.server_port, timeout=timeout)
                url = urlsplit(request.full_url)
                headers = dict(request.header_items())
                headers.update({"Host": url.netloc, "X-Forwarded-For": ORACLE_IP,
                                "Tailscale-User-Login": LOGIN})
                conn.request(request.get_method(), url.path + ("?" + url.query if url.query else ""),
                             body=request.data, headers=headers)
                response = conn.getresponse()
                original_close = response.close

                def close():
                    original_close()
                    conn.close()

                response.close = close
                return response

        front = PhoneFront(self.node.client.status,
                           {"zenbook": PUBLIC, "old-main": "http://127.0.0.1:1"},
                           bind_port=0, opener=LoopbackServe())
        front.start()
        self.addCleanup(front.close)

        def request(method, path, data=None, credentials=None):
            conn = http.client.HTTPConnection("127.0.0.1", front.bind_port, timeout=10)
            headers = {"Host": FRONT.removeprefix("https://"), "Origin": FRONT,
                       "X-Forwarded-For": PHONE_A, "Tailscale-User-Login": LOGIN,
                       "X-JARVIS-Phone": "1", "Content-Type": "application/json"}
            headers.update(credentials or {})
            try:
                conn.request(method, path, body=json.dumps(data).encode() if data else None, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                return response.status, raw, response
            finally:
                conn.close()

        self.assertEqual(request("GET", "/")[0], 200)
        code, code_id = self.backend.store.new_code("Pocket")
        self.assertEqual(request("POST", "/phone/pair", {"code": code})[0], 202)
        self.backend.store.approve(code_id)
        status, raw, response = request("POST", "/phone/pair/status", {"code": code})
        self.assertEqual(status, 200)
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertTrue(cookie.startswith(phone.COOKIE + ".zenbook="))
        credentials = {"Cookie": cookie, "X-Jarvis-Csrf-zenbook": json.loads(raw)["csrf_token"]}
        self.assertEqual(request("GET", "/phone/api/me", credentials=credentials)[0], 200)
        self.assertEqual(request("POST", "/phone/api/ask", {"text": "status"}, credentials)[0], 200)
        with mock.patch.object(self.backend, "frame", return_value=(99, b"jpeg", 1600, 900)):
            status, raw, response = request("GET", "/phone/api/session/frame?after=98", credentials=credentials)
        self.assertEqual((status, raw), (200, b"jpeg"))
        self.assertEqual(response.getheader("X-Frame-Seq"), "99")
        self.assertEqual(response.getheader("X-Jarvis-Main"), "zenbook")


if __name__ == "__main__":
    unittest.main()
