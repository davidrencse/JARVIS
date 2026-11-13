"""L5 observe: predicates, matching, polling. Never lists the real desktop."""

import json
import unittest
from unittest import mock

from jarvis import observe, remote


def win(app, title="", focused=False):
    return {"app": app, "title": title, "focused": focused}


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PredicateTests(unittest.TestCase):
    def cmd(self, intent, **args):
        return {"intent": intent, "args": args}

    def test_app_intents(self):
        self.assertEqual(observe.predicate_for(self.cmd("open_app", name="Spotify")),
                         {"kind": "window", "app": "Spotify"})
        self.assertEqual(observe.predicate_for(self.cmd("focus_window", name="Code")),
                         {"kind": "focused", "app": "Code"})
        self.assertEqual(observe.predicate_for(self.cmd("close_app", name="Notepad")),
                         {"kind": "gone", "app": "Notepad"})

    def test_open_url_words(self):
        for url, word in (("https://www.youtube.com/watch?v=1", "youtube"),
                          ("https://mail.google.com/", "gmail"),
                          ("https://github.com/x/y", "github"),
                          ("https://www.amazon.com", "amazon")):
            with self.subTest(url=url):
                self.assertEqual(observe.predicate_for(self.cmd("open_url", url=url)),
                                 {"kind": "title", "text": word})

    def test_open_url_without_host_is_none(self):
        self.assertIsNone(observe.predicate_for(self.cmd("open_url", url="not a url")))

    def test_media_volume_give_state_predicates_list_apps_none(self):
        self.assertEqual(observe.predicate_for(self.cmd("media", command="pause")),
                         {"kind": "playback", "command": "pause"})
        self.assertEqual(observe.predicate_for(self.cmd("media", command="Play-Pause")),
                         {"kind": "playback", "command": "play_pause"})
        self.assertEqual(observe.predicate_for(self.cmd("volume", level="30%")),
                         {"kind": "volume", "level": "30"})
        self.assertEqual(observe.predicate_for(self.cmd("volume", level="Mute")),
                         {"kind": "volume", "level": "mute"})
        self.assertEqual(observe.predicate_for(self.cmd("spotify_play", target="liked")),
                         {"kind": "playback", "command": "play"})
        self.assertIsNone(observe.predicate_for(self.cmd("list_apps", query="")))

    def test_empty_media_or_volume_is_none(self):
        self.assertIsNone(observe.predicate_for(self.cmd("media", command="")))
        self.assertIsNone(observe.predicate_for(self.cmd("volume", level="")))


class AppOnlyPredicateTests(unittest.TestCase):
    kitty = win("kitty", "nvim discord_bot.py")

    def test_terminal_title_is_not_the_app(self):
        self.assertFalse(observe.matches({"kind": "window", "app": "discord"}, [self.kitty])[0])
        self.assertTrue(observe.matches({"kind": "gone", "app": "discord"}, [self.kitty])[0])
        listed = [self.kitty, win("discord", "General")]
        self.assertEqual(observe.matches({"kind": "window", "app": "discord"}, listed),
                         (True, "discord"))

    def test_multi_word_name_needs_every_word(self):
        studio = win("jetbrains-studio", "MyApp")
        self.assertEqual(observe.app_match(studio, "visual studio code", {"visual", "studio", "code"}), 0)
        self.assertEqual(observe.app_match(win("code-oss", "x"), "visual studio code",
                                           {"visual", "studio", "code"}), 3)


class ValidateTests(unittest.TestCase):
    def test_good(self):
        p = {"kind": "title", "text": "youtube"}
        self.assertIs(observe.validate(p), p)
        observe.validate({"kind": "gone", "app": "Spotify"})

    def test_bad(self):
        for p in (None, "x", {}, {"kind": "nope", "app": "chrome"},
                  {"kind": "window"}, {"kind": "window", "app": "a"},
                  {"kind": "window", "app": "!!"}, {"kind": "window", "app": 5},
                  {"kind": "title", "app": "chrome"}, {"kind": "title", "text": "x"},
                  {"kind": "window", "app": "a" * 101}):
            with self.subTest(p=p), self.assertRaises(ValueError):
                observe.validate(p)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.listed = [win("chrome", "Inbox - Gmail", focused=False),
                       win("Spotify", "Spotify Free", focused=True)]

    def test_window_by_process_name(self):
        self.assertEqual(observe.matches({"kind": "window", "app": "Spotify"}, self.listed),
                         (True, "Spotify"))

    def test_window_by_title(self):
        self.assertTrue(observe.matches({"kind": "window", "app": "gmail"}, self.listed)[0])

    def test_window_absent(self):
        self.assertEqual(observe.matches({"kind": "window", "app": "notepad"}, self.listed),
                         (False, None))

    def test_title(self):
        self.assertEqual(observe.matches({"kind": "title", "text": "gmail"}, self.listed),
                         (True, "chrome"))
        self.assertFalse(observe.matches({"kind": "title", "text": "youtube"}, self.listed)[0])

    def test_focused_only_focused_window(self):
        self.assertTrue(observe.matches({"kind": "focused", "app": "spotify"}, self.listed)[0])
        self.assertFalse(observe.matches({"kind": "focused", "app": "chrome"}, self.listed)[0])

    def test_gone(self):
        self.assertTrue(observe.matches({"kind": "gone", "app": "notepad"}, self.listed)[0])
        self.assertFalse(observe.matches({"kind": "gone", "app": "spotify"}, self.listed)[0])

    def test_gone_ignores_browser_tab_named_like_the_app(self):
        listed = [win("google-chrome", "Spotify - Web Player: Music for everyone - Google Chrome")]
        self.assertEqual(observe.matches({"kind": "gone", "app": "Spotify"}, listed), (True, None))
        self.assertTrue(observe.matches({"kind": "window", "app": "Spotify"}, listed)[0])


