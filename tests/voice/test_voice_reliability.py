"""Voice listener supervision and the hub's lazy heavy parts."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis import daemon
from jarvis.chatgpt import Brain

HOLD = ("import sys, time\nfrom jarvis.daemon import InstanceLock\n"
        "with InstanceLock(sys.argv[1]):\n    print('held', flush=True)\n    time.sleep(30)\n")


class ListenerLockTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_a_stale_pid_file_is_not_a_listener(self):
        (self.state / "voice.pid").write_text(str(os.getpid()))  # alive, but holds no lock
        (self.state / "voice.lock").touch()
        with patch("jarvis.daemon._is_listener", return_value=True):
            self.assertIsNone(daemon._pid(self.state))

    @unittest.skipIf(os.name == "nt", "flock")
    def test_lock_is_seen_while_held_and_gone_once_the_holder_dies(self):
        proc = subprocess.Popen([sys.executable, "-c", HOLD, str(self.state)],
                                stdout=subprocess.PIPE, text=True,
                                cwd=Path(__file__).resolve().parents[2])
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            self.assertTrue(daemon.lock_held(self.state))
            (self.state / "voice.pid").write_text(str(proc.pid))
            with patch("jarvis.daemon._is_listener", return_value=True):
                self.assertEqual(daemon._pid(self.state), proc.pid)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
        self.assertFalse(daemon.lock_held(self.state))
        with patch("jarvis.daemon._is_listener", return_value=True):
            self.assertIsNone(daemon._pid(self.state))

    def test_a_restart_waits_for_the_old_listener_to_let_go(self):
        first = daemon.InstanceLock(self.state).__enter__()
        import threading
        hold = 0.12

        def let_go():
            time.sleep(hold)
            first.__exit__(None, None, None)

        holder = threading.Thread(target=let_go)
        holder.start()
        self.addCleanup(holder.join)
        started = time.monotonic()
        # The lock polls every 0.5 s. Cap that poll for this test; the wait itself stays real.
        real_sleep = time.sleep
        with patch.object(daemon.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.02))), \
                daemon.InstanceLock(self.state, wait=2):
            waited = time.monotonic() - started
        holder.join(2)
        self.assertGreater(waited, hold / 2)  # it blocked until the holder let go

    def test_a_busy_lock_still_refuses_without_wait(self):
        with daemon.InstanceLock(self.state):
            with self.assertRaises(RuntimeError):
                daemon.InstanceLock(self.state).__enter__()


class SupervisedStartTests(unittest.TestCase):
    def test_start_and_stop_go_through_the_unit(self):
        state = Path(tempfile.mkdtemp())
        (state / "voice").mkdir()
        (state / "voice" / "voiceprint.json").write_text("{}")
        with patch("jarvis.daemon._supervised", return_value=True), \
                patch("jarvis.daemon._systemctl", return_value=True) as systemctl, \
                patch("jarvis.daemon._pid", side_effect=[None, 4242, 4242]), \
                patch("jarvis.daemon.time.sleep"), \
                patch("jarvis.daemon.subprocess.Popen") as popen:
            self.assertTrue(daemon.start(state)["started"])
        systemctl.assert_any_call("start", daemon.UNIT_NAME)
        popen.assert_not_called()
        with patch("jarvis.daemon._supervised", return_value=True), \
                patch("jarvis.daemon._systemctl", return_value=True) as systemctl, \
                patch("jarvis.daemon._pid", return_value=4242), \
                patch("jarvis.daemon.os.kill") as kill:
            self.assertTrue(daemon.stop(state)["stopped"])
        systemctl.assert_any_call("stop", daemon.UNIT_NAME)
        kill.assert_not_called()


class LazyHeavyPartsTests(unittest.TestCase):
    def test_brain_warm_leaves_the_browser_lane_cold(self):
        chat, act = Mock(), Mock()
        Brain(chat, act).warm()
        chat.warm.assert_called_once()
        act.warm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
