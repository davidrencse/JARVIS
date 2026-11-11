"""Chaos tests for the remote-control lifecycle: the hub's Sessions and the node's NodeSession
driven together over an in-process fake transport, with fake hands (pointer/keys), a fake
screen, a fake Lan Mouse on each side and ControllerPause on the main.

Nothing real is touched: no mouse, keyboard, screen, Lan Mouse, node or model. Timings are
patched short. Scratch files live under ~/.cache/jarvis-breakit/.

Invariants checked after every scenario:
- every way a session ends releases all held keys/buttons on the node;
- the node's Lan Mouse comes back exactly once after the last session, never mid-session;
- the main's Lan Mouse is paused during and restored after (never left off, never restored
  while apart, never turned into a guard override);
- no input reaches a node after its session ended;
- the console never sees a frozen picture as live;
- only the main's console can start sessions.

Tests that expose a still-open bug are marked expectedFailure with the finding id (LC-n).
"""

import base64
import json
import os
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import remote_view, sharedinput
from jarvis.cluster import Hub, HubRefused
from jarvis.locking import exclusive_lock
from jarvis.remote_view import (CONSOLE_QUIET, MAIN_IDLE, MAX_SESSION, NODE_IDLE, Ended, Injector,
                                NodeSession, Sessions)

SCRATCH = Path(os.path.expanduser("~/.cache/jarvis-breakit"))
JPEG = b"\xff\xd8\xff" + b"chaos"
NODE = "dell"
SEED = 20261107
HUB_RUNS = int(os.environ.get("JARVIS_CHAOS_HUB_RUNS", "3000"))
NODE_RUNS = int(os.environ.get("JARVIS_CHAOS_NODE_RUNS", "8"))


def _tmpdir():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # A session thread can still be finishing a marker write as the test ends; that must not
    # fail the run (it blocked a node release on 2026-11-07).
    return tempfile.TemporaryDirectory(dir=SCRATCH, ignore_cleanup_errors=True)


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return bool(check())


class Clock:
    """Real time plus a jump offset: the hub's held waits still pass, and tests can jump."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset

    def jump(self, seconds):
        self.offset += seconds


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# -- the node's desktop ----------------------------------------------------------------
class Desk:
    """One physical desktop: what is held down right now, whichever process pressed it."""

    def __init__(self):
        self.keys, self.buttons, self.log = set(), set(), []
        self.lock = threading.Lock()

    def hands(self):
        return Hands(self)


class Hands:
    """One process's way to the desk. dead: that process is gone, its calls land nowhere."""

    def __init__(self, desk):
        self.desk, self.dead = desk, False
        self.pointer, self.keys = _Pointer(self), _Keys(self)

    def _do(self, what, *args):
        if self.dead:
            return False
        with self.desk.lock:
            self.desk.log.append((what,) + args)
        return True


class _Pointer:
    def __init__(self, hands):
        self.h = hands

    def move(self, x, y):
        self.h._do("move", x, y)

    def button(self, which, how):
        if self.h._do("button", which, how):
            (self.h.desk.buttons.add if how == "down" else self.h.desk.buttons.discard)(which)

    def scroll(self, direction, amount):
        self.h._do("scroll", direction, amount)

    def type(self, text):
        self.h._do("type", text)

    def release(self):
        self.h._do("backend-release")  # deliberately clears nothing: the Injector must


class _Keys:
    def __init__(self, hands):
        self.h = hands

    def key(self, code, down):
        if self.h._do("key", code, down):
            (self.h.desk.keys.add if down else self.h.desk.keys.discard)(code)


def tiny():
    from PIL import Image
    return Image.new("RGB", (8, 8)), {"x": 0, "y": 0, "w": 800, "h": 600}


def fake_encode(image, quality):
    return JPEG, image.width, image.height


# -- the transport: node -> hub, in process --------------------------------------------
class Link:
    """What the node's Client.request does against the hub, mapped like the hub's HTTP
    handler maps errors. down: requests fail fast (cable out); hang_input/hang_end: those
    requests block until the event is set (a frozen socket)."""

    def __init__(self, sessions, node=NODE):
        self.sessions, self.node = sessions, node
        self.down = False
        self.hang_input = threading.Event()
        self.hang_input.set()
        self.hang_end = threading.Event()
        self.hang_end.set()
        self.blocked = 0
        self.ends = []

    def request(self, path, body, timeout=None):
        if self.down:
            time.sleep(0.01)
            raise OSError("link down")
        sid = body.get("session")
        try:
            if path == "/v1/remote/frame":
                if body.get("end") is not None:
                    self.ends.append(body["end"])
                    if not self.hang_end.is_set():
                        self.hang_end.wait(timeout or 5)
                    return self.sessions.node_end(self.node, sid, body["end"])
                jpeg = base64.b64decode(body["jpeg"]) if body.get("jpeg") is not None else None
                return self.sessions.seal(sid, "frame", self.sessions.frame(
                    self.node, sid, jpeg, body.get("width"), body.get("height")))
            if path == "/v1/remote/input":
                if not self.hang_input.is_set():
                    self.blocked += 1
                    self.hang_input.wait(30)
                    self.blocked -= 1
                    if self.down:
                        raise OSError("link down")
                return self.sessions.seal(sid, "input", self.sessions.take_input(
                    self.node, sid, body.get("after", 0), hold=0.05))  # sealed like the hub's handler
        except PermissionError as exc:
            raise HubRefused(str(exc), 403) from None
        except LookupError as exc:
            raise HubRefused(str(exc), 404) from None
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            raise HubRefused(str(exc), 400) from None
        raise HubRefused("Unknown cluster endpoint.", 404)


