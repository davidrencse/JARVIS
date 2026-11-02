import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from jarvis import sharedinput

CONFIG = {"clients": [{"hostname": "WinLaptop", "ips": ["100.64.0.13"]}]}
HOME = [ipaddress.ip_network("172.1.41.0/24")]  # David's home LAN isn't RFC1918


def near(config, status, nets=HOME):
    return sharedinput.peer_nearby(config, status, nets)


def ts(online=True, cur="", host="WinLaptop", ips=("100.64.0.13",)):
    return {"BackendState": "Running",
            "Peer": {"k": {"HostName": host, "Online": online, "CurAddr": cur, "TailscaleIPs": list(ips)}}}


class PeerNearbyTests(unittest.TestCase):
    def test_lan_direct_address_is_near(self):
        r = near(CONFIG, ts(cur="10.0.0.12:41641"))
        self.assertIs(r["near"], True)

    def test_public_endpoint_and_offline_are_apart(self):
        for status in (ts(cur="198.51.100.58:55157"), ts(online=False)):
            self.assertIs(near(CONFIG, status)["near"], False, status)

    def test_home_lan_outside_rfc1918_is_near(self):
        self.assertIs(near(CONFIG, ts(cur="172.1.41.40:41641"))["near"], True)

    def test_idle_peer_with_no_address_is_unknown_not_apart(self):
        self.assertIsNone(near(CONFIG, ts(cur=""))["near"])

    def test_configured_lan_address_on_my_subnet_is_near_even_if_tailscale_is_off(self):
        cfg = {"clients": [{"hostname": "WinLaptop", "ips": ["172.1.41.40"]}]}
        self.assertIs(near(cfg, ts(online=False))["near"], True)

    def test_stale_offline_twin_does_not_hide_the_live_peer(self):
        status = {"BackendState": "Running", "Peer": {
            "a": {"HostName": "WinLaptop", "Online": False, "CurAddr": "", "TailscaleIPs": ["100.9.9.9"]},
            "b": {"HostName": "WinLaptop", "Online": True, "CurAddr": "172.1.41.40:1", "TailscaleIPs": ["100.64.0.13"]}}}
        self.assertIs(near(CONFIG, status)["near"], True)

    def test_tailscale_cgnat_address_says_nothing_about_distance(self):
        self.assertIs(near(CONFIG, ts(cur="100.64.0.13:41641"))["near"], False)

    def test_matches_peer_by_tailscale_ip_when_hostname_differs(self):
        r = near({"clients": [{"hostname": "", "ips": ["100.64.0.13"]}]},
                                    ts(host="DESKTOP-X", cur="192.168.1.20:41641"))
        self.assertIs(r["near"], True)

    def test_unknown_when_no_tailscale_or_no_peer(self):
        self.assertIsNone(near(CONFIG, {})["near"])
        self.assertIsNone(near({"clients": []}, ts())["near"])
        self.assertIsNone(near(CONFIG, ts(host="other", ips=("100.1.1.1",)))["near"])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.running = True
        self.calls = []

    def tearDown(self):
        self.dir.cleanup()

    def step(self, near):
        def status():
            return {"running": self.running}

        def start():
            self.calls.append("start"); self.running = True

        def stop():
            self.calls.append("stop"); self.running = False
        return sharedinput.guard(self.state, nearby=lambda: {"near": near, "peer": "Dell", "why": "test."},
                                 status_fn=status, start_fn=start, stop_fn=stop)

    def test_stops_after_two_apart_readings_and_restarts_when_back(self):
        self.assertIsNone(self.step(False)["action"])          # one reading: could be a blip
        self.assertEqual(self.step(False)["action"], "stopped")
        self.assertIsNone(self.step(True)["action"])
        self.assertEqual(self.step(True)["action"], "started")
        self.assertEqual(self.calls, ["stop", "start"])

    def test_a_blip_resets_the_streak(self):
        self.step(False); self.step(True); self.step(False)
        self.assertEqual(self.calls, [])

    def test_manual_stop_is_not_undone(self):
        self.running = False
        for _ in range(4):
            self.step(True)
        self.assertEqual(self.calls, [])

    def test_unknown_never_acts(self):
        for _ in range(4):
            self.step(None)
        self.assertEqual(self.calls, [])

    def test_manual_start_while_apart_is_an_override(self):
        self.step(False); self.step(False)            # guard stops it
        self.running = True                           # David starts it by hand
        for _ in range(3):
            self.assertIsNone(self.step(False)["action"])
        self.assertEqual(self.calls, ["stop"])
        self.step(True)                               # together again: override ends
        self.step(False); self.step(False)
        self.assertEqual(self.calls, ["stop", "stop"])

    def test_default_stop_keeps_the_ip_watcher(self):
        from unittest import mock
        with mock.patch.object(sharedinput, "_units", return_value=["lan-mouse.service", "lan-mouse-ipwatch.service"]), \
             mock.patch.object(sharedinput, "_run") as run, mock.patch.object(sharedinput.shutil, "which", return_value=None), \
             mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": False, "build": None}), \
             mock.patch.object(sharedinput.os, "name", "posix"):
            sharedinput.stop(sharing_only=True)
        stopped = [c.args for c in run.call_args_list if "stop" in c.args]
        self.assertEqual(stopped, [("systemctl", "--user", "stop", "lan-mouse.service")])

    def test_memo_survives_between_runs(self):
        self.step(False)
        self.assertEqual(json.loads((self.state / sharedinput.GUARD_FILE).read_text())["apart"], 1)


if __name__ == "__main__":
    unittest.main()
