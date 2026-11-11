"""The phone app end to end: the real phone server (jarvis/phone.py) on a loopback port, a real
hub (cluster.Hub) with no nodes attached, the real session code running on the main itself
(remote_view.run, camera.run through cluster.LocalClient), the real pairing CLI
(phone.pair_cli) and the real locate request (locate.request -> run_here).

Only the edges are fakes: Tailscale's answers, the screen grabber and input injector, ffmpeg,
the location provider and the brain's job. Nothing here runs tailscale, opens a camera, reads
a location, moves the pointer or shows a notification."""

import functools
import http.client
import json
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import camera, cluster_proxy, locate, phone, remote_view
from jarvis.cluster import Hub
from jarvis.events import Bus
from jarvis.remote_view import Injector
from tests.mobile.test_camera import fake_popen, until
from tests.mobile.test_locate import WIFI_FIX, FakeRun, fake_which
from tests.remote.test_remote_kinds import Keys, Pointer, tiny

DNS = "main-host.tail0000.ts.net"
LOGIN = "david@example.com"
IPHONE, PIXEL, MAIN_IP = "100.90.0.2", "100.90.0.3", "100.100.1.1"


def configuration():
    return {"devices": {"main": {"label": "Main PC", "hostname": "main-host", "transport": "local"},
                        "node": {"label": "Node PC", "hostname": "node-host", "transport": "ssh"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": "main",
                        "auto_update": False, "main_url": "http://127.0.0.1:7778"}}


def whois(stable, os_name, hostname, ip):
    return {"Node": {"StableID": stable, "Name": f"{hostname}.tail0000.ts.net.",
                     "Hostinfo": {"OS": os_name, "Hostname": hostname}, "Addresses": [f"{ip}/32"]},
            "UserProfile": {"LoginName": LOGIN}}


class Tailscale:
    """`tailscale status/whois/serve status --json` as phone.Tailnet runs them: canned."""

    def __init__(self):
        self.status = {"Self": {"ID": "nMAIN", "DNSName": DNS + ".", "HostName": "main-host",
                                "TailscaleIPs": [MAIN_IP]}, "Peer": {}}
        self.whois = {IPHONE: whois("nIPHONE", "iOS", "iphone", IPHONE),
                      PIXEL: whois("nPIXEL", "android", "pixel", PIXEL),
                      MAIN_IP: whois("nMAIN", "linux", "main-host", MAIN_IP)}
        # Serve on, tailnet only: no AllowFunnel.
        self.serve = {"TCP": {"443": {"HTTPS": True}},
                      "Web": {f"{DNS}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7787"}}}}}

    def __call__(self, args, timeout=5):
        if args[:2] == ["status", "--json"]:
            return 0, json.dumps(self.status)
        if args[:2] == ["whois", "--json"] and args[2] in self.whois:
            return 0, json.dumps(self.whois[args[2]])
        if args[:2] == ["serve", "status"]:
            return 0, json.dumps(self.serve)
        return 1, ""


class Console:
    """ui.Console as the phone server uses it: a real event bus, submit/job (each job finishes
    on its own thread, as the brain's does) and remote_status."""

    def __init__(self):
        self.bus, self.jobs = Bus(), {}
        self.result = {"reply": "It's three o'clock."}

    def submit(self, text, tier=None, speak=False, case=None, origin_device=None, write=False):
        if any(j["state"] == "running" for j in self.jobs.values()):
            raise ValueError("JARVIS is still on the last request.")
        from jarvis.topology import ORIGIN
        job = {"id": uuid.uuid4().hex[:12], "text": text.strip(), "state": "running", "result": None,
               "error": None, "origin_device": origin_device or ORIGIN.get()}
        self.jobs[job["id"]] = job
        result = dict(self.result)

        def work():
            time.sleep(0.2)
            job.update(state="done", result=result)
        threading.Thread(target=work, daemon=True).start()
        return job

    def job(self, job_id):
        if job_id not in self.jobs:
            raise LookupError("Unknown job.")
        return self.jobs[job_id]

    def remote_status(self):
        return {"devices": [], "session": None, "why": None}


