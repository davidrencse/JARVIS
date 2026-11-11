"""While David drives a node from the main's console, the main keeps its own mouse and
keyboard: its Lan Mouse is paused for the session and comes back after (not while apart)."""
import json
import tempfile
import unittest
from pathlib import Path

from jarvis import sharedinput
from jarvis.remote_view import Sessions


class FakeLanMouse:
    def __init__(self, running=True):
        self.running, self.calls = running, []

    def status(self):
        return {"running": self.running}

    def start(self):
        self.calls.append("start")
        self.running = True

    def stop(self):
        self.calls.append("stop")
        self.running = False


class ControllerPauseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.lm = FakeLanMouse()
        self.now = [0.0]
        self.pause = sharedinput.ControllerPause(self.state, self.lm.status, self.lm.start, self.lm.stop,
                                                 clock=lambda: self.now[0])

    def tearDown(self):
        self.dir.cleanup()

    def test_session_pauses_and_restores(self):
        self.pause.set(True, wait=True)
        self.assertEqual(self.lm.calls, ["stop"])
        self.assertTrue(sharedinput.controlling_remote(self.state))
        self.pause.set(False, wait=True)
        self.assertEqual(self.lm.calls, ["stop", "start"])
        self.assertFalse(sharedinput.controlling_remote(self.state))

    def test_not_running_stays_off(self):
        self.lm.running = False
        self.pause.set(True, wait=True)
        self.pause.set(False, wait=True)
        self.assertEqual(self.lm.calls, [])

    def test_no_restart_while_apart(self):
        self.pause.set(True, wait=True)
        memo = json.loads((self.state / sharedinput.GUARD_FILE).read_text())
        memo["apart_now"] = True
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(memo))
        self.pause.set(False, wait=True)
        self.assertEqual(self.lm.calls, ["stop"])
        # the guard brings it back once together: it counts as its own stop
        self.assertTrue(json.loads((self.state / sharedinput.GUARD_FILE).read_text())["stopped_by_guard"])

    def test_repeated_live_calls_stop_once(self):
        for _ in range(5):
            self.pause.set(True, wait=True)
        self.now[0] += 20
        self.pause.set(True, wait=True)
        self.assertEqual(self.lm.calls, ["stop"])

    def test_guard_does_not_restart_during_session(self):
        self.pause.set(True, wait=True)
        out = sharedinput.guard(self.state, nearby=lambda: {"near": True, "peer": "p", "why": "near"},
                                status_fn=self.lm.status, start_fn=self.lm.start, stop_fn=self.lm.stop)
        out = sharedinput.guard(self.state, nearby=lambda: {"near": True, "peer": "p", "why": "near"},
                                status_fn=self.lm.status, start_fn=self.lm.start, stop_fn=self.lm.stop)
        self.assertIsNone(out["action"])
        self.assertEqual(self.lm.calls, ["stop"])

    def test_failure_never_raises(self):
        def boom():
            raise OSError("systemctl gone")
        pause = sharedinput.ControllerPause(self.state, self.lm.status, boom, boom)
        pause.set(True, wait=True)
        pause.set(False, wait=True)

    def test_quick_open_close_still_restores(self):
        # Connect, then an instant failure: set(False) lands while apply() is mid-stop
        import threading
        gate = threading.Event()
        lm = self.lm

        def slow_status():
            gate.wait(2)
            return lm.status()
        pause = sharedinput.ControllerPause(self.state, slow_status, lm.start, lm.stop)
        pause.set(True)
        import time
        time.sleep(0.05)
        pause.set(False)
        gate.set()
        for _ in range(100):
            if lm.calls == ["stop", "start"]:
                break
            time.sleep(0.02)
        self.assertEqual(lm.calls, ["stop", "start"])
        self.assertFalse(pause.applied)

    def test_restore_is_not_an_override(self):
        # after the session, the guard must still stop Lan Mouse when the laptops part
        self.pause.set(True, wait=True)
        self.pause.set(False, wait=True)
        apart = lambda: {"near": False, "peer": "p", "why": "apart"}
        for _ in range(2):
            out = sharedinput.guard(self.state, nearby=apart, status_fn=self.lm.status,
                                    start_fn=self.lm.start, stop_fn=self.lm.stop, now=10_000)
        self.assertEqual(out["action"], "stopped")
        self.assertFalse(out["override"])


class SessionsBusyTests(unittest.TestCase):
    def test_open_and_close_report_busy(self):
        seen = []
        s = Sessions(on_busy=seen.append)
        view = s.open("dell")
        self.assertEqual(seen[-1], True)
        s.close(view["id"])
        self.assertEqual(seen[-1], False)

    def test_expiry_reports_not_busy(self):
        now = [0.0]
        seen = []
        s = Sessions(clock=lambda: now[0], on_busy=seen.append)
        s.open("dell")
        now[0] += 10_000
        s.current()
        self.assertEqual(seen[-1], False)

    def test_callback_error_does_not_break_sessions(self):
        def boom(_):
            raise RuntimeError("x")
        s = Sessions(on_busy=boom)
        view = s.open("dell")
        s.close(view["id"])


if __name__ == "__main__":
    unittest.main()
