import ipaddress
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def test_clipboard_apply_skips_while_apart(self):
        from unittest import mock
        from jarvis import clipsync
        self.step(False); self.step(False)
        with mock.patch.object(clipsync, "local_clipboard") as board:
            self.assertFalse(clipsync.apply(str(self.state), "text", b"hi"))
            board.assert_not_called()

    def test_remote_control_turns_clipboard_on_even_apart(self):
        from unittest import mock
        from jarvis import clipsync
        self.step(False); self.step(False)
        self.assertEqual(sharedinput.clipboard_paused(self.state), "the laptops are apart")
        for marker in (sharedinput.REMOTE_MARKER, sharedinput.CONTROLLING_MARKER):
            (self.state / marker).write_text("{}")
            self.assertIsNone(sharedinput.clipboard_paused(self.state))
            self.assertIsNotNone(sharedinput.sharing_paused(self.state))  # shared input stays off
            with mock.patch.object(clipsync, "local_clipboard") as board:
                self.assertTrue(clipsync.apply(str(self.state), "text", b"hi"))
                board.return_value.write.assert_called_once_with("text", b"hi")
            (self.state / marker).unlink()
        self.assertEqual(sharedinput.clipboard_paused(self.state), "the laptops are apart")


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


class PhonePeerTests(unittest.TestCase):
    """A phone on cellular Tailscale is not the other computer."""

    def test_phone_on_a_public_address_is_not_apart(self):
        status = ts(cur="8.8.8.8:41641")
        status["Peer"]["k"]["OS"] = "android"
        reading = near(CONFIG, status)
        self.assertIsNone(reading["near"])
        self.assertNotIn("another network", reading["why"])

    def test_ios_phone_does_not_hide_the_laptop_on_the_lan(self):
        status = {"BackendState": "Running", "Peer": {
            "phone": {"HostName": "WinLaptop", "OS": "iOS", "Online": True,
                      "CurAddr": "172.58.1.9:41641", "TailscaleIPs": ["100.9.9.9"]},
            "pc": {"HostName": "WinLaptop", "OS": "windows", "Online": True,
                   "CurAddr": "172.1.41.40:41641", "TailscaleIPs": ["100.64.0.13"]}}}
        self.assertIs(near(CONFIG, status)["near"], True)

    def test_a_peer_with_no_os_is_still_a_computer(self):
        self.assertIs(near(CONFIG, ts(cur="8.8.8.8:1"))["near"], False)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def write(self, **memo):
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(memo), encoding="utf-8")

    def avail(self):
        return sharedinput.sharing_availability(self.state, now=self.now)

    def test_missing_memo_is_unknown_and_remote_stays_open(self):
        got = self.avail()
        self.assertEqual(got["state"], "unknown")
        self.assertIsNone(got["available"])
        self.assertTrue(got["remote"])
        self.assertEqual(got["settled"], "unknown")

    def test_fresh_apart_together_and_unsettled(self):
        self.write(apart_now=True, read_at=self.now - 10, why="Dell is on another network.", override=False)
        apart = self.avail()
        self.assertEqual((apart["state"], apart["available"], apart["settled"], apart["remote"]),
                         ("apart", False, "apart", True))
        self.write(apart_now=False, read_at=self.now - 10, why="Dell is on this network.")
        together = self.avail()
        self.assertEqual((together["state"], together["available"], together["settled"]),
                         ("together", True, "together"))
        self.write(read_at=self.now - 10, why="Tailscale hasn't talked to Dell lately; can't tell.", near=None)
        unknown = self.avail()
        self.assertEqual(unknown["state"], "unknown")
        self.assertIn("can't tell", unknown["why"].lower())
        self.assertTrue(unknown["remote"])

    def test_missing_or_stale_timestamp_stays_unknown(self):
        self.write(apart_now=True, why="old")
        self.assertEqual(self.avail()["state"], "unknown")
        self.assertEqual(self.avail()["settled"], "apart")
        self.write(apart_now=False, read_at=self.now - sharedinput.AVAILABILITY_TTL - 5)
        self.assertEqual(self.avail()["state"], "unknown")
        self.assertEqual(self.avail()["settled"], "together")

    def test_guard_reading_records_a_fresh_projection(self):
        self.running = True

        def status():
            return {"running": self.running}

        sharedinput.guard(self.state, nearby=lambda: {"near": False, "peer": "Dell", "why": "Dell is elsewhere."},
                          status_fn=status, start_fn=lambda: None, stop_fn=lambda: None, now=self.now)
        sharedinput.guard(self.state, nearby=lambda: {"near": False, "peer": "Dell", "why": "Dell is elsewhere."},
                          status_fn=status, start_fn=lambda: None, stop_fn=lambda: None, now=self.now + 30)
        got = sharedinput.sharing_availability(self.state, now=self.now + 30)
        self.assertEqual(got["state"], "apart")
        self.assertTrue(got["remote"])


class StartBlockedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def memo(self, **fields):
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(fields), encoding="utf-8")

    def test_fresh_apart_refuses_start_and_stop_still_works(self):
        self.memo(apart_now=True, read_at=self.now, why="apart", override=False)
        with unittest.mock.patch.object(sharedinput, "start") as start, \
                unittest.mock.patch.object(sharedinput, "stop", return_value={"stopped": True}):
            ok, text = sharedinput.control("start", state=self.state, config={}, now=self.now)
            stopped, _ = sharedinput.control("stop", state=self.state, config={}, now=self.now)
        self.assertFalse(ok)
        self.assertIn("Remote control still works", text)
        start.assert_not_called()
        self.assertTrue(stopped)

    def test_hand_start_is_not_turned_back_on_while_the_settled_flag_says_apart(self):
        self.memo(apart_now=True, override=True)  # no timestamp: the screen says unknown
        screen = sharedinput.sharing_availability(self.state, now=self.now)
        self.assertEqual(screen["state"], "unknown")
        with unittest.mock.patch.object(sharedinput, "start") as start:
            ok, text = sharedinput.control("start", state=self.state, config={}, now=self.now)
        self.assertFalse(ok)
        self.assertIn("started by hand", text)
        start.assert_not_called()

    def test_unknown_and_together_still_start(self):
        with unittest.mock.patch.object(sharedinput, "start", return_value={"started": True}) as start:
            ok, _ = sharedinput.control("start", state=self.state, config={}, now=self.now)
            self.assertTrue(ok)
            self.memo(apart_now=False, read_at=self.now, why="here")
            ok, _ = sharedinput.control("start", state=self.state, config={}, now=self.now)
        self.assertTrue(ok)
        self.assertEqual(start.call_count, 2)

    def test_switch_off_does_not_block_a_start(self):
        self.memo(apart_now=True, read_at=self.now, override=True)
        with unittest.mock.patch.object(sharedinput, "start", return_value={"started": True}) as start:
            ok, _ = sharedinput.control("start", state=self.state,
                                       config={"lan_mouse": {"off_when_apart": False}}, now=self.now)
        self.assertTrue(ok)
        start.assert_called_once()

    def test_override_does_not_restart_while_apart(self):
        self.memo(stopped_by_guard=True, override=True, apart_now=True, apart=4, together=2)
        calls = []
        for i in range(3):
            out = sharedinput.guard(
                self.state, nearby=lambda: {"near": False, "peer": "Dell", "why": "apart."},
                status_fn=lambda: {"running": False}, start_fn=lambda: calls.append("start"),
                stop_fn=lambda: calls.append("stop"), now=self.now + 30 * i)
            self.assertIsNone(out["action"])
        self.assertEqual(calls, [])


