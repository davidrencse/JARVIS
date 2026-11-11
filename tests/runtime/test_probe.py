import unittest
from unittest.mock import patch

from jarvis import probe
from jarvis.probe import Prober, parse_kv, parse_netstat_udp, remote_summary, verdict

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  UDP    0.0.0.0:4242           *:*                                    13584
  UDP    0.0.0.0:55327          172.1.42.215:4242                     13584
  UDP    0.0.0.0:5353           *:*                                    999
"""
REMOTE = """host=archmain
uptime=110949.42
load=0.98
lm_unit=active
lm_fallback=inactive
lm_udp=1
mem_total_kb=16000000
mem_avail_kb=992000
swap_total_kb=8000000
swap_free_kb=5136000
powersave=off
ip=172.1.42.215
"""
LOCAL_UP = {"lan_mouse": {"running": True, "pid": 1, "listening": True, "peers": ["172.1.42.215"]}}


class ParseTests(unittest.TestCase):
    def test_netstat_finds_listen_and_peer_for_pid(self):
        self.assertEqual(parse_netstat_udp(NETSTAT, 13584), (True, ["172.1.42.215"]))
        self.assertEqual(parse_netstat_udp(NETSTAT, 999), (False, []))

    def test_remote_summary(self):
        s = remote_summary(parse_kv(REMOTE))
        self.assertEqual((s["hostname"], s["ip"], s["mem_avail_pct"], s["swap_used_pct"]),
                         ("archmain", "172.1.42.215", 6.2, 35.8))
        self.assertTrue(s["lan_mouse"]["running"] and s["lan_mouse"]["listening"])

    def test_carriage_returns_are_not_values(self):
        # A \r\n script once broke every field; values must never carry \r.
        self.assertEqual(parse_kv("ip=1.2.3.4\r\nload=1\r\n")["ip"], "1.2.3.4")


class VerdictTests(unittest.TestCase):
    def remote(self, **kw):
        base = {"reachable": True, "ssh": True, **remote_summary(parse_kv(REMOTE))}
        base["mem_avail_pct"] = 40
        base.update(kw)
        return base

    def test_all_up(self):
        level, lines = verdict(LOCAL_UP, self.remote(), "172.1.42.215")
        self.assertEqual(level, "up")

    def test_unreachable_is_down(self):
        level, lines = verdict(LOCAL_UP, {"reachable": False, "error": "asleep"}, "172.1.42.215")
        self.assertEqual((level, lines[0]), ("down", "asleep"))

    def test_dhcp_move_memory_and_powersave_warn(self):
        level, lines = verdict(LOCAL_UP, self.remote(ip="172.1.9.9", mem_avail_pct=6.2,
                                                     wifi_power_save="on"), "172.1.42.215")
        text = " ".join(lines)
        self.assertEqual(level, "warn")
        self.assertIn("daemons are up", lines[0])  # warnings never hide that the link is up
        self.assertIn("moved to 172.1.9.9", text)
        self.assertIn("6.2% free", text)
        self.assertIn("power save is on", text)

    def test_local_daemon_missing_is_down(self):
        level, lines = verdict({"lan_mouse": {"running": False}}, self.remote(), "172.1.42.215")
        self.assertEqual(level, "down")
        self.assertIn("isn't running on the Dell", lines[0])

    def test_projection_rewrites_apart_and_leaves_unknown_honest(self):
        from jarvis.sharedinput import present_input_link
        level, lines = verdict({"lan_mouse": {"running": False}}, self.remote(lan_mouse={
            "running": False, "listening": False, "unit": "inactive", "fallback": "inactive"}), "172.1.42.215")
        apart_level, apart_lines, _ = present_input_link(level, lines, {
            "state": "apart", "enforced": True, "remote": True, "why": "Dell is on another network."})
        self.assertEqual(apart_level, "warn")
        self.assertIn("Remote control still works", apart_lines[0])
        self.assertNotIn("isn't running", " ".join(apart_lines))
        unknown_level, unknown_lines, _ = present_input_link(level, lines, {
            "state": "unknown", "enforced": True, "why": "The guard hasn't read the network lately; can't tell."})
        self.assertEqual(unknown_level, "down")
        self.assertIn("Can't tell", unknown_lines[0])
        self.assertNotIn("apart", unknown_lines[0].lower())
        same_level, same_lines, _ = present_input_link("up", ["Both Lan Mouse daemons are up."], {
            "state": "together", "enforced": True})
        self.assertEqual((same_level, same_lines), ("up", ["Both Lan Mouse daemons are up."]))


class ProbeTests(unittest.TestCase):
    def test_unreachable_host_skips_ssh(self):
        with patch.object(probe, "tcp_ping", return_value=None), \
                patch.object(probe.subprocess, "run") as run:
            out = probe.probe_ssh("jdoe@10.0.0.9")
        self.assertFalse(out["reachable"])
        run.assert_not_called()

    def test_script_is_sent_as_bytes(self):
        class Done:
            returncode, stdout, stderr = 0, REMOTE.encode(), b""
        with patch.object(probe, "tcp_ping", return_value=5.0), \
                patch.object(probe.shutil, "which", return_value="ssh"), \
                patch.object(probe.subprocess, "run", return_value=Done()) as run:
            out = probe.probe_ssh("jdoe@172.1.42.215", None)
        self.assertIsInstance(run.call_args.kwargs["input"], bytes)
        self.assertNotIn(b"\r", run.call_args.kwargs["input"])
        self.assertEqual(out["hostname"], "archmain")

    def test_prober_caches(self):
        p = Prober({"devices": {}})
        with patch.object(Prober, "_probe", side_effect=[{"checked_at": 1e12}, {"checked_at": 2e12}]):
            self.assertIs(p.get(), p.get())
            self.assertEqual(p.get(refresh=True)["checked_at"], 2e12)


if __name__ == "__main__":
    unittest.main()
