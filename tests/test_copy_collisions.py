"""Exact filename checks; no real SSH connections or device writes."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import transfer
from jarvis.storage import REMOTE_SCRIPT, Storage

from . import test_storage as fixtures


class CopyCollisionTests(fixtures.Temp):
    def setUp(self):
        super().setUp()
        self.fake = fixtures.FakeRemote(self)
        for patcher in (
                patch.object(Storage, "_remote", lambda s, d, p, a: self.fake.remote(d, p, a)),
                patch("jarvis.storage.subprocess.run", lambda args, **kw: self.fake.run(args, **kw)),
                patch("jarvis.storage.shutil.which", lambda n: n)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_truncated_listing_is_not_used_to_choose_destination(self):
        (self.remote / "a.txt").write_text("keep me")
        with patch.object(self.fs, "list", side_effect=AssertionError("listing is not authoritative")):
            copied = self.fs.copy("dell", str(self.local / "a.txt"), "zen", str(self.remote))
        self.assertEqual(copied["as"], "a (2).txt")
        self.assertEqual((self.remote / "a.txt").read_text(), "keep me")
        self.assertEqual((self.remote / "a (2).txt").read_text(), "alpha")

    def test_sanitized_name_is_checked_before_copy(self):
        (self.local / "a_b.txt").write_text("keep me")
        # Linux permits a colon, Windows cannot create that source fixture. The
        # remote resolver returns it as data, just as a real SSH listing would.
        remote = {"path": "/home/owner/a:b.txt", "dir": False, "size": 5, "roots": ["/home/owner"]}
        with patch.object(self.fs, "_remote", return_value=remote), \
                patch("jarvis.storage.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, "", "")) as run:
            result = self.fs.copy("zen", "/home/owner/a:b.txt", "dell", str(self.local))
        self.assertEqual(result["as"], "a_b (2).txt")
        self.assertEqual(run.call_args.args[0][-1], "a_b (2).txt")
        self.assertEqual((self.local / "a_b.txt").read_text(), "keep me")

    @unittest.skipUnless(os.name == "nt", "Windows destination case semantics")
    def test_windows_case_collision_preserves_existing_file(self):
        (self.remote / "A.TXT").write_text("upper")
        result = self.fs.copy("zen", str(self.remote / "A.TXT"), "dell", str(self.local))
        self.assertEqual(result["as"], "A (2).TXT")
        self.assertEqual((self.local / "a.txt").read_text(), "alpha")

    def test_broken_symlink_is_occupied(self):
        (self.remote / "broken.txt").write_text("source")
        link = self.local / "broken.txt"
        try:
            link.symlink_to(self.local / "missing.txt")
        except OSError:
            self.skipTest("symlink creation unavailable")
        result = self.fs.copy("zen", str(self.remote / "broken.txt"), "dell", str(self.local))
        self.assertEqual(result["as"], "broken (2).txt")
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.local / "missing.txt").exists())


class ExactLookupTests(fixtures.Temp):
    def run_remote(self, path):
        req = {"action": "exists", "path": str(path), "roots": [str(self.remote)]}
        result = subprocess.run([sys.executable, "-"],
                                input=f"REQUEST = {json.dumps(json.dumps(req))}\n" + REMOTE_SCRIPT,
                                text=True, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_remote_exact_check_counts_files_directories_and_missing(self):
        (self.remote / "file").write_text("x")
        (self.remote / "folder").mkdir()
        self.assertTrue(self.run_remote(self.remote / "file")["exists"])
        self.assertTrue(self.run_remote(self.remote / "folder")["exists"])
        self.assertFalse(self.run_remote(self.remote / "absent")["exists"])
        self.assertIn("error", self.run_remote(self.base / "outside"))

    def test_remote_broken_symlink_counts_without_following_it(self):
        link = self.remote / "broken"
        try:
            link.symlink_to(self.base / "outside-does-not-exist")
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertTrue(self.run_remote(link)["exists"])

    def test_local_exact_lookup_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.fs.resolve("dell", str(self.base / "outside"), "exists")

    def test_permission_error_is_not_treated_as_available(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")), \
                self.assertRaises(PermissionError):
            self.fs.resolve("dell", str(self.local / "a.txt"), "exists")

    def test_windows_reserved_and_trailing_dot_names_are_normalized(self):
        for source, expected in (("CON.txt", "_CON.txt"), ("nul", "_nul"),
                                 ("report.txt.", "report.txt"), ("AUX", "_AUX")):
            self.assertEqual(transfer.safe_name(source), expected)

    def test_collision_names_keep_numeric_suffix_with_long_utf8_names(self):
        for name in ("a" * 251 + ".txt", "report." + "x" * 248, "é" * 125 + ".txt"):
            first = transfer.collision_name(name, 2)
            second = transfer.collision_name(name, 3)
            self.assertLessEqual(len(first.encode("utf-8")), 255)
            self.assertLessEqual(len(second.encode("utf-8")), 255)
            self.assertNotEqual(first, second)
            self.assertIn(" (2)", first)

    def test_send_counts_directories_but_inbox_remains_file_only(self):
        source = self.local / "a.txt"
        calls = []

        def run(args, timeout, **kw):
            calls.append(args)
            listing = "a.txt/\nfile.txt\n" if "ls -1p" in args[-1] else ""
            return subprocess.CompletedProcess(args, 0, listing, "")

        with patch.object(transfer, "_run", side_effect=run), \
                patch.object(transfer.shutil, "which", side_effect=lambda name: name):
            self.assertEqual(transfer.inbox("owner@host", None), ["file.txt"])
            result = transfer.send("owner@host", None, source)
        self.assertEqual(result["sent"], "a (2).txt")
        self.assertTrue(calls[-1][-1].endswith("/a (2).txt"))

    def test_repeated_long_drop_names_stay_visible_to_collision_selection(self):
        name = "x" * 196 + ".txt"
        taken = {name}

        def run(args, timeout, **kw):
            if "ls -1p" in args[-1]:
                return subprocess.CompletedProcess(args, 0, "\n".join(taken), "")
            if args[0] == "scp":
                destination = args[-1].split("/")[-1]
                self.assertNotIn(destination, taken)
                taken.add(destination)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(transfer, "_run", side_effect=run), \
                patch.object(transfer.shutil, "which", side_effect=lambda name: name):
            for _ in range(3):
                result = transfer.send("owner@host", None, self.local / "a.txt", name=name)
                self.assertLessEqual(len(result["sent"]), 200)
            self.assertEqual(set(transfer.inbox("owner@host", None)), taken)
        self.assertEqual(len(taken), 4)


if __name__ == "__main__":
    unittest.main()
