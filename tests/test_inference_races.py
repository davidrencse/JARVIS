"""Concurrency fixes in the brain failover (2026-11-02 race audit). Fake lanes and a fake
CLI only; nothing here starts codex or claude."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis.fastlane import FastLane
from jarvis.inference import Failover

LIMIT = "ChatGPT error: You've hit your usage limit. Try again at 11:34 PM."


class Lane:
    def __init__(self, name, error=None, tools=(), gate=None, close_delay=0):
        self.name, self.error, self._tools, self.gate = name, error, list(tools), gate
        self.close_delay, self.closed, self.on_tool, self.tool_log = close_delay, 0, None, []
        self.closed_during_ask = False
        self.in_ask = False

    def ask(self, text, recall=(), on_sentence=None):
        self.tool_log = list(self._tools)
        self.in_ask = True
        try:
            if self.gate:
                self.gate.wait(2)
            if self.error:
                raise self.error
            return self.name
        finally:
            self.in_ask = False

    def pick(self, text):
        return "chat"

    def warm(self):
        pass

    def close(self):
        if self.in_ask:
            self.closed_during_ask = True
        if self.close_delay:
            time.sleep(self.close_delay)
        self.closed += 1


def limited(on):
    return mock.patch("jarvis.chatgpt.usage_limit", return_value="until 11 PM" if on else "")


class FailoverRaces(unittest.TestCase):
    def factory(self):
        self.made = []

        def make():
            lane = Lane(f"s{len(self.made)}")
            self.made.append(lane)
            return lane
        return make

    def test_concurrent_standin_created_once(self):  # bug 1
        primary = Lane("p", close_delay=0.3)
        fo = Failover(primary, self.factory())
        got = []
        threads = [threading.Thread(target=lambda: got.append(fo._standin())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.made), 1)
        self.assertIs(got[0], got[1])

    def test_standin_survives_lapsed_limit_while_chatgpt_still_limited(self):  # bug 2
        primary = Lane("p", error=RuntimeError(LIMIT))
        fo = Failover(primary, self.factory())
        with limited(True):
            self.assertEqual(fo.ask("hi"), "s0")
        with limited(False):  # the guessed window lapsed; ChatGPT still refuses
            self.assertEqual(fo.ask("hi"), "s0")
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.made[0].closed, 0)
        primary.error = None  # ChatGPT really answers again: now the stand-in goes
        with limited(False):
            self.assertEqual(fo.ask("hi"), "p")
        self.assertEqual(self.made[0].closed, 1)
        self.assertIsNone(fo.fallback)

    def test_retire_between_select_and_enter_cannot_close_lane(self):  # bug 3
        fo = Failover(Lane("p"), self.factory())
        with limited(True):
            fo._standin()
        lane = self.made[0]
        lane.gate = threading.Event()
        threads = []

        class Racy(Failover):
            def _enter(inner, lane_):  # a retire fires right where the race window was
                if not threads:
                    t = threading.Thread(target=inner._retire_standin)
                    threads.append(t)
                    t.start()
                    t.join(0.3)
                Failover._enter(inner, lane_)
        racy = Racy(fo.primary, fo._make_fallback)
        racy.fallback = lane
        out = []
        with limited(True):
            worker = threading.Thread(target=lambda: out.append(racy.ask("hi")))
            worker.start()
            time.sleep(0.5)
            self.assertEqual(lane.closed, 0, "closed under an in-flight request")
            lane.gate.set()
            worker.join()
        threads[0].join()
        self.assertEqual(out, ["s0"])
        self.assertFalse(lane.closed_during_ask)
        self.assertEqual(lane.closed, 1)  # closed after the ask finished

    def test_slow_close_does_not_hold_guard(self):  # bug 4
        fo = Failover(Lane("p"), self.factory())
        lane = Lane("x", close_delay=0.5)
        fo._enter(lane)
        fo._close_when_idle(lane)
        t = threading.Thread(target=fo._leave, args=(lane,))
        t.start()
        time.sleep(0.1)
        start = time.monotonic()
        fo._enter(Lane("y"))
        self.assertLess(time.monotonic() - start, 0.2)
        t.join()
        self.assertEqual(lane.closed, 1)

    def test_empty_exc_tool_log_does_not_block_failover(self):  # bug 6
        exc = RuntimeError(LIMIT)
        exc.tool_log = []  # Brain.ask: nothing ran in this request
        primary = Lane("p", error=exc, tools=[("x", True, "moved by another request")])
        fo = Failover(primary, self.factory())
        with limited(False):
            self.assertEqual(fo.ask("hi"), "s0")

    def test_spoken_marked_on_non_limit_failure(self):  # bug 7
        primary = Lane("p", error=ValueError("boom"))
        primary.ask = lambda text, recall=(), on_sentence=None: (
            on_sentence("Hello."), (_ for _ in ()).throw(ValueError("boom")))
        fo = Failover(primary, self.factory())
        with limited(False):
            with self.assertRaises(ValueError) as cm:
                fo.ask("hi", on_sentence=lambda s: None)
        self.assertTrue(cm.exception.spoken)
        self.assertEqual(self.made, [])


class FastLaneClose(unittest.TestCase):
    def test_close_mid_ask_raises_runtime_error(self):  # bug 5
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "cli.py"
            script.write_text(
                "import sys, json, time\n"
                "for line in sys.stdin:\n"
                "    print(json.dumps({'type':'stream_event','event':{'delta':{"
                "'type':'text_delta','text':'Hi there. More'}}}), flush=True)\n"
                "    time.sleep(30)\n")
            lane = FastLane({}, d, command=[sys.executable, str(script)], timeout=20)
            spoke, out = threading.Event(), {}

            def run():
                try:
                    out["r"] = lane.ask("hi", on_sentence=lambda s: spoke.set())
                except BaseException as exc:
                    out["e"] = exc
            t = threading.Thread(target=run)
            t.start()
            self.assertTrue(spoke.wait(5))
            lane.close()
            t.join(10)
            self.assertIsInstance(out.get("e"), RuntimeError, out)


    def test_close_between_spawn_and_write_raises_runtime_error(self):  # bug 5
        lane = FastLane({}, ".", command=[sys.executable, "-c", "import time; time.sleep(5)"],
                        timeout=5)
        spawn = lane._spawn

        def spawn_then_closed():  # another thread closes the lane right after the spawn
            spawn()
            lane.close()
        lane._spawn = spawn_then_closed
        with self.assertRaises(RuntimeError):
            lane.ask("hi")


class HistoryRetry(unittest.TestCase):
    def test_failed_open_is_not_cached(self):  # bug 8
        from jarvis import pipeline
        calls = []

        class H:
            def __init__(self, state):
                calls.append(state)
                if len(calls) == 1:
                    raise OSError("locked")
        key = "/nonexistent-race-audit"
        pipeline._HISTORIES.pop(key, None)
        try:
            with mock.patch("jarvis.history.History", H):
                self.assertIsNone(pipeline._history(key))
                self.assertIsInstance(pipeline._history(key), H)
                self.assertIs(pipeline._history(key), pipeline._history(key))
            self.assertEqual(len(calls), 2)
        finally:
            pipeline._HISTORIES.pop(key, None)


class FailedTurnToolsTravelOnTheError(unittest.TestCase):
    """Audit 2026-11-07 F1: a ChatGPT turn that ran tools then hit the usage limit must not
    be re-asked on the Claude stand-in, even when another request moved Brain.last."""

    def setUp(self):
        from jarvis import chatgpt
        saved = dict(chatgpt._LIMIT)  # note_usage_limit records the fake limit: put it back
        self.addCleanup(lambda: (chatgpt._LIMIT.clear(), chatgpt._LIMIT.update(saved)))

    def test_limit_after_tools_ran_is_not_replayed_on_the_standin(self):
        from jarvis.chatgpt import Brain, ChatGPTLane

        class Chat:
            tool_log, on_tool = [], None

            def ask(self, *a, **k):
                return "chat"

        with tempfile.TemporaryDirectory() as d:
            act = ChatGPTLane({}, Path(d))
            brain = Brain(Chat(), act, wants=lambda t: True)

            def fake(text, recall, on_sentence):
                act.tool_log.append(("close_app", True, "closed"))
                brain.last = brain.chat  # another console request took the talk lane
                raise RuntimeError("usageLimitExceeded: You've hit your usage limit")

            standins = []

            def make():
                lane = Lane("standin")
                standins.append(lane)
                return lane
            fo = Failover(brain, make)
            with mock.patch("jarvis.chatgpt.usage_limit", return_value=""), \
                    mock.patch.object(act, "_ask", side_effect=fake):
                with self.assertRaises(RuntimeError) as ctx:
                    fo.ask("close discord")
            self.assertEqual([t[0] for t in ctx.exception.tool_log], ["close_app"])
            self.assertFalse(standins, "the stand-in must not re-run close_app")

    def test_wrapped_lane_failure_keeps_the_tools_that_ran(self):
        from jarvis.chatgpt import ChatGPTLane
        with tempfile.TemporaryDirectory() as d:
            lane = ChatGPTLane({}, Path(d))

            def fake(text, recall, on_sentence):
                lane.tool_log.append(("open_app", True, "ok"))
                raise ValueError("odd reply")
            with mock.patch("jarvis.chatgpt.usage_limit", return_value=""), \
                    mock.patch.object(lane, "_ask", side_effect=fake):
                with self.assertRaises(RuntimeError) as ctx:
                    lane.ask("open spotify")
            self.assertEqual(ctx.exception.tool_log, [("open_app", True, "ok")])

    def test_nudged_retry_failure_reports_both_turns_tools(self):
        from jarvis.chatgpt import Brain, ChatGPTLane

        class Chat:
            tool_log, on_tool = [], None
        with tempfile.TemporaryDirectory() as d:
            act = ChatGPTLane({}, Path(d))
            brain = Brain(Chat(), act, wants=lambda t: True)
            turns = []

            def fake(text, recall, on_sentence):
                turns.append(text)
                if len(turns) == 1:
                    act.tool_log.append(("list_apps", True, "ok"))
                    return "I can't do that, the tool is disabled"  # ungrounded: nudged once
                act.tool_log.append(("open_app", True, "ok"))
                raise RuntimeError("usageLimitExceeded: You've hit your usage limit")
            with mock.patch("jarvis.chatgpt.usage_limit", return_value=""), \
                    mock.patch("jarvis.chatgpt.ungrounded", side_effect=["nudge: ", None]), \
                    mock.patch.object(act, "_ask", side_effect=fake):
                act._mine.log = None
                with self.assertRaises(RuntimeError) as ctx:
                    brain.ask("open spotify")
            self.assertEqual(len(turns), 2)
            self.assertEqual([t[0] for t in ctx.exception.tool_log], ["list_apps", "open_app"])


if __name__ == "__main__":
    unittest.main()
