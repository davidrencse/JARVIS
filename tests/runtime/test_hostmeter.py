"""Battery health and RAM, swap, disk, CPU. Fixtures only: nothing here reads the Dell."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis import hostmeter, probe
from jarvis.cluster_store import Broker


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
NOW_S = NOW.timestamp()
GIB = 1024 ** 3


def iso(when):
    return when.astimezone(timezone.utc).isoformat()


def row(shown, title, label):
    for block in shown["blocks"]:
        if block["title"] == title:
            for item in block["rows"]:
                if item[0] == label:
                    return item[1], item[2]
    raise AssertionError(f"no {title} / {label} in {[b['title'] for b in shown['blocks']]}")


def supply(**kw):
    base = {"kind": "linux", "name": "BAT0", "status": "Discharging", "capacity": "77"}
    base.update(kw)
    return base


def reading(**kw):
    raw = {"v": 1, "measured_at": iso(NOW), "source": "sysfs",
           "ram": {"total": 16 * GIB, "free": 4 * GIB, "unit": "B"},
           "swap": {"total": 8 * GIB, "free": 6 * GIB, "unit": "B"},
           "disks": [{"name": "system", "total": 100 * GIB, "free": 40 * GIB, "unit": "B"}],
           "cpu": {"load1": 1.4, "cores": 14},
           "battery": {"applicability": "present", "supplies": [supply(
               energy_full="82300000", energy_full_design="96800000", cycle_count="120")]}}
    raw.update(kw)
    return raw


class HealthTests(unittest.TestCase):
    def show(self, raw, **kw):
        return hostmeter.view(raw, now=NOW_S, **kw)

    def test_healthy_pack_uses_full_over_design_not_the_charge(self):
        shown = self.show(reading())
        self.assertEqual(row(shown, "Battery charge", "Now")[0], "77% · discharging")
        self.assertEqual(row(shown, "Battery health", "Health")[0], "85%")
        self.assertEqual(row(shown, "Battery health", "Capacity")[0], "82.3 Wh full · 96.8 Wh design")
        self.assertEqual(row(shown, "Battery health", "Degradation")[0], "15% below design")
        self.assertEqual(row(shown, "Battery health", "Cycles"), ("120", None))
        self.assertIn("fresh", row(shown, "Battery health", "When")[0])
        self.assertEqual(shown["battery"]["pack"]["health_percent"], 85.0)
        # 82.3 of 96.8 is about 85, not the 77% charge.
        self.assertNotEqual(shown["battery"]["pack"]["health_percent"], 77)

    def test_energy_now_does_not_become_health_or_charge(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            capacity=None, energy_now="50000000", energy_full="80000000", energy_full_design="100000000")]})
        shown = self.show(raw)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "80%")
        self.assertEqual(row(shown, "Battery charge", "Now")[0], "Unavailable")
        self.assertIn("not reported", row(shown, "Battery charge", "Now")[1])

    def test_missing_design_zero_full_negative_and_malformed(self):
        missing = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="80000000", energy_full_design=None)]}))
        self.assertEqual(row(missing, "Battery health", "Health")[0], "Unavailable")
        self.assertIn("design capacity", row(missing, "Battery health", "Health")[1])

        zero = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="80000000", energy_full_design="0")]}))
        self.assertIn("zero", row(zero, "Battery health", "Health")[1])
        self.assertNotIn("0%", row(zero, "Battery health", "Health")[0])

        empty = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="0", energy_full_design="80000000")]}))
        self.assertIn("full-charge capacity is zero", row(empty, "Battery health", "Health")[1])

        negative = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="-1", energy_full_design="80000000")]}))
        self.assertIn("negative", row(negative, "Battery health", "Health")[1])

        junk = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="nope", energy_full_design="80000000")]}))
        self.assertIn("not a number", row(junk, "Battery health", "Health")[1])

    def test_above_design_is_shown_and_not_clamped(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="112000000", energy_full_design="100000000")]})
        shown = self.show(raw)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "112% of design")
        self.assertEqual(shown["battery"]["pack"]["degradation_percent"], -12.0)
        self.assertIn("above design", row(shown, "Battery health", "Degradation")[0])
        self.assertTrue(shown["battery"]["pack"]["above_design"])
        self.assertTrue(shown["battery"]["pack"]["plausible"])

    def test_implausible_ratio_is_labeled_and_still_not_clamped(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="160", energy_full_design="100")]})
        shown = self.show(raw)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "160% of design (check units)")
        self.assertIn("not clamped", row(shown, "Battery health", "Health")[1])
        self.assertFalse(shown["battery"]["pack"]["plausible"])

    def test_disagreeing_unit_families_are_not_averaged(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="90", energy_full_design="100", charge_full="40", charge_full_design="100")]})
        shown = self.show(raw)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "Unavailable")
        self.assertIn("energy 90% and charge 40%", row(shown, "Battery health", "Health")[1])

    def test_agreeing_families_prefer_energy(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="90000000", energy_full_design="100000000",
            charge_full="92000000", charge_full_design="100000000")]})
        pack = self.show(raw)["battery"]["pack"]
        self.assertEqual(pack["unit"], "uWh")
        self.assertEqual(pack["health_percent"], 90.0)

    def test_unreadable_energy_still_uses_a_charge_pair(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(
            energy_full=None, energy_full_design=None,
            charge_full="40000000", charge_full_design="50000000",
            errors={"energy_full": "unreadable", "energy_full_design": "permission denied /home/jdoe"})]})
        shown = self.show(raw)
        pack = shown["battery"]["pack"]
        self.assertEqual(pack["health_percent"], 80.0)
        self.assertEqual(pack["unit"], "uAh")
        self.assertEqual(row(shown, "Battery health", "Capacity")[0], "40 Ah full · 50 Ah design")

    def test_charge_outside_0_100_is_not_clamped(self):
        raw = reading(battery={"applicability": "present", "supplies": [supply(capacity="140")]})
        shown = self.show(raw)
        value, detail = row(shown, "Battery charge", "Now")
        self.assertEqual(value, "Unavailable")
        self.assertIn("outside 0–100", detail)
        self.assertIn("140", detail)

    def test_missing_cycle_is_omitted_and_a_bad_cycle_is_unavailable(self):
        quiet = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="80", energy_full_design="100", cycle_count=None)]}))
        titles = [item[0] for block in quiet["blocks"] if block["title"] == "Battery health" for item in block["rows"]]
        self.assertNotIn("Cycles", titles)
        bad = self.show(reading(battery={"applicability": "present", "supplies": [supply(
            energy_full="80", energy_full_design="100", cycle_count="nope")]}))
        self.assertEqual(row(bad, "Battery health", "Cycles")[0], "Unavailable")

    def test_two_batteries_are_weighted_and_not_collapsed(self):
        raw = reading(battery={"applicability": "present", "supplies": [
            supply(name="BAT0", energy_full="50", energy_full_design="100", capacity="10", cycle_count="2"),
            supply(name="BAT1", energy_full="100", energy_full_design="100", capacity="40", status="Charging", cycle_count="3"),
        ]})
        shown = self.show(raw)
        pack = shown["battery"]["pack"]
        self.assertEqual(pack["health_percent"], 75.0)
        self.assertEqual(pack["charge_percent"], 30.0)
        self.assertEqual(pack["power"], "mixed")
        self.assertEqual(pack["cycle_count"], 5)
        self.assertIn("BAT0 health", [b["title"] for b in shown["blocks"]])
        self.assertIn("BAT1 charge", [b["title"] for b in shown["blocks"]])
        # One battery is not also titled as a pack. Two batteries are.
        self.assertEqual(sum(1 for b in shown["blocks"] if b["title"] == "Battery health"), 1)

    def test_mixed_units_do_not_invent_a_pack_percent(self):
        raw = reading(battery={"applicability": "present", "supplies": [
            supply(name="BAT0", energy_full="50", energy_full_design="100", charge_full=None, charge_full_design=None),
            supply(name="BAT1", energy_full=None, energy_full_design=None, charge_full="40", charge_full_design="100"),
        ]})
        pack = self.show(raw)["battery"]["pack"]
        self.assertIsNone(pack["health_percent"])
        self.assertIn("different capacity units", pack["health_reason"])

    def test_no_battery_is_not_applicable_and_resources_remain(self):
        shown = self.show(reading(battery={"applicability": "not_applicable", "reason": "No battery reported.", "supplies": []}))
        self.assertEqual(row(shown, "Battery", "Status")[0], "Not applicable")
        self.assertEqual(row(shown, "RAM", "Used")[0], "12 GB")
        self.assertEqual(row(shown, "Reading", "Source")[0], "Linux sysfs and /proc")

    def test_old_inventory_without_a_battery_section_is_unreported(self):
        shown = hostmeter.view(hostmeter.legacy_from_inventory(
            {"collected_at": iso(NOW), "hardware": {"memory": "MemTotal:        1048576 kB\nMemAvailable:     262144 kB\n"}}),
            now=NOW_S)
        self.assertEqual(row(shown, "Battery", "Status")[0], "Not reported")
        self.assertEqual(row(shown, "RAM", "Total")[0], "1 GB")
        self.assertNotIn("failure", json.dumps(shown).lower())

    def test_saved_energy_fields_recompute_without_a_node_update(self):
        inv = {"collected_at": iso(NOW - timedelta(minutes=10)), "hardware": {
            "memory": "MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
            "batteries": [{"name": "BAT0", "energy_full": "80000000", "energy_full_design": "100000000",
                           "capacity": "50", "status": "Full"}]}}
        shown = hostmeter.view(hostmeter.legacy_from_inventory(inv), now=NOW_S)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "80%")
        self.assertEqual(row(shown, "Swap", "Status")[0], "Not applicable")
        self.assertIn("fresh", row(shown, "Battery health", "When")[0])

    def test_a_bare_health_percent_is_ignored(self):
        payload = reading()
        payload["battery"]["supplies"][0]["health_percent"] = 7
        payload["battery"]["supplies"][0]["SerialNumber"] = "SECRET-SERIAL"
        payload["battery"]["supplies"][0]["name"] = "/home/jdoe/battery"
        kept = hostmeter.accept(payload)
        text = json.dumps(kept)
        self.assertNotIn("SECRET-SERIAL", text)
        self.assertNotIn("health_percent", text)
        self.assertNotIn("/home/jdoe", text)
        shown = self.show(kept)
        self.assertEqual(row(shown, "Battery health", "Health")[0], "85%")
        self.assertNotIn("jdoe", json.dumps(shown))

    def test_fresh_aging_stale_and_offline_stay_distinct(self):
        fresh = self.show(reading())
        self.assertEqual(fresh["freshness"], "fresh")
        aging = self.show(reading(measured_at=iso(NOW - timedelta(hours=2))))
        self.assertEqual(aging["freshness"], "aging")
        stale = self.show(reading(measured_at=iso(NOW - timedelta(hours=7))), presence="offline")
        self.assertEqual(stale["freshness"], "stale")
        self.assertEqual(row(stale, "Battery health", "Health")[0], "85%")
        self.assertEqual(row(stale, "Battery health", "Link")[0], "Offline")
        self.assertIn("last reading", row(stale, "Battery health", "Link")[1])
        ahead = self.show(reading(measured_at=iso(NOW + timedelta(minutes=5))))
        self.assertEqual(ahead["freshness"], "unknown")
        self.assertIn("doesn't agree", row(ahead, "Battery health", "When")[0])


class ResourceTests(unittest.TestCase):
    def test_panel_shape_used_free_total_and_load(self):
        raw = reading(
            ram={"total": 15.3 * GIB, "free": 2.4 * GIB, "unit": "B",
                 "free_means": "Free counts memory the kernel can give out, including reclaimable cache."},
            swap={"total": 23.7 * GIB, "free": 13.7 * GIB, "unit": "B"},
            disks=[{"name": "system", "total": 304.3 * GIB, "free": 86.0 * GIB, "unit": "B"}],
            cpu={"load1": 2.24, "cores": 14})
        shown = hostmeter.view(raw, now=NOW_S)
        self.assertEqual(row(shown, "RAM", "Used")[0], "12.9 GB")
        self.assertEqual(row(shown, "RAM", "Free")[0], "2.4 GB")
        self.assertEqual(row(shown, "RAM", "Total")[0], "15.3 GB")
        self.assertIn("kernel can give out", row(shown, "RAM", "Free")[1])
        self.assertEqual(row(shown, "Swap", "Used")[0], "10 GB")
        self.assertEqual(row(shown, "Swap", "Free")[0], "13.7 GB")
        self.assertEqual(row(shown, "Swap", "Total")[0], "23.7 GB")
        self.assertEqual(row(shown, "Disk", "Used")[0], "218.3 GB")
        self.assertEqual(row(shown, "Disk", "Free")[0], "86 GB")
        self.assertEqual(row(shown, "Disk", "Total")[0], "304.3 GB")
        self.assertEqual(row(shown, "CPU", "Load")[0], "16%")
        self.assertIn("1-minute load", row(shown, "CPU", "Load")[1])
        self.assertNotIn("Busy", [item[0] for block in shown["blocks"] if block["title"] == "CPU" for item in block["rows"]])

    def test_memfree_is_not_the_free_figure_and_swap_zero_is_not_applicable(self):
        text = "MemTotal: 2000000 kB\nMemFree: 1000 kB\nMemAvailable: 500000 kB\nSwapTotal: 0 kB\n"
        info = hostmeter._parse_meminfo(text)
        self.assertNotIn("MemFree", info)
        raw = hostmeter.read_linux(proc_root=self._proc(text, "1.50 1 1 1/1 1\n"),
                                   sys_root=self._empty_power(), mounts=[])
        # read_linux stamps its own time; check the bytes, not the label clock.
        self.assertEqual(raw["ram"]["free"], 500000 * 1024)
        self.assertEqual(raw["swap"]["applicability"], "not_applicable")

    def test_free_larger_than_total_is_unavailable(self):
        shown = hostmeter.view(reading(ram={"total": 100, "free": 150, "unit": "B"}), now=NOW_S)
        self.assertEqual(row(shown, "RAM", "Status")[0], "Unavailable")
        self.assertIn("free is larger than total", row(shown, "RAM", "Status")[1])

    def test_load_over_100_is_not_clamped_and_missing_cores_is_not_a_percent(self):
        hot = hostmeter.view(reading(cpu={"load1": 20, "cores": 2}), now=NOW_S)
        self.assertEqual(row(hot, "CPU", "Load")[0], "1000%")
        plain = hostmeter.view(reading(cpu={"load1": 1.5}), now=NOW_S)
        self.assertEqual(row(plain, "CPU", "Load")[0], "1.5")
        self.assertIn("not a percent", row(plain, "CPU", "Load")[1])

    def test_windows_busy_is_not_called_load_and_pagefile_is_not_clamped(self):
        payload = {
            "memory": {"ok": True, "total_kb": 16 * 1024 * 1024, "free_kb": 4 * 1024 * 1024},
            "pages": {"ok": True, "items": [{"allocated_mb": 1024, "usage_mb": 2048}]},
            "disks": {"ok": True, "items": [{"name": "C:", "total": 100 * GIB, "free": 20 * GIB},
                                            {"name": "D:\\Users\\secret", "total": 10, "free": 1}]},
            "cpu": {"ok": True, "items": [{"load": 16, "cores": 8}]},
            "win32": {"ok": True, "items": []},
        }
        raw = hostmeter.windows_canonical(payload, iso(NOW))
        shown = hostmeter.view(raw, now=NOW_S)
        self.assertEqual(row(shown, "CPU", "Busy")[0], "16%")
        self.assertNotIn("Load", [item[0] for block in shown["blocks"] if block["title"] == "CPU" for item in block["rows"]])
        self.assertIn("not Linux available", row(shown, "RAM", "Free")[1])
        self.assertEqual(row(shown, "Swap", "Status")[0], "Unavailable")
        self.assertIn("larger than its size", row(shown, "Swap", "Status")[1])
        self.assertEqual(row(shown, "Battery", "Status")[0], "Not applicable")
        self.assertNotIn("secret", json.dumps(hostmeter.accept(raw)))

    def test_windows_health_prefers_wmi_and_refuses_a_disagreement(self):
        good = self._win(wmi=(80000, 100000), win=(79000, 100000), charge=61, status=6, cycles=40)
        shown = hostmeter.view(good, now=NOW_S)
        self.assertEqual(row(shown, "Battery charge", "Now")[0], "61% · charging")
        self.assertEqual(shown["battery"]["pack"]["health_percent"], 80.0)
        self.assertEqual(shown["battery"]["pack"]["unit"], "mWh")
        self.assertEqual(row(shown, "Battery health", "Cycles"), ("40", None))
        bad = self._win(wmi=(90000, 100000), win=(40000, 100000), charge=10, status=1)
        refused = hostmeter.view(bad, now=NOW_S)
        self.assertIn("root\\wmi", row(refused, "Battery health", "Health")[1])
        self.assertIn("Win32_Battery", row(refused, "Battery health", "Health")[1])

    def test_windows_timeout_is_unavailable_not_a_failure_of_the_whole_inventory_shape(self):
        raw = hostmeter.windows_canonical({"status": "unavailable", "reason": "TimeoutExpired"}, iso(NOW))
        shown = hostmeter.view(raw, now=NOW_S)
        self.assertEqual(shown["blocks"][0]["title"], "Stats")
        self.assertEqual(row(shown, "Stats", "Status"), ("Unavailable", "the reading timed out"))
        script = hostmeter.WINDOWS_METERS_SCRIPT
        for banned in ("SerialNumber", "UniqueID", "batteryreport", "powercfg", "PNPDeviceID"):
            self.assertNotIn(banned, script)
        # DeviceID is read only as a drive letter, and only A:–Z: is kept.
        self.assertIn("^[A-Z]:$", script)
        battery = script.split("Win32_Battery", 1)[1].split("BatteryStaticData", 1)[0]
        self.assertNotIn("DeviceID", battery)

    def test_linux_fixture_tree_skips_mains_and_absent_packs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._bat(root / "BAT0", "Battery", "1", "80", "100", "55", "Discharging", "12")
            self._bat(root / "BAT1", "Battery", "0", "10", "10", "10", "Full", "1")
            (root / "ADP1").mkdir()
            (root / "ADP1" / "type").write_text("Mains\n")
            (root / "weird name").mkdir()
            (root / "weird name" / "type").write_text("Battery\n")
            found = hostmeter.linux_supplies(root)
        self.assertEqual([s["name"] for s in found["supplies"]], ["BAT0"])
        self.assertEqual(found["supplies"][0]["cycle_count"], "12")
        self.assertNotIn("serial", json.dumps(found).lower())

    def _win(self, wmi, win, charge, status, cycles=None):
        payload = {
            "memory": {"ok": True, "total_kb": 1024, "free_kb": 256},
            "pages": {"ok": True, "items": []},
            "disks": {"ok": True, "items": [{"name": "C:", "total": GIB, "free": GIB // 2}]},
            "cpu": {"ok": True, "items": [{"load": 4, "cores": 4}]},
            "win32": {"ok": True, "items": [{"name": "Battery 1", "charge_percent": charge, "status_code": status,
                                            "full_mwh": win[0], "design_mwh": win[1]}]},
            "static": {"ok": True, "items": [{"design_mwh": wmi[1]}]},
            "full": {"ok": True, "items": [{"full_mwh": wmi[0]}]},
            "power": {"ok": True, "items": [{"charging": status == 6, "discharging": status == 1, "ac": False}]},
            "cycles": {"ok": cycles is not None, "items": [{"cycle_count": cycles}] if cycles is not None else []},
        }
        return hostmeter.windows_canonical(payload, iso(NOW))

    def _bat(self, path, kind, present, full, design, cap, status, cycles):
        path.mkdir()
        for name, text in (("type", kind), ("present", present), ("energy_full", full),
                           ("energy_full_design", design), ("capacity", cap), ("status", status),
                           ("cycle_count", cycles)):
            (path / name).write_text(text + "\n")

    def _proc(self, meminfo, loadavg):
        root = Path(tempfile.mkdtemp())
        (root / "meminfo").write_text(meminfo)
        (root / "loadavg").write_text(loadavg)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        return root

    def _empty_power(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        return root


class TransportTests(unittest.TestCase):
    def test_probe_keeps_its_percents_and_adds_blocks(self):
        text = "host=zen\nmem_total_kb=16000000\nmem_avail_kb=992000\nswap_total_kb=8000000\nswap_free_kb=5136000\nload=0.98\nnproc=14\nbats=none\n"
        summary = probe.remote_summary(probe.parse_kv(text))
        self.assertEqual((summary["hostname"], summary["mem_avail_pct"], summary["swap_used_pct"]), ("zen", 6.2, 35.8))
        self.assertEqual(row(summary["meters"], "Battery", "Status")[0], "Not applicable")
        self.assertEqual(row(summary["meters"], "CPU", "Load")[0], "7%")
        garbage = probe.remote_summary(probe.parse_kv("host=zen\nmem_total_kb=abc\nlm_unit=active\n"))
        self.assertIsNone(garbage["mem_avail_pct"])
        self.assertTrue(garbage["lan_mouse"]["running"])
        self.assertIn("meters", garbage)

    def test_linux_probe_that_cannot_see_power_supply_is_unavailable_and_labelled_as_a_probe(self):
        """The shape the console guards against: SSH probe of a Windows host -> bats=missing. Its battery
        block says so honestly and carries the SSH source, so the card can prefer the node's own CIM reading."""
        summary = probe.remote_summary(probe.parse_kv("host=DELL\nmem_total_kb=1000\nmem_avail_kb=500\nbats=missing\n"))
        battery = summary["meters"]["battery"]
        self.assertEqual(battery["applicability"], "unavailable")
        self.assertEqual(row(summary["meters"], "Reading", "Source")[0], "SSH status")

    def test_heartbeat_stores_raw_meters_and_old_payloads_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Broker(tmp)
            broker.enroll("dell")
            broker.heartbeat("dell", {"hostname": "WinLaptop", "platform": "Windows", "code": "abc"})
            old = broker.nodes()[0]
            self.assertEqual(old["hostname"], "WinLaptop")
            self.assertNotIn("meters", old)
            payload = reading()
            payload["source"] = "cim"
            payload["battery"]["supplies"][0]["health_percent"] = 1
            payload["battery"]["supplies"][0]["UniqueID"] = "UID"
            broker.heartbeat("dell", {"hostname": "WinLaptop", "platform": "Windows", "meters": payload})
            node = broker.nodes()[0]
            self.assertEqual(node["code"], "")  # omitted keys stay empty strings, as before
            self.assertNotIn("health_percent", json.dumps(node["meters"]))
            self.assertNotIn("UniqueID", json.dumps(node["meters"]))
            shown = hostmeter.for_device({}, node, presence="online", now=NOW_S)
            self.assertEqual(row(shown, "Battery health", "Health")[0], "85%")
            huge = {"v": 1, "source": "cim", "ram": {"total": 1, "free": 1}, "note": "x" * 13000}
            broker.heartbeat("dell", {"meters": huge, "hostname": "kept"})
            self.assertNotIn("meters", broker.nodes()[0])
            self.assertEqual(broker.nodes()[0]["hostname"], "kept")

    def test_local_sample_only_when_the_hostname_matches(self):
        with patch("jarvis.hostmeter.socket.gethostname", return_value="archmain"):
            self.assertIsNone(hostmeter.live_if_host({"hostname": "WinLaptop"}))
            with patch("jarvis.hostmeter.cached_sample", return_value=reading()) as sample:
                got = hostmeter.live_if_host({"hostname": "archmain.tail.ts.net"})
                self.assertEqual(got["v"], 1)
                sample.assert_called_once()
            other = hostmeter.summarize({"hostname": "archmain"}, {}, {}, here=False)
            self.assertEqual(row(other, "Stats", "Status")[0], "Not reported")

    def test_newer_heartbeat_beats_an_older_inventory(self):
        inv = {"meters": reading(measured_at=iso(NOW - timedelta(hours=5)))}
        node = {"meters": reading(measured_at=iso(NOW))}
        shown = hostmeter.for_device(inv, node, now=NOW_S)
        self.assertEqual(shown["freshness"], "fresh")
        older = hostmeter.for_device(inv, {"meters": reading(measured_at=iso(NOW - timedelta(days=3)))}, now=NOW_S)
        self.assertEqual(older["freshness"], "aging")


if __name__ == "__main__":
    unittest.main()
