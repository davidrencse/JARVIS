"""Remote control (remote_view.py): lifecycle races, Lan Mouse handoff on the node, the hub's
session rules, coordinate and key mapping, and input validation.

Everything is a fake: no real Lan Mouse, screen, mouse or keyboard is touched (sharedinput's
start/stop/status are mocks), and no node is contacted."""

import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import control, remote_view, sharedinput
from jarvis.cluster import Hub
from jarvis.remote_view import (KEYS, MAIN_IDLE, Ended, Grabber, Injector, NodeSession, Sessions,
                                clean_event, failure_message, hyprland_area)

WEB = Path(__file__).resolve().parents[1] / "jarvis" / "web" / "remote.js"
JPEG = b"\xff\xd8\xff" + b"x"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Pointer:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def move(self, x, y):
        if self.fail == "move":
            raise OSError("SetCursorPos failed")
        self.calls.append(("move", x, y))

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


class NodeClient:
    """The main, as the node's session loops see it. gate: input polls block until it's set."""

    def __init__(self, gate=None):
        self.gate, self.ends = gate, []

    def request(self, path, body, timeout=None):
        if body.get("end") is not None:
            self.ends.append(body["end"])
            return {"stop": True}
        if path == "/v1/remote/input":
            if self.gate is not None:
                self.gate.wait(10)
            time.sleep(0.02)
            return {"events": [], "stop": False}
        return {"stop": False}


def tiny():
    from PIL import Image
    return Image.new("RGB", (8, 8)), {"x": 0, "y": 0, "w": 8, "h": 8}


def marker(state):
    try:
        return json.loads((Path(state) / remote_view.MARKER).read_text(encoding="utf-8")).get("session")
    except FileNotFoundError:
        return None


class NodeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        remote_view._ACTIVE.clear()
        remote_view._LAN.update(owed=False, starting=0)
        self.addCleanup(lambda: [s.end("test over") for s in list(remote_view._ACTIVE.values())])
        self.addCleanup(remote_view._ACTIVE.clear)
        self.addCleanup(lambda: remote_view._LAN.update(owed=False, starting=0))
        self.lan = {"running": True}
        self.events = []  # ("stop"|"start", which sessions were active)

        def status():
            return {"running": self.lan["running"]}

        def stop(sharing_only=False):
            self.lan["running"] = False
            self.events.append(("stop", sorted(remote_view._ACTIVE)))
            return {"stopped": True}

        def start():
            self.lan["running"] = True
            self.events.append(("start", sorted(remote_view._ACTIVE)))
            return {"started": True}
        for p in (patch.object(sharedinput, "lan_mouse_status", side_effect=status),
                  patch.object(sharedinput, "stop", side_effect=stop),
                  patch.object(sharedinput, "start", side_effect=start),
                  patch.dict(os.environ, {"JARVIS_NO_MODELS": "0"}),  # let start() reach the mocks
                  patch("jarvis.control._stopped", return_value=False),
                  patch("jarvis.platforms.detect", return_value={"os": "linux", "desktop": "hyprland"})):
            p.start()
            self.addCleanup(p.stop)

    def session(self, sid, client=None, pointer=None):
        return NodeSession({}, self.state, sid, "low", client=client or NodeClient(),
                           injector=Injector(pointer or Pointer(), Keys()), capture=tiny)

    def run_job(self, sid, client=None):
        return remote_view.run({"op": "start", "session": sid, "quality": "low"}, {}, self.state,
                               client=client or NodeClient(),
                               injector=Injector(Pointer(), Keys()), capture=tiny)

    def done(self, session):
        """Every thread of the session has finished (_finish ran to the end)."""
        return self.until(lambda: session.threads and not any(t.is_alive() for t in session.threads))

    def until(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return False


class LanMouseHandoffTests(NodeBase):
    """H1: a replaced session finishing late must not take the marker or Lan Mouse back."""

    def test_late_finish_of_a_replaced_session_leaves_the_new_one_alone(self):
        a, b = self.session("a" * 32), self.session("b" * 32)
        a._lan_mouse_was_running = True  # A had stopped the node's Lan Mouse
        remote_view._ACTIVE.update({a.sid: a, b.sid: b})
        b._mark()
        a._finish()  # A's last thread finishes after B started (its join timed out)
        self.assertEqual(marker(self.state), "b" * 8)
        self.assertEqual(self.events, [])  # Lan Mouse stayed off while B drives
        self.assertTrue(b._lan_mouse_was_running)  # B inherited the restart
        b._finish()
        self.assertEqual(self.events, [("start", [])])
        self.assertIsNone(marker(self.state))

    def test_replace_then_end_restores_lan_mouse_exactly_once(self):
        self.assertEqual(self.run_job("a" * 32)["ok"], True)
        self.assertEqual(self.events, [("stop", ["a" * 32])])
        a = remote_view._ACTIVE["a" * 32]
        self.assertEqual(self.run_job("b" * 32)["ok"], True)  # A ends inside run()
        self.assertTrue(self.done(a))
        self.assertEqual(marker(self.state), "b" * 8)
        self.assertFalse(self.lan["running"])
        self.assertEqual([e for e, _ in self.events], ["stop"])
        b = remote_view._ACTIVE["b" * 32]
        b.end("Disconnected.")
        self.assertTrue(self.done(b))
        self.assertEqual([e for e, _ in self.events], ["stop", "start"])
        self.assertIsNone(marker(self.state))

    def test_replace_when_the_old_session_is_stuck_hands_over_too(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.run_job("a" * 32, client=NodeClient(gate))
        self.assertTrue(self.until(lambda: self.events == [("stop", ["a" * 32])]))
        with patch.object(NodeSession, "join", lambda self, timeout=None: None):  # join "timed out"
            self.run_job("b" * 32)
        old, new = remote_view._ACTIVE["a" * 32], remote_view._ACTIVE["b" * 32]
        self.assertFalse(old._lan_mouse_was_running)
        self.assertTrue(new._lan_mouse_was_running)
        gate.set()  # the stuck poll returns: A finishes now, while B runs
        self.assertTrue(self.done(old))
        self.assertEqual(marker(self.state), "b" * 8)
        self.assertEqual([e for e, _ in self.events], ["stop"])
        new.end("Disconnected.")
        self.assertTrue(self.done(new))
        self.assertEqual([e for e, _ in self.events], ["stop", "start"])

    def test_a_failed_replacement_still_brings_lan_mouse_back(self):
        self.run_job("a" * 32)
        a = remote_view._ACTIVE["a" * 32]
        with patch.object(remote_view, "NodeSession", side_effect=LookupError("no desktop here")):
            result = self.run_job("b" * 32)
        self.assertFalse(result["ok"])
        self.assertTrue(self.done(a))
        self.assertEqual([e for e, _ in self.events], ["stop", "start"])
        self.assertFalse(remote_view._LAN["owed"])

    def test_lan_mouse_that_was_off_stays_off(self):
        self.lan["running"] = False
        self.run_job("a" * 32)
        self.run_job("b" * 32)
        b = remote_view._ACTIVE["b" * 32]
        b.end("Disconnected.")
        self.assertTrue(self.done(b))
        self.assertEqual(self.events, [])


    def test_heir_marker_records_the_inherited_restart(self):
        # LC-2: the heir's marker must say lan_mouse:true at once, so a crash before its next
        # press still has recover() bring Lan Mouse back.
        a, b = self.session("a" * 32), self.session("b" * 32)
        a._lan_mouse_was_running = True
        self.lan["running"] = False  # A had stopped it
        remote_view._ACTIVE.update({a.sid: a, b.sid: b})
        b._mark()
        a._finish()
        data = json.loads((self.state / remote_view.MARKER).read_text(encoding="utf-8"))
        self.assertEqual((data["session"], data["lan_mouse"]), ("b" * 8, True))
        remote_view._ACTIVE.clear()  # B's process "dies": the next start recovers
        self.assertTrue(remote_view.recover(self.state, injector=Injector(Pointer(), Keys())))
        self.assertEqual([e for e, _ in self.events], ["start"])
        self.assertIsNone(marker(self.state))


class MarkerTests(NodeBase):
    def test_finish_removes_only_its_own_marker(self):
        a = self.session("a" * 32)
        a._mark()
        self.assertEqual(marker(self.state), "a" * 8)
        (self.state / remote_view.MARKER).write_text(json.dumps({"session": "b" * 8}), encoding="utf-8")
        a._finish()
        self.assertEqual(marker(self.state), "b" * 8)
        b = self.session("b" * 32)
        b._finish()
        self.assertIsNone(marker(self.state))

    def test_an_unreadable_marker_goes_only_when_no_other_session_runs(self):
        a, b = self.session("a" * 32), self.session("b" * 32)
        remote_view._ACTIVE[b.sid] = b
        path = self.state / remote_view.MARKER
        path.write_text("{not json", encoding="utf-8")
        a._finish()
        self.assertTrue(path.exists())
        b._finish()
        self.assertFalse(path.exists())

    def test_an_ending_session_never_overwrites_a_newer_ones_marker(self):
        a, b = self.session("a" * 32), self.session("b" * 32)
        remote_view._ACTIVE.update({a.sid: a, b.sid: b})
        b._mark()
        a.stop_event.set()  # A is ending; its late release writes what it holds
        a._mark(["ShiftLeft"], [])
        self.assertEqual(marker(self.state), "b" * 8)
        a.stop_event.clear()  # a live session still writes its own (e.g. before B started)
        remote_view._ACTIVE.pop(b.sid)
        a._mark(["ShiftLeft"], [])
        self.assertEqual(marker(self.state), "a" * 8)

    def test_a_refused_key_up_at_the_end_keeps_the_marker_and_is_retried(self):
        # W1: the desktop refuses the up (locked screen / secure desktop) as the session ends.
        class RefuseUp(Keys):
            refuse = False

            def key(self, code, down):
                if not down and self.refuse:
                    raise OSError("SendInput returned 0")
                super().key(code, down)
        keys, pointer = RefuseUp(), Pointer()
        s = NodeSession({}, self.state, "a" * 32, "low", client=NodeClient(),
                        injector=Injector(pointer, keys), capture=tiny)
        remote_view._ACTIVE[s.sid] = s
        s.start()
        s.injector.apply({"t": "key", "code": "KeyA", "down": True})
        keys.refuse = True
        s.end("Disconnected.")
        self.assertTrue(self.done(s))
        self.assertEqual(s.injector.held, {"KeyA"})
        data = json.loads((self.state / remote_view.MARKER).read_text(encoding="utf-8"))
        self.assertEqual((data["session"], data["held"], data["lan_mouse"]), ("a" * 8, ["KeyA"], False))
        self.assertEqual([e for e, _ in self.events], ["stop", "start"])  # Lan Mouse back once
        # The process dies now: the next start's recover() lets go of KeyA from the marker.
        fresh = Keys()
        self.assertTrue(remote_view.recover(self.state, injector=Injector(Pointer(), fresh)))
        self.assertIn(("KeyA", False), fresh.calls)
        self.assertIsNone(marker(self.state))
        self.assertEqual([e for e, _ in self.events], ["stop", "start"])  # not twice
        # Or it lives on: the late retry lets go once the desktop accepts, sending only ups.
        sent = len(keys.calls)
        keys.refuse = False
        s.release_thread.join(10)
        self.assertFalse(s.release_thread.is_alive())
        self.assertFalse(s.injector.busy())
        self.assertEqual(keys.calls[sent:], [("KeyA", False)])
        self.assertTrue(all(c[0] == "release" or c[2] == "up" for c in pointer.calls if c[0] != "move"))
        self.assertIsNone(marker(self.state))
        with self.assertRaises(remote_view.Stopped):
            s.injector.apply({"t": "key", "code": "KeyB", "down": True})

    def _ended_with_key_a_refused(self):
        class RefuseUp(Keys):
            refuse = False

            def key(self, code, down):
                if not down and self.refuse:
                    raise OSError("SendInput returned 0")
                super().key(code, down)
        keys, pointer = RefuseUp(), Pointer()
        a = NodeSession({}, self.state, "a" * 32, "low", client=NodeClient(),
                        injector=Injector(pointer, keys), capture=tiny)
        remote_view._ACTIVE[a.sid] = a
        a.start()
        a.injector.apply({"t": "key", "code": "KeyA", "down": True})
        keys.refuse = True
        a.end("Disconnected.")
        self.assertTrue(self.done(a))
        self.assertTrue(a.release_thread.is_alive())
        return a, keys, pointer

    def test_a_newer_session_takes_over_a_refused_up_and_the_old_retry_goes_quiet(self):
        # W1: A's late retry must not send anything into B's desktop (no backend release,
        # which lifts B's drag and modifiers) and B's clean end must not drop A's stuck key.
        a, a_keys, a_pointer = self._ended_with_key_a_refused()
        a_sent, a_moves = len(a_keys.calls), len(a_pointer.calls)
        b_keys, b_pointer = Keys(), Pointer()
        b = NodeSession({}, self.state, "b" * 32, "low", client=NodeClient(),
                        injector=Injector(b_pointer, b_keys), capture=tiny)
        remote_view._ACTIVE[b.sid] = b
        b.start()
        b.injector.apply({"t": "down", "b": "left", "x": .5, "y": .5})
        a.release_thread.join(3)
        self.assertFalse(a.release_thread.is_alive())
        time.sleep(1.2)  # past A's first retry: still nothing from A
        self.assertEqual(a_keys.calls[a_sent:], [])
        self.assertEqual(a_pointer.calls[a_moves:], [])
        self.assertNotIn(("release",), b_pointer.calls)
        self.assertIn("KeyA", b.injector.held)
        data = json.loads((self.state / remote_view.MARKER).read_text(encoding="utf-8"))
        self.assertEqual((data["session"], data["held"], data["buttons"]), ("b" * 8, ["KeyA"], ["left"]))
        b.end("Disconnected.")
        self.assertTrue(self.done(b))
        self.assertEqual(b_keys.calls.count(("KeyA", False)), 1)
        self.assertIn(("button", "left", "up"), b_pointer.calls)
        self.assertIsNone(marker(self.state))
        self.assertIsNone(b.release_thread)
        kinds = [e for e, _ in self.events]
        self.assertEqual(kinds, ["stop", "start", "stop", "start"])  # back once after each
        self.assertTrue(self.lan["running"])

    def test_the_late_retry_never_sends_the_backend_release(self):
        a, keys, pointer = self._ended_with_key_a_refused()
        before = pointer.calls.count(("release",))
        time.sleep(2.0)  # 0.5 s, then 1 s: two retries
        self.assertEqual(pointer.calls.count(("release",)), before)
        keys.refuse = False
        a.release_thread.join(5)
        self.assertFalse(a.release_thread.is_alive())
        self.assertEqual(pointer.calls.count(("release",)), before)
        self.assertEqual(keys.calls[-1], ("KeyA", False))
        self.assertIsNone(marker(self.state))

    def test_the_late_retry_stops_on_the_stop_file_or_its_bound_and_keeps_the_marker(self):
        for how in ("stop_file", "bound"):
            with self.subTest(how):
                remote_view._ACTIVE.clear()
                self.events.clear()
                with patch.object(NodeSession, "LATE_RELEASE_FOR", 0.3 if how == "bound" else 600.0):
                    a, keys, pointer = self._ended_with_key_a_refused()
                    before = pointer.calls.count(("release",))  # the ending's own, from end()
                    if how == "stop_file":
                        stop = patch("jarvis.control._stopped", return_value=True)
                        stop.start()
                        self.addCleanup(stop.stop)
                    a.release_thread.join(3)
                    if how == "stop_file":
                        stop.stop()
                self.assertFalse(a.release_thread.is_alive())
                data = json.loads((self.state / remote_view.MARKER).read_text(encoding="utf-8"))
                self.assertEqual((data["session"], data["held"]), ("a" * 8, ["KeyA"]))
                self.assertEqual(pointer.calls.count(("release",)), before)
                # The next session here takes the list over and lets go of it at its end.
                b_keys = Keys()
                b = NodeSession({}, self.state, "b" * 32, "low", client=NodeClient(),
                                injector=Injector(Pointer(), b_keys), capture=tiny)
                remote_view._ACTIVE[b.sid] = b
                b.start()
                self.assertEqual(json.loads((self.state / remote_view.MARKER).read_text())["held"], ["KeyA"])
                b.end("Disconnected.")
                self.assertTrue(self.done(b))
                self.assertIn(("KeyA", False), b_keys.calls)
                self.assertIsNone(marker(self.state))

    def test_every_ending_lets_go_of_what_was_held(self):
        for how in ("end", "stop_job", "crash"):
            with self.subTest(how):
                pointer, keys = Pointer(), Keys()
                sid = {"end": "c", "stop_job": "d", "crash": "e"}[how] * 32
                client = NodeClient()
                if how == "crash":
                    def boom():
                        raise RuntimeError("capture exploded")
                    session = NodeSession({}, self.state, sid, "low", client=client,
                                          injector=Injector(pointer, keys), capture=boom)
                else:
                    session = NodeSession({}, self.state, sid, "low", client=client,
                                          injector=Injector(pointer, keys), capture=tiny)
                session.injector.apply({"t": "down", "b": "left"})
                session.injector.apply({"t": "key", "code": "ControlLeft", "down": True})
                remote_view._ACTIVE[sid] = session
                session.start()
                if how == "end":
                    session.end("Disconnected.")
                elif how == "stop_job":
                    remote_view.run({"op": "stop", "session": sid}, {}, self.state)
                self.assertTrue(self.done(session))
                self.assertIn(("ControlLeft", False), keys.calls)
                self.assertIn(("button", "left", "up"), pointer.calls)
                self.assertFalse(session.injector.busy())
                with self.assertRaises(remote_view.Stopped):  # nothing presses after the end
                    session.injector.apply({"t": "key", "code": "KeyA", "down": True})


class InputFailureTests(NodeBase):
    def test_failure_message_names_what_failed(self):
        with patch.object(remote_view, "desktop_problem", return_value=None):
            self.assertEqual(failure_message({"t": "move"}, OSError("no")), "Couldn't move the pointer here: no")
            self.assertEqual(failure_message({"t": "key"}, OSError("no")), "Couldn't press keys here: no")
            self.assertEqual(failure_message({"t": "down"}, OSError("no")), "Couldn't click here: no")
            self.assertEqual(failure_message(None, OSError("no")), "Couldn't press keys here: no")
        with patch.object(remote_view, "desktop_problem", return_value="The screen is locked or a UAC prompt is showing."):
            self.assertIn("UAC prompt", failure_message({"t": "move"}, OSError("no")))
        self.assertLessEqual(len(failure_message({"t": "text"}, OSError("x" * 900))), 300)

    def test_five_failed_moves_end_with_a_pointer_message(self):
        moves = [[n, {"t": "move", "x": 0.5, "y": 0.5}] for n in range(1, 8)]

        class Client(NodeClient):
            def request(self, path, body, timeout=None):
                return {"events": moves, "stop": False}
        session = self.session("f" * 32, client=Client(), pointer=Pointer(fail="move"))
        session.injector.area = {"x": 0, "y": 0, "w": 100, "h": 100}
        with patch.object(remote_view, "desktop_problem", return_value=None):
            session._input()
        self.assertTrue(session.why.startswith("Couldn't move the pointer here"), session.why)


class HubSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = Sessions(self.clock)

    def test_fast_frames_are_kept_so_the_console_shows_the_newest(self):  # M1
        sid = self.s.open("n", "medium")["id"]
        self.s.frame("n", sid, JPEG + b"1", 10, 10)
        self.clock.now += 0.03
        self.assertEqual(self.s.frame("n", sid, JPEG + b"2", 10, 10), {"stop": False})
        seq, data, _, _ = self.s.wait_frame(sid, 0, hold=0)
        self.assertEqual((seq, data), (2, JPEG + b"2"))

    def test_a_pending_start_is_not_ended_for_console_silence(self):  # L6
        sid = self.s.open("n", "low", pending=True)["id"]
        self.clock.now += MAIN_IDLE + 3  # dispatch + _await_poll outlived MAIN_IDLE
        self.assertEqual(self.s.get(sid)["state"], "starting")
        self.s.touch(sid)  # the start request came back: from now on console silence counts
        self.s.frame("n", sid, JPEG, 10, 10)
        self.clock.now += 10
        self.s.frame("n", sid, None, None, None)  # node keep-alive; the console says nothing
        self.assertEqual(self.s.get(sid)["state"], "live")
        self.clock.now += MAIN_IDLE - 9
        self.assertEqual(self.s.get(sid)["error"], "The console stopped watching, so the session ended.")

    def test_a_pending_start_still_ends_if_the_node_never_sends_its_screen(self):
        sid = self.s.open("n", "low", pending=True)["id"]
        self.clock.now += remote_view.START_GRACE + 1
        self.assertEqual(self.s.get(sid)["error"], "n never sent its screen.")

    def test_console_hidden_timer_beats_the_hub_idle_timeout(self):  # M3
        hidden = int(re.search(r"HIDDEN_STOP_MS = (\d+)", WEB.read_text(encoding="utf-8")).group(1))
        self.assertLess(hidden + 2500, MAIN_IDLE * 1000)  # + a frame poll's hold before abort

    def test_replacing_ends_the_old_session_for_the_console_and_the_node(self):
        old = self.s.open("n", "low")["id"]
        self.s.push(old, [{"t": "key", "code": "KeyA", "down": True}])
        new = self.s.open("n", "low")["id"]
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(Ended, "Replaced"):
            self.s.push(old, [])
        self.assertEqual(self.s.take_input("n", old, hold=0)["stop"], True)
        self.assertEqual(self.s.frame("n", old, JPEG, 10, 10)["stop"], True)
        self.assertEqual(self.s.current()["id"], new)

    def test_sessions_on_two_nodes_are_independent(self):
        a = self.s.open("a", "low")["id"]
        b = self.s.open("b", "low")["id"]
        self.assertEqual(self.s.get(a)["state"], "starting")
        self.s.close(b)
        self.assertEqual(self.s.get(a)["state"], "starting")

    def test_unknown_and_malformed_session_ids(self):
        for bad in (None, 5, "", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "../" + "a" * 29):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.s.push(bad, [])
        with self.assertRaises(LookupError):  # the 404 the console reads as "main restarted"
            self.s.wait_frame("f" * 32, 0, hold=0)
        with self.assertRaises(LookupError):
            self.s.node_ago("f" * 32)

    def test_an_ended_session_is_forgotten_after_keep_ended(self):
        sid = self.s.open("n", "low")["id"]
        self.s.close(sid)
        with self.assertRaises(Ended):
            self.s.wait_frame(sid, 0, hold=0)
        self.clock.now += remote_view.KEEP_ENDED + 1
        self.assertIsNone(self.s.current())
        with self.assertRaises(LookupError):
            self.s.wait_frame(sid, 0, hold=0)

    def test_sessions_end_after_max_session(self):
        sid = self.s.open("n", "low")["id"]
        for _ in range(int(remote_view.MAX_SESSION // 10) + 2):
            self.clock.now += 10
            if self.s.frame("n", sid, JPEG, 10, 10)["stop"]:
                break
            self.s.push(sid, [])
        self.assertIn("4 hours", self.s.get(sid)["error"])

    def test_frames_are_validated(self):
        sid = self.s.open("n", "low")["id"]
        for jpeg, w, h in ((b"\x89PNG", 10, 10), (JPEG, 0, 10), (JPEG, 10, 20001), (JPEG, True, 10),
                           (JPEG, 1.5, 10), (JPEG, "10", 10),
                           (JPEG + b"x" * remote_view.MAX_FRAME, 10, 10)):
            with self.subTest(jpeg=jpeg[:4], w=w, h=h), self.assertRaises(ValueError):
                self.s.frame("n", sid, jpeg, w, h)
        self.assertEqual(self.s.get(sid)["frames"], 0)
        self.assertEqual(self.s.frame("n", sid, None, None, None), {"stop": False})  # keep-alive

    def test_only_the_sessions_node_can_end_it(self):
        sid = self.s.open("n", "low")["id"]
        with self.assertRaises(PermissionError):
            self.s.node_end("other", sid, "bye")
        self.assertEqual(self.s.node_end("n", sid, "x" * 999), {"stop": True})
        self.assertEqual(len(self.s.get(sid)["error"]), 300)

    def test_close_all_stops_every_node(self):
        a, b = self.s.open("a", "low")["id"], self.s.open("b", "low")["id"]
        self.s.close_all("JARVIS on the main stopped.")
        self.assertTrue(self.s.take_input("a", a, hold=0)["stop"])
        self.assertEqual(self.s.take_input("b", b, hold=0)["why"], "JARVIS on the main stopped.")

    def test_take_input_rejects_bad_after(self):
        sid = self.s.open("n", "low")["id"]
        for bad in (-1, True, "3", 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.s.take_input("n", sid, bad, hold=0)

    def test_busy_callback_follows_live_sessions(self):
        seen = []
        s = Sessions(self.clock, on_busy=seen.append)
        sid = s.open("n", "low")["id"]
        self.assertEqual(seen[-1], True)
        s.close(sid)
        self.assertEqual(seen[-1], False)
        s = Sessions(self.clock, on_busy=lambda busy: 1 / 0)  # a broken callback never breaks a session
        s.close(s.open("n", "low")["id"])


class HubOpenTests(unittest.TestCase):
    """Hub.remote_open with a fake dispatch: no node is contacted."""

    def hub(self, dispatch):
        clock = Clock()
        fake = SimpleNamespace(main="main", remote=Sessions(clock), clock=clock,
                               config={"devices": {"main": {}, "dell": {"name": "dell"}}})
        fake.dispatch = lambda node, kind, payload, timeout: dispatch(fake, payload)
        return fake

    def test_a_dispatch_timeout_says_plainly_to_check_and_connect_again(self):
        def dispatch(fake, payload):
            raise TimeoutError("The remote action on Dell has no confirmed result. It may still be "
                               "running, so don't repeat it (job 123).")
        fake = self.hub(dispatch)
        with self.assertRaises(TimeoutError) as caught:
            Hub.remote_open(fake, "dell", "low")
        self.assertEqual(str(caught.exception),
                         "Dell didn't answer within 20 s. Check it's awake, then press Connect again.")
        self.assertNotIn("job", str(caught.exception))
        self.assertEqual(fake.remote.current()["error"], str(caught.exception))

    def test_a_slow_start_is_still_starting_when_it_returns(self):
        def dispatch(fake, payload):
            fake.clock.now += MAIN_IDLE + 3
            return {"ok": True}
        fake = self.hub(dispatch)
        session = Hub.remote_open(fake, "dell", "low")
        self.assertEqual(session["state"], "starting")
        self.assertIsNone(session["error"])

    def test_a_refused_start_closes_the_session_with_the_nodes_reason(self):
        fake = self.hub(lambda fake, payload: {"ok": False, "text": "Desktop control is stopped."})
        with self.assertRaisesRegex(RuntimeError, "Desktop control is stopped"):
            Hub.remote_open(fake, "dell", "low")
        self.assertEqual(fake.remote.current()["state"], "ended")

    def test_only_other_known_computers(self):
        fake = self.hub(lambda fake, payload: {"ok": True})
        for node in ("main", "nobody"):
            with self.subTest(node=node), self.assertRaises(ValueError):
                Hub.remote_open(fake, node, "low")
        with self.assertRaises(ValueError):
            Hub.remote_open(fake, "dell", "ultra")

    def test_console_maps_the_timeout_to_a_400_with_the_same_text(self):
        from jarvis.ui import Console
        hub = SimpleNamespace(remote_open=lambda device, quality: (_ for _ in ()).throw(
            TimeoutError("Dell didn't answer within 20 s. Check it's awake, then press Connect again.")))
        console = SimpleNamespace(_hub=lambda: hub)
        with self.assertRaisesRegex(ValueError, "didn't answer within 20 s"):
            Console.remote_start(console, {"device": "dell", "quality": "low"})


class EventValidationTests(unittest.TestCase):
    def test_good_events(self):
        cases = [
            ({"t": "move", "x": 0, "y": 1}, {"t": "move", "x": 0.0, "y": 1.0}),
            ({"t": "move", "x": 1.04, "y": -0.04}, {"t": "move", "x": 1.0, "y": 0.0}),  # past the edge: clamped
            ({"t": "down", "b": "middle"}, {"t": "down", "b": "middle"}),
            ({"t": "up", "b": "right", "x": 0.5, "y": 0.25}, {"t": "up", "b": "right", "x": 0.5, "y": 0.25}),
            ({"t": "wheel", "dy": -20}, {"t": "wheel", "dy": -20}),
            ({"t": "wheel", "dy": 20}, {"t": "wheel", "dy": 20}),
            ({"t": "key", "code": "AudioVolumeUp", "down": False}, {"t": "key", "code": "AudioVolumeUp", "down": False}),
            ({"t": "text", "text": "é" * 1000}, {"t": "text", "text": "é" * 1000}),
            ({"t": "release", "junk": 1}, {"t": "release"}),
        ]
        for event, want in cases:
            with self.subTest(event=event):
                self.assertEqual(clean_event(event), want)

    def test_bad_events(self):
        bad = [None, [], {"t": None}, {"t": "move", "x": float("nan"), "y": 0},
               {"t": "move", "x": float("inf"), "y": 0}, {"t": "move", "x": 10 ** 400, "y": 0},
               {"t": "move", "x": "0.5", "y": 0}, {"t": "move", "x": 0.5}, {"t": "move", "x": -0.06, "y": 0},
               {"t": "down", "b": "left", "x": 0.5}, {"t": "down", "b": "back"}, {"t": "up"},
               {"t": "wheel", "dy": 21}, {"t": "wheel", "dy": -21}, {"t": "wheel", "dy": 1.0},
               {"t": "wheel", "dy": True}, {"t": "wheel"},
               {"t": "key", "code": "K" * 25, "down": True}, {"t": "key", "code": "Key-A", "down": True},
               {"t": "key", "code": "KeyA", "down": 1}, {"t": "key", "code": "", "down": True},
               {"t": "text", "text": "x" * 1001}, {"t": "text", "text": 5}]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ValueError):
                clean_event(event)

    def test_push_drops_bad_events_alone_and_checks_the_batch(self):
        s = Sessions(Clock())
        sid = s.open("n", "low")["id"]
        self.assertEqual(s.push(sid, [{"t": "key", "code": "KeyA", "down": True}, {"t": "nope"},
                                      {"t": "key", "code": "KeyA", "down": False}]), 2)
        for bad in ({"t": "move"}, "x", None, [{}] * (remote_view.MAX_EVENTS + 1)):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(ValueError):
                s.push(sid, bad)

    def test_node_job_validation(self):
        good = {"op": "start", "session": "a" * 32, "quality": "high"}
        remote_view.validate(good)
        for bad in ({**good, "op": "run"}, {**good, "session": "x"}, {**good, "quality": "4k"},
                    {**good, "extra": 1}, "start", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                remote_view.validate(bad)
        for bad in ({"op": "start"}, {"op": "start", "session": "a" * 32, "quality": "4k"}, None):
            with self.subTest(run=bad), self.assertRaises(ValueError):
                remote_view.run(bad, {}, tempfile.gettempdir())


class CoordinateTests(unittest.TestCase):
    def test_fractions_map_onto_the_real_screen_and_stay_inside_it(self):
        pointer = Pointer()
        inj = Injector(pointer, Keys())
        inj.area = {"x": -1920, "y": -200, "w": 1920, "h": 1080}  # a monitor left of and above the main
        for x, y, want in ((0, 0, (-1920, -200)), (0.5, 0.5, (-960, 340)), (1, 1, (-1, 879))):  # the last whole pixel (INPUT-1)
            pointer.calls.clear()
            inj.apply({"t": "move", "x": x, "y": y})
            got = pointer.calls[0][1:]
            self.assertAlmostEqual(got[0], want[0], places=3)
            self.assertAlmostEqual(got[1], want[1], places=3)
            self.assertLess(got[0], 0)  # 1.0 is the last pixel, never the next monitor's first

    def test_a_click_without_a_point_presses_where_the_pointer_is(self):
        pointer = Pointer()
        inj = Injector(pointer, Keys())
        inj.area = {"x": 0, "y": 0, "w": 100, "h": 100}
        inj.apply({"t": "up", "b": "left"})
        self.assertEqual(pointer.calls, [("button", "left", "up")])

    def test_wheel_direction_and_amount(self):
        pointer = Pointer()
        inj = Injector(pointer, Keys())
        inj.apply({"t": "wheel", "dy": 3})
        inj.apply({"t": "wheel", "dy": -20})
        self.assertEqual(pointer.calls, [("scroll", "down", 3), ("scroll", "up", 20)])

    def test_hyprland_area_uses_logical_size_and_rotation(self):
        mon = {"x": 1920, "y": 0, "width": 2880, "height": 1800, "scale": 1.5}
        self.assertEqual(hyprland_area(mon), {"x": 1920, "y": 0, "w": 1920.0, "h": 1200.0})
        for transform, swapped in ((0, False), (1, True), (2, False), (3, True), (4, False), (5, True), (7, True)):
            with self.subTest(transform=transform):
                area = hyprland_area({**mon, "transform": transform})
                self.assertEqual((area["w"], area["h"]), (1200.0, 1920.0) if swapped else (1920.0, 1200.0))
        self.assertEqual(hyprland_area({"x": 0, "y": 0, "width": 800, "height": 600, "scale": 0})["w"], 800)


class FakeHyprland(control.Hyprland):
    def __init__(self):
        self.focused, self.runs, self.fail = "DP-1", [], False

    def _monitor(self):
        self.runs.append("monitors")
        return {"name": self.focused, "x": 0 if self.focused == "DP-1" else 1920, "y": 0,
                "width": 64, "height": 32, "scale": 1, "transform": 0}

    def _run(self, *args, data=None):
        self.runs.append(args[0])
        if self.fail:
            raise OSError("grim: output gone")
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.new("RGB", (64, 32)).save(out, "PPM")
        return SimpleNamespace(stdout=out.getvalue())


class GrabberTests(unittest.TestCase):
    def test_the_monitor_is_picked_once_per_session(self):  # L7
        backend = FakeHyprland()
        grab = Grabber(detect=lambda: {"os": "linux"}, backend=backend)
        _, first = grab()
        backend.focused = "HDMI-A-1"  # David focuses the other monitor mid-session
        image, second = grab()
        self.assertEqual(first, second)
        self.assertEqual(image.size, (64, 32))
        self.assertEqual(backend.runs.count("monitors"), 1)

    def test_a_failed_capture_picks_the_monitor_again(self):
        backend = FakeHyprland()
        grab = Grabber(detect=lambda: {"os": "linux"}, backend=backend)
        grab()
        backend.fail, backend.focused = True, "HDMI-A-1"
        with self.assertRaises(OSError):
            grab()
        backend.fail = False
        self.assertEqual(grab()[1]["x"], 1920)

    def test_other_backends_use_their_own_shot(self):
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.new("RGB", (4, 4)).save(out, "PNG")
        backend = SimpleNamespace(shot=lambda: (out.getvalue(), {"x": 0, "y": 0, "w": 4, "h": 4}))
        image, area = Grabber(detect=lambda: {"os": "linux"}, backend=backend)()
        self.assertEqual((image.size, area["w"]), ((4, 4), 4))

    def test_windows_uses_the_full_desktop_grab(self):
        with patch.object(remote_view, "grab", return_value=("img", {"x": 0})) as grab:
            self.assertEqual(Grabber(detect=lambda: {"os": "windows"})(), ("img", {"x": 0}))
        grab.assert_called_once_with()


class KeyMappingTests(unittest.TestCase):
    def test_linux_codes_match_evdev(self):
        for code, ev in (("KeyA", 30), ("KeyZ", 44), ("KeyQ", 16), ("KeyM", 50), ("Digit1", 2), ("Digit0", 11),
                         ("F1", 59), ("F10", 68), ("F11", 87), ("F12", 88), ("Enter", 28), ("Space", 57),
                         ("ShiftLeft", 42), ("ControlRight", 97), ("MetaLeft", 125), ("ArrowUp", 103),
                         ("Delete", 111), ("Numpad0", 82), ("Numpad9", 73), ("NumpadEnter", 96)):
            with self.subTest(code=code):
                self.assertEqual(KEYS[code][2], ev)

    def test_windows_extended_flags(self):
        extended = {"ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown",
                    "Insert", "Delete", "ControlRight", "AltRight", "MetaLeft", "MetaRight", "NumpadEnter",
                    "NumpadDivide", "ContextMenu", "PrintScreen", "NumLock"}
        for code, (_, ext, _) in KEYS.items():
            with self.subTest(code=code):
                self.assertEqual(ext, code in extended)

    def test_windows_key_presses_with_scan_codes_and_flags(self):
        calls = []
        def send(count, inputs, size):  # SendInput: one keyboard INPUT per key
            calls.append((inputs[0].ki.wVk, inputs[0].ki.wScan, inputs[0].ki.dwFlags))
            return count
        user32 = SimpleNamespace(MapVirtualKeyW=lambda vk, kind: vk + 1000, SendInput=send)
        keys = object.__new__(remote_view.WindowsKeys)
        keys.user32 = user32
        keys.key("ArrowUp", True)
        keys.key("ArrowUp", False)
        keys.key("KeyA", False)
        keys.key("OSRight", True)
        self.assertEqual(calls, [(0x26, 1038, 0x1), (0x26, 1038, 0x3), (0x41, 1065, 0x2), (0x5C, 1092, 0x1)])

    def test_linux_keys_go_through_ydotool(self):
        with patch("subprocess.run") as run:
            remote_view.EvdevKeys().key("ShiftLeft", True)
            remote_view.EvdevKeys().key("OSLeft", False)
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["ydotool", "key", "42:1"], ["ydotool", "key", "125:0"]])

    def test_unknown_keys_are_ignored_and_never_held(self):
        keys = Keys()
        inj = Injector(Pointer(), keys)
        inj.apply({"t": "key", "code": "AudioVolumeUp", "down": True})
        self.assertEqual(keys.calls, [])
        self.assertFalse(inj.busy())

    def test_key_repeat_holds_once_and_one_up_lets_go(self):
        keys = Keys()
        inj = Injector(Pointer(), keys)
        for _ in range(3):
            inj.apply({"t": "key", "code": "KeyA", "down": True})
        self.assertEqual(inj.held, {"KeyA"})
        inj.apply({"t": "key", "code": "KeyA", "down": False})
        self.assertFalse(inj.busy())
        self.assertEqual(keys.calls, [("KeyA", True)] * 3 + [("KeyA", False)])


if __name__ == "__main__":
    unittest.main()
