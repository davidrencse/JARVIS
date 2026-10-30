"""Desktop backends beyond Windows and Hyprland: sway, X11 (wmctrl), macOS, pactl, nowplaying-cli.

Nothing real runs: platforms.detect, shutil.which and the _run helpers are patched, and
os.name is patched where the code under test branches on it."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import actions, observe, platforms
from jarvis import sharedinput as si


def done(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def detect(desktop, os_name=None):
    os_name = os_name or {"macos": "macos", "windows": "windows"}.get(desktop, "linux")
    return mock.patch.object(platforms, "detect", return_value={
        "os": os_name, "name": "x", "version": "1", "desktop": desktop, "label": "x"})


def which(*present):
    return mock.patch("shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}" if n in present else None)


def posix():
    return mock.patch.object(actions.os, "name", "posix")


class Fake:
    """A _run replacement: handler(args) -> result; every call is recorded."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        return self.handler(list(args))


SWAY_TREE = {"type": "root", "nodes": [
    {"type": "output", "nodes": [{"type": "workspace", "nodes": [
        {"id": 11, "type": "con", "app_id": "firefox", "name": "Docs - Firefox", "focused": True, "nodes": []},
        {"id": 12, "type": "con", "app_id": None, "window_properties": {"class": "Steam"},
         "name": "Steam", "focused": False, "nodes": []}],
        "floating_nodes": [{"id": 13, "type": "floating_con", "app_id": "pavucontrol",
                            "name": "Volume", "focused": False, "nodes": []}]}]}]}

WMCTRL = ("0x04000007  0 Navigator.firefox  box Docs - Firefox\n"
          "0x05200003  1 xterm.XTerm         box ~ : bash\n"
          "garbage line\n")


class PlatformsLabelTests(unittest.TestCase):
    def test_machine_comes_from_platforms(self):
        self.assertEqual(actions.MACHINE, platforms.label())
        self.assertNotIn("Arch Linux laptop", actions.INSTRUCTIONS)
        self.assertIn(actions.MACHINE, actions.INSTRUCTIONS)


class ObserveListingTests(unittest.TestCase):
    def listing(self, desktop, tools, handler):
        fake = Fake(handler)
        with posix(), detect(desktop), which(*tools), mock.patch.object(observe, "_run", fake):
            return observe.windows(), fake

    def test_sway_tree(self):
        got, fake = self.listing("sway", ["swaymsg"], lambda a: done(json.dumps(SWAY_TREE)))
        self.assertEqual(fake.calls, [["swaymsg", "-t", "get_tree"]])
        self.assertEqual(got, [
            {"app": "firefox", "title": "Docs - Firefox", "focused": True},
            {"app": "Steam", "title": "Steam", "focused": False},
            {"app": "pavucontrol", "title": "Volume", "focused": False}])

    def test_sway_bad_json_is_empty(self):
        got, _ = self.listing("sway", ["swaymsg"], lambda a: done("not json"))
        self.assertEqual(got, [])

    def test_wmctrl_with_xdotool_focus(self):
        got, fake = self.listing("x11", ["wmctrl", "xdotool"],
                                 lambda a: done("67108871\n") if a[0] == "xdotool" else done(WMCTRL))
        self.assertEqual(got, [
            {"app": "Navigator firefox", "title": "Docs - Firefox", "focused": True},
            {"app": "xterm XTerm", "title": "~ : bash", "focused": False}])

    def test_wmctrl_without_xdotool_focus_unknown(self):
        got, fake = self.listing("kde", ["wmctrl"], lambda a: done(WMCTRL))
        self.assertEqual([w["focused"] for w in got], [False, False])
        self.assertEqual(fake.calls, [["wmctrl", "-lx"]])

    def test_gnome_without_wmctrl_raises_with_advice(self):
        with posix(), detect("gnome"), which(), self.assertRaises(LookupError) as ctx:
            observe.windows()
        self.assertIn("wmctrl", str(ctx.exception))

    def test_plain_wayland_and_none_raise(self):
        for desktop in ("wayland", "none"):
            with self.subTest(desktop), posix(), detect(desktop), which("wmctrl"):
                with self.assertRaises(LookupError) as ctx:
                    observe.windows()
                self.assertIn("wmctrl (X11) or a supported compositor", str(ctx.exception))

    def test_macos_parse(self):
        got, fake = self.listing("macos", ["osascript"],
                                 lambda a: done("Finder, Safari, Notes, false, true, false\n"))
        self.assertEqual(fake.calls[0][:2], ["osascript", "-e"])
        self.assertIn("System Events", fake.calls[0][2])
        self.assertEqual([(w["app"], w["focused"]) for w in got],
                         [("Finder", False), ("Safari", True), ("Notes", False)])

    def test_macos_parse_defensive(self):
        self.assertEqual(observe.parse_macos_processes(""), [])
        self.assertEqual(observe.parse_macos_processes("{}"), [])
        odd = observe.parse_macos_processes("Finder, Safari, true")  # unreadable pairing
        self.assertEqual([w["focused"] for w in odd], [False, False])
        braces = observe.parse_macos_processes('{"Finder", "Mail", true, false}')
        self.assertEqual([(w["app"], w["focused"]) for w in braces], [("Finder", True), ("Mail", False)])

    def test_macos_osascript_failure(self):
        with posix(), detect("macos"), which("osascript"), \
                mock.patch.object(observe, "_run", Fake(lambda a: done(returncode=1, stderr="not allowed"))):
            with self.assertRaises(LookupError):
                observe.windows()


