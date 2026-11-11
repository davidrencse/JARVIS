"""Audit 2026-11-04 (#4): the terminal sandbox is an allowlist. Read-only root, a few writable
work folders, the JARVIS repo read-only as a whole, no display/audio sockets, own PID namespace."""
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import terminal


class SandboxAllowlist(unittest.TestCase):
    def setUp(self):
        if not terminal.shutil.which("bwrap") or os.name == "nt":
            self.skipTest("bwrap not available here")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for sub in ("Downloads", "Documents/obsidian-vault", "projects/JARVIS/.jarvis", "projects/other/.git/hooks",
                    ".local/bin", ".grok", ".hermes", ".config/openclaw", ".config/Grok Bot", ".config/Codex",
                    ".config/Cursor", ".config/gogcli", ".config/google-chrome-for-testing", ".config/systemd"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "projects/other/.git/config").write_text("")
        self.repo = self.home / "projects/JARVIS"
        self.runtime = self.home / "run"
        for name in ("wayland-1", "pipewire-0", "pulse", "bus"):
            (self.runtime / name).mkdir(parents=True)
        env = {"HOME": str(self.home), "XDG_RUNTIME_DIR": str(self.runtime)}
        with mock.patch.dict(os.environ, env):
            self.args = terminal.sandbox("/tmp", repo=self.repo,
                                         config={"vault": {"path": {"linux": "~/Documents/obsidian-vault"}}})
        self.joined = " ".join(self.args)

    def tearDown(self):
        self.tmp.cleanup()

    def pairs(self, flag):
        return [self.args[i + 1] for i, a in enumerate(self.args) if a == flag]

    def test_root_is_read_only(self):
        self.assertIn("--ro-bind / /", self.joined)
        self.assertNotIn("--bind / /", self.joined.replace("--ro-bind / /", ""))

    def test_only_allowlisted_folders_are_writable(self):
        writable = self.pairs("--bind")
        self.assertIn(str(self.home / "Downloads"), writable)
        self.assertIn(str(self.home / "projects"), writable)
        self.assertIn("/tmp", writable)
        for bad in (str(self.home), str(self.home / ".local"), str(self.home / ".local/bin"),
                    str(self.home / ".config"), str(self.repo)):
            self.assertNotIn(bad, writable)

    def test_repo_vault_and_hooks_read_only_after_writable_parents(self):
        read_only = self.pairs("--ro-bind")
        for path in (self.repo, self.home / "Documents/obsidian-vault", self.home / "projects/other/.git/hooks",
                     self.home / "projects/other/.git/config"):
            self.assertIn(str(path), read_only)
        projects = self.args.index(str(self.home / "projects"))
        self.assertGreater(self.args.index(str(self.repo)), projects)  # later mount wins

    def test_secrets_and_state_hidden_last(self):
        hidden = self.pairs("--tmpfs")
        for sub in (".grok", ".hermes", ".config/openclaw", ".config/Grok Bot", ".config/Codex", ".config/Cursor",
                    ".config/gogcli", ".config/google-chrome-for-testing", "projects/JARVIS/.jarvis"):
            self.assertIn(str(self.home / sub), hidden)
        self.assertGreater(self.args.index(str(self.repo / ".jarvis")), self.args.index(str(self.repo)))

    def test_no_display_audio_or_session_sockets(self):
        self.assertIn(str(self.runtime), self.pairs("--tmpfs"))
        for name in ("wayland", "pipewire", "pulse", "/bus"):
            self.assertNotIn(str(self.runtime) + "/" + name.strip("/"), self.joined)
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS"):
            self.assertIn(name, self.pairs("--unsetenv"))

    def test_own_pid_namespace(self):
        self.assertIn("--unshare-pid", self.args)
        self.assertIn("--die-with-parent", self.args)
        self.assertEqual(self.args[-1], "--")

    def test_host_sockets_are_covered(self):
        path = str(self.home / "Downloads" / "s.sock")
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(path)
            self.assertEqual(terminal._sockets(self.home), [path])
        finally:
            sock.close()
        with mock.patch.object(terminal, "SOCKET_DIRS", ("/fake",)), \
                mock.patch.object(terminal, "_sockets", lambda top: ["/fake/docker.sock"]):
            args = terminal.sandbox("/tmp", repo=self.repo, config={})
        self.assertIn("--ro-bind /dev/null /fake/docker.sock", " ".join(args))


if __name__ == "__main__":
    unittest.main()
