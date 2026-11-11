"""The lane's own model-capacity retry must not replay a started effect."""

import unittest
from unittest.mock import patch

from jarvis.chatgpt import ChatGPTLane
from jarvis.inference import Failover
from tests.intelligence.test_inference_started_tools import Lane, no_limit


class CapacityRetry(unittest.TestCase):
    def lane(self):
        lane = ChatGPTLane({"models": {"chatgpt": {"model": "first", "fallback_model": "second"}}}, ".")
        self.addCleanup(lane.close)
        return lane

    def run_failure(self, lane, tool, error=None):
        def fail(*args):
            tool(lane)
            raise error or RuntimeError("serverOverloaded")
        return patch.object(lane, "_ask", side_effect=fail)

    def test_started_effect_blocks_internal_capacity_retry_without_callback(self):
        lane = self.lane()
        with no_limit(), patch("jarvis.chatgpt.note_usage_limit"), \
                self.run_failure(lane, lambda x: x._tool("1", "open_app", None, "")) as ask, \
                self.assertRaises(RuntimeError) as caught:
            lane.ask("open spotify")
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(lane.model, "first")
        self.assertEqual(caught.exception.tool_unfinished, ["open_app"])
        self.assertEqual(caught.exception.tool_log, [])

    def test_safe_pretool_capacity_error_still_uses_fallback(self):
        lane = self.lane()
        with no_limit(), patch("jarvis.chatgpt.note_usage_limit"), \
                patch.object(lane, "_ask", side_effect=[RuntimeError("serverOverloaded"), "ok"]) as ask:
            self.assertEqual(lane.ask("hello"), "ok")
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(lane.model, "second")

    def test_completed_effect_blocks_retry_but_is_not_unfinished(self):
        lane = self.lane()
        def tools(x):
            x._tool("1", "open_app", None, "")
            x._tool("1", "open_app", True, "opened")
        with no_limit(), patch("jarvis.chatgpt.note_usage_limit"), self.run_failure(lane, tools) as ask, \
                self.assertRaises(RuntimeError) as caught:
            lane.ask("open spotify")
        self.assertEqual(ask.call_count, 1)
        self.assertFalse(hasattr(caught.exception, "tool_unfinished"))
        self.assertEqual(caught.exception.tool_log, [("open_app", True, "opened")])

    def test_malformed_failure_preserves_unfinished_evidence(self):
        lane = self.lane()
        with no_limit(), patch("jarvis.chatgpt.note_usage_limit"), \
                self.run_failure(lane, lambda x: x._tool("1", "open_app", None, ""), ValueError("bad")), \
                self.assertRaises(RuntimeError) as caught:
            lane.ask("open spotify")
        self.assertEqual(caught.exception.tool_unfinished, ["open_app"])

    def test_pending_state_does_not_block_next_turn(self):
        lane = self.lane()
        with no_limit(), patch("jarvis.chatgpt.note_usage_limit"), \
                self.run_failure(lane, lambda x: x._tool("1", "open_app", None, "")), \
                self.assertRaises(RuntimeError):
            lane.ask("open spotify")
        with no_limit(), patch.object(lane, "_ask", return_value="ok"):
            self.assertEqual(lane.ask("hello"), "ok")
        self.assertEqual(lane._tool_pending, {})


class CallIdentity(unittest.TestCase):
    def test_completed_then_unfinished_same_tool_keeps_the_unfinished_call(self):
        primary = Lane(error=RuntimeError("usage limit"), events=[
            ("1", "open_app", None), ("1", "open_app", True), ("2", "open_app", None)],
            finished=[("open_app", True, "opened")])
        lane = Failover(primary, lambda: self.fail("replayed"))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            lane.ask("open two apps")
        self.assertEqual(caught.exception.tool_unfinished, ["open_app"])

    def test_completed_callback_is_not_labelled_unfinished(self):
        primary = Lane(error=RuntimeError("usage limit"), events=[
            ("1", "open_app", None), ("1", "open_app", True)],
            finished=[("open_app", True, "opened")])
        lane = Failover(primary, lambda: self.fail("replayed"))
        with no_limit(), self.assertRaises(RuntimeError) as caught:
            lane.ask("open spotify")
        self.assertFalse(hasattr(caught.exception, "tool_unfinished"))
