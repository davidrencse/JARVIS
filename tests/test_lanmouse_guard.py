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
        r = near(CONFIG, ts(cur="172.1.41.12:41641"))
        self.assertIs(r["near"], True)

    def test_private_address_on_another_network_is_apart(self):
        for cur in ("10.0.0.12:41641", "192.168.1.20:41641"):
            self.assertIs(near(CONFIG, ts(cur=cur))["near"], False, cur)

    def test_stale_cur_addr_on_offline_peer_is_not_near(self):
        r = near(CONFIG, ts(online=False, cur="172.1.41.40:41641"))
        self.assertIs(r["near"], False)

    def test_virtual_and_vpn_interfaces_are_not_own_subnets(self):
        for name in ("tun0", "utun3", "ppp0", "vmnet8", "vboxnet0", "cni0", "flannel.1", "ham0",
                     "wg0", "tailscale0", "docker0", "br-ab12", "veth1", "virbr0", "tap0"):
            self.assertTrue(sharedinput.VIRTUAL_IF.match(name), name)
        for name in ("wlan0", "eth0", "enp3s0", "Wi-Fi", "Ethernet"):
            self.assertFalse(sharedinput.VIRTUAL_IF.match(name), name)

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

    def test_a_planted_local_address_never_beats_tailscale_seeing_the_peer_elsewhere(self):
        # Café Wi-Fi: a spoofed mDNS answer put a local IP in the Lan Mouse config, while
        # Tailscale reaches the Dell at home. Apart (audit 2026-11-07).
        cfg = {"clients": [{"hostname": "WinLaptop", "ips": ["172.1.41.66"]}]}
        self.assertIs(near(cfg, ts(cur="198.51.100.58:55157"))["near"], False)
        self.assertIs(near(cfg, ts(cur="172.1.41.40:41641"))["near"], True)  # really here

    def test_stale_offline_twin_does_not_hide_the_live_peer(self):
        status = {"BackendState": "Running", "Peer": {
            "a": {"HostName": "WinLaptop", "Online": False, "CurAddr": "", "TailscaleIPs": ["100.9.9.9"]},
            "b": {"HostName": "WinLaptop", "Online": True, "CurAddr": "172.1.41.40:1", "TailscaleIPs": ["100.64.0.13"]}}}
        self.assertIs(near(CONFIG, status)["near"], True)

    def test_tailscale_cgnat_address_says_nothing_about_distance(self):
        self.assertIs(near(CONFIG, ts(cur="100.64.0.13:41641"))["near"], False)

    def test_matches_peer_by_tailscale_ip_when_hostname_differs(self):
        r = near({"clients": [{"hostname": "", "ips": ["100.64.0.13"]}]},
                                    ts(host="DESKTOP-X", cur="172.1.41.20:41641"))
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
        self.clock = 1000.0

    def tearDown(self):
        self.dir.cleanup()

    def step(self, near):
        def status():
            return {"running": self.running}

        def start():
            self.calls.append("start"); self.running = True

        def stop():
            self.calls.append("stop"); self.running = False
        self.clock += 30
        return sharedinput.guard(self.state, nearby=lambda: {"near": near, "peer": "Dell", "why": "test."},
                                 status_fn=status, start_fn=start, stop_fn=stop, now=self.clock)

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
        self.clock += sharedinput.GUARD_GRACE         # well past the stop
        self.running = True                           # David starts it by hand
        for _ in range(3):
            self.assertIsNone(self.step(False)["action"])
        self.assertEqual(self.calls, ["stop"])
        self.step(True)                               # together again: override ends
        self.step(False); self.step(False)
        self.assertEqual(self.calls, ["stop", "stop"])

    def test_daemon_still_dying_after_stop_is_not_a_manual_start(self):
        self.step(False); self.step(False)            # guard signals the stop
        self.running = True                           # the daemon is still alive one tick later
        r = self.step(True)
        self.assertFalse(r["override"])
        self.assertTrue(r["stopped_by_guard"])
        self.running = False                          # it finally exits
        self.assertEqual(self.step(True)["action"], "started")
        self.assertEqual(self.calls, ["stop", "start"])

    def test_failed_start_is_retried_next_reading(self):
        self.step(False); self.step(False)
        attempts = []

        def start():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("systemctl refused")
            self.running = True

        def go():
            self.clock += 30
            return sharedinput.guard(self.state, nearby=lambda: {"near": True, "peer": "D", "why": "t."},
                                     status_fn=lambda: {"running": self.running}, start_fn=start,
                                     stop_fn=lambda: None, now=self.clock)
        go()                                          # together streak 1
        self.assertEqual(go()["action"], "start_failed")
        r = go()
        self.assertEqual(r["action"], "started")
        self.assertEqual(len(attempts), 2)
        self.assertFalse(r["stopped_by_guard"])

    def test_start_that_leaves_it_stopped_is_not_remembered_as_done(self):
        self.step(False); self.step(False)
        r = None
        for _ in range(2):
            self.clock += 30
            r = sharedinput.guard(self.state, nearby=lambda: {"near": True, "peer": "D", "why": "t."},
                                  status_fn=lambda: {"running": False}, start_fn=lambda: {"started": False},
                                  stop_fn=lambda: None, now=self.clock)
        self.assertEqual(r["action"], "start_failed")
        self.assertTrue(r["stopped_by_guard"])

    def test_busy_lock_skips_the_tick(self):
        from jarvis.locking import exclusive_lock
        with exclusive_lock(self.state / (sharedinput.GUARD_FILE + ".lock")):
            r = self.step(False)
        self.assertTrue(r.get("busy"))
        self.assertIsNone(r["action"])
        self.assertFalse((self.state / sharedinput.GUARD_FILE).exists())

    def test_default_stop_keeps_the_ip_watcher(self):
        from unittest import mock
        with mock.patch.object(sharedinput, "_units", return_value=["lan-mouse.service", "lan-mouse-ipwatch.service"]), \
             mock.patch.object(sharedinput, "_run") as run, mock.patch.object(sharedinput.shutil, "which", return_value=None), \
             mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": False, "build": None}), \
             mock.patch.object(sharedinput.os, "name", "posix"):
            sharedinput.stop(sharing_only=True)
        stopped = [c.args for c in run.call_args_list if "stop" in c.args]
        self.assertEqual(stopped, [("systemctl", "--user", "stop", "lan-mouse.service")])

    def boot_step(self, near, boot):
        return sharedinput.guard(self.state, nearby=lambda: {"near": near, "peer": "Dell", "why": "test."},
                                 status_fn=lambda: {"running": self.running},
                                 start_fn=lambda: (self.calls.append("start"), setattr(self, "running", True)),
                                 stop_fn=lambda: (self.calls.append("stop"), setattr(self, "running", False)),
                                 autostart=True, boot=boot)

    def test_autostart_after_boot_when_together(self):
        self.running = False
        self.assertIsNone(self.boot_step(True, 1000.0)["action"])
        self.assertEqual(self.boot_step(True, 1001.5)["action"], "started")  # jitter: same boot
        self.assertEqual(self.calls, ["start"])

    def test_autostart_waits_while_apart(self):
        self.running = False
        for _ in range(3):
            self.boot_step(False, 1000.0)
        self.assertEqual(self.calls, [])
        self.boot_step(True, 1000.0); self.boot_step(True, 1000.0)
        self.assertEqual(self.calls, ["start"])

    def test_stop_by_hand_sticks_until_next_boot(self):
        self.running = False
        self.boot_step(True, 1000.0); self.boot_step(True, 1000.0)
        self.running = False                          # David stops it
        for _ in range(3):
            self.boot_step(True, 1000.0)
        self.assertEqual(self.calls, ["start"])
        self.boot_step(True, 9000.0); self.boot_step(True, 9000.0)  # rebooted
        self.assertEqual(self.calls, ["start", "start"])

    def test_autostart_off_never_starts(self):
        self.running = False
        for _ in range(3):
            self.step(True)
        self.assertEqual(self.calls, [])

    def test_memo_survives_between_runs(self):
        self.step(False)
        self.assertEqual(json.loads((self.state / sharedinput.GUARD_FILE).read_text())["apart"], 1)


