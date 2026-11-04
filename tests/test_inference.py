"""The inference layer (2026-11-01): ChatGPT first, Claude while ChatGPT's plan is over
its limit, for every part of JARVIS; and a Claude brain with the same tools and gates."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import inference
from jarvis.inference import Failover

LIMIT = RuntimeError("ChatGPT error: You've hit your usage limit. Try again at 11:34 PM.")


class Lane:
    def __init__(self, reply="ok", error=None, speak=None, tools=()):
        self.reply, self.error, self.speak, self.asked = reply, error, speak, []
        self.tool_log, self.closed, self.on_tool, self._tools = [], False, None, list(tools)

    def ask(self, text, recall=(), on_sentence=None):
        self.asked.append(text)
        self.tool_log = list(self._tools)
        if self.speak and on_sentence:
            on_sentence(self.speak)
        if self.error:
            raise self.error
        return self.reply

    def pick(self, text):
        return "chat"

    def warm(self):
        pass

    def close(self):
        self.closed = True


def limited(on=True):
    return mock.patch("jarvis.chatgpt.usage_limit", return_value="until 11:34 PM" if on else "")


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.made = []

    def failover(self, primary):
        def make():
            lane = Lane("Claude answered.")
            self.made.append(lane)
            return lane
        return Failover(primary, make)

    def test_chatgpt_answers_while_it_can(self):
        lane = self.failover(Lane("ChatGPT answered."))
        with limited(False):
            self.assertEqual(lane.ask("hi"), "ChatGPT answered.")
        self.assertEqual(self.made, [])  # no Claude process started for nothing

    def test_claude_answers_while_chatgpt_is_limited_and_has_the_tools(self):
        primary = Lane("never")
        lane = self.failover(primary)
        with limited():
            self.assertEqual(lane.pick("open spotify"), "act")
            self.assertEqual(lane.ask("open spotify"), "Claude answered.")
        self.assertEqual(primary.asked, [])

    def test_a_limit_hit_mid_call_before_speaking_fails_over(self):
        lane = self.failover(Lane(error=LIMIT))
        with limited(False):
            self.assertEqual(lane.ask("hi"), "Claude answered.")

    def test_a_limit_after_speaking_or_after_tools_ran_is_raised(self):
        for primary in (Lane(error=LIMIT, speak="Sure."), Lane(error=LIMIT, tools=[("open_app", True, "ok")])):
            lane = self.failover(primary)
            with limited(False), self.assertRaises(RuntimeError):
                lane.ask("hi", on_sentence=lambda s: None)
        self.assertEqual(self.made, [])  # never half an answer twice, never a repeated action

    def test_other_errors_are_raised(self):
        lane = self.failover(Lane(error=RuntimeError("ChatGPT turn failed: boom")))
        with limited(False), self.assertRaises(RuntimeError):
            lane.ask("hi")

    def test_the_stand_in_closes_once_chatgpt_is_back(self):
        primary = Lane("ChatGPT answered.")
        lane = self.failover(primary)
        with limited():
            lane.ask("hi")
        with limited(False):
            lane.ask("hi again")
        self.assertTrue(self.made[0].closed)
        self.assertIsNone(lane.fallback)

    def test_a_busy_lane_is_closed_only_after_its_ask_returns(self):
        import threading
        started, release = threading.Event(), threading.Event()

        class Slow(Lane):
            def ask(self, *a, **k):
                started.set()
                release.wait(5)
                return "slow"
        primary = Slow()
        lane = self.failover(primary)
        with limited(False):
            t = threading.Thread(target=lambda: lane.ask("first"))
            t.start()
            started.wait(5)
            with limited():
                lane.ask("second")  # the stand-in needs the profile: primary is mid-turn
            self.assertFalse(primary.closed)
            release.set()
            t.join(5)
        self.assertTrue(primary.closed)

    def test_an_idle_primary_is_closed_for_the_stand_in(self):
        primary = Lane()
        lane = self.failover(primary)
        with limited():
            lane.ask("hi")
        self.assertTrue(primary.closed)

    def test_a_busy_standin_is_not_closed_under_its_ask(self):
        import threading
        started, release = threading.Event(), threading.Event()
        standin = Lane("c")
        orig = standin.ask

        def slow(*a, **k):
            started.set()
            release.wait(5)
            return orig(*a, **k)
        standin.ask = slow
        lane = Failover(Lane("p"), lambda: standin)
        with limited():
            t = threading.Thread(target=lambda: lane.ask("x"))
            t.start()
            started.wait(5)
        with limited(False):
            lane.ask("y")  # ChatGPT is back and retires the busy stand-in
        self.assertFalse(standin.closed)
        release.set()
        t.join(5)
        self.assertTrue(standin.closed)

    def test_the_stand_in_is_retired_after_chatgpt_answers(self):  # was: before (race audit bug 2)
        order = []
        primary = Lane("ChatGPT answered.")
        lane = self.failover(primary)
        with limited():
            lane.ask("hi")
        standin = self.made[0]
        standin.close = lambda: order.append("closed")
        orig = primary.ask
        primary.ask = lambda *a, **k: (order.append("asked"), orig(*a, **k))[1]
        with limited(False):
            lane.ask("again")
        self.assertEqual(order, ["asked", "closed"])

    def test_tool_log_comes_from_the_exception_when_the_lane_was_reset(self):
        err = RuntimeError(str(LIMIT))
        err.tool_log = [("open_app", True, "ok")]
        lane = self.failover(Lane(error=err))  # lane.tool_log is reset by another request
        lane.primary.ask = mock.Mock(side_effect=err)
        lane.primary.tool_log = []
        with limited(False), self.assertRaises(RuntimeError):
            lane.ask("hi")
        self.assertEqual(self.made, [])

    def test_a_limit_after_output_is_marked_spoken(self):
        lane = self.failover(Lane(error=LIMIT, speak="Sure."))
        with limited(False):
            with self.assertRaises(RuntimeError) as ctx:
                lane.ask("hi", on_sentence=lambda s: None)
        self.assertTrue(ctx.exception.spoken)

    def test_tool_reports_follow_the_lane_that_answered(self):
        seen = []
        lane = self.failover(Lane())
        lane.on_tool = lambda *a: seen.append(a)
        with limited():
            lane.ask("hi")
        self.assertIs(self.made[0].on_tool, lane.on_tool)
        self.assertIs(lane.last, self.made[0])


class ClaudeBrainTests(unittest.TestCase):
    def test_same_tools_over_mcp_on_the_tools_tier(self):
        with tempfile.TemporaryDirectory() as state:
            config = {"actions": {"enabled": False}, "devices": {}}
            lane = inference.claude_brain(config, state, Path(state))
            args = lane._args("claude")
        self.assertEqual(lane.model, "sonnet")  # tools tier: mid
        self.assertIn("mcp__jarvis", args[args.index("--allowedTools") + 1])
        servers = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
        self.assertEqual(servers["jarvis"]["args"][-1], "brain-tools")
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("standing in", args[args.index("--append-system-prompt") + 1])

    def test_roles_can_be_retuned_in_config(self):
        self.assertEqual(inference.role({"inference": {"roles": {"talk": {"tier": "high"}}}}, "talk")["tier"], "high")
        with self.assertRaises(KeyError):
            inference.role({}, "nonsense")


class AskTests(unittest.TestCase):
    def test_stateless_questions_fail_over_too(self):
        asked = []

        class Claude(Lane):
            def __init__(self, *a, **k):
                super().__init__("{}")

            def ask(self, text, *a, **k):
                asked.append(text)
                return "{}"
        with limited(), mock.patch("jarvis.fastlane.FastLane", Claude):
            self.assertEqual(inference.ask({}, ".", "consolidate", "summarize", instructions="RULES"), "{}")
        self.assertTrue(asked[0].startswith("RULES"))


class ToolsServerTests(unittest.TestCase):
    def test_lists_and_calls_through_the_gate(self):
        from jarvis.chatgpt import ConfirmGate
        from jarvis.tools_mcp import serve

        class Tools:
            def specs(self):
                return [{"type": "function", "name": "terminal", "description": "run",
                         "inputSchema": {"type": "object"}}]

            def call(self, name, args):
                return True, "ran"
        gate = ConfirmGate(Tools(), {"rules": {"confirm": "never"}}, tempfile.mkdtemp(), here=lambda: "zenbook")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "terminal", "arguments": {"command": "cat ~/.codex/auth.json"}}}]
        out = io.StringIO()
        serve({}, ".", ".", stdin=io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"),
              stdout=out, toolbox=gate)
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "jarvis-brain-tools")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]], ["terminal"])
        self.assertTrue(replies[2]["result"]["isError"])  # hard_deny holds over MCP too
        self.assertIn("Refused", replies[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()


class BrainToolsLimitTests(unittest.TestCase):
    def test_mcp_config_hands_the_chatgpt_limit_to_the_subprocess(self):
        import time
        from jarvis import chatgpt, tools_mcp
        saved = dict(chatgpt._LIMIT)
        self.addCleanup(chatgpt._LIMIT.update, saved)
        chatgpt._LIMIT.update(until=0.0, text="")
        env = tools_mcp.mcp_config("c.json", "state")["mcpServers"]["jarvis"]["env"]
        self.assertNotIn(chatgpt.LIMIT_ENV, env)
        chatgpt._LIMIT.update(until=time.time() + 600, text="until soon")
        env = tools_mcp.mcp_config("c.json", "state")["mcpServers"]["jarvis"]["env"]
        self.assertGreater(float(env[chatgpt.LIMIT_ENV]), time.time())
