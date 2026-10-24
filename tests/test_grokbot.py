import json
import os
import threading
import urllib.error
import urllib.request
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import grokbot
from jarvis.grokbot import Grokbot, parse

def which(*found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


class ParseTests(unittest.TestCase):
    def test_slash_picks_mode(self):
        self.assertEqual(parse("/terminal htop"), ("terminal", "htop"))
        self.assertEqual(parse("/browser github.com"), ("browser", "github.com"))
        self.assertEqual(parse("/gb find flights"), ("grokbot-browser", "find flights"))
        self.assertEqual(parse("/jarvis check the weather"), ("jarvis-browser", "check the weather"))

    def test_default_and_selected_mode(self):
        self.assertEqual(parse("summarize my inbox"), ("grokbot", "summarize my inbox"))
        self.assertEqual(parse("ls -la", "terminal"), ("terminal", "ls -la"))
        with self.assertRaises(ValueError):
            parse("x", "rm")


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = Grokbot({}, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @patch.object(grokbot, "_quiet")
    @patch.object(grokbot.shutil, "which", which("kitty"))
    def test_terminal_runs_command_then_stays_open(self, quiet):
        out = self.bot.open_terminal("htop", cwd=self.tmp.name)
        argv = quiet.call_args.args[0]
        self.assertEqual(argv[:5], ["kitty", "--title", "JARVIS", "--directory", self.tmp.name])
        self.assertIn("htop; exec", argv[-1])
        self.assertEqual(out["command"], "htop")

    @patch.object(grokbot, "_quiet")
    @patch.object(grokbot.shutil, "which", which("kitty"))
    def test_terminal_rejects_missing_folder(self, quiet):
        with self.assertRaises(ValueError):
            self.bot.open_terminal("", cwd=os.path.join(self.tmp.name, "nope"))
        quiet.assert_not_called()

    @patch.object(grokbot, "_quiet")
    @patch.object(grokbot.shutil, "which", which("google-chrome-stable"))
    def test_browser_uses_jarvis_profile_and_http_only(self, quiet):
        out = self.bot.open_browser("github.com")
        argv = quiet.call_args.args[0]
        self.assertEqual(argv[-1], "https://github.com")
        self.assertIn(f"--user-data-dir={Path(self.tmp.name).resolve() / 'browser-profile'}", argv)
        self.assertEqual(out["url"], "https://github.com")
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "--disable-web-security"):
            with self.assertRaises(ValueError):
                self.bot.open_browser(bad)


CONFIG = {"devices": {"dell": {"transport": "local", "hostname": "WinLaptop"},
                      "zenbook": {"transport": "ssh", "target": "jdoe@10.0.0.2",
                                  "identity_file": "~/.ssh/jarvis_ed25519",
                                  "hostname": "archmain"}},
          "personal_agents": {"grokbot": {"devices": {"dell": {"os": "windows"},
                                                      "zenbook": {"os": "linux"}}}}}


def done(stdout=b"ok=1\nworkspace=5\n", code=0):
    return MagicMock(stdout=stdout, stderr=b"", returncode=code)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = Grokbot(CONFIG, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_linux_script_is_valid_bash(self):
        out = grokbot.subprocess.run(["bash", "-n"], input=grokbot.LINUX_SCRIPT.encode(),
                                     capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(grokbot, "WINDOWS", False)
    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_refuses_here_outside_hyprland(self, _host):
        with self.assertRaises(ValueError):
            self.bot.send("hello")

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    @patch.object(Grokbot, "can_type", return_value=(True, ""))
    def test_here_pipes_fixed_script_with_prompt_as_data(self, _ok, _host):
        with patch.object(grokbot.subprocess, "run", return_value=done()) as run:
            out = self.bot.send("open my calendar; rm -rf ~", browser=True)
        argv, kw = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv, ["bash", "-s"])
        first, rest = kw["input"].decode().split("\n", 1)
        sent = "Use your browser for this: open my calendar; rm -rf ~"
        self.assertEqual(first, "B64=" + grokbot.base64.b64encode(sent.encode()).decode())
        self.assertEqual(rest, grokbot.LINUX_SCRIPT)
        self.assertEqual(out, {"sent": sent, "to": "grokbot", "device": "zenbook", "workspace": "5"})

    @patch.object(grokbot.socket, "gethostname", return_value="WinLaptop")
    @patch.object(grokbot.shutil, "which", which("ssh"))
    def test_dell_types_to_zenbook_over_ssh(self, _host):
        with patch.object(grokbot.subprocess, "run", return_value=done()) as run:
            out = self.bot.send("hi", device="zenbook")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-2:], ["jdoe@10.0.0.2", "bash -s"])
        self.assertNotIn("hi", " ".join(argv))  # the prompt only travels on stdin
        self.assertEqual(out["device"], "zenbook")

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_no_route_to_dell_says_what_is_missing(self, _host):
        with self.assertRaises(LookupError) as err:
            self.bot.send("hi", device="dell")
        self.assertIn("personal_agents.grokbot.devices.dell.ssh", str(err.exception))
        config = {**CONFIG, "personal_agents": {"grokbot": {"devices": {
            "dell": {"os": "windows", "ssh": {"target": "David@10.0.0.3"}}}}}}
        with self.assertRaises(LookupError):  # Windows over SSH can't reach the desktop
            Grokbot(config, self.tmp.name).send("hi", device="dell")
        with self.assertRaises(LookupError):
            self.bot.send("hi", device="toaster")

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    @patch.object(Grokbot, "can_type", return_value=(True, ""))
    def test_reports_the_scripts_error(self, _ok, _host):
        with patch.object(grokbot.subprocess, "run",
                          return_value=done(b"error=Grok Bot isn't installed there.\n", 4)):
            with self.assertRaisesRegex(RuntimeError, "isn't installed"):
                self.bot.send("hi")

    @patch.object(grokbot.socket, "gethostname", return_value="WinLaptop")
    @patch.object(grokbot, "WINDOWS", True)
    def test_windows_here_runs_powershell_with_prompt_file(self, _host):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            seen["prompt"] = Path(argv[argv.index("-PromptFile") + 1]).read_text(encoding="utf-8")
            return MagicMock(stdout="ok=1\n")
        with patch.object(grokbot.subprocess, "run", side_effect=run):
            out = self.bot.send("hello dell")
        self.assertEqual(seen["argv"][0], "powershell.exe")
        self.assertEqual(seen["prompt"], "hello dell")
        self.assertEqual(out["device"], "dell")

    def test_empty_and_oversized_prompts(self):
        with self.assertRaises(ValueError):
            self.bot.send("   ")
        with self.assertRaises(ValueError):
            self.bot.send("x" * (grokbot.MAX_PROMPT + 1))

    def test_jarvis_browser_is_handed_to_the_brain(self):
        out = self.bot.command("/jarvis check the weather")
        self.assertEqual(out, {"mode": "jarvis-browser",
                               "ask": "Use the browser to do this: check the weather"})

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_devices_lists_routes(self, _host):
        by = {d["id"]: d for d in self.bot.devices()}
        self.assertTrue(by["zenbook"]["ok"] and by["zenbook"]["here"])
        self.assertFalse(by["dell"]["ok"])


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def relay(self, token="t0k"):
        fake = MagicMock()
        fake.send.side_effect = lambda prompt: {"sent": prompt, "to": "grokbot", "device": "dell",
                                                "workspace": None}
        server = grokbot.relay_server(fake, token, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return fake, server.server_address[1]

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_arch_types_to_dell_through_the_tunnel(self, _host):
        fake, port = self.relay()
        Path(self.tmp.name, "grokbot-dell.json").write_text(json.dumps({"port": port, "token": "t0k"}))
        with patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name)):
            bot = Grokbot({**CONFIG, "devices": {"zenbook": {"transport": "local",
                                                             "hostname": "archmain"}}},
                          self.tmp.name)
            self.assertTrue({d["id"]: d for d in bot.devices()}["dell"]["ok"])
            out = bot.send("hi dell", device="dell")
        fake.send.assert_called_once_with("hi dell")
        self.assertEqual(out["device"], "dell")

    def test_relay_refuses_wrong_token_and_other_paths(self):
        fake, port = self.relay()
        for path, token in (("/send", "nope"), ("/send", ""), ("/api/grokbot", "t0k")):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b'{"prompt":"x"}',
                                         headers={"X-JARVIS-Token": token})
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(err.exception.code, 403)
        fake.send.assert_not_called()

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_down_tunnel_is_reported(self, _host):
        Path(self.tmp.name, "grokbot-dell.json").write_text('{"port": 1, "token": "x"}')
        with patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name)):
            bot = Grokbot(CONFIG, self.tmp.name)
            self.assertFalse({d["id"]: d for d in bot.devices()}["dell"]["ok"])
            with self.assertRaises(ConnectionError):
                bot.send("hi", device="dell")

    @patch.object(grokbot.socket, "gethostname", return_value="WinLaptop")
    @patch.object(grokbot.shutil, "which", which("ssh"))
    def test_tunnel_writes_token_then_forwards_loopback_only(self, _host):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return MagicMock(returncode=0, stderr=b"")
        with patch.object(grokbot.subprocess, "run", side_effect=run), \
                patch.object(grokbot, "relay_server") as relay:
            Grokbot(CONFIG, self.tmp.name).tunnel(once=True)
        token = relay.call_args.args[1]
        put, forward, withdraw = calls
        self.assertIn("umask 077", put[0][-1])
        self.assertEqual(json.loads(put[1]), {"port": 7778, "token": token, "from": "dell"})
        self.assertIn("127.0.0.1:7778:127.0.0.1:7779", forward[0])
        self.assertIn("ExitOnForwardFailure=yes", forward[0])
        self.assertEqual(withdraw[0][-1], "rm -f .config/jarvis/grokbot-dell.json")


class StatusTests(unittest.TestCase):
    def test_never_reads_the_credential_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "local-exec-daemon.json").write_text('{"pid": %d}' % os.getpid())
            (data / "local-computer-use-gate.json").write_text('{"isOn": true}')
            (data / "local-exec-daemon-credential.json").write_text("SECRET")
            opened = []
            real = Path.read_text

            def spy(self, *a, **kw):
                opened.append(self.name)
                return real(self, *a, **kw)
            with patch.object(grokbot, "DATA", data), patch.object(Path, "read_text", spy), \
                    patch.object(Grokbot, "window", return_value=None):
                st = Grokbot({}, tmp).status()
        self.assertTrue(st["daemon"])
        self.assertTrue(st["computer_use"])
        self.assertNotIn("local-exec-daemon-credential.json", opened)


if __name__ == "__main__":
    unittest.main()
