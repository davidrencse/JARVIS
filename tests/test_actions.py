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

    def test_browser_never_opens_this_pc_or_home_network(self):
        def ab(tool, **args):
            return {**approval(tool, **args), "serverName": "agent_browser"}
        for url in ("http://127.0.0.1:7777/#settings", "http://localhost:7780/", "http://[::1]:7777",
                    "http://0.0.0.0:7777", "http://2130706433:7777/", "http://127.1:7777",
                    "http://[::ffff:127.0.0.1]:7777", "http://192.168.1.1/", "http://10.0.0.5",
                    "http://169.254.169.254/latest", "http://archmain.local:7777",
                    "https://router.lan/", "http://foo.localhost/", "http://user@127.0.0.1:7777/"):
            self.assertFalse(approve_browser(approval("browser_navigate", url=url))[0], url)
            self.assertFalse(approve_browser(ab("agent_browser_open", url=url))[0], url)
        with patch("jarvis.actions.socket.getaddrinfo",
                        return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):  # public name -> loopback
            self.assertFalse(approve_browser(approval("browser_navigate", url="http://evil.example"))[0])
        with patch("jarvis.actions.socket.getaddrinfo",
                        return_value=[(2, 1, 6, "", ("93.184.215.14", 0))]):
            self.assertTrue(approve_browser(approval("browser_navigate", url="https://example.com"))[0])
            self.assertFalse(approve_browser(ab("agent_browser_open", url="https://example.com",
                                                 targetUrl="http://127.0.0.1:7777"))[0])

    def test_browser_server_blocks_risky_tools_and_uses_own_profile(self):
        with tempfile.TemporaryDirectory() as d:
            spec = browser_mcp(d).get("playwright")
            if spec is None:
                self.skipTest("npx not installed")
            self.assertIn("browser_file_upload", spec["disabled_tools"])
            self.assertIn(str(Path(d).resolve() / "browser-profile"), spec["args"])
            self.assertIn("http://127.0.0.1:7777", spec["args"][spec["args"].index("--blocked-origins") + 1])

    def test_my_chrome_server_attaches_by_extension_with_saved_token(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAYWRIGHT_MCP_EXTENSION_TOKEN", None)
            spec = browser_mcp(d, ["chrome"]).get("chrome")
            if spec is None:
                self.skipTest("npx not installed")
            self.assertIn("--extension", spec["args"])
            self.assertNotIn("--user-data-dir", spec["args"])
            self.assertIn("browser_evaluate", spec["disabled_tools"])
            self.assertNotIn("env", spec)  # no token saved: the extension asks David
            (Path(d) / "chrome-extension-token").write_text("tok\n")
            spec = browser_mcp(d, ["chrome"])["chrome"]
            self.assertEqual(spec["env"], {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"})
            self.assertNotIn("chrome", browser_mcp(d, ["playwright"]))

    def test_my_chrome_policy_keeps_gates_and_off_limits(self):
        def chrome(tool, **args):
            return {**approval(tool, **args), "serverName": "chrome"}
        with patch("jarvis.actions.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("93.184.215.14", 0))]):
            self.assertTrue(approve_browser(chrome("browser_navigate", url="https://mail.google.com/"))[0])
            self.assertTrue(approve_browser(chrome("browser_snapshot"))[0])
            for bad in (chrome("browser_navigate", url="https://myaccount.google.com/security"),
                        chrome("browser_navigate", url="https://passwords.google.com"),
                        chrome("browser_navigate", url="file:///etc/passwd"),
                        chrome("browser_evaluate", function="() => document.cookie"),
                        chrome("browser_file_upload", paths=["/home/jdoe/.ssh/id_ed25519"])):
                self.assertFalse(approve_browser(bad)[0], bad)
            # Off limits only in his own Chrome; JARVIS's profile has no such sign-ins.
            self.assertTrue(approve_browser(approval("browser_navigate", url="https://myaccount.google.com"))[0])

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


class HomeNetworkTests(unittest.TestCase):
    def test_browser_never_opens_home_lan_or_cgnat(self):
        import ipaddress
        from jarvis import actions
        lan = [ipaddress.ip_network("172.1.0.0/16")]  # David's home LAN: not RFC1918
        with patch.object(actions, "_own_networks", return_value=lan):
            for url in ("http://172.1.0.1/", "http://172.1.4.20:7777/", "http://100.101.102.103/",
                        "http://[fd00::1]/", "http://192.0.0.8/"):
                self.assertFalse(approve_browser(approval("browser_navigate", url=url))[0], url)
            with patch("jarvis.actions.socket.getaddrinfo",
                       return_value=[(2, 1, 6, "", ("172.1.0.1", 0))]):  # a public name for the router
                self.assertFalse(approve_browser(approval("browser_navigate", url="http://evil.example"))[0])
            self.assertTrue(approve_browser(approval("browser_navigate", url="http://93.184.215.14/"))[0])

    def test_global_ipv6_on_own_subnet_is_local(self):
        import ipaddress
        from jarvis import actions
        nets = [ipaddress.ip_network("172.1.0.0/16"), ipaddress.ip_network("2601:1::/64")]
        with patch("jarvis.sharedinput.local_networks", return_value=nets) as read:
            actions._NETS["at"] = 0.0
            self.assertIsNotNone(actions._local_address("http://[2601:1::9]/"))
            self.assertIsNone(actions._local_address("http://[2606:4700::1111]/"))
        read.assert_called_with(families=(4, 6))
        actions._NETS["at"] = 0.0

    def test_own_networks_cached_and_failure_safe(self):
        from jarvis import actions
        with patch("jarvis.sharedinput.local_networks", side_effect=OSError("no ip")) as nets:
            actions._NETS["at"] = 0.0
            self.assertEqual(actions._own_networks(), [])
            self.assertEqual(actions._own_networks(), [])
        self.assertEqual(nets.call_count, 1)
        actions._NETS["at"] = 0.0


class SpotifyPlayingCheckTests(unittest.TestCase):
    def test_failed_metadata_read_is_not_playing(self):
        import json
        from types import SimpleNamespace
        from jarvis import actions

        def run(*args):
            if args[:2] == ("playerctl", "-l"):
                return SimpleNamespace(returncode=0, stdout="spotify\n", stderr="")
            if "status" in args:
                return SimpleNamespace(returncode=0, stdout="Playing", stderr="")  # the old track
            if "metadata" in args and "trackid" in args[-1] and run.before:
                run.before = False
                return SimpleNamespace(returncode=0, stdout="/com/spotify/track/old", stderr="")
            if "metadata" in args:
                return SimpleNamespace(returncode=1, stdout="", stderr="no player")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        run.before = True
        clock = iter(range(0, 1000))
        with patch.object(actions, "_run", side_effect=run), \
                patch.object(actions, "_backend", return_value="hyprland"), \
                patch.object(actions.os, "name", "posix"), \
                patch.object(actions.shutil, "which", return_value="/usr/bin/playerctl"), \
                patch.object(actions.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(actions.time, "sleep"):
            ok, said = actions.spotify(json.dumps({"action": "play", "target": "album:4LH4d3cOWNNsVw41Gqt2kv"}))
        self.assertFalse(ok, said)
        self.assertIn("didn't report playing", said)


class DesktopToolTests(unittest.TestCase):
    def setUp(self):
        self.tools = DesktopTools()
        self.tools._apps, self.tools._loaded = APPS, 1e18  # never refresh from Windows

    def test_matching_prefers_the_plain_app(self):
        self.assertEqual(self.tools.match("chrome")[0]["Name"], "Google Chrome")
        self.assertEqual(self.tools.match("vs code")[0]["Name"], "Visual Studio Code")
        self.assertEqual(self.tools.match("spotfy")[0]["Name"], "Spotify")  # typo
        self.assertEqual(self.tools.match("photoshop"), [])

    def test_matching_uses_observer_names_and_no_loose_neighbours(self):
        # audit 2026-11-07: "discord" opened KDE Discover, "steam"/"teams" opened Telegram.
        tools = DesktopTools()
        tools._apps, tools._loaded = [
            {"Name": "Discover", "AppID": "org.kde.discover.desktop"},
            {"Name": "Telegram", "AppID": "org.telegram.desktop"},
            {"Name": "Vesktop", "AppID": "dev.vencord.Vesktop.desktop"},
            {"Name": "KiCad PCB Calculator", "AppID": "org.kicad.pcbcalculator.desktop"},
            {"Name": "KCalc", "AppID": "org.kde.kcalc.desktop"},
            {"Name": "Spotify", "AppID": "spotify.desktop"}], 1e18
        self.assertEqual([a["Name"] for a in tools.match("Discord")], ["Vesktop"])
        self.assertEqual(tools.match("steam"), [])
        self.assertEqual(tools.match("teams"), [])
        self.assertEqual(tools.match("Calculator")[0]["Name"], "KCalc")
        self.assertEqual(tools.match("spotfy")[0]["Name"], "Spotify")  # a typo still finds it
        self.assertEqual(tools.match("discover")[0]["Name"], "Discover")
        ok, said = tools.call("open_app", {"name": "steam"})
        self.assertFalse(ok)
        self.assertIn("No installed app", said)

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

    def test_list_apps_wildcards_list_everything(self):
        everything = self.tools.call("list_apps", {"query": ""})
        for query in ("*", "all", "All", "everything"):  # what models write (eval 2026-11-02)
            self.assertEqual(self.tools.call("list_apps", {"query": query}), everything, query)
        self.assertNotEqual(self.tools.call("list_apps", {"query": "chrome"}), everything)

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
        assert [t["name"] for t in p["dynamicTools"]] == ["open_app", "list_apps", "terminal", "open_url",
                                                          "open_page", "close_app", "focus_window",
                                                          "media", "scroll", "spotify", "computer", "app_ui", "volume"]
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


class CloseByTitleTests(unittest.TestCase):
    def test_browser_tab_title_never_closes_the_browser(self):
        from jarvis import actions
        tab = {"app": "google-chrome", "title": "Spotify Wrapped 2026 - YouTube", "id": "0x1", "focused": False}
        with patch.object(actions, "open_windows", return_value=[tab]), \
                patch.object(actions, "_close", return_value=True) as close:
            ok, text = actions.close_app("spotify")
        self.assertFalse(ok)
        self.assertIn("isn't running", text)
        close.assert_not_called()

    def test_vs_code_does_not_match_android_studio(self):
        from jarvis import actions
        studio = {"app": "jetbrains-studio", "title": "MyApp", "id": "0x2", "focused": False}
        with patch.object(actions, "open_windows", return_value=[studio]), \
                patch.object(actions, "_close", return_value=True) as close:
            ok, _ = actions.close_app("vs code")
        self.assertFalse(ok)
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()


FAKE_USAGE_SERVER = r'''
import json, sys
def send(m): print(json.dumps(m), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"id": rid, "result": {}})
    elif meth == "thread/start":
        send({"id": rid, "result": {"thread": {"id": "t1"}}})
    elif meth == "turn/start":
        for n in (10, 5):
            send({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
                  "last": {"inputTokens": n, "outputTokens": 1}, "total": {"inputTokens": 99}}}})
        send({"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": "ok"}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
'''


class AuditTwoLaneTests(unittest.TestCase):
    def test_turn_usage_sums_every_request(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d, "usage_server.py")
            script.write_text(FAKE_USAGE_SERVER, encoding="utf-8")
            lane = ChatGPTLane({}, d, timeout=20, command=[sys.executable, str(script)])
            try:
                lane.ask("hi")
            finally:
                lane.close()
        self.assertEqual((lane.last_usage["inputTokens"], lane.last_usage["outputTokens"]), (15, 2))

    def test_limited_fast_path_leaves_tool_log_alone(self):
        lane = ChatGPTLane({}, ".", timeout=20)
        lane.tool_log = [("x", True, "")]
        with patch("jarvis.chatgpt.usage_limit", return_value="try later"):
            with self.assertRaises(RuntimeError) as ctx:
                lane.ask("hi")
        self.assertEqual(lane.tool_log, [("x", True, "")])
        self.assertEqual(ctx.exception.tool_log, [])

    def test_startup_deadline_capped_by_lane_timeout(self):
        lane = ChatGPTLane({}, ".", timeout=3)
        seen = []
        lane._send = lambda m: None
        lane._next = lambda deadline: (seen.append(deadline - __import__("time").monotonic()),
                                       {"id": lane.next_id, "result": {}})[1]
        lane._request("initialize", {})
        self.assertLess(seen[0], 3.5)
