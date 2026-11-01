"""Command history (L6): unit tests for History and its use inside the fast path."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import gates, history as hist, pipeline
from jarvis.history import History

from tests.test_pipeline import CONFIG, FakeRemote


def cmd(intent="open_app", device="dell", **args):
    return {"id": "c1", "device": device, "intent": intent,
            "args": args or {"name": "chrome"}, "depends_on": []}


def out(ok=True, state=None, text="done", **kw):
    return {"ok": ok, "state": state or ("acknowledged" if ok else "pre_effect_failure"),
            "text": text, **kw}


class HistoryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.h = History(self.dir)


class UnitTests(HistoryCase):
    def test_command_key_stable_and_distinct(self):
        a = cmd("open_url", url="x", browser="b")
        b = cmd("open_url", browser="b", url="x")
        self.assertEqual(hist.command_key(a), hist.command_key(b))
        self.assertNotEqual(hist.command_key(a),
                            hist.command_key(cmd("open_url", url="y", browser="b")))
        self.assertNotEqual(hist.command_key(cmd()), hist.command_key(cmd(device="zenbook")))
        self.assertNotEqual(hist.command_key(cmd()), hist.command_key(cmd("close_app")))

    def test_error_class(self):
        a = hist.error_class("No app matches 'Foo Bar' after 30 tries")
        b = hist.error_class('No app matches "Baz" after 7 tries')
        self.assertEqual(a, b)
        self.assertNotIn("foo", a)
        self.assertFalse(any(ch.isdigit() for ch in a))
        self.assertEqual(hist.error_class(None), "")
        self.assertEqual(len(hist.error_class("x" * 300)), 80)

    def test_record_and_hops(self):
        now = time.time()
        cid = self.h.record("r", cmd(), out(), "open chrome", hops=[
            {"name": "parse", "ms": 50}, {"name": "gates", "ms": 2},
            {"name": "run", "ms": 30, "state": "done"}], total_ms=40, now=now)
        self.assertRegex(cid, r"^cmd:\d{8}:[0-9a-f]{8}$")
        rows = self.h.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["state"], rows[0]["ok"]),
                         (cid, "acknowledged", 1))
        # insertion order, not sorted by ms (50, 2, 30)
        self.assertEqual([x["name"] for x in self.h.hops(cid)], ["parse", "gates", "run"])

    def test_purge_old(self):
        now = time.time()
        old = self.h.record("r", cmd(), out(), hops=[{"name": "parse", "ms": 1}],
                            now=now - (hist.KEEP_DAYS + 1) * 86400)
        self.assertEqual(len(self.h.recent()), 1)
        self.h.record("r", cmd(), out(), now=now)
        self.assertNotIn(old, [r["id"] for r in self.h.recent()])
        self.assertEqual(self.h.hops(old), [])

    def test_recent_order_and_limit(self):
        for i in range(5):
            self.h.record(f"r{i}", cmd(), out(), now=1_000_000 + i)
        self.assertEqual([r["request"] for r in self.h.recent(3)], ["r4", "r3", "r2"])
        self.assertEqual(len(self.h.recent(0)), 1)       # clamped up to 1
        self.assertEqual(len(self.h.recent(10_000)), 5)  # clamped down to 500


class H1Tests(HistoryCase):
    NOW = 2_000_000.0

    def test_failure_in_window(self):
        self.h.record("r", cmd(), out(False, text="boom"), now=self.NOW - 120)
        got = self.h.recent_failure(cmd(), now=self.NOW)
        self.assertEqual(got["ago"], 120)
        self.assertEqual(got["state"], "pre_effect_failure")

    def test_success_after_hides(self):
        self.h.record("r", cmd(), out(False), now=self.NOW - 120)
        self.h.record("r", cmd(), out(True), now=self.NOW - 60)
        self.assertIsNone(self.h.recent_failure(cmd(), now=self.NOW))

    def test_unconfirmed_is_not_failure(self):
        self.h.record("r", cmd(), out(True, "unconfirmed"), now=self.NOW - 60)
        self.assertIsNone(self.h.recent_failure(cmd(), now=self.NOW))

    def test_not_sent_ignored(self):
        self.h.record("r", cmd(), out(False, "not_sent"), now=self.NOW - 30)
        self.assertIsNone(self.h.recent_failure(cmd(), now=self.NOW))
        self.h.record("r", cmd(), out(False), now=self.NOW - 90)
        self.h.record("r", cmd(), out(False, "not_sent"), now=self.NOW - 30)
        self.assertEqual(self.h.recent_failure(cmd(), now=self.NOW)["ago"], 90)

    def test_outside_window(self):
        self.h.record("r", cmd(), out(False), now=self.NOW - hist.H1_WINDOW - 5)
        self.assertIsNone(self.h.recent_failure(cmd(), now=self.NOW))


class CheckTests(HistoryCase):
    NOW = 3_000_000.0

    def fill(self, n, origin_class, **kw):
        for i in range(n):
            self.h.record("r", cmd(), out(True), now=self.NOW - 10 * (i + 1),
                          origin_class=origin_class, **kw)

    def test_owner_warned_not_blocked(self):
        self.h.record("r", cmd(), out(False, text="no such app"), now=self.NOW - 180)
        got = self.h.check(cmd(), "owner", now=self.NOW)
        self.assertFalse(got["block"])
        self.assertEqual(got["warn"], "That failed 3 min ago (no such app); trying again.")

    def test_agent_blocked_on_failure(self):
        self.h.record("r", cmd(), out(False), now=self.NOW - 60, origin_class="agent")
        got = self.h.check(cmd(), "agent", now=self.NOW)
        self.assertTrue(got["block"])
        self.assertIsNone(got["warn"])
        self.assertFalse(got["alert"])

    def test_agent_loop_blocks_and_alerts(self):
        self.fill(hist.H3_REPEATS, "agent")
        got = self.h.check(cmd(), "agent", now=self.NOW)
        self.assertTrue(got["block"] and got["alert"])
        self.assertFalse(next(c for c in got["checks"] if c["name"].startswith("H3"))["pass"])

    def test_agent_below_threshold_ok(self):
        self.fill(hist.H3_REPEATS - 1, "agent")
        got = self.h.check(cmd(), "agent", now=self.NOW)
        self.assertFalse(got["block"] or got["alert"])

    def test_agent_repeats_outside_window_ignored(self):
        for i in range(hist.H3_REPEATS):
            self.h.record("r", cmd(), out(True), now=self.NOW - hist.H3_WINDOW - 10 - i,
                          origin_class="agent")
        self.assertFalse(self.h.check(cmd(), "agent", now=self.NOW)["block"])

    def test_brain_loop_blocks(self):
        # Audit 2026-11-01: under rules.confirm never, the brain's own tool calls (which a
        # web page can steer) get the same loop limit as an agent's.
        self.fill(hist.H3_REPEATS, "brain")
        got = self.h.check(cmd(), "brain", now=self.NOW)
        self.assertTrue(got["block"])
        self.fill(hist.H3_REPEATS - 1, "owner")
        self.assertFalse(self.h.check(cmd(), "owner", now=self.NOW)["block"])

    def test_owner_repeats_never_block(self):
        self.fill(hist.H3_REPEATS * 3, "owner")
        got = self.h.check(cmd(), "owner", now=self.NOW)
        self.assertFalse(got["block"] or got["alert"])

    def test_repeats_counted_per_origin_class(self):
        self.fill(hist.H3_REPEATS, "owner")
        self.assertEqual(self.h.repeats(cmd(), "owner", now=self.NOW), hist.H3_REPEATS)
        self.assertEqual(self.h.repeats(cmd(), "agent", now=self.NOW), 0)
        self.assertFalse(self.h.check(cmd(), "agent", now=self.NOW)["block"])
        self.fill(hist.H3_REPEATS, "system")
        self.assertTrue(self.h.check(cmd(), "system", now=self.NOW)["block"])
        self.assertFalse(self.h.check(cmd(), "agent", now=self.NOW)["block"])


class StatsTests(HistoryCase):
    def test_empty(self):
        self.assertEqual(self.h.stats("open_app", "dell")["runs"], 0)

    def test_runs_success_median(self):
        n = 1_000.0
        self.h.record("r", cmd(), out(True, "observed"), total_ms=100, now=n)
        self.h.record("r", cmd(), out(True, "observed"), total_ms=300, now=n + 1)
        self.h.record("r", cmd(), out(True, "unconfirmed"), total_ms=200, now=n + 2)
        self.h.record("r", cmd(), out(False), total_ms=400, now=n + 3)
        self.h.record("r", cmd(), out(False, "not_sent"), total_ms=1, now=n + 4)  # excluded
        self.h.record("r", cmd(device="zenbook"), out(True), total_ms=1, now=n + 5)  # other device
        s = self.h.stats("open_app", "dell")
        self.assertEqual(s["runs"], 4)
        self.assertEqual(s["success"], 0.5)
        self.assertEqual(s["median_ms"], 300)  # sorted [100,200,300,400] -> index 2


class PipelineTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(pipeline, "INFLIGHT", gates.InFlight())
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.h = History(self.dir)

    def run_fast(self, text, remote=None, history="default", **kw):
        remote = remote or FakeRemote()
        history = self.h if history == "default" else history
        return pipeline.fast_path(text, CONFIG, Path(self.dir), remote=remote,
                                  app_exists=lambda d, n: True, history=history, **kw), remote

    def test_success_records_row_and_hops(self):
        got, _ = self.run_fast("open chrome and spotify")
        rows = self.h.recent()
        self.assertEqual(len(rows), 2)
        self.assertEqual({o["history"] for o in got["outcomes"]}, {r["id"] for r in rows})
        for row in rows:
            self.assertEqual(row["state"], "observed")
            self.assertEqual(row["origin_class"], "owner")
            self.assertEqual({h["name"] for h in self.h.hops(row["id"])},
                             {"parse", "gates", "run", "observe"})

    def test_owner_retry_after_failure_warns_but_runs(self):
        bad = FakeRemote(respond=lambda d, a, arg: (False, "app exploded"))
        first, _ = self.run_fast("open chrome", remote=bad)
        self.assertEqual(first["outcomes"][0]["state"], "pre_effect_failure")
        good = FakeRemote()
        second, _ = self.run_fast("open chrome", remote=good)
        self.assertEqual(len(good.acts), 1)
        self.assertTrue(second["reply"].startswith("That failed"), second["reply"])
        self.assertTrue(second["outcomes"][0]["ok"])

    def test_back_to_back_failure_runs_twice_and_warns_just_now(self):
        bad = FakeRemote(respond=lambda d, a, arg: (False, "app exploded"))
        self.run_fast("open chrome", remote=bad)
        second, _ = self.run_fast("open chrome", remote=bad)
        self.assertEqual(len(bad.acts), 2)
        self.assertNotEqual(second["outcomes"][0]["state"], "merged")
        self.assertTrue(second["reply"].startswith("That failed just now"), second["reply"])

    def test_agent_blocked_by_u9_before_history(self):
        got, remote = self.run_fast("open chrome", origin_class="agent")
        self.assertEqual(remote.calls, [])
        self.assertFalse(got["outcomes"][0]["ok"])
        checks = got["checks"][0]["checks"]
        self.assertTrue(any(c["name"].startswith("U9") and not c["pass"] for c in checks))
        self.assertFalse(any(c["name"].startswith("H") for c in checks))
        self.assertEqual(self.h.recent()[0]["state"], "not_sent")

    def test_h3_through_fast_path_with_non_effect_command(self):
        # The parser never emits list_apps from text, so inject one the way the brain would.
        listing = cmd("list_apps", query="chrome")
        for _ in range(hist.H3_REPEATS):
            self.h.record("r", listing, out(True), origin_class="agent")
        with mock.patch.object(pipeline, "parse_for", return_value={"commands": [listing]}):
            got, remote = self.run_fast("list apps", origin_class="agent")
        self.assertTrue(got["alert"])
        self.assertEqual(remote.calls, [])
        self.assertIn("loop", got["reply"])

    def test_history_exception_does_not_block(self):
        class Broken(History):
            def check(self, *a, **k):
                raise RuntimeError("db gone")
        got, remote = self.run_fast("open chrome", history=Broken(self.dir))
        self.assertEqual(len(remote.acts), 1)
        self.assertTrue(got["outcomes"][0]["ok"])

    def test_history_false_records_nothing(self):
        got, remote = self.run_fast("open chrome", history=False)
        self.assertEqual(len(remote.acts), 1)
        self.assertNotIn("history", got["outcomes"][0])
        self.assertEqual(self.h.recent(), [])


class RequestsTests(HistoryCase):
    def test_empty(self):
        self.assertEqual(self.h.requests(), [])

    def test_grouping_order_and_hops(self):
        a = self.h.record("ra", cmd(), out(), "open chrome", "dell", "owner",
                          [{"name": "parse", "ms": 50}, {"name": "gates", "ms": 2},
                           {"name": "run", "ms": 30, "state": "done"}], 40, now=1000)
        b = self.h.record("ra", cmd(name="spotify"), out(), "open chrome", "dell", "owner",
                          [{"name": "parse", "ms": 5}], 90, now=1001)
        self.h.record("rb", cmd("close_app"), out(), "close it", "dell", "owner", [], 10,
                      now=2000)
        got = self.h.requests()
        self.assertEqual([r["request"] for r in got], ["rb", "ra"])
        ra = got[1]
        self.assertEqual([c["id"] for c in ra["commands"]], [a, b])
        self.assertEqual([x["name"] for x in ra["commands"][0]["hops"]],
                         ["parse", "gates", "run"])
        self.assertEqual(ra["total_ms"], 90)
        self.assertEqual((ra["text"], ra["origin"]), ("open chrome", "dell"))
        self.assertEqual(ra["commands"][0]["args"], {"name": "chrome"})
        self.assertIs(ra["commands"][0]["ok"], True)

    def test_limit_clamp(self):
        for i in range(120):
            self.h.record(f"r{i}", cmd(), out(), now=1_000_000 + i)
        self.assertEqual(len(self.h.requests(0)), 1)
        self.assertEqual(len(self.h.requests(500)), 100)
        self.assertEqual(len(self.h.requests(3)), 3)
        self.assertEqual(self.h.requests(1)[0]["request"], "r119")


class ConsoleHistoryTests(HistoryCase):
    def call(self, *limit):
        from types import SimpleNamespace
        from jarvis.ui import Console
        return Console.command_history(SimpleNamespace(state=Path(self.dir)), *limit)

    def test_shape(self):
        self.h.record("r1", cmd(), out(), "open chrome", now=10)
        self.h.record("r2", cmd(), out(), "open it again", now=20)
        got = self.call()
        self.assertEqual(list(got), ["requests"])
        self.assertEqual([r["request"] for r in got["requests"]], ["r2", "r1"])
        self.assertEqual(len(self.call("1")["requests"]), 1)

    def test_non_numeric_limit(self):
        with self.assertRaises(ValueError):
            self.call("abc")

    def test_history_route_defined_once(self):
        # A dict literal with "/api/history" twice silently keeps the last one.
        import inspect
        from jarvis import ui
        src = inspect.getsource(ui.Handler.do_GET)
        self.assertEqual(src.count('"/api/history"'), 1, "duplicate route key")


if __name__ == "__main__":
    unittest.main()
