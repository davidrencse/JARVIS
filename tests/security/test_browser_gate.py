import tempfile
import threading
import time
import unittest
from unittest import mock

from jarvis import browser_gate
from jarvis.browser_gate import commits, gate


def call(tool, server="playwright", **args):
    return {"serverName": server, "message": f'Allow the playwright MCP server to run tool "{tool}"?',
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": args}}


class CommitDetectionTest(unittest.TestCase):
    def setUp(self):
        browser_gate._TYPED.clear()
        browser_gate.PAGES.clear()
        # Search boxes count only when a snapshot shows them (not the model's own words).
        # A search box excuses Enter only on a known, non-account site (the label is the page's).
        patcher = mock.patch("jarvis.actions._local_address", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        browser_gate.observe("playwright", '- Page URL: https://search.example.org/\n'
                                           '- searchbox "Search" [ref=e11]\n- textbox "Message to Sam" [ref=e2]\n'
                                           '- link "Next page" [ref=e4]')

    def test_reading_and_moving_around_never_ask(self):
        for params in (call("browser_navigate", url="https://x.com"), call("browser_snapshot"),
                       call("browser_take_screenshot"), call("browser_click", element="Next page link", ref="e4"),
                       call("browser_type", element="Message box", ref="e9", text="post this now")):
            self.assertIsNone(commits(params), params)

    def test_unnamed_ref_is_not_vouched_for_by_the_models_words(self):
        browser_gate.observe("playwright", "- button [ref=e5]")
        found = browser_gate.judge(call("browser_click", ref="e5", element="Next page arrow"))
        self.assertIsNotNone(found)
        self.assertFalse(found["checked"])
        self.assertFalse(found["named"])

    def test_commit_clicks_ask(self):
        for label in ("Post button", "Send", "Reply", "Buy now", "Delete account", "Place order"):
            self.assertIsNotNone(commits(call("browser_click", element=label, ref="e1")), label)

    def test_enter_asks_unless_the_last_field_was_a_search_box(self):
        commits(call("browser_type", element="Search box", ref="e11", text="weather"))
        self.assertIsNone(commits(call("browser_press_key", key="Enter")))
        commits(call("browser_type", element="Message to Sam", ref="e2", text="hi"))
        self.assertIsNotNone(commits(call("browser_press_key", key="Enter")))
        self.assertIsNone(commits(call("browser_press_key", key="ArrowDown")))

    def test_type_with_submit_and_dialogs(self):
        self.assertIsNotNone(commits(call("browser_type", element="Comment", ref="e1", text="x", submit=True)))
        self.assertIsNone(commits(call("browser_type", element="Search", ref="e11", text="x", submit=True)))
        self.assertIsNotNone(commits(call("browser_handle_dialog", accept=True)))
        self.assertIsNone(commits(call("browser_handle_dialog", accept=False)))

    def test_agent_browser_tools_are_read_the_same_way(self):
        self.assertIsNotNone(commits(call("agent_browser_click", server="agent_browser",
                                          selector="button:has-text('Publish')")))


class _Clock:
    """ask() polls with time.sleep(0.5) until time.monotonic() passes the deadline.
    A clock the test owns keeps that order (pending polls, then a late approval)
    without waiting out the real half-seconds."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)


def _use_clock(clock):
    return mock.patch.object(browser_gate.time, "monotonic", clock.monotonic), \
        mock.patch.object(browser_gate.time, "sleep", clock.sleep)


class FakeQueue:
    def __init__(self, answer=None, after=0.0, clock=None):
        self.answer, self.after, self.parked, self.rejected = answer, after, None, []
        self._clock = clock or time.monotonic

    def park(self, command, origin, channel):
        self.parked, self.t0 = command, self._clock()
        return {"id": "C1"}

    def status(self, number):
        if self.answer and self._clock() - self.t0 >= self.after:
            return self.answer
        return "pending"

    def reject(self, id, origin=None):
        self.rejected.append(id)


class GateTest(unittest.TestCase):
    def approve(self, params, queue, wait=5):
        allow = lambda p: (True, "browser tool")
        with mock.patch("jarvis.assistant.confirmation_origin", return_value="dell"):
            return gate(allow, {}, "state", wait=wait, queue=queue)(params)

    def test_harmless_calls_skip_the_queue(self):
        queue = FakeQueue()
        self.assertTrue(self.approve(call("browser_snapshot"), queue)[0])
        self.assertIsNone(queue.parked)

    def test_policy_refusals_win_before_asking(self):
        queue = FakeQueue("approved")
        deny = lambda p: (False, "not allowed")
        result = gate(deny, {}, "state", queue=queue)(call("browser_click", element="Post"))
        self.assertEqual(result, (False, "not allowed"))
        self.assertIsNone(queue.parked)

    def test_waits_for_david_then_runs_or_declines(self):
        post = call("browser_click", element="Post button", ref="e3")
        clock = _Clock()
        queue = FakeQueue("approved", after=0.6, clock=clock.monotonic)
        mono, pause = _use_clock(clock)
        with mono, pause:
            self.assertTrue(self.approve(post, queue)[0])
        self.assertEqual(queue.parked["intent"], "browser_action")
        self.assertIn("Post", queue.parked["args"]["text"])
        self.assertFalse(self.approve(post, FakeQueue("rejected"))[0])

    def test_no_answer_is_a_decline_and_the_prompt_is_withdrawn(self):
        clock = _Clock()
        queue = FakeQueue(clock=clock.monotonic)
        mono, pause = _use_clock(clock)
        with mono, pause:
            ok, why = self.approve(call("browser_click", element="Send"), queue, wait=0.6)
        self.assertFalse(ok)
        self.assertEqual(queue.rejected, ["C1"])


class LateApprovalTest(unittest.TestCase):
    """audit 2026-11-04: an approval landing after the last poll was reported as a timeout."""

    def test_approval_after_the_last_poll_is_honoured(self):
        # Polls at 0 s and 0.5 s see "pending"; David approves at 0.8 s, before the loop's
        # next check at 1.0 s finds the deadline passed. Withdrawing is then a no-op.
        # The clock advances only inside ask()'s sleep, so that order stays exact.
        clock = _Clock()
        queue = FakeQueue("approved", after=0.8, clock=clock.monotonic)
        mono, pause = _use_clock(clock)
        with mono, pause, \
                mock.patch("jarvis.assistant.confirmation_origin", return_value="dell"), \
                mock.patch("jarvis.confirm.notify"):
            ok, why = browser_gate.ask("click Post", {}, "state", wait=0.8, queue=queue)
        self.assertTrue(ok, why)
        self.assertIn("approved", why)

class QueueRoundTripTest(unittest.TestCase):
    def test_console_approval_releases_the_waiting_browser(self):
        from jarvis.assistant import confirm_pending
        from jarvis.confirm import ConfirmQueue
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("jarvis.assistant.confirmation_origin", return_value="dell"), \
                mock.patch.object(browser_gate.time, "sleep",
                                  lambda seconds, _real=time.sleep: _real(min(seconds, 0.01))):
            result = {}
            worker = threading.Thread(target=lambda: result.update(
                out=browser_gate.ask("click “Post”", {}, d, wait=10)))
            worker.start()
            queue = ConfirmQueue(d)
            for _ in range(50):
                pending = queue.pending("dell")
                if pending:
                    break
                time.sleep(0.05)
            self.assertIn("let the browser click", pending[0]["summary"])
            reply = confirm_pending({}, d, pending[0]["id"], "approve")
            worker.join(5)
            self.assertEqual(reply["reply"], "Okay, go ahead.")
            self.assertTrue(result["out"][0])


if __name__ == "__main__":
    unittest.main()
