"""Browser confirmations David can read (2026-11-02): refs resolved from snapshots, structured
pending items, site grants, 60 s low-stakes timeout with its own outcome, desktop nudge."""

import json
import tempfile
import time
import unittest
from unittest import mock

from jarvis import browser_gate, confirm
from jarvis.browser_gate import gate, judge, observe
from jarvis.confirm import ConfirmQueue

PLAYWRIGHT = '''### Page state
- Page URL: https://www.x.com/home
- Page Title: Home / X
- Page Snapshot:
```yaml
- main [ref=e1]:
  - textbox "Password" [ref=e4]
  - textbox "Message" [ref=e5]
  - button "Post" [ref=e12]
  - link "Next page" [ref=e13]
  - button [ref=e14]
```'''
AGENT = '''- navigation
  - link "Home" [ref=e1]
- form
  - button "Save [ref=e3]" [ref=e2]
  - checkbox "I agree" [checked=false, ref=e3]
  - textbox "Password" [ref=e4]
  - button "Send message" [ref=e5]
'''


def call(tool, server="playwright", **args):
    return {"serverName": server, "message": f'Allow the {server} MCP server to run tool "{tool}"?',
            "_meta": {"tool_params": args}}


def reset():
    browser_gate.PAGES.clear()
    browser_gate._TYPED.clear()


class ResolveTest(unittest.TestCase):
    def setUp(self):
        reset()

    def test_playwright_snapshot_names_the_target_and_site(self):
        observe("playwright", PLAYWRIGHT)
        info = judge(call("browser_click", ref="e12"))
        self.assertEqual((info["target"], info["site"], info["stakes"], info["verb"]),
                         ('button "Post"', "x.com", "high", "click"))
        self.assertEqual(info["question"], 'click button "Post" on x.com')
        info = judge(call("browser_click", element="Next page", ref="e13"))
        self.assertIsNone(info)

    def test_agent_browser_refs_with_at_sign(self):
        observe("agent_browser", AGENT)
        info = judge(call("agent_browser_click", server="agent_browser", selector="@e5"))
        self.assertEqual(info["target"], 'button "Send message"')
        self.assertEqual(info["stakes"], "high")
        self.assertEqual(browser_gate.PAGES["agent_browser"]["refs"]["e2"], ("button", "Save [ref=e3]"))
        observe("agent_browser", '- link "Other" [ref=e1]')  # a new snapshot renumbers everything
        self.assertNotIn("e5", browser_gate.PAGES["agent_browser"]["refs"])

    def test_unresolvable_ref_still_asks_in_plain_words(self):
        observe("playwright", PLAYWRIGHT)
        info = judge(call("browser_click", ref="e99"))
        self.assertEqual(info["question"], "click an element the page didn't name (e99) on x.com")
        self.assertEqual(info["stakes"], "low")
        self.assertFalse(info["named"])
        self.assertEqual(judge(call("browser_click", ref="e14"))["stakes"], "low")  # unnamed button

    def test_typing_question_and_enter(self):
        observe("playwright", PLAYWRIGHT)
        info = judge(call("browser_type", ref="e5", text="hi", submit=True))
        self.assertEqual(info["question"], 'type into textbox "Message" on x.com (presses Enter)')
        self.assertEqual(info["stakes"], "high")

    def test_password_textbox_via_ref_is_refused(self):
        observe("playwright", PLAYWRIGHT)
        observe("agent_browser", AGENT)
        for server, tool, key in (("playwright", "browser_type", "ref"),
                                  ("agent_browser", "agent_browser_fill", "selector")):
            ref = "e4" if server == "playwright" else "@e4"
            ask = mock.Mock()
            with mock.patch("jarvis.browser_gate.ask", ask):
                ok, why = gate(lambda p: (True, "ok"), {}, "state")(
                    call(tool, server=server, text="hunter2", **{key: ref}))
            self.assertFalse(ok, server)
            self.assertIn("passwords", why)
            ask.assert_not_called()

    def test_page_url_change_voids_old_refs_and_navigation_resets(self):
        observe("playwright", PLAYWRIGHT)
        observe("playwright", "- Page URL: https://discord.com/channels/1\n- button \"Go\" [ref=e1]")
        self.assertEqual(list(browser_gate.PAGES["playwright"]["refs"]), ["e1"])
        browser_gate.observe_call("playwright", "browser_navigate", {"url": "https://github.com/a/b"})
        self.assertEqual(browser_gate.PAGES["playwright"], {"url": "https://github.com/a/b", "refs": {}})
        self.assertEqual(browser_gate.site_of("https://news.bbc.co.uk/x"), "bbc.co.uk")

    def test_result_content_is_observed(self):
        browser_gate.observe_result("playwright", {"content": [{"type": "text", "text": PLAYWRIGHT}]})
        self.assertEqual(browser_gate.PAGES["playwright"]["refs"]["e12"], ("button", "Post"))


