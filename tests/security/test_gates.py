"""L3 gates: the checks a command passes before it is sent, and the in-flight guard."""

import time
import unittest
from unittest import mock

from jarvis import gates

DEVICES = {
    "dell": {"hostname": "WinLaptop", "aliases": ["windows"]},
    "zenbook": {"hostname": "archmain", "aliases": ["arch"]},
}


def cmd(intent="open_app", args=None, device="dell", cid="c1"):
    return {"id": cid, "intent": intent, "device": device,
            "args": args if args is not None else {"name": "Chrome"}, "depends_on": []}


def names(verdict):
    return [c["name"] for c in verdict["checks"]]


class CheckTests(unittest.TestCase):
    def test_valid_allow_command_passes_with_ordered_checks(self):
        v = gates.check(cmd(), DEVICES, reachable=lambda d: True,
                        ready=lambda e, d: True, app_exists=lambda d, n: True)
        self.assertTrue(v["pass"])
        self.assertEqual(v["risk"], "allow")
        self.assertEqual(names(v), [
            "U1 schema_valid", "U2 device_resolved", "U3 device_reachable",
            "U4 executor_ready", "U5 args_safe", "U6 target_exists", "U9 loop_free",
            "U10 success_defined", "policy"])
        self.assertTrue(all(c["pass"] for c in v["checks"]))

    def test_schema_failure_u1(self):
        v = gates.check(cmd(args={}), DEVICES)
        self.assertFalse(v["pass"])
        self.assertEqual(names(v), ["U1 schema_valid"])
        self.assertFalse(v["checks"][0]["pass"])
        self.assertIn("needs name", v["why"])

    def test_unknown_device_fails_u1(self):
        v = gates.check(cmd(device="mars"), DEVICES)
        self.assertFalse(v["pass"])
        self.assertEqual(v["checks"][-1]["name"], "U1 schema_valid")

    def test_unreachable_u3(self):
        v = gates.check(cmd(), DEVICES, reachable=lambda d: False)
        self.assertFalse(v["pass"])
        self.assertEqual(v["checks"][-1]["name"], "U3 device_reachable")
        self.assertIn("isn't reachable", v["why"])

    def test_reachable_receives_device(self):
        seen = []
        gates.check(cmd(device="zenbook"), DEVICES,
                    reachable=lambda d: seen.append(d) or True)
        self.assertEqual(seen, ["zenbook"])

    def test_not_ready_u4(self):
        v = gates.check(cmd(), DEVICES, ready=lambda e, d: False)
        self.assertFalse(v["pass"])
        self.assertEqual(v["checks"][-1]["name"], "U4 executor_ready")
        self.assertIn("open_app", v["why"])

    def test_app_missing_u6_fails(self):
        v = gates.check(cmd(), DEVICES, app_exists=lambda d, n: False)
        self.assertFalse(v["pass"])
        self.assertEqual(v["checks"][-1]["name"], "U6 target_exists")
        self.assertIn("isn't installed", v["why"])

    def test_app_unknown_none_is_left_to_the_node(self):
        v = gates.check(cmd(), DEVICES, app_exists=lambda d, n: None)
        self.assertTrue(v["pass"])
        u6 = [c for c in v["checks"] if c["name"] == "U6 target_exists"][0]
        self.assertTrue(u6["pass"])
        self.assertEqual(u6["why"], "left to the node")

    def test_u6_skipped_for_other_intents(self):
        called = []
        v = gates.check(cmd("media", {"command": "pause"}), DEVICES,
                        app_exists=lambda d, n: called.append(1) or False)
        self.assertTrue(v["pass"])
        self.assertEqual(called, [])
        self.assertNotIn("U6 target_exists", names(v))

    def test_agent_and_system_origin_blocked_on_effects_u9(self):
        for origin in ("agent", "system"):
            with self.subTest(origin=origin):
                v = gates.check(cmd(), DEVICES, origin_class=origin)
                self.assertFalse(v["pass"])
                self.assertEqual(v["checks"][-1]["name"], "U9 loop_free")

    def test_owner_allowed_u9(self):
        self.assertTrue(gates.check(cmd(), DEVICES, origin_class="owner")["pass"])

    def test_agent_may_run_non_effect_intent(self):
        v = gates.check(cmd("list_apps", {"query": "chrome"}), DEVICES, origin_class="agent")
        self.assertTrue(v["pass"], v["why"])

    def test_close_non_safe_app_needs_confirmation(self):
        v = gates.check(cmd("close_app", {"name": "Notepad"}), DEVICES)
        self.assertFalse(v["pass"])
        self.assertEqual(v["risk"], "confirm")
        self.assertEqual(v["why"], "needs your OK first")
        self.assertEqual(v["checks"][-1]["name"], "policy")
        self.assertFalse(v["checks"][-1]["pass"])

    def test_close_spotify_is_allowed(self):
        v = gates.check(cmd("close_app", {"name": "Spotify"}), DEVICES)
        self.assertTrue(v["pass"], v["why"])
        self.assertEqual(v["risk"], "allow")


class InFlightTests(unittest.TestCase):
    def test_second_identical_claim_refused(self):
        f = gates.InFlight()
        self.assertTrue(f.claim(cmd()))
        self.assertFalse(f.claim(cmd(cid="c9")))  # id is not part of the key

    def test_different_command_allowed(self):
        f = gates.InFlight()
        self.assertTrue(f.claim(cmd()))
        self.assertTrue(f.claim(cmd(args={"name": "Spotify"})))
        self.assertTrue(f.claim(cmd(device="zenbook")))

    def test_release_keeps_merge_window(self):
        f = gates.InFlight()
        c = cmd()
        self.assertTrue(f.claim(c))
        f.release(c)
        self.assertFalse(f.claim(c))

    def test_claim_allowed_after_window(self):
        f = gates.InFlight()
        c = cmd()
        base = time.monotonic()
        with mock.patch.object(gates.time, "monotonic", return_value=base):
            self.assertTrue(f.claim(c))
            f.release(c)
        with mock.patch.object(gates.time, "monotonic",
                               return_value=base + gates.DUPLICATE_WINDOW + 0.1):
            self.assertTrue(f.claim(c))

    def test_slow_run_blocks_repeat_until_it_finishes(self):
        # "open spotify on arch" takes 3-45 s; a double trigger 2.5 s in must not resend.
        f = gates.InFlight()
        c = cmd()
        base = time.monotonic()
        with mock.patch.object(gates.time, "monotonic", return_value=base):
            self.assertTrue(f.claim(c, "r1"))
        with mock.patch.object(gates.time, "monotonic", return_value=base + 45):
            self.assertFalse(f.claim(c, "r2"))
            f.release(c, True, "r1")
            self.assertFalse(f.claim(c, "r2"))  # still inside the after-finish window
        with mock.patch.object(gates.time, "monotonic",
                               return_value=base + 45 + gates.DUPLICATE_WINDOW + 0.1):
            self.assertTrue(f.claim(c, "r2"))

    def test_unreleased_run_stops_blocking_after_cap(self):
        f = gates.InFlight()
        c = cmd()
        base = time.monotonic()
        with mock.patch.object(gates.time, "monotonic", return_value=base):
            self.assertTrue(f.claim(c, "r1"))
        with mock.patch.object(gates.time, "monotonic",
                               return_value=base + gates.RUNNING_CAP + 0.1):
            self.assertTrue(f.claim(c, "r2"))


if __name__ == "__main__":
    unittest.main()
