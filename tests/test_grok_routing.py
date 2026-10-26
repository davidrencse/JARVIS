import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.assistant import decide
from jarvis.router import grok_first

GROK = {"personal_agents": {"grokbot": {"integration": "window_automation"}}}


class GrokRoutingTest(unittest.TestCase):
    def test_account_and_web_work_goes_to_grok(self):
        for text in ("read my email", "summarize my unread mail", "check my gmail",
                     "what's on my outlook calendar", "ask grok to find flights",
                     "check my messages on discord"):
            self.assertTrue(grok_first(text, GROK), text)
            self.assertEqual(decide(text, GROK, conversational=True)["tier"], "medium", text)

    def test_plain_talk_and_the_clock_stay_local(self):
        for text in ("tell me a joke", "what time is it", "open spotify"):
            self.assertNotEqual(decide(text, GROK, conversational=True)["tier"], "medium", text)

    def test_switches(self):
        self.assertFalse(grok_first("read my email", {}))  # Grok Bot not set up
        self.assertFalse(grok_first("read my email",
                                    {**GROK, "routing": {"orchestrator": "brain"}}))
        self.assertFalse(grok_first("read my email", {**GROK, "actions": {"grokbot": False}}))
        self.assertTrue(grok_first("tell me a joke", {**GROK, "routing": {"grok_scope": "all"}}))

    def test_explicit_tier_wins(self):
        self.assertEqual(decide("read my email", GROK, tier="simple")["tier"], "simple")


    def test_timeout_ends_the_turn_with_a_failed_step(self):
        from jarvis.assistant import ask
        from jarvis.grokbot import Grokbot
        from jarvis.memory import Memory
        from jarvis.store import Store
        steps = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(Grokbot, "task", side_effect=TimeoutError("timed out")) as task, \
                patch("jarvis.grok_preflight.assess", return_value={
                    "decision": "forward", "task": "find flights", "reply": None}) as check, \
                patch("jarvis.grokbot.threading.Thread") as thread:
            base = Path(folder)
            result = ask("ask grok to find flights", Store(base), Memory(base),
                         GROK, base, on_step=lambda *args: steps.append(args))
        check.assert_called_once()
        task.assert_called_once_with("find flights", timeout=30, prechecked=True)
        thread.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("timed out", result["reply"])
        self.assertTrue(any(s[0] == "grok-send" and s[-1] == "fail" for s in steps))


if __name__ == "__main__":
    unittest.main()