class ObserveMediaVolumeTests(unittest.TestCase):
    def test_macos_playback_rates(self):
        for rate, status in (("1", "Playing"), ("0", "Paused"), ("1.0", "Playing")):
            with self.subTest(rate), posix(), detect("macos"), which("nowplaying-cli"), \
                    mock.patch.object(observe, "_run", Fake(lambda a, r=rate: done(r + "\n"))) as fake:
                self.assertEqual(observe.playback(), {"status": status})
                self.assertEqual(fake.calls, [["nowplaying-cli", "get", "playbackRate"]])

    def test_macos_playback_null_and_missing_tool(self):
        with posix(), detect("macos"), which("nowplaying-cli"), \
                mock.patch.object(observe, "_run", Fake(lambda a: done("(null)\n"))):
            self.assertIsNone(observe.playback())
        with posix(), detect("macos"), which(), self.assertRaises(LookupError):
            observe.playback()

    def test_pactl_volume_state(self):
        def handler(a):
            if a[1] == "get-sink-volume":
                return done("Volume: front-left: 42000 /  64% / -11.00 dB,   front-right: 42000 /  64% / -11.00 dB\n")
            return done("Mute: yes\n")
        with posix(), detect("gnome"), which("pactl"), mock.patch.object(observe, "_run", Fake(handler)):
            self.assertEqual(observe.volume_state(), {"percent": 64, "muted": True})

    def test_wpctl_still_preferred(self):
        out = done("Volume: 0.50\n")
        with posix(), detect("hyprland"), which("wpctl", "pactl"), \
                mock.patch.object(observe.subprocess, "run", return_value=out) as run:
            self.assertEqual(observe.volume_state(), {"percent": 50, "muted": False})
        self.assertEqual(run.call_args[0][0][0], "wpctl")

    def test_no_audio_tool(self):
        with posix(), detect("x11"), which(), self.assertRaises(LookupError):
            observe.volume_state()

    def test_macos_volume_state(self):
        fake = Fake(lambda a: done("35\n") if "volume of" in a[2] else done("true\n"))
        with posix(), detect("macos"), mock.patch.object(observe, "_run", fake):
            self.assertEqual(observe.volume_state(), {"percent": 35, "muted": True})
        self.assertEqual(fake.calls[0], ["osascript", "-e", "output volume of (get volume settings)"])
        self.assertEqual(fake.calls[1], ["osascript", "-e", "output muted of (get volume settings)"])


