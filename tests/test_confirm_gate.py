import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.chatgpt import ConfirmGate
from jarvis.confirm import ConfirmQueue


CONFIG = {"devices": {"dell": {}, "zenbook": {}}, "cluster": {"enabled": False}}


class FakeTools:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [{"type": "function", "name": "close_app"}]

    def call(self, name, args):
        self.calls.append((name, args))
        return True, "ran"


class ConfirmGateTests(unittest.TestCase):
    """Brain tool calls that need David's OK are parked, never run (web or vault text
    can steer the model into calling them)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.tools = FakeTools()
        self.gate = ConfirmGate(self.tools, CONFIG, self.state, here=lambda: "dell")

    def pending(self):
        return ConfirmQueue(self.state).pending()

    def test_close_app_is_parked(self):
        ok, text = self.gate.call("close_app", {"name": "Word"})
        self.assertFalse(ok)
        self.assertEqual(self.tools.calls, [])
        [item] = self.pending()
        self.assertEqual(item["command"]["intent"], "close_app")
        self.assertEqual(item["origin"], "dell")
        self.assertIn(item["id"], text)

    def test_close_app_on_other_laptop_is_parked_for_that_laptop(self):
        self.gate.call("close_app", {"name": "Word", "device": "zenbook"})
        [item] = self.pending()
        self.assertEqual(item["command"]["device"], "zenbook")
        self.assertEqual(item["origin"], "dell")

    def test_safe_to_close_runs(self):
        self.assertEqual(self.gate.call("close_app", {"name": "Spotify"}), (True, "ran"))
        self.assertEqual(self.pending(), [])

    def test_cross_device_open_url_is_parked(self):
        ok, _ = self.gate.call("open_url", {"url": "https://example.com", "device": "zenbook"})
        self.assertFalse(ok)
        self.assertEqual(self.tools.calls, [])
        [item] = self.pending()
        self.assertEqual(item["command"]["args"], {"url": "https://example.com"})

    def test_local_open_url_runs(self):
        for args in ({"url": "https://example.com"},
                     {"url": "https://example.com", "device": "dell"}):
            self.assertEqual(self.gate.call("open_url", args), (True, "ran"))
        self.assertEqual(self.pending(), [])

    def test_grok_agent_is_parked_as_agent_task(self):
        ok, _ = self.gate.call("grok_agent", {"task": "summarise my inbox"})
        self.assertFalse(ok)
        self.assertEqual(self.tools.calls, [])
        [item] = self.pending()
        self.assertEqual(item["command"]["intent"], "agent_task")
        self.assertEqual(item["command"]["args"], {"text": "summarise my inbox"})

    def test_grok_agent_with_credentials_is_refused_not_parked(self):
        ok, text = self.gate.call("grok_agent", {"task": "type my password into the bank"})
        self.assertFalse(ok)
        self.assertIn("Refused", text)
        self.assertEqual(self.pending(), [])

    def test_unknown_device_is_not_parked(self):
        ok, _ = self.gate.call("close_app", {"name": "Word", "device": "mars"})
        self.assertFalse(ok)
        self.assertEqual((self.tools.calls, self.pending()), ([], []))

    def test_other_tools_pass_through(self):
        self.assertEqual(self.gate.call("open_app", {"name": "Word", "device": "zenbook"}),
                         (True, "ran"))
        self.assertEqual(self.gate.specs(), self.tools.specs())


class ConfirmedAgentOutcomeTests(unittest.TestCase):
    """An approved Grok task that never reached Grok certainly did nothing: it is a
    pre_effect_failure, not "outcome unknown". Only a failure mid-request is uncertain."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        gate = ConfirmGate(FakeTools(), CONFIG, self.state, here=lambda: "dell")
        gate.call("grok_agent", {"task": "summarise my inbox"})
        [self.item] = ConfirmQueue(self.state).pending()

    def approve(self, available=True, task=None):
        from jarvis.assistant import confirm_pending
        with mock.patch("jarvis.assistant.confirmation_origin", return_value="dell"), \
                mock.patch("jarvis.router.grok_available", return_value=available), \
                mock.patch("jarvis.grokbot.Grokbot.task", side_effect=task) as sent:
            done = confirm_pending(CONFIG, self.state, self.item["id"], "approve")
        return done["outcomes"][0], sent

    def test_grok_unavailable_is_pre_effect(self):
        outcome, sent = self.approve(available=False)
        self.assertEqual((outcome["state"], outcome["ok"]), ("pre_effect_failure", False))
        sent.assert_not_called()

    def test_unreachable_tunnel_is_pre_effect(self):
        outcome, _ = self.approve(task=ConnectionError("The tunnel from dell is down"))
        self.assertEqual(outcome["state"], "pre_effect_failure")

    def test_timeout_after_sending_stays_uncertain(self):
        outcome, _ = self.approve(task=TimeoutError("Grok request timed out"))
        self.assertEqual(outcome["state"], "uncertain")


if __name__ == "__main__":
    unittest.main()


class FullPermissionTests(unittest.TestCase):
    """rules.confirm: "never" (David, 2026-11-01): nothing waits for his OK, but the
    refusals stay (credentials, agent-origin actions)."""

    NEVER = {**CONFIG, "rules": {"confirm": "never"}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_default_still_asks(self):
        from jarvis.gates import asks
        self.assertTrue(asks({}))
        self.assertTrue(asks(CONFIG))
        self.assertFalse(asks(self.NEVER))

    def test_brain_tools_run_without_parking(self):
        tools = FakeTools()
        gate = ConfirmGate(tools, self.NEVER, self.state, here=lambda: "dell")
        self.assertEqual(gate.call("close_app", {"name": "Word"}), (True, "ran"))
        self.assertEqual(gate.call("grok_agent", {"task": "post my photo"}), (True, "ran"))
        self.assertEqual(ConfirmQueue(self.state).pending(), [])

    def test_credentials_are_still_refused(self):
        tools = FakeTools()
        gate = ConfirmGate(tools, self.NEVER, self.state, here=lambda: "dell")
        ok, text = gate.call("grok_agent", {"task": "type my password into the bank"})
        self.assertFalse(ok)
        self.assertEqual(tools.calls, [])

    def test_gates_pass_confirm_commands(self):
        from jarvis import gates
        command = {"id": "c1", "intent": "close_app", "device": "dell",
                   "args": {"name": "Word"}, "depends_on": []}
        self.assertFalse(gates.check(command, CONFIG["devices"])["pass"])
        self.assertTrue(gates.trusted_rules(self.NEVER)["never_ask"])
        self.assertFalse(gates.trusted_rules(CONFIG)["never_ask"])

    def test_browser_commit_needs_no_ok(self):
        from jarvis import browser_gate
        approve = browser_gate.gate(lambda p: (True, "policy ok"), self.NEVER, self.state, wait=0)
        with mock.patch.object(browser_gate, "commits", return_value="click Send"):
            self.assertEqual(approve({"tool": "click", "label": "Send"}), (True, "policy ok"))
        self.assertEqual(ConfirmQueue(self.state).pending(), [])
