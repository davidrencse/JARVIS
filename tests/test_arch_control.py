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
            ok, text = actions.close_app("pinterest")  # a lone browser tab title: not an app
        self.assertFalse(ok)
        self.assertIn("isn't running", text)

    def test_close_falls_back_to_the_classic_dispatcher(self, _which):
        calls = []
        replies = lambda a: (done(self.CLIENTS) if a[1] == "clients" else
                             done("ok") if a[2] == "closewindow" else done("invalid dispatcher"))
        with self.run_with(calls, replies):
            self.assertTrue(actions.close_app("google chrome")[0])
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
        self.assertIn(("playerctl", "-p", "spotify,%any", "pause"), calls)
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

    def test_talk_excuse_goes_to_the_tools_unspoken(self):
        # Seen 2026-10-30: the talk lane answered instead of handing off.
        for excuse in ("I can’t inspect your computers right now, so I don’t know.",
                       "I don't have access to your Dell.",
                       "I tried, but the tool service is disabled right now."):
            chat, act = self.Lane(excuse), self.Lane("Your Dell lists 283 apps.")
            spoken = []
            reply = Brain(chat, act, wants=lambda t: False).ask("count the dell's apps",
                                                                on_sentence=spoken.append)
            self.assertEqual(reply, "Your Dell lists 283 apps.")
            self.assertEqual(spoken, ["Your Dell lists 283 apps."])

    def test_answers_about_the_world_are_not_excuses(self):
        for answer in ("You can't see the aurora from London.", "Most people cannot run a marathon.",
                       "You don't have access to that feature on the free plan.",
                       "Say 'Jarvis, yes' or press Confirm (C3).",
                       "I can't see why that would fail.", "I can't read minds, but here goes.",
                       "I don't have a way to know how you feel.",
                       "I cannot list every prime, but here are ten."):
            chat, act = self.Lane(answer), self.Lane("x")
            self.assertEqual(Brain(chat, act, wants=lambda t: False).ask("q"), answer)
            self.assertEqual(act.asked, [])

    def test_usage_limit_turn_reports_no_earlier_tools(self):
        from unittest.mock import patch
        from jarvis import chatgpt
        lane = chatgpt.ChatGPTLane({}, ".", tools=object())
        lane.tool_log = [("open_app", True, "Opened Spotify.")]  # the previous turn's
        brain = Brain(self.Lane("x"), lane, wants=lambda t: True)
        with patch.object(chatgpt, "usage_limit", return_value="try again at 8:00 PM"):
            with self.assertRaises(RuntimeError):
                brain.ask("open chrome")
        self.assertEqual(brain.tool_log, [])

    def test_tool_less_lanes_keep_code_mode_off(self):
        from jarvis.chatgpt import ChatGPTLane
        talk = ChatGPTLane({}, ".")
        act = ChatGPTLane({}, ".", tools=object())
        self.assertIn("features.code_mode_host=false", talk.overrides)
        self.assertNotIn("features.code_mode_host=false", act.overrides)

    def test_the_tools_lane_excuse_stands(self):
        chat, act = self.Lane("x"), self.Lane("I can't open that page: it needs a login.")
        reply = Brain(chat, act, wants=lambda t: True).ask("open my bank")
        self.assertEqual(reply, "I can't open that page: it needs a login.")
        self.assertEqual(chat.asked, [])

    def test_talk_deferrals_go_to_the_tools(self):
        # Seen 2026-10-29/30: the talk lane offered to check, or asked him to repeat it.
        for excuse in ("Say “Jarvis, how many apps are on my Dell?” so I can check.",
                       "I don’t have a list of apps installed on your Dell. I can check it if you want.",
                       "I can’t inspect the Dell right now because the Windows-check tool is unavailable."):
            chat, act = self.Lane(excuse), self.Lane("Your Dell lists 283 apps.")
            reply = Brain(chat, act, wants=lambda t: False).ask("how many apps are on my dell")
            self.assertEqual(reply, "Your Dell lists 283 apps.", excuse)

    class ToolLane(Lane):
        """A tools lane whose turns answer in order, each with its own tool log."""
        def __init__(self, turns):
            self.turns, self.asked, self.tool_log = list(turns), [], []

        def ask(self, text, recall=(), on_sentence=None):
            self.asked.append(text)
            reply, self.tool_log = self.turns.pop(0)
            if on_sentence:
                for sentence in reply.split(". "):
                    on_sentence(sentence)
            return reply

    def test_tools_lane_excuse_without_a_call_is_retried_unspoken(self):
        # Seen 2026-10-30: "the tool service is disabled" with no tool called.
        act = self.ToolLane([("I tried checking your Dell, but the tool service is disabled right now.", []),
                             ("Your Dell lists 283 apps.", [("list_apps", True, "283 apps")])])
        spoken = []
        reply = Brain(self.Lane("x"), act, wants=lambda t: True).ask(
            "how many apps are on my dell", on_sentence=spoken.append)
        self.assertEqual(reply, "Your Dell lists 283 apps.")
        self.assertEqual(spoken, ["Your Dell lists 283 apps."])
        self.assertEqual(len(act.asked), 2)
        self.assertIn("did not call any", act.asked[1])
        self.assertTrue(act.asked[1].endswith("how many apps are on my dell"))

    def test_tools_blamed_for_a_real_failure_names_the_error(self):
        act = self.ToolLane([("The tool is unavailable right now.", [("list_apps", False, "Node dell is offline")]),
                             ("Your Dell is offline, so I couldn't count its apps.", [])])
        reply = Brain(self.Lane("x"), act, wants=lambda t: True).ask("how many apps are on my dell")
        self.assertEqual(reply, "Your Dell is offline, so I couldn't count its apps.")
        self.assertIn("list_apps: Node dell is offline", act.asked[1])
        self.assertEqual(act.tool_log[0], ("list_apps", False, "Node dell is offline"))  # both turns kept

    def test_tools_lane_naming_a_real_limit_stands_and_is_spoken(self):
        act = self.ToolLane([("I can't open that app on the Dell, it isn't installed.",
                              [("list_apps", True, "no match")])])
        spoken = []
        reply = Brain(self.Lane("x"), act, wants=lambda t: True).ask("open photoshop on the dell",
                                                                    on_sentence=spoken.append)
        self.assertEqual(reply, "I can't open that app on the Dell, it isn't installed.")
        self.assertEqual(spoken, ["I can't open that app on the Dell, it isn't installed."])
        self.assertEqual(len(act.asked), 1)

    def test_unused_tools_are_pointed_out_once(self):
        act = self.ToolLane([("Opened Spotify. I can't control playback with the tools available here.",
                              [("open_app", True, "Opened Spotify.")]),
                             ("Playing now.", [("media", True, "ok")])])
        reply = Brain(self.Lane("x"), act, wants=lambda t: True).ask("open spotify and play something")
        self.assertEqual(reply, "Playing now.")
        self.assertIn("whole tool list", act.asked[1])

    def test_first_turn_tools_survive_a_failed_retry(self):
        # Audit 2026-11-01: the re-ask cleared the log, so a usage limit on it hid open_app
        # and the fallback could open the app twice.
        class Failing(self.ToolLane):
            def ask(inner, text, recall=(), on_sentence=None):
                if inner.asked:
                    inner.tool_log = []
                    raise RuntimeError("ChatGPT usage limit: later")
                return super().ask(text, recall, on_sentence)
        act = Failing([("Opened Spotify. My tools are unavailable right now.",
                        [("open_app", True, "Opened Spotify.")])])
        brain = Brain(self.Lane("x"), act, wants=lambda t: True)
        with self.assertRaises(RuntimeError):
            brain.ask("open spotify and set volume 50")
        self.assertEqual(brain.tool_log, [("open_app", True, "Opened Spotify.")])

    def test_what_was_already_said_is_not_repeated(self):
        act = self.ToolLane([("I opened Spotify. I can't set the volume with the tools available here.",
                              [("open_app", True, "ok")]),
                             ("Volume is 50.", [("volume", True, "ok")])])
        spoken = []
        Brain(self.Lane("x"), act, wants=lambda t: True).ask("spotify at 50", on_sentence=spoken.append)
        self.assertEqual(spoken, ["I opened Spotify", "Volume is 50."])
        self.assertIn('already heard you say: "I opened Spotify"', act.asked[1])

    def test_an_offline_machine_is_an_answer(self):
        act = self.ToolLane([("I can't check the Dell right now because the Dell is offline.", [])])
        reply = Brain(self.Lane("x"), act, wants=lambda t: True).ask("what's open on the dell")
        self.assertEqual(reply, "I can't check the Dell right now because the Dell is offline.")
        self.assertEqual(len(act.asked), 1)

    def test_a_disabled_feature_found_by_a_tool_is_an_answer(self):
        for finding in ("Developer tools are disabled in this Chrome profile.",
                        "The Lan Mouse tool is not connected right now."):
            act = self.ToolLane([(finding, [("browser_snapshot", True, "ok")])])
            self.assertEqual(Brain(self.Lane("x"), act, wants=lambda t: True).ask("check it"), finding)
            self.assertEqual(len(act.asked), 1)

    def test_the_decider_routes_what_the_keywords_miss(self):
        from unittest.mock import patch
        brain = Brain(self.Lane("x"), self.Lane("y"), decider=True)
        with patch("jarvis.decide.wants_tools", return_value=True) as asked:
            self.assertEqual(brain.pick("switch to discord"), "act")
        asked.assert_called_once()
        with patch("jarvis.decide.wants_tools", return_value=False):
            self.assertEqual(brain.pick("tell me about rome"), "chat")
        self.assertEqual(Brain(self.Lane("x"), self.Lane("y")).decider, False)  # off under tests

    def test_the_decider_never_blocks_on_its_model(self):
        from unittest.mock import patch
        from jarvis import decide
        started = []
        with patch.object(decide, "_models", {}), patch.object(decide, "_warming", []), \
                patch("threading.Thread", side_effect=lambda **k: type("T", (), {"start": lambda s: started.append(1)})()):
            self.assertFalse(decide.wants_tools("start up kitty"))
        self.assertEqual(started, [1])  # building in the background instead

    def test_a_failed_warm_up_is_logged_and_retried_later(self):
        import io
        from unittest.mock import patch
        from jarvis import decide

        class Inline:  # runs the build on the calling thread
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()
        failed, err = {}, io.StringIO()
        with patch.object(decide, "_models", {}), patch.object(decide, "_warming", []), \
                patch.object(decide, "_warm_failed", failed), patch("threading.Thread", Inline), \
                patch.object(decide, "choose", side_effect=RuntimeError("no model")) as built, \
                patch("sys.stderr", err):
            decide.warm("mode")
            self.assertEqual(decide._warming, [])  # the flag is cleared, not stuck
            self.assertIn("building the mode model failed", err.getvalue())
            decide.warm("mode")  # inside the cooldown: no second build
            self.assertEqual(built.call_count, 1)
            failed["mode"] -= decide.WARM_RETRY_S + 1  # cooldown over
            decide.warm("mode")
            self.assertEqual(built.call_count, 2)

    def test_a_silent_warm_up_failure_counts_as_failed(self):
        import io
        from unittest.mock import patch
        from jarvis import decide

        class Inline:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()
        failed = {}
        with patch.object(decide, "_models", {}), patch.object(decide, "_warming", []), \
                patch.object(decide, "_warm_failed", failed), patch("threading.Thread", Inline), \
                patch.object(decide, "choose", return_value=None), patch("sys.stderr", io.StringIO()):
            decide.warm("mode")  # no embedding model: choose() returns None
        self.assertIn("mode", failed)

    def test_one_job_warming_does_not_block_another(self):
        from unittest.mock import patch
        from jarvis import decide
        started = []
        with patch.object(decide, "_models", {}), patch.object(decide, "_warming", ["notes"]), \
                patch.object(decide, "_warm_failed", {}), \
                patch("threading.Thread", side_effect=lambda **k: type("T", (), {"start": lambda s: started.append(1)})()):
            decide.warm("mode")
        self.assertEqual(started, [1])

    def test_plain_talk_stays_talk(self):
        chat, act = self.Lane("Lima."), self.Lane("x")
        self.assertEqual(Brain(chat, act, wants=lambda t: False).ask("capital of Peru?"), "Lima.")
        self.assertEqual(act.asked, [])


if __name__ == "__main__":
    unittest.main()
