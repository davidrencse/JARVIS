"""Audit 4 brain/failover fixes. Fakes only: nothing starts codex or claude."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import assistant, brain, fastlane, inference, runner
from jarvis.history import History
from jarvis.inference import Failover, is_limit

from tests.intelligence.test_brain import Stub, plan_json, S
from tests.runtime.test_history import cmd, out
from tests.intelligence.test_inference_races import Lane, LIMIT, limited


class FailoverReplyText(unittest.TestCase):
    def test_answer_mentioning_authentication_passes_through(self):
        text = "Two-factor authentication adds a second step when you sign in."
        self.assertEqual(assistant._failover_reply(text, {}, "claude"), text)
        self.assertEqual(assistant._failover_reply(text, {"state": "finished"}, "claude"), text)

    def test_real_error_still_maps(self):
        got = assistant._failover_reply("", {"lane_error": "Not logged in - please run /login"},
                                        "claude")
        self.assertIn("Claude isn't signed in", got)

    def test_long_finished_answer_with_error_words_is_not_an_error(self):
        text = "Not logged in is what the page said. " + "x" * 300
        self.assertEqual(assistant._failover_reply(text, {"state": "finished"}, "claude"), text)


class StandinLifecycle(unittest.TestCase):
    def tearDown(self):
        assistant._STANDIN.update(lane=None, users=0, retire=False)

    def test_done_during_ask_defers_close_until_ask_returns(self):
        gate, started = threading.Event(), threading.Event()
        closed_in_ask = []

        class L:
            closed = False

            def ask(self, text, recall, on_sentence=None):
                started.set()
                gate.wait(2)
                closed_in_ask.append(self.closed)
                return "ok"

            def close(self):
                self.closed = True
        lane = L()
        with mock.patch.object(assistant, "_fastlane", return_value=lane):
            got = []
            t = threading.Thread(target=lambda: got.append(assistant._standin_reply(
                "hi", [], {}, ".", 5, None, lambda *a, **k: None, {})))
            t.start()
            started.wait(2)
            assistant._standin_done()  # ChatGPT answered another request meanwhile
            self.assertFalse(lane.closed)
            gate.set()
            t.join(3)
        self.assertEqual(got, ["ok"])
        self.assertEqual(closed_in_ask, [False])
        self.assertTrue(lane.closed)  # closed once the last user left
        self.assertIsNone(assistant._STANDIN["lane"])


class ClaudeLimit(unittest.TestCase):
    def setUp(self):
        fastlane._CLAUDE_LIMIT.update(until=0.0, text="")
        self.addCleanup(fastlane._CLAUDE_LIMIT.update, until=0.0, text="")

    def test_error_result_text_is_kept_and_sets_cooldown(self):
        lane = fastlane.FastLane({}, ".", command=["x"])
        lane.proc = mock.Mock(poll=lambda: None)
        lane.proc.stdin = mock.Mock()
        import queue
        lane.lines = queue.Queue()
        lane.lines.put(json.dumps({"type": "result", "is_error": True, "subtype": "success",
                                   "result": "You've hit your limit - resets 3pm"}) + "\n")
        with self.assertRaises(RuntimeError) as cm:
            lane.ask("hi")
        self.assertIn("hit your limit", str(cm.exception))
        self.assertTrue(fastlane.claude_limit())

    def test_cooldown_refuses_without_spawning(self):
        fastlane.note_claude_limit(RuntimeError("Claude usage limit reached"))
        lane = fastlane.FastLane({}, ".")
        with mock.patch.object(lane, "_spawn") as spawn, self.assertRaises(RuntimeError) as cm:
            lane.ask("hi")
        spawn.assert_not_called()
        self.assertIn("usage limit", str(cm.exception))

    def test_default_cooldown_and_parse(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 4, 10, 0)
        fastlane.note_claude_limit("429 rate limit", now)
        self.assertAlmostEqual(fastlane._CLAUDE_LIMIT["until"], (now + dt.timedelta(minutes=15)).timestamp())
        fastlane.note_claude_limit("hit your limit, resets 3pm", now)
        self.assertAlmostEqual(fastlane._CLAUDE_LIMIT["until"], dt.datetime(2026, 11, 4, 15).timestamp())

    def test_is_limit_phrasings(self):
        for text in ("429 Too Many Requests", "rate limit exceeded", "You've hit your limit",
                     "usage limit reached"):
            self.assertTrue(is_limit(RuntimeError(text)), text)
        self.assertFalse(is_limit(RuntimeError("port 14290 refused")))


class ToolStepChecker(unittest.TestCase):
    def test_checker_failure_after_tools_is_not_a_failed_step(self):
        class Boom(Stub):
            def __call__(self, level, prompt, instructions=""):
                if instructions is brain.CHECKER:
                    raise RuntimeError("checker down")
                return super().__call__(level, prompt, instructions)
        stub = Boom(plan_json(S("s1", "open the file", tools=True)))
        with tempfile.TemporaryDirectory() as d:
            res = brain.orchestrate("req", {}, Path(d), d, ask=stub,
                                    run_tools=lambda p: "TOOL-OUT")
        step = res["steps"][0]
        self.assertTrue(step["ok"])
        combined = next(c[1] for c in stub.calls if c[2] == "combine")
        self.assertIn("TOOL-OUT", combined)
        self.assertIn("unchecked", combined)


class RepeatsIgnoreBlocked(unittest.TestCase):
    def test_not_sent_rows_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            h = History(d)
            c = cmd()
            for _ in range(3):
                h.record("r", c, {"ok": False, "state": "not_sent", "text": "blocked"}, origin_class="agent")
            self.assertEqual(h.repeats(c, "agent"), 0)
            h.record("r", c, out(), origin_class="agent")
            self.assertEqual(h.repeats(c, "agent"), 1)


class FailoverPerCall(unittest.TestCase):
    def test_last_is_per_thread(self):
        gate = threading.Event()
        slow = Lane("p", gate=gate, tools=[("slow_tool", True, "")])
        fo = Failover(slow, lambda: Lane("s"))
        seen = {}

        def a():
            fo.ask("x")
            seen["a"] = (fo.last, fo.tool_log)
        t = threading.Thread(target=a)
        with limited(False):  # one patch around everything: patches don't nest across threads
            t.start()
            time.sleep(0.1)
            fo.last = "other"  # another request's thread
            gate.set()
            t.join(3)
        self.assertIs(seen["a"][0], slow)
        self.assertEqual(fo.last, "other")

    def test_close_deferred_while_busy(self):
        gate = threading.Event()
        p = Lane("p", gate=gate)
        fo = Failover(p, lambda: Lane("s"))
        with limited(False):
            t = threading.Thread(target=lambda: fo.ask("x"))
            t.start()
            time.sleep(0.1)
            fo.close()
            self.assertEqual(p.closed, 0)
            gate.set()
            t.join(3)
        self.assertEqual(p.closed, 1)
        self.assertFalse(p.closed_during_ask)


class RunnerSettingSources(unittest.TestCase):
    def test_one_shot_claude_pins_project_settings_once(self):
        spec = {"provider": "claude", "objective": "do the thing", "mode": "read-only"}
        for role in ("reviewer", "assistant"):
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch("jarvis.runner.shutil.which", return_value="/usr/bin/claude"):
                plan = runner.make_plan({**spec, "role": role}, {"workspace_roots": ["."]}, Path(d).resolve())
            argv = plan["argv"]
            self.assertEqual(argv.count("--setting-sources"), 1, role)
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")


if __name__ == "__main__":
    unittest.main()
