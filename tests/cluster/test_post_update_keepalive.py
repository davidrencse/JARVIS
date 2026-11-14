"""Post-update keep-alive: fakes only. No clipboard, Lan Mouse, or processes."""
import unittest
from unittest.mock import Mock, patch

from jarvis import _under_tests
from jarvis import cluster


class PostUpdateKeepaliveTests(unittest.TestCase):
    def test_run_calls_ensure_dependents_once_after_delay(self):
        state, config = "state-dir", {"clipboard": {"autostart": True}}
        with patch("jarvis.cluster_recovery.ensure_dependents") as ensure, \
             patch("jarvis.cluster.time.sleep") as sleep:
            cluster._run_keepalive_after_update(state, config, None, 0)
        sleep.assert_called_once_with(0)
        ensure.assert_called_once_with(config, state)

        stop = Mock()
        with patch("jarvis.cluster_recovery.ensure_dependents") as ensure, \
             patch("jarvis.cluster.time.sleep") as sleep:
            cluster._run_keepalive_after_update(state, None, stop, 0)
        stop.wait.assert_called_once_with(0)
        sleep.assert_not_called()
        ensure.assert_called_once_with({}, state)

    def test_ensure_exception_is_printed_not_raised(self):
        with patch("jarvis.cluster_recovery.ensure_dependents", side_effect=RuntimeError("boom")), \
             patch("builtins.print") as printed:
            cluster._run_keepalive_after_update("state-dir", {}, None, 0)
        printed.assert_called_once()
        self.assertEqual(printed.call_args.args[0],
                         "JARVIS node: post-update keep-alive failed: boom")

    def test_starter_does_nothing_under_tests(self):
        self.assertTrue(_under_tests())
        with patch("jarvis.cluster.threading.Thread") as thread, \
             patch("jarvis.cluster_recovery.ensure_dependents") as ensure, \
             patch("jarvis.cluster._run_keepalive_after_update") as body:
            cluster._keepalive_after_update("state-dir", {"c": 1}, None, 0)
        thread.assert_not_called()
        ensure.assert_not_called()
        body.assert_not_called()


class RealEnsureTests(unittest.TestCase):
    def _run(self, config, pid, hand, polls=True):
        state = "state-dir"
        with patch("jarvis.clipsync._pid", return_value=pid) as pid_fn, \
             patch("jarvis.clipsync.stopped_by_hand", return_value=hand) as hand_fn, \
             patch("jarvis.clipsync.start") as start, \
             patch("jarvis.sharedinput.guard_for") as guard:
            cluster._run_keepalive_after_update(state, config, None, 0)
        if polls:
            pid_fn.assert_called_with(str(state))
        else:
            pid_fn.assert_not_called()
        return start, guard, hand_fn

    def test_starts_clipboard_when_down_and_not_stopped_by_hand(self):
        start, guard, hand = self._run({}, None, False)
        hand.assert_called_with("state-dir")
        start.assert_called_once_with("state-dir")
        guard.assert_called_once_with({}, "state-dir")

    def test_does_not_start_clipboard_stopped_by_hand(self):
        start, guard, _hand = self._run({}, None, True)
        start.assert_not_called()
        guard.assert_called_once_with({}, "state-dir")

    def test_guard_for_only_when_off_when_apart(self):
        _start, guard, _hand = self._run({}, 123, False)
        guard.assert_called_once_with({}, "state-dir")
        off = {"lan_mouse": {"off_when_apart": False}}
        start, guard, _hand = self._run(off, None, False)
        start.assert_called_once_with("state-dir")
        guard.assert_not_called()

    def test_clipboard_autostart_off_and_guard_off_start_nothing(self):
        both = {"clipboard": {"autostart": False}, "lan_mouse": {"off_when_apart": False}}
        start, guard, _hand = self._run(both, None, False, polls=False)
        start.assert_not_called()
        guard.assert_not_called()

    def test_running_clipboard_is_left_alone(self):
        start, _guard, _hand = self._run({}, 123, False)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
