"""DesktopTools / actions executors: spotify deep links, open_url payloads, browsers.
Nothing real runs: _run, subprocess, shutil.which, time.sleep and os.name are patched."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis import actions


def done(code=0, err=""):
    return SimpleNamespace(returncode=code, stdout="", stderr=err)


class SpotifyDeepLinkTests(unittest.TestCase):
    def go(self, url, os_name, run_result=None):
        tools = actions.DesktopTools()
        with mock.patch.object(actions.os, "name", os_name), \
                mock.patch.object(actions, "_run", return_value=run_result or done()) as run, \
                mock.patch.object(actions.time, "sleep") as sleep, \
                mock.patch.object(actions.subprocess, "Popen") as popen, \
                mock.patch.object(actions, "_spotify_ready", return_value=True):
            result = tools.open_url(url)
        popen.assert_not_called()
        return result, run, sleep

    VALID = ("spotify:collection:tracks", "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
             "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "spotify:album:abc123",
             "spotify:artist:0OdUWJ0sBjDrqHygGUXeCF")

    def test_valid_on_windows_uses_cmd_start(self):
        for url in self.VALID:
            with self.subTest(url=url):
                result, run, sleep = self.go(url, "nt")
                self.assertEqual(result, (True, "Opened Spotify."))
                run.assert_called_once_with("cmd.exe", "/c", "start", "", url)
                sleep.assert_not_called()

    def test_valid_on_linux_uses_xdg_open(self):
        for url in self.VALID:
            with self.subTest(url=url):
                result, run, _ = self.go(url, "posix")
                self.assertTrue(result[0])
                run.assert_called_once_with("xdg-open", url)

    def test_opener_failure_reported(self):
        result, _, sleep = self.go("spotify:collection:tracks", "posix", done(1, "no handler"))
        self.assertEqual(result, (False, "no handler"))
        sleep.assert_not_called()
        result, _, _ = self.go("spotify:collection:tracks", "posix", done(1, ""))
        self.assertEqual(result, (False, "Couldn't open Spotify."))

    def test_invalid_uris_run_nothing(self):
        for url in ("spotify:evil&calc", "spotify:track:", "spotify:", "spotify:track:a b",
                    "spotify:track:abc\nx", "spotify:collection:tracks&calc",
                    "spotify:user:x", "spotify:track:" + "a" * 65, "spotify:track:a;b",
                    "spotify:collection:tracks\n"):
            for os_name in ("nt", "posix"):
                with self.subTest(url=url, os=os_name):
                    result, run, sleep = self.go(url, os_name)
                    self.assertEqual(result, (False, "Invalid Spotify deep link."))
                    run.assert_not_called()
                    sleep.assert_not_called()


class CallPayloadTests(unittest.TestCase):
    def test_json_payload_routes_to_open_url(self):
        tools = actions.DesktopTools()
        payload = json.dumps({"url": "https://amazon.com", "browser": "chrome"})
        with mock.patch.object(actions.DesktopTools, "open_url",
                               return_value=(True, "ok")) as open_url:
            self.assertEqual(tools.call("open_url", {"url": payload}), (True, "ok"))
        open_url.assert_called_once_with("https://amazon.com", "chrome")

    def test_json_payload_without_browser(self):
        tools = actions.DesktopTools()
        with mock.patch.object(actions.DesktopTools, "open_url",
                               return_value=(True, "ok")) as open_url:
            tools.call("open_url", {"url": json.dumps({"url": "https://a.com"})})
        open_url.assert_called_once_with("https://a.com", None)

    def test_plain_url_and_separate_browser_arg(self):
        tools = actions.DesktopTools()
        with mock.patch.object(actions.DesktopTools, "open_url",
                               return_value=(True, "ok")) as open_url:
            tools.call("open_url", {"url": "https://a.com", "browser": "firefox"})
        open_url.assert_called_once_with("https://a.com", "firefox")

    def test_malformed_json_payload_reports_instead_of_raising(self):
        tools = actions.DesktopTools()
        with mock.patch.object(actions.DesktopTools, "open_url") as open_url:
            ok, text = tools.call("open_url", {"url": "{not json"})
        self.assertFalse(ok)
        open_url.assert_not_called()

    def test_payload_missing_url_reports(self):
        tools = actions.DesktopTools()
        ok, text = tools.call("open_url", {"url": json.dumps({"browser": "chrome"})})
        self.assertFalse(ok)
        self.assertIn("KeyError", text)

    def test_spotify_uri_through_call(self):
        tools = actions.DesktopTools()
        with mock.patch.object(actions.os, "name", "nt"), \
                mock.patch.object(actions, "_run", return_value=done()) as run, \
                mock.patch.object(actions, "_spotify_ready", return_value=True):
            self.assertEqual(tools.call("open_url", {"url": "spotify:collection:tracks"}),
                             (True, "Opened Spotify."))
        run.assert_called_once()


class WindowsMediaTests(unittest.TestCase):
    def call(self, command, state):
        with mock.patch.object(actions.os, "name", "nt"), \
                mock.patch.object(actions, "_windows_media_state", return_value=state) as read, \
                mock.patch.object(actions, "_press") as press:
            result = actions.media(command)
        return result, read, press

    def test_play_while_playing_does_not_toggle(self):
        result, read, press = self.call("play", {"status": "Playing"})
        self.assertEqual(result, (True, "Already playing."))
        read.assert_called_once_with()
        press.assert_not_called()

    def test_pause_while_paused_or_stopped_does_not_toggle(self):
        for status in ("Paused", "Stopped"):
            with self.subTest(status=status):
                result, _, press = self.call("pause", {"status": status})
                self.assertEqual(result, (True, "Already paused."))
                press.assert_not_called()

    def test_only_toggles_when_state_needs_change(self):
        result, _, press = self.call("play", {"status": "Paused"})
        self.assertEqual(result, (True, "Pressed play/pause."))
        press.assert_called_once_with(actions.VK["play_pause"])
        result, _, press = self.call("pause", {"status": "Playing"})
        self.assertEqual(result, (True, "Pressed play/pause."))
        press.assert_called_once_with(actions.VK["play_pause"])

    def test_unknown_state_never_toggles(self):
        result, _, press = self.call("play", None)
        self.assertEqual(result, (False, "Couldn't read the Windows media playback state."))
        press.assert_not_called()

    def test_reads_smtc_json_and_rejects_failed_powershell(self):
        with mock.patch.object(actions, "_run", return_value=done()) as run:
            run.return_value.stdout = '{"status":"Playing","app_id":"Spotify"}'
            self.assertEqual(actions._windows_media_state(),
                             {"status": "Playing", "app_id": "Spotify"})
            self.assertIn("$ErrorActionPreference = 'Stop'", run.call_args.args[-1])
        with mock.patch.object(actions, "_run", return_value=done(1, "WinRT unavailable")):
            self.assertIsNone(actions._windows_media_state())

    def test_spotify_readiness_poll_is_bounded_to_six_seconds(self):
        elapsed = [0.0]

        def sleep(duration):
            elapsed[0] += duration

        with mock.patch.object(actions, "_spotify_ready", return_value=False) as ready:
            self.assertFalse(actions._wait_for_spotify(
                clock=lambda: elapsed[0], sleep=sleep))
        self.assertEqual(elapsed[0], 6.0)
        self.assertGreater(ready.call_count, 1)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tools = actions.DesktopTools()
        self.Popen = self.enter(mock.patch.object(actions.subprocess, "Popen"))
        self.enter(mock.patch.object(actions.os, "name", "nt"))
        self.match = self.enter(mock.patch.object(actions.DesktopTools, "match",
                                                  side_effect=AssertionError("app scan")))

    def enter(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_unsupported_browser(self):
        ok, text = self.tools.open_url("https://example.com", "netscape")
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Unsupported browser"), text)
        self.Popen.assert_not_called()

    def test_non_http_urls_refused(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.com/a",
                    "not a url", "https://a.com/x y"):
            with self.subTest(url=url):
                self.assertEqual(self.tools.open_url(url, "chrome"),
                                 (False, "Only http(s) links can be opened."))
        self.Popen.assert_not_called()

    def test_known_browser_launches_found_executable(self):
        with mock.patch.object(actions.shutil, "which", return_value=r"C:\x\chrome.exe"):
            ok, text = self.tools.open_url("https://example.com", "Google Chrome")
        self.assertTrue(ok)
        self.assertIn("Google Chrome", text)
        self.assertEqual(self.Popen.call_args.args[0], [r"C:\x\chrome.exe", "https://example.com"])

    def test_bare_domain_gets_https(self):
        with mock.patch.object(actions.shutil, "which", return_value=r"C:\x\msedge.exe"):
            self.tools.open_url("example.com/a", "edge")
        self.assertEqual(self.Popen.call_args.args[0][1], "https://example.com/a")

    def test_browser_not_installed(self):
        winreg = __import__("winreg") if hasattr(__import__("os"), "startfile") else None
        if winreg is None:
            self.skipTest("winreg is Windows only")
        with mock.patch.object(actions.shutil, "which", return_value=None), \
                mock.patch.object(winreg, "OpenKey", side_effect=OSError):
            ok, text = self.tools.open_url("https://example.com", "firefox")
        self.assertFalse(ok)
        self.assertIn("Couldn't find", text)
        self.Popen.assert_not_called()

    def test_no_browser_uses_default_via_vault_open_link(self):
        with mock.patch("jarvis.vault.open_link") as open_link:
            ok, text = self.tools.open_url("https://example.com")
        self.assertTrue(ok)
        open_link.assert_called_once_with("https://example.com")


class VolumeCoreAudioTests(unittest.TestCase):
    def core(self, now=0.5, muted=False):
        """core_audio fake: reads return (now, muted); sets echo back what was set."""
        def fake(level=None, mute=None):
            return (now if level is None else level), (muted if mute is None else mute)
        return mock.patch("jarvis.observe.core_audio", side_effect=fake)

    def test_mute_and_unmute_set_rather_than_toggle(self):
        with self.core() as core:
            self.assertEqual(actions._volume_core_audio("mute"), (True, "Volume is 50 percent, muted."))
            core.assert_called_once_with(mute=True)
        with self.core(muted=True) as core:
            self.assertEqual(actions._volume_core_audio("unmute"), (True, "Volume is 50 percent."))
            core.assert_called_once_with(mute=False)

    def test_digits_set_level(self):
        with self.core() as core:
            self.assertEqual(actions._volume_core_audio("30"), (True, "Volume is 30 percent."))
            core.assert_called_once_with(level=0.3)

    def test_up_reads_then_sets_and_unmutes(self):
        step = actions.VOLUME_STEP / 100
        with self.core(now=0.5) as core:
            ok, text = actions._volume_core_audio("up")
        self.assertTrue(ok)
        self.assertEqual(core.call_args_list[0], mock.call())
        self.assertEqual(core.call_args_list[1], mock.call(level=min(1.0, 0.5 + step), mute=False))
        self.assertEqual(text, f"Volume is {round(min(1.0, 0.5 + step) * 100)} percent.")

    def test_down_reads_then_sets_without_touching_mute(self):
        step = actions.VOLUME_STEP / 100
        with self.core(now=0.5) as core:
            actions._volume_core_audio("down")
        self.assertEqual(core.call_args_list[1], mock.call(level=0.5 - step, mute=None))

    def test_clamps_at_both_ends(self):
        with self.core(now=0.99) as core:
            actions._volume_core_audio("up")
        self.assertEqual(core.call_args_list[1], mock.call(level=1.0, mute=False))
        with self.core(now=0.01) as core:
            actions._volume_core_audio("down")
        self.assertEqual(core.call_args_list[1], mock.call(level=0.0, mute=None))

    def test_volume_on_windows_uses_core_audio_not_keys(self):
        with mock.patch.object(actions.os, "name", "nt"), \
                mock.patch.object(actions, "_press") as press, self.core():
            self.assertEqual(actions.volume("40%"), (True, "Volume is 40 percent."))
        press.assert_not_called()

    def test_volume_falls_back_to_keys_when_core_audio_unavailable(self):
        with mock.patch.object(actions.os, "name", "nt"), \
                mock.patch.object(actions, "_press") as press, \
                mock.patch("jarvis.observe.core_audio", side_effect=LookupError("no")):
            self.assertEqual(actions.volume("mute"), (True, "Toggled mute."))
        press.assert_called_once_with(actions.VK["mute"])

    def test_core_audio_rejects_out_of_range_before_com(self):
        from jarvis import observe

        def boom(*a, **k):
            raise AssertionError("COM touched")
        with mock.patch.object(observe.os, "name", "nt"), \
                mock.patch.object(observe.ctypes, "WinDLL", boom, create=True):
            for bad in (1.5, -0.1):
                with self.subTest(level=bad), self.assertRaises(ValueError):
                    observe.core_audio(level=bad)

    def test_media_play_with_unreadable_state_presses_nothing(self):
        for command in ("play", "pause"):
            with self.subTest(command=command), mock.patch.object(actions.os, "name", "nt"), \
                    mock.patch.object(actions, "_windows_media_state", return_value=None), \
                    mock.patch.object(actions, "_press") as press:
                self.assertEqual(actions.media(command),
                                 (False, "Couldn't read the Windows media playback state."))
                press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
