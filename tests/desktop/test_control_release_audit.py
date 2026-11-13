"""Audit 2026-11-13: letting go of held input (control.py). Nothing real is pressed: subprocess
and the remote sessions are fakes."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import control, remote_view


class HyprlandReleaseTests(unittest.TestCase):
    """A wtype killed after a modifier-down (the 15 s timeout) must not leave Ctrl/Alt/Super down."""

    def run_release(self, side_effect=None):
        calls = []

        def fake_run(args, **kw):
            calls.append((list(args), kw))
            if side_effect:
                side_effect(args)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with mock.patch.object(control.subprocess, "run", fake_run):
            control.Hyprland().release()
        return calls

    def test_release_lifts_the_buttons_and_every_modifier(self):
        calls = self.run_release()
        argvs = [a for a, _ in calls]
        self.assertIn(["ydotool", "click", "0x80"], argvs)  # left up
        self.assertIn(["ydotool", "click", "0x81"], argvs)  # right up
        wtype = next(a for a in argvs if a[0] == "wtype")
        for mod in ("ctrl", "shift", "alt", "logo"):
            self.assertIn(mod, wtype)
        self.assertEqual(wtype.count("-m"), 4)  # -m is release, never -M (press)
        self.assertNotIn("-M", wtype)
        keys = next(a for a in argvs if a[:2] == ["ydotool", "key"])
        up = {k for k in keys[2:]}
        for name in ("ControlLeft", "ControlRight", "ShiftLeft", "ShiftRight", "AltLeft", "AltRight",
                     "MetaLeft", "MetaRight"):
            self.assertIn(f"{remote_view.KEYS[name][2]}:0", up, name)
        self.assertTrue(all(k.endswith(":0") for k in keys[2:]))  # only ups, never a press

    def test_release_never_hangs_or_raises_when_tools_are_missing_or_stuck(self):
        for failure in (FileNotFoundError("wtype"), subprocess.TimeoutExpired("wtype", 3),
                        subprocess.CalledProcessError(1, "ydotool")):
            def boom(args, failure=failure):
                raise failure
            calls = self.run_release(boom)
            self.assertEqual(len(calls), 4, failure)  # one failure never skips the rest
            self.assertTrue(all(kw["timeout"] <= 3 for _, kw in calls), calls)


class StopReleasesRemoteKeysTests(unittest.TestCase):
    def test_stop_lets_go_of_the_keys_a_remote_session_still_holds(self):
        held = []

        def session():
            class Session:
                class injector:
                    @staticmethod
                    def release(backend=True):
                        held.append(backend)
            return Session
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(control, "_release"), \
                mock.patch.dict(remote_view._ACTIVE, {"s": session()}), \
                mock.patch.dict(remote_view._LATE, {"l": session()}, clear=False):
            ok, _ = control.stop(Path(tmp))
        self.assertTrue(ok)
        self.assertEqual(held, [False, False])  # the session's own ups; the sweep above is the backend's

    def test_stop_still_stops_when_the_remote_release_breaks(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(control, "_release"), \
                mock.patch.object(remote_view, "release_held", side_effect=RuntimeError("boom")):
            ok, _ = control.stop(Path(tmp))
            self.assertTrue(ok)
            self.assertTrue(control._stopped(tmp))


if __name__ == "__main__":
    unittest.main()
