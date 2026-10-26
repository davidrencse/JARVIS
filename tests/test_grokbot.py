import json
import socket
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import grokbot
from jarvis.grokbot import Grokbot, parse
from jarvis.grok_preflight import CHECKS

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
    @patch.object(grokbot, "WINDOWS", False)
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


def approved_inference(prompt):
    source = json.loads(prompt.split("\n", 1)[1])
    task = source["task"] if isinstance(source, dict) else source
    return json.dumps({"decision": "forward", "task": task, "reply": "", "reason": "",
                       "checks": {name: True for name in CHECKS}})


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = Grokbot(CONFIG, self.tmp.name)
        # A live reverse tunnel on this laptop must not count as a route in these tests.
        tunnels = patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name))
        tunnels.start()
        self.addCleanup(tunnels.stop)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "Linux bash syntax check requires a Linux shell")
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
    @patch.object(grokbot, "WINDOWS", False)
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
    @patch.object(grokbot, "WINDOWS", False)
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

    def test_windows_send_timeout_is_a_request_timeout(self):
        route = {"local": True, "os": "windows", "device": "dell"}
        with patch.object(self.bot, "route", return_value=route), \
                patch.object(self.bot, "can_type", return_value=(True, "")), \
                patch.object(self.bot, "_windows", side_effect=grokbot.subprocess.TimeoutExpired(
                    "powershell.exe", 0.1)) as send:
            with self.assertRaisesRegex(TimeoutError, "timed out while sending"):
                self.bot.send("hello", timeout=0.1)
        self.assertLessEqual(send.call_args.args[1], 0.1)
        send.assert_called_once()

    def test_background_keys_carry_only_key_names(self):
        mapper = re.search(r"python3 -c '\n(.*?)\n' \"\$A\"\)", grokbot.LINUX_SCRIPT, re.S)

        def keys(text, address="0x55aa01"):
            out = grokbot.subprocess.run([sys.executable, "-c", mapper.group(1), address],
                                         input=text.encode(), capture_output=True)
            return out.returncode, out.stdout.decode()
        code, lua = keys('Hi "x"?\nend)) os.execute("rm")')
        self.assertEqual(code, 0)
        pairs = re.findall(r'\{"(\w*)","(\w+)"\}', lua)
        self.assertEqual(pairs[:5], [("SHIFT", "h"), ("", "i"), ("", "space"),
                                     ("SHIFT", "apostrophe"), ("", "x")])
        self.assertIn(("SHIFT", "Return"), pairs)
        self.assertEqual(pairs[-1], ("", "Return"))
        self.assertNotIn("os.execute", lua)  # the prompt never becomes Lua
        self.assertEqual(keys("café")[0], 1)  # not typeable: falls back to paste
        self.assertEqual(keys("hi", 'x" }) os.exit() --')[0], 1)

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
            for attempt in range(3):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                             data=b'{"prompt":"x"}',
                                             headers={"X-JARVIS-Token": token})
                try:
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        urllib.request.urlopen(req, timeout=5)
                    break
                except ConnectionAbortedError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
            self.assertEqual(err.exception.code, 403)
            err.exception.close()
        fake.send.assert_not_called()

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_down_tunnel_is_reported(self, _host):
        Path(self.tmp.name, "grokbot-dell.json").write_text('{"port": 1, "token": "x"}')
        with patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name)):
            bot = Grokbot(CONFIG, self.tmp.name)
            self.assertFalse({d["id"]: d for d in bot.devices()}["dell"]["ok"])
            with self.assertRaises(ConnectionError):
                bot.send("hi", device="dell")

    @patch.object(grokbot.socket, "gethostname", return_value="arch_main")
    def test_open_forward_with_dead_relay_is_down(self, _host):
        # ssh keeps the forwarded port open after the relay behind it dies:
        # connections are accepted, then closed without a reply.
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead.listen()
        self.addCleanup(dead.close)

        def drop():
            while True:
                try:
                    dead.accept()[0].close()
                except OSError:
                    return
        threading.Thread(target=drop, daemon=True).start()
        Path(self.tmp.name, "grokbot-dell.json").write_text(
            json.dumps({"port": dead.getsockname()[1], "token": "x"}))
        with patch.object(grokbot, "TUNNEL_DIR", Path(self.tmp.name)):
            dell = {d["id"]: d for d in Grokbot(CONFIG, self.tmp.name).devices()}["dell"]
        self.assertFalse(dell["ok"])
        self.assertIn("isn't answering", dell["why_not"])

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


