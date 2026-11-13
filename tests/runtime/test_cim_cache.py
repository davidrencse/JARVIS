"""TTL cache for the Windows CIM queries. Fakes only: nothing here starts PowerShell.

Call sites that fork that query:
- cluster.py node poll and ui.py Devices both use hostmeter.cached_sample
  (Devices via summarize -> live_if_host)
- inventory.collect is the inventory job and `jarvis inventory`
- inventory._attach_meters calls hostmeter.sample for the live meters
"""

import threading
import time
import unittest
from unittest.mock import patch

from jarvis import hostmeter, inventory


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def meters_ok():
    return {
        "memory": {"ok": True, "total_kb": 1024, "free_kb": 256},
        "pages": {"ok": True, "items": []},
        "disks": {"ok": True, "items": [{"name": "C:", "total": 1000, "free": 400}]},
        "cpu": {"ok": True, "items": [{"load": 3, "cores": 4}]},
        "win32": {"ok": True, "items": []},
    }


def hardware_ok():
    return {
        "computer": {"Manufacturer": "Dell", "Model": "XPS", "TotalPhysicalMemory": 8},
        "cpu": {"Name": "Test CPU", "NumberOfCores": 4},
        "memory": [{"Manufacturer": "Micron", "Capacity": 8}],
        "os": {"Caption": "Windows", "Version": "10"},
        "bios": {"SMBIOSBIOSVersion": "1.0"},
        "gpus": [{"Name": "Intel"}],
        "monitors": [{"FriendlyName": "Panel", "Active": True}],
    }


