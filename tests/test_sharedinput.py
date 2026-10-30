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
