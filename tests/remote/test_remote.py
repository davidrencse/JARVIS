import base64
import json
import secrets
import shlex
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import grokbot, remote
from jarvis.actions import DesktopTools
from jarvis.remote import Remote

CONFIG = {"devices": {
    "dell": {"transport": "local", "hostname": "WinLaptop"},
    "zenbook": {"transport": "ssh", "target": "jdoe@192.168.0.194",
                "fallback_targets": ["jdoe@100.64.0.11"], "hostname": "archmain"}}}


class FakeTools:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        return True, f"did {name} {args}"


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tools = FakeTools()

    def remote(self, host):
        r = Remote(CONFIG, self.tmp.name, tools=self.tools)
        r.here = lambda: host
        return r

    def test_here_runs_locally(self):
        ok, _ = self.remote("zenbook").call("zenbook", "open_app", "Spotify")
        self.assertTrue(ok)
        self.assertEqual(self.tools.calls, [("open_app", {"name": "Spotify"})])
        self.remote("zenbook").call(None, "open_url", "github.com")
        self.assertEqual(self.tools.calls[-1], ("open_url", {"url": "github.com"}))

    def test_unknown_device_and_action(self):
        ok, text = self.remote("zenbook").call("mac", "open_app", "x")
        self.assertFalse(ok)
        self.assertIn("dell, zenbook", text)
        self.assertFalse(self.remote("zenbook").call("dell", "run_shell", "rm -rf /")[0])

    def test_dell_reaches_zenbook_over_ssh_with_the_value_as_data(self):
        evil = "Spotify'; rm -rf ~ #"
        seen = []

        def run(args, **kw):
            seen.append(args)
            if args[-2] == "jdoe@192.168.0.194":  # first address is down
                return subprocess.CompletedProcess(
                    args, 255, "", "ssh: connect to host 192.168.0.194 port 22: No route to host\n")
            return subprocess.CompletedProcess(args, 0, '{"ok": true, "text": "Opened Spotify."}\n', "")

        with patch.object(remote.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(remote.subprocess, "run", side_effect=run):
            ok, text = self.remote("dell").call("zenbook", "open_app", evil)
        self.assertEqual((ok, text), (True, "Opened Spotify."))
        self.assertEqual([a[-2] for a in seen], ["jdoe@192.168.0.194", "jdoe@100.64.0.11"])
        command = seen[-1][-1]
        self.assertNotIn("rm -rf", command)  # only base64 crosses the shell
        data = shlex.split(command.split("--b64", 1)[1])[0]
        self.assertEqual(base64.b64decode(data).decode(), evil)
        self.assertIn("StrictHostKeyChecking=yes", seen[-1])

    def test_ssh_drop_mid_command_is_not_resent_on_the_next_address(self):
        seen = []

        def run(args, **kw):
            seen.append(args[-2])
            return subprocess.CompletedProcess(
                args, 255, "", "Connection to 192.168.0.194 closed by remote host.\n")

        with patch.object(remote.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(remote.subprocess, "run", side_effect=run):
            ok, text = self.remote("dell").call("zenbook", "close_app", "Spotify")
        self.assertFalse(ok)
        self.assertIn("may have run", text)
        self.assertEqual(seen, ["jdoe@192.168.0.194"])  # never re-sent over Tailscale

    def test_ssh_reply_without_a_result_may_have_run(self):
        def run(args, **kw):
            return subprocess.CompletedProcess(args, 1, "", "Traceback ...\nKeyError: 'x'\n")

        with patch.object(remote.shutil, "which", return_value="/usr/bin/ssh"), \
                patch.object(remote.subprocess, "run", side_effect=run):
            ok, text = self.remote("dell").call("zenbook", "media", "next")
        self.assertFalse(ok)
        self.assertIn("may have run", text)

    def test_zenbook_reaches_dell_through_the_tunnel_relay(self):
        token = secrets.token_urlsafe(16)
        fake = FakeTools()
        relay = grokbot.relay_server(object(), token, 0)
        port = relay.server_address[1]
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        folder = Path(self.tmp.name, "cfg")
        folder.mkdir()
        (folder / "grokbot-dell.json").write_text(json.dumps({"port": port, "token": token}))
        with patch.object(grokbot, "TUNNEL_DIR", folder), \
                patch("jarvis.actions.DesktopTools", return_value=fake), \
                patch("jarvis.pipeline._local_tools", return_value=fake):  # run_here uses the cached one
            ok, text = self.remote("zenbook").call("dell", "open_app", "Google Chrome")
        self.assertTrue(ok, text)
        self.assertEqual(fake.calls, [("open_app", {"name": "Google Chrome"})])
        # wrong token: refused
        (folder / "grokbot-dell.json").write_text(json.dumps({"port": port, "token": "nope"}))
        with patch.object(grokbot, "TUNNEL_DIR", folder):
            # Windows can abort a fresh localhost connection before the relay
            # replies. Repeating an invalid-token request has no side effects.
            for _ in range(3):
                ok, text = self.remote("zenbook").call("dell", "open_app", "x")
                if "Refused" in text:
                    break
        self.assertFalse(ok)
        self.assertIn("Refused", text)

    def test_no_tunnel_says_how_to_fix_it(self):
        with patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name)):
            ok, text = self.remote("zenbook").call("dell", "open_url", "https://x.com")
        self.assertFalse(ok)
        self.assertIn("grokbot tunnel", text)