if __name__ == "__main__":
    unittest.main()


class SharingPausedTests(GuardTests):
    """Clipboard + shared input follow one rule: off while apart or while remote control is on."""

    def test_apart_pauses_and_together_resumes(self):
        self.assertIsNone(sharedinput.sharing_paused(self.state))
        self.step(False); self.step(False)
        self.assertEqual(sharedinput.sharing_paused(self.state), "the laptops are apart")
        self.step(None)  # can't tell: no change
        self.assertEqual(sharedinput.sharing_paused(self.state), "the laptops are apart")
        self.step(True); self.step(True)
        self.assertIsNone(sharedinput.sharing_paused(self.state))

    def test_remote_session_pauses_and_blocks_guard_restart(self):
        self.step(False); self.step(False)  # guard stopped it
        (self.state / sharedinput.REMOTE_MARKER).write_text("{}")
        self.assertEqual(sharedinput.sharing_paused(self.state), "remote control is on")
        self.step(True); self.step(True); self.step(True)
        self.assertEqual(self.calls, ["stop"])  # not restarted under the remote session
        (self.state / sharedinput.REMOTE_MARKER).unlink()
        self.assertEqual(self.step(True)["action"], "started")

    def test_stale_remote_marker_is_ignored(self):
        import os, time
        marker = self.state / sharedinput.REMOTE_MARKER
        marker.write_text("{}")
        old = time.time() - sharedinput.REMOTE_MARKER_TTL - 5
        os.utime(marker, (old, old))
        self.assertIsNone(sharedinput.sharing_paused(self.state))

    def test_clipboard_apply_skips_while_paused(self):
        from unittest import mock
        from jarvis import clipsync
        (self.state / sharedinput.REMOTE_MARKER).write_text("{}")
        with mock.patch.object(clipsync, "local_clipboard") as board:
            self.assertFalse(clipsync.apply(str(self.state), "text", b"hi"))
            board.assert_not_called()


