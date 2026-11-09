import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import sharedinput as si

FP = "6a:bd:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee"
CONFIG = f'''release_bind = [ "KeyLeftCtrl", "KeyLeftShift", "KeyLeftMeta", "KeyLeftAlt" ]
port = 4242
motion_scale = 0.666667
# authorized peers
[authorized_fingerprints]
"{FP}" = "WinLaptop"

[[clients]]
position = "left"
hostname = "archmain"
activate_on_startup = true
ips = [ "172.1.41.116", "100.64.0.11" ]
port = 4242
'''


def write_config(text=CONFIG):
    d = tempfile.mkdtemp()
    p = Path(d) / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def mon(w, h):
    return {"name": "m", "width": w, "height": h, "primary": True}


def info(host, position, scale, ips, addrs, running=True, build="patched",
         bind=("KeyLeftCtrl",), peer="other"):
    return {"hostname": host, "os": "linux", "monitors": [], "addresses": addrs,
            "lan_mouse": {"running": running, "build": build, "config": {
                "port": 4242, "motion_scale": scale, "release_bind": list(bind),
                "clients": [{"position": position, "hostname": peer, "ips": ips, "port": 4242}]}}}


def good_pair():
    dell = {"id": "dell", "label": "Dell", "name": "Dell", "ppi": 141.2,
            "info": info("dell", "left", 0.666667, ["10.0.0.2"], ["10.0.0.1"], peer="zenbook")}
    zen = {"id": "zenbook", "label": "Zenbook", "name": "Zenbook", "ppi": 211.8,
           "info": info("zenbook", "right", 1.5, ["10.0.0.1"], ["10.0.0.2"], peer="dell")}
    return dell, zen


class ReadConfigTests(unittest.TestCase):
    def check(self, cfg):
        self.assertEqual(cfg["port"], 4242)
        self.assertAlmostEqual(cfg["motion_scale"], 0.666667)
        self.assertEqual(cfg["release_bind"], ["KeyLeftCtrl", "KeyLeftShift", "KeyLeftMeta", "KeyLeftAlt"])
        self.assertEqual(len(cfg["clients"]), 1)
        c = cfg["clients"][0]
        self.assertEqual((c["position"], c["hostname"], c["port"]), ("left", "archmain", 4242))
        self.assertEqual(c["ips"], ["172.1.41.116", "100.64.0.11"])
        dumped = json.dumps(cfg)
        self.assertNotIn(FP, dumped)
        self.assertNotIn("6a:bd", dumped)
        self.assertNotIn("WinLaptop", dumped)

    def test_tomllib(self):
        self.check(si.read_config(write_config()))

    def test_mini_toml(self):
        with mock.patch.object(si, "tomllib", None):
            self.check(si.read_config(write_config()))

    def test_missing(self):
        self.assertIsNone(si.read_config(Path(tempfile.mkdtemp()) / "nope.toml"))


class PpiTests(unittest.TestCase):
    def test_values(self):
        self.assertAlmostEqual(si.ppi(mon(1920, 1080), 15.6), 141.2, delta=0.1)
        self.assertAlmostEqual(si.ppi(mon(2880, 1620), 15.6), 211.8, delta=0.1)

    def test_none(self):
        self.assertIsNone(si.ppi(mon(1920, 1080), None))
        self.assertIsNone(si.ppi(None, 15.6))


class ChecksTests(unittest.TestCase):
    def test_all_good(self):
        out = si.checks(list(good_pair()))
        self.assertTrue(out)
        self.assertEqual({c["level"] for c in out}, {"ok"}, out)

    def test_stale_address(self):
        dell, zen = good_pair()
        dell["info"]["lan_mouse"]["config"]["clients"][0]["ips"] = ["10.0.0.2", "9.9.9.9"]
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "warn" and "9.9.9.9" in c["text"] for c in out), out)

    def test_disagreeing_positions(self):
        dell, zen = good_pair()
        zen["info"]["lan_mouse"]["config"]["clients"][0]["position"] = "left"
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "down" for c in out), out)

    def test_wrong_scale(self):
        dell, zen = good_pair()
        dell["info"]["lan_mouse"]["config"]["motion_scale"] = 1.0
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "warn" and "should be about" in c["text"] for c in out), out)

    def test_not_running(self):
        dell, zen = good_pair()
        dell["info"]["lan_mouse"]["running"] = False
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "warn" and "isn't running" in c["text"] for c in out), out)

    def test_official_build(self):
        dell, zen = good_pair()
        dell["info"]["lan_mouse"]["build"] = "official"
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "warn" and "official" in c["text"] for c in out), out)

    def test_info_none(self):
        dell, zen = good_pair()
        zen["info"], zen["error"] = None, "refused"
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "down" and "refused" in c["text"] for c in out), out)

    def test_release_bind_differs(self):
        dell, zen = good_pair()
        zen["info"]["lan_mouse"]["config"]["release_bind"] = ["KeyLeftAlt"]
        out = si.checks([dell, zen])
        self.assertTrue(any(c["level"] == "warn" and "release" in c["text"] for c in out), out)


