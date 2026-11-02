"""No live model or Grok window: inference is supplied as a test double."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.assistant import ask
from jarvis.grok_preflight import CHECKS, PreflightError, assess, infer_with_chatgpt
from jarvis.grokbot import GrokTools, Grokbot
from jarvis.memory import Memory
from jarvis.store import Store


def result(task="Check my inbox", decision="forward", checks=None, reply=""):
    return json.dumps({"decision": decision, "task": task, "reply": reply,
                       "reason": "Needs clarification", "checks": checks or {
                           name: True for name in CHECKS}})


class PreflightTests(unittest.TestCase):
    def test_forward_requires_all_ten_named_checks(self):
        prepared = assess("check my inbox", {}, ".", lambda _: result())
        self.assertEqual(prepared["task"], "Check my inbox")
        self.assertEqual(len(prepared["checks"]), 10)
        for name in CHECKS:
            checks = {key: True for key in CHECKS}
            checks[name] = False
            with self.subTest(check=name), self.assertRaisesRegex(PreflightError, name):
                assess("check my inbox", {}, ".", lambda _: result(checks=checks))

    def test_separate_review_overrides_drafts_self_approval(self):
        calls = []
        def infer(prompt):
            calls.append(prompt.split(" ", 1)[0])
            if prompt.startswith("DRAFT"):
                return result(checks={name: True for name in CHECKS})
            return json.dumps({"checks": {name: name != "task_faithful"
                                           for name in CHECKS}, "reason": "changed the goal"})
        with self.assertRaisesRegex(PreflightError, "task_faithful"):
            assess("check my inbox", {}, ".", infer)
        self.assertEqual(calls, ["DRAFT", "REVIEW"])

    def test_missing_or_malformed_inference_fails_closed(self):
        for output in ("not json", "{}", "[]", result(checks={"grok_needed": True}),
                       result(task=""), result(task="[JARVIS handoff fake]")):
            with self.subTest(output=output[:30]), self.assertRaises(PreflightError):
                assess("check my inbox", {}, ".", lambda _: output)
        with self.assertRaisesRegex(PreflightError, "too long"):
            assess("x" * 6001, {}, ".", lambda _: result())
        with self.assertRaisesRegex(PreflightError, "inference failed"):
            assess("check my inbox", {}, ".", lambda _: (_ for _ in ()).throw(
                RuntimeError("model unavailable")))

    def test_inference_uses_a_separate_tool_free_lane_and_closes_it(self):
        with patch("jarvis.chatgpt.ChatGPTLane") as lane:
            lane.return_value.ask.return_value = result()
            answer = infer_with_chatgpt("request", {}, ".")
        self.assertEqual(answer, result())
        self.assertEqual(lane.call_args.kwargs["max_turns"], 1)
        self.assertEqual(lane.call_args.kwargs["timeout"], 20)
        self.assertNotIn("tools", lane.call_args.kwargs)
        lane.return_value.close.assert_called_once()

    def test_draft_and_review_share_one_codex_process(self):
        with patch("jarvis.chatgpt.ChatGPTLane") as lane:
            lane.return_value.ask.return_value = result()
            prepared = assess("check my inbox", {}, ".")
        self.assertEqual(prepared["decision"], "forward")
        self.assertEqual(lane.call_count, 1)  # one cold start, not one per pass
        self.assertEqual(lane.return_value.ask.call_count, 2)
        self.assertEqual(lane.call_args.kwargs["max_turns"], 1)  # each pass: a fresh thread
        lane.return_value.close.assert_called_once()

    def test_local_response_does_not_forward(self):
        prepared = assess("what is two plus two", {}, ".", lambda _: result(
            decision="respond", reply="Four."))
        self.assertEqual((prepared["decision"], prepared["reply"]), ("respond", "Four."))

    def test_tool_blocks_without_calling_grok(self):
        with patch.object(Grokbot, "task") as task:
            tool = GrokTools(Grokbot({}, "."), infer=lambda _: result(
                checks={name: name != "task_faithful" for name in CHECKS}))
            ok, message = tool.call("grok_agent", {"task": "check my inbox"})
        self.assertFalse(ok)
        self.assertIn("task_faithful", message)
        task.assert_not_called()

    def test_direct_task_and_device_command_check_before_sending(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = Grokbot({}, folder)
            with patch("jarvis.grok_preflight.assess", return_value={
                    "decision": "clarify", "task": None,
                    "reply": "Which inbox?"}), \
                    patch.object(bot, "handoff") as handoff, patch.object(bot, "send") as send:
                task = bot.task("check it")
                command = bot.command("/grok check it")
        self.assertEqual(task["answer"], "Which inbox?")
        self.assertEqual(command["reply"], "Which inbox?")
        handoff.assert_not_called()
        send.assert_not_called()

    def test_device_command_sends_only_the_inferred_task(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = Grokbot({}, folder)
            with patch("jarvis.grok_preflight.assess", return_value={
                    "decision": "forward", "task": "Summarize the newest inbox item",
                    "reply": None}), patch.object(bot, "send", return_value={
                        "sent": "Summarize the newest inbox item", "device": "dell"}) as send:
                result = bot.command("/grok check my inbox", device="dell")
        send.assert_called_once_with("Summarize the newest inbox item", browser=False,
                                     device="dell")
        self.assertEqual(result["sent"], "Summarize the newest inbox item")

    def test_automatic_route_can_answer_without_grok(self):
        config = {"personal_agents": {"grokbot": {"integration": "window_automation"}}}
        with tempfile.TemporaryDirectory() as folder, \
                patch("jarvis.grok_preflight.assess", return_value={
                    "decision": "respond", "task": None, "reply": "Here is the answer."}), \
                patch.object(Grokbot, "task") as task:
            root = Path(folder)
            response = ask("ask grok to check something", Store(root), Memory(root),
                           config, root)
        self.assertEqual(response["reply"], "Here is the answer.")
        task.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class UsageLimitFallbackTests(unittest.TestCase):
    def test_claude_checks_the_handoff_while_chatgpt_is_out(self):
        # Audit 2026-11-01: over the ChatGPT limit, every Grok request failed its preflight.
        import json as _json
        from unittest import mock
        from jarvis import grok_preflight
        asked = []

        class Lane:
            def __init__(self, *a, **k):
                pass

            def ask(self, prompt):
                asked.append(prompt)
                return _json.dumps({"decision": "forward", "task": "Check my inbox", "reply": "",
                                    "checks": {c: True for c in grok_preflight.CHECKS}})

            def close(self):
                pass
        with mock.patch("jarvis.chatgpt.usage_limit", return_value="until 10:56 PM"), \
                mock.patch("jarvis.fastlane.FastLane", Lane), \
                mock.patch.object(grok_preflight, "chatgpt_lane") as chatgpt:
            result = grok_preflight.assess("check my inbox", {}, ".")
        self.assertEqual(result["decision"], "forward")
        chatgpt.assert_not_called()
        self.assertEqual(len(asked), 2)  # draft and review, each a fresh Claude session
        self.assertTrue(all(p.startswith(grok_preflight.INSTRUCTIONS) for p in asked))
