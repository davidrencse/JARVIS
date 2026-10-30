import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import fsio


@unittest.skipIf(os.name == "nt", "POSIX permission bits")
class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_private_file_is_owner_only_from_creation(self):
        born, real = [], os.fdopen

        def spy(fd, *args, **kwargs):  # the mode the temporary file had when first opened
            born.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return real(fd, *args, **kwargs)
        with patch("jarvis.fsio.os.fdopen", spy):
            fsio.atomic_write(self.root / "keys.json", "secret", mode=0o600)
        self.assertEqual(born, [0o600])
        self.assertEqual(stat.S_IMODE((self.root / "keys.json").stat().st_mode), 0o600)
        self.assertEqual((self.root / "keys.json").read_text(), "secret")

    def test_read_only_mode_and_bytes(self):
        fsio.atomic_write(self.root / "r", b"x", mode=0o444)
        self.assertEqual(stat.S_IMODE((self.root / "r").stat().st_mode), 0o444)
        self.assertEqual((self.root / "r").read_bytes(), b"x")


if __name__ == "__main__":
    unittest.main()