class RelayFeedTest(unittest.TestCase):
    def test_relay_learns_refs_from_server_results_and_navigation(self):
        import io
        from jarvis.browser_proxy import Relay
        reset()
        relay = Relay("playwright", {}, lambda p: (True, ""), io.StringIO(), io.StringIO())
        relay._filter({"jsonrpc": "2.0", "id": 1,
                       "result": {"content": [{"type": "text", "text": PLAYWRIGHT}]}})
        self.assertEqual(judge(call("browser_click", ref="e12"))["question"], 'click button "Post" on x.com')


class Case(unittest.TestCase):
    def setUp(self):
        reset()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.queue = ConfirmQueue(self.dir.name)
        patch = mock.patch("jarvis.assistant.confirmation_origin", return_value="dell")
        patch.start()
        self.addCleanup(patch.stop)
        observe("playwright", PLAYWRIGHT)

    def approve(self, params, wait=0.6):
        return gate(lambda p: (True, "ok"), {}, self.dir.name, wait=wait, queue=self.queue)(params)

    def park_via_gate(self, params, wait=5):
        import threading
        out = {}
        # ask() looks up time.sleep on the time module. Capture the real sleep
        # first, and only while this question is parked: a 0.2 s grant expiry
        # later in the file still has to wait out the real clock.
        real_sleep = time.sleep
        nap = mock.patch.object(
            browser_gate.time, "sleep",
            lambda seconds, _real=real_sleep: _real(min(seconds, 0.01)))
        nap.start()
        self.addCleanup(nap.stop)
        worker = threading.Thread(target=lambda: out.update(r=self.approve(params, wait)))
        worker.start()
        for _ in range(100):
            pending = self.queue.pending("dell")
            if pending:
                return worker, out, pending[0]
            time.sleep(0.05)
        self.fail("nothing parked")


class ListingTest(Case):
    def test_listing_shape(self):
        worker, out, item = self.park_via_gate(call("browser_click", ref="e99"), wait=60)
        listing = self.queue.listing("dell")
        got = listing["pending"][0]
        for key in ("id", "summary", "hash"):
            self.assertIn(key, got)
        self.assertEqual((got["kind"], got["site"], got["verb"], got["target"], got["stakes"], got["can_grant"]),
                         ("browser", "x.com", "click", None, "low", False))
        self.assertGreater(got["expires_at"], time.time() + 50)
        self.assertEqual(listing["grants"], [])
        self.queue.reject(got["id"], "dell")
        worker.join(5)
        self.assertEqual(self.queue.listing("dell")["recent"][0]["outcome"], "rejected")
        self.assertIn("declined", out["r"][1])

    def test_command_items_are_kind_command(self):
        self.queue.park({"intent": "close_app", "device": "dell", "args": {"name": "Word"}}, "dell", "console")
        got = self.queue.listing("dell")["pending"][0]
        self.assertEqual((got["kind"], got["can_grant"]), ("command", False))


