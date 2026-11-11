"""Money and messages wait for David even under rules.confirm "never" (agreed 2026-11-03)."""
import tempfile
import unittest
from unittest.mock import patch

from jarvis import browser_gate
from jarvis.gates import always_asks_text

NEVER = {"rules": {"confirm": "never"}}


def call(tool, **args):
    return {"serverName": "chrome", "message": f'Allow the chrome MCP server to run tool "{tool}"?',
            "_meta": {"tool_params": args}}


class AlwaysAskTests(unittest.TestCase):
    def run_gate(self, params, config=NEVER):
        asked = []
        with tempfile.TemporaryDirectory() as state, \
                patch.object(browser_gate, "ask", side_effect=lambda q, *a, **k: (asked.append(q), (False, "declined"))[1]):
            ok, _ = browser_gate.gate(lambda p: (True, "policy ok"), config, state)(params)
        return ok, asked

    def test_buying_and_posting_ask_even_in_full_auto(self):
        for element in ("Place order", "Buy now", "Post", "Send"):
            ok, asked = self.run_gate(call("browser_click", element=element, ref="e1"))
            self.assertTrue(asked, element)
            self.assertFalse(ok, element)  # declined in this test, so it never ran

    def test_ordinary_clicks_still_run_alone(self):
        # Only clicks checked against a page snapshot run alone (audit 2026-11-04): an
        # unseen ref is judged by the model's own words, so it asks.
        browser_gate.PAGES.clear()
        browser_gate.observe("chrome", '- link "Next page" [ref=e1]\n- button "Show more" [ref=e2]\n'
                                     '- button "Delete draft" [ref=e3]')
        for ref, element in (("e1", "Next page"), ("e2", "Show more"), ("e3", "Delete draft")):
            ok, asked = self.run_gate(call("browser_click", element=element, ref=ref))
            self.assertEqual(asked, [], element)
            self.assertTrue(ok, element)

    def test_can_be_switched_off(self):
        config = {"rules": {"confirm": "never", "always_ask": []}}
        ok, asked = self.run_gate(call("browser_click", element="Place order", ref="e1"), config)
        self.assertEqual(asked, [])
        self.assertTrue(ok)

    def test_agent_tasks(self):
        self.assertTrue(always_asks_text("buy the cheapest usb c cable on amazon", NEVER))
        self.assertTrue(always_asks_text("send alex a message saying I'm late", NEVER))
        self.assertFalse(always_asks_text("find flights to tokyo in december", NEVER))
        self.assertFalse(always_asks_text("buy milk", {"rules": {"always_ask": []}}))


if __name__ == "__main__":
    unittest.main()
