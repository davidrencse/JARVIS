"""Console overview TTL: one-second cache, a copy for each caller, and clear-during-compute."""

import unittest
from types import SimpleNamespace

from jarvis import ui


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class TtlRaceTests(unittest.TestCase):
    def test_clear_during_compute_does_not_restore_the_stale_value(self):
        cache, calls = ui._Ttl(Clock()), []

        def compute():
            calls.append(1)
            if len(calls) == 1:
                cache.clear()
            return len(calls)

        self.assertEqual(cache.get("k", 12, compute), 1)
        self.assertEqual(cache.get("k", 12, compute), 2)  # the cleared value was not stored
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.get("k", 12, compute), 2)
        self.assertEqual(len(calls), 2)


class OverviewCacheTests(unittest.TestCase):
    def overview(self, clock, calls, clear_on_first=False):
        stub = SimpleNamespace(ttl=ui._Ttl(clock))

        def compute():
            calls.append(clock.now)
            if clear_on_first and len(calls) == 1:
                stub.ttl.clear()
            return {"n": len(calls), "devices": [{"id": "dell"}]}

        stub._overview = compute
        return stub

    def test_cached_within_ttl_cleared_and_not_mutated_by_the_caller(self):
        clock, calls = Clock(), []
        stub = self.overview(clock, calls)
        first = ui.Console.overview(stub)
        first["n"] = 99
        first["devices"].append({"id": "other"})
        clock.now = 0.5
        second = ui.Console.overview(stub)
        self.assertEqual(second, {"n": 1, "devices": [{"id": "dell"}]})
        self.assertIsNot(first, second)
        self.assertEqual(len(calls), 1)
        stub.ttl.clear()
        self.assertEqual(ui.Console.overview(stub)["n"], 2)
        clock.now = 1.5  # stored at 0.5; age 1.0 is a miss
        self.assertEqual(ui.Console.overview(stub)["n"], 3)
        self.assertEqual(len(calls), 3)

    def test_clear_during_overview_compute_is_not_cached(self):
        clock, calls = Clock(), []
        stub = self.overview(clock, calls, clear_on_first=True)
        self.assertEqual(ui.Console.overview(stub)["n"], 1)
        self.assertEqual(ui.Console.overview(stub)["n"], 2)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