class GrantTest(Case):
    def test_unnamed_asks_are_never_grantable(self):
        worker, out, item = self.park_via_gate(call("browser_click", ref="e99"))
        self.assertFalse(self.queue.listing("dell")["pending"][0]["can_grant"])
        with self.assertRaises(ValueError):
            self.queue.grant_item(item["id"], "dell")
        self.queue.reject(item["id"], "dell")
        worker.join(5)

    def make_low_named(self, site="x.com", named=True, stakes="low"):
        meta = {"site": site, "verb": "click", "target": 'button "Like"', "stakes": stakes,
                "named": named, "expires_at": time.time() + 60}
        return self.queue.park({"intent": "browser_action", "device": "dell",
                                "args": {"text": "let the browser click", "browser": meta}}, "dell", "console")

    def test_grant_approves_and_stores(self):
        item = self.make_low_named()
        command, info = self.queue.grant_item(item["id"], "dell", hash=item["hash"])
        self.assertEqual(command["intent"], "browser_action")
        self.assertTrue(self.queue.has_grant("x.com", "click"))
        self.assertFalse(self.queue.has_grant("x.com", "type"))
        self.assertFalse(self.queue.has_grant("y.com", "click"))
        listing = self.queue.listing("dell")
        self.assertEqual([(g["site"], g["verb"]) for g in listing["grants"]], [("x.com", "click")])
        self.assertAlmostEqual(listing["grants"][0]["expires_at"], time.time() + 600, delta=5)
        self.assertEqual(listing["recent"][0]["outcome"], "granted")

    def test_high_stakes_or_siteless_cannot_be_granted(self):
        for kwargs in ({"stakes": "high"}, {"site": None}, {"named": False}):
            item = self.make_low_named(**kwargs)
            with self.assertRaises(ValueError, msg=kwargs):
                self.queue.grant_item(item["id"], "dell")
            self.assertFalse(self.queue.has_grant("x.com", "click"))

    def test_grant_does_not_cover_unnamed_elements(self):
        self.queue.grant("x.com", "type")
        observe("playwright", "- Page URL: https://x.com/home\n- textbox \"Message\" [ref=e5]")
        # An unnamed field still asks: grants never cover elements nothing names.
        worker, out, item = self.park_via_gate(call("browser_type", ref="e77", text="a"))
        self.queue.reject(item["id"], "dell")
        worker.join(5)
        self.assertFalse(out["r"][0])

    def test_gate_skips_the_question_for_a_named_low_stakes_call_under_grant(self):
        info = {"question": "q", "reason": "r", "verb": "click", "target": 'button "Like"',
                "site": "x.com", "stakes": "low", "named": True}
        ask = mock.Mock(return_value=(False, "asked"))
        self.queue.grant("x.com", "click")
        with mock.patch("jarvis.browser_gate.judge", return_value=dict(info)), \
                mock.patch("jarvis.browser_gate.ask", ask):
            ok, why = self.approve(call("browser_click", element="x"))
            self.assertTrue(ok)
            self.assertIn("grant", why)
            ask.assert_not_called()
            # same site, high stakes: asks
            with mock.patch("jarvis.browser_gate.judge", return_value={**info, "stakes": "high"}):
                self.assertEqual(self.approve(call("browser_click", element="x")), (False, "asked"))
            # other site / other verb: asks
            with mock.patch("jarvis.browser_gate.judge", return_value={**info, "site": "y.com"}):
                self.assertEqual(self.approve(call("browser_click", element="x"))[1], "asked")
            with mock.patch("jarvis.browser_gate.judge", return_value={**info, "verb": "type"}):
                self.assertEqual(self.approve(call("browser_click", element="x"))[1], "asked")
            # unknown site: asks
            with mock.patch("jarvis.browser_gate.judge", return_value={**info, "site": None}):
                self.assertEqual(self.approve(call("browser_click", element="x"))[1], "asked")

    def test_grant_never_overrides_a_refusal(self):
        self.queue.grant("x.com", "type")
        ok, why = self.approve(call("browser_type", element="Password", text="x"))
        self.assertFalse(ok)
        self.assertIn("passwords", why)

    def test_grants_expire_and_revoke(self):
        self.queue.grant("x.com", "click", seconds=0.2)
        self.assertTrue(self.queue.has_grant("x.com", "click"))
        time.sleep(0.3)
        self.assertFalse(self.queue.has_grant("x.com", "click"))
        self.assertEqual(self.queue.grants(), [])
        self.queue.grant("x.com", "click")
        self.assertEqual(self.queue.revoke("x.com", "click"), 1)
        self.assertFalse(self.queue.has_grant("x.com", "click"))
        self.assertEqual(self.queue.revoke("x.com", "click"), 0)


