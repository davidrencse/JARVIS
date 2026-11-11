"""The agent browser: parser wiring, the browse executor, the live frame and its fail-safes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import actions, agent_browser, parse, registry
from jarvis.pipeline import DIRECT

DEVICES = {"dell": {}, "zenbook": {}}


def commands(text):
    result = parse.parse(text, DEVICES, "dell")
    return result["commands"] and [(c["intent"], c["args"], c["depends_on"])
                                   for c in result["commands"]], result["reason"]


class ParseTests(unittest.TestCase):
    def test_go_to_instagram_and_scroll_in_the_agent_browser(self):
        got, _ = commands("in the agent browser go to instagram and scroll")
        self.assertEqual(got, [
            ("browse", {"action": "open", "url": "https://www.instagram.com"}, []),
            ("browse", {"action": "scroll", "direction": "down", "mode": "once"}, ["c1"])])

    def test_the_phrase_may_come_anywhere_and_keep_scrolling_is_auto(self):
        got, _ = commands("go to instagram in the agent browser and start scrolling")
        self.assertEqual(got[1][1]["mode"], "auto")
        self.assertEqual(got[1][2], ["c1"])

    def test_without_the_phrase_it_is_still_davids_browser(self):
        got, _ = commands("go to instagram and scroll")
        self.assertEqual([g[0] for g in got], ["open_url", "scroll"])

    def test_close_the_agent_browser(self):
        self.assertEqual(commands("jarvis, close the agent browser")[0],
                         [("browse", {"action": "close"}, [])])

    def test_stop_scrolling_stays_a_scroll_that_stops_both(self):
        self.assertEqual(commands("stop scrolling in the agent browser")[0],
                         [("scroll", {"mode": "stop"}, [])])

    def test_apps_cannot_open_in_a_browser(self):
        got, reason = commands("in the agent browser open spotify")
        self.assertIsNone(got)
        self.assertIn("agent browser", reason)

    def test_a_named_device_carries_over(self):
        result = parse.parse("on zenbook in the agent browser go to instagram", DEVICES, "dell")
        self.assertEqual(result["commands"][0]["device"], "zenbook")


class RegistryTests(unittest.TestCase):
    def test_browse_is_a_direct_intent_with_strict_args(self):
        self.assertIn("browse", DIRECT)
        good = [{"id": "c1", "intent": "browse", "device": "dell",
                 "args": {"action": "open", "url": "https://x.com"}, "depends_on": []}]
        self.assertEqual(registry.validate(good, DEVICES), [])
        for bad in ({}, {"action": "eval"}, {"action": "open", "url": "file:///etc/passwd"},
                    {"action": "open", "url": "spotify:liked"}, {"action": "scroll", "mode": "x"},
                    {"action": "scroll", "every": "1"}, {"action": "open", "session": "other"}):
            self.assertTrue(registry.validate([{**good[0], "args": bad}], DEVICES), bad)


class BrowseTests(unittest.TestCase):
    def setUp(self):
        agent_browser.stop_scrolling()
        self.calls = []
        run = mock.patch.object(agent_browser, "run",
                                side_effect=lambda *a, **k: self.calls.append(a) or (True, {}))
        run.start()
        self.addCleanup(run.stop)
        self.addCleanup(agent_browser.stop_scrolling)

    def test_open_only_takes_http_pages(self):
        ok, _ = agent_browser.browse({"action": "open", "url": "file:///etc/passwd"})
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        ok, _ = agent_browser.browse(json.dumps({"action": "open", "url": "https://x.com"}))
        self.assertTrue(ok)
        self.assertEqual(self.calls, [("open", "https://x.com")])

    def test_unknown_actions_are_refused(self):
        self.assertFalse(agent_browser.browse({"action": "eval"})[0])
        self.assertFalse(agent_browser.browse("not json")[0])

    def test_scrolling_a_closed_browser_never_launches_one(self):
        with mock.patch.object(agent_browser, "running", return_value=False):
            for spec in ({"action": "scroll"}, {"action": "back"}, {"action": "reload"},
                         {"action": "close"}):
                agent_browser.browse(spec)
        self.assertEqual(self.calls, [])

    def test_scroll_once_scrolls_a_screen(self):
        with mock.patch.object(agent_browser, "running", return_value=True):
            ok, _ = agent_browser.browse({"action": "scroll", "direction": "up"})
        self.assertTrue(ok)
        self.assertEqual(self.calls, [("scroll", "up", str(agent_browser.SCROLL_PX))])

    def test_auto_starts_one_thread_and_desktop_stop_scrolling_ends_it(self):
        with mock.patch.object(agent_browser, "running", return_value=True), \
                mock.patch.object(agent_browser, "_scroll_loop"):
            ok, text = agent_browser.browse({"action": "scroll", "mode": "auto", "every": "5"})
        self.assertTrue(ok, text)
        self.assertIsNotNone(agent_browser._SCROLLER["stop"])
        ok, text = actions.scroll(json.dumps({"mode": "stop"}))
        self.assertTrue(ok)
        self.assertIn("agent browser", text)
        self.assertIsNone(agent_browser._SCROLLER["stop"])

    def test_the_loop_ends_once_the_browser_is_gone(self):
        class Stop:
            def __init__(self):
                self.waits = 0

            def wait(self, _):
                self.waits += 1
                return self.waits > 50
        stop = Stop()
        with mock.patch.object(agent_browser, "running", return_value=False):
            agent_browser._scroll_loop(stop, "down", 1, None)
        self.assertEqual(stop.waits, agent_browser.SCROLL_MISSES)
        self.assertEqual(self.calls, [])


class FrameTests(unittest.TestCase):
    def test_a_closed_browser_is_reported_without_a_screenshot(self):
        with mock.patch.object(agent_browser, "running", return_value=False), \
                mock.patch.object(agent_browser, "run") as run:
            self.assertEqual(agent_browser.frame(), {"open": False})
        run.assert_not_called()

    def test_an_open_browser_returns_the_jpeg_and_page(self):
        with tempfile.TemporaryDirectory() as state:
            def fake(*args, **_):
                if args[0] == "screenshot":
                    Path(args[1]).write_bytes(b"\xff\xd8jpeg")
                    return True, {}
                return True, {args[1]: "Instagram" if args[1] == "title" else "https://www.instagram.com/"}
            with mock.patch.object(agent_browser, "running", return_value=True), \
                    mock.patch.object(agent_browser, "run", side_effect=fake):
                shot = agent_browser.frame(state)
        self.assertEqual(shot["image"], b"\xff\xd8jpeg")
        self.assertEqual(shot["title"], "Instagram")
        self.assertEqual(shot["url"], "https://www.instagram.com/")

    def test_running_needs_the_session_and_its_chrome(self):
        with mock.patch.object(agent_browser, "run", return_value=(True, {"sessions": []})):
            self.assertFalse(agent_browser.running())


class WiringTests(unittest.TestCase):
    def test_mcp_server_shares_the_cli_session_and_profile(self):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch("shutil.which", return_value="/usr/bin/npx"):
            servers = actions.browser_mcp(state, ("agent-browser",))
            env = servers["agent_browser"]["env"]
            self.assertEqual(env, agent_browser.env(state))
        self.assertEqual(env["AGENT_BROWSER_SESSION"], agent_browser.SESSION)

    def fake_build(self, root, version):
        exe = Path(root) / "cli" / "target" / "release" / "agent-browser"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\necho 'agent-browser %s'\n" % version)
        exe.chmod(0o755)
        return exe

    def test_source_build_is_preferred_only_at_the_pinned_version(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            exe = self.fake_build(root, agent_browser.VERSION)
            with mock.patch.dict("os.environ", {agent_browser.SOURCE_ENV: root}), \
                    mock.patch("shutil.which", return_value="/usr/bin/npx"):
                self.assertEqual(agent_browser.binary(), [str(exe)])
                server = actions.browser_mcp(state, ("agent-browser",))["agent_browser"]
                self.assertEqual((server["command"], server["args"]), (str(exe), ["mcp", "--tools", "core"]))
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            self.fake_build(root, "0.0.1")  # another version: the pinned npx copy instead
            with mock.patch.dict("os.environ", {agent_browser.SOURCE_ENV: root}), \
                    mock.patch("shutil.which", return_value="/usr/bin/npx"):
                self.assertIsNone(agent_browser.source_build())
                server = actions.browser_mcp(state, ("agent-browser",))["agent_browser"]
                self.assertEqual(server["command"], "/usr/bin/npx")
                self.assertIn(actions.AGENT_BROWSER, server["args"])

    @unittest.skipUnless(Path("/proc/self/exe").exists(), "needs /proc")
    def test_a_daemon_older_than_its_binary_is_stale(self):
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        time.sleep(0.2)
        exe = str(Path(f"/proc/{child.pid}/exe").resolve())
        with mock.patch.object(Path, "stat", lambda self, **k: type("S", (), {"st_mtime": time.time() + 10})()):
            self.assertIn(child.pid, agent_browser._stale_daemons(exe))
        self.assertNotIn(child.pid, agent_browser._stale_daemons(exe))  # not rebuilt since

    def test_browse_travels_to_other_machines(self):
        from jarvis import remote
        self.assertIn("browse", remote.ACTIONS)
        self.assertEqual(remote.ARG["browse"], "spec")

    def test_console_buttons_are_a_closed_list(self):
        from jarvis.ui import Console
        console = Console.__new__(Console)
        console.state = Path(".")
        with mock.patch.object(agent_browser, "browse", return_value=(True, "ok")) as browse:
            self.assertTrue(console.agent_screen_command({"action": "stop"})["ok"])
            browse.assert_called_with({"action": "scroll", "mode": "stop"}, Path("."))
            with self.assertRaises(ValueError):
                console.agent_screen_command({"action": "open"})


if __name__ == "__main__":
    unittest.main()


class AuditModelsTests(unittest.TestCase):
    def test_starting_a_second_auto_scroll_stops_the_first(self):
        agent_browser.stop_scrolling()
        started = []
        with mock.patch.object(agent_browser, "running", return_value=True), \
                mock.patch.object(agent_browser.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: started.append(1)
            spec = {"action": "scroll", "mode": "auto", "direction": "down"}
            agent_browser.browse(spec)
            first = agent_browser._SCROLLER["stop"]
            agent_browser.browse(spec)
            second = agent_browser._SCROLLER["stop"]
        agent_browser.stop_scrolling()
        self.assertIsNot(first, second)
        self.assertTrue(first.is_set())

    def test_frames_do_not_respawn_the_checks_on_every_poll(self):
        agent_browser._FRAME_CACHE.clear()
        calls = []

        def fake(*args, **_):
            calls.append(args[0])
            if args[0] == "screenshot":
                Path(args[1]).write_bytes(b"jpg")
                return True, {}
            return True, {args[1]: "x"}
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(agent_browser, "running", return_value=True) as running, \
                mock.patch.object(agent_browser, "run", side_effect=fake):
            agent_browser.frame(state)
            agent_browser.frame(state)
        agent_browser._FRAME_CACHE.clear()
        self.assertEqual(running.call_count, 1)
        self.assertEqual(calls.count("get"), 2)  # url + title once, not twice
        self.assertEqual(calls.count("screenshot"), 2)