class CheckTests(unittest.TestCase):
    P = {"kind": "window", "app": "spotify"}

    def run_check(self, snapshots, settle=5):
        clock, polls = Clock(), []

        def lister():
            polls.append(clock.now)
            return snapshots[min(len(polls) - 1, len(snapshots) - 1)]
        got = observe.check(self.P, settle, lister=lister, clock=clock, sleep=clock.sleep)
        return got, polls, clock

    def test_immediate_match(self):
        got, polls, clock = self.run_check([[win("Spotify")]])
        self.assertEqual(got, {"match": True, "after_ms": 0, "app": "Spotify"})
        self.assertEqual(len(polls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_match_on_third_poll(self):
        got, polls, _ = self.run_check([[], [], [win("Spotify")]])
        self.assertEqual(len(polls), 3)
        self.assertEqual(got["after_ms"], 500)
        self.assertTrue(got["match"])

    def test_never_matches(self):
        got, _, clock = self.run_check([[]], settle=1)
        self.assertEqual((got["match"], got["after_ms"], got["app"]), (False, 1000, None))
        self.assertAlmostEqual(clock.now, 1.0)

    def test_settle_capped(self):
        got, _, _ = self.run_check([[]], settle=999)
        self.assertEqual(got["after_ms"], round(observe.MAX_SETTLE * 1000))

    def test_negative_settle_polls_once(self):
        got, polls, _ = self.run_check([[]], settle=-3)
        self.assertEqual((got["match"], got["after_ms"], len(polls)), (False, 0, 1))

    def test_bad_predicate_raises_without_listing(self):
        lister = mock.Mock()
        with self.assertRaises(ValueError):
            observe.check({"kind": "x"}, 1, lister=lister)
        lister.assert_not_called()


def fake_windows(clock=None, **kw):
    """observe.run() reaches the lister through check()'s default argument, which is bound
    at import time, so patching observe.windows alone does not stop a real desktop listing
    (suspected source bug). Patch the name and the default so nothing real is listed."""
    fake = mock.Mock(**kw)
    defaults = (fake,) + observe.check.__defaults__[1:]
    if clock is not None:  # check()'s clock is bound at import too: pin it so a loaded box can't tick
        defaults = (fake, clock) + defaults[2:]
    stack = mock.patch.object(observe, "windows", fake)
    return fake, stack, mock.patch.object(observe.check, "__defaults__", defaults)


class RunTests(unittest.TestCase):
    def test_valid(self):
        arg = json.dumps({"predicate": {"kind": "window", "app": "spotify"}, "settle": 2})
        fake, a, b = fake_windows(clock=lambda: 0.0, return_value=[win("Spotify")])
        with a, b:
            ok, text = observe.run(arg)
        self.assertTrue(ok)
        self.assertEqual(json.loads(text), {"match": True, "after_ms": 0, "app": "Spotify"})

    def test_bad_input(self):
        good = {"kind": "window", "app": "spotify"}
        fake, a, b = fake_windows(return_value=[])
        with a, b:
            for arg in ("not json", "[]", json.dumps({}),
                        json.dumps({"predicate": {"kind": "nope"}, "settle": 1}),
                        json.dumps({"predicate": good, "settle": "soon"})):
                with self.subTest(arg=arg):
                    ok, text = observe.run(arg)
                    self.assertFalse(ok)
                    self.assertTrue(text.startswith("Can't observe: "))

    def test_lister_failure_reported(self):
        arg = json.dumps({"predicate": {"kind": "window", "app": "spotify"}, "settle": 1})
        fake, a, b = fake_windows(side_effect=LookupError("no hyprctl"))
        with a, b:
            ok, text = observe.run(arg)
        self.assertEqual((ok, text), (False, "Can't observe: no hyprctl"))


class HyprlandTimeoutTests(unittest.TestCase):
    def test_hyprctl_timeout_becomes_lookup_error(self):
        with mock.patch.object(observe.shutil, "which", return_value="/usr/bin/hyprctl"), \
                mock.patch.object(observe.subprocess, "run",
                                  side_effect=observe.subprocess.TimeoutExpired("hyprctl", 5)):
            with self.assertRaises(LookupError):
                observe._windows_hyprland()


class PlaybackVolumeValidateTests(unittest.TestCase):
    def test_playback_commands_allowlisted(self):
        from jarvis.registry import MEDIA
        for c in MEDIA:
            with self.subTest(c=c):
                p = {"kind": "playback", "command": c}
                self.assertIs(observe.validate(p), p)
        for c in ("rm", "", None, 5, "PLAY"):
            with self.subTest(c=c), self.assertRaises(ValueError):
                observe.validate({"kind": "playback", "command": c})
        with self.assertRaises(ValueError):
            observe.validate({"kind": "playback"})

    def test_volume_levels(self):
        for level in ("up", "down", "mute", "unmute", "0", "7", "100"):
            with self.subTest(level=level):
                observe.validate({"kind": "volume", "level": level})
        for level in ("101", "999", "loud", "rm", "", "-5", "5.5", 50, None, "1000"):
            with self.subTest(level=level), self.assertRaises(ValueError):
                observe.validate({"kind": "volume", "level": level})


class PlaybackVolumeMatchTests(unittest.TestCase):
    def pb(self, command, state):
        return observe.matches({"kind": "playback", "command": command}, state)

    def test_play_variants_need_playing(self):
        for c in ("play", "play_pause"):
            self.assertEqual(self.pb(c, {"status": "Playing"}), (True, None))
            self.assertEqual(self.pb(c, {"status": "Paused"}), (False, None))

    def test_pause_and_stop(self):
        for c in ("pause", "stop"):
            self.assertTrue(self.pb(c, {"status": "Paused"})[0])
            self.assertTrue(self.pb(c, {"status": "Stopped"})[0])
            self.assertFalse(self.pb(c, {"status": "Playing"})[0])

    def test_next_previous_need_any_session(self):
        for c in ("next", "previous"):
            self.assertTrue(self.pb(c, {"status": "Playing"})[0])
            self.assertFalse(self.pb(c, {"status": ""})[0])

    def test_none_or_empty_state_never_matches(self):
        for c in ("play", "pause", "next"):
            self.assertEqual(self.pb(c, None), (False, None))
            self.assertEqual(self.pb(c, {}), (False, None))

    def vol(self, level, state):
        return observe.matches({"kind": "volume", "level": level}, state)

    def test_volume_digits(self):
        self.assertEqual(self.vol("30", {"percent": 30, "muted": False}), (True, None))
        self.assertFalse(self.vol("30", {"percent": 40, "muted": False})[0])
        self.assertTrue(self.vol("0", {"percent": 0, "muted": False})[0])

    def test_volume_mute_unmute(self):
        self.assertTrue(self.vol("mute", {"percent": 30, "muted": True})[0])
        self.assertFalse(self.vol("mute", {"percent": 30, "muted": False})[0])
        self.assertTrue(self.vol("unmute", {"percent": 30, "muted": False})[0])
        self.assertFalse(self.vol("unmute", {"percent": 30, "muted": True})[0])

    def test_volume_relative_matches_any_readback(self):
        for level in ("up", "down"):
            self.assertTrue(self.vol(level, {"percent": 50, "muted": False})[0])

    def test_volume_none_state(self):
        for level in ("30", "mute", "up"):
            self.assertEqual(self.vol(level, None), (False, None))


class PlaybackVolumeCheckTests(unittest.TestCase):
    def test_playback_uses_injected_lister_and_polls(self):
        clock = Clock()
        lister = mock.Mock(side_effect=[{"status": "Paused"}, {"status": "Playing"}])
        got = observe.check({"kind": "playback", "command": "play"}, 5, lister=lister,
                            clock=clock, sleep=clock.sleep)
        self.assertEqual(got, {"match": True, "after_ms": 250, "app": None})
        self.assertEqual(lister.call_count, 2)

    def test_default_lister_picked_by_kind(self):
        with mock.patch.object(observe, "playback", return_value={"status": "Playing"}) as pb, \
                mock.patch.object(observe, "volume_state",
                                  return_value={"percent": 20, "muted": False}) as vs, \
                mock.patch.object(observe, "windows", return_value=[]) as wn:
            observe.check({"kind": "playback", "command": "play"}, 0)
            pb.assert_called_once()
            vs.assert_not_called()
            observe.check({"kind": "volume", "level": "20"}, 0)
            vs.assert_called_once()
            wn.assert_not_called()

    def test_windows_volume_state_maps_core_audio_readback(self):
        with mock.patch.object(observe.os, "name", "nt"), \
                mock.patch.object(observe, "_core_audio_volume", return_value=(0.305, 0)) as read:
            self.assertEqual(observe.volume_state(), {"percent": 30, "muted": False})
        read.assert_called_once_with()

        with mock.patch.object(observe.os, "name", "nt"), \
                mock.patch.object(observe, "_core_audio_volume", return_value=(0.5, True)):
            self.assertEqual(observe.volume_state(), {"percent": 50, "muted": True})

    def test_run_turns_windows_volume_com_failure_into_cant_observe(self):
        arg = json.dumps({"predicate": {"kind": "volume", "level": "30"}, "settle": 1})
        with mock.patch.object(observe.os, "name", "nt"), \
                mock.patch.object(observe, "_core_audio_volume",
                                  side_effect=LookupError("GetMute failed")) as read:
            ok, text = observe.run(arg)
        self.assertFalse(ok)
        self.assertEqual(text, "Can't observe: GetMute failed")
        read.assert_called_once_with()


class RunHereTests(unittest.TestCase):
    def test_routes_to_observe_run(self):
        with mock.patch("jarvis.observe.run", return_value=(True, "{}")) as run, \
                mock.patch("jarvis.actions.DesktopTools") as tools:
            self.assertEqual(remote.run_here("observe", '{"a": 1}'), (True, "{}"))
        run.assert_called_once_with('{"a": 1}')
        tools.assert_not_called()


class SnapshotTests(unittest.TestCase):
    """A predicate that already held before the action proves nothing: with a `before`
    digest the state must also change, and only a digest crosses the wire."""
    P = {"kind": "window", "app": "Spotify"}

    def test_already_open_and_untouched_is_unchanged(self):
        listed = [win("Spotify", "Spotify Premium", focused=True)]
        before = observe.snapshot(self.P, lister=lambda: listed)["before"]
        self.assertRegex(before, r"^[0-9a-f]{16}$")
        clock = Clock()
        got = observe.check(self.P, 5, lister=lambda: listed, clock=clock, sleep=clock.sleep,
                            before=before)
        self.assertEqual((got["match"], got["changed"]), (True, False))
        self.assertLess(got["after_ms"], 5000)  # a short grace, not the whole settle

    def test_raising_an_open_window_is_a_change(self):
        before = observe.snapshot(self.P, lister=lambda: [win("Spotify", "S")])["before"]
        got = observe.check(self.P, 5, lister=lambda: [win("Spotify", "S", focused=True)],
                            before=before)
        self.assertEqual((got["match"], got["changed"]), (True, True))

    def test_new_window_is_a_change(self):
        before = observe.snapshot(self.P, lister=lambda: [])["before"]
        got = observe.check(self.P, 5, lister=lambda: [win("Spotify", "S")], before=before)
        self.assertEqual((got["match"], got["changed"]), (True, True))

    def test_skip_needs_a_new_track(self):
        p = {"kind": "playback", "command": "next"}
        before = observe.snapshot(p, lister=lambda: {"status": "Playing", "track": "a"})["before"]
        clock = Clock()
        same = observe.check(p, 3, lister=lambda: {"status": "Playing", "track": "a"},
                             clock=clock, sleep=clock.sleep, before=before)
        moved = observe.check(p, 3, lister=lambda: {"status": "Playing", "track": "b"},
                              before=before)
        self.assertFalse(same["changed"])
        self.assertTrue(moved["changed"])

    def test_no_snapshot_where_it_cannot_tell(self):
        for p in ({"kind": "playback", "command": "pause"}, {"kind": "volume", "level": "30"},
                  {"kind": "gone", "app": "Spotify"}):
            self.assertEqual(observe.snapshot(p, lister=lambda: 1 / 0), {"before": None})
        p = {"kind": "playback", "command": "next"}  # no track identity on this player
        self.assertEqual(observe.snapshot(p, lister=lambda: {"status": "Playing"}),
                         {"before": None})

    def test_run_answers_snapshot_and_rejects_bad_digest(self):
        _, stack, defaults = fake_windows(return_value=[win("Spotify", "S")])
        with stack, defaults:
            ok, text = observe.run(json.dumps({"snapshot": self.P}))
            self.assertTrue(ok)
            self.assertRegex(json.loads(text)["before"], r"^[0-9a-f]{16}$")
            ok, _ = observe.run(json.dumps({"predicate": self.P, "settle": 0, "before": "x"}))
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