class LocalInfoTests(unittest.TestCase):
    def test_never_raises(self):
        boom = mock.Mock(side_effect=RuntimeError("x"))
        with mock.patch.object(si, "monitors", boom), mock.patch.object(si, "addresses", boom), \
                mock.patch.object(si, "lan_mouse_status", boom), mock.patch.object(si, "read_config", boom), \
                mock.patch.object(si, "_fingerprint", boom):
            got = si.local_info()
        self.assertEqual(got["monitors"], [])
        self.assertEqual(got["addresses"], [])
        self.assertEqual(got["lan_mouse"], {"running": False, "build": None, "config": None,
                                            "fingerprint": None})
        self.assertIn("hostname", got)

    def test_run(self):
        with mock.patch.object(si, "local_info", return_value={"hostname": "h"}):
            ok, text = si.run("")
        self.assertTrue(ok)
        self.assertEqual(json.loads(text), {"hostname": "h"})

    def test_run_real_json(self):
        with mock.patch.object(si, "monitors", return_value=[]), \
                mock.patch.object(si, "addresses", return_value=["1.2.3.4"]), \
                mock.patch.object(si, "lan_mouse_status", return_value={"running": True, "build": "patched"}), \
                mock.patch.object(si, "read_config", return_value=None):
            ok, text = si.run()
        self.assertTrue(ok)
        self.assertEqual(json.loads(text)["addresses"], ["1.2.3.4"])


class FakeRemote:
    def __init__(self, here, replies):
        self._here, self.replies, self.calls = here, replies, []

    def here(self):
        return self._here

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        return self.replies[device]


class OverviewTests(unittest.TestCase):
    CFG = {"devices": {
        "dell": {"label": "Dell XPS, Windows", "screen_inches": 15.6},
        "zenbook": {"label": "Zenbook, Arch", "screen_inches": 15.6}}}

    def local(self):
        i = info("dell", "left", 0.666667, ["10.0.0.2"], ["10.0.0.1"], peer="zenbook")
        i["monitors"] = [mon(1920, 1080)]
        return i

    def peer(self):
        i = info("zenbook", "right", 1.5, ["10.0.0.1"], ["10.0.0.2"], peer="dell")
        i["monitors"] = [mon(2880, 1620)]
        return i

    def test_overview(self):
        remote = FakeRemote("dell", {"zenbook": (True, json.dumps(self.peer()))})
        with mock.patch.object(si, "local_info", return_value=self.local()):
            out = si.overview(self.CFG, None, remote=remote)
        self.assertEqual(remote.calls, [("zenbook", "input_info", "")])
        self.assertEqual(out["here"], "dell")
        self.assertEqual(out["layout"], {"dell": "center", "zenbook": "left"})
        by = {d["id"]: d for d in out["devices"]}
        self.assertEqual(by["dell"]["name"], "Dell XPS")
        self.assertEqual(by["zenbook"]["name"], "Zenbook")
        self.assertTrue(by["dell"]["here"])
        self.assertEqual(by["dell"]["screen_inches"], 15.6)
        self.assertAlmostEqual(by["dell"]["ppi"], 141.2, delta=0.1)
        self.assertAlmostEqual(by["zenbook"]["ppi"], 211.8, delta=0.1)
        self.assertTrue(all(c["level"] == "ok" for c in out["checks"]), out["checks"])

    def test_remote_failure(self):
        remote = FakeRemote("dell", {"zenbook": (False, "refused")})
        with mock.patch.object(si, "local_info", return_value=self.local()):
            out = si.overview(self.CFG, None, remote=remote)
        z = next(d for d in out["devices"] if d["id"] == "zenbook")
        self.assertIsNone(z["info"])
        self.assertEqual(z["error"], "refused")
        self.assertIsNone(z["ppi"])
        self.assertTrue(any(c["level"] == "down" for c in out["checks"]))


