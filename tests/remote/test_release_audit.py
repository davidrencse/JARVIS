"""Audit 2026-11-13: ending a remote session always lets go of held keys, and Lan Mouse stays
off while the console drives another computer. Fakes only: no real input, no real Lan Mouse."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import control, remote_view, sharedinput
from jarvis.remote_view import Injector, NodeSession

from tests.remote.test_remote_lifecycle import Keys, NodeClient, Pointer, marker, tiny


class RefuseUp(Keys):
    refuse = False

    def key(self, code, down):
        if not down and self.refuse:
            raise OSError("SendInput returned 0")
        super().key(code, down)


class LateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        remote_view._ACTIVE.clear()
        remote_view._LATE.clear()
        self.addCleanup(remote_view._ACTIVE.clear)
        self.addCleanup(remote_view._LATE.clear)
        self.keys, self.pointer = RefuseUp(), Pointer()
        self.session = NodeSession({}, self.state, "a" * 32, "low", client=NodeClient(),
                                   injector=Injector(self.pointer, self.keys), capture=tiny, lan_mouse=False)
        remote_view._ACTIVE[self.session.sid] = self.session
        self.session.start()
        self.session.injector.apply({"t": "key", "code": "KeyA", "down": True})
        self.keys.refuse = True  # lock screen: the up is refused
        self.session.end("Disconnected.")
        for thread in self.session.threads:
            thread.join(3)
        self.assertTrue(self.session.release_thread.is_alive())
        self.addCleanup(self._stop_retry)

    def _stop_retry(self):
        self.keys.refuse = False
        remote_view.end_all("test over", wait=1)

    def test_the_control_stop_file_does_not_end_the_late_retry(self):
        # Before: the retry saw the stop file, gave up with the key still down, and left it to the
        # next process start. `jarvis control stop` is exactly when the key must come up.
        with mock.patch("jarvis.control._stopped", return_value=True):
            time.sleep(1.3)  # past two retry rounds (0.5 s, then 1 s later) with the stop file set
            self.assertTrue(self.session.release_thread.is_alive(), "the retry gave up on the stop file")
            self.assertTrue(self.session.injector.busy())
            self.keys.refuse = False  # the desktop accepts ups again
            self.session.release_thread.join(5)
        self.assertFalse(self.session.release_thread.is_alive())
        self.assertFalse(self.session.injector.busy())
        self.assertEqual(self.keys.calls[-1], ("KeyA", False))
        self.assertIsNone(marker(self.state))  # let go for good: nothing left for recover()

    def test_control_stop_lets_go_of_the_key_at_once(self):
        self.keys.refuse = False
        with mock.patch.object(control, "_release"):  # the backend sweep: not the real mouse
            ok, _ = control.stop(self.state)
        self.assertTrue(ok)
        self.assertFalse(self.session.injector.busy())
        self.assertEqual(self.keys.calls[-1], ("KeyA", False))
        self.session.release_thread.join(3)
        self.assertFalse(self.session.release_thread.is_alive())

    def test_control_stop_with_the_up_still_refused_keeps_the_key_tracked_for_the_retry(self):
        with mock.patch.object(control, "_release"):
            control.stop(self.state)
        self.assertTrue(self.session.injector.busy())  # still down, still tracked
        self.assertEqual(json.loads((self.state / remote_view.MARKER).read_text())["held"], ["KeyA"])

    def test_release_held_is_bounded_by_a_stuck_press(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = self.session.injector.release
        self.session.injector.release = lambda *a, **k: gate.wait(5) or real(*a, **k)
        began = time.monotonic()
        remote_view.release_held(wait=0.3)
        self.assertLess(time.monotonic() - began, 2.0)
        gate.set()


class FakeLan:
    def __init__(self, running=True):
        self.running, self.calls = running, []
        self.stop_fails = None  # an exception to raise, or "stays" (returns but keeps running)

    def status(self):
        return {"running": self.running}

    def start(self):
        self.calls.append("start")
        self.running = True

    def stop(self):
        self.calls.append("stop")
        if isinstance(self.stop_fails, Exception):
            raise self.stop_fails
        if self.stop_fails == "stays":
            return {"stopped": True}  # said it signalled, but it's still up
        if self.stop_fails == "refused":
            return {"stopped": False}
        self.running = False
        return {"stopped": True}


class ControllerPauseAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.lan, self.now = FakeLan(), [100.0]
        self.pause = sharedinput.ControllerPause(self.state, self.lan.status, self.lan.start, self.lan.stop,
                                                 clock=lambda: self.now[0])

    def test_a_stop_that_throws_is_not_applied_and_is_tried_again(self):
        self.lan.stop_fails = OSError("systemctl stop failed")
        self.pause.set(True, wait=True)
        self.assertFalse(self.pause.applied)
        self.assertTrue(self.lan.running)
        self.assertTrue(sharedinput.controlling_remote(self.state))  # the guard still stays away
        self.pause.set(True, wait=True)  # the very next poll: not hammered
        self.assertEqual(self.lan.calls, ["stop"])
        self.lan.stop_fails = None
        self.now[0] += sharedinput.ControllerPause.RETRY + 1
        self.pause.set(True, wait=True)
        self.assertTrue(self.pause.applied)
        self.assertFalse(self.lan.running)
        self.assertEqual(self.lan.calls, ["stop", "stop"])
        self.pause.set(False, wait=True)  # and the end brings it back, since it was running
        self.assertEqual(self.lan.calls, ["stop", "stop", "start"])
        self.assertFalse(sharedinput.controlling_remote(self.state))

    def test_a_stop_that_did_not_take_is_not_applied(self):
        for how in ("stays", "refused"):
            with self.subTest(how):
                self.lan.running, self.lan.calls = True, []
                pause = sharedinput.ControllerPause(self.state, self.lan.status, self.lan.start, self.lan.stop,
                                                    clock=lambda: self.now[0])
                self.lan.stop_fails = how
                pause.set(True, wait=True)
                self.assertFalse(pause.applied)
                pause.set(False, wait=True)
                self.assertFalse(sharedinput.controlling_remote(self.state))  # an end clears the marker
                self.assertEqual(self.lan.calls, ["stop"])  # still running: nothing to start

    def test_lan_mouse_started_again_mid_session_is_stopped_again(self):
        self.pause.set(True, wait=True)
        self.assertTrue(self.pause.applied)
        self.lan.running = True  # something (a guard tick that already chose "start") started it
        self.now[0] += sharedinput.ControllerPause.TOUCH + 1
        self.pause.set(True, wait=True)
        self.assertFalse(self.lan.running)
        self.assertEqual(self.lan.calls, ["stop", "stop"])
        self.assertTrue(self.pause.applied)

    def test_failed_stop_then_end_leaves_no_marker_and_no_stray_start(self):
        self.lan.stop_fails = OSError("nope")
        self.pause.set(True, wait=True)
        self.pause.set(False, wait=True)
        self.assertFalse(sharedinput.controlling_remote(self.state))
        self.assertEqual(self.lan.calls, ["stop"])  # it is still running: never started a second copy


class GuardMarkerRaceTests(unittest.TestCase):
    """The guard decided to start Lan Mouse, then the console connected before start() ran."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.lan = FakeLan(running=False)
        memo = {"stopped_by_guard": True, "stopped_at": 0, "together": sharedinput.GUARD_STREAK,
                "apart": 0, "override": False}
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(memo))
        self.near = lambda: {"near": True, "peer": "p", "why": "near"}

    def write_marker(self):
        (self.state / sharedinput.CONTROLLING_MARKER).write_text(json.dumps({"was_running": True}))

    def guard(self):
        return sharedinput.guard(self.state, nearby=self.near, status_fn=self.lan.status,
                                 start_fn=self.lan.start, stop_fn=self.lan.stop)

    def memo(self):
        return json.loads((self.state / sharedinput.GUARD_FILE).read_text())

    def test_baseline_starts_when_nothing_controls(self):
        out = self.guard()
        self.assertEqual(out["action"], "started")
        self.assertEqual(self.lan.calls, ["start"])

    def test_the_marker_written_after_the_decision_stops_the_start(self):
        real = sharedinput._save_memo

        def save(path, memo):
            if memo.get("pending_start"):
                self.write_marker()  # the console connects right after the decision, before start()
            return real(path, memo)
        with mock.patch.object(sharedinput, "_save_memo", save):
            out = self.guard()
        self.assertEqual(self.lan.calls, [])  # never started
        self.assertIsNone(out["action"])
        self.assertTrue(self.memo()["stopped_by_guard"])  # the debt stays: it comes back after
        self.assertNotIn("pending_start", self.memo())

    def test_the_marker_written_while_start_runs_puts_it_straight_back_down(self):
        def start():
            self.lan.start()
            self.write_marker()  # the console connected while systemctl start was running
        self.lan_start = start
        out = sharedinput.guard(self.state, nearby=self.near, status_fn=self.lan.status,
                                start_fn=start, stop_fn=self.lan.stop)
        self.assertEqual(self.lan.calls, ["start", "stop"])
        self.assertFalse(self.lan.running)
        self.assertEqual(out["action"], "start_failed")
        self.assertTrue(self.memo()["stopped_by_guard"])


if __name__ == "__main__":
    unittest.main()
