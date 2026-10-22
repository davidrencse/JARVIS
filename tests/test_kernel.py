import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.devices import ssh_inventory
from jarvis.runner import make_plan, run_task
from jarvis.store import Store
from jarvis.voice import speak


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.config = {"workspace_roots": ["."]}
        self.spec = {"objective": "Check the code", "provider": "codex",
                     "workspace": ".", "mode": "read-only"}
        self.store = Store(self.base / "state")

    def test_ledger_persists_and_rejects_duplicate_or_concurrent_claim(self):
        first = self.store.add(self.spec)
        second = self.store.add(self.spec)
        self.store.transition(first["id"], "queued", "running")
        with self.assertRaises(ValueError):
            self.store.transition(first["id"], "queued", "running")
        with self.assertRaises(ValueError):
            self.store.transition(second["id"], "queued", "running")
        self.store.transition(first["id"], "running", "finished")
        self.store.transition(second["id"], "queued", "running")
        reopened = Store(self.base / "state")
        self.assertEqual(reopened.get(first["id"])["state"], "finished")
        self.assertEqual(len(reopened.events(first["id"])), 3)

    @patch("jarvis.runner.shutil.which", return_value=sys.executable)
    def test_plan_enforces_scope_and_role(self, _):
        plan = make_plan(self.spec, self.config, self.base)
        self.assertIn("--sandbox", plan["argv"])
        self.assertIn("read-only", plan["argv"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", plan["argv"])
        with self.assertRaises(ValueError):
            make_plan({**self.spec, "workspace": ".."}, self.config, self.base)
        with self.assertRaises(ValueError):
            make_plan({**self.spec, "role": "reviewer", "mode": "workspace-write"},
                      self.config, self.base)
        claude = make_plan({**self.spec, "provider": "claude"}, self.config, self.base)
        self.assertIn("plan", claude["argv"])
        self.assertIn("--permission-prompts", claude["argv"])

    def fake_plan(self, code):
        return {"argv": [sys.executable, "-c", code], "prompt": "A task",
                "workspace": str(self.base), "provider": "test", "mode": "read-only"}

    def test_actual_process_logs_and_exit_status(self):
        task = self.store.add(self.spec)
        with patch("jarvis.runner.make_plan", return_value=self.fake_plan(
                "import sys; print(sys.stdin.read()); sys.exit(0)")):
            result = run_task(self.store, task["id"], self.config, self.base, 10)
        self.assertEqual(result["state"], "finished")
        self.assertIn("A task", (Path(result["artifacts"]) / "stdout.jsonl").read_text())
        self.assertEqual(self.store.get(task["id"])["state"], "finished")
        with patch("jarvis.runner.make_plan", return_value=self.fake_plan("pass")):
            with self.assertRaises(ValueError):
                run_task(self.store, task["id"], self.config, self.base)

    def test_failed_process_is_not_success(self):
        task = self.store.add(self.spec)
        with patch("jarvis.runner.make_plan", return_value=self.fake_plan(
                "import sys; sys.exit(7)")):
            result = run_task(self.store, task["id"], self.config, self.base, 10)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 7)

    def test_timeout_is_recorded(self):
        task = self.store.add(self.spec)
        with patch("jarvis.runner.make_plan", return_value=self.fake_plan(
                "import time; time.sleep(30)")):
            result = run_task(self.store, task["id"], self.config, self.base, 1)
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(self.store.get(task["id"])["state"], "timed_out")

    def test_missing_binary_releases_claim(self):
        task = self.store.add(self.spec)
        plan = self.fake_plan("pass")
        plan["argv"] = [str(self.base / "does-not-exist")]
        with patch("jarvis.runner.make_plan", return_value=plan):
            with self.assertRaises(OSError):
                run_task(self.store, task["id"], self.config, self.base)
        self.assertEqual(self.store.get(task["id"])["state"], "failed")

    def test_ssh_refuses_option_and_shell_injection(self):
        for target in (None, "-oProxyCommand=evil", "user@host;evil", "user@$(evil)"):
            with self.assertRaises(ValueError):
                ssh_inventory(target)

    @patch("jarvis.devices.shutil.which", return_value="ssh")
    @patch("jarvis.devices.subprocess.run")
    def test_remote_probe_uses_strict_trust_and_fixed_stdin(self, run, _):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"platform":"Linux"}'
        result = ssh_inventory("jdoe@172.1.42.215")
        self.assertEqual(result["source"], "live_ssh_probe")
        argv = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-1], "python3 -")
        self.assertFalse(run.call_args.kwargs["shell"])

    @patch("jarvis.devices.shutil.which", return_value="ssh")
    @patch("jarvis.devices.subprocess.run")
    def test_remote_probe_uses_configured_identity(self, run, _):
        identity = self.base / "test-identity"
        identity.write_text("test fixture, not a key")
        run.return_value.returncode = 0
        run.return_value.stdout = '{"platform":"Linux"}'
        ssh_inventory("jdoe@172.1.42.215", str(identity))
        self.assertIn(str(identity), run.call_args.args[0])
        self.assertIn("IdentitiesOnly=yes", run.call_args.args[0])

    @patch("jarvis.inventory.command")
    def test_display_probe_discovers_single_session_and_drops_serials(self, command):
        from jarvis.inventory import linux_displays
        command.side_effect = [
            {"status": "unavailable"},
            [{"instance": "session-1"}],
            [{"name": "eDP-1", "width": 2880, "height": 1620, "serial": "private"}],
        ]
        result = linux_displays()
        self.assertEqual(result[0]["width"], 2880)
        self.assertNotIn("serial", result[0])
        self.assertEqual(command.call_args.args[0][2], "session-1")

    @patch.dict(os.environ, {}, clear=True)
    @patch("jarvis.voice.urllib.request.urlopen")
    def test_missing_voice_credentials_do_not_make_request(self, urlopen):
        with self.assertRaises(ValueError):
            speak("hello", self.base / "voice.mp3")
        urlopen.assert_not_called()

    @patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key", "ELEVENLABS_VOICE_ID": "test-voice"})
    def test_voice_request_and_no_overwrite(self):
        response = io.BytesIO(b"fake-mp3")
        response.headers = {"Content-Type": "audio/mpeg"}
        output = self.base / "voice.mp3"
        with patch("jarvis.voice.urllib.request.urlopen", return_value=response) as call:
            result = speak("hello", output)
            self.assertEqual(result["bytes"], 8)
            req = call.call_args.args[0]
            self.assertEqual(json.loads(req.data)["text"], "hello")
            self.assertNotIn("test-key", req.full_url)
            with self.assertRaises(FileExistsError):
                speak("again", output)
            self.assertEqual(call.call_count, 1)

    @patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key", "ELEVENLABS_VOICE_ID": "test-voice"})
    def test_invalid_audio_removes_partial_output(self):
        response = io.BytesIO(b"{}")
        response.headers = {"Content-Type": "application/json"}
        output = self.base / "bad.mp3"
        with patch("jarvis.voice.urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                speak("hello", output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