class CacheTests(unittest.TestCase):
    def setUp(self):
        hostmeter.clear_cache()
        inventory.clear_cache()

    def tearDown(self):
        hostmeter.clear_cache()
        inventory.clear_cache()

    def test_ttls_are_ten_seconds_five_seconds_and_five_minutes(self):
        self.assertEqual(hostmeter._SAMPLE_TTL, 10)
        self.assertEqual(inventory._HARDWARE_TTL, 300)
        self.assertLessEqual(hostmeter._FAIL_TTL, 5)
        self.assertLessEqual(inventory._FAIL_TTL, 5)

    def test_two_meter_reads_inside_ttl_run_powershell_once(self):
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, meters_ok):
            first = hostmeter.sample(clock=clock)
            second = hostmeter.sample(clock=clock)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first["ram"]["total"], second["ram"]["total"])
        self.assertIsNot(first, second)
        self.assertIsNot(first["ram"], second["ram"])

    def test_meter_read_runs_again_after_the_ttl(self):
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, meters_ok):
            hostmeter.sample(clock=clock)
            clock.advance(hostmeter._FAIL_TTL)
            hostmeter.sample(clock=clock)
            self.assertEqual(calls["n"], 1)
            clock.advance(hostmeter._SAMPLE_TTL - hostmeter._FAIL_TTL)
            hostmeter.sample(clock=clock)
        self.assertEqual(calls["n"], 2)

    def test_meter_failure_is_kept_only_for_the_short_ttl(self):
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, lambda: {"status": "unavailable", "reason": "TimeoutExpired"}):
            first = hostmeter.sample(clock=clock)
            second = hostmeter.sample(clock=clock)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(first["reason"], "the reading timed out")
            first["reason"] = "mutated"
            cached = hostmeter.sample(clock=clock)
            self.assertEqual(cached["reason"], "the reading timed out")
            self.assertIsNot(first, cached)
            clock.advance(hostmeter._FAIL_TTL)
            hostmeter.sample(clock=clock)
        self.assertEqual(calls["n"], 2)

    def test_poll_and_devices_share_one_windows_query_and_drop_failures_quickly(self):
        # cached_sample is the node poll and live_if_host (the Devices page).
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, meters_ok):
            first = hostmeter.cached_sample(clock=clock)
            second = hostmeter.cached_sample(clock=clock)
            self.assertEqual(calls["n"], 1)
            first["ram"]["total"] = 0
            third = hostmeter.cached_sample(clock=clock)
            self.assertNotEqual(third["ram"]["total"], 0)
            self.assertIsNot(first["ram"], third["ram"])
            clock.advance(hostmeter._SAMPLE_TTL)
            hostmeter.cached_sample(clock=clock)
        self.assertEqual(calls["n"], 2)
        hostmeter.clear_cache()
        fail = self._counting()
        clock = Clock()
        with self._windows_meters(fail, lambda: {"status": "unavailable", "reason": "TimeoutExpired"}):
            hostmeter.cached_sample(clock=clock)
            clock.advance(hostmeter._FAIL_TTL)
            hostmeter.cached_sample(clock=clock)
        self.assertEqual(fail["n"], 2)

    def test_one_failed_class_still_uses_the_meter_ttl(self):
        def payload():
            reading = meters_ok()
            reading["win32"] = {"ok": False, "error": "TimeoutExpired", "message": "timed out", "items": []}
            return reading
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, payload):
            hostmeter.sample(clock=clock)
            clock.advance(hostmeter._FAIL_TTL)
            hostmeter.sample(clock=clock)
        self.assertEqual(calls["n"], 1)

    def test_concurrent_meter_calls_share_one_powershell(self):
        calls = self._counting()
        entered, release = threading.Event(), threading.Event()

        def runner(timeout):
            self._enter(calls, entered)
            if not release.wait(3):
                raise RuntimeError("runner held past 3s")
            return meters_ok()

        with self._windows_meters(calls, runner=runner):
            results = self._overlap(lambda: hostmeter.sample(clock=Clock()), entered, release, calls)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["ram"]["total"], results[1]["ram"]["total"])
        self.assertIsNot(results[0], results[1])
        self.assertIsNot(results[0]["ram"], results[1]["ram"])

    def test_clear_cache_forces_a_new_meter_read(self):
        clock = Clock()
        calls = self._counting()
        with self._windows_meters(calls, meters_ok):
            hostmeter.sample(clock=clock)
            hostmeter.clear_cache()
            hostmeter.sample(clock=clock)
        self.assertEqual(calls["n"], 2)

    def test_linux_meters_stay_a_direct_read(self):
        with patch("jarvis.hostmeter._use_windows_meters", return_value=False), \
             patch("jarvis.hostmeter.read_linux", return_value={"v": 1, "source": "sysfs", "ram": {"total": 1}}) as read:
            hostmeter.sample(clock=Clock())
            hostmeter.sample(clock=Clock())
            self.assertEqual(read.call_count, 2)
            hostmeter.cached_sample()
            hostmeter.cached_sample()
        self.assertEqual(read.call_count, 3)

    def test_hardware_query_runs_once_inside_ttl_and_nvidia_does_not(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        with self._windows_hardware(calls, hardware_ok):
            first = inventory.collect(clock=clock)
            second = inventory.collect(clock=clock)
        self.assertEqual(calls["ps"], 1)
        self.assertEqual(calls["nv"], 2)
        self.assertEqual(first["hardware"]["computer"]["Manufacturer"], "Dell")
        self.assertIsNot(first["hardware"], second["hardware"])
        self.assertIsNot(first["hardware"]["computer"], second["hardware"]["computer"])

    def test_hardware_query_runs_again_after_the_ttl(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        with self._windows_hardware(calls, hardware_ok):
            inventory.collect(clock=clock)
            clock.advance(inventory._FAIL_TTL)
            inventory.collect(clock=clock)
            self.assertEqual(calls["ps"], 1)
            clock.advance(inventory._HARDWARE_TTL - inventory._FAIL_TTL)
            inventory.collect(clock=clock)
        self.assertEqual(calls["ps"], 2)

    def test_hardware_failure_is_kept_only_briefly_and_a_mutation_does_not_stick(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        with self._windows_hardware(calls, lambda: {"status": "unavailable", "reason": "TimeoutExpired"}):
            first = inventory.collect(clock=clock)
            inventory.collect(clock=clock)
            self.assertEqual(calls["ps"], 1)
            self.assertEqual(first["hardware"]["reason"], "TimeoutExpired")
            first["hardware"]["reason"] = "mutated"
            cached = inventory.collect(clock=clock)
            self.assertEqual(cached["hardware"]["reason"], "TimeoutExpired")
            self.assertIsNot(first["hardware"], cached["hardware"])
            clock.advance(inventory._FAIL_TTL)
            inventory.collect(clock=clock)
        self.assertEqual(calls["ps"], 2)

    def test_non_json_hardware_is_not_held_for_the_long_ttl(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        with self._windows_hardware(calls, lambda: "not json"):
            inventory.collect(clock=clock)
            inventory.collect(clock=clock)
            self.assertEqual(calls["ps"], 1)
            clock.advance(inventory._FAIL_TTL)
            inventory.collect(clock=clock)
        self.assertEqual(calls["ps"], 2)

    def test_concurrent_hardware_calls_share_one_powershell(self):
        calls = {"ps": 0, "nv": 0}
        entered, release = threading.Event(), threading.Event()

        def produce():
            self._enter_key(calls, "ps", entered)
            if not release.wait(3):
                raise RuntimeError("runner held past 3s")
            return hardware_ok()

        with self._windows_hardware(calls, produce, blocking=True):
            results = self._overlap(lambda: inventory.collect(clock=Clock()), entered, release, calls, key="ps")
        self.assertEqual(calls["ps"], 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["hardware"]["computer"]["Manufacturer"], "Dell")
        self.assertIsNot(results[0]["hardware"]["computer"], results[1]["hardware"]["computer"])
        self.assertEqual(calls["nv"], 2)

    def test_meter_failure_does_not_blank_hardware(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        unavailable = {"v": 1, "source": "cim", "applicability": "unavailable",
                       "reason": "the reading timed out"}
        with self._windows_hardware(calls, hardware_ok), \
             patch("jarvis.hostmeter.sample", return_value=unavailable):
            data = inventory.collect(clock=clock)
            again = inventory.collect(clock=clock)
        self.assertEqual(data["hardware"]["computer"]["Manufacturer"], "Dell")
        self.assertEqual(again["hardware"]["computer"]["Manufacturer"], "Dell")
        self.assertEqual(data["meters"]["applicability"], "unavailable")
        self.assertEqual(calls["ps"], 1)

    def test_linux_inventory_is_not_cached(self):
        calls = {"n": 0}

        def fake_command(args):
            calls["n"] += 1
            return {"status": "unavailable"}

        with patch("jarvis.inventory.platform.system", return_value="Linux"), \
             patch("jarvis.inventory.command", side_effect=fake_command), \
             patch("jarvis.inventory.read_text", return_value="x"), \
             patch("jarvis.inventory.linux_displays", return_value=[]), \
             patch("jarvis.hostmeter.sample", return_value={"v": 1, "battery_rows": []}):
            inventory.collect()
            inventory.collect()
        self.assertEqual(calls["n"], 6)

    def test_clear_cache_forces_a_new_hardware_read(self):
        clock = Clock()
        calls = {"ps": 0, "nv": 0}
        with self._windows_hardware(calls, hardware_ok):
            inventory.collect(clock=clock)
            inventory.clear_cache()
            inventory.collect(clock=clock)
        self.assertEqual(calls["ps"], 2)

    def _counting(self):
        return {"n": 0}

    def _enter(self, calls, entered):
        self._enter_key(calls, "n", entered)

    def _enter_key(self, calls, key, entered):
        with calls.setdefault("_lock", threading.Lock()):
            calls[key] += 1
        entered.set()

    def _windows_meters(self, calls, payload=None, runner=None):
        def default_runner(timeout):
            calls["n"] += 1
            return payload()
        return _patched("jarvis.hostmeter._use_windows_meters", "jarvis.hostmeter._run_windows",
                        runner or default_runner)

    def _windows_hardware(self, calls, produce, blocking=False):
        def fake(args):
            if args and args[0] == "powershell.exe":
                if blocking:
                    return produce()
                calls["ps"] += 1
                return produce()
            calls["nv"] += 1
            return "NVIDIA, 1 MiB, 1"
        return _stack(
            patch("jarvis.inventory.platform.system", return_value="Windows"),
            patch("jarvis.inventory.command", side_effect=fake),
            patch("jarvis.hostmeter.sample", return_value={"v": 1, "source": "cim", "ram": {"total": 1}}),
        )

    def _overlap(self, call, entered, release, calls, key="n"):
        barrier = threading.Barrier(2)
        errors = []
        results = []

        def invoke():
            try:
                barrier.wait(3)
                results.append(call())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=invoke, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(3), "runner did not start")
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and calls[key] == 1:
                time.sleep(0.01)
            self.assertEqual(calls[key], 1)
        finally:
            release.set()
            for thread in threads:
                thread.join(3)
        self.assertEqual(errors, [])
        for thread in threads:
            self.assertFalse(thread.is_alive())
        return results


class _patched:
    def __init__(self, guard, target, runner):
        self._guard = patch(guard, return_value=True)
        self._target = patch(target, side_effect=runner)

    def __enter__(self):
        self._guard.start()
        self._target.start()
        return self

    def __exit__(self, *exc):
        self._target.stop()
        self._guard.stop()


class _stack:
    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self._patches):
            item.stop()


if __name__ == "__main__":
    unittest.main()
