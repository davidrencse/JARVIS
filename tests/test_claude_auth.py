"""Renewal protocol tests use fake child processes; never a real CLI or account."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import claude_auth as auth


class ClaudeRenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        auth._attempts.clear()
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "account")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(auth._attempts.clear)

    def child(self, source):
        script = self.root / "fake_claude.py"
        script.write_text(source, encoding="utf-8")
        patched = patch.object(auth, "_command", return_value=[sys.executable, str(script)])
        patched.start()
        self.addCleanup(patched.stop)

    def test_only_initialize_is_sent_and_cwd_and_env_are_isolated(self):
        report = self.root / "safe-report.json"
        self.child("import json, os, pathlib, sys\n"
                   "messages = [json.loads(line) for line in sys.stdin]\n"
                   f"pathlib.Path({str(report)!r}).write_text(json.dumps({{'messages':messages,"
                   "'cwd':os.getcwd(),'config':os.environ.get('CLAUDE_CONFIG_DIR'),"
                   "'home':os.environ.get('HOME'),'keys':list(os.environ)}))\n"
                   "print('private-test-output')\n")
        with patch.dict(os.environ, {key: "private-test-secret" for key in auth._AUTH_ENV}), \
                patch.dict(os.environ, {"HOME": "preserved-home"}):
            auth.renew_claude_session()
        data = json.loads(report.read_text())
        self.assertEqual(data["messages"], [{"type": "control_request", "request_id": "jarvis-quota-init",
            "request": {"subtype": "initialize", "hooks": {}}}])
        self.assertEqual(data["config"], os.path.normcase(str(self.root / "account")))
        self.assertEqual(data["home"], "preserved-home")
        self.assertFalse(set(data["keys"]) & auth._AUTH_ENV)
        self.assertFalse(Path(data["cwd"]).exists())
        self.assertNotEqual(data["cwd"], str(self.root))

    def test_timeout_reaps_child_and_closes_stdin(self):
        self.child("import sys,time\nsys.stdin.read()\ntime.sleep(60)\n")
        processes = []
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(auth, "TIMEOUT", 0.25), patch.object(auth.subprocess, "Popen", side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                auth.renew_claude_session()
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)

    def test_failed_exit_is_sanitized_and_cooled_down(self):
        self.child("import sys\nprint('private-test-token',file=sys.stderr)\nsys.exit(3)\n")
        real_run = subprocess.run
        with patch.object(auth.subprocess, "run", wraps=real_run) as run:
            for _ in range(2):
                with self.assertRaises(RuntimeError) as error:
                    auth.renew_claude_session()
                self.assertNotIn("private-test-token", str(error.exception))
        self.assertEqual(run.call_count, 1)

    def test_concurrent_calls_share_one_startup(self):
        self.child("import sys,time\nsys.stdin.read()\ntime.sleep(.1)\n")
        real_run = subprocess.run
        errors = []

        def renew():
            try:
                auth.renew_claude_session()
            except Exception as error:
                errors.append(error)

        with patch.object(auth.subprocess, "run", wraps=real_run) as run:
            threads = [threading.Thread(target=renew) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(run.call_count, 1)

    def test_cooldown_is_scoped_to_config_directory(self):
        self.child("import sys\nsys.stdin.read()\n")
        real_run = subprocess.run
        with patch.object(auth.subprocess, "run", wraps=real_run) as run:
            auth.renew_claude_session()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "other")}):
                auth.renew_claude_session()
        self.assertEqual(run.call_count, 2)

    def test_launch_error_does_not_leak_exception_text(self):
        with patch.object(auth, "_command", side_effect=OSError("private-test-token")):
            with self.assertRaises(RuntimeError) as error:
                auth.renew_claude_session()
        self.assertNotIn("private-test-token", str(error.exception))

    def test_native_cli_flags_disable_customizations_without_disabling_oauth(self):
        with patch.object(auth.shutil, "which", return_value="claude.exe"):
            command = auth._command()
        for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--no-chrome"):
            self.assertIn(flag, command)
        self.assertNotIn("--bare", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        with patch.object(auth.shutil, "which", return_value="claude.cmd"):
            with self.assertRaises(RuntimeError):
                auth._command()


if __name__ == "__main__":
    unittest.main()
