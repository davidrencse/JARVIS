"""A Grok handoff that needs no logged-in browser falls back to the brain; the router keeps
public-web work away from Grok. No live model or Grok window."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.assistant import ask, decide
from jarvis.grok_preflight import CHECKS, GrokNotNeeded, PreflightError, assess
from jarvis.grokbot import Grokbot
from jarvis.memory import Memory
from jarvis.router import grok_first
from jarvis.store import Store

GROK = {"personal_agents": {"grokbot": {"integration": "window_automation"}}}


def review(**off):
    return json.dumps({"reason": "public page", "checks": {n: n not in off for n in CHECKS}})


def infer_with(review_text):
    answers = iter([json.dumps({"decision": "forward", "task": "Summarize the page",
                                "reply": "", "reason": "x"}), review_text])
    return lambda _prompt: next(answers)


class PreflightFallbackTests(unittest.TestCase):
    def test_only_grok_needed_failing_is_not_needed(self):
        with self.assertRaises(GrokNotNeeded) as caught:
            assess("summarize https://example.com", {}, ".", infer_with(review(grok_needed=True)))
        self.assertIsInstance(caught.exception, PreflightError)
        self.assertIn("grok_needed", str(caught.exception))

    def test_any_other_failure_still_fails_closed(self):
        for name in CHECKS:
            if name == "grok_needed":
                continue
            with self.subTest(check=name), self.assertRaises(PreflightError) as caught:
                assess("x", {}, ".", infer_with(review(**{name: True})))
            self.assertNotIsInstance(caught.exception, GrokNotNeeded)

    def test_grok_needed_plus_another_failure_fails_closed(self):
        with self.assertRaises(PreflightError) as caught:
            assess("x", {}, ".", infer_with(review(grok_needed=True, task_faithful=True)))
        self.assertNotIsInstance(caught.exception, GrokNotNeeded)


class FakeLane:
    def __init__(self):
        self.asked = []

    def ask(self, text, recall, on_sentence=None):
        self.asked.append(text)
        return "It is an example domain."


class AssistantFallbackTests(unittest.TestCase):
    def run_ask(self, text, assessed):
        lane, steps = FakeLane(), []
        with tempfile.TemporaryDirectory() as folder, \
                patch("jarvis.assistant.grok_first", return_value=True), \
                patch("jarvis.assistant._orchestrated", return_value=None), \
                patch("jarvis.grok_preflight.assess", **assessed), \
                patch.object(Grokbot, "task") as task:
            base = Path(folder)
            memory = Memory(base)
            result = ask(text, Store(base), memory, GROK, base, lane=lane,
                         on_step=lambda *a: steps.append(a))
            self.inbound = [m for m in memory.search(text, limit=20) if m.get("kind") == "message"]
        return result, lane, task, steps

    def test_grok_not_needed_reruns_on_the_brain(self):
        result, lane, task, steps = self.run_ask(
            "summarize https://example.com",
            {"side_effect": GrokNotNeeded("Grok preflight blocked grok_needed.")})
        self.assertEqual(lane.asked, ["summarize https://example.com"])
        self.assertEqual(result["reply"], "It is an example domain.")
        self.assertEqual(result["tier"], "simple")
        self.assertIn("grok_needed", result["grok_fallback"])
        self.assertNotIn("Grok request failed", result["reply"])
        task.assert_not_called()
        self.assertEqual(len(self.inbound), 1, "the fallback remembered the message twice")

    def test_other_preflight_failure_still_reports_failed(self):
        result, lane, task, _ = self.run_ask(
            "read my email",
            {"side_effect": PreflightError("Grok preflight blocked task_faithful.")})
        self.assertEqual(lane.asked, [])
        self.assertIn("Grok request failed", result["reply"])
        self.assertEqual(result["state"], "failed")
        task.assert_not_called()


class RouterTests(unittest.TestCase):
    def test_public_web_stays_with_the_brain(self):
        for text in ("summarize https://example.com", "read www.example.com for me",
                     "look up the capital of France", "open youtube in chrome",
                     "summarize https://x.com/someone/status/1"):
            self.assertFalse(grok_first(text, GROK), text)
            self.assertEqual(decide(text, GROK, conversational=True)["tier"], "simple", text)

    def test_account_work_still_goes_to_grok(self):
        for text in ("read my latest emails", "post this on twitter",
                     "ask grok bot to check my x notifications", "check my gmail",
                     "check my messages on discord", "log in to canvas",
                     "summarize https://example.com and email it to me"):
            self.assertTrue(grok_first(text, GROK), text)


if __name__ == "__main__":
    unittest.main()
