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
                return subprocess.CompletedProcess(args, 255, "", "No route to host")
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
                patch("jarvis.actions.DesktopTools", return_value=fake):
            ok, text = self.remote("zenbook").call("dell", "open_app", "Google Chrome")
        self.assertTrue(ok, text)
        self.assertEqual(fake.calls, [("open_app", {"name": "Google Chrome"})])
        # wrong token: refused
        (folder / "grokbot-dell.json").write_text(json.dumps({"port": port, "token": "nope"}))
        with patch.object(grokbot, "TUNNEL_DIR", folder):
            ok, text = self.remote("zenbook").call("dell", "open_app", "x")
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


if __name__ == "__main__":
    unittest.main()
