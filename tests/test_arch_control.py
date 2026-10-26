"""Controlling the other laptop: routing, laptop names, the SSH session, and the
close/media/volume tools (run with fake hyprctl/playerctl/wpctl)."""

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jarvis import actions, remote
from jarvis.actions import DesktopTools, is_action
from jarvis.chatgpt import HANDOFF, Brain
from jarvis.remote import Remote

CONFIG = {"devices": {
    "dell": {"transport": "local", "hostname": "WinLaptop", "aliases": ["windows"]},
    "zenbook": {"transport": "ssh", "target": "jdoe@192.168.0.194", "hostname": "archmain",
                "label": "ASUS Zenbook, Arch Linux", "aliases": ["arch", "arch linux", "asus"]}}}


def done(out="", code=0, err=""):
    return subprocess.CompletedProcess([], code, out, err)


class RoutingTests(unittest.TestCase):
    def test_actions_anywhere_in_what_he_said(self):
        for q in ("Shut up. Claude, open the wiki page on my Arch Linux.",
                  "I want Spotify open on my Arch", "I need you to close Chrome",
                  "turn the volume down", "pause the music", "okay so, next song please",
                  "mute it on the dell"):
            self.assertTrue(is_action(q), q)
        for q in ("You cannot control this, okay I see why.", "how do I install yay on arch",
                  "what is the capital of Peru?", "tell me a joke", "how do I open a jar file"):
            self.assertFalse(is_action(q), q)


class NameTests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote(CONFIG, tempfile.gettempdir())
        self.remote.here = lambda: "dell"

    def test_spoken_names(self):
        for said, key in (("Arch Linux", "zenbook"), ("my arch linux laptop", "zenbook"),
                          ("the ASUS", "zenbook"), ("archmain", "zenbook"),
                          ("the other laptop", "zenbook"), ("Windows", "dell"),
                          ("here", None), (None, None), ("toaster", "toaster")):
            self.assertEqual(self.remote.resolve(said), key, said)

    def test_named_laptop_is_reached_and_unknown_one_refused(self):
        with patch.object(Remote, "_ssh", return_value=(True, "Opened Knowledge.")) as ssh:
            self.assertEqual(self.remote.call("arch linux", "open_page", "wiki"),
                             (True, "Opened Knowledge."))
        self.assertEqual(ssh.call_args.args[0], "zenbook")
        self.assertFalse(self.remote.call("toaster", "open_app", "x")[0])

    def test_tools_name_the_laptops_and_carry_each_argument(self):
        seen = []
        self.remote.call = lambda device, action, arg: seen.append((device, action, arg)) or (True, "")
        tools = DesktopTools(remote=self.remote)
        spec = next(s for s in tools.specs() if s["name"] == "volume")
        self.assertIn("Arch Linux", spec["description"])
        for name, args in (("open_page", {"page": "wiki"}), ("close_app", {"name": "Chrome"}),
                           ("media", {"command": "pause"}), ("volume", {"level": "30"})):
            tools.call(name, {**args, "device": "zenbook"})
        self.assertEqual(seen, [("zenbook", "open_page", "wiki"), ("zenbook", "close_app", "Chrome"),
                                ("zenbook", "media", "pause"), ("zenbook", "volume", "30")])


class SessionTests(unittest.TestCase):
    def test_ssh_shell_borrows_the_desktop_display(self):
        shown = "DISPLAY=:1\nWAYLAND_DISPLAY=wayland-1\nXDG_SESSION_TYPE=wayland\nLS_COLORS=$'a b'\n"
        with patch.dict(remote.os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000",
                                            "XDG_SESSION_TYPE": "tty"}, clear=True), \
                patch.object(remote.subprocess, "run", return_value=done(shown)):
            env = remote.session_env()
        self.assertEqual((env["DISPLAY"], env["WAYLAND_DISPLAY"], env["XDG_SESSION_TYPE"]),
                         (":1", "wayland-1", "wayland"))
        self.assertNotIn("LS_COLORS", env)


