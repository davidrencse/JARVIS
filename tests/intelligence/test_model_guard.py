"""The suite must never reach a real model: each codex or claude run spends David's quota."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jarvis
from jarvis.cluster import Client

ROOT = Path(__file__).resolve().parents[2]


class ModelGuardTests(unittest.TestCase):
    def test_guard_is_on_and_exported(self):
        self.assertTrue(getattr(sys, "_jarvis_models_blocked", False))
        self.assertEqual(os.environ.get("JARVIS_NO_MODELS"), "1")

    def test_real_model_clis_cannot_start(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("codex", "claude", "codex.exe"):
                with self.assertRaises(PermissionError, msg=name):
                    subprocess.Popen([str(Path(d) / name), "exec", "hi"])
                with self.assertRaises(PermissionError, msg=name):
                    subprocess.run(name + " --print hi", shell=True)  # shell string form too
                with self.assertRaises(PermissionError, msg=name):
                    subprocess.Popen(["anything"], executable=str(Path(d) / name))

    def test_launchers_and_os_process_calls_are_caught(self):
        for start in (lambda: subprocess.Popen(["env", "codex", "exec", "hi"]),
                      lambda: subprocess.Popen(["npx", "@anthropic-ai/claude-code"]),
                      lambda: subprocess.Popen(["node", "/lib/node_modules/codex/bin/codex.js"]),
                      lambda: subprocess.Popen(["ssh", "dell", "claude --print hi"]),
                      lambda: subprocess.Popen(["/opt/codex-x86_64-unknown-linux-musl"]),
                      lambda: os.system("codex exec hi"),
                      lambda: os.execvp("claude", ["claude", "--print", "hi"]),
                      lambda: os.posix_spawnp("codex", ["codex"], dict(os.environ))):
            with self.assertRaises(PermissionError):
                start()

    def test_a_child_given_its_own_environment_is_still_guarded(self):
        env = {"PATH": os.environ.get("PATH", "")}
        done = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('JARVIS_NO_MODELS'))"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "1")

    def test_fakes_still_run(self):
        done = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "ok")

    def test_child_jarvis_processes_are_guarded(self):
        code = "import sys, jarvis, subprocess\ntry:\n subprocess.Popen(['codex'])\nexcept PermissionError:\n print('blocked')"
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                              env={**os.environ, "JARVIS_NO_MODELS": "1"})
        self.assertEqual(done.stdout.strip(), "blocked", done.stderr)

    def test_real_cluster_credentials_are_off_limits(self):
        config = {"cluster": {"enabled": True, "main_device": "a", "local_device": "b"},
                  "devices": {"a": {}, "b": {}}}
        with self.assertRaises(PermissionError):
            Client(config, ROOT / ".jarvis")

    def test_detection(self):
        self.assertTrue(jarvis._under_tests())


if __name__ == "__main__":
    unittest.main()
