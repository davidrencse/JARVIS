"""Latency fixes: stale-while-revalidate app list, short focus_window timeout, parser-aware route."""

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import __main__ as cli
from jarvis import actions
from jarvis.actions import DesktopTools
from jarvis.remote import Remote

DEVICES = {"dell": {"hostname": "WinLaptop", "aliases": ["windows"]},
           "zenbook": {"hostname": "archmain", "aliases": ["arch", "linux"]}}


class AppCacheTests(unittest.TestCase):
    def tools(self, builder):
        patcher = patch.multiple(actions, on_windows=lambda: False, _backend=lambda: "linux",
                                 desktop_entries=builder)
        patcher.start()
        self.addCleanup(patcher.stop)
        return DesktopTools(cache_seconds=0.01)

    def test_first_build_is_synchronous(self):
        tools = self.tools(lambda: [{"Name": "A"}])
        self.assertEqual(tools.apps(), [{"Name": "A"}])

    def test_expired_cache_returns_old_list_and_refreshes_once(self):
        gate, started, calls = threading.Event(), threading.Event(), []

        def builder():
            calls.append(1)
            if len(calls) > 1:
                started.set()
                gate.wait(5)
            return [{"Name": f"v{len(calls)}"}]

        tools = self.tools(builder)
        old = tools.apps()
        time.sleep(0.03)
        t0 = time.monotonic()
        self.assertEqual(tools.apps(), old)  # stale, immediately
        self.assertLess(time.monotonic() - t0, 1)
        self.assertTrue(started.wait(2))
        self.assertEqual(tools.apps(), old)  # refresh still running: no second thread
        self.assertEqual(len(calls), 2)
        gate.set()
        for _ in range(100):
            if tools.apps() != old:
                break
            time.sleep(0.02)
        self.assertNotEqual(tools.apps(), old)

    def test_failed_refresh_keeps_old_list_and_allows_retry(self):
        state = {"fail": False}

        def builder():
            if state["fail"]:
                raise OSError("boom")
            return [{"Name": "keep"}]

        tools = self.tools(builder)
        old = tools.apps()
        state["fail"] = True
        time.sleep(0.03)
        self.assertEqual(tools.apps(), old)
        for _ in range(100):
            if tools._refreshing.acquire(blocking=False):
                tools._refreshing.release()
                break
            time.sleep(0.02)
        self.assertEqual(tools.apps(), old)


class FocusTimeoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = {"devices": DEVICES,
                  "cluster": {"enabled": True, "local_device": "dell", "main_device": "dell"}}
        self.remote = Remote(config, tmp.name)

    def timeout_for(self, action):
        with patch("jarvis.cluster.dispatch", return_value={"ok": True, "text": "x"}) as dispatch:
            self.remote.call("zenbook", action, "chrome")
        return dispatch.call_args.kwargs.get("timeout")

    def test_focus_window_gets_a_short_timeout(self):
        self.assertEqual(self.timeout_for("focus_window"), 8)

    def test_other_actions_keep_the_default(self):
        self.assertIsNone(self.timeout_for("open_app"))


class RouteDryRunTests(unittest.TestCase):
    config = {"devices": DEVICES, "cluster": {"main_device": "dell"}}

    def test_parser_resolved_request_is_reported_as_parser(self):
        route = cli.parser_route("open spotify", self.config)
        self.assertIn("parser:", route["reasons"][0])
        self.assertEqual(route["parser"][0]["intent"], "open_app")

    def test_unresolved_request_falls_through(self):
        self.assertIsNone(cli.parser_route("why is the sky blue and what should i do about it", self.config))

    def test_dry_run_executes_nothing(self):
        with patch("jarvis.remote.run_here", side_effect=AssertionError("executed")), \
                patch("subprocess.run", side_effect=AssertionError("spawned")), \
                patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            self.assertIsNotNone(cli.parser_route("open spotify", self.config))


if __name__ == "__main__":
    unittest.main()
