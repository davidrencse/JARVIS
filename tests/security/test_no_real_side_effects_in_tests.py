"""Tests must not open a real shell, Tailscale, a desktop notification, or the real home.

The audit hook in jarvis/__init__.py refuses those while the suite runs. Every program
path below does not exist, so a missed guard fails the assertion instead of contacting
a host or writing a file. Loopback ssh/scp/sftp is allowed through the hook and then
fails because that program path is missing.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jarvis


REMOTE = ("real ssh, scp, and sftp are off while tests run (JARVIS_NO_MODELS) "
          "unless the destination is loopback. Patch the call or fake the transport.")
TAILSCALE = ("real tailscale is off while tests run (JARVIS_NO_MODELS). "
             "Patch the call or fake it.")
NOTIFY = "tests must patch jarvis notification to a fake."
HOME_WRITE = ("real ~/.local/bin and ~/.config/systemd are off while tests run "
              "(JARVIS_NO_MODELS). Patch the writer or use a temp home.")
AUTOSTART = ("real autostart is off while tests run (JARVIS_NO_MODELS). "
             "Patch jarvis.autostart.install or use a temp home.")
# Not a real binary. The stem is what the hook reads; exec never happens.
MISSING = "/no/such/jarvis-guard/{}"
PROBE = "jarvis-side-effect-probe-does-not-exist"


def _program(name):
    return MISSING.format(name)


class NoRealSideEffectTests(unittest.TestCase):
    def _refused(self, argv, message, program):
        with self.assertRaises(PermissionError) as caught:
            subprocess.run(argv, check=False)
        text = str(caught.exception)
        self.assertIn(message, text)
        self.assertIn(program, text)

    def _not_refused(self, argv):
        # The hook let it through. The path is not a real program, so the OS answers.
        with self.assertRaises(FileNotFoundError):
            subprocess.run(argv, check=False)

    def test_public_and_unparsable_destinations_are_refused(self):
        refused = [
            ["ssh", "dell"],
            ["ssh", "8.8.8.8"],
            ["ssh", "user@example.com"],
            ["ssh.exe", "public.example"],
            ["ssh"],  # no destination
            ["ssh", "-p"],  # option value missing
            ["ssh", "-4"],  # flags only
            ["scp", "a", "dell:b"],
            ["scp", "a", "8.8.8.8:b"],
            ["scp", "a", "user@example.com:b"],
            ["scp"],
            ["scp", "user@:b"],
            ["sftp", "dell"],
            ["sftp", "8.8.8.8"],
            ["sftp", "user@example.com:dir"],
            ["sftp.exe", "public.example"],
            ["sftp"],
            ["sftp", "-o"],
        ]
        for args in refused:
            with self.subTest(args=args):
                self._refused([_program(args[0]), *args[1:]], REMOTE, Path(args[0]).stem.lower())

    def test_loopback_ssh_scp_and_sftp_are_not_refused(self):
        allowed = [
            ["ssh", "127.0.0.1"],
            ["ssh", "127.8.8.8"],  # the whole 127.0.0.0/8, not only .1
            ["ssh", "localhost"],
            ["ssh", "LocalHost"],
            ["ssh", "::1"],
            ["ssh", "[::1]"],
            ["ssh", "user@127.0.0.1"],
            ["ssh", "user@[::1]"],
            ["ssh", "-p", "22", "-o", "StrictHostKeyChecking=no", "127.0.0.1"],
            ["ssh", "-p22", "localhost"],
            ["ssh", "-4", "127.0.0.1"],
            ["ssh", "-J", "jump.example", "127.0.0.1"],
            ["ssh.exe", "127.0.0.1"],
            ["scp", "a", "127.0.0.1:b"],
            ["scp", "a", "localhost:b"],
            ["scp", "a", "::1:b"],
            ["scp", "a", "[::1]:b"],
            ["scp", "-P", "22", "a", "user@127.0.0.1:b"],
            ["scp.exe", "a", "localhost:b"],
            ["sftp", "127.0.0.1"],
            ["sftp", "localhost"],
            ["sftp", "::1"],
            ["sftp", "[::1]"],
            ["sftp", "user@localhost"],
            ["sftp", "-P", "22", "127.0.0.1"],
        ]
        for args in allowed:
            with self.subTest(args=args):
                self._not_refused([_program(args[0]), *args[1:]])

    def test_launchers_are_caught_and_loopback_through_them_is_not(self):
        self._refused([_program("bash"), "-c", "ssh dell uptime"], REMOTE, "ssh")
        self._refused([_program("env"), "scp", "a", "dell:b"], REMOTE, "scp")
        self._refused([_program("bash"), "-c", "sftp 8.8.8.8"], REMOTE, "sftp")
        self._not_refused([_program("bash"), "-c", "ssh 127.0.0.1 true"])
        self._not_refused([_program("env"), "scp", "a", "127.0.0.1:b"])
        self._not_refused([_program("env"), "sftp", "localhost"])

    def test_tailscale_and_notify_send_are_refused(self):
        for name in ("tailscale", "tailscaled", "tailscale.exe"):
            with self.subTest(name=name):
                self._refused([_program(name), "status"], TAILSCALE, Path(name).stem.lower())
        self._refused([_program("env"), "tailscale", "status"], TAILSCALE, "tailscale")
        self._refused([_program("notify-send"), "hi"], NOTIFY, "notify-send")
        self._refused([_program("notify-send.exe"), "hi"], NOTIFY, "notify-send")
        self._refused([_program("bash"), "-c", "notify-send hi"], NOTIFY, "notify-send")

    def test_a_mocked_subprocess_never_reaches_the_hook(self):
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess(["ssh"], 0)) as run:
            subprocess.run(["ssh", "dell", "uptime"])
        run.assert_called_once()

    def _assert_missing(self, path):
        self.assertFalse(path.exists(), path)

    def _refuse_write(self, path, message):
        self._assert_missing(path)
        try:
            with self.assertRaises(RuntimeError) as caught:
                path.write_text("never written", encoding="utf-8")
            self.assertIn(message, str(caught.exception))
            self._assert_missing(path)
            with self.assertRaises(RuntimeError) as caught:
                path.unlink(missing_ok=True)
            self.assertIn(message, str(caught.exception))
            with tempfile.TemporaryDirectory() as tmp:
                outside = Path(tmp) / "moved-in"
                outside.write_text("x", encoding="utf-8")
                with self.assertRaises(RuntimeError) as caught:
                    os.rename(outside, path)
                self.assertIn(message, str(caught.exception))
            self._assert_missing(path)
        finally:
            # A missed guard would have created this file. Remove only that accident.
            if path.exists():
                path.unlink()

    def test_real_local_bin_and_systemd_tree_are_refused(self):
        home = Path(jarvis._account_home())
        bin_root = os.path.normcase(str(home / ".local" / "bin"))
        tree_root = os.path.normcase(str(home / ".config" / "systemd"))
        self.assertIn(bin_root, jarvis._HOME_WRITE_ROOTS)
        self.assertIn(tree_root, jarvis._HOME_WRITE_ROOTS)
        self._refuse_write(home / ".local" / "bin" / PROBE, HOME_WRITE)
        self._refuse_write(home / ".config" / "systemd" / PROBE, HOME_WRITE)
        # The whole tree, not only the directory itself.
        self._refuse_write(home / ".config" / "systemd" / "system" / PROBE, HOME_WRITE)
        # The user unit directory keeps the autostart message.
        user = os.path.normcase(str(home / ".config" / "systemd" / "user"))
        self.assertIn(user, jarvis._AUTOSTART_ROOTS)
        self._refuse_write(home / ".config" / "systemd" / "user" / PROBE, AUTOSTART)

    def test_a_temp_home_is_writable_and_the_real_paths_stay_blocked(self):
        home = Path(jarvis._account_home())
        real_bin = home / ".local" / "bin" / PROBE
        real_tree = home / ".config" / "systemd" / PROBE
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp}):
            for relative in ((".local", "bin", "jarvis-guard-ok"),
                             (".config", "systemd", "jarvis-guard-ok"),
                             (".config", "systemd", "user", "jarvis-guard-ok.service")):
                allowed = Path(tmp).joinpath(*relative)
                allowed.parent.mkdir(parents=True, exist_ok=True)
                allowed.write_text("ok", encoding="utf-8")
                renamed = allowed.with_name(allowed.name + "-2")
                os.rename(allowed, renamed)
                renamed.unlink()
                self.assertFalse(renamed.exists())
            with self.assertRaises(RuntimeError) as caught:
                real_bin.write_text("never written", encoding="utf-8")
            self.assertIn(HOME_WRITE, str(caught.exception))
            with self.assertRaises(RuntimeError) as caught:
                real_tree.write_text("never written", encoding="utf-8")
            self.assertIn(HOME_WRITE, str(caught.exception))
        self.assertFalse(real_bin.exists())
        self.assertFalse(real_tree.exists())


if __name__ == "__main__":
    unittest.main()
