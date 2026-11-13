"""`jarvis sleep` / `jarvis wake` (rest.py): every systemctl call is faked."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import rest


class FakeSystemd:
    def __init__(self, running, fail=()):
        self.running, self.fail, self.calls = set(running), set(fail), []

    def user(self, *args, timeout=30):
        self.calls.append(args)
        verb, unit = args[0], args[-1]
        if verb == "is-active":
            return (0 if unit in self.running else 3), ""
        if unit in self.fail:
            return 1, "Job failed"
        if verb == "stop":
            self.running.discard(unit)
        elif verb == "start":
            self.running.add(unit)
        return 0, ""


class RestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        for p in (mock.patch.object(rest, "supported", return_value=True),):
            p.start()
            self.addCleanup(p.stop)

    def systemd(self, running, blockers=(), fail=()):
        fake = FakeSystemd(running, fail)
        for p in (mock.patch.object(rest, "_user", fake.user),
                  mock.patch.object(rest, "sleep_blockers", return_value=list(blockers))):
            p.start()
            self.addCleanup(p.stop)
        return fake


class PauseWakeTests(RestCase):
    def test_pause_stops_running_jarvis_units_and_blockers_and_wake_restores_in_reverse(self):
        fake = self.systemd({"lan-mouse-ipwatch.service", "lan-mouse.service", "jarvis-voice.service",
                             "jarvis-cluster.service", "instabox-listener.service", "other.service"},
                            blockers=[("instabox-listener.service", "instabox", "24x7")])
        out = rest.pause(self.state)
        self.assertEqual(out["stopped"], ["lan-mouse-ipwatch.service", "lan-mouse.service",
                                          "jarvis-voice.service", "jarvis-cluster.service",
                                          "instabox-listener.service"])
        self.assertEqual(fake.running, {"other.service"})  # never touches what it didn't list
        stops = [c[1] for c in fake.calls if c[0] == "stop"]
        self.assertEqual(stops[0], "lan-mouse-ipwatch.service")  # the guard first, or it restarts Lan Mouse
        back = rest.wake(self.state)
        self.assertEqual(back["started"], list(reversed(out["stopped"])))
        self.assertIn("jarvis-cluster.service", fake.running)
        self.assertFalse((self.state / rest.RECORD).exists())

    def test_units_not_running_are_not_started_on_wake(self):
        fake = self.systemd({"jarvis-cluster.service"})  # David stopped Lan Mouse himself
        rest.pause(self.state)
        rest.wake(self.state)
        self.assertNotIn("lan-mouse.service", fake.running)

    def test_wake_without_pause_is_harmless(self):
        self.systemd(set())
        self.assertFalse(rest.wake(self.state)["woke"])

    def test_a_failed_start_keeps_the_record_for_another_try(self):
        self.systemd({"jarvis-voice.service"}, fail={"jarvis-voice.service"})
        with mock.patch.object(rest, "_user") as user:
            user.side_effect = lambda *a, timeout=30: (0, "") if a[0] != "is-active" else (0, "")
            rest.pause(self.state)
        self.systemd(set(), fail={"jarvis-voice.service"})
        out = rest.wake(self.state)
        self.assertTrue(out["failed"])
        self.assertTrue((self.state / rest.RECORD).exists())

    def test_two_pauses_keep_the_first_ones_units(self):
        self.systemd({"jarvis-voice.service"})
        rest.pause(self.state)
        rest.pause(self.state)  # voice already stopped: still restored on wake
        record = json.loads((self.state / rest.RECORD).read_text())
        self.assertEqual(record["stopped"], ["jarvis-voice.service"])

    def test_a_blocker_stopped_by_the_first_pause_is_restored_after_a_second_one(self):
        # `jarvis sleep --no-suspend` twice: the Instabox unit is already down for the second
        # pause (and may no longer read as a blocker), but wake must still bring it back.
        unit = "instabox-listener.service"
        fake = self.systemd({unit}, blockers=[(unit, "instabox", "24x7")])
        rest.pause(self.state)
        self.assertNotIn(unit, fake.running)
        with mock.patch.object(rest, "sleep_blockers", return_value=[]):
            rest.pause(self.state)
        record = json.loads((self.state / rest.RECORD).read_text())
        self.assertIn(unit, record["stopped"])
        self.assertIn(unit, [b["unit"] for b in record["blockers"]])
        out = rest.wake(self.state)
        self.assertIn(unit, out["started"])
        self.assertIn(unit, fake.running)

    def test_record_cannot_start_arbitrary_text(self):
        fake = self.systemd(set())
        (self.state / rest.RECORD).write_text(json.dumps({"stopped": ["x; rm -rf ~", 5, "evil.service", "jarvis-voice.service"]}))
        rest.wake(self.state)
        self.assertEqual([c[1] for c in fake.calls if c[0] == "start"], ["jarvis-voice.service"])


class SleepTests(RestCase):
    def test_sleep_arms_the_restore_then_suspends(self):
        self.systemd({"jarvis-voice.service"})
        order = []
        out = rest.sleep(self.state, self.state, launch=lambda: order.append("arm") or (0, ""),
                         request=lambda: order.append("suspend") or (0, ""))
        self.assertEqual(order, ["arm", "suspend"])
        self.assertEqual(out["sleeping"], "suspend-then-hibernate")

    def test_refused_suspend_brings_jarvis_straight_back(self):
        fake = self.systemd({"jarvis-voice.service", "jarvis-cluster.service"})
        with self.assertRaisesRegex(RuntimeError, "back on"):
            rest.sleep(self.state, self.state, launch=lambda: (0, ""),
                       request=lambda: (1, "Operation inhibited"))
        self.assertEqual(fake.running, {"jarvis-voice.service", "jarvis-cluster.service"})

    def test_unarmed_restore_never_suspends(self):
        fake = self.systemd({"jarvis-voice.service"})
        asked = []
        with self.assertRaises(RuntimeError):
            rest.sleep(self.state, self.state, launch=lambda: (1, "no systemd-run"),
                       request=lambda: asked.append(1) or (0, ""))
        self.assertEqual(asked, [])
        self.assertIn("jarvis-voice.service", fake.running)

    def test_no_suspend_only_pauses(self):
        self.systemd({"jarvis-voice.service"})
        out = rest.sleep(self.state, self.state, suspend=False,
                         launch=lambda: self.fail("armed"), request=lambda: self.fail("suspended"))
        self.assertIn("jarvis wake", out["note"])

    def test_unknown_action_refused(self):
        with self.assertRaises(ValueError):
            rest.sleep(self.state, self.state, action="poweroff")


class ResumeDetectionTests(unittest.TestCase):
    def test_resume_is_the_boottime_gap_growing(self):
        ticks = iter([(100.0, 50.0), (101.0, 51.0), (700.0, 52.0)])  # 597 s asleep
        self.assertTrue(rest.wait_for_resume(50.0, clocks=lambda: next(ticks), sleep=lambda s: None))

    def test_gives_up_when_the_pc_never_slept(self):
        clock = [0.0]

        def clocks():
            clock[0] += 1
            return 1000 + clock[0], clock[0]
        self.assertFalse(rest.wait_for_resume(1000.0, limit=10, clocks=clocks, sleep=lambda s: None))


class BlockerTests(unittest.TestCase):
    def test_only_this_users_block_sleep_locks_count(self):
        uid = os.getuid()
        locks = [("sleep", "NetworkManager", "x", "delay", 0, 1),
                 ("sleep", "hypridle", "x", "delay", uid, 2),
                 ("sleep", "instabox", "24x7", "block", uid, 3),
                 ("shutdown", "foo", "x", "block", uid, 4),
                 ("sleep:idle", "root thing", "x", "block", 0, 5),
                 ("idle:sleep", "lan-mouse", "x", "block", uid, 6)]
        units = {3: "instabox-listener.service", 4: "foo.service", 6: "lan-mouse.service"}
        with mock.patch.object(rest, "_unit_of", side_effect=lambda pid: units.get(pid)):
            self.assertEqual(rest.sleep_blockers(locks), [("instabox-listener.service", "instabox", "24x7")])


if __name__ == "__main__":
    unittest.main()
