"""System intents (jarvis/sysintents.py): parsing, validation, command lines, honest replies.
Every subprocess and the network are fakes: nothing here dims a screen, grabs one or locks."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import registry, sysintents
from jarvis.parse import parse

DEVICES = {"zenbook": {"hostname": "z", "aliases": ["arch"]},
           "dell": {"hostname": "d", "aliases": []}}


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Fake:
    """Stands in for sysintents._run: records command lines, answers from `replies`."""

    def __init__(self, replies=None, effect=None):
        self.calls, self.replies, self.effect = [], replies or {}, effect

    def __call__(self, *args, timeout=10):
        self.calls.append(list(args))
        if self.effect:
            self.effect(args)
        for prefix, reply in self.replies.items():
            if " ".join(args).startswith(prefix):
                return reply(args) if callable(reply) else reply
        return done()


def linux(fake, tools=("brightnessctl", "grim", "hyprctl", "nmcli", "bluetoothctl", "loginctl",
                       "notify-send")):
    which = lambda name: f"/usr/bin/{name}" if name in tools else None
    return mock.patch.multiple(sysintents, _run=fake, _linux=lambda: True), \
        mock.patch.object(sysintents.shutil, "which", which)


class Env(unittest.TestCase):
    def patched(self, fake, **kw):
        for p in linux(fake, **kw):
            p.start()
            self.addCleanup(p.stop)
        return fake


def parsed(text):
    got = parse(text, DEVICES, "zenbook")
    return [(c["intent"], c["args"]) for c in got["commands"]] if got["commands"] else None


class ParsingTests(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "set brightness to 50 percent": [("brightness", {"level": "50"})],
            "brightness up": [("brightness", {"level": "up"})],
            "brightness down": [("brightness", {"level": "down"})],
            "dim the screen": [("brightness", {"level": "down"})],
            "make the screen brighter": [("brightness", {"level": "up"})],
            "take a screenshot": [("screenshot", {})],
            "screenshot this window": [("screenshot", {"window": True})],
            "how much battery do I have": [("system_status", {"what": "battery"})],
            "what wifi am I on": [("system_status", {"what": "wifi"})],
            "is bluetooth on": [("system_status", {"what": "bluetooth"})],
            "what windows are open": [("system_status", {"what": "windows"})],
            "switch to workspace 3": [("workspace", {"n": "3"})],
            "go to workspace two": [("workspace", {"n": "2"})],
            "lock my screen": [("lock_screen", {})],
            "remind me in 10 minutes to call mom": [("reminder", {"minutes": "10", "text": "call mom"})],
            "remind me to stretch in 2 hours": [("reminder", {"minutes": "120", "text": "stretch"})],
            "set a timer for 5 minutes": [("reminder", {"minutes": "5"})],
            "what's the weather": [("weather", {})],
            "what's the weather in London tomorrow": [("weather", {"city": "london", "day": "tomorrow"})],
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parsed(text), want)

    def test_out_of_range_or_unclear_is_not_parsed(self):
        for text in ("set brightness to 150", "switch to workspace 100", "remind me in 5 minutes",
                     "remind me in 3 days to x", "set a timer for 5 minutes and sing",
                     "remind me in 2 hours to stretch and drink water"):
            with self.subTest(text=text):
                self.assertIsNone(parsed(text))

    def test_screenshot_runs_after_a_preceding_command(self):
        got = parse("open chrome and take a screenshot", DEVICES, "zenbook")["commands"]
        self.assertEqual([c["intent"] for c in got], ["open_app", "screenshot"])
        self.assertEqual(got[1]["depends_on"], ["c1"])


class RegistryTests(unittest.TestCase):
    def check(self, intent, args):
        return registry.validate([{"id": "c1", "intent": intent, "device": "dell", "args": args,
                                   "depends_on": []}], DEVICES)

    def test_levels_and_bounds(self):
        for level in ("0", "100", "up", "down"):
            self.assertEqual(self.check("brightness", {"level": level}), [], level)
        for level in ("101", "-1", "loud", "", True):
            self.assertTrue(self.check("brightness", {"level": level}), level)
        self.assertTrue(self.check("brightness", {}))
        self.assertTrue(self.check("workspace", {"n": "0"}))
        self.assertEqual(self.check("workspace", {"n": "10"}), [])
        self.assertTrue(self.check("reminder", {"minutes": "0"}))
        self.assertTrue(self.check("reminder", {"minutes": "1441"}))
        self.assertTrue(self.check("reminder", {"minutes": "nan"}))
        self.assertTrue(self.check("system_status", {"what": "cpu"}))
        self.assertTrue(self.check("lock_screen", {"force": "1"}))
        self.assertTrue(self.check("weather", {"city": "a; rm -rf /"}))

    def test_all_are_direct_allow(self):
        for name in ("brightness", "screenshot", "system_status", "workspace", "lock_screen",
                     "reminder", "weather"):
            self.assertEqual((registry.get(name).executor, registry.get(name).risk), ("direct", "allow"))


class BrightnessTests(Env):
    def test_command_lines_and_readback(self):
        fake = self.patched(Fake({"brightnessctl -m": done("intel_backlight,backlight,500,40%,1000\n")}))
        for level, target in (("50", "50%"), ("up", "+10%"), ("down", "10%-")):
            fake.calls.clear()
            ok, text = sysintents.brightness(json.dumps({"level": level}))
            self.assertTrue(ok)
            self.assertEqual(fake.calls[0], ["brightnessctl", "-c", "backlight", "-n", "set", target])
            self.assertEqual(text, "Brightness is 40 percent.")  # the read-back, not the request

    def test_bad_level_runs_nothing(self):
        fake = self.patched(Fake())
        self.assertFalse(sysintents.brightness('{"level": "101"}')[0])
        self.assertEqual(fake.calls, [])

    def test_failure_and_missing_tool_are_reported(self):
        self.patched(Fake({"brightnessctl -c": done(code=1, stderr="no device")}))
        self.assertEqual(sysintents.brightness('{"level": "up"}'), (False, "no device"))
        self.patched(Fake(), tools=())
        self.assertIn("brightnessctl", sysintents.brightness('{"level": "up"}')[1])

    def test_not_linux(self):
        with mock.patch.object(sysintents, "_linux", return_value=False):
            ok, text = sysintents.brightness('{"level": "up"}')
        self.assertFalse(ok)
        self.assertIn("isn't supported", text)


class ScreenshotTests(Env):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()

    def test_path_policy(self):
        pictures = self.home / "Pictures"
        for good in (pictures / "a.png", "~/Downloads/x.png", "Documents/sub/y.jpg"):
            with mock.patch.object(Path, "home", return_value=self.home):
                target, why = sysintents.screenshot_path(str(good), self.home)
            self.assertIsNone(why, good)
        for bad in ("/etc/x.png", str(self.home / "secret.png"), str(self.home / ".ssh" / "a.png"),
                    str(pictures / ".." / ".ssh" / "a.png"), str(pictures / "a.txt"),
                    str(self.home / "Pictures"), "/tmp/x.png"):
            target, why = sysintents.screenshot_path(bad, self.home)
            self.assertIsNone(target, bad)
            self.assertTrue(why, bad)

    def test_symlink_escape_is_refused(self):
        (self.home / "Pictures").mkdir()
        (self.home / "Pictures" / "link").symlink_to(self.home / ".ssh", target_is_directory=False)
        target, why = sysintents.screenshot_path(str(self.home / "Pictures" / "link" / "a.png"), self.home)
        self.assertIsNone(target)

    def test_existing_file_is_not_overwritten(self):
        (self.home / "Pictures").mkdir()
        (self.home / "Pictures" / "a.png").write_bytes(b"x")
        self.assertIn("already exists", sysintents.screenshot_path("~/Pictures/a.png", self.home)[1])

    def test_default_path_is_timestamped_under_screenshots(self):
        target, why = sysintents.screenshot_path(None, self.home)
        self.assertIsNone(why)
        self.assertEqual(target.parent, self.home / "Pictures" / "Screenshots")
        self.assertRegex(target.name, r"jarvis-\d{8}-\d{6}\.png\Z")

    def run_shot(self, spec, writes=b"png", replies=None):
        def effect(args):
            if args[0] == "grim" and writes is not None:
                Path(args[-1]).write_bytes(writes)
        fake = self.patched(Fake(replies or {}, effect))
        with mock.patch.object(Path, "home", return_value=self.home):
            return fake, sysintents.screenshot(spec, home=self.home)

    def test_success_names_the_exact_path(self):
        path = self.home / "Pictures" / "Screenshots" / "one.png"
        fake, (ok, text) = self.run_shot({"path": str(path)})
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [["grim", str(path)]])
        self.assertIn(str(path), text)

    def test_window_uses_active_window_geometry(self):
        path = self.home / "Documents" / "w.png"
        geometry = json.dumps({"at": [10, 20], "size": [300, 200]})
        fake, (ok, text) = self.run_shot({"path": str(path), "window": True},
                                         replies={"hyprctl activewindow": done(geometry)})
        self.assertTrue(ok)
        self.assertEqual(fake.calls[-1], ["grim", "-g", "10,20 300x200", str(path)])

    def test_no_claim_without_a_file(self):
        path = self.home / "Pictures" / "ghost.png"
        for writes in (None, b""):  # grim "succeeds" but wrote nothing, or an empty file
            _, (ok, text) = self.run_shot({"path": str(path)}, writes=writes)
            self.assertFalse(ok)
            self.assertIn("wasn't saved", text)
            path.unlink(missing_ok=True)

    def test_grim_failure_and_refused_path_run_nothing(self):
        path = self.home / "Pictures" / "bad.png"
        _, (ok, text) = self.run_shot({"path": str(path)}, replies={"grim": done(code=1, stderr="no compositor")})
        self.assertEqual((ok, text), (False, "no compositor"))
        fake, (ok, text) = self.run_shot({"path": "/etc/passwd.png"})
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [])
        self.assertIn("outside", text)


class StatusTests(Env):
    def test_battery_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bat = Path(tmp) / "BAT0"
            bat.mkdir()
            (bat / "capacity").write_text("77\n")
            (bat / "status").write_text("Charging\n")
            self.assertEqual(sysintents._battery(tmp), (True, "Battery 77 percent, charging."))
            self.assertFalse(sysintents._battery(Path(tmp) / "none")[0])

    def test_wifi_bluetooth_windows(self):
        windows = [{"class": f"app{n}", "title": f"t{n}\nline", "mapped": True} for n in range(20)]
        self.patched(Fake({
            "nmcli -t -f ACTIVE,SSID dev wifi": done("no:Other\nyes:Home\\:5G\n"),
            "bluetoothctl show": done("Controller AA\n\tPowered: yes\n"),
            "hyprctl clients -j": done(json.dumps(windows))}))
        self.assertEqual(sysintents.system_status('{"what": "wifi"}'), (True, "Wi-Fi connected to Home:5G."))
        self.assertEqual(sysintents.system_status('{"what": "bluetooth"}'), (True, "Bluetooth is on."))
        ok, text = sysintents.system_status('{"what": "windows"}')
        self.assertTrue(ok)
        self.assertIn("20 windows open", text)
        self.assertIn("app14: t14 line", text)
        self.assertNotIn("app15", text)
        self.assertIn("(and 5 more)", text)

    def test_unconnected_and_off(self):
        self.patched(Fake({"nmcli": done("no:Other\n"), "bluetoothctl show": done("Powered: no\n")}))
        self.assertEqual(sysintents.system_status({"what": "wifi"}), (True, "Wi-Fi isn't connected."))
        self.assertEqual(sysintents.system_status({"what": "bluetooth"}), (True, "Bluetooth is off."))

    def test_unknown_what_and_failed_reads(self):
        self.patched(Fake({"nmcli": done(code=1, stderr="NM off")}))
        self.assertFalse(sysintents.system_status({"what": "cpu"})[0])
        self.assertEqual(sysintents.system_status({"what": "wifi"}), (False, "NM off"))


class WorkspaceAndLockTests(Env):
    def test_switch_is_read_back(self):
        fake = self.patched(Fake({"hyprctl dispatch": done("ok\n"),
                                  "hyprctl activeworkspace": done('{"id": 3}')}))
        with mock.patch.object(sysintents.time, "sleep"):
            self.assertEqual(sysintents.workspace({"n": 3}), (True, "Switched to workspace 3."))
            self.assertEqual(fake.calls[0], ["hyprctl", "dispatch", "hl.dsp.focus({ workspace = 3 })"])
            fake.replies["hyprctl activeworkspace"] = done('{"id": 1}')
            ok, text = sysintents.workspace({"n": 3})
        self.assertFalse(ok)
        self.assertIn("didn't move", text)

    def test_bad_workspace_runs_nothing(self):
        fake = self.patched(Fake())
        self.assertFalse(sysintents.workspace({"n": "0"})[0])
        self.assertFalse(sysintents.workspace({"n": "x; reboot"})[0])
        self.assertEqual(fake.calls, [])

    def test_lock_uses_loginctl_or_configured_hyprlock(self):
        fake = self.patched(Fake())
        self.assertTrue(sysintents.lock_screen(config={})[0])
        self.assertEqual(fake.calls, [["loginctl", "lock-session"]])
        popen = mock.Mock()
        with mock.patch("subprocess.Popen", popen):
            sysintents.lock_screen(config={"lock_screen": {"command": "hyprlock"}})
        # hyprlock is not in the faked tool list above: it must fall back to loginctl
        popen.assert_not_called()
        self.patched(Fake(), tools=("hyprlock", "loginctl"))
        with mock.patch("subprocess.Popen", popen):
            self.assertEqual(sysintents.lock_screen(config={"lock_screen": {"command": "hyprlock"}}),
                             (True, "Started hyprlock."))
        self.assertEqual(popen.call_args[0][0], ["hyprlock"])

    def test_lock_failure_is_reported(self):
        self.patched(Fake({"loginctl": done(code=1, stderr="no session")}))
        self.assertEqual(sysintents.lock_screen(config={}), (False, "no session"))


class ReminderTests(Env):
    def test_schedules_an_in_process_timer_and_notifies(self):
        fake = self.patched(Fake())
        timers = mock.Mock()
        ok, text = sysintents.reminder({"minutes": "2", "text": "call mom"}, timer=timers)
        self.assertTrue(ok)
        self.assertIn("(in 2 minutes): call mom", text)
        delay, fire = timers.call_args[0][:2]
        self.assertEqual(delay, 120.0)
        self.assertTrue(timers.return_value.daemon)
        timers.return_value.start.assert_called_once()
        self.assertEqual(fake.calls, [])  # nothing runs until it fires
        fire(*timers.call_args[1]["args"])
        self.assertEqual(fake.calls[0][:5], ["notify-send", "-a", "JARVIS", "-u", "normal"])
        self.assertIn("call mom", fake.calls[0])
        sysintents._TIMERS.clear()

    def test_bad_time_schedules_nothing(self):
        self.patched(Fake())
        timers = mock.Mock()
        for minutes in ("0", "-5", "5000", "soon", "nan", True):
            self.assertFalse(sysintents.reminder({"minutes": minutes}, timer=timers)[0], minutes)
        timers.assert_not_called()


class WeatherTests(unittest.TestCase):
    DATA = {"current_condition": [{"temp_C": "12", "temp_F": "54", "weatherDesc": [{"value": "Partly cloudy"}]}],
            "weather": [{"maxtempC": "15", "mintempC": "8", "hourly": [{"chanceofrain": "10"}, {"chanceofrain": "40"}]},
                        {"maxtempC": "17", "mintempC": "9", "hourly": [{"chanceofrain": "70"}]}]}

    def test_url_and_summary(self):
        seen = []
        fetch = lambda url: seen.append(url) or self.DATA
        ok, text = sysintents.weather({"city": "New York"}, config={}, fetch=fetch)
        self.assertEqual(seen, ["https://wttr.in/New%20York?format=j1"])
        self.assertTrue(ok)
        self.assertIn("partly cloudy, 12 degrees C (54 F)", text)
        self.assertIn("40 percent chance of rain", text)
        ok, text = sysintents.weather({"city": "x", "day": "tomorrow"}, config={}, fetch=fetch)
        self.assertIn("high 17, low 9 degrees C, 70 percent", text)

    def test_default_city_from_config_else_ask(self):
        fetch = lambda url: self.DATA
        ok, text = sysintents.weather({}, config={"weather": {"city": "Paris"}}, fetch=fetch)
        self.assertTrue(ok)
        self.assertIn("Paris", text)
        ok, text = sysintents.weather({}, config={}, fetch=fetch)
        self.assertFalse(ok)
        self.assertIn("Which city", text)

    def test_bad_city_and_network_errors(self):
        self.assertFalse(sysintents.weather({"city": "a/../b?x=1"}, config={}, fetch=lambda u: self.DATA)[0])

        def boom(url):
            raise OSError("down")
        ok, text = sysintents.weather({"city": "Rome"}, config={}, fetch=boom)
        self.assertFalse(ok)
        self.assertIn("Couldn't get the weather", text)
        self.assertFalse(sysintents.weather({"city": "Rome", "day": "friday"}, config={}, fetch=boom)[0])


class WiringTests(unittest.TestCase):
    def test_every_intent_has_an_executor_and_tool(self):
        from jarvis.actions import DesktopTools as Tools
        from jarvis.pipeline import DIRECT
        from jarvis.remote import ACTIONS, ARG
        names = ("brightness", "screenshot", "system_status", "workspace", "lock_screen",
                 "reminder", "weather")
        tools = {t["name"] for t in Tools._system_tools()}
        for name in names:
            self.assertIn(name, DIRECT)
            self.assertIn(name, ACTIONS)
            self.assertEqual(ARG[name], "spec")
            self.assertIn(name, tools)

    def test_run_dispatches_with_a_spec(self):
        with mock.patch.object(sysintents, "brightness", return_value=(True, "b")) as b:
            self.assertEqual(sysintents.run("brightness", '{"level": "up"}'), (True, "b"))
            b.assert_called_once_with('{"level": "up"}')


if __name__ == "__main__":
    unittest.main()
