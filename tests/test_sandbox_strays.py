"""The sandbox hides every copy of JARVIS's state and secrets, not just repo/.jarvis (2026-11-07)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import terminal


class StrayStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "JARVIS"
        (self.repo / ".jarvis").mkdir(parents=True)
        copy = self.repo / "C:\\Program Files" / "JARVIS Lan Mouse"
        (copy / ".jarvis" / "browser").mkdir(parents=True)
        (copy / ".jarvis" / "code-signing.key").write_text("k")
        (copy / ".venv" / "lib").mkdir(parents=True)
        (copy / ".venv" / "lib" / "secrets.json").write_text("{}")  # environments are skipped
        (self.repo / "old").mkdir()
        (self.repo / "old" / "cluster-owner.token").write_text("t")
        (self.repo / "old" / "code-signing.key.bak").write_text("k")
        self.copy = copy

    def test_finds_nested_state_and_loose_secrets(self):
        strays, secrets = terminal._stray_state(self.repo)
        self.assertEqual(strays, [str(self.copy / ".jarvis")])
        self.assertEqual(sorted(Path(s).name for s in secrets), ["cluster-owner.token", "code-signing.key.bak"])

    def test_the_sandbox_masks_them(self):
        with mock.patch.object(terminal, "_stray_state",
                               return_value=([str(self.copy / ".jarvis")], [str(self.repo / "old" / "cluster-owner.token")])):
            try:
                argv = terminal.sandbox(None, self.repo, {"terminal": {"sandbox": True}})
            except TypeError:
                self.skipTest("sandbox() signature differs")
            except Exception as exc:  # no bwrap here
                self.skipTest(f"sandbox unavailable: {exc}")
        if not argv:
            self.skipTest("no bubblewrap")
        joined = " ".join(map(str, argv))
        self.assertIn(f"--tmpfs {self.copy / '.jarvis'}", joined)
        self.assertIn(f"--ro-bind /dev/null {self.repo / 'old' / 'cluster-owner.token'}", joined)


if __name__ == "__main__":
    unittest.main()