class TimeoutTest(Case):
    def test_low_stakes_wait_is_60_and_high_is_180(self):
        seen = []
        with mock.patch("jarvis.browser_gate.ask", side_effect=lambda *a, **k: seen.append(a[3]) or (False, "")):
            gate(lambda p: (True, "ok"), {}, self.dir.name, queue=self.queue)(call("browser_click", ref="e99"))
            gate(lambda p: (True, "ok"), {}, self.dir.name, queue=self.queue)(call("browser_click", ref="e12"))
        self.assertEqual(seen, [60, 180])

    def test_no_answer_is_expired_not_rejected_and_says_so(self):
        from tests.security.test_browser_gate import _Clock, _use_clock
        clock = _Clock()
        mono, pause = _use_clock(clock)
        with mono, pause:
            ok, why = self.approve(call("browser_click", ref="e99"), wait=0.6)
        self.assertFalse(ok)
        self.assertIn("David didn't answer in 0.6 s (timed out)", why)
        self.assertNotIn("declined", why)
        self.assertEqual(self.queue.listing("dell")["recent"][0]["outcome"], "expired")
        self.assertEqual(self.queue.pending("dell"), [])

    def test_decline_text_differs(self):
        worker, out, item = self.park_via_gate(call("browser_click", ref="e12"))
        self.queue.reject(item["id"], "dell")
        worker.join(5)
        self.assertIn("David declined", out["r"][1])


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        import os
        os.environ.pop("JARVIS_NO_NOTIFY", None)
        self.addCleanup(self.env.stop)

    def test_linux_notify_send_is_called_with_a_timeout(self):
        with mock.patch("jarvis.platforms.detect", return_value={"os": "linux"}), \
                mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run") as run:
            self.assertTrue(confirm.notify("let the browser click button \"Post\""))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["/usr/bin/notify-send", "-a", "JARVIS", "-u", "critical", "JARVIS needs your OK"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_failures_and_other_systems_are_silent(self):
        with mock.patch("jarvis.platforms.detect", return_value={"os": "linux"}), \
                mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run", side_effect=OSError("no bus")):
            self.assertFalse(confirm.notify("x"))
        with mock.patch("jarvis.platforms.detect", return_value={"os": "windows"}), \
                mock.patch("subprocess.run") as run:
            self.assertFalse(confirm.notify("x"))
        run.assert_not_called()
        with mock.patch("jarvis.platforms.detect", return_value={"os": "linux"}), \
                mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run") as run:
            self.assertFalse(confirm.notify("x"))
        run.assert_not_called()

    def test_ask_notifies_when_parked(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("jarvis.assistant.confirmation_origin", return_value="dell"), \
                mock.patch("jarvis.confirm.notify") as note:
            real_sleep = time.sleep
            with mock.patch.object(
                    browser_gate.time, "sleep",
                    lambda seconds, _real=real_sleep: _real(min(seconds, 0.01))):
                browser_gate.ask("click “Post”", {}, d, wait=0.1)
        note.assert_called_once()
        self.assertIn("Post", note.call_args.args[0])


class ConsoleApiTest(Case):
    def console(self):
        from jarvis.ui import Console
        console = Console.__new__(Console)
        console.state, console.config = self.dir.name, {}
        console.bus = mock.Mock()
        return console

    def test_get_post_grant_and_revoke(self):
        console = self.console()
        meta = {"site": "x.com", "verb": "click", "target": 'button "Like"', "stakes": "low",
                "named": True, "expires_at": time.time() + 60}
        item = self.queue.park({"intent": "browser_action", "device": "dell",
                                "args": {"text": "let the browser click", "browser": meta}}, "dell", "console")
        shape = console.confirmations()
        self.assertEqual(set(shape), {"pending", "grants", "recent"})
        self.assertTrue(shape["pending"][0]["can_grant"])
        json.dumps(shape)  # serializable
        reply = console.confirmation({"id": item["id"], "action": "grant", "hash": item["hash"]})
        self.assertEqual(reply["grant"], {"site": "x.com", "verb": "click"})
        self.assertEqual(console.confirmations()["grants"][0]["site"], "x.com")
        self.assertEqual(console.confirmations()["recent"][0]["outcome"], "granted")
        self.assertTrue(console.confirmation({"action": "revoke", "site": "x.com", "verb": "click"})["revoked"])
        self.assertEqual(console.confirmations()["grants"], [])
        with self.assertRaises(ValueError):
            console.confirmation({"action": "revoke", "site": "x.com"})


if __name__ == "__main__":
    unittest.main()
