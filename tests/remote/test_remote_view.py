"""Remote control (remote_view.py): sessions on the hub, the node's session, and the console API.

Real loopback HTTP between a hub and a node worker; the screen and the mouse/keyboard are fakes.
"""

import copy
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from jarvis import remote_view
from jarvis.cluster import Client, Hub, NodeWorker, save_json, validate_action
from jarvis.cluster_store import CAPABILITIES
from jarvis.remote_view import KEYS, Ended, Injector, Sessions, clean_event
from jarvis.ui import Console, ConsoleServer, Handler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakePointer:
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


class FakeKeys:
    def __init__(self):
        self.calls = []

    def key(self, code, down):
        self.calls.append((code, down))


def jpeg(color=(10, 20, 30), size=(64, 40)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


class EventTests(unittest.TestCase):
    def test_events_are_checked_and_reduced(self):
        self.assertEqual(clean_event({"t": "move", "x": 0.5, "y": 1, "extra": "x"}),
                         {"t": "move", "x": 0.5, "y": 1.0})
        self.assertEqual(clean_event({"t": "key", "code": "KeyA", "down": True}),
                         {"t": "key", "code": "KeyA", "down": True})
        for bad in ({"t": "move", "x": 1.5, "y": 0}, {"t": "move", "x": True, "y": 0},
                    {"t": "down", "b": "thumb"}, {"t": "wheel", "dy": 0}, {"t": "wheel", "dy": 99},
                    {"t": "key", "code": "Key A", "down": True}, {"t": "key", "code": "KeyA"},
                    {"t": "text", "text": ""}, {"t": "shell", "cmd": "rm"}, "move"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_event(bad)

    def test_key_table_has_distinct_codes(self):
        evdev = [ev for _, _, ev in KEYS.values()]
        dupes = {code for code, (_, _, ev) in KEYS.items() if evdev.count(ev) > 1}
        self.assertEqual(dupes, set())  # every browser key presses its own Linux key
        self.assertEqual(KEYS["KeyA"][0], 0x41)
        self.assertEqual(KEYS["ArrowLeft"][:2], (0x25, True))  # extended, or Windows sees numpad 4


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = Sessions(self.clock)

    def test_moves_coalesce_but_clicks_and_keys_keep_order(self):
        sid = self.s.open("node")["id"]
        self.s.push(sid, [{"t": "move", "x": 0.1, "y": 0.1}, {"t": "move", "x": 0.2, "y": 0.2},
                          {"t": "down", "b": "left"}, {"t": "move", "x": 0.3, "y": 0.3},
                          {"t": "key", "code": "KeyA", "down": True}])
        got = [e for _, e in self.s.take_input("node", sid, hold=0)["events"]]
        self.assertEqual([e["t"] for e in got], ["move", "down", "move", "key"])
        self.assertEqual(got[0]["x"], 0.2)

    def test_a_bad_event_is_dropped_alone_so_the_key_up_still_lands(self):
        sid = self.s.open("node")["id"]
        self.assertEqual(self.s.push(sid, [{"t": "move", "x": 10 ** 400, "y": 0}, {"t": "bogus"},
                                           {"t": "key", "code": "ShiftLeft", "down": False},
                                           {"t": "up", "b": "left", "x": 1.0000001, "y": 0.5}]), 2)
        got = [e for _, e in self.s.take_input("node", sid, hold=0)["events"]]
        self.assertEqual(got, [{"t": "key", "code": "ShiftLeft", "down": False},
                               {"t": "up", "b": "left", "x": 1.0, "y": 0.5}])
        with self.assertRaises(ValueError):
            self.s.push(sid, "not a list")

    def test_events_stay_until_the_node_says_it_pressed_them(self):
        sid = self.s.open("node")["id"]
        self.s.push(sid, [{"t": "key", "code": "KeyA", "down": True}, {"t": "key", "code": "KeyA", "down": False}])
        first = self.s.take_input("node", sid, after=0, hold=0)["events"]
        self.assertEqual(self.s.take_input("node", sid, after=0, hold=0)["events"], first)  # reply lost: resent
        last = first[-1][0]
        self.assertEqual(self.s.take_input("node", sid, after=first[0][0], hold=0)["events"], first[1:])
        self.assertEqual(self.s.take_input("node", sid, after=last, hold=0)["events"], [])

    def test_a_silent_console_makes_the_node_let_go(self):
        sid = self.s.open("node")["id"]
        self.s.frame("node", sid, jpeg(), 64, 40)
        self.s.push(sid, [{"t": "key", "code": "ShiftLeft", "down": True}])
        n = self.s.take_input("node", sid, hold=0)["events"][-1][0]
        self.clock.now += remote_view.CONSOLE_QUIET + 1
        got = self.s.take_input("node", sid, after=n, hold=0)["events"]
        self.assertEqual([e for _, e in got], [{"t": "release"}])
        self.assertEqual(self.s.take_input("node", sid, after=got[-1][0], hold=0)["events"], [])  # once

    def test_only_the_sessions_node_may_feed_or_drain_it(self):
        sid = self.s.open("node")["id"]
        with self.assertRaises(PermissionError):
            self.s.frame("other", sid, jpeg(), 64, 40)
        with self.assertRaises(PermissionError):
            self.s.take_input("other", sid, hold=0)
        with self.assertRaises(LookupError):
            self.s.frame("node", "0" * 32, jpeg(), 64, 40)
        with self.assertRaises(ValueError):
            self.s.frame("node", sid, b"\x89PNG not a jpeg", 64, 40)

    def test_frames_go_live_and_wait_returns_only_newer(self):
        sid = self.s.open("node")["id"]
        self.assertIsNone(self.s.wait_frame(sid, 0, hold=0))
        self.assertEqual(self.s.frame("node", sid, jpeg(), 1920, 1080), {"stop": False})
        seq, data, w, h = self.s.wait_frame(sid, 0, hold=0)
        self.assertEqual((seq, w, h, self.s.get(sid)["state"]), (1, 1920, 1080, "live"))
        self.assertIsNone(self.s.wait_frame(sid, 1, hold=0))

    def test_console_gone_ends_the_session_and_tells_the_node(self):
        sid = self.s.open("node")["id"]
        self.s.frame("node", sid, jpeg(), 64, 40)
        self.clock.now += remote_view.MAIN_IDLE + 1
        reply = self.s.take_input("node", sid, hold=0)
        self.assertTrue(reply["stop"])
        self.assertIn("console", reply["why"])
        with self.assertRaises(Ended):
            self.s.wait_frame(sid, 0, hold=0)

    def test_silent_node_and_no_first_frame_end_it(self):
        sid = self.s.open("node")["id"]
        self.clock.now += remote_view.START_GRACE + 1
        self.assertEqual(self.s.get(sid)["state"], "ended")
        sid = self.s.open("node")["id"]
        self.s.frame("node", sid, jpeg(), 64, 40)
        for _ in range(3):  # the console keeps watching, the node went quiet
            self.clock.now += remote_view.NODE_IDLE / 2 + 1
            try:
                self.s.wait_frame(sid, 99, hold=0)
            except Ended:
                break
        self.assertIn("stopped answering", self.s.get(sid)["error"])

    def test_one_live_session_per_node(self):
        first = self.s.open("node")["id"]
        second = self.s.open("node")["id"]
        self.assertEqual(self.s.get(first)["state"], "ended")
        self.assertEqual(self.s.current()["id"], second)

    def test_queue_overflow_keeps_newest_and_releases_first(self):
        sid = self.s.open("node")["id"]
        for _ in range(remote_view.MAX_QUEUE // 100 + 2):
            self.s.push(sid, [{"t": "key", "code": "KeyA", "down": d} for d in [True, False] * 50])
        got = [e for _, e in self.s.take_input("node", sid, hold=0)["events"]]
        self.assertIn({"t": "release"}, got)
        self.assertLessEqual(len(got), remote_view.MAX_QUEUE + 1)
        self.assertNotIn({"t": "key", "code": "KeyA", "down": True}, got[:got.index({"t": "release"})])

    def test_a_fast_frame_is_kept_so_the_newest_screen_shows(self):
        # M1: the node counts any reply as "sent" and never resends an unchanged screen, so a
        # frame the hub dropped for arriving fast left the console on an old picture.
        sid = self.s.open("node", "low")["id"]
        self.s.frame("node", sid, jpeg(), 64, 40)
        self.s.frame("node", sid, jpeg((1, 1, 1)), 64, 40)  # same instant: still the newest
        self.assertEqual(self.s.get(sid)["frames"], 2)
        self.assertEqual(self.s.wait_frame(sid, 1, hold=0)[1], jpeg((1, 1, 1)))
        self.s.close(sid)
        self.assertIsNone(self.s._all[sid]["frame"])  # an ended session drops its picture


class InjectorTests(unittest.TestCase):
    def test_maps_to_the_real_screen_and_releases_what_it_held(self):
        pointer, keys = FakePointer(), FakeKeys()
        inj = Injector(pointer, keys)
        inj.area = {"x": -1920, "y": 0, "w": 3840, "h": 1080}
        inj.apply({"t": "down", "b": "left", "x": 0.5, "y": 0.5})
        inj.apply({"t": "key", "code": "ShiftLeft", "down": True})
        inj.apply({"t": "key", "code": "MediaPlayPause", "down": True})  # unknown: ignored
        inj.apply({"t": "wheel", "dy": -2})
        self.assertEqual(pointer.calls[:3], [("move", 0, 540), ("button", "left", "down"), ("scroll", "up", 2)])
        inj.release()
        self.assertIn(("ShiftLeft", False), keys.calls)
        self.assertIn(("button", "left", "up"), pointer.calls)
        self.assertEqual(pointer.calls[-1], ("release",))
        self.assertEqual((inj.held, inj.buttons), (set(), set()))

    def test_a_failed_key_up_stays_tracked_and_is_retried(self):
        class Flaky(FakeKeys):
            fail = True

            def key(self, code, down):
                if not down and self.fail:
                    raise OSError("ydotool timed out")
                super().key(code, down)
        keys = Flaky()
        inj = Injector(FakePointer(), keys)
        inj.apply({"t": "key", "code": "ShiftLeft", "down": True})
        with self.assertRaises(OSError):
            inj.apply({"t": "key", "code": "ShiftLeft", "down": False})
        self.assertEqual(inj.held, {"ShiftLeft"})
        keys.fail = False
        inj.release()
        self.assertEqual(keys.calls, [("ShiftLeft", True), ("ShiftLeft", False)])
        self.assertEqual(inj.held, set())

    def test_nothing_presses_after_the_final_release_or_a_stop(self):
        inj = Injector(FakePointer(), FakeKeys())
        inj.release(close=True)
        with self.assertRaises(remote_view.Stopped):
            inj.apply({"t": "key", "code": "KeyA", "down": True})
        stopped = Injector(FakePointer(), FakeKeys(), stopped=lambda: True)
        with self.assertRaises(remote_view.Stopped):
            stopped.apply({"t": "key", "code": "KeyA", "down": True})

    def test_release_waits_for_a_press_in_progress(self):
        started, go = threading.Event(), threading.Event()

        class Slow(FakeKeys):
            def key(self, code, down):
                if down:
                    started.set()
                    go.wait(2)
                super().key(code, down)
        keys = Slow()
        inj = Injector(FakePointer(), keys)
        presser = threading.Thread(target=inj.apply, args=({"t": "key", "code": "KeyA", "down": True},))
        presser.start()
        started.wait(2)
        releaser = threading.Thread(target=inj.release, kwargs={"close": True})
        releaser.start()
        time.sleep(0.1)
        go.set()
        presser.join(2)
        releaser.join(2)
        self.assertEqual(keys.calls, [("KeyA", True), ("KeyA", False)])  # the late down was let go

    def test_a_marker_left_by_a_dead_agent_releases_input_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(remote_view.recover(tmp))
            (Path(tmp) / remote_view.MARKER).write_text("{}")
            pointer = FakePointer()
            with patch("jarvis.remote_view.Injector", lambda: Injector(pointer, FakeKeys())):
                self.assertTrue(remote_view.recover(tmp))
            self.assertIn(("release",), pointer.calls)
            self.assertFalse((Path(tmp) / remote_view.MARKER).exists())

    def test_marker_records_what_is_held_and_recovery_lets_go_of_it(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            marks = []
            inj = Injector(FakePointer(), FakeKeys())
            inj.on_change = lambda held, buttons: marks.append((held, buttons))
            inj.apply({"t": "key", "code": "KeyA", "down": True})
            inj.apply({"t": "down", "b": "middle"})
            self.assertEqual(marks[-1], (["KeyA"], ["middle"]))  # written before the press
            # The process dies here: the marker on disk names what it held.
            (Path(tmp) / remote_view.MARKER).write_text(json.dumps(
                {"session": "x", "held": ["KeyA", "NotAKey"], "buttons": ["middle"]}))
            pointer, keys = FakePointer(), FakeKeys()
            self.assertTrue(remote_view.recover(tmp, injector=Injector(pointer, keys)))
            self.assertEqual(keys.calls, [("KeyA", False)])  # unknown codes dropped
            self.assertIn(("button", "middle", "up"), pointer.calls)
            self.assertFalse((Path(tmp) / remote_view.MARKER).exists())

    def test_recovery_that_cannot_let_go_keeps_the_marker(self):
        import json
        class Broken(FakeKeys):
            def key(self, code, down):
                raise OSError("no desktop session")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / remote_view.MARKER).write_text(json.dumps({"held": ["KeyA"]}))
            self.assertTrue(remote_view.recover(tmp, injector=Injector(FakePointer(), Broken())))
            self.assertTrue((Path(tmp) / remote_view.MARKER).exists())  # retried next start

    def test_no_area_yet_means_no_pointer_move(self):
        pointer = FakePointer()
        Injector(pointer, FakeKeys()).apply({"t": "move", "x": 0.5, "y": 0.5})
        self.assertEqual(pointer.calls, [])


def configuration(local="main"):
    return {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                        "node": {"hostname": "node-host", "transport": "ssh", "label": "Node PC"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": local, "auto_update": False,
                        "main_url": "http://127.0.0.1:7778"}}


class EndToEndTests(unittest.TestCase):
    """Hub + node worker over loopback HTTP; the node's screen and input are fakes."""

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
        self.main = configuration()
        self.console = Console(self.main, self.root, self.root / "main")
        self.addCleanup(self.console.close)
        self.hub = Hub(self.main, self.root, self.root / "main", console=self.console).start(port=0)
        self.addCleanup(self.hub.close)
        self.main["cluster"]["main_url"] = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        self.node = copy.deepcopy(self.main)
        self.node["cluster"]["local_device"] = "node"
        self.state = self.root / "node"
        token = self.hub.broker.enroll("node", CAPABILITIES)
        save_json(self.state / "cluster-node.json", {"node_id": "node", "main_device": "main", "token": token})
        self.client = Client(self.node, self.state)
        self.pointer, self.keys = FakePointer(), FakeKeys()
        self.frames = 0

        def grab():
            from PIL import Image
            self.frames += 1
            return Image.new("RGB", (320, 200), (self.frames % 255, 0, 0)), {"x": 0, "y": 0, "w": 1600, "h": 1000}
        for p in (patch("jarvis.remote_view.grab", grab),
                  patch("jarvis.remote_view.Grabber", lambda: grab),
                  patch("jarvis.remote_view.Injector", lambda: Injector(self.pointer, self.keys))):
            p.start()
            self.addCleanup(p.stop)
        def _end_node_sessions():
            sessions = list(remote_view._ACTIVE.values())
            for session in sessions:
                session.end("test over")
            for session in sessions:
                session.join(2)
        self.addCleanup(_end_node_sessions)
        self.worker = NodeWorker(self.node, self.state)
        self.worker.once()  # heartbeat: the node is online with this code

    def _open(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.hub.remote_open, "node", "low")
            deadline = time.monotonic() + 5
            while not future.done() and time.monotonic() < deadline:
                self.worker.once()
            return future.result(timeout=1)

    def _until(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return False

    def test_screen_streams_and_input_lands_on_the_node(self):
        session = self._open()
        self.assertEqual(session["device"], "node")
        frame = self.hub.remote.wait_frame(session["id"], 0, hold=5)
        self.assertIsNotNone(frame)
        seq, data, w, h = frame
        self.assertEqual((data[:3], w, h), (b"\xff\xd8\xff", 1600, 1000))
        self.hub.remote.push(session["id"], [{"t": "down", "b": "left", "x": 0.25, "y": 0.5},
                                             {"t": "key", "code": "ControlLeft", "down": True}])
        self.assertTrue(self._until(lambda: ("ControlLeft", True) in self.keys.calls))
        self.assertIn(("move", 400, 500), self.pointer.calls)
        self.assertIn(("button", "left", "down"), self.pointer.calls)
        # Disconnect: the node hears it, stops, and lets go of the button and the key.
        self.hub.remote_close(session["id"])
        self.assertTrue(self._until(lambda: not remote_view._ACTIVE))
        self.assertIn(("ControlLeft", False), self.keys.calls)
        self.assertIn(("button", "left", "up"), self.pointer.calls)
        log = (self.state / "logs" / "control.jsonl").read_text(encoding="utf-8")
        self.assertIn("remote_start", log)
        self.assertIn("remote_end", log)

    def test_stop_file_on_the_node_ends_the_session(self):
        from jarvis.control import stop
        session = self._open()
        self.assertIsNotNone(self.hub.remote.wait_frame(session["id"], 0, hold=5))
        stop(self.state)
        self.assertTrue(self._until(lambda: not remote_view._ACTIVE))
        # The node says so at once; the console doesn't wait out NODE_IDLE.
        self.assertTrue(self._until(lambda: self.hub.remote.get(session["id"])["state"] == "ended", 2))
        self.assertIn("stopped on this computer", self.hub.remote.get(session["id"])["error"])

    def test_nodes_cannot_start_remote_control(self):
        with self.assertRaisesRegex(RuntimeError, "Only the main's console"):
            self.client.request("/v1/control", {"node": "node", "kind": "remote", "payload": {
                "op": "start", "session": "a" * 32, "quality": "low"}})

    def test_node_without_desktop_cannot_touch_sessions(self):
        token = self.hub.broker.enroll("other", {"inventory"})
        sid = self.hub.remote.open("node")["id"]
        request = urllib.request.Request(self.main["cluster"]["main_url"] + "/v1/remote/input",
                                         data=json.dumps({"session": sid}).encode(), headers={
                                             "Authorization": f"Bearer {token}", "X-JARVIS-Main": "main",
                                             "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_remote_job_is_validated_and_not_a_desktop_action(self):
        with self.assertRaises(ValueError):
            validate_action("remote", {"op": "start", "session": "nothex", "quality": "low"})
        with self.assertRaises(ValueError):
            validate_action("remote", {"op": "start", "session": "a" * 32, "cmd": "x"})
        with self.assertRaises(ValueError):  # the brain's desktop tools can't name it
            validate_action("desktop", {"action": "remote_view", "arg": "{}"})
        with self.assertRaisesRegex(ValueError, "not the main"):
            self.hub.dispatch("main", "remote", {"op": "start", "session": "a" * 32})

    def test_console_api_over_http(self):
        handler = type("H", (Handler,), {"console": self.console, "hosts": ("127.0.0.1",)})
        server = ConsoleServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(path, body=None):
            request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                             headers={"X-JARVIS": "1", "Content-Type": "application/json"})
            return opener.open(request, timeout=10)

        status = json.loads(call("/api/remote").read())
        self.assertEqual([(d["id"], d["ready"]) for d in status["devices"]], [("node", True)])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: json.loads(call("/api/remote/start", {"device": "node", "quality": "low"}).read()))
            deadline = time.monotonic() + 5
            while not future.done() and time.monotonic() < deadline:
                self.worker.once()
            session = future.result(timeout=1)
        with call(f"/api/remote/frame?session={session['id']}&after=0") as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(response.headers["X-Frame-Width"], "1600")
            self.assertGreaterEqual(int(response.headers["X-Frame-Seq"]), 1)
            self.assertEqual(response.read(3), b"\xff\xd8\xff")
        self.assertTrue(json.loads(call("/api/remote/input", {"session": session["id"], "events": [
            {"t": "text", "text": "hi"}]}).read())["ok"])
        self.assertTrue(self._until(lambda: ("type", "hi") in self.pointer.calls))
        self.assertTrue(json.loads(call("/api/remote/stop", {"session": session["id"]}).read())["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            call(f"/api/remote/frame?session={session['id']}&after=0")
        self.assertEqual(caught.exception.code, 410)
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:  # no X-JARVIS header: refused
            opener.open(urllib.request.Request(base + "/api/remote/start", data=b"{}"), timeout=5)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_old_code_on_the_node_is_not_ready(self):
        with patch("jarvis.release.served_code", return_value="different"):
            status = self.console.remote_status()
        self.assertFalse(status["devices"][0]["ready"])
        self.assertIn("jarvis cluster update node", status["devices"][0]["why"])


class ReleaseReadinessTests(unittest.TestCase):
    """Release freshness and older-node errors in the console Remote view."""

    def setUp(self):
        EndToEndTests.setUp(self)

    def _release(self, with_remote):
        """A cut release in the main's state, with or without remote control in it."""
        folder = self.root / "main" / "code-release"
        (folder / "jarvis").mkdir(parents=True, exist_ok=True)
        (folder / "release.json").write_text(json.dumps({"code": "served"}), encoding="utf-8")
        target = folder / "jarvis" / "remote_view.py"
        if with_remote:
            target.write_text("# shipped", encoding="utf-8")
        else:
            target.unlink(missing_ok=True)

    def test_busy_node_is_not_presented_as_online_or_ready(self):
        self.hub.broker.nodes = lambda: [{"id": "node", "online": False,
            "seen": time.time() - 100, "capabilities": ["desktop"], "code": "served"}]
        with patch("jarvis.connections.busy_nodes", return_value={"node"}):
            device = self.console.remote_status()["devices"][0]
        self.assertEqual(device["presence"], "busy")
        self.assertFalse(device["online"])
        self.assertFalse(device["ready"])
        self.assertIn("awaiting contact", device["why"])

    def test_fenced_former_main_does_not_advertise_remote_ready(self):
        from jarvis.continuity.runtime import RuntimeRefusal
        from types import SimpleNamespace
        runtime = SimpleNamespace(require_authority=lambda _profile: (_ for _ in ()).throw(RuntimeRefusal("not_holder")))
        with (patch("jarvis.continuity.integration.continuity_state", return_value="managed"),
              patch("jarvis.continuity.integration.production_binding", return_value={"runtime": runtime})):
            status = self.console.remote_status()
        self.assertEqual(status["devices"], [])
        self.assertIsNone(status["session"])
        self.assertIn("MAIN", status["why"])

    def test_release_with_remote_control_is_ready_even_if_live_code_moved_on(self):
        self._release(True)
        with patch("jarvis.deploy.code_fingerprint", return_value="live"):
            self.hub.broker.nodes = lambda: [{"id": "node", "online": True, "seen": time.time(),
                                               "capabilities": ["desktop"], "code": "served"}]
            status = self.console.remote_status()
        self.assertTrue(status["devices"][0]["ready"], status["devices"][0]["why"])

    def test_live_code_ahead_of_release_is_not_ready(self):
        self._release(False)
        with (patch("jarvis.deploy.code_fingerprint", return_value="live"),
              patch("jarvis.release.served_code", return_value="served")):
            self.hub.broker.nodes = lambda: [{"id": "node", "online": True, "seen": time.time(),
                                               "capabilities": ["desktop"], "code": "served"}]
            status = self.console.remote_status()
        self.assertFalse(status["devices"][0]["ready"])
        self.assertIn("jarvis cluster release", status["devices"][0]["why"])
        self.assertEqual(status["release"], {"live": "live", "served": "served"})

    def test_failed_release_reports_test_failure(self):
        self._release(False)
        (self.root / "main" / "code-release-failed.json").write_text(
            json.dumps({"code": "live", "tests": "FAILED test_latest_release.py"}), encoding="utf-8")
        with (patch("jarvis.deploy.code_fingerprint", return_value="live"),
              patch("jarvis.release.served_code", return_value="served")):
            self.hub.broker.nodes = lambda: [{"id": "node", "online": True, "seen": time.time(),
                                               "capabilities": ["desktop"], "code": "served"}]
            status = self.console.remote_status()
        self.assertFalse(status["devices"][0]["ready"])
        self.assertIn("failed its release tests", status["devices"][0]["why"])
        self.assertIn("FAILED test_latest_release.py", status["devices"][0]["why"])

    def test_old_code_start_error_is_plainly_mapped(self):
        with patch.object(self.hub, "remote_open", side_effect=RuntimeError(
                "Nodes execute only desktop, inventory, or Grokbot operations, and file transfers.")):
            with self.assertRaisesRegex(ValueError, "jarvis cluster update node"):
                self.console.remote_start({"device": "node", "quality": "low"})


if __name__ == "__main__":
    unittest.main()


class WindowsHardeningTests(unittest.TestCase):
    """Windows startup and frame-loop hardening, exercised with platform/API fakes."""

    def test_run_refuses_windows_desktop_problem_before_replacing_sessions(self):
        message = "The screen is locked or a UAC prompt is showing."
        with (patch("jarvis.platforms.detect", return_value={"os": "windows"}),
              patch("jarvis.remote_view.desktop_problem", return_value=message),
              patch("jarvis.control._stopped", return_value=False),
              patch.object(remote_view, "end_all") as end_all):
            result = remote_view.run({"op": "start", "session": "a" * 32}, {}, tempfile.gettempdir())
        self.assertEqual(result, {"ok": False, "text": message})
        end_all.assert_not_called()

    def _session(self, capture, client, encoder=None):
        session = remote_view.NodeSession({}, tempfile.gettempdir(), "b" * 32, "low",
                                          client=client, injector=Injector(FakePointer(), FakeKeys()),
                                          capture=capture, encoder=encoder or (lambda image, quality: (b"\xff\xd8\xffx", 8, 8)))
        # These loop tests need no wall-clock delay; client/capture decide when to stop.
        session.stop_event.wait = lambda timeout=None: False
        return session

    def test_frames_recovers_from_transient_capture_errors(self):
        from PIL import Image
        attempts, requests = [], []

        def capture():
            attempts.append(None)
            if len(attempts) <= 3:
                raise OSError("temporarily unavailable")
            return Image.new("RGB", (8, 8)), {"x": 0, "y": 0, "w": 8, "h": 8}

        class Client:
            def request(self, path, body, timeout):
                requests.append(body)
                return {"stop": True, "why": "done"}

        session = self._session(capture, Client())
        session._frames()
        self.assertEqual(len(attempts), 4)
        self.assertEqual(len(requests), 1)

    def test_twenty_capture_failures_end_with_capture_error(self):
        session = self._session(lambda: (_ for _ in ()).throw(OSError("no pixels")), object())
        session._frames()
        self.assertTrue(session.stop_event.is_set())
        self.assertEqual(session.why, "Can't capture the screen: no pixels")

    def test_unchanged_screen_is_not_reencoded(self):
        from PIL import Image
        image = Image.new("RGB", (8, 8), "red")
        encodes, bodies = [], []

        def encoder(image, quality):
            encodes.append(1)
            return b"\xff\xd8\xffx", 8, 8

        class Client:
            def request(self, path, body, timeout):
                bodies.append(body)
                if len(bodies) >= 5:
                    session.stop_event.set()
                return {"stop": False}

        session = self._session(lambda: (image, {"x": 0, "y": 0, "w": 8, "h": 8}), Client(), encoder)
        with patch.object(remote_view, "KEEPALIVE", 0.001):
            session._frames()
        self.assertEqual(len(encodes), 1)
        self.assertTrue(all("jpeg" not in body for body in bodies[1:]))

    def test_osleft_alias_is_resolved_before_fake_keys(self):
        keys = FakeKeys()
        injector = Injector(FakePointer(), keys)
        injector.apply({"t": "key", "code": "OSLeft", "down": True})
        self.assertEqual(keys.calls, [("MetaLeft", True)])


class RemoteLanMouseTests(unittest.TestCase):
    def test_node_session_temporarily_stops_running_lan_mouse(self):
        from jarvis import sharedinput
        session = remote_view.NodeSession({}, tempfile.gettempdir(), "c" * 32, "low",
                                          client=object(), injector=Injector(FakePointer(), FakeKeys()))
        with (patch.object(sharedinput, "lan_mouse_status", return_value={"running": True}),
              patch.object(sharedinput, "stop", return_value={"stopped": True}) as stop,
              patch.object(sharedinput, "start", return_value={"started": True}) as start):
            session._suspend_lan_mouse()
            self.assertTrue(session._lan_mouse_was_running)
            stop.assert_called_once_with(sharing_only=True)
            session._restore_lan_mouse()
            start.assert_called_once_with()
            self.assertFalse(session._lan_mouse_was_running)