class ApartPairPolicyTests(unittest.TestCase):
    """One projection, three decisions. Start allows a stale apart memo. Pair refuses
    before any write, fresh or stale, and does not stop or start the daemon. The
    clipboard keeps pausing on that settled apart. Temp files only; no daemon."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.now = time.time()
        self.config = {"lan_mouse": {"off_when_apart": True}}

    def tearDown(self):
        self.dir.cleanup()

    def memo(self, **fields):
        (self.state / sharedinput.GUARD_FILE).write_text(json.dumps(fields), encoding="utf-8")

    def fresh(self):
        self.memo(apart_now=True, read_at=self.now, why="apart", override=False)

    def stale(self, read_at="old"):
        if read_at == "old":
            read_at = self.now - sharedinput.AVAILABILITY_TTL - 30
        fields = {"apart_now": True, "why": "old", "override": False}
        if read_at is not None:
            fields["read_at"] = read_at
        self.memo(**fields)

    def test_start_allows_stale_apart_and_clipboard_still_pauses(self):
        self.stale()
        self.assertEqual(sharedinput.sharing_availability(self.state, now=self.now)["state"], "unknown")
        self.assertEqual(sharedinput.sharing_availability(self.state, now=self.now)["settled"], "apart")
        self.assertIsNone(sharedinput.start_blocked(self.state, self.config, now=self.now))
        self.assertEqual(sharedinput.sharing_paused(self.state), "the laptops are apart")
        with unittest.mock.patch.object(sharedinput, "start", return_value={"started": True}) as start, \
                unittest.mock.patch.object(sharedinput, "stop") as stop, \
                unittest.mock.patch.object(sharedinput, "lan_mouse_status") as status:
            ok, _ = sharedinput.control("start", state=self.state, config=self.config, now=self.now)
        self.assertTrue(ok)
        start.assert_called_once()
        stop.assert_not_called()
        status.assert_not_called()
        self.stale(read_at=None)  # no timestamp: still settled apart, still not a start block
        self.assertIsNone(sharedinput.start_blocked(self.state, self.config, now=self.now))
        self.assertEqual(sharedinput.sharing_paused(self.state), "the laptops are apart")

    def test_pair_refuses_fresh_and_stale_apart_before_any_write_or_restart(self):
        for name, plant in (("fresh", self.fresh), ("stale", self.stale),
                            ("no-timestamp", lambda: self.stale(read_at=None))):
            with self.subTest(name=name):
                plant()
                cfg_path = self.state / "lan-mouse-config.toml"
                cfg_path.write_text("port = 4242\n", encoding="utf-8")
                before = cfg_path.read_bytes()
                remote = unittest.mock.Mock()
                with unittest.mock.patch.object(sharedinput, "config_path", return_value=cfg_path), \
                        unittest.mock.patch.object(sharedinput, "write_pairing",
                                                   side_effect=AssertionError("config write")) as write, \
                        unittest.mock.patch.object(sharedinput, "start",
                                                   side_effect=AssertionError("start")) as start, \
                        unittest.mock.patch.object(sharedinput, "stop",
                                                   side_effect=AssertionError("stop")) as stop, \
                        unittest.mock.patch.object(sharedinput, "lan_mouse_status",
                                                   side_effect=AssertionError("status")) as status, \
                        unittest.mock.patch.object(sharedinput, "local_info",
                                                   side_effect=AssertionError("local")) as info:
                    with self.assertRaises(RuntimeError) as raised:
                        sharedinput.pair(self.config, self.state, "dell", "right", remote)
                self.assertIn("Remote control still works", str(raised.exception))
                remote.call.assert_not_called()
                write.assert_not_called()
                start.assert_not_called()
                stop.assert_not_called()
                status.assert_not_called()
                info.assert_not_called()
                self.assertEqual(cfg_path.read_bytes(), before)
                self.assertFalse(list(self.state.glob("*.bak-*")))

    def test_switch_off_does_not_take_the_apart_refusal(self):
        self.fresh()
        config = {"lan_mouse": {"off_when_apart": False}}
        self.assertIsNone(sharedinput.pair_blocked(self.state, config, now=self.now))
        self.assertIsNone(sharedinput.start_blocked(self.state, config, now=self.now))
        self.stale()
        self.assertIsNone(sharedinput.pair_blocked(self.state, config, now=self.now))
        # The clipboard follows the settled reading, not the switch.
        self.assertEqual(sharedinput.sharing_paused(self.state), "the laptops are apart")
        remote = unittest.mock.Mock()
        remote.call.return_value = (True, json.dumps({"hostname": "dell", "addresses": [],
                                                      "lan_mouse": {"fingerprint": None}}))
        with unittest.mock.patch.object(sharedinput, "local_info",
                                        return_value={"hostname": "here", "addresses": ["192.0.2.1"],
                                                      "lan_mouse": {"fingerprint": "ab"}}), \
                unittest.mock.patch.object(sharedinput, "write_pairing",
                                           side_effect=AssertionError("config write")) as write, \
                unittest.mock.patch.object(sharedinput, "start") as start, \
                unittest.mock.patch.object(sharedinput, "stop") as stop:
            with self.assertRaisesRegex(ValueError, "no certificate on dell"):
                sharedinput.pair(config, self.state, "dell", "right", remote)
        remote.call.assert_called_once()
        self.assertEqual(remote.call.call_args.args[1], "input_info")
        write.assert_not_called()
        start.assert_not_called()
        stop.assert_not_called()

    def test_direct_write_pairing_still_writes_but_does_not_restart_when_stale(self):
        self.stale()
        path = self.state / "lan-mouse-config.toml"
        path.write_text("port = 4242\n", encoding="utf-8")
        fingerprint = ":".join(["ab"] * 32)
        with unittest.mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": True}), \
                unittest.mock.patch.object(sharedinput, "start", side_effect=AssertionError("start")) as start, \
                unittest.mock.patch.object(sharedinput, "stop", side_effect=AssertionError("stop")) as stop:
            out = sharedinput.write_pairing("Dell", ["192.0.2.20"], "right", fingerprint,
                                            path, state=self.state, config=self.config)
        start.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(out["restarted"])
        self.assertEqual(sharedinput.read_config(path)["clients"][0]["ips"], ["192.0.2.20"])

    def test_cli_pair_refuses_fresh_and_stale_apart_before_pair_runs(self):
        from jarvis.__main__ import main
        cfg = self.state / "jarvis.config.json"
        cfg.write_text(json.dumps({
            "cluster": {"enabled": False},
            "devices": {"dell": {"hostname": "pair-test-peer.invalid", "label": "Dell",
                                 "transport": "ssh", "target": "david@192.0.2.10"}},
            "lan_mouse": {"off_when_apart": True},
        }), encoding="utf-8")
        config_bytes = cfg.read_bytes()
        lan = self.state / "lan-mouse-config.toml"
        lan.write_text("port = 4242\n", encoding="utf-8")
        lan_bytes = lan.read_bytes()
        for name, plant in (("fresh", self.fresh), ("stale", self.stale),
                            ("no-timestamp", lambda: self.stale(read_at=None))):
            with self.subTest(name=name):
                plant()
                memo_bytes = (self.state / sharedinput.GUARD_FILE).read_bytes()
                out, err = io.StringIO(), io.StringIO()
                with unittest.mock.patch.object(sharedinput, "pair",
                                                side_effect=AssertionError("pair ran")) as pair, \
                        unittest.mock.patch.object(sharedinput, "write_pairing",
                                                   side_effect=AssertionError("config write")) as write, \
                        unittest.mock.patch.object(sharedinput, "start",
                                                   side_effect=AssertionError("start")) as start, \
                        unittest.mock.patch.object(sharedinput, "stop",
                                                   side_effect=AssertionError("stop")) as stop, \
                        unittest.mock.patch.object(sharedinput, "lan_mouse_status",
                                                   side_effect=AssertionError("status")) as status, \
                        unittest.mock.patch.object(sharedinput, "local_info",
                                                   side_effect=AssertionError("local")) as info, \
                        unittest.mock.patch.object(sharedinput, "config_path", return_value=lan), \
                        unittest.mock.patch.object(sys, "argv",
                                                   ["jarvis", "--config", str(cfg), "--state", str(self.state),
                                                    "lanmouse", "pair", "--device", "dell", "--position", "right"]), \
                        redirect_stdout(out), redirect_stderr(err):
                    code = main()
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn("Remote control still works", err.getvalue())
                pair.assert_not_called()
                write.assert_not_called()
                start.assert_not_called()
                stop.assert_not_called()
                status.assert_not_called()
                info.assert_not_called()
                self.assertEqual(cfg.read_bytes(), config_bytes)
                self.assertEqual(lan.read_bytes(), lan_bytes)
                self.assertEqual((self.state / sharedinput.GUARD_FILE).read_bytes(), memo_bytes)
                self.assertEqual(out.getvalue(), "")
