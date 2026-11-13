"""Data-audit regressions: vault reads stay inside the vault, state files are private."""

import json
import os
import stat
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from jarvis import briefing, cluster_setup, pathsafe
from jarvis.cluster_store import Broker
from jarvis.history import History
from jarvis.memory import Memory
from jarvis.store import Store, connect_database
from jarvis.vault import Vault

TODAY = date(2026, 11, 13)
SECRET = "---\ntitle: s\n---\n\n## Log\n\n- 09:00 LEAKED-LOG-LINE\n\n## Priorities\n\n1. - [ ] LEAKED-PRIORITY\n"
POSIX = os.name != "nt"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class VaultReadEscapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "vault"
        self.root.mkdir()
        (self.root / ".obsidian").mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.md").write_text(SECRET, encoding="utf-8")
        (base / "secretfile").write_text(SECRET, encoding="utf-8")
        self.state = base / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def vault(self, settings, write=False):
        (self.root / ".obsidian" / "daily-notes.json").write_text(json.dumps(settings))
        return Vault(self.root, self.state, {"write": write})

    def leaked(self, vault):
        world = vault.world(today=TODAY)
        line = briefing.priorities_line(vault, TODAY)
        return "LEAKED" in world or "LEAKED" in line

    def test_folder_escape_is_not_read(self):
        v = self.vault({"folder": "../outside", "format": "secret"})
        self.assertFalse(self.leaked(v))
        self.assertIsNone(v.daily_text(TODAY))

    def test_format_escape_is_not_read(self):
        # moment() leaves unknown letters alone, so "../zzz" names ../zzz.md... use ../ + secretfile stem
        (self.root.parent / "zzz.md").write_text(SECRET, encoding="utf-8")
        v = self.vault({"format": "../zzz"})
        self.assertEqual(v.daily_path(TODAY), self.root / "../zzz.md")
        self.assertFalse(self.leaked(v))

    def test_absolute_format_is_not_read(self):
        target = self.outside / "secret"
        v = self.vault({"format": str(target)})
        self.assertFalse(self.leaked(v))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlink_daily_note_is_not_read(self):
        v = self.vault({})
        os.symlink(self.root.parent / "secretfile", self.root / "2026-11-13.md")
        self.assertFalse(self.leaked(v))
        self.assertIsNone(v.daily_text(TODAY))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlinked_daily_folder_out_of_vault_is_not_read(self):
        os.symlink(self.outside, self.root / "daily")
        (self.outside / "2026-11-13.md").write_text(SECRET, encoding="utf-8")
        v = self.vault({"folder": "daily"})
        self.assertFalse(self.leaked(v))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlinked_memory_note_is_not_read(self):
        (self.root.parent / "secretfile").write_text("## Owner\n\n- LEAKED-OWNER\n")
        os.symlink(self.root.parent / "secretfile", self.root / "MEMORY.md")
        v = self.vault({})
        self.assertNotIn("LEAKED", v.world(today=TODAY))

    def test_regular_daily_note_still_read(self):
        (self.root / "2026-11-13.md").write_text(SECRET.replace("LEAKED", "OK"), encoding="utf-8")
        v = self.vault({})
        self.assertIn("OK-PRIORITY", v.world(today=TODAY))
        self.assertIn("OK-PRIORITY", briefing.priorities_line(v, TODAY))

    def test_escaping_template_is_not_used_for_new_daily(self):
        for template in (str(self.outside / "secret.md"), "../outside/secret.md"):
            v = self.vault({"template": template}, write=True)
            result = v.log("hello", when=datetime(2026, 11, 13, 9, 0))
            text = (self.root / result["path"]).read_text(encoding="utf-8")
            self.assertNotIn("LEAKED", text)
            (self.root / result["path"]).unlink()

    def test_log_still_refuses_escaping_daily_path(self):
        v = self.vault({"folder": "../outside"}, write=True)
        with self.assertRaises(ValueError):
            v.log("x", when=datetime(2026, 11, 13, 9, 0))