@patch.object(actions.os, "name", "posix")
@patch.object(actions.shutil, "which", return_value="/usr/bin/x")
class LinuxControlTests(unittest.TestCase):
    CLIENTS = json.dumps([
        {"address": "0x55aa", "class": "google-chrome", "initialClass": "google-chrome", "title": "YouTube"},
        {"address": "0x55bb", "class": "app.zen_browser.zen", "initialClass": "", "title": "JARVIS Wiki — Zen"},
        {"address": "0x55cc", "class": "app.zen_browser.zen", "initialClass": "", "title": "Pinterest — Zen"},
        {"address": "0x55dd", "class": "chrome-127.0.0.1__console.html", "initialClass": "", "title": "JARVIS Console"}])

    def run_with(self, calls, replies):
        def run(*args):
            calls.append(args)
            return replies(args)
        return patch.object(actions, "_run", side_effect=run)

    def test_close_app_by_class_with_the_lua_dispatcher(self, _which):
        calls = []
        replies = lambda a: done(self.CLIENTS) if a[1] == "clients" else done("ok")
        with self.run_with(calls, replies):
            self.assertEqual(actions.close_app("Google Chrome"), (True, "Closed 1 Google Chrome window."))
        closes = [a[2] for a in calls if a[1] == "dispatch"]  # not JARVIS's own app window
        self.assertEqual(closes, ['hl.dsp.window.close({ window = "address:0x55aa" })'])
        calls.clear()
        with self.run_with(calls, replies):
            self.assertEqual(actions.close_app("zen"), (True, "Closed 2 zen windows."))

    def test_close_by_title_only_when_one_window_matches(self, _which):
        calls = []
        replies = lambda a: done(self.CLIENTS) if a[1] == "clients" else done("ok")
        with self.run_with(calls, replies):
            ok, text = actions.close_app("JARVIS")
        self.assertFalse(ok)
        self.assertIn("Several windows match", text)
        self.assertFalse([a for a in calls if a[1] == "dispatch"])
        with self.run_with(calls, replies):
            self.assertTrue(actions.close_app("pinterest")[0])

    def test_close_falls_back_to_the_classic_dispatcher(self, _which):
        calls = []
        replies = lambda a: (done(self.CLIENTS) if a[1] == "clients" else
                             done("ok") if a[2] == "closewindow" else done("invalid dispatcher"))
        with self.run_with(calls, replies):
            self.assertTrue(actions.close_app("youtube")[0])
        self.assertEqual(calls[-1], ("hyprctl", "dispatch", "closewindow", "address:0x55aa"))

    def test_media_and_volume(self, _which):
        calls = []
        replies = lambda a: (done("Volume: 0.30") if a[1] == "get-volume" else
                             done(code=1, err="No players found") if a[0] == "playerctl" else done())
        with self.run_with(calls, replies):
            self.assertEqual(actions.media("pause"), (False, "No players found"))
            self.assertEqual(actions.volume("30%"), (True, "Volume is 30 percent."))
            self.assertTrue(actions.volume("up")[0])
            self.assertFalse(actions.volume("eleven")[0])
            self.assertFalse(actions.media("rm -rf")[0])
        self.assertIn(("playerctl", "pause"), calls)
        self.assertIn(("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "30%"), calls)
        self.assertIn(("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"), calls)


class HandoffTests(unittest.TestCase):
    class Lane:
        def __init__(self, reply):
            self.reply, self.asked = reply, []

        def ask(self, text, recall=(), on_sentence=None):
            self.asked.append(text)
            if on_sentence:
                on_sentence(self.reply)
            return self.reply

    def test_talk_hands_a_missed_action_to_the_tools(self):
        chat, act = self.Lane(HANDOFF + "."), self.Lane("Opened Knowledge on the Arch.")
        spoken = []
        reply = Brain(chat, act, wants=lambda t: False).ask("yo the wiki page on arch",
                                                            on_sentence=spoken.append)
        self.assertEqual(reply, "Opened Knowledge on the Arch.")
        self.assertEqual(spoken, ["Opened Knowledge on the Arch."])  # the token is never spoken

    def test_plain_talk_stays_talk(self):
        chat, act = self.Lane("Lima."), self.Lane("x")
        self.assertEqual(Brain(chat, act, wants=lambda t: False).ask("capital of Peru?"), "Lima.")
        self.assertEqual(act.asked, [])


if __name__ == "__main__":
    unittest.main()
