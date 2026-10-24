import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.actions import DesktopTools, approve_browser, browser_mcp, is_action
from jarvis.chatgpt import ChatGPTLane, lean_command

APPS = [{"Name": "Google Chrome", "AppID": "Chrome"},
        {"Name": "Google Chrome Canary", "AppID": "ChromeCanary"},
        {"Name": "Spotify", "AppID": r"C:\Users\jdoe\AppData\Roaming\Spotify\Spotify.exe"},
        {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
        {"Name": "Visual Studio Code", "AppID": "Microsoft.VisualStudioCode"}]


def approval(tool, **args):
    return {"serverName": "playwright", "message": f'Allow the playwright MCP server to run tool "{tool}"?',
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": args}}


class RoutingTests(unittest.TestCase):
    def test_actions_vs_questions(self):
        for q in ("open chrome", "Jarvis, can you open Spotify?", "search youtube for lofi",
                  "go to github.com", "use the browser to check the weather",
                  "summarize https://example.com/post", "launch notepad",
                  "ask grok to check hacker news", "have Grok Bot find me flights"):
            self.assertTrue(is_action(q), q)
        for q in ("what is the capital of Peru?", "tell me a joke",
                  "what is a good browser for privacy?", "how do I open a jar file"):
            self.assertFalse(is_action(q), q)


class ApprovalTests(unittest.TestCase):
    def test_browser_policy(self):
        self.assertTrue(approve_browser(approval("browser_navigate", url="https://example.com"))[0])
        self.assertTrue(approve_browser(approval("browser_click", element="Search"))[0])
        for bad in (approval("browser_navigate", url="file:///C:/Users/jdoe/.ssh/id"),
                    approval("browser_navigate", url="chrome://settings"),
                    approval("browser_file_upload", paths=["C:/secret.txt"]),
                    approval("browser_run_code_unsafe", code="1"),
                    {**approval("browser_click"), "serverName": "node_repl"}):
            self.assertFalse(approve_browser(bad)[0], bad)

    def test_browser_server_blocks_risky_tools_and_uses_own_profile(self):
        with tempfile.TemporaryDirectory() as d:
            spec = browser_mcp(d).get("playwright")
            if spec is None:
                self.skipTest("npx not installed")
            self.assertIn("browser_file_upload", spec["disabled_tools"])
            self.assertIn(str(Path(d).resolve() / "browser-profile"), spec["args"])

    def test_agent_browser_policy(self):
        def ab(tool, **args):
            return {**approval(tool, **args), "serverName": "agent_browser"}
        self.assertTrue(approve_browser(ab("agent_browser_open", url="https://example.com"))[0])
        self.assertTrue(approve_browser(ab("agent_browser_snapshot", interactive=True))[0])
        self.assertTrue(approve_browser(ab("agent_browser_click", selector="@e2"))[0])
        for bad in (ab("agent_browser_open", url="file:///home/jdoe/.ssh/id_ed25519"),
                    ab("agent_browser_read", url="chrome://settings"),
                    ab("agent_browser_eval", script="document.cookie"),
                    ab("agent_browser_click", selector="@e1", extraArgs=["--profile", "/"]),
                    ab("agent_browser_snapshot", session="other"),
                    ab("agent_browser_screenshot", path="/home/jdoe/.bashrc"),
                    ab("browser_navigate", url="https://example.com"),
                    approval("agent_browser_open", url="https://example.com")):
            self.assertFalse(approve_browser(bad)[0], bad)

    def test_agent_browser_server_is_core_only_with_own_profile(self):
        with tempfile.TemporaryDirectory() as d:
            servers = browser_mcp(d)
            spec = servers.get("agent_browser")
            if spec is None:
                self.skipTest("npx not installed")
            self.assertEqual(spec["args"][-3:], ["mcp", "--tools", "core"])
            self.assertIn("agent_browser_eval", spec["disabled_tools"])
            self.assertEqual(spec["env"]["AGENT_BROWSER_PROFILE"],
                             str(Path(d).resolve() / "agent-browser-profile"))
            self.assertEqual(set(browser_mcp(d, ["playwright"])), {"playwright"})
            self.assertEqual(set(browser_mcp(d, [])), set())

    def test_lean_command_passes_server_env_as_toml(self):
        with patch("jarvis.chatgpt.user_mcp_servers", return_value=[]):
            args = lean_command("codex", {"agent_browser": {
                "command": "npx", "env": {"AGENT_BROWSER_PROFILE": "C:\\x\\p"},
                "env_vars": ["DISPLAY"]}})
        self.assertIn('mcp_servers.agent_browser.env.AGENT_BROWSER_PROFILE="C:\\\\x\\\\p"', args)
        self.assertIn('mcp_servers.agent_browser.env_vars=["DISPLAY"]', args)


class DesktopToolTests(unittest.TestCase):
    def setUp(self):
        self.tools = DesktopTools()
        self.tools._apps, self.tools._loaded = APPS, 1e18  # never refresh from Windows

    def test_matching_prefers_the_plain_app(self):
        self.assertEqual(self.tools.match("chrome")[0]["Name"], "Google Chrome")
        self.assertEqual(self.tools.match("vs code")[0]["Name"], "Visual Studio Code")
        self.assertEqual(self.tools.match("spotfy")[0]["Name"], "Spotify")  # typo
        self.assertEqual(self.tools.match("photoshop"), [])

    @unittest.skipUnless(os.name == "nt", "Windows launches through the Start Apps folder")
    @patch("jarvis.actions.subprocess.Popen")
    def test_open_app_launches_through_shell_apps_folder(self, popen):
        self.assertTrue(self.tools.call("open_app", {"name": "notepad"})[0])
        self.assertEqual(popen.call_args.args[0],
                         ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"])

    @unittest.skipIf(os.name == "nt", "Linux launches the .desktop entry")
    @patch("jarvis.actions.launch_desktop_entry", return_value=(True, "Opened Notepad."))
    def test_open_app_launches_the_desktop_entry(self, launch):
        self.assertTrue(self.tools.call("open_app", {"name": "notepad"})[0])
        self.assertEqual(launch.call_args.args[0]["Name"], "Notepad")

    def test_open_app_needs_a_matching_app(self):
        self.assertFalse(self.tools.call("open_app", {"name": "photoshop"})[0])

    @patch("jarvis.vault.open_link")
    def test_open_url_only_http(self, open_link):
        self.assertTrue(self.tools.call("open_url", {"url": "youtube.com"})[0])
        open_link.assert_called_with("https://youtube.com")
        for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "C:\\Windows\\notepad.exe"):
            self.assertFalse(self.tools.call("open_url", {"url": bad})[0], bad)


class LeanCommandTests(unittest.TestCase):
    def test_turns_off_davids_servers_but_keeps_jarvis_ones(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "config.toml").write_text(
                '[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers.playwright]\ncommand = "y"\n',
                encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": home}):
                args = lean_command("codex", {"playwright": {"command": "npx", "args": ["-y", "pw"]}},
                                    ['web_search="disabled"'])
        joined = " ".join(args)
        self.assertIn("--disable apps", joined)
        self.assertIn("--disable plugins", joined)
        self.assertIn("mcp_servers.node_repl.enabled=false", joined)
        self.assertNotIn("mcp_servers.playwright.enabled=false", joined)
        self.assertIn('mcp_servers.playwright.args=["-y", "pw"]', args)
        self.assertEqual(args[-2:], ["-c", 'web_search="disabled"'])


FAKE_ACTION_SERVER = r'''
import json, sys
def send(m): print(json.dumps(m), flush=True)
replies = {}
for line in sys.stdin:
    m = json.loads(line)
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        assert m["params"]["capabilities"]["experimentalApi"] is True
        send({"id": rid, "result": {}})
    elif meth == "thread/start":
        p = m["params"]
        assert p["approvalPolicy"] == "on-request"
        assert [t["name"] for t in p["dynamicTools"]] == ["open_app", "list_apps", "open_url",
                                                          "open_page", "close_app", "media", "volume"]
        send({"method": "mcpServer/startupStatus/updated", "params": {"name": "playwright", "status": "ready"}})
        send({"id": rid, "result": {"thread": {"id": "t1"}}})
    elif meth == "turn/start":
        send({"id": 50, "method": "item/tool/call", "params": {"tool": "open_app",
              "arguments": {"name": "notepad"}, "callId": "c", "threadId": "t1", "turnId": "u"}})
        for i, url in ((51, "https://example.com"), (52, "file:///C:/secret")):
            send({"id": i, "method": "mcpServer/elicitation/request", "params": {
                "serverName": "playwright", "message": 'Allow the playwright MCP server to run tool "browser_navigate"?',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"url": url}}}})
        send({"method": "item/completed", "params": {"item": {"type": "mcpToolCall", "server": "playwright",
              "tool": "browser_navigate", "status": "completed"}}})
        turn = rid
    elif rid in (50, 51, 52):
        replies[rid] = m["result"]
        if len(replies) == 3:
            text = (f"app={replies[50]['success']} https={replies[51]['action']} "
                    f"file={replies[52]['action']}.")
            send({"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": text}})
            send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
'''


class ActionLaneTests(unittest.TestCase):
    def test_tools_and_approvals_end_to_end(self):
        tools = DesktopTools()
        launched = []
        tools.open_app = lambda name: (launched.append(name) or True, f"Opened {name}.")
        with tempfile.TemporaryDirectory() as d:
            script = Path(d, "fake_action_server.py")
            script.write_text(FAKE_ACTION_SERVER, encoding="utf-8")
            lane = ChatGPTLane({}, d, timeout=20, command=[sys.executable, str(script)],
                               tools=tools, approve=approve_browser,
                               mcp_servers={"playwright": {"command": "npx"}})
            seen = []
            lane.on_tool = lambda key, name, ok, text: seen.append((key, name, ok))
            try:
                answer = lane.ask("open notepad then example.com")
            finally:
                lane.close()
        self.assertEqual(answer, "app=True https=accept file=decline.")
        self.assertEqual(launched, ["notepad"])  # the launch went through JARVIS's tool
        names = [n for n, _, _ in lane.tool_log]
        self.assertIn("open_app", names)
        self.assertIn("playwright.browser_navigate", names)
        self.assertTrue(any(n.startswith("decline") for n in names))
        # The connection tree sees JARVIS's own tool start, then finish, under one key.
        self.assertEqual([x for x in seen if x[1] == "open_app"],
                         [("jarvis:50", "open_app", None), ("jarvis:50", "open_app", True)])
        self.assertIn("playwright.browser_navigate", [n for _, n, _ in seen])


if __name__ == "__main__":
    unittest.main()