@unittest.skipUnless(POSIX, "POSIX modes")
class PrivateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self.old_umask)
        self.tmp.cleanup()

    def assert_private(self, folder, *names):
        self.assertEqual(mode(folder), 0o700, folder)
        for name in names:
            self.assertEqual(mode(Path(folder) / name), 0o600, name)

    def test_task_store_is_private(self):
        folder = Path(self.tmp.name) / "s1"
        Store(folder, prune=False)
        self.assert_private(folder, "tasks.sqlite3")

    def test_history_and_memory_are_private(self):
        folder = Path(self.tmp.name) / "s2"
        History(folder)
        Memory(folder)
        self.assert_private(folder, "history.sqlite3", "memory.sqlite3")

    def test_vault_index_is_private(self):
        root = Path(self.tmp.name) / "vault"
        root.mkdir()
        (root / "a.md").write_text("# A\n")
        state = Path(self.tmp.name) / "s3"
        vault = Vault(root, state, {})
        vault.refresh(force=True)
        self.assert_private(state, "vault.sqlite3")

    def test_existing_world_readable_files_are_tightened(self):
        folder = Path(self.tmp.name) / "old"
        folder.mkdir()
        db = folder / "x.sqlite3"
        sqlite3.connect(db).close()
        os.chmod(db, 0o644)
        os.chmod(folder, 0o755)
        with connect_database(db) as conn:
            conn.execute("CREATE TABLE t(a)")
        self.assert_private(folder, "x.sqlite3")

    def test_wal_and_journal_sidecars_are_private(self):
        folder = Path(self.tmp.name) / "wal"
        db = folder / "x.sqlite3"
        folder.mkdir()
        with connect_database(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(a)")
            conn.execute("INSERT INTO t VALUES(1)")
        keep = sqlite3.connect(db)
        keep.execute("INSERT INTO t VALUES(2)")
        keep.commit()
        with connect_database(db) as conn:  # reopen: sidecars exist now
            conn.execute("SELECT * FROM t").fetchall()
            for suffix in ("-wal", "-shm"):
                side = Path(str(db) + suffix)
                if side.exists():
                    self.assertEqual(mode(side), 0o600, suffix)
        keep.close()

    def test_cluster_state_dir_is_private(self):
        folder = Path(self.tmp.name) / "cluster-state"
        Broker(folder)
        self.assertEqual(mode(folder), 0o700)

    def test_ssh_private_key_born_0600(self):
        folder = Path(self.tmp.name) / "keys"
        seen = {}
        real = cluster_setup.os.open

        def spy(path, flags, perm=0o777, *a, **k):
            if str(path).endswith(".writing"):
                seen["perm"] = perm
            return real(path, flags, perm, *a, **k)

        with mock.patch.object(cluster_setup.os, "open", spy), \
                mock.patch("jarvis.platforms.private_key_permissions", lambda p: p):
            path = cluster_setup._private_file(folder / "id", "KEY\n")
        self.assertEqual(seen.get("perm"), 0o600)
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(folder), 0o700)


class OpenLeafJunctionTests(unittest.TestCase):
    """Windows has no O_NOFOLLOW: a name swapped for a junction after the check must fail."""

    def test_swap_after_check_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "f.bin").write_bytes(b"x")
            real_open = os.open

            def swapped(path, flags, perm=0o777, **k):
                fd = real_open(path, flags, perm, **k)
                return fd

            reparse = iter([False, False, True])  # folder, leaf (pre-open) clean; post-open leaf is a junction
            with mock.patch.object(pathsafe.os, "name", "nt"), \
                    mock.patch.object(pathsafe, "_is_reparse", lambda p: next(reparse, True)), \
                    mock.patch.object(pathsafe.os, "open", swapped):
                with self.assertRaises(ValueError):
                    pathsafe.open_leaf(tmp, "f.bin")

    def test_identity_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            a.write_bytes(b"1")
            b.write_bytes(b"2")
            fd = os.open(a, os.O_RDONLY)
            try:
                self.assertTrue(pathsafe._same_file(fd, a))
                self.assertFalse(pathsafe._same_file(fd, b))
            finally:
                os.close(fd)

    def test_normal_open_still_works_on_the_fallback_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "f.bin").write_bytes(b"ok")
            with mock.patch.object(pathsafe.os, "name", "nt"):
                with pathsafe.open_leaf(tmp, "f.bin") as stream:
                    self.assertEqual(stream.read(), b"ok")


if __name__ == "__main__":
    unittest.main()