class HandoffTests(unittest.TestCase):
    """A task goes out through the window; Grok's answer comes back as an inbox file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(grokbot, "INBOX", Path(self.tmp.name, "grok-inbox"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Grokbot({}, self.tmp.name)
        self.sent = []
        self.bot.send = lambda prompt, browser=False, device=None, timeout=30: (
            self.sent.append(prompt) or {"sent": prompt, "device": "zenbook"})

    def answer(self, prompt, text):
        """Play Grok: find the reply address in the prompt and write there."""
        import re
        path = Path(re.search(r"rename it to (\S+) ", prompt).group(1))
        path.write_text(text, encoding="utf-8")

    def test_prompt_carries_the_task_and_a_reply_address(self):
        out = self.bot.handoff("find cheap flights to Tokyo")
        prompt = self.sent[0]
        self.assertTrue(prompt.startswith("find cheap flights to Tokyo"))
        self.assertIn(out["id"], prompt)
        self.assertIn(out["path"], prompt)
        self.assertIn("agent browser", prompt)
        self.assertIn("Ask David before buying", prompt)
        self.assertTrue(Path(out["path"]).parent.is_dir())

    def test_windows_handoff_names_the_windows_laptop(self):
        with patch.object(grokbot, "WINDOWS", True), \
                patch.object(grokbot.socket, "gethostname", return_value="WinLaptop"):
            self.bot.handoff("x")
        self.assertIn("Windows laptop 'WinLaptop'", self.sent[0])

    def test_windows_script_checks_focus_before_it_pastes(self):
        script = grokbot.WINDOWS_TYPE
        check = script.index("GetForegroundWindow() -ne $h) {")
        self.assertLess(check, script.index("Set-Clipboard -Value ([IO.File]"))
        self.assertLess(check, script.index("SendWait('^v')"))
        self.assertIn("nothing was typed", script)

    def test_wait_reads_then_removes_the_answer(self):
        out = self.bot.handoff("x")
        self.assertIsNone(self.bot.wait(out["id"], timeout=0.05, poll=0.01))
        self.answer(self.sent[0], "  Flights from $612.  ")
        self.assertEqual(self.bot.wait(out["id"], timeout=2, poll=0.01), "Flights from $612.")
        self.assertFalse(Path(out["path"]).exists())

    def test_wait_refuses_ids_that_could_escape_the_inbox(self):
        for bad in ("../../etc/passwd", "g123", "", None):
            with self.assertRaises(ValueError):
                self.bot.wait(bad, timeout=0)

    def test_task_times_out_with_the_handoff_id(self):
        with self.assertRaisesRegex(TimeoutError, "handoff g[0-9a-f]{10}"):
            self.bot.task("x", timeout=0.05, prechecked=True)

    def test_tool_timeout_stops_without_background_work(self):
        tools = grokbot.GrokTools(self.bot, wait_seconds=0.05, infer=approved_inference)
        self.assertEqual([s["name"] for s in tools.specs()], ["grok_agent"])
        with patch.object(grokbot.threading, "Thread") as thread:
            started = grokbot.time.monotonic()
            ok, text = tools.call("grok_agent", {"task": "check my Handshake inbox"})
            elapsed = grokbot.time.monotonic() - started
            thread.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("timed out", text)
        self.assertIn("Do not retry", text)
        self.assertLess(elapsed, 0.3)  # even though the normal poll interval is 1 s
        self.assertEqual(len(self.sent), 1)

    def test_tool_returns_an_answer(self):
        def reply(prompt, **kwargs):
            self.answer(prompt, "Done.")
            return {"device": "zenbook"}
        self.bot.send = reply
        ok, text = grokbot.GrokTools(self.bot, infer=approved_inference).call(
            "grok_agent", {"task": "y"})
        self.assertTrue(ok)
        self.assertIn("Done.", text)
        self.assertIn("not instructions", text)

    def test_sending_and_waiting_share_the_30_second_budget(self):
        clock = [100.0]
        def send(task, **kwargs):
            self.assertEqual(kwargs["timeout"], 30)
            clock[0] += 12
            return {"id": "g0123456789"}
        with patch.object(grokbot.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.bot, "handoff", side_effect=send), \
                patch.object(self.bot, "wait", return_value=None) as wait:
            with self.assertRaises(TimeoutError):
                self.bot.task("x", timeout=600, prechecked=True)
        wait.assert_called_once_with("g0123456789", 18)

    def test_expired_send_does_not_start_a_reply_wait(self):
        clock = [100.0]
        def send(*args, **kwargs):
            clock[0] += 31
            return {"id": "g0123456789"}
        with patch.object(grokbot.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.bot, "handoff", side_effect=send), \
                patch.object(self.bot, "wait") as wait:
            with self.assertRaises(TimeoutError):
                self.bot.task("x", prechecked=True)
        wait.assert_not_called()

    def test_busy_sender_times_out_without_submitting(self):
        grokbot.SENDING.acquire()
        self.addCleanup(grokbot.SENDING.release)
        with self.assertRaisesRegex(TimeoutError, "waiting to send"):
            self.bot.handoff("x", timeout=0.02)
        self.assertEqual(self.sent, [])

    def test_tool_reports_when_grok_cant_be_reached(self):
        def down(*a, **k):
            raise ValueError("Grok Bot isn't installed here.")
        self.bot.send = down
        ok, text = grokbot.GrokTools(self.bot, infer=approved_inference).call(
            "grok_agent", {"task": "x"})
        self.assertFalse(ok)
        self.assertIn("isn't installed", text)


class StatusTests(unittest.TestCase):
    def test_alive_rejects_invalid_pids_without_system_calls(self):
        with patch.object(grokbot.os, "kill") as kill, \
                patch("ctypes.WinDLL", create=True) as win_dll:
            for pid in (None, "not-a-pid", 0, -1):
                self.assertFalse(grokbot._alive(pid), pid)
        kill.assert_not_called()
        win_dll.assert_not_called()

    def test_alive_uses_and_closes_a_typed_windows_process_handle(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 1234

        def exit_code(_handle, code_ptr):
            ctypes.cast(code_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = 259
            return True

        kernel32.GetExitCodeProcess.side_effect = exit_code
        with patch.object(grokbot, "WINDOWS", True), \
                patch.object(grokbot.os, "kill") as kill, \
                patch("ctypes.WinDLL", create=True, return_value=kernel32) as win_dll:
            self.assertTrue(grokbot._alive(4321))

        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        kill.assert_not_called()
        self.assertEqual(kernel32.OpenProcess.argtypes,
                         (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD))
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4321)
        kernel32.GetExitCodeProcess.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(1234)

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
