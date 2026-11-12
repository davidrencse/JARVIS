"""Tests must not touch this machine's login startup.

The audit hook in jarvis/__init__.py refuses service-manager launches and writes into
the real autostart directories. These checks use a program path and a unit path that
do not exist, so a missed guard fails the assertion instead of changing the machine.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jarvis


REFUSAL = ("real autostart is off while tests run (JARVIS_NO_MODELS). "
           "Patch jarvis.autostart.install or use a temp home.")
# Not a real binary. The stem is what the hook reads; exec never happens.
MISSING = "/no/such/jarvis-autostart-guard/{}"


class NoRealAutostartTests(unittest.TestCase):
    def test_service_managers_are_refused(self):
        for name in ("systemctl", "launchctl", "schtasks", "loginctl", "reg"):
            program = MISSING.format(name)
            with self.assertRaises(RuntimeError) as caught:
                subprocess.run([program, "--user", "status"], check=False)
            self.assertIn(REFUSAL, str(caught.exception), name)
            self.assertIn(name, str(caught.exception), name)
        with self.assertRaises(RuntimeError) as caught:
            subprocess.run(MISSING.format("systemctl") + " --user status", shell=True)
        self.assertIn(REFUSAL, str(caught.exception))

    def test_a_mocked_subprocess_never_reaches_the_hook(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(["systemctl"], 0)) as run:
            subprocess.run(["systemctl", "--user", "daemon-reload"])
        run.assert_called_once()

    def test_write_into_the_real_systemd_user_dir_is_refused(self):
        # Frozen at hook install from the account home, not from a later HOME patch.
        root = os.path.normcase(str(Path(jarvis._account_home()) / ".config" / "systemd" / "user"))
        self.assertIn(root, jarvis._AUTOSTART_ROOTS)
        # A name systemd will not load, and a path this test never creates.
        target = Path(root) / "jarvis-guard-probe-does-not-exist"
        self.assertFalse(target.exists(), target)
        try:
            with self.assertRaises(RuntimeError) as caught:
                target.write_text("never written", encoding="utf-8")
            self.assertIn(REFUSAL, str(caught.exception))
            self.assertFalse(target.exists(), target)
            with self.assertRaises(RuntimeError):
                target.unlink(missing_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                outside = Path(tmp) / "moved-in"
                outside.write_text("x", encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    os.rename(outside, target)
            self.assertFalse(target.exists(), target)
        finally:
            # A missed guard would have created this file. Remove only that accident.
            if target.exists():
                target.unlink()

    def test_a_temp_home_is_writable_and_the_real_path_stays_blocked(self):
        real = (Path.home() / ".config" / "systemd" / "user"
                / "jarvis-guard-probe-does-not-exist")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp, "XDG_CONFIG_HOME": tmp}):
            allowed = Path(tmp) / ".config" / "systemd" / "user" / "jarvis-cluster.service"
            allowed.parent.mkdir(parents=True)
            allowed.write_text("[Unit]\nDescription=temp\n", encoding="utf-8")
            self.assertTrue(allowed.is_file())
            allowed.unlink()
            with self.assertRaises(RuntimeError) as caught:
                real.write_text("never written", encoding="utf-8")
            self.assertIn(REFUSAL, str(caught.exception))
        self.assertFalse(real.exists())


if __name__ == "__main__":
    unittest.main()
