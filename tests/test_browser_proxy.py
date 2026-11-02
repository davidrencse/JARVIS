"""The Claude brain's browsers (2026-11-02): every tool call passes the same approve_browser
and browser_gate checks Codex runs for ChatGPT, through browser_proxy's relay."""

import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from jarvis import browser_proxy
from jarvis.browser_proxy import Relay

# A stand-in MCP server: lists three tools, answers every call with what it was asked.
FAKE = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m.get("method") == "tools/list":
            r = {"tools": [{"name": "browser_navigate"}, {"name": "browser_type"},
                           {"name": "browser_file_upload"}]}
        else:
            r = {"content": [{"type": "text", "text": "ran " + json.dumps(m.get("params"))}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
""")


def run(messages, confirm="never", approve=None):
    with tempfile.TemporaryDirectory() as root:
        state = Path(root) / "state"  # asks read root/jarvis.config.json, never $TMPDIR's
        state.mkdir()
        return _run(messages, confirm, approve, state)


def _run(messages, confirm, approve, state):
    config = {"rules": {"confirm": confirm}}
    spec = {"command": sys.executable, "args": ["-c", FAKE], "disabled_tools": ["browser_file_upload"]}
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    Relay("playwright", spec, approve or browser_proxy.approver(config, state), stdin, out).run()
    return {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}


def call(rid, tool, **args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": tool, "arguments": args}}


class RelayTests(unittest.TestCase):
    def test_lists_without_the_disabled_tools(self):
        replies = run([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]],
                         ["browser_navigate", "browser_type"])

    def test_plain_browsing_reaches_the_browser(self):
        replies = run([call(1, "browser_navigate", url="https://github.com")])
        self.assertIn("github.com", replies[1]["result"]["content"][0]["text"])
        self.assertNotIn("isError", replies[1]["result"])

    def test_refusals_never_reach_the_browser(self):
        replies = run([
            call(1, "browser_type", element="Password field", text="hunter2"),
            call(2, "browser_type", element="Card number", text="4111 1111 1111 1111"),
            call(3, "browser_navigate", url="file:///etc/passwd"),
            call(4, "browser_navigate", url="http://127.0.0.1:7777/"),
            call(5, "browser_file_upload", paths=["/home/jdoe/.ssh/id_ed25519"]),
            call(6, "browser_type", element="Enter the 6-digit verification code", text="482913"),
        ])
        for rid in range(1, 7):
            self.assertTrue(replies[rid]["result"]["isError"], rid)
            self.assertTrue(replies[rid]["result"]["content"][0]["text"].startswith("Refused"), rid)

    def test_commit_clicks_wait_for_david_while_rules_ask(self):
        click = call(1, "browser_click", element="Post button", ref="e12")
        with mock.patch("jarvis.browser_gate.ask", return_value=(False, "David didn't approve")) as ask:
            replies = run([click], confirm="always")
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("Post", ask.call_args.args[0])
        with mock.patch("jarvis.browser_gate.ask") as ask:  # full permissions: no question
            replies = run([click], confirm="never")
        ask.assert_not_called()
        self.assertNotIn("isError", replies[1]["result"])

    def test_a_broken_check_refuses(self):
        def broken(params):
            raise ValueError("boom")
        replies = run([call(1, "browser_navigate", url="https://github.com")], approve=broken)
        self.assertTrue(replies[1]["result"]["isError"])


class WiringTests(unittest.TestCase):
    def test_claude_brain_gets_each_browser_through_a_relay(self):
        from jarvis import inference
        specs = {"playwright": {"command": "npx"}, "agent_browser": {"command": "agent-browser"}}
        with tempfile.TemporaryDirectory() as state, \
                mock.patch("jarvis.actions.browser_mcp", return_value=specs), \
                mock.patch("jarvis.chatgpt.brain_parts", return_value={
                    "context": lambda: "", "instructions": []}):
            lane = inference.claude_brain({"actions": {"enabled": True}}, state, Path(state))
            args = lane._args("claude")
        servers = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
        self.assertEqual(sorted(servers), ["agent_browser", "jarvis", "playwright"])
        self.assertEqual(servers["playwright"]["args"][-2:], ["browser-proxy", "playwright"])
        allowed = args[args.index("--allowedTools") + 1].split(",")
        self.assertEqual(sorted(allowed), ["mcp__agent_browser", "mcp__jarvis", "mcp__playwright"])

    def test_no_browser_when_switched_off(self):
        with mock.patch("jarvis.actions.browser_mcp") as specs:
            self.assertEqual(browser_proxy.servers({"actions": {"browser": False}}, "."), {})
            self.assertEqual(browser_proxy.servers({"actions": {"enabled": False}}, "."), {})
        specs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