class DesktopToolDeviceTests(unittest.TestCase):
    def test_device_argument_goes_to_the_other_laptop(self):
        class R:
            def names(self):
                return ["dell", "zenbook"]

            def here(self):
                return "zenbook"

            def call(self, device, action, arg):
                return True, f"{device}:{action}:{arg}"

        tools = DesktopTools(remote=R())
        spec = next(s for s in tools.specs() if s["name"] == "open_app")
        self.assertEqual(spec["inputSchema"]["properties"]["device"]["enum"], ["dell", "zenbook"])
        self.assertEqual(tools.call("open_app", {"name": "Chrome", "device": "dell"}),
                         (True, "dell:open_app:Chrome"))
        plain = DesktopTools()
        self.assertNotIn("device", plain.specs()[0]["inputSchema"]["properties"])


class AutostartTests(unittest.TestCase):
    def test_only_on_windows_with_an_ssh_device(self):
        with patch.object(grokbot, "WINDOWS", False):
            self.assertIsNone(grokbot.autostart_tunnel(CONFIG, "."))
        with patch.object(grokbot, "WINDOWS", True), \
                patch.object(grokbot.Grokbot, "here", return_value="dell"), \
                patch.object(grokbot.Grokbot, "tunnel") as tunnel:
            thread = grokbot.autostart_tunnel(CONFIG, ".")
            thread.join(2)
            tunnel.assert_called_once()
            off = {**CONFIG, "personal_agents": {"grokbot": {"tunnel_autostart": False}}}
            self.assertIsNone(grokbot.autostart_tunnel(off, "."))


class ClusterFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {**CONFIG, "cluster": {"enabled": True, "local_device": "dell", "main_device": "dell"}}
        self.remote = Remote(self.config, self.tmp.name)

    def test_offline_before_dispatch_uses_ssh(self):
        from jarvis.cluster import NodeOffline
        with patch("jarvis.cluster.dispatch", side_effect=NodeOffline("offline")), \
                patch.object(self.remote, "_ssh", return_value=(True, "opened")) as ssh:
            self.assertEqual(self.remote.call("zenbook", "open_app", "Chrome"), (True, "opened"))
        ssh.assert_called_once_with("zenbook", CONFIG["devices"]["zenbook"], "open_app", "Chrome")

    def test_uncertain_or_denied_cluster_never_retries_over_ssh(self):
        for error in (TimeoutError("may have run"), ConnectionError("hub lost"), ValueError("not enrolled")):
            with patch("jarvis.cluster.dispatch", side_effect=error), patch.object(self.remote, "_ssh") as ssh:
                self.assertFalse(self.remote.call("zenbook", "open_app", "Chrome")[0])
                ssh.assert_not_called()

    def test_online_uses_cluster_only(self):
        with patch("jarvis.cluster.dispatch", return_value={"ok": True, "text": "opened"}), \
                patch.object(self.remote, "_ssh") as ssh:
            self.assertEqual(self.remote.call("zenbook", "open_app", "Chrome"), (True, "opened"))
            ssh.assert_not_called()


class RunHereToolsTests(unittest.TestCase):
    def test_run_here_reuses_the_process_wide_tools(self):
        from jarvis import pipeline
        fake = FakeTools()
        with patch.object(pipeline, "_TOOLS", fake), \
                patch.dict("os.environ", {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}):
            remote.run_here("list_apps", "spot")
            remote.run_here("open_app", "Spotify")
        self.assertEqual(fake.calls, [("list_apps", {"query": "spot"}), ("open_app", {"name": "Spotify"})])


class NodeTerminalGateTests(unittest.TestCase):
    # audit 4: a node ran terminal commands from the hub with no hard_deny of its own.
    def test_node_refuses_hard_denied_terminal_commands(self):
        cfg = {"vault": {"path": {"windows": "C:\\Users\\jdoe\\Documents\\Vault"}}}
        with patch("jarvis.gates.local_config", return_value=cfg), \
                patch("jarvis.terminal.run", side_effect=AssertionError("ran")), \
                patch.dict("os.environ", {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}):
            for arg in ("rm -rf ~", json.dumps({"command": "cat ~/.grok/auth.json"}),
                        "Remove-Item -Recurse -Force C:\\Users\\jdoe\\Documents\\Vault"):
                ok, text = remote.run_here("terminal", arg)
                self.assertFalse(ok, arg)
                self.assertIn("Refused", text)

    def test_node_runs_allowed_terminal_commands(self):
        with patch("jarvis.gates.local_config", return_value={}), \
                patch("jarvis.terminal.run", return_value=(True, "hi")) as run, \
                patch.dict("os.environ", {"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}):
            self.assertEqual(remote.run_here("terminal", "echo hi"), (True, "hi"))
        run.assert_called_once_with("echo hi")


class LocalTargetWithClusterTests(unittest.TestCase):
    def check(self, local):
        cfg = {"devices": {"dell": {"transport": "local", "hostname": "d"},
                           "zenbook": {"transport": "ssh", "target": "x@y", "hostname": "z"}},
               "cluster": {"enabled": True, "main_device": "dell", "local_device": local}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("jarvis.remote.run_here", return_value=(True, "here")) as run, \
                patch("jarvis.cluster.Client", side_effect=AssertionError("Client used")), \
                patch("jarvis.cluster.dispatch", side_effect=AssertionError("dispatch used")):
            r = Remote(cfg, tmp, tools=FakeTools())
            self.assertEqual(r.call(local, "open_app", "x"), (True, "here"))
            self.assertEqual(run.call_count, 1)

    def test_main_runs_its_own_action_locally(self):
        self.check("dell")

    def test_node_runs_its_own_action_locally(self):
        self.check("zenbook")


if __name__ == "__main__":
    unittest.main()