class DisabledSwitchTests(unittest.TestCase):
    """lan_mouse.enabled false: the guard may stop sharing but never starts it (2026-11-05,
    David switched Lan Mouse off until its Windows bugs were fixed)."""

    def test_guard_never_starts_when_disabled(self):
        import tempfile
        from jarvis import sharedinput as si
        state = tempfile.mkdtemp()
        started = []
        running = {"r": False}
        for i in range(6):
            si.guard(state, nearby=lambda: {"near": True, "peer": "Dell", "why": "t."},
                     status_fn=lambda: {"running": running["r"]}, start_fn=lambda: started.append(1),
                     stop_fn=lambda: None, autostart=True, boot=1_000_000.0 + (3600 if i > 2 else 0),
                     now=10_000 + 30 * i, allow_start=False)
        self.assertEqual(started, [])

    def test_guard_for_reads_the_switch(self):
        from unittest import mock
        from jarvis import sharedinput as si
        with mock.patch.object(si, "guard", lambda state, **kw: kw), \
                mock.patch("jarvis.platforms.boot_time", lambda: 5.0):
            off = si.guard_for({"lan_mouse": {"enabled": False}}, "/tmp/x")
            on = si.guard_for({}, "/tmp/x")
        self.assertEqual((off["allow_start"], off["autostart"]), (False, False))
        self.assertEqual((on["allow_start"], on["autostart"], on["boot"]), (True, True, 5.0))
