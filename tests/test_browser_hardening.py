"""Hardening of the browser gates and relay (2026-11-02 audit)."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import actions, browser_gate, browser_proxy
from jarvis.browser_proxy import Relay


def params(server, tool, **args):
    return {"serverName": server, "message": f'Allow the {server} MCP server to run tool "{tool}"?',
            "_meta": {"tool_params": args}}


class ApproveBrowserTests(unittest.TestCase):
    def test_drop_and_upload_blocked(self):
        for tool in ("browser_drop", "browser_file_upload"):
            self.assertFalse(actions.approve_browser(params("playwright", tool, paths=["/x"]))[0])

    def test_file_arguments_refused_on_any_playwright_tool(self):
        for key in ("filename", "path", "paths", "outputPath", "outputDir", "Filename"):
            ok, _ = actions.approve_browser(params("playwright", "browser_take_screenshot", **{key: "x"}))
            self.assertFalse(ok, key)
        ok, _ = actions.approve_browser(params("playwright", "browser_find", query={"filename": "a"}))
        self.assertFalse(ok)
        self.assertTrue(actions.approve_browser(params("playwright", "browser_click", element="Ok"))[0])

    def test_page_tools_and_unknown_names_fail_closed(self):
        # Audit 2026-11-04: browser_webmcp_call ran page-registered tools (send_email) unasked.
        for server, tool in (("playwright", "browser_webmcp_call"), ("chrome", "browser_webmcp_call"),
                             ("playwright", "browser_mouse_click_xy"), ("playwright", "browser_mouse_down"),
                             ("playwright", "browser_cookie_get"), ("playwright", "browser_network_request"),
                             ("playwright", "browser_set_storage_state"), ("playwright", "browser_route"),
                             ("playwright", "browser_some_future_tool"),
                             ("agent_browser", "agent_browser_webmcp_invoke"),
                             ("agent_browser", "agent_browser_dialog_accept"),
                             ("agent_browser", "agent_browser_keyboard_type"),
                             ("agent_browser", "agent_browser_some_future_tool")):
            ok, why = actions.approve_browser(params(server, tool, name="send_email", arguments={}))
            self.assertFalse(ok, tool)
            self.assertIn("not allowed", why)
        self.assertIn("browser_webmcp_call", actions.BROWSER_BLOCKED)
        self.assertFalse(actions.BROWSER_ALLOWED & actions.BROWSER_BLOCKED)
        self.assertFalse(actions.AGENT_BROWSER_ALLOWED & actions.AGENT_BROWSER_BLOCKED)

    def test_read_only_and_navigation_tools_still_allowed(self):
        for server, tool, args in (("playwright", "browser_snapshot", {}),
                                   ("playwright", "browser_navigate", {"url": "https://example.com"}),
                                   ("playwright", "browser_wait_for", {"time": 1}),
                                   ("playwright", "browser_webmcp_list", {}),
                                   ("playwright", "browser_press_sequentially", {"text": "hi"}),
                                   ("agent_browser", "agent_browser_scroll", {"direction": "down"}),
                                   ("agent_browser", "agent_browser_snapshot", {}),
                                   ("agent_browser", "agent_browser_get_text", {"selector": "@e1"})):
            self.assertTrue(actions.approve_browser(params(server, tool, **args))[0], tool)

    def test_press_sequentially_is_typing(self):
        self.assertEqual(browser_gate.blocked("browser_press_sequentially", {"text": "4111 1111 1111 1111"}),
                         "JARVIS never types card numbers")
        self.assertTrue(browser_gate.commits(params("t", "browser_press_sequentially", text="hi", submit=True)))

    def test_playwright_output_dir_is_under_state(self):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch("shutil.which", return_value="/usr/bin/npx"):
            args = actions.browser_mcp(state, ("playwright",))["playwright"]["args"]
            out = Path(args[args.index("--output-dir") + 1])
            self.assertEqual(out.parent, Path(state).resolve())
            self.assertTrue(out.is_dir())


class GateTests(unittest.TestCase):
    def test_unlabelled_interactions_need_confirmation(self):
        for tool, args in (("agent_browser_click", {"selector": "@e12"}),
                           ("browser_click", {"target": "e12"}),
                           ("agent_browser_fill", {"selector": "@e3", "text": "x"}),
                           ("agent_browser_select", {"selector": "e4", "value": "a"})):
            self.assertTrue(browser_gate.commits(params("s", tool, **args)), tool)

    def test_labelled_harmless_click_is_automatic(self):
        browser_gate.observe("labelled", '- link "Next page" [ref=e12]')  # only a snapshot proves it
        self.assertIsNone(browser_gate.commits(params("labelled", "browser_click", element="Next page",
                                                      ref="e12", button="left")))

    def test_line_break_in_typed_text_commits(self):
        for text in ("hi\n", "hi\r"):
            self.assertTrue(browser_gate.commits(
                params("s", "browser_type", element="Message box", text=text, slowly=True)))
        browser_gate.observe("ss", '- searchbox "Search" [ref=e1]')  # only a snapshot proves a search box
        self.assertIsNone(browser_gate.commits(
            params("ss", "browser_type", element="Search box", ref="e1", text="hi\n")))

    def test_card_check_kept(self):
        self.assertTrue(browser_gate.blocked("browser_type", {"element": "Number", "text": "4111 1111 1111 1111"}))

    def test_unavailable_asking_refuses(self):
        approve = browser_gate.gate(lambda p: (True, "ok"), {}, ".")
        with mock.patch("jarvis.gates.asks_now", return_value=True), \
                mock.patch("jarvis.browser_gate.ask", side_effect=OSError("no queue")):
            ok, _ = approve(params("s", "agent_browser_click", selector="@e1"))
        self.assertFalse(ok)


FAKE = ("import json,sys\n"
        "for l in sys.stdin:\n"
        " m=json.loads(l)\n"
        " if isinstance(m,dict) and 'id' in m:\n"
        "  r={'tools':'bad'} if m.get('method')=='tools/list' else {'echo':m.get('method')}\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)\n"
        "  print('{\"id\":[1],\"result\":{}}',flush=True)\n")


def relay(lines, approve=lambda p: (True, "ok")):
    spec = {"command": sys.executable, "args": ["-c", FAKE]}
    out = io.StringIO()
    Relay("playwright", spec, approve, io.StringIO("".join(lines)), out).run()
    return [json.loads(x) for x in out.getvalue().splitlines()]


class ProxyTests(unittest.TestCase):
    def test_batches_never_forwarded(self):
        calls = []
        batch = [{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "browser_drop", "arguments": {}}}]
        out = relay([json.dumps(batch) + "\n"], approve=lambda p: calls.append(p) or (True, ""))
        self.assertEqual([m for m in out if m.get("id") == 7 and "error" in m][0]["id"], 7)
        self.assertFalse([m for m in out if "result" in m and m.get("id") == 7])

    def test_unhashable_ids_and_bad_replies_do_not_kill_the_relay(self):
        msgs = [{"jsonrpc": "2.0", "id": [1], "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
        out = relay([json.dumps(m) + "\n" for m in msgs])
        self.assertIn(3, [m.get("id") for m in out])

    def test_non_object_arguments_refused(self):
        out = relay([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "browser_click", "arguments": ["x"]}}) + "\n"])
        self.assertTrue(out[0]["result"]["isError"])

    def test_stuck_child_is_killed_after_terminate(self):
        proc = mock.MagicMock()
        proc.stdout = []
        proc.stdin = mock.MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("c", 5)] * 2 + [0]
        Relay("p", {}, lambda p: (True, ""), io.StringIO(""), io.StringIO()).run(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class WindowTests(unittest.TestCase):
    def test_pointer_restored_when_focus_raises(self):
        calls = []

        def run(*cmd):
            calls.append(cmd)
            out = mock.Mock(returncode=0)
            out.stdout = {"clients": json.dumps([{"address": "0xa", "at": [0, 0], "size": [10, 10]}]),
                          "cursorpos": "5, 6"}.get(cmd[1], "ok")
            if cmd[:2] == ("hyprctl", "dispatch") and "get_window" in cmd[2]:
                raise subprocess.TimeoutExpired("hyprctl", 1)
            return out
        with mock.patch.object(actions, "_run", run):
            with self.assertRaises(subprocess.TimeoutExpired):
                actions._hypr_pointer_focus("0xa", {"id": "0xa"})
        self.assertIn("x = 5, y = 6", calls[-1][2])

    def test_no_synthetic_alt_key(self):
        import inspect
        self.assertNotIn("keybd_event", inspect.getsource(actions.raise_window))


if __name__ == "__main__":
    unittest.main()
