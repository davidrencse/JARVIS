"""Session kinds and openers (remote_view.py), the camera and locate job kinds, and the main as a
session target (cluster.py): what the phone server builds on.

Real loopback HTTP between a hub and node workers where routes matter; the screen, the
mouse/keyboard, ffmpeg and notifications are fakes. No camera, location provider, Lan Mouse or
real input is touched."""

import base64
import copy
import functools
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import camera, cluster, connections, control, remote_view, sharedinput
from jarvis.cluster import Client, Hub, HubRefused, LocalClient, NodeWorker, execute_local, save_json, validate_action
from jarvis.cluster_store import CAPABILITIES, NODE_DEFAULT
from jarvis.remote_view import Injector, NodeSession, Sessions, Taken, WrongKind
from tests.mobile.test_camera import fake_popen, jpeg, until

PHONE, OTHER_PHONE = "phone:p1", "phone:p2"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Pointer:
    def __init__(self):
        self.calls = []

    def move(self, x, y):
        self.calls.append(("move", round(x), round(y)))

    def button(self, which, how):
        self.calls.append(("button", which, how))

    def scroll(self, direction, amount):
        self.calls.append(("scroll", direction, amount))

    def type(self, text):
        self.calls.append(("type", text))

    def release(self):
        self.calls.append(("release",))


class Keys:
    def __init__(self):
        self.calls = []

    def key(self, code, down):
        self.calls.append((code, down))


def tiny():
    from PIL import Image
    return Image.new("RGB", (16, 10), (200, 0, 0)), {"x": 0, "y": 0, "w": 1600, "h": 1000}


class SessionKindTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = Sessions(self.clock)
        self.addCleanup(self.s.close_all, "test over")  # no sweeper thread outlives the test

    def test_screen_and_camera_sessions_live_side_by_side(self):
        screen = self.s.open("dell", "low")
        cam = self.s.open("dell", "low", kind="camera", opener=PHONE)
        self.assertEqual((screen["kind"], screen["opener"]), ("screen", "console"))
        self.assertEqual((cam["kind"], cam["opener"]), ("camera", PHONE))
        self.assertEqual(self.s.get(screen["id"])["state"], "starting")  # the camera didn't replace it
        again = self.s.open("dell", "low", kind="camera", opener=PHONE)  # same phone, same kind: replaced
        self.assertEqual(self.s.get(cam["id"])["state"], "ended")
        self.assertEqual(self.s.get(again["id"])["state"], "starting")
        self.assertEqual(self.s.get(screen["id"])["state"], "starting")

    def test_a_phone_never_replaces_someone_elses_session(self):
        console = self.s.open("dell", "low")
        with self.assertRaises(Taken) as caught:
            self.s.open("dell", "low", opener=PHONE)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.s.get(console["id"])["state"], "starting")
        cam = self.s.open("dell", "low", kind="camera", opener=PHONE)
        with self.assertRaisesRegex(Taken, "watching that camera"):
            self.s.open("dell", "low", kind="camera", opener=OTHER_PHONE)
        # David at the console still takes over anything, as before.
        self.s.open("dell", "low", kind="camera")
        self.assertEqual(self.s.get(cam["id"])["error"], "Replaced by a new session.")

    def test_the_consoles_take_over_offer_sees_only_its_own_screen_sessions(self):
        self.assertIsNone(self.s.current())
        self.s.open("dell", "low", opener=PHONE)
        self.s.open("zenbook", "low", kind="camera")
        self.assertIsNone(self.s.current())
        mine = self.s.open("asus", "low")
        self.assertEqual(self.s.current()["id"], mine["id"])
        self.assertEqual(self.s.current(kind="camera")["device"], "zenbook")
        self.assertEqual(self.s.current(opener=PHONE)["device"], "dell")
        self.assertEqual(len(self.s.views()), 3)
        self.assertEqual([v["device"] for v in self.s.views(PHONE)], ["dell"])

    def test_another_openers_session_reads_as_unknown(self):
        sid = self.s.open("dell", "low", opener=PHONE)["id"]
        self.s.frame("dell", sid, jpeg(), 64, 48)
        for opener in ("console", OTHER_PHONE):
            for call in (lambda: self.s.wait_frame(sid, 0, hold=0, opener=opener),
                         lambda: self.s.push(sid, [{"t": "release"}], opener=opener),
                         lambda: self.s.get(sid, opener=opener),
                         lambda: self.s.node_ago(sid, opener=opener),
                         lambda: self.s.close(sid, opener=opener)):
                with self.subTest(opener=opener), self.assertRaisesRegex(LookupError, "Unknown remote session"):
                    call()
        self.assertEqual(self.s.get(sid)["state"], "live")  # nothing above touched it
        self.assertIsNotNone(self.s.wait_frame(sid, 0, hold=0, opener=PHONE))
        self.assertEqual(self.s.push(sid, [{"t": "release"}], opener=PHONE), 1)
        self.assertIsNotNone(self.s.wait_frame(sid, 0, hold=0))  # the console's own calls: any opener
        self.assertEqual(self.s.close(sid, opener=PHONE)["state"], "ended")

    def test_camera_sessions_take_no_input(self):
        sid = self.s.open("dell", "low", kind="camera", opener=PHONE)["id"]
        with self.assertRaises(WrongKind) as caught:
            self.s.push(sid, [{"t": "key", "code": "KeyA", "down": True}], opener=PHONE)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(WrongKind):  # and a node can't drain any
            self.s.take_input("dell", sid, 0, hold=0)

    def test_camera_sessions_never_pause_lan_mouse(self):
        seen = []
        s = Sessions(self.clock, on_busy=seen.append)
        cam = s.open("dell", "low", kind="camera", opener=PHONE)
        self.assertEqual(seen[-1], False)
        screen = s.open("dell", "low", opener=PHONE)
        self.assertEqual(seen[-1], True)
        s.close(screen["id"])
        self.assertEqual(seen[-1], False)  # the camera still streams, but drives nothing
        self.assertEqual(s.get(cam["id"])["state"], "starting")
        s.close_all("test over")

    def test_camera_sessions_end_after_fifteen_minutes(self):
        cam = self.s.open("dell", "low", kind="camera", opener=PHONE)["id"]
        screen = self.s.open("dell", "low", opener=PHONE)["id"]
        for sid in (cam, screen):
            self.s.touch(sid)
            self.s.frame("dell", sid, jpeg(), 64, 48)
        for _ in range(remote_view.CAMERA_MAX // 10 + 1):  # both keep streaming and being watched
            self.clock.now += 10
            for sid in (cam, screen):
                self.s.frame("dell", sid, None, None, None)
                self.s.touch(sid)
        self.assertIn("15 minutes", self.s.get(cam)["error"])
        self.assertEqual(self.s.get(screen)["state"], "live")

    def test_a_camera_that_never_sends_a_picture_says_so(self):
        cam = self.s.open("dell", "low", pending=True, kind="camera", opener=PHONE)["id"]
        self.clock.now += remote_view.START_GRACE + 1
        self.assertEqual(self.s.get(cam)["error"], "dell never sent its camera picture.")
        quiet = self.s.open("dell", "low", kind="camera", opener=PHONE)["id"]
        self.clock.now += remote_view.MAIN_IDLE + 1
        self.assertEqual(self.s.get(quiet)["error"], "The phone stopped watching, so the session ended.")

    def test_end_opener_ends_only_that_openers_live_sessions(self):
        a = self.s.open("dell", "low", opener=PHONE)["id"]
        b = self.s.open("zenbook", "low", kind="camera", opener=PHONE)["id"]
        c = self.s.open("asus", "low")["id"]
        ended = self.s.end_opener(PHONE, "That phone was signed out.")
        self.assertEqual(sorted(v["id"] for v in ended), sorted([a, b]))
        self.assertEqual(self.s.get(a)["error"], "That phone was signed out.")
        self.assertEqual(self.s.get(c)["state"], "starting")
        self.assertEqual(self.s.end_opener(PHONE, "again"), [])

    def test_bad_kinds_and_openers_are_refused(self):
        for kw in ({"kind": "microphone"}, {"opener": "phone:"}, {"opener": "phone:../x"},
                   {"opener": "brain"}, {"opener": None}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.s.open("dell", "low", **kw)


class JobTests(unittest.TestCase):
    def test_a_remote_job_may_say_a_phone_opened_it(self):
        good = {"op": "start", "session": "a" * 32, "quality": "low", "key": "b" * 32}
        remote_view.validate({**good, "by": "phone"})
        remote_view.validate({**good, "by": "console"})
        for bad in ({**good, "by": "brain"}, {**good, "by": "phone", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                remote_view.validate(bad)

    def test_camera_and_locate_jobs_go_to_their_modules(self):
        validate_action("camera", {"op": "start", "session": "a" * 32, "quality": "low"})
        with self.assertRaises(ValueError):
            validate_action("camera", {"op": "start", "session": "a" * 32, "cmd": "ffmpeg"})
        calls = []
        fake = types.ModuleType("jarvis.locate")

        def check(payload):
            if set(payload) - {"timeout"}:
                raise ValueError("Invalid locate request.")
        fake.validate = check
        fake.run_here = lambda payload, config=None, state=None: calls.append((payload, state)) or {"fix": {}}
        with patch.dict(sys.modules, {"jarvis.locate": fake}), \
                patch("jarvis.camera.run", side_effect=lambda payload, config, state: {"ok": True, "via": "camera"}):
            validate_action("locate", {"timeout": 9})
            with self.assertRaises(ValueError):
                validate_action("locate", {"shell": "where-am-i"})
            self.assertEqual(execute_local("locate", {}, {}, "/tmp/x"), {"fix": {}})
            self.assertEqual(calls, [({}, "/tmp/x")])
            self.assertEqual(execute_local("camera", {"op": "stop", "session": "a" * 32}, {}, "/tmp/x"),
                             {"ok": True, "via": "camera"})

    def test_camera_and_locate_are_opt_in_capabilities(self):
        self.assertLessEqual({"camera", "locate"}, CAPABILITIES)
        self.assertFalse({"camera", "locate"} & NODE_DEFAULT)
        self.assertIn("camera", connections.CAPS)
        self.assertIn("locate", connections.CAPS)
        self.assertNotIn("locate", cluster.READ_ONLY_DESKTOP)
        self.assertFalse(cluster._read_only("locate", {}))
        self.assertFalse(cluster._read_only("camera", {}))

    def hub(self):
        fake = SimpleNamespace(main="main", remote=Sessions(Clock()), sent=[],
                               config={"devices": {"main": {}, "dell": {"name": "dell"}}})
        self.addCleanup(fake.remote.close_all, "test over")

        def dispatch(node, kind, payload, timeout):
            fake.sent.append((kind, dict(payload)))
            return {"ok": True}
        fake.dispatch = dispatch
        return fake

    def test_hub_start_jobs_for_each_kind_and_opener(self):
        fake = self.hub()
        console = Hub.remote_open(fake, "dell", "low")  # the console's own path: the job is unchanged
        kind, payload = fake.sent[-1]
        self.assertEqual((kind, sorted(payload)), ("remote", ["key", "op", "quality", "session"]))
        fake.remote.close(console["id"])
        Hub.remote_open(fake, "dell", "low", kind="screen", opener=PHONE)
        self.assertEqual(fake.sent[-1][0], "remote")
        self.assertEqual(fake.sent[-1][1]["by"], "phone")
        view = Hub.remote_open(fake, "dell", "low", kind="camera", opener=PHONE)
        kind, payload = fake.sent[-1]
        self.assertEqual((kind, sorted(payload)), ("camera", ["key", "op", "quality", "session"]))
        self.assertEqual((view["kind"], view["opener"]), ("camera", PHONE))
        with self.assertRaises(ValueError):
            Hub.remote_open(fake, "dell", "low", kind="files")
        for node in ("main", "nobody"):  # the main only with allow_main; unknown never
            with self.subTest(node=node), self.assertRaises(ValueError):
                Hub.remote_open(fake, node, "low", opener=PHONE)

    def test_a_phone_cannot_take_the_consoles_screen_through_the_hub(self):
        fake = self.hub()
        console = Hub.remote_open(fake, "dell", "low")
        sent = len(fake.sent)
        with self.assertRaises(Taken):
            Hub.remote_open(fake, "dell", "low", opener=PHONE)
        self.assertEqual(len(fake.sent), sent)  # nothing reached the node
        self.assertNotEqual(fake.remote.get(console["id"])["state"], "ended")


class NodeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        remote_view._ACTIVE.clear()
        self.addCleanup(lambda: remote_view._LAN.update(owed=False, starting=0))
        self.addCleanup(remote_view._ACTIVE.clear)
        self.addCleanup(lambda: [s.end("test over") for s in list(remote_view._ACTIVE.values())])

    class Hub:
        def __init__(self):
            self.ends = []

        def request(self, path, body, timeout=None):
            if body.get("end") is not None:
                self.ends.append(body["end"])
                return {"stop": True}
            if path == "/v1/remote/input":
                time.sleep(0.05)
                return {"events": [], "stop": False}
            return {"stop": False}

    def test_a_phone_screen_session_shows_a_notification_at_start_and_end(self):
        notes = []
        with patch("jarvis.remote_view.notify_here", side_effect=lambda text, title="JARVIS": notes.append(text)):
            result = remote_view.run({"op": "start", "session": "a" * 32, "quality": "low", "by": "phone"},
                                     {}, self.state, client=self.Hub(), injector=Injector(Pointer(), Keys()),
                                     capture=tiny)
            self.assertTrue(result["ok"])
            session = remote_view._ACTIVE["a" * 32]
            self.assertTrue(session.announce)
            session.end("Disconnected.")
            session.join(3)
            self.assertTrue(until(lambda: session._done))
        self.assertIn("Your phone is viewing and controlling this screen", notes[0])
        self.assertIn("stopped controlling", notes[-1])

    def test_a_console_session_shows_none(self):
        with patch("jarvis.remote_view.notify_here") as note:
            remote_view.run({"op": "start", "session": "b" * 32, "quality": "low"}, {}, self.state,
                            client=self.Hub(), injector=Injector(Pointer(), Keys()), capture=tiny)
            remote_view.end_all("test over")
        note.assert_not_called()

    def test_the_mains_own_session_leaves_lan_mouse_to_controller_pause(self):
        calls = []
        remote_view._LAN.update(owed=True)  # even an owed restart is not this session's to pay
        with patch.object(sharedinput, "lan_mouse_status", side_effect=lambda: calls.append("status") or {"running": True}), \
                patch.object(sharedinput, "stop", side_effect=lambda sharing_only=False: calls.append("stop")), \
                patch.object(sharedinput, "start", side_effect=lambda: calls.append("start")), \
                patch.dict(os.environ, {"JARVIS_NO_MODELS": "0"}):
            session = NodeSession({}, self.state, "c" * 32, "low", client=self.Hub(),
                                  injector=Injector(Pointer(), Keys()), capture=tiny, lan_mouse=False)
            remote_view._ACTIVE[session.sid] = session
            session.start()
            session.end("done")
            session.join(3)
            self.assertTrue(until(lambda: session._done))
        self.assertEqual(calls, [])
        self.assertTrue(remote_view._LAN["owed"])
        marker = self.state / remote_view.MARKER
        self.assertFalse(marker.exists())

    def test_notices_are_silent_under_tests_and_never_put_text_in_a_command(self):
        self.assertFalse(remote_view.notify_here("hello"))
        seen = []
        with patch("jarvis.platforms.on_windows", return_value=True):
            remote_view._show_notice("JARVIS", "x'; Remove-Item C:\\ -Recurse; '",
                                     popen=lambda argv, **kw: seen.append((argv, kw)), which=lambda n: None)
        argv, kw = seen[0]
        self.assertNotIn("Remove-Item", " ".join(argv))
        self.assertEqual(kw["env"]["JARVIS_NOTICE_TEXT"], "x'; Remove-Item C:\\ -Recurse; '")
        self.assertIn("-WindowStyle", argv)
        ran = []
        with patch("jarvis.platforms.on_windows", return_value=False):
            remote_view._show_notice("JARVIS", "on", run=lambda argv, **kw: ran.append(argv),
                                     which=lambda n: "/usr/bin/notify-send" if n == "notify-send" else None)
        self.assertEqual(ran, [["/usr/bin/notify-send", "-a", "JARVIS", "JARVIS", "on"]])


class LocalClientTests(unittest.TestCase):
    def test_refusals_become_hub_refused_with_the_routes_status(self):
        for error, status in ((PermissionError("no"), 403), (LookupError("gone"), 404),
                              (ValueError("bad"), 400), (WrongKind("no input"), 400)):
            def call(node, path, data, error=error):
                raise error
            client = LocalClient(SimpleNamespace(main="main", session_call=call))
            with self.subTest(error=error), self.assertRaises(HubRefused) as caught:
                client.request("/v1/remote/frame", {"session": "a" * 32})
            self.assertEqual(caught.exception.status, status)
        got = []
        client = LocalClient(SimpleNamespace(main="main", session_call=lambda *a: got.append(a) or {"stop": False}))
        self.assertEqual(client.request("/v1/remote/input", None), {"stop": False})
        self.assertEqual(got, [("main", "/v1/remote/input", {})])


def configuration(local="main"):
    return {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                        "node": {"hostname": "node-host", "transport": "ssh", "label": "Node PC"},
                        "other": {"hostname": "other-host", "transport": "ssh"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": local, "auto_update": False,
                        "main_url": "http://127.0.0.1:7778"}}


class HubBase(unittest.TestCase):
    """A real hub on loopback; nodes enrolled with the capabilities a test needs."""

    def setUp(self):
        for p in (patch.object(NodeWorker, "_untrusted", return_value=None),
                  patch("jarvis.cluster.POLL_WAIT", 0.05),
                  patch("jarvis.remote_view.INPUT_HOLD", 0.05),
                  patch("jarvis.remote_view.KEEPALIVE", 0.05)):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main_state = self.root / "main"
        self.config = configuration()
        self.prepare()
        self.hub = Hub(self.config, self.root, self.main_state).start(port=0)
        self.addCleanup(self.hub.close)
        self.config["cluster"]["main_url"] = self.url = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        remote_view._ACTIVE.clear()
        self.addCleanup(remote_view._ACTIVE.clear)
        def _end_node_sessions():
            sessions = list(remote_view._ACTIVE.values())
            for session in sessions:
                session.end("test over")
            for session in sessions:
                session.join(2)
        self.addCleanup(_end_node_sessions)
        self.addCleanup(camera.end_all, "test over", 2)

    def prepare(self):
        """Runs before the hub starts (a test may leave a marker for it)."""

    def node(self, name, caps, executor=None):
        token = self.hub.broker.enroll(name, caps)
        config = copy.deepcopy(self.config)
        config["cluster"]["local_device"] = name
        state = self.root / name
        save_json(state / "cluster-node.json", {"node_id": name, "main_device": "main", "token": token})
        worker = NodeWorker(config, state, executor=executor)
        worker.once()  # heartbeat: online, with this code
        return SimpleNamespace(token=token, config=config, state=state, worker=worker)

    def post(self, token, path, body):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {token}", "X-JARVIS-Main": "main", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def serve(self, worker, call, seconds=8):
        """Run call() (it waits on the node) while the node polls."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call)
            deadline = time.monotonic() + seconds
            while not future.done() and time.monotonic() < deadline:
                worker.once()
            return future.result(timeout=2)


class HubRouteTests(HubBase):
    def test_each_session_kind_needs_its_own_capability(self):
        cam_only = self.node("node", {"camera"})
        desk_only = self.node("other", {"desktop"})
        screen = self.hub.remote.open("node", "low")["id"]
        cam = self.hub.remote.open("other", "low", kind="camera", opener=PHONE)["id"]
        frame = {"session": screen, "jpeg": base64.b64encode(jpeg()).decode(), "width": 64, "height": 48}
        self.assertEqual(self.post(cam_only.token, "/v1/remote/frame", frame)[0], 403)
        self.assertEqual(self.post(cam_only.token, "/v1/remote/input", {"session": screen})[0], 403)
        self.assertEqual(self.post(desk_only.token, "/v1/remote/frame", {**frame, "session": cam})[0], 403)
        nothing = self.hub.broker.enroll("spare", {"inventory"})
        self.assertEqual(self.post(nothing, "/v1/remote/frame", frame)[0], 403)
        self.assertEqual(self.hub.remote.get(screen)["frames"], 0)
        # the right capability for the kind: accepted
        cam2 = self.hub.remote.open("node", "low", kind="camera", opener=PHONE)["id"]
        status, reply = self.post(cam_only.token, "/v1/remote/frame", {**frame, "session": cam2})
        self.assertEqual((status, reply["stop"]), (200, False))
        self.assertIn("mac", reply)
        self.assertEqual(self.post(cam_only.token, "/v1/remote/input", {"session": cam2})[0], 400)  # no input

    def test_nobody_starts_sessions_or_locates_over_http_not_even_the_owner(self):
        dell = self.node("node", CAPABILITIES)  # includes "console": may use /v1/control
        jobs = {"remote": {"op": "start", "session": "a" * 32, "quality": "low"},
                "camera": {"op": "start", "session": "a" * 32, "quality": "low"},
                "locate": {}}
        for kind, payload in jobs.items():
            for token, path, node in ((self.hub.owner, "/v1/action", "node"), (self.hub.owner, "/v1/action", "main"),
                                      (dell.token, "/v1/control", "node")):
                with self.subTest(kind=kind, path=path, node=node):
                    status, reply = self.post(token, path, {"node": node, "kind": kind, "payload": payload})
                    self.assertEqual(status, 403)
                    self.assertIn("Only the main's console", reply["error"])

    def test_nodes_claim_camera_and_locate_jobs_only_with_the_capability(self):
        ran = []
        fake = types.ModuleType("jarvis.locate")
        fake.validate = lambda payload: None
        fake.run_here = lambda payload, config=None, state=None: {"fix": None}
        node = self.node("node", {"camera", "locate"}, executor=lambda kind, payload: ran.append(kind) or {"ok": True})
        payload = {"op": "stop", "session": "a" * 32}
        self.assertEqual(self.serve(node.worker, lambda: self.hub.dispatch("node", "camera", payload, timeout=5)),
                         {"ok": True})
        with patch.dict(sys.modules, {"jarvis.locate": fake}):
            self.assertEqual(self.serve(node.worker, lambda: self.hub.dispatch("node", "locate", {}, timeout=5)),
                             {"ok": True})
            self.assertEqual(self.hub.dispatch("main", "locate", {}, timeout=5), {"fix": None})  # in-process
        self.assertEqual(ran, ["camera", "locate"])
        self.node("other", {"desktop"})  # no camera capability: the hub never queues it
        with self.assertRaisesRegex(ValueError, "not enrolled for camera"):
            self.hub.dispatch("other", "camera", payload, timeout=5)

    def test_sessions_on_the_main_never_go_through_dispatch(self):
        with self.assertRaisesRegex(ValueError, "not the main"):
            self.hub.dispatch("main", "remote", {"op": "start", "session": "a" * 32})
        with self.assertRaisesRegex(ValueError, "camera"):
            self.hub.dispatch("main", "camera", {"op": "start", "session": "a" * 32})
        with self.assertRaisesRegex(ValueError, "other computers"):
            self.hub.remote_open("main", "low", opener=PHONE)

    def test_target_caps(self):
        self.node("node", {"desktop", "camera"})
        caps = self.hub.target_caps("node")
        self.assertEqual((caps["remote"], caps["camera"]), ((True, None), (True, None)))
        self.assertFalse(caps["locate"][0])
        self.assertIn("jarvis cluster caps node", caps["locate"][1])
        self.assertIn("Not paired", self.hub.target_caps("other")["camera"][1])
        self.assertEqual(self.hub.target_caps("nobody")["remote"], (False, "Unknown computer."))
        with patch("jarvis.cluster._screen_here", return_value=(True, None)):
            main = self.hub.target_caps("main")
        self.assertEqual((main["remote"], main["locate"]), ((True, None), (True, None)))
        self.assertFalse(main["camera"][0])
        self.assertIn("jarvis camera allow on", main["camera"][1])
        with patch("jarvis.release.served_code", return_value="different"), \
                patch("jarvis.cluster.served_code", return_value="different"):
            self.assertIn("older JARVIS code", self.hub.target_caps("node")["remote"][1])

    def test_screen_here_reasons(self):
        (self.main_state / control.STOP_FILE).write_text(str(time.time() + 600))
        self.assertIn("stopped", cluster._screen_here(self.main_state)[1])
        (self.main_state / control.STOP_FILE).unlink()
        with patch("jarvis.control.backend", side_effect=LookupError("wtype is missing")), \
                patch("jarvis.platforms.detect", return_value={"os": "linux"}):
            self.assertEqual(cluster._screen_here(self.main_state),
                             (False, "Remote control can't run on this computer: wtype is missing"))
        with patch("jarvis.control.backend", return_value=object()), \
                patch("jarvis.platforms.detect", return_value={"os": "linux"}):
            self.assertEqual(cluster._screen_here(self.main_state), (True, None))


class NodeCameraTests(HubBase):
    def test_a_camera_only_node_streams_to_the_phone_and_stops(self):
        node = self.node("node", {"camera"})
        (node.state / camera.SETTINGS).write_text(json.dumps({"allow": True, "device": "/dev/video1"}))
        popen, notes = fake_popen(), []
        real = camera.run
        with patch("jarvis.camera.run", functools.partial(
                real, which=lambda name: "/usr/bin/ffmpeg", os_name="linux", popen=popen,
                notify=lambda text, title=None: notes.append(text))):
            view = self.serve(node.worker, lambda: self.hub.remote_open("node", "low", kind="camera", opener=PHONE))
            sid = view["id"]
            self.assertEqual((view["kind"], view["opener"], view["device"]), ("camera", PHONE, "node"))
            frame = self.hub.session_frame(sid, 0, PHONE)
            self.assertIsNotNone(frame)
            self.assertEqual(frame[1][:2], b"\xff\xd8")
            self.assertEqual(self.hub.session_view(sid, PHONE)["state"], "live")
            with self.assertRaises(WrongKind):
                self.hub.session_input(sid, [{"t": "key", "code": "KeyA", "down": True}], PHONE)
            with self.assertRaises(LookupError):  # another phone can't watch it
                self.hub.session_frame(sid, 0, OTHER_PHONE)
            self.assertIsNone(self.hub.remote.current())  # nor does the console's take-over offer show it
            self.assertEqual(remote_view._ACTIVE, {})
            self.assertEqual(self.hub.remote_close(sid, opener=PHONE)["state"], "ended")
            self.assertTrue(until(lambda: not camera._ACTIVE))
            self.assertIn("terminate", popen.procs[0].calls)
            self.assertTrue(until(lambda: len(notes) == 2))
            self.assertIn("camera off", notes[-1])


class MainTargetTests(HubBase):
    """allow_main: the phone views or drives the main itself, in the hub's own process."""

    def setUp(self):
        super().setUp()
        self.pointer, self.keys, self.notes = Pointer(), Keys(), []
        for p in (patch("jarvis.remote_view.Grabber", lambda: tiny),
                  patch("jarvis.remote_view.Injector", lambda: Injector(self.pointer, self.keys)),
                  patch("jarvis.remote_view.notify_here",
                        side_effect=lambda text, title="JARVIS": self.notes.append(text))):
            p.start()
            self.addCleanup(p.stop)

    def test_the_phone_drives_the_mains_own_screen(self):
        view = self.hub.remote_open("main", "low", opener=PHONE, allow_main=True)
        sid = view["id"]
        self.assertEqual((view["device"], view["kind"], view["opener"]), ("main", "screen", PHONE))
        session = remote_view._ACTIVE[sid]
        self.assertFalse(session.lan_mouse)  # ControllerPause owns the main's Lan Mouse
        self.assertTrue(session.announce)
        self.assertIsInstance(session.client, LocalClient)
        seq, data, width, height = self.hub.session_frame(sid, 0, PHONE)
        self.assertEqual((data[:3], width, height), (b"\xff\xd8\xff", 1600, 1000))
        self.assertEqual(self.hub.session_input(sid, [{"t": "down", "b": "left", "x": 0.5, "y": 0.5},
                                                      {"t": "key", "code": "ShiftLeft", "down": True}], PHONE), 2)
        self.assertTrue(until(lambda: ("ShiftLeft", True) in self.keys.calls))
        self.assertIn(("move", 800, 500), self.pointer.calls)
        self.assertIn("Your phone is viewing and controlling this screen", self.notes[0])
        self.hub.remote_close(sid, opener=PHONE)
        self.assertTrue(until(lambda: session._done))
        self.assertIn(("ShiftLeft", False), self.keys.calls)
        self.assertIn(("button", "left", "up"), self.pointer.calls)
        self.assertFalse((self.main_state / remote_view.MARKER).exists())
        self.assertIn("stopped controlling", self.notes[-1])

    def test_closing_the_hub_lets_go_of_the_mains_own_session_first(self):
        sid = self.hub.remote_open("main", "low", opener=PHONE, allow_main=True)["id"]
        self.assertIsNotNone(self.hub.session_frame(sid, 0, PHONE))
        self.hub.session_input(sid, [{"t": "key", "code": "ControlLeft", "down": True}], PHONE)
        self.assertTrue(until(lambda: ("ControlLeft", True) in self.keys.calls))
        session = remote_view._ACTIVE[sid]
        self.hub.close()
        self.assertTrue(session._done)
        self.assertIn(("ControlLeft", False), self.keys.calls)
        self.assertEqual(remote_view._ACTIVE, {})
        self.assertEqual(self.hub._here, {})

    def test_the_mains_own_camera_and_control_stop(self):
        (self.main_state / camera.SETTINGS).write_text(json.dumps({"allow": True, "device": "/dev/video1"}))
        popen, notes = fake_popen(), []
        real = camera.run
        with patch("jarvis.camera.run", functools.partial(
                real, which=lambda name: "/usr/bin/ffmpeg", os_name="linux", popen=popen,
                notify=lambda text, title=None: notes.append(text))):
            sid = self.hub.remote_open("main", "low", kind="camera", opener=PHONE, allow_main=True)["id"]
            self.assertIsNotNone(self.hub.session_frame(sid, 0, PHONE))
            self.assertEqual(remote_view._ACTIVE, {})  # never a screen session's registry or marker
            self.assertFalse((self.main_state / remote_view.MARKER).exists())
            # `jarvis control stop` ends camera sessions too
            (self.main_state / control.STOP_FILE).write_text(str(time.time() + 600))
            self.assertTrue(until(lambda: self.hub.remote.get(sid)["state"] == "ended"))
            self.assertIn("stopped on this computer", self.hub.remote.get(sid)["error"])
            self.assertTrue(until(lambda: not camera._ACTIVE))
            self.assertIn("terminate", popen.procs[0].calls)

    def test_a_refused_start_on_the_main_closes_the_session(self):
        sid_before = {v["id"] for v in self.hub.sessions(PHONE)}
        with self.assertRaisesRegex(RuntimeError, "jarvis camera allow on"):
            self.hub.remote_open("main", "low", kind="camera", opener=PHONE, allow_main=True)
        ended = [v for v in self.hub.sessions(PHONE) if v["id"] not in sid_before]
        self.assertEqual([v["state"] for v in ended], ["ended"])
        self.assertEqual(self.hub._here, {})

    def test_end_opener_ends_the_phones_sessions_on_the_main_too(self):
        sid = self.hub.remote_open("main", "low", opener=PHONE, allow_main=True)["id"]
        session = remote_view._ACTIVE[sid]
        self.assertEqual([v["id"] for v in self.hub.end_opener(PHONE)], [sid])
        self.assertTrue(until(lambda: session._done))
        self.assertIn("signed out", self.hub.session_view(sid, PHONE)["error"])


class RecoverAtStartTests(HubBase):
    def prepare(self):
        self.main_state.mkdir(parents=True, exist_ok=True)
        (self.main_state / remote_view.MARKER).write_text(json.dumps({"session": "x", "held": ["KeyA"],
                                                                      "buttons": ["left"]}))
        self.pointer, self.keys = Pointer(), Keys()
        p = patch("jarvis.remote_view.Injector", lambda: Injector(self.pointer, self.keys))
        p.start()
        self.addCleanup(p.stop)

    def test_the_hub_lets_go_of_what_a_dead_main_session_held(self):
        self.assertIn(("KeyA", False), self.keys.calls)
        self.assertIn(("button", "left", "up"), self.pointer.calls)
        self.assertFalse((self.main_state / remote_view.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