class ActionsWindowTests(unittest.TestCase):
    def test_sway_focus_and_close_use_con_id(self):
        fake = Fake(lambda a: done(json.dumps(SWAY_TREE)) if a[:2] == ["swaymsg", "-t"] else done())
        with posix(), detect("sway"), which("swaymsg"), \
                mock.patch.object(actions, "_run", fake), mock.patch.object(observe, "_run", fake):
            self.assertEqual(actions.focus_window("firefox"), (True, "Switched to Docs - Firefox."))
            self.assertEqual(actions.close_app("pavucontrol"), (True, "Closed 1 pavucontrol window."))
        self.assertIn(["swaymsg", "[con_id=11]", "focus"], fake.calls)
        self.assertIn(["swaymsg", "[con_id=13]", "kill"], fake.calls)

    def test_hyprland_focus_tries_lua_then_classic(self):
        clients = json.dumps([{"address": "0xabc", "class": "kitty", "initialClass": "kitty",
                               "title": "shell"}])
        for lua_ok in (True, False):  # 0.56+ takes Lua only; older Hyprland only classic
            def handler(a, lua_ok=lua_ok):
                if a[:3] == ["hyprctl", "clients", "-j"]:
                    return done(clients)
                if a[:2] == ["hyprctl", "dispatch"]:
                    return done("ok" if a[2].startswith("hl.dsp.") == lua_ok else "error: bad syntax")
                return done()
            fake = Fake(handler)
            with posix(), detect("hyprland"), which("hyprctl"), mock.patch.object(actions, "_run", fake):
                self.assertEqual(actions.focus_window("kitty"), (True, "Switched to shell."))
            self.assertEqual(fake.calls[1], ["hyprctl", "dispatch",
                                             'hl.dsp.focus({ window = "address:0xabc" })'])
            self.assertEqual(len(fake.calls), 2 if lua_ok else 3)

    def test_hyprland_console_app_window_is_not_chrome(self):
        clients = json.dumps([{"address": "0xc0", "class": "chrome-127.0.0.1__-Default",
                               "initialClass": "chrome-127.0.0.1__-Default", "title": "JARVIS"}])
        fake = Fake(lambda a: done(clients) if a[:3] == ["hyprctl", "clients", "-j"] else done("ok"))
        with posix(), detect("hyprland"), which("hyprctl"), mock.patch.object(actions, "_run", fake):
            self.assertFalse(actions.close_app("chrome")[0])
        self.assertFalse(any(a[:2] == ["hyprctl", "dispatch"] for a in fake.calls))
        self.assertFalse(observe.matches({"kind": "window", "app": "chrome"},
                                         [{"app": "localconsole localconsole", "title": "JARVIS",
                                           "focused": True}])[0])

    def test_wmctrl_focus_and_close_use_window_id(self):
        fake = Fake(lambda a: done(WMCTRL) if a[:2] == ["wmctrl", "-lx"] else done())
        with posix(), detect("x11"), which("wmctrl"), \
                mock.patch.object(actions, "_run", fake), mock.patch.object(observe, "_run", fake):
            self.assertTrue(actions.focus_window("xterm")[0])
            self.assertTrue(actions.close_app("xterm")[0])
        self.assertIn(["wmctrl", "-i", "-a", "0x05200003"], fake.calls)
        self.assertIn(["wmctrl", "-i", "-c", "0x05200003"], fake.calls)

    def test_unsupported_wayland_raises_lookup_error(self):
        with posix(), detect("wayland"), which(), self.assertRaises(LookupError) as ctx:
            actions.open_windows()
        self.assertIn("window control needs wmctrl (X11) or a supported compositor", str(ctx.exception))

    def test_unsupported_wayland_reported_through_the_tool(self):
        with posix(), detect("wayland"), which():
            ok, text = actions.DesktopTools().call("close_app", {"name": "firefox"})
        self.assertFalse(ok)
        self.assertIn("LookupError", text)

    def test_macos_focus_and_close_scripts(self):
        fake = Fake(lambda a: done("Finder, Safari, false, true\n") if "System Events" in a[2] else done())
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake), \
                mock.patch.object(observe, "_run", fake):
            self.assertTrue(actions.focus_window("safari")[0])
            self.assertTrue(actions.close_app("safari")[0])
        self.assertIn(["osascript", "-e", 'tell application "Safari" to activate'], fake.calls)
        self.assertIn(["osascript", "-e", 'tell application "Safari" to quit'], fake.calls)

    def test_macos_app_name_validation(self):
        evil = 'Safari"; do shell script "x'
        self.assertIsNone(actions.macos_app_name(evil))
        for bad in ('a"b', "a\\b", "a;b", "a\nb", "", "   ", "$(x)", "a`b`", "x" * 101):
            self.assertIsNone(actions.macos_app_name(bad), bad)
        for good in ("Safari", "Google Chrome", "Visual Studio Code", "1Password 7", "my-app_2.0"):
            self.assertEqual(actions.macos_app_name(good), good)

    def test_macos_rejects_injection_in_close_and_focus(self):
        evil = 'Safari"; do shell script "x'
        fake = Fake(lambda a: done())
        window = {"id": evil, "app": evil, "title": evil}
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake):
            self.assertFalse(actions._close(window))
        self.assertEqual(fake.calls, [])
        with posix(), detect("macos"), mock.patch.object(actions, "open_windows", return_value=[window]), \
                mock.patch.object(actions, "_run", fake):
            self.assertFalse(actions.focus_window("safari")[0])
        self.assertEqual(fake.calls, [])


