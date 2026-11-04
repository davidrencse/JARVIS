"""The spotify tool: links parsed strictly, playback checked against what Spotify reports.
The player is mocked: nothing plays."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import actions


def done(out="", code=0):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


class SpotifyTests(unittest.TestCase):
    def test_targets(self):
        u = actions.spotify_uri
        self.assertEqual(u("track:0VjIjW4GlUZAMYd2vXMi3b"), "spotify:track:0VjIjW4GlUZAMYd2vXMi3b")
        self.assertEqual(u("https://open.spotify.com/intl-de/album/4LH4d3cOWNNsVw41Gqt2kv?si=x"),
                         "spotify:album:4LH4d3cOWNNsVw41Gqt2kv")
        self.assertEqual(u("liked"), "spotify:collection:tracks")
        for bad in ("track:short", "; rm -rf ~", "spotify:track:0VjIjW4GlUZAMYd2vXMi3b; x", ""):
            self.assertIsNone(u(bad), bad)

    def fake_linux(self, playing="Blinding Lights", before="/com/spotify/track/old"):
        state = {"opened": None}

        def run(*args):
            if args[:2] == ("playerctl", "-l"):
                return done("spotify\n")
            if "open" in args:
                state["opened"] = args[-1]
                return done()
            if "status" in args:
                return done("Playing" if state["opened"] else "Paused")
            if "metadata" in args:
                field = args[-1]
                if "trackid" in field:
                    return done("/com/spotify/track/0VjIjW4GlUZAMYd2vXMi3b" if state["opened"] else before)
                return done(playing if "title" in field else "The Weeknd")
            return done()
        return state, run

    def test_play_reports_what_started(self):
        state, run = self.fake_linux()
        with patch.object(actions, "_run", side_effect=run), patch.object(actions, "_backend", return_value="hyprland"), \
                patch.object(actions.os, "name", "posix"), patch.object(actions.shutil, "which", return_value="/usr/bin/playerctl"):
            ok, said = actions.spotify(json.dumps({"action": "play", "target": "track:0VjIjW4GlUZAMYd2vXMi3b"}))
        self.assertTrue(ok, said)
        self.assertEqual(state["opened"], "spotify:track:0VjIjW4GlUZAMYd2vXMi3b")
        self.assertIn("Blinding Lights by The Weeknd", said)

    def test_bad_target_never_reaches_spotify(self):
        with patch.object(actions, "_run") as run:
            ok, said = actions.spotify(json.dumps({"action": "play", "target": "blinding lights"}))
        self.assertFalse(ok)
        self.assertIn("open.spotify.com/search", said)
        run.assert_not_called()

    def test_search_opens_the_apps_search(self):
        state, run = self.fake_linux()
        with patch.object(actions, "_run", side_effect=run), patch.object(actions, "_backend", return_value="hyprland"), \
                patch.object(actions.os, "name", "posix"), patch.object(actions.shutil, "which", return_value="/usr/bin/playerctl"):
            ok, said = actions.spotify(json.dumps({"action": "search", "query": "lofi beats"}))
        self.assertTrue(ok, said)
        self.assertEqual(state["opened"], "spotify:search:lofi%20beats")

    def test_says_so_when_spotify_wont_stay_open(self):
        with patch.object(actions, "_run", return_value=done("")), patch.object(actions, "_backend", return_value="hyprland"), \
                patch.object(actions.os, "name", "posix"), patch.object(actions.shutil, "which", return_value="/usr/bin/playerctl"), \
                patch.object(actions.DesktopTools, "open_app", return_value=(True, "Opened Spotify.")), \
                patch.object(actions, "_spotify_player", return_value=False):
            ok, said = actions.spotify(json.dumps({"action": "play", "target": "liked"}))
        self.assertFalse(ok)
        self.assertIn("didn't stay open", said)


if __name__ == "__main__":
    unittest.main()
