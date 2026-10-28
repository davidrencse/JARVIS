"""Token and latency savings: lean Codex prompt, interpreter replies, recall, trusted rules."""

import tempfile
import unittest
from pathlib import Path

from jarvis import gates
from jarvis.chatgpt import ChatGPTLane, lean_prompt
from jarvis.interpret import parse_reply
from jarvis.memory import Memory
from jarvis.pipeline import fast_path

DEVICES = {"dell": {"hostname": "d", "aliases": []}, "zenbook": {"hostname": "z", "aliases": []}}


class FakeRemote:
    def __init__(self):
        self.calls = []

    def here(self):
        return "zenbook"

    def call(self, device, action, arg):
        self.calls.append((device, action))
        if action == "observe":
            return True, '{"match": true, "after_ms": 5}'
        return True, f"did {action}."


class LeanPromptTests(unittest.TestCase):
    def test_lanes_get_the_lean_prompt_unless_turned_off(self):
        lane = ChatGPTLane({}, ".", overrides=['web_search="disabled"'])
        self.assertTrue(any(o.startswith("model_instructions_file=") for o in lane.overrides))
        self.assertEqual(lane.overrides[-1], 'web_search="disabled"')
        full = ChatGPTLane({"models": {"lean_prompt": False}}, ".")
        self.assertEqual(full.overrides, ())
        self.assertIn("include_environment_context=false", lean_prompt())


class InterpreterReplyTests(unittest.TestCase):
    def test_mode_as_key_is_read(self):
        got = parse_reply('{"act":{"commands":[{"intent":"open_app"}]}}')
        self.assertEqual(got["mode"], "act")
        self.assertEqual(got["commands"], [{"intent": "open_app"}])
        self.assertEqual(parse_reply('{"converse":{}}'), {"mode": "converse"})
        self.assertIsNone(parse_reply('{"act":{}, "x":1}'))
        got = parse_reply('{"act":[{"intent":"open_app","device":"zenbook","args":{"name":"calculator"}}]}')
        self.assertEqual(got["commands"][0]["args"], {"name": "calculator"})


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.mem = Memory(Path(tempfile.mkdtemp()))

    def test_relevant_lines_only_without_repeats(self):
        add = self.mem.add
        add("message", "what is the capital of australia")
        add("reply", "The capital of Australia is Canberra.")
        add("reply", "The capital of France is Paris.")
        add("reply", "The capital of Mongolia is Ulaanbaatar.")
        lines = self.mem.context("what is the capital of australia")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("Canberra", lines[0])
        self.assertNotIn(" — ", lines[0], "title and preview are the same text")

    def test_small_talk_recalls_nothing(self):
        self.mem.add("reply", "hello David")
        self.assertEqual(self.mem.context("hi"), [])


class TrustedLocalTests(unittest.TestCase):
    def setUp(self):
        from jarvis import pipeline
        pipeline.INFLIGHT = gates.InFlight()  # each test is its own request stream

    def run_it(self, text, config=None):
        remote = FakeRemote()
        looked = []
        config = {"devices": DEVICES, **(config or {})}
        result = fast_path(text, config, tempfile.mkdtemp(), origin="zenbook", remote=remote,
                           history=False, app_exists=lambda d, n: looked.append(n) or True)
        return result, remote.calls, looked

    def test_local_open_skips_lookup_and_screen_check(self):
        result, calls, looked = self.run_it("open chrome")
        self.assertEqual(calls, [("zenbook", "open_app")])
        self.assertEqual(looked, [])
        self.assertTrue(result["outcomes"][0]["ok"])

    def test_other_pc_keeps_every_check(self):
        _, calls, looked = self.run_it("open chrome on dell")
        self.assertEqual([a for _, a in calls], ["open_app", "observe"])
        self.assertEqual(looked, ["Chrome"])

    def test_a_step_something_waits_on_keeps_its_screen_check(self):
        _, calls, _ = self.run_it("open spotify and play")
        self.assertEqual([a for _, a in calls], ["open_app", "observe", "media"])

    def test_rule_can_be_turned_off(self):
        _, calls, looked = self.run_it("open chrome",
                                       {"rules": {"trusted_local": {"enabled": False}}})
        self.assertEqual([a for _, a in calls], ["open_app", "observe"])

    def test_agents_and_risky_intents_never_skip(self):
        rule = gates.trusted_rules({})
        cmd = {"id": "c1", "intent": "open_app", "device": "zenbook", "args": {"name": "x"}}
        self.assertTrue(gates.skips(cmd, "zenbook", "owner", rule))
        self.assertFalse(gates.skips(cmd, "zenbook", "agent", rule))
        close = {**cmd, "intent": "close_app", "args": {"name": "Word"}}
        self.assertFalse(gates.skips(close, "zenbook", "owner", {**rule, "intents": ["close_app"]}))


if __name__ == "__main__":
    unittest.main()