class ActionsMacosLaunchTests(unittest.TestCase):
    def tools(self):
        tools = actions.DesktopTools()
        tools._apps, tools._loaded = [{"Name": n, "AppID": n, "Path": f"/Applications/{n}.app"}
                                      for n in ("Google Chrome", "Safari", "Spotify")], 1e18
        return tools

    def test_open_app(self):
        fake = Fake(lambda a: done())
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(self.tools().open_app("chrome"), (True, "Opened Google Chrome."))
        self.assertEqual(fake.calls, [["open", "-a", "Google Chrome"]])

    def test_open_app_failure(self):
        with posix(), detect("macos"), mock.patch.object(actions, "_run",
                                                         Fake(lambda a: done(returncode=1, stderr="nope"))):
            self.assertEqual(self.tools().open_app("safari"), (False, "nope"))

    def test_open_url_default_and_named_browser(self):
        fake = Fake(lambda a: done())
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake), \
                mock.patch("jarvis.vault.open_link") as default:
            self.assertTrue(self.tools().open_url("https://example.com/a", "Google Chrome")[0])
            self.tools().open_url("https://example.com")
        self.assertEqual(fake.calls, [["open", "-a", "Google Chrome", "https://example.com/a"]])
        default.assert_called_once_with("https://example.com")

    def test_spotify_deep_link(self):
        fake = Fake(lambda a: done())
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(self.tools().open_url("spotify:track:4uLU6hMCjMI75M1A2tKUQC"),
                             (True, "Opened Spotify."))
            self.assertFalse(self.tools().open_url("spotify:track:x; rm -rf /")[0])
        self.assertEqual(fake.calls, [["open", "spotify:track:4uLU6hMCjMI75M1A2tKUQC"]])

    def test_macos_apps_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("Applications/Notes.app", "Applications/Zed.app", "home/Applications/Mine.app",
                        "Applications/readme.txt"):
                (root / rel).mkdir(parents=True) if rel.endswith(".app") else \
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)

            class FakePath:
                def __new__(cls, text):
                    return root / str(text).lstrip("/")

                @staticmethod
                def home():
                    return root / "home"

            with mock.patch.object(actions, "Path", FakePath):
                names = [a["Name"] for a in actions.macos_apps()]
        self.assertEqual(names, ["Notes", "Zed", "Mine"])