class PhoneEndToEnd(unittest.TestCase):
    def setUp(self):
        self.pointer, self.keys, self.notes, self.logged = Pointer(), Keys(), [], []
        self.popen = fake_popen()
        self.where = FakeRun(**{"where-am-i": WIFI_FIX, "nmcli": "yes:Home WiFi\n",
                                "tailscale": "100.100.1.1\n", "notify-send": ""})
        note = lambda text, title=None: self.notes.append(text)  # noqa: E731
        real_run, real_ready = camera.run, camera.ready
        for p in (mock.patch("jarvis.remote_view.INPUT_HOLD", 0.5),
                  mock.patch("jarvis.remote_view.KEEPALIVE", 0.2),
                  mock.patch("jarvis.remote_view.Grabber", lambda: tiny),
                  mock.patch("jarvis.remote_view.Injector", lambda: Injector(self.pointer, self.keys)),
                  mock.patch("jarvis.remote_view.notify_here", side_effect=note),
                  mock.patch("jarvis.cluster._screen_here", return_value=(True, None)),
                  mock.patch("jarvis.camera.run", functools.partial(
                      real_run, which=lambda name: "/usr/bin/ffmpeg", os_name="linux",
                      popen=self.popen, notify=note)),
                  mock.patch("jarvis.camera.ready", functools.partial(
                      real_ready, which=lambda name: "/usr/bin/ffmpeg", os_name="linux")),
                  mock.patch.object(locate, "_lan_ip", return_value="172.1.0.20"),
                  mock.patch.object(phone, "_log", self.logged.append)):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / ".jarvis"
        self.config = configuration()
        remote_view._ACTIVE.clear()
        self.addCleanup(remote_view._ACTIVE.clear)
        self.addCleanup(lambda: [s.end("test over") for s in list(remote_view._ACTIVE.values())])
        self.addCleanup(camera.end_all, "test over", 2)
        self.hub = Hub(self.config, root, self.state).start(port=0)
        self.addCleanup(self.hub.close)
        self.console = Console()
        self.events = []
        publish = self.console.bus.publish
        self.console.bus.publish = lambda kind, data=None: (self.events.append((kind, data)),
                                                            publish(kind, data))
        camera.set_allowed(self.state, True)  # David's own consent on the main
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": True, "device": "/dev/video1"}))
        phone.Store(self.state).set_on(True)
        self.server = phone.PhoneServer(
            self.config, self.state, console=self.console, hub=self.hub, port=0,
            tailnet=phone.Tailnet(run=Tailscale()))
        self.server.poll = 0.02
        self.server.start()
        # The real runtime's order: the phone server closes first, then the hub.
        self.runtime = cluster_proxy.Runtime(hub=self.hub, phone=self.server)
        self.addCleanup(self.runtime.close)

    # ---- helpers
    def call(self, method, path, body=None, ip=IPHONE, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        headers = {"Host": DNS, "Tailscale-User-Login": LOGIN, "Sec-Fetch-Site": "same-origin"}
        if ip:
            headers["X-Forwarded-For"] = ip
        if cookie:
            headers["Cookie"] = f"{phone.COOKIE}={cookie}"
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers.update({"X-JARVIS-Phone": "1", "Content-Type": "application/json"})
        try:
            conn.request(method, path, body=data, headers=headers)
            res = conn.getresponse()
            raw = res.read()
        finally:
            conn.close()
        kind = res.getheader("Content-Type") or ""
        return res.status, json.loads(raw) if raw and "json" in kind else raw, res

    def pair(self, ip, name, answer="y"):
        """Pair through the real CLI: it prints the code, the phone sends it, the CLI asks David
        y/N, the phone polls for its key. -> (token, phone id, what the CLI asked), or the
        refusal's (status, body) when David says no."""
        printed, asked, result = [], [], {}
        def ask(question):
            asked.append(question)
            return answer
        cli = threading.Thread(target=lambda: result.update(phone.pair_cli(
            self.state, ask=ask, out=printed.append, sleep=lambda s: time.sleep(0.02))), daemon=True)
        cli.start()
        self.assertTrue(until(lambda: printed))
        code = "".join(re.search(r"\b([A-Z2-9]{4})-([A-Z2-9]{4})\b", printed[0]).groups())
        status, body, _ = self.call("POST", "/phone/pair", {"code": code, "name": name}, ip=ip)
        self.assertEqual((status, body), (202, {"pending": True}))
        self.assertTrue(until(lambda: asked))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, body, res = self.call("POST", "/phone/pair/status", {"code": code}, ip=ip)
            if status != 202:
                break
            time.sleep(0.05)
        cli.join(5)
        self.assertFalse(cli.is_alive())
        if answer != "y":
            self.assertFalse(result["ok"])
            return status, body
        self.assertEqual(status, 200, body)
        self.assertTrue(result["ok"], result)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=7776000", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        # The code went to the CLI's own output and nowhere else.
        self.assertNotIn(code, json.dumps([self.events, self.logged], default=str))
        self.assertNotIn(code, (self.state / phone.STORE).read_text())
        return token, body["phone"]["id"], asked[0]

    def secret_free(self, *secrets):
        text = json.dumps([self.events, self.logged], default=str)
        for secret in secrets:
            self.assertNotIn(secret, text)

    # ---- the flows
    def test_the_real_shell_is_served_without_a_key(self):
        for path in phone.SHELL:
            status, data, res = self.call("GET", path, ip=None)
            self.assertEqual(status, 200, path)
            self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        status, html, res = self.call("GET", "/")
        self.assertEqual(res.getheader("Content-Security-Policy"), phone.CSP)
        self.assertIn(b"/phone.js", html)
        status, worker, res = self.call("GET", "/sw.js")
        self.assertNotIn(b"__SHELL_VERSION__", worker)
        self.assertIn(self.server.shell_version().encode(), worker)
        self.assertEqual(res.getheader("Cache-Control"), "no-cache")
        self.assertEqual(self.call("GET", "/app.js")[0], 404)  # never a console file

    def test_pairing_overview_and_locate(self):
        token, pid, asked = self.pair(IPHONE, "David's iPhone")
        self.assertEqual(asked, "iphone (iOS, david@example.com) wants to pair as 'David's iPhone'. Allow? [y/N] ")
        self.assertIn(("phone.paired", {"name": "David's iPhone"}), self.events)
        self.assertEqual(self.pair(PIXEL, "Pixel", answer="n"), (403, {"error": "Pairing was refused on the PC."}))
        status, body, _ = self.call("GET", "/phone/api/me", ip=PIXEL)
        self.assertEqual((status, body["pair"]), (401, True))
        # The iPhone's key works only from the iPhone.
        self.assertEqual(self.call("GET", "/phone/api/me", ip=PIXEL, cookie=token)[0], 401)
        status, me, _ = self.call("GET", "/phone/api/me", cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual((me["phone"]["id"], me["phone"]["name"], me["host"], me["main"]),
                         (pid, "David's iPhone", DNS, {"id": "main", "label": "Main PC"}))

        status, view, _ = self.call("GET", "/phone/api/overview", cookie=token)
        self.assertEqual(status, 200)
        devices = {d["id"]: d for d in view["devices"]}
        self.assertEqual((devices["main"]["role"], devices["main"]["online"]), ("main", True))
        self.assertEqual(devices["main"]["caps"], {"remote": True, "camera": True})
        self.assertEqual(devices["node"]["caps"], {"remote": False, "camera": False})
        self.assertIn("jarvis cluster invite node", devices["node"]["why"]["remote"])
        self.assertNotIn("location", devices["main"])
        self.assertEqual((view["sessions"], view["ask"]), ([], {"enabled": True}))

        self.assertEqual(self.call("POST", "/phone/api/locate", {"device": "main"}, cookie=token)[0], 404)
        self.assertFalse(self.where.called("where-am-i"))

    def test_sessions_on_the_main_stay_with_the_phone_that_opened_them(self):
        token, pid, _ = self.pair(IPHONE, "iPhone")
        other, _, _ = self.pair(PIXEL, "Pixel")

        status, view, _ = self.call("POST", "/phone/api/session/start",
                                    {"device": "main", "kind": "screen"}, cookie=token)
        self.assertEqual(status, 200, view)
        sid = view["id"]
        self.assertEqual((view["device"], view["kind"], view["opener"], view["quality"]),
                         ("main", "screen", f"phone:{pid}", "low"))  # low unless asked
        session = remote_view._ACTIVE[sid]
        self.assertFalse(session.lan_mouse)  # the hub's ControllerPause owns the main's Lan Mouse
        self.assertIn("Your phone is viewing and controlling this screen", self.notes[0])

        status, data, res = self.call("GET", f"/phone/api/session/frame?session={sid}&after=0", cookie=token)
        self.assertEqual((status, res.getheader("Content-Type"), data[:2]), (200, "image/jpeg", b"\xff\xd8"))
        self.assertEqual((res.getheader("X-Frame-Width"), res.getheader("X-Frame-Height")), ("1600", "1000"))
        events = [{"t": "move", "x": 0.5, "y": 0.5}, {"t": "key", "code": "KeyA", "down": True}]
        status, body, _ = self.call("POST", "/phone/api/session/input", {"session": sid, "events": events},
                                    cookie=token)
        self.assertEqual((status, body), (200, {"ok": True, "queued": 2}))
        self.assertTrue(until(lambda: ("KeyA", True) in self.keys.calls))
        self.assertIn(("move", 800, 500), self.pointer.calls)

        # The other phone can't see, drive, end or take it: unknown, as if it never existed.
        self.assertEqual(self.call("GET", "/phone/api/overview", ip=PIXEL, cookie=other)[1]["sessions"], [])
        for method, path, body in (("GET", f"/phone/api/session/frame?session={sid}&after=0", None),
                                   ("POST", "/phone/api/session/input", {"session": sid, "events": events}),
                                   ("POST", "/phone/api/session/stop", {"session": sid})):
            status, body, _ = self.call(method, path, body, ip=PIXEL, cookie=other)
            self.assertEqual((status, body), (404, {"error": "No such session."}), path)
        status, body, _ = self.call("POST", "/phone/api/session/start", {"device": "main", "kind": "screen"},
                                    ip=PIXEL, cookie=other)
        self.assertEqual(status, 409)
        self.assertIn("Someone else", body["error"])
        self.assertEqual([v["id"] for v in self.call("GET", "/phone/api/overview", cookie=token)[1]["sessions"]],
                         [sid])

        status, body, _ = self.call("POST", "/phone/api/session/stop", {"session": sid}, cookie=token)
        self.assertEqual((status, body["ok"], body["session"]["state"]), (200, True, "ended"))
        self.assertTrue(until(lambda: session._done))
        self.assertIn(("KeyA", False), self.keys.calls)  # ending lets go of what it held
        status, _, _ = self.call("GET", f"/phone/api/session/frame?session={sid}&after=0", cookie=token)
        self.assertEqual(status, 410)
        status, body, _ = self.call("POST", "/phone/api/session/input", {"session": sid, "events": events},
                                    cookie=token)
        self.assertEqual((status, body), (410, {"error": "Disconnected."}))  # the app stops, not retries

        # The camera: its own session kind, never input, ffmpeg stopped at the end.
        status, view, _ = self.call("POST", "/phone/api/session/start",
                                    {"device": "main", "kind": "camera", "quality": "medium"}, cookie=token)
        self.assertEqual(status, 200, view)
        cid = view["id"]
        self.assertEqual((view["kind"], view["quality"]), ("camera", "medium"))
        self.assertIn("-f", self.popen.procs[0].argv)
        self.assertEqual(self.popen.procs[0].argv[-1], "-")
        status, data, res = self.call("GET", f"/phone/api/session/frame?session={cid}&after=0", cookie=token)
        self.assertEqual((status, data[:2]), (200, b"\xff\xd8"))
        status, body, _ = self.call("POST", "/phone/api/session/input",
                                    {"session": cid, "events": [{"t": "key", "code": "KeyA", "down": True}]},
                                    cookie=token)
        self.assertEqual(status, 409)
        self.assertEqual(self.call("GET", f"/phone/api/session/frame?session={cid}&after=0",
                                   ip=PIXEL, cookie=other)[0], 404)
        self.assertEqual(remote_view._ACTIVE.get(cid), None)  # never the screen sessions' registry
        status, body, _ = self.call("POST", "/phone/api/session/stop", {"session": cid}, cookie=token)
        self.assertEqual(status, 200)
        self.assertTrue(until(lambda: not camera._ACTIVE))
        self.assertIn("terminate", self.popen.procs[0].calls)
        self.assertTrue(until(lambda: "JARVIS turned the camera off." in self.notes))

        kinds = [k for k, _ in self.events]
        for wanted in ("phone.session", "camera.on", "camera.off"):
            self.assertIn(wanted, kinds)
        self.assertIn(("camera.on", {"device": "main", "by": "iPhone"}), self.events)
        self.secret_free(token, other, sid, cid)

    def test_ask_runs_as_the_main_and_approvals_stay_on_the_pc(self):
        token, pid, _ = self.pair(IPHONE, "iPhone")
        other, _, _ = self.pair(PIXEL, "Pixel")
        status, body, _ = self.call("POST", "/phone/api/ask", {"text": "what time is it"}, cookie=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["reply"], "It's three o'clock.")
        job = self.console.jobs[body["job"]]
        self.assertEqual((job["via"], job["origin_device"]), (f"phone:{pid}", None))
        self.assertEqual(self.call("GET", f"/phone/api/ask/{job['id']}", cookie=token)[1]["reply"],
                         "It's three o'clock.")
        self.assertEqual(self.call("GET", f"/phone/api/ask/{job['id']}", ip=PIXEL, cookie=other)[0], 404)

        for word in ("yes", "Jarvis, approve", "ok"):
            status, body, _ = self.call("POST", "/phone/api/ask", {"text": word}, cookie=token)
            self.assertEqual((status, body), (400, {"error": "Approve on your PC."}), word)
        self.console.result = {"reply": "Shall I send it?", "confirmations": [{"id": "c1"}]}
        status, body, _ = self.call("POST", "/phone/api/ask", {"text": "email Sam the notes"}, cookie=token)
        self.assertEqual((status, body["reply"], body["waiting"]), (200, "Waiting for your OK on the PC.", True))

    def test_revoke_and_logout_end_that_phones_sessions(self):
        token, pid, _ = self.pair(IPHONE, "iPhone")
        other, oid, _ = self.pair(PIXEL, "Pixel")
        status, cam, _ = self.call("POST", "/phone/api/session/start", {"device": "main", "kind": "camera"},
                                   ip=PIXEL, cookie=other)
        self.assertEqual(status, 200, cam)

        # `jarvis phone revoke <id>` at the main's terminal: the key dies on the next request,
        # and the server's watcher ends the phone's sessions.
        self.assertEqual(phone.cli(SimpleNamespace(action="revoke", value=oid), self.config, self.state)["revoked"],
                         ["Pixel"])
        status, body, _ = self.call("GET", "/phone/api/me", ip=PIXEL, cookie=other)
        self.assertEqual((status, body["pair"]), (401, True))
        self.server.sync_phones()  # what its watcher does every few seconds
        self.assertTrue(until(lambda: not camera._ACTIVE))
        self.assertIn("signed out", self.hub.session_view(cam["id"], f"phone:{oid}")["error"])
        self.assertIn(("phone.revoked", {"name": "Pixel"}), self.events)

        # Logout from the phone itself: the same, plus the cookie is cleared.
        status, view, _ = self.call("POST", "/phone/api/session/start", {"device": "main"}, cookie=token)
        self.assertEqual(status, 200, view)
        session = remote_view._ACTIVE[view["id"]]
        status, body, res = self.call("POST", "/phone/api/logout", {}, cookie=token)
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertIn("Max-Age=0", res.getheader("Set-Cookie"))
        self.assertEqual(self.call("GET", "/phone/api/me", cookie=token)[0], 401)
        self.assertTrue(until(lambda: session._done))
        self.assertEqual(phone.listing(self.state), [])

        # Closing the runtime: the phone server goes first, then the hub lets go of the main.
        status, view, _ = self.call("GET", "/", cookie=None)
        self.assertEqual(status, 200)
        self.runtime.close()
        with self.assertRaises(OSError):
            self.call("GET", "/")
        self.assertEqual(self.hub._here, {})


if __name__ == "__main__":
    unittest.main()