class LanMouse:
    """A fake Lan Mouse. frozen: the process that would call us is dead (calls are no-ops)."""

    def __init__(self, running=True, live=lambda: [], apart=lambda: False, side="node"):
        self.running, self.calls, self.bad = running, [], []
        self.live, self.apart, self.side = live, apart, side
        self.frozen = False

    def status(self):
        return {"running": self.running}

    def start(self):
        if self.frozen:
            return {"started": False}
        self.calls.append("start")
        if self.live():
            self.bad.append(f"{self.side} Lan Mouse restarted while a session drives: {self.live()}")
        if self.apart():
            self.bad.append(f"{self.side} Lan Mouse restarted while the laptops read apart")
        self.running = True
        return {"started": True}

    def stop(self, sharing_only=False):
        if self.frozen:
            return {"stopped": False}
        self.calls.append("stop")
        self.running = False
        return {"stopped": True}


# -- the base: one main, one node --------------------------------------------------------
class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.main_state, self.node_state = root / "main", root / "node"
        self.main_state.mkdir()
        self.node_state.mkdir()
        self.stopped = False  # `jarvis control stop` on the node
        self.created = []
        remote_view._ACTIVE.clear()
        remote_view._LAN.update(owed=False, starting=0)
        self.addCleanup(lambda: remote_view._LAN.update(owed=False, starting=0))
        self.addCleanup(remote_view._ACTIVE.clear)
        self.addCleanup(self._wind_down)

        self.node_lm = LanMouse(True, live=self._driving, side="node")
        test = self
        original_start = NodeSession.start

        def recording_start(session):
            test.created.append(session)
            return original_start(session)
        patches = [
            patch.object(sharedinput, "lan_mouse_status", side_effect=lambda: self.node_lm.status()),
            patch.object(sharedinput, "stop", side_effect=lambda sharing_only=False: self.node_lm.stop(sharing_only)),
            patch.object(sharedinput, "start", side_effect=lambda: self.node_lm.start()),
            patch.dict(os.environ, {"JARVIS_NO_MODELS": "0", "JARVIS_NO_NOTIFY": "1"}),
            patch("jarvis.control._stopped", side_effect=lambda state: self.stopped),
            patch("jarvis.platforms.detect", return_value={"os": "linux", "desktop": "hyprland"}),
            patch.object(remote_view, "desktop_problem", return_value=None),
            patch.object(remote_view, "DEADMAN", 0.4),
            patch.object(remote_view, "KEEPALIVE", 0.1),
            patch.object(remote_view.end_all, "__defaults__", ("JARVIS on this computer is restarting.", 0.6)),
            patch.object(NodeSession, "LINK_LOSS", 0.8),
            patch.object(NodeSession, "start", recording_start),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # the main
        self.clock = Clock()
        self.main_lm = LanMouse(True, live=self._hub_live, apart=self._apart, side="main")
        self.pause = sharedinput.ControllerPause(self.main_state, self.main_lm.status, self.main_lm.start,
                                                 self.main_lm.stop)
        self.sessions = Sessions(self.clock, on_busy=self.pause.set)
        self.link = Link(self.sessions)
        self.desk = Desk()
        self.hands = self.desk.hands()

    def _wind_down(self):
        self.link.down = True
        self.link.hang_input.set()
        self.link.hang_end.set()
        for s in list(self.created):
            s.end("test over")
        for s in list(self.created):
            s.join(2)
        # sessions a test took out of self.created (a "killed" node) and pause workers
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.name.startswith(("remote-", "lan-mouse-controller")) and t.name != "remote-sweeper":
                t.join(2)

    # -- helpers ---------------------------------------------------------------------
    def _driving(self):
        return [s.sid[:6] for s in self.created if not s.stop_event.is_set()]

    def _hub_live(self):
        with self.sessions._cond:
            return [s["id"][:6] for s in self.sessions._all.values() if s["state"] != "ended"]

    def _apart(self):
        try:
            return bool(json.loads((self.main_state / sharedinput.GUARD_FILE).read_text()).get("apart_now"))
        except (OSError, ValueError):
            return False

    def hub(self, dispatch=None):
        fake = SimpleNamespace(main="main", remote=self.sessions,
                               config={"devices": {"main": {}, NODE: {"name": "Dell"}}})
        fake.dispatch = dispatch or (lambda node, kind, payload, timeout: self.node_job(payload))
        return fake

    def node_job(self, payload, hands=None):
        return remote_view.run(payload, {}, self.node_state, client=self.link,
                               injector=Injector((hands or self.hands).pointer, (hands or self.hands).keys),
                               capture=tiny, encoder=fake_encode)

    def connect(self):
        session = Hub.remote_open(self.hub(), NODE, "low")
        self.assertTrue(until(lambda: self.sessions.get(session["id"])["state"] in ("live", "ended")))
        return session["id"]

    def node_session(self, sid):
        return next(s for s in self.created if s.sid == sid)

    def press(self, sid):
        self.sessions.push(sid, [{"t": "down", "b": "left", "x": 0.5, "y": 0.5},
                                 {"t": "key", "code": "ShiftLeft", "down": True},
                                 {"t": "key", "code": "KeyA", "down": True}])
        self.assertTrue(until(lambda: {"ShiftLeft", "KeyA"} <= self.desk.keys and "left" in self.desk.buttons),
                        "the presses never landed")

    def settle_pause(self):
        def idle():
            if self.pause.want != self.pause.applied:
                return False
            if self.pause._lock.acquire(blocking=False):
                self.pause._lock.release()
                return True
            return False
        self.assertTrue(until(idle), "ControllerPause never settled")

    def node_marker(self):
        try:
            return json.loads((self.node_state / remote_view.MARKER).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def all_node_sessions_done(self):
        return all(s._done and not any(t.is_alive() for t in s.threads) for s in self.created)

    LC7 = "main Lan Mouse restarted while a session drives"

    def assert_clean(self, what="", known=()):
        """Every invariant, once everything has ended. known: finding ids whose symptom is
        checked by its own expectedFailure test and tolerated here."""
        self.assertTrue(until(self.all_node_sessions_done), f"{what}: a node session never finished")
        self.assertEqual(self.desk.keys, set(), f"{what}: keys left held on the node")
        self.assertEqual(self.desk.buttons, set(), f"{what}: buttons left held on the node")
        self.assertEqual(remote_view._ACTIVE, {}, f"{what}: node still tracks a session")
        self.assertIsNone(self.node_marker(), f"{what}: node session marker left behind")
        self.assertEqual(self.node_lm.bad, [], what)
        self.assertTrue(self.node_lm.running, f"{what}: node Lan Mouse left off")
        self.assertEqual(self.node_lm.calls.count("start"), self.node_lm.calls.count("stop"),
                         f"{what}: node Lan Mouse stop/start unbalanced: {self.node_lm.calls}")
        self.assertFalse(remote_view._LAN["owed"], what)
        self.assertEqual(self._hub_live(), [], f"{what}: a hub session is still live")
        self.settle_pause()
        self.assertFalse(self.pause.applied, what)
        bad = [b for b in self.main_lm.bad if not ("LC-7" in known and b.startswith(self.LC7))]
        self.assertEqual(bad, [], what)
        if not self._apart():
            self.assertTrue(self.main_lm.running, f"{what}: main Lan Mouse left off")
        self.assertFalse(sharedinput.controlling_remote(self.main_state), what)
        try:
            memo = json.loads((self.main_state / sharedinput.GUARD_FILE).read_text())
        except (OSError, ValueError):
            memo = {}
        self.assertFalse(memo.get("override"), f"{what}: the restore became a guard override")


# -- scenarios ---------------------------------------------------------------------------
class ScenarioTests(Rig):
    def test_plain_connect_press_disconnect(self):
        sid = self.connect()
        self.assertEqual(self.node_lm.calls, ["stop"])
        self.settle_pause()
        self.assertEqual(self.main_lm.calls, ["stop"])
        self.press(sid)
        Hub.remote_close(self.hub(), sid)
        self.assert_clean("disconnect")

    def test_link_drop_mid_press_deadman_lets_go_and_session_survives(self):
        sid = self.connect()
        self.press(sid)
        self.link.down = True  # cable out while the mouse button and keys are down
        self.assertTrue(until(lambda: not self.desk.keys and not self.desk.buttons, 3),
                        "nothing let go while the link was down")
        self.link.down = False
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "live"))
        self.press(sid)  # input works again after the link came back
        Hub.remote_close(self.hub(), sid)
        self.assert_clean("link drop then disconnect")

    def test_frozen_input_socket_mid_press_deadman_lets_go(self):
        sid = self.connect()
        self.press(sid)
        self.link.hang_input.clear()  # the poll hangs (no error): only the dead-man can help
        self.assertTrue(until(lambda: not self.desk.keys and not self.desk.buttons, 3))
        self.link.hang_input.set()
        Hub.remote_close(self.hub(), sid)
        self.assert_clean("frozen socket")

    def test_link_lost_for_good_ends_both_sides(self):
        sid = self.connect()
        self.press(sid)
        self.link.down = True
        session = self.node_session(sid)
        self.assertTrue(until(lambda: session._done, 4), "node never gave up on the link")
        self.assertIn("Lost the link", session.why)
        self.clock.jump(NODE_IDLE + 1)
        self.assertEqual(self.sessions.get(sid)["state"], "ended")
        self.assert_clean("link lost")

    def test_node_stop_job_mid_press(self):
        sid = self.connect()
        self.press(sid)
        remote_view.run({"op": "stop", "session": sid}, {}, self.node_state)
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"),
                        "the hub never heard the node end it")
        self.assert_clean("stop job")

    def test_control_stop_on_the_node_mid_press(self):
        sid = self.connect()
        self.press(sid)
        self.stopped = True
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assertIn("stopped", self.sessions.get(sid)["error"])
        self.stopped = False
        self.assert_clean("control stop")

    def test_console_tab_closed_mid_press(self):
        sid = self.connect()
        self.press(sid)
        self.clock.jump(CONSOLE_QUIET + 1)  # the console went silent: the node lets go first
        self.assertTrue(until(lambda: not self.desk.keys and not self.desk.buttons, 3))
        self.assertEqual(self.sessions.get(sid)["state"], "live")
        self.clock.jump(MAIN_IDLE)
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assert_clean("tab closed")

    def test_max_session_clock_jump(self):
        sid = self.connect()
        self.press(sid)
        self.clock.jump(MAX_SESSION + 1)
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assert_clean("max session")

    def test_huge_clock_jump(self):
        sid = self.connect()
        self.press(sid)
        self.clock.jump(10 ** 7)
        self.assertTrue(until(lambda: self.sessions.current()["state"] == "ended"))
        self.assert_clean("clock jump")

    def test_double_disconnect(self):
        sid = self.connect()
        self.press(sid)
        first = Hub.remote_close(self.hub(), sid)
        second = Hub.remote_close(self.hub(), sid)
        self.assertEqual(first["state"], second["state"])
        self.assertEqual(second["error"], "Disconnected.")
        self.assert_clean("double disconnect")

    def test_double_connect_leaves_one_session(self):
        results, errors = [], []

        def go():
            try:
                results.append(Hub.remote_open(self.hub(), NODE, "low"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertTrue(until(lambda: len(self._hub_live()) <= 1 and
                              sum(not s.stop_event.is_set() for s in self.created) <= 1, 3))
        live = self._hub_live()
        if live:
            sid = next(s["id"] for s in self.sessions._all.values() if s["state"] != "ended")
            self.press(sid)
            Hub.remote_close(self.hub(), sid)
        self.assert_clean("double connect")

    def test_connect_again_replaces_mid_press(self):
        a = self.connect()
        self.press(a)
        b = self.connect()
        self.assertEqual(self.sessions.get(a)["state"], "ended")
        self.assertEqual(self.desk.keys, set())  # the replaced one let go before the new one
        self.press(b)
        self.assertEqual(self.node_lm.calls, ["stop"])
        Hub.remote_close(self.hub(), b)
        self.assert_clean("replace", known=("LC-7",))

    def test_connect_again_never_restarts_the_mains_lan_mouse(self):
        a = self.connect()
        self.settle_pause()
        self.connect()
        self.settle_pause()
        self.assertEqual(self.main_lm.calls, ["stop"], "the main's Lan Mouse flapped during the replace")

    def test_replace_while_old_session_is_stuck_mid_finish(self):
        a = self.connect()
        self.press(a)
        self.link.hang_input.clear()  # A's input poll freezes: its end_all join times out
        self.assertTrue(until(lambda: self.link.blocked >= 1))
        b = self.connect()
        old = self.node_session(a)
        self.assertFalse(old._done)
        self.link.hang_input.set()  # A finishes now, while B drives
        self.assertTrue(until(lambda: old._done))
        self.assertEqual((self.node_marker() or {}).get("session"), b[:8])
        self.assertEqual(self.node_lm.bad, [])
        self.press(b)
        Hub.remote_close(self.hub(), b)
        self.assert_clean("replace mid-finish", known=("LC-7",))

    def test_replace_while_old_session_posts_its_end_slowly(self):
        a = self.connect()
        self.link.hang_end.clear()  # A's end message to the main hangs inside _finish
        b = self.connect()
        self.press(b)
        self.link.hang_end.set()
        Hub.remote_close(self.hub(), b)
        self.assertNotEqual(self.sessions.get(a)["state"], "live")
        self.assert_clean("slow end post", known=("LC-7",))

    def test_disconnect_during_connect(self):
        gate, started = threading.Event(), threading.Event()
        sid_box = []

        def dispatch(node, kind, payload, timeout):
            sid_box.append(payload["session"])
            started.set()
            gate.wait(5)
            return self.node_job(payload)
        result = []
        t = threading.Thread(target=lambda: result.append(Hub.remote_open(self.hub(dispatch), NODE, "low")))
        t.start()
        self.assertTrue(started.wait(5))
        Hub.remote_close(self.hub(), sid_box[0])  # Disconnect pressed while Connect waits
        gate.set()
        t.join(10)
        self.assertEqual(result[0]["state"], "ended")
        self.assert_clean("disconnect during connect")

    def test_input_pushed_just_before_disconnect_never_lands(self):
        sid = self.connect()
        self.link.hang_input.clear()
        self.assertTrue(until(lambda: self.link.blocked >= 1))
        self.sessions.push(sid, [{"t": "key", "code": "KeyQ", "down": True}])
        Hub.remote_close(self.hub(), sid)
        self.link.hang_input.set()
        self.assert_clean("push then disconnect")
        self.assertNotIn(("key", "KeyQ", True), self.desk.log)
        with self.assertRaises(Ended):
            self.sessions.push(sid, [{"t": "key", "code": "KeyW", "down": True}])

    def test_main_close_all_releases_the_node(self):
        sid = self.connect()
        self.press(sid)
        self.sessions.close_all("JARVIS on the main stopped.")
        self.assertTrue(until(lambda: self.node_session(sid)._done))
        self.assert_clean("main close_all")

    def test_main_restart_node_side_lets_go(self):
        sid = self.connect()
        self.press(sid)
        # The main dies and comes back: a fresh Sessions that never heard of the session.
        self.sessions = Sessions(self.clock)
        self.link.sessions = self.sessions
        self.assertTrue(until(lambda: self.node_session(sid)._done))
        self.assertEqual(self.desk.keys, set())
        self.assertEqual(self.desk.buttons, set())
        self.assertTrue(self.node_lm.running)
        self.assertEqual(self.node_lm.calls, ["stop", "start"])
        self.assertIsNone(self.node_marker())

    def test_node_restart_recover_lets_go_of_what_the_dead_session_held(self):
        sid = self.connect()
        self.press(sid)
        self._kill_node(sid)
        fresh = self.desk.hands()
        self.assertTrue(remote_view.recover(self.node_state, injector=Injector(fresh.pointer, fresh.keys)))
        self.assertEqual(self.desk.keys, set())
        self.assertEqual(self.desk.buttons, set())
        self.assertIsNone(self.node_marker())

    def test_node_restart_recover_brings_lan_mouse_back(self):
        sid = self.connect()
        self.press(sid)
        self._kill_node(sid)
        fresh = self.desk.hands()
        remote_view.recover(self.node_state, injector=Injector(fresh.pointer, fresh.keys))
        self.assertTrue(self.node_lm.running, "the node's Lan Mouse stays off after a mid-session crash")

    def test_graceful_node_restart_with_a_slow_link_restores_lan_mouse(self):
        sid = self.connect()
        self.press(sid)
        self.link.hang_input.clear()  # the input poll is mid-request (up to INPUT_HOLD + 8 s)
        self.assertTrue(until(lambda: self.link.blocked >= 1))
        remote_view.end_all()  # what NodeWorker runs before respawn(); then the process exits
        self.assertEqual(self.desk.keys, set())  # end() let go at once: that part holds
        self.assertTrue(self.node_lm.running, "the process exits with the node's Lan Mouse off")

    def _kill_node(self, sid):
        """Leave exactly what a killed node process leaves: keys held on the desk, the marker
        listing them, Lan Mouse stopped. (The session ends with dead hands and a frozen Lan
        Mouse, then its marker is put back.)"""
        self.assertTrue(until(lambda: set((self.node_marker() or {}).get("held") or ()) >= {"ShiftLeft", "KeyA"}))
        marker = (self.node_state / remote_view.MARKER).read_text(encoding="utf-8")
        self.hands.dead = True
        self.node_lm.frozen = True
        self.link.down = True
        session = self.node_session(sid)
        session.end("killed")
        self.assertTrue(until(lambda: session._done))
        (self.node_state / remote_view.MARKER).write_text(marker, encoding="utf-8")
        remote_view._ACTIVE.clear()
        remote_view._LAN.update(owed=False, starting=0)
        self.node_lm.frozen = False
        self.created.remove(session)
        self.assertTrue({"ShiftLeft", "KeyA"} <= self.desk.keys)
        self.assertFalse(self.node_lm.running)

    def test_main_restart_mid_session_restores_the_mains_lan_mouse(self):
        sid = self.connect()
        self.settle_pause()
        self.assertFalse(self.main_lm.running)
        # The main's JARVIS dies (or Hub.close's daemon restore thread dies with it).
        pause = sharedinput.ControllerPause(self.main_state, self.main_lm.status, self.main_lm.start,
                                            self.main_lm.stop)
        self.sessions = Sessions(self.clock, on_busy=pause.set)
        self.link.sessions = self.sessions
        self.assertTrue(until(lambda: self.node_session(sid)._done))
        self.sessions.current()  # the new hub's first look at sessions
        pause.set(False, wait=True)
        self.assertTrue(self.main_lm.running, "nothing on the new hub brings the main's Lan Mouse back")

    def test_same_session_id_started_twice(self):
        view = self.sessions.open(NODE, "low")
        sid = view["id"]
        payload = {"op": "start", "session": sid, "quality": "low"}
        self.assertTrue(self.node_job(payload)["ok"])
        old = self.node_session(sid)
        self.link.hang_input.clear()
        self.assertTrue(until(lambda: self.link.blocked >= 1))
        self.assertTrue(self.node_job(payload)["ok"])  # the same job, delivered again
        new = [s for s in self.created if s.sid == sid][-1]
        self.assertIsNot(old, new)
        self.link.hang_input.set()
        self.assertTrue(until(lambda: old._done))
        self.assertFalse(new.stop_event.is_set())
        self.assertIs(remote_view._ACTIVE.get(sid), new, "the old session's _finish popped the new one")
        self.assertIsNotNone(self.node_marker(), "the old session deleted the live one's marker")
        self.assertEqual(self.node_lm.bad, [])


class HubFindingTests(unittest.TestCase):
    """Hub-side findings with a fake clock: deterministic, no threads."""

    def test_a_stuck_frame_thread_is_not_shown_as_live(self):  # LC-1 (same bug as BR-5)
        clock = FakeClock()
        s = Sessions(clock)
        sid = s.open(NODE, "low")["id"]
        s.touch(sid)
        s.frame(NODE, sid, JPEG, 800, 600)
        for _ in range(40):  # 40 s: the node's capture hangs, its input thread still polls
            clock.now += 1
            s.take_input(NODE, sid, 0, hold=0)
            try:
                self.assertIsNone(s.wait_frame(sid, 1, hold=0))  # the console watches: no new frame
            except Ended:
                break  # the hub ended it: what we want
        state = s.get(sid)["state"]
        self.assertTrue(state == "ended" or s.node_ago(sid) > NODE_IDLE,
                        f"40 s without a frame, yet state={state} and node_ago={s.node_ago(sid)}")

    def test_a_silent_node_is_not_shown_as_live(self):
        clock = FakeClock()
        s = Sessions(clock)
        sid = s.open(NODE, "low")["id"]
        s.touch(sid)
        s.frame(NODE, sid, JPEG, 800, 600)
        clock.now += 3
        self.assertIsNone(s.wait_frame(sid, 1, hold=0))
        self.assertGreaterEqual(s.node_ago(sid), 3)
        clock.now += NODE_IDLE
        with self.assertRaises(Ended):
            s.wait_frame(sid, 1, hold=0)

    def test_replace_reports_busy_without_a_gap(self):
        seen = []
        s = Sessions(FakeClock(), on_busy=seen.append)
        s.open(NODE, "low")
        s.open(NODE, "low")  # Connect again (or a second tab)
        self.assertNotIn(False, seen[seen.index(True):], f"on_busy saw {seen}: ControllerPause restarts Lan Mouse mid-replace")

    @patch.object(remote_view, "SWEEP", 0.01)  # LC-6 (fixed): the hub sweeps on a timer
    def test_sessions_expire_without_requests(self):
        clock, seen = FakeClock(), []
        s = Sessions(clock, on_busy=seen.append)
        sid = s.open(NODE, "low")["id"]
        s.touch(sid)
        s.frame(NODE, sid, JPEG, 800, 600)
        clock.now += MAIN_IDLE + NODE_IDLE + 60  # browser crashed and the node lost power
        time.sleep(0.05)
        self.assertEqual(seen[-1], False, "the main's Lan Mouse stays paused until a request arrives")

    def test_only_the_mains_console_starts_sessions(self):
        s = Sessions(FakeClock())
        sid = s.open(NODE, "low")["id"]
        with self.assertRaises(PermissionError):
            s.frame("asus", sid, JPEG, 8, 8)
        with self.assertRaises(PermissionError):
            s.take_input("asus", sid, 0, hold=0)
        with self.assertRaises(PermissionError):
            s.node_end("asus", sid, "x")
        with self.assertRaises(LookupError):  # a session the console never opened
            s.frame(NODE, "e" * 32, JPEG, 8, 8)
        src = (Path(__file__).resolve().parents[2] / "jarvis" / "cluster.py").read_text(encoding="utf-8")
        self.assertIn('raise PermissionError("Only the main\'s console can start remote control.")', src)


class ControllerPauseFindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        p = patch.dict(os.environ, {"JARVIS_NO_NOTIFY": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.lm = LanMouse(True, side="main")

    def guard(self, near, now):
        reading = {"near": near, "peer": "asus", "why": "test"}
        return sharedinput.guard(self.state, nearby=lambda: reading, status_fn=self.lm.status,
                                 start_fn=self.lm.start, stop_fn=self.lm.stop, now=now)

    def set_apart(self, apart):
        path = self.state / sharedinput.GUARD_FILE
        try:
            memo = json.loads(path.read_text())
        except (OSError, ValueError):
            memo = {}
        memo["apart_now"] = apart
        path.write_text(json.dumps(memo))

    def test_end_while_apart_then_together_brings_it_back(self):
        pause = sharedinput.ControllerPause(self.state, self.lm.status, self.lm.start, self.lm.stop)
        pause.set(True, wait=True)
        self.set_apart(True)
        pause.set(False, wait=True)
        self.assertFalse(self.lm.running)  # not while apart
        for i in range(3):
            self.guard(True, now=time.time() + i)
        self.assertTrue(self.lm.running)
        self.assertEqual(self.lm.calls, ["stop", "start"])

    def test_busy_guard_lock_at_pause_then_end_while_apart(self):
        pause = sharedinput.ControllerPause(self.state, self.lm.status, self.lm.start, self.lm.stop)
        with exclusive_lock(self.state / (sharedinput.GUARD_FILE + ".lock")):  # a guard reading
            pause.set(True, wait=True)
        self.assertFalse(self.lm.running)
        self.set_apart(True)
        pause.set(False, wait=True)  # a short session that ends while the laptops read apart
        for i in range(3):
            self.guard(True, now=time.time() + i)  # together again
        self.assertTrue(self.lm.running, "the main's Lan Mouse is never brought back")


# -- randomized ----------------------------------------------------------------------------
class RandomHubTests(unittest.TestCase):
    """Thousands of seeded random interleavings of every hub call, fake clock, real
    ControllerPause. Invariants after every step."""

    def test_random_hub_interleavings(self):
        rng = random.Random(SEED)
        with _tmpdir() as tmp, patch.dict(os.environ, {"JARVIS_NO_NOTIFY": "1"}):
            for run in range(HUB_RUNS):
                state = Path(tmp) / f"r{run}"
                state.mkdir()
                self._one(rng, state, run)

    def _one(self, rng, state, run):
        clock = FakeClock()
        guard_path = state / sharedinput.GUARD_FILE
        apart = {"now": False}

        def is_apart():
            return apart["now"]
        lm = LanMouse(rng.random() < 0.8, apart=is_apart, side="main")
        initially = lm.running
        pause = sharedinput.ControllerPause(state, lm.status, lm.start, lm.stop, clock=clock)
        s = Sessions(clock, on_busy=lambda live: pause.set(live, wait=True))
        nodes = ("dell", "asus")
        pressed = {}
        trail = []
        for step in range(rng.randint(5, 40)):
            op = rng.choice(["open", "open", "touch", "close", "close_all", "push", "push", "frame",
                             "frame", "input", "input", "node_end", "watch", "jump", "current", "apart"])
            ids = list(s._all)
            sid = rng.choice(ids) if ids and rng.random() < 0.9 else "f" * 32
            node = rng.choice(nodes)
            trail.append(op)
            try:
                if op == "open":
                    s.open(node, rng.choice(["low", "medium", "high"]), pending=rng.random() < 0.5)
                elif op == "touch":
                    s.touch(sid)
                elif op == "close":
                    s.close(sid)
                elif op == "close_all":
                    s.close_all("main stopped")
                elif op == "push":
                    s.push(sid, [rng.choice([{"t": "key", "code": "KeyA", "down": rng.random() < 0.5},
                                             {"t": "move", "x": rng.random(), "y": rng.random()},
                                             {"t": "down", "b": "left"}, {"t": "up", "b": "left"}])
                                 for _ in range(rng.randint(0, 5))])
                elif op == "frame":
                    owner = s._all[sid]["node"] if sid in s._all and rng.random() < 0.9 else node
                    s.frame(owner, sid, JPEG if rng.random() < 0.7 else None, 800, 600)
                elif op == "input":
                    owner = s._all[sid]["node"] if sid in s._all and rng.random() < 0.9 else node
                    was_ended = sid in s._all and s._all[sid]["state"] == "ended"
                    reply = s.take_input(owner, sid, pressed.get(sid, 0), hold=0)
                    if was_ended or reply["stop"]:
                        self.assertEqual(reply["events"], [], f"run {run}: input after the end {trail}")
                    for n, _ in reply["events"]:
                        self.assertGreater(n, pressed.get(sid, 0))
                    if reply["events"] and rng.random() < 0.8:  # sometimes the reply is lost
                        pressed[sid] = reply["events"][-1][0]
                elif op == "node_end":
                    owner = s._all[sid]["node"] if sid in s._all else node
                    s.node_end(owner, sid, "node ended")
                elif op == "watch":
                    s.wait_frame(sid, rng.randint(0, 3), hold=0)
                elif op == "jump":
                    clock.now += rng.choice([0.5, 3, CONSOLE_QUIET + 1, NODE_IDLE + 1, MAIN_IDLE + 1, 31, 125,
                                             MAX_SESSION + 1])
                    continue
                elif op == "current":
                    s.current()
                elif op == "apart":
                    apart["now"] = not apart["now"]
                    memo = {}
                    try:
                        memo = json.loads(guard_path.read_text())
                    except (OSError, ValueError):
                        pass
                    memo["apart_now"] = apart["now"]
                    guard_path.write_text(json.dumps(memo))
                    continue
            except (Ended, LookupError, ValueError, PermissionError):
                pass
            with s._cond:
                live = [x for x in s._all.values() if x["state"] != "ended"]
                for n in nodes:
                    self.assertLessEqual(sum(x["node"] == n for x in live), 1, f"run {run}: two live on {n} {trail}")
                for x in s._all.values():
                    if x["state"] == "ended":
                        self.assertEqual(x["events"], [], f"run {run}: ended session still queues {trail}")
                        self.assertIsNone(x["frame"])
            if op not in ("touch",):  # every other call expired and reported busy
                self.assertEqual(pause.want, bool(live), f"run {run}: pause wish wrong after {trail}")
                self.assertEqual(pause.applied, bool(live), f"run {run}: pause not applied after {trail}")
                if live and initially:
                    self.assertFalse(lm.running, f"run {run}: main Lan Mouse on during a session {trail}")
        s.close_all("done")
        self.assertFalse(pause.applied)
        self.assertEqual(lm.bad, [], f"run {run}: {trail}")
        self.assertFalse(sharedinput.controlling_remote(state))
        if initially and not lm.running:  # ended while apart: the guard owes it, once together
            memo = json.loads(guard_path.read_text())
            self.assertTrue(memo.get("stopped_by_guard"), f"run {run}: nobody owes the restart {trail}")
            apart["now"] = False
            for i in range(sharedinput.GUARD_STREAK):
                sharedinput.guard(state, nearby=lambda: {"near": True, "peer": "p", "why": "t"},
                                  status_fn=lm.status, start_fn=lm.start, stop_fn=lm.stop, now=time.time() + i)
            self.assertTrue(lm.running, f"run {run}: main Lan Mouse left off {trail}")
        try:
            memo = json.loads(guard_path.read_text())
        except (OSError, ValueError):
            memo = {}
        self.assertFalse(memo.get("override"), f"run {run}: became an override {trail}")
        self.assertLessEqual(lm.calls.count("start"), lm.calls.count("stop"))


class RandomEndToEndTests(Rig):
    """Seeded random interleavings with real threads on both sides."""

    def test_random_end_to_end_interleavings(self):
        rng = random.Random(SEED + 1)
        for run in range(NODE_RUNS):
            with self.subTest(run=run):
                self._one(rng, run)

    def _reset(self):
        self._wind_down()
        remote_view._ACTIVE.clear()
        remote_view._LAN.update(owed=False, starting=0)
        self.created.clear()
        self.desk.keys.clear()
        self.desk.buttons.clear()
        self.node_lm = LanMouse(True, live=self._driving, side="node")
        self.main_lm = LanMouse(True, live=self._hub_live, apart=self._apart, side="main")
        self.settle_pause()
        self.pause = sharedinput.ControllerPause(self.main_state, self.main_lm.status, self.main_lm.start,
                                                 self.main_lm.stop)
        self.sessions = Sessions(self.clock, on_busy=self.pause.set)
        self.link = Link(self.sessions)
        self.stopped = False
        for name in (remote_view.MARKER, sharedinput.CONTROLLING_MARKER, sharedinput.GUARD_FILE):
            for d in (self.node_state, self.main_state):
                (d / name).unlink(missing_ok=True)

    def _one(self, rng, run):
        self._reset()
        trail = []
        for _ in range(rng.randint(3, 10)):
            op = rng.choice(["connect", "connect", "press", "press", "disconnect", "stop_job", "link_blip",
                             "hang_blip", "quiet", "control_stop", "replace_stuck"])
            trail.append(op)
            live = [x["id"] for x in list(self.sessions._all.values()) if x["state"] != "ended"]
            try:
                if op == "connect":
                    Hub.remote_open(self.hub(), NODE, "low")
                elif op == "press" and live:
                    self.sessions.push(live[0], [{"t": "down", "b": "left", "x": 0.3, "y": 0.3},
                                                 {"t": "key", "code": rng.choice(["ShiftLeft", "KeyA"]), "down": True}])
                elif op == "disconnect" and live:
                    Hub.remote_close(self.hub(), live[0])
                elif op == "stop_job" and live:
                    remote_view.run({"op": "stop", "session": live[0]}, {}, self.node_state)
                elif op == "link_blip":
                    self.link.down = True
                    time.sleep(rng.choice([0.05, 0.5, 1.2]))
                    self.link.down = False
                elif op == "hang_blip":
                    self.link.hang_input.clear()
                    time.sleep(rng.choice([0.05, 0.5]))
                    self.link.hang_input.set()
                elif op == "quiet":
                    self.clock.jump(CONSOLE_QUIET + 1)
                elif op == "control_stop":
                    self.stopped = True
                    time.sleep(0.2)
                    self.stopped = False
                elif op == "replace_stuck":
                    self.link.hang_input.clear()
                    until(lambda: self.link.blocked >= 1, 0.5)
                    try:
                        Hub.remote_open(self.hub(), NODE, "low")
                    finally:
                        self.link.hang_input.set()
            except (ValueError, RuntimeError, OSError, Ended, LookupError):
                pass
            time.sleep(rng.choice([0.0, 0.02, 0.1]))
        self.link.down = False
        self.link.hang_input.set()
        self.sessions.close_all("JARVIS on the main stopped.")
        self.assert_clean(f"run {run} {trail}", known=("LC-7",))


if __name__ == "__main__":
    unittest.main()