class ActionsMediaVolumeTests(unittest.TestCase):
    def test_macos_media_with_nowplaying(self):
        for command, verb in (("play_pause", "togglePlayPause"), ("play", "play"), ("pause", "pause"),
                              ("next", "next"), ("previous", "previous"), ("stop", "pause")):
            fake = Fake(lambda a: done())
            with self.subTest(command), posix(), detect("macos"), which("nowplaying-cli"), \
                    mock.patch.object(actions, "_run", fake):
                self.assertTrue(actions.media(command)[0])
            # play and pause read the rate first (unreadable here, so they go ahead).
            self.assertEqual(fake.calls[-1], ["nowplaying-cli", verb])

    def test_macos_already_paused_presses_nothing(self):
        fake = Fake(lambda a: done("0\n"))
        with posix(), detect("macos"), which("nowplaying-cli"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(actions.media("pause"), (True, "Already paused."))
        self.assertEqual(fake.calls, [["nowplaying-cli", "get", "playbackRate"]])

    def test_macos_media_without_nowplaying(self):
        with posix(), detect("macos"), which():
            self.assertEqual(actions.media("next"),
                             (False, "Media keys on macOS need nowplaying-cli (brew install nowplaying-cli)."))

    def test_linux_media_still_playerctl(self):
        fake = Fake(lambda a: done())
        with posix(), detect("gnome"), which("playerctl"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(actions.media("play_pause"), (True, "Toggled play/pause."))
        self.assertEqual(fake.calls, [["playerctl", "-p", "spotify,%any", "play-pause"]])

    def test_linux_play_pause_read_the_pinned_player_first(self):
        def handler(status):
            return lambda a: done(status + "\n") if a[-1] == "status" else done()
        cases = (("pause", "Paused", (True, "Already paused."), False),
                 ("pause", "Stopped", (True, "Already paused."), False),
                 ("play", "Playing", (True, "Already playing."), False),
                 ("pause", "Playing", (True, "Paused."), True),
                 ("play", "Paused", (True, "Playing."), True))
        for command, status, said, pressed in cases:
            fake = Fake(handler(status))
            with self.subTest(command=command, status=status), posix(), detect("gnome"), \
                    which("playerctl"), mock.patch.object(actions, "_run", fake):
                self.assertEqual(actions.media(command), said)
            self.assertEqual(fake.calls[0], ["playerctl", "-p", "spotify,%any", "status"])
            self.assertEqual(["playerctl", "-p", "spotify,%any", command] in fake.calls, pressed)

    def test_linux_unreadable_state_lets_the_player_decide(self):
        fake = Fake(lambda a: done(returncode=1, stderr="No players found") if a[-1] == "status" else done())
        with posix(), detect("gnome"), which("playerctl"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(actions.media("play"), (True, "Playing."))
        self.assertIn(["playerctl", "-p", "spotify,%any", "play"], fake.calls)

    def test_pactl_volume_levels(self):
        def handler(a):
            if a[0] != "pactl":
                return done()
            if a[1] == "get-sink-volume":
                return done("Volume: front-left: 1 /  55% / -1 dB\n")
            if a[1] == "get-sink-mute":
                return done("Mute: no\n")
            return done()
        cases = {"up": [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
                        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"]],
                 "down": [["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"]],
                 "mute": [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"]],
                 "unmute": [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"]],
                 "30": [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
                        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "30%"]]}
        for level, expected in cases.items():
            fake = Fake(handler)
            with self.subTest(level), posix(), detect("x11"), which("pactl"), \
                    mock.patch.object(actions, "_run", fake), mock.patch.object(observe, "_run", fake):
                self.assertEqual(actions.volume(level), (True, "Volume is 55 percent."))
            self.assertEqual(fake.calls[:len(expected)], expected)

    def test_pactl_clamps_above_100(self):
        def handler(a):
            if a[1] == "get-sink-volume":
                return done("Volume: front-left: 1 / 110% / 2 dB\n")
            return done("Mute: no\n") if a[1] == "get-sink-mute" else done()
        fake = Fake(handler)
        with posix(), detect("x11"), which("pactl"), \
                mock.patch.object(actions, "_run", fake), mock.patch.object(observe, "_run", fake):
            self.assertEqual(actions.volume("up"), (True, "Volume is 100 percent."))
        self.assertIn(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"], fake.calls)

    def test_no_volume_tool(self):
        with posix(), detect("x11"), which():
            ok, text = actions.volume("up")
        self.assertFalse(ok)
        self.assertIn("wpctl", text)
        self.assertIn("pactl", text)

    def test_macos_volume_set_and_mute(self):
        state = {"muted": "false"}

        def handler(a):
            script = a[2]
            if script.startswith("output volume of"):
                return done("40\n")
            if script.startswith("output muted of"):
                return done(state["muted"] + "\n")
            if script == "set volume with output muted":
                state["muted"] = "true"
            return done()
        fake = Fake(handler)
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake), \
                mock.patch.object(observe, "_run", fake):
            self.assertEqual(actions.volume("mute"), (True, "Volume is 40 percent, muted."))
            self.assertEqual(actions.volume("unmute"), (True, "Volume is 40 percent, muted."))  # fake keeps state
            self.assertTrue(actions.volume("75")[0])
            self.assertTrue(actions.volume("up")[0])
            self.assertTrue(actions.volume("down")[0])
        scripts = [c[2] for c in fake.calls if c[2].startswith("set volume")]
        self.assertEqual(scripts, ["set volume with output muted", "set volume without output muted",
                                   "set volume output volume 75", "set volume without output muted",
                                   "set volume output volume 50", "set volume without output muted",
                                   "set volume output volume 30"])

    def test_macos_volume_clamps(self):
        fake = Fake(lambda a: done("95\n") if a[2].startswith("output volume") else done("false\n"))
        with posix(), detect("macos"), mock.patch.object(actions, "_run", fake), \
                mock.patch.object(observe, "_run", fake):
            actions.volume("up")
        self.assertIn(["osascript", "-e", "set volume output volume 100"], fake.calls)

    def test_wpctl_path_unchanged(self):
        fake = Fake(lambda a: done("Volume: 0.30\n"))
        with posix(), detect("hyprland"), which("wpctl", "pactl"), mock.patch.object(actions, "_run", fake):
            self.assertEqual(actions.volume("30"), (True, "Volume is 30 percent."))
        self.assertEqual(fake.calls[0][0], "wpctl")


class SharedInputBackendTests(unittest.TestCase):
    def test_sway_outputs(self):
        outputs = [{"name": "eDP-1", "make": "BOE", "model": "0x0", "active": True, "focused": True,
                    "scale": 2.0, "current_mode": {"width": 2880, "height": 1800},
                    "rect": {"x": 0, "y": 0, "width": 1440, "height": 900}},
                   {"name": "HDMI-A-1", "active": False}]
        with detect("sway"), which("swaymsg"), mock.patch.object(si, "_run", Fake(lambda a: done(json.dumps(outputs)))):
            got = si.monitors()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["name"], got[0]["width"], got[0]["height"], got[0]["primary"], got[0]["scale"]),
                         ("eDP-1", 2880, 1800, True, 2.0))

    def test_xrandr_listmonitors(self):
        text = ("Monitors: 2\n 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                " 1: +HDMI-1 2560x1440+1920+-100  HDMI-1\n")
        fake = Fake(lambda a: done(text))
        with detect("x11"), which("xrandr"), mock.patch.object(si, "_run", fake):
            got = si.monitors()
        self.assertEqual(fake.calls, [["xrandr", "--listmonitors"]])
        self.assertEqual([(m["name"], m["width"], m["height"], m["x"], m["y"], m["primary"]) for m in got],
                         [("eDP-1", 1920, 1080, 0, 0, True), ("HDMI-1", 2560, 1440, 1920, -100, False)])

    def test_system_profiler(self):
        data = {"SPDisplaysDataType": [{"spdisplays_ndrvs": [
            {"_name": "Color LCD", "_spdisplays_resolution": "1512 x 982 @ 120.00Hz",
             "spdisplays_main": "spdisplays_yes"},
            {"_name": "DELL", "_spdisplays_pixels": "2560 x 1440"}, {"_name": "odd"}]}]}
        fake = Fake(lambda a: done(json.dumps(data)))
        with detect("macos"), which("system_profiler"), mock.patch.object(si, "_run", fake):
            got = si.monitors()
        self.assertEqual(fake.calls[0][:3], ["system_profiler", "SPDisplaysDataType", "-json"])
        self.assertEqual([(m["name"], m["width"], m["height"], m["primary"]) for m in got],
                         [("Color LCD", 1512, 982, True), ("DELL", 2560, 1440, False)])

    def test_other_desktops_have_no_monitors(self):
        for desktop in ("wayland", "none"):
            with self.subTest(desktop), detect(desktop), mock.patch.object(si.os, "name", "posix"):
                self.assertEqual(si.monitors(), [])

    def test_macos_lan_mouse_status(self):
        with detect("macos"), which("pgrep"), mock.patch.object(si, "_run", Fake(lambda a: done("123\n"))) as fake:
            self.assertEqual(si.lan_mouse_status(), {"running": True, "build": "official"})
        self.assertEqual(fake.calls, [["pgrep", "-x", "lan-mouse"]])
        with detect("macos"), which("pgrep"), mock.patch.object(si, "_run", Fake(lambda a: done(returncode=1))):
            self.assertEqual(si.lan_mouse_status(), {"running": False, "build": None})

    def test_macos_config_path(self):
        with detect("macos"):
            self.assertEqual(si.config_path().parts[-4:],
                             ("Library", "Application Support", "lan-mouse", "config.toml"))

    def test_macos_addresses(self):
        text = ("lo0: flags=8049<UP>\n\tinet 127.0.0.1 netmask 0xff000000\n"
                "en0: flags=8863<UP>\n\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n"
                "utun3:\n\tinet 100.64.0.7 --> 100.64.0.7 netmask 0xffffffff\n\tinet6 fe80::1 prefixlen 64\n")
        with detect("macos"), which("ifconfig"), mock.patch.object(si, "_run", Fake(lambda a: done(text))):
            self.assertEqual(si.addresses(), ["100.64.0.7", "192.168.1.20"])


if __name__ == "__main__":
    unittest.main()