class WiringTests(unittest.TestCase):
    def test_actions(self):
        from jarvis import remote
        self.assertIn("input_info", remote.ACTIONS)
        self.assertIn("input_info", remote.ARG)

    def test_run_here_routes(self):
        from jarvis import remote
        with mock.patch.object(si, "run", return_value=(True, "{}")) as run:
            self.assertEqual(remote.run_here("input_info", ""), (True, "{}"))
        run.assert_called_once_with("")

    def test_console_route(self):
        from jarvis.ui import Console
        fake = SimpleNamespace(config={"devices": {}}, state=Path(tempfile.mkdtemp()))
        with mock.patch.object(si, "overview", return_value={"ok": 1}) as ov:
            self.assertEqual(Console.shared_input(fake), {"ok": 1})
        ov.assert_called_once_with(fake.config, fake.state)
        src = (Path(__file__).resolve().parent.parent / "jarvis" / "ui.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(re.escape('"/api/input/shared"'), src)), 1)


if __name__ == "__main__":
    unittest.main()


class StatusCostTests(unittest.TestCase):
    def test_stop_reads_the_status_once(self):
        with mock.patch.object(si, "os") as fake_os, \
                mock.patch.object(si, "_run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run, \
                mock.patch.object(si, "lan_mouse_status", return_value={"running": False, "build": None}) as st:
            fake_os.name = "nt"
            self.assertTrue(si.stop()["stopped"])
        st.assert_called_once()
        # taskkill, then one cheap tasklist to confirm the exit (never the slow CIM query)
        self.assertEqual([c.args[0] for c in run.call_args_list], ["taskkill", "tasklist"])

    def test_start_polls_cheaply_on_windows(self):
        calls = []

        def run(*args, timeout=5):
            calls.append(args[0])
            return SimpleNamespace(stdout="lan-mouse.exe,1\n" if args[0] == "tasklist" else "",
                                   returncode=1 if args[0] == "schtasks" else 0, stderr="")
        with mock.patch.object(si, "os") as fake_os, mock.patch.object(si, "_run", side_effect=run), \
                mock.patch.object(si, "_desktop", return_value="windows"), \
                mock.patch.object(si, "_units", return_value=[]), \
                mock.patch.object(si, "_windows_launcher", return_value=Path("/nonexistent")), \
                mock.patch.object(si, "_windows_once_launcher", return_value=Path("/nonexistent")), \
                mock.patch.object(si, "_binary", return_value="lan-mouse.exe"), \
                mock.patch.object(si.subprocess, "Popen") as popen, \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(Path, "mkdir"), \
                mock.patch("time.sleep"), \
                mock.patch.object(si, "lan_mouse_status", side_effect=[
                    {"running": False, "build": None}, {"running": True, "build": "patched"}]) as st:
            fake_os.name = "nt"
            fake_os.environ = {}
            out = si.start()
        popen.assert_called_once()
        self.assertTrue(out["started"])
        self.assertEqual(st.call_count, 2)  # before launching, and once for the build type
        self.assertIn("tasklist", calls)


class LocalNetworkFamilyTests(unittest.TestCase):
    IP_ADDR = ("2: wlan0    inet 172.1.0.23/16 brd 172.1.255.255 scope global wlan0\n"
               "2: wlan0    inet6 2601:1::5/64 scope global\n"
               "3: tailscale0    inet 100.84.1.2/32 scope global tailscale0\n")

    def nets(self, **kw):
        with mock.patch.object(si.os, "name", "posix"), \
                mock.patch.object(si.shutil, "which", return_value="/usr/bin/ip"), \
                mock.patch.object(si, "_run", return_value=SimpleNamespace(stdout=self.IP_ADDR)):
            return [str(n) for n in si.local_networks(**kw)]

    def test_guard_reads_ipv4_only_and_address_checks_get_ipv6_too(self):
        self.assertEqual(self.nets(), ["172.1.0.0/16"])
        self.assertEqual(self.nets(families=(4, 6)), ["172.1.0.0/16", "2601:1::/64"])

    def test_macos_ifconfig(self):
        text = ("en0: flags=8863<UP> mtu 1500\n\tinet6 fe80::1c%en0 prefixlen 64 scopeid 0x4\n"
                "\tinet 172.1.0.23 netmask 0xffffff00 broadcast 172.1.0.255\n"
                "lo0: flags=8049<UP>\n\tinet 127.0.0.1 netmask 0xff000000\n")
        with mock.patch.object(si.os, "name", "posix"), \
                mock.patch.object(si.shutil, "which", side_effect=lambda b: None if b == "ip" else "/sbin/ifconfig"), \
                mock.patch.object(si, "_run", return_value=SimpleNamespace(stdout=text)):
            self.assertEqual([str(n) for n in si.local_networks(families=(4, 6))], ["fe80::/64", "172.1.0.0/24"])


class ElevatedTaskTests(unittest.TestCase):
    def win(self, existing, status=None):
        calls = []

        def run(*args, timeout=5):
            calls.append(args)
            if args[0] == "schtasks" and args[1] == "/Query":
                return SimpleNamespace(stdout="", stderr="", returncode=0 if args[3] in existing else 1)
            if args[0] == "taskkill":
                return SimpleNamespace(stdout="", stderr="Access is denied", returncode=1)
            return SimpleNamespace(stdout="lan-mouse.exe,1\n" if args[0] == "tasklist" else "", stderr="", returncode=0)
        return calls, run

    def patches(self, run, statuses):
        fake_os = mock.MagicMock()
        fake_os.name = "nt"
        fake_os.environ = {"USERDOMAIN": "PC", "USERNAME": "david", "LOCALAPPDATA": tempfile.mkdtemp()}
        return [mock.patch.object(si, "os", fake_os), mock.patch.object(si, "_run", side_effect=run),
                mock.patch.object(si, "_desktop", return_value="windows"),
                mock.patch.object(si, "_units", return_value=[]),
                mock.patch("time.sleep"),
                mock.patch.object(si, "lan_mouse_status", side_effect=statuses)]

    def enter(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_start_uses_the_task_when_it_exists(self):
        calls, run = self.win({si.ELEVATED_TASK})
        self.enter(self.patches(run, [{"running": False, "build": None}, {"running": True, "build": "patched"}]))
        with mock.patch.object(si.subprocess, "Popen") as popen:
            self.assertTrue(si.start()["started"])
        popen.assert_not_called()
        self.assertIn(("schtasks", "/Run", "/TN", si.ELEVATED_TASK), calls)

    def test_start_falls_back_without_the_task(self):
        calls, run = self.win(set())
        self.enter(self.patches(run, [{"running": False, "build": None}, {"running": True, "build": "patched"}]))
        with mock.patch.object(si, "_windows_launcher", return_value=Path("/nonexistent")), \
                mock.patch.object(si, "_windows_once_launcher", return_value=Path("/nonexistent")), \
                mock.patch.object(si, "_binary", return_value="lan-mouse.exe"), \
                mock.patch.object(si.subprocess, "Popen") as popen, \
                mock.patch("builtins.open", mock.mock_open()), mock.patch.object(Path, "mkdir"):
            self.assertTrue(si.start()["started"])
        popen.assert_called_once()
        self.assertNotIn(("schtasks", "/Run", "/TN", si.ELEVATED_TASK), calls)

    def test_stop_runs_the_stop_task(self):
        calls, run = self.win({si.ELEVATED_STOP_TASK})
        self.enter(self.patches(run, [{"running": False, "build": None}]))
        out = si.stop()
        self.assertIn(("schtasks", "/Run", "/TN", si.ELEVATED_STOP_TASK), calls)
        self.assertTrue(out["stopped"])

    def test_admin_stop_waits_for_the_exit(self):
        # From an admin terminal taskkill succeeds but returns before the process is gone;
        # elevate()'s start() right after then saw it "already running" and left it off.
        calls, run = self.win(set())
        lingering = iter([True, True, False])
        self.enter(self.patches(run, [{"running": False, "build": None}]))
        with mock.patch.object(si, "_run", side_effect=lambda *a, timeout=5: (
                SimpleNamespace(stdout="", stderr="", returncode=0) if a[0] == "taskkill" else run(*a, timeout=timeout))), \
                mock.patch.object(si, "_quick_status", side_effect=lambda: {"running": next(lingering)}) as quick:
            self.assertTrue(si.stop()["stopped"])
        self.assertEqual(quick.call_count, 3)

    def test_elevate_refuses_when_not_admin(self):
        calls, run = self.win(set())
        self.enter(self.patches(run, []))
        with mock.patch.object(si, "_is_admin", return_value=False), self.assertRaises(ValueError) as cm:
            si.elevate()
        self.assertIn("admin terminal", str(cm.exception))
        self.assertFalse([c for c in calls if c[1:2] == ("/Create",)])

    def test_elevate_creates_both_tasks(self):
        import tempfile
        calls, run = self.win(set())
        self.enter(self.patches(run, [{"running": False, "build": None}] * 3))
        copied = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_is_admin", return_value=True), \
                mock.patch.object(si, "install_dir", return_value=Path(tmp)), \
                mock.patch.object(si, "_install_copy", lambda src, dst: copied.append((src, dst))), \
                mock.patch.object(si, "start", return_value={"started": True}) as start, \
                mock.patch.object(si, "_binary", return_value=str(Path("/home/u/lm/bin/lan-mouse.exe"))):
            out = si.elevate()
            vbs = (Path(tmp) / "start-elevated.vbs").read_text()
        # Audit cluster-07: it wasn't running (say the guard stopped it while apart): stays off.
        start.assert_not_called()
        self.assertEqual(out, {"elevated": True, "tasks": [si.ELEVATED_TASK, si.ELEVATED_STOP_TASK], "running": False})
        self.assertEqual(len([c for c in calls if c[:2] == ("schtasks", "/Create")]), 2)
        # The elevated task runs only the admin-only copy, never the user-writable original.
        self.assertEqual(copied, [(Path("/home/u/lm/bin/lan-mouse.exe"), Path(tmp))])
        self.assertIn(str(Path(tmp) / "lan-mouse.exe"), vbs)
        self.assertNotIn("/home/u/lm", vbs)

    def test_elevate_restarts_a_running_daemon_through_the_task(self):
        import tempfile
        calls, run = self.win(set())
        self.enter(self.patches(run, [{"running": True, "build": "official"}] * 3))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_is_admin", return_value=True), \
                mock.patch.object(si, "install_dir", return_value=Path(tmp)), \
                mock.patch.object(si, "_install_copy"), \
                mock.patch.object(si, "stop") as stop, \
                mock.patch.object(si, "start", return_value={"started": True}) as start, \
                mock.patch.object(si, "_binary", return_value=str(Path("/home/u/lm/bin/lan-mouse.exe"))):
            out = si.elevate()
        stop.assert_called_once()
        start.assert_called_once()
        self.assertTrue(out["running"])

    def test_elevated_copy_counts_as_the_patched_build(self):
        # Audit cluster-06: the Program Files copy elevate() runs was labelled official.
        with mock.patch.object(si, "install_dir", return_value=Path("C:/Program Files/JARVIS Lan Mouse")):
            self.assertEqual(si._windows_build(r"C:\Program Files\JARVIS Lan Mouse\lan-mouse.exe"), "patched")
            self.assertEqual(si._windows_build(r"C:\Users\d\AppData\Local\lan-mouse\bin\lan-mouse.exe"), "patched")
            self.assertEqual(si._windows_build(r"C:\Program Files\Lan Mouse\lan-mouse.exe"), "official")
            self.assertEqual(si._windows_build(r"C:\Program Files\JARVIS Lan Mouse Old\lan-mouse.exe"), "official")

    def test_install_copy_takes_only_the_exe_and_dlls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as home:
            src, home = Path(src), Path(home)
            for name in ("lan-mouse.exe", "lan-mouse.exe.pre-uacfix", "lan-mouse.exe.off", "SDL2.dll"):
                (src / name).write_text(name)
            for name in ("lan-mouse.exe.pre-kbdfix", "daemon.log", "start-elevated.vbs"):
                (home / name).write_text("old")
            si._install_copy(src / "lan-mouse.exe", home)
            self.assertEqual(sorted(p.name for p in home.iterdir()),
                             ["SDL2.dll", "daemon.log", "lan-mouse.exe", "start-elevated.vbs"])
            self.assertEqual((home / "lan-mouse.exe").read_text(), "lan-mouse.exe")

    def test_elevated_daemon_with_a_hidden_path_still_counts_as_running(self):
        # An unelevated CIM query returns no ExecutablePath for the elevated daemon.
        empty = mock.Mock(stdout="", returncode=0)
        with mock.patch.object(si, "on_windows", return_value=True), \
                mock.patch.object(si, "_desktop", return_value="windows"), \
                mock.patch.object(si, "_run", return_value=empty), \
                mock.patch.object(si, "_quick_status", return_value={"running": True, "build": None}), \
                mock.patch.object(si, "_task_exists", return_value=True):
            self.assertEqual(si.lan_mouse_status(), {"running": True, "build": "patched"})
        with mock.patch.object(si, "on_windows", return_value=True), \
                mock.patch.object(si, "_desktop", return_value="windows"), \
                mock.patch.object(si, "_run", return_value=empty), \
                mock.patch.object(si, "_quick_status", return_value={"running": False, "build": None}):
            self.assertEqual(si.lan_mouse_status(), {"running": False, "build": None})

    def test_task_xml(self):
        xml = si._task_xml("wscript.exe", '"C:\\a&b\\x.vbs"', "PC\\david")
        for needle in ("HighestAvailable", "InteractiveToken", "PT0S", "a&amp;b", "PC\\david",
                       "<AllowStartOnDemand>true", "<Hidden>true"):
            self.assertIn(needle, xml)
        self.assertNotIn("<Triggers", xml)
        self.assertNotIn("a&b", xml)
