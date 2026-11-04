"""Nodes get only tested code (release.cut), and a node rolls back code that won't start
(release.guard). The test suite itself is never run here: `tests` is a stand-in."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import deploy, release


def tree(root, files):
    for name, text in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class CutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo, self.state = Path(self.tmp.name, "repo"), Path(self.tmp.name, "state")
        tree(self.repo, {"jarvis/__init__.py": "x = 1\n", "jarvis/web/app.js": "1\n",
                         "tests/test_x.py": "", "pyproject.toml": "[project]\n",
                         ".git/HEAD": "ref", ".jarvis/memory.sqlite3": "db"})
        self.ran = []

    def suite(self, ok):
        def run(stage):
            self.ran.append(sorted(p.name for p in Path(stage).iterdir()))
            return ok, "Ran 3 tests\n\nOK" if ok else "FAILED (failures=1)"
        return run

    def cut(self, ok=True, quiet=0):
        return release.cut(self.state, self.repo, quiet=quiet, tests=self.suite(ok))

    def test_passing_tests_make_the_snapshot_what_nodes_get(self):
        report = self.cut()
        self.assertTrue(report["released"])
        self.assertEqual(self.ran, [["jarvis", "pyproject.toml", "tests"]])  # no .git/.jarvis
        code = deploy.code_fingerprint(self.repo / "jarvis")
        self.assertEqual(release.served_code(self.state), code)
        self.assertEqual(release.source(self.state), self.state / "code-release" / "jarvis")
        # Editing the live tree afterwards doesn't change what nodes get.
        tree(self.repo, {"jarvis/__init__.py": "x = 2  # half-finished\n"})
        self.assertEqual(release.served_code(self.state), code)
        self.assertEqual((self.state / "code-release/jarvis/__init__.py").read_text(), "x = 1\n")

    def test_failing_tests_keep_the_old_release_and_are_not_retried(self):
        self.cut()
        before = release.served_code(self.state)
        tree(self.repo, {"jarvis/__init__.py": "x = (\n"})
        report = self.cut(ok=False)
        self.assertFalse(report["released"])
        self.assertEqual(release.served_code(self.state), before)
        with patch("jarvis.release.code_fingerprint", return_value=report["code"]), \
                patch("jarvis.release.settled", return_value=True):
            self.assertFalse(release.due(self.state))  # same broken code: not tested again

    def test_unsettled_tree_waits(self):
        report = self.cut(quiet=60)  # files were written just now
        self.assertFalse(report["released"])
        self.assertEqual(self.ran, [])

    def test_bundle_serves_the_release_not_the_live_tree(self):
        self.cut()
        tree(self.repo, {"jarvis/__init__.py": "broken(\n"})
        with patch("jarvis.deploy.sign", return_value="sig"):
            bundle = deploy.code_bundle(self.state)
        self.assertEqual(bundle["code"], release.served_code(self.state))

    def test_no_release_yet_serves_the_live_tree_as_before(self):
        self.assertEqual(release.served_code(self.state), deploy.code_fingerprint())
        self.assertEqual(release.source(self.state), deploy.PACKAGE)


class DependencyTests(unittest.TestCase):
    OLD = '[project]\nname = "jarvis"\ndependencies = ["Pillow>=12"]\n'
    NEW = '[project]\nname = "jarvis"\ndependencies = ["Pillow>=12", "psutil>=5.9"]\n'

    def test_changed_dependencies_are_installed_with_this_python(self):
        calls = []
        run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        self.assertTrue(release.install_dependencies(self.OLD, self.NEW, log=lambda _: None, run=run))
        self.assertEqual(calls[0][:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(calls[0][-2:], ["Pillow>=12", "psutil>=5.9"])

    def test_unchanged_or_unreadable_dependencies_run_nothing(self):
        run = lambda *a, **k: self.fail("pip ran")
        self.assertIsNone(release.install_dependencies(self.OLD, self.OLD, run=run))
        self.assertIsNone(release.install_dependencies(self.OLD, "not toml [", run=run))

    def test_failed_install_is_reported_not_raised(self):
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no network")
        logged = []
        self.assertFalse(release.install_dependencies(self.OLD, self.NEW, log=logged.append, run=run))
        self.assertIn("no network", logged[-1])


class GuardTests(unittest.TestCase):
    """The real watchdog script, run as a separate process the way a node runs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo, self.state = Path(self.tmp.name, "repo"), Path(self.tmp.name, "state")
        tree(self.repo, {"jarvis/a.py": "old\n", "pyproject.toml": "old\n"})
        release.backup(self.state, self.repo)
        tree(self.repo, {"jarvis/a.py": "new\n", "jarvis/b.py": "new file\n", "pyproject.toml": "new\n"})

    def run_guard(self, child, wait=4):
        (self.state / "logs").mkdir(parents=True, exist_ok=True)
        script = self.state / release.GUARD_FILE
        script.write_text(release.GUARD, encoding="utf-8")
        return subprocess.run([sys.executable, str(script), str(self.repo), str(self.state), "new1",
                               str(wait), "launch", sys.executable, "-c", child],
                              timeout=60, capture_output=True, text=True)

    def test_code_that_dies_before_checking_in_is_rolled_back_and_marked_bad(self):
        # First run: the new code crashes. The guard restores and starts the "old" code
        # (the same command here), which exits 0.
        out = self.run_guard("import sys; sys.exit(3)")
        self.assertEqual((self.repo / "jarvis/a.py").read_text(), "old\n")
        self.assertFalse((self.repo / "jarvis/b.py").exists())
        self.assertEqual((self.repo / "pyproject.toml").read_text(), "old\n")
        self.assertTrue(release.is_bad(self.state, "new1"))
        self.assertIn("rolling back", (self.state / "logs/code-guard.log").read_text())
        self.assertEqual(out.returncode, 3)  # the restarted (old) command's own exit

    def test_code_that_checks_in_is_kept(self):
        confirm = (f"import json; json.dump({{'code': 'new1'}}, "
                   f"open({str(self.state / release.CONFIRMED)!r}, 'w'))")
        out = self.run_guard(confirm)
        self.assertEqual(out.returncode, 0)
        self.assertEqual((self.repo / "jarvis/a.py").read_text(), "new\n")
        self.assertFalse(release.is_bad(self.state, "new1"))


if __name__ == "__main__":
    unittest.main()


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name, "jarvis")
        tree(self.root, {"a.py": "x = 1\n"})

    def test_backups_and_editor_files_do_not_change_the_fingerprint(self):
        before = deploy.code_fingerprint(self.root)
        tree(self.root, {"a.py.pre-fix": "old", "a.py~": "", ".a.py.swp": "", ".#a.py": "", "t.tmp": ""})
        self.assertEqual(deploy.code_fingerprint(self.root), before)
        self.assertEqual([p.name for p in deploy._files(self.root)], ["a.py"])

    def test_remote_fingerprint_uses_the_same_rule(self):
        tree(self.root, {"a.py.pre-fix": "old", "a.py~": ""})
        out = subprocess.run([sys.executable, "-c", deploy.REMOTE_FINGERPRINT, str(self.root)],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, deploy.code_fingerprint(self.root))

    def test_same_size_edit_with_old_mtime_changes_the_main_fingerprint(self):
        import os
        with patch.object(deploy, "PACKAGE", self.root):
            deploy._CACHE.update(stamp=None, value=None)
            first = deploy.code_fingerprint(self.root)
            path = self.root / "a.py"
            stat = path.stat()
            path.write_text("x = 22\n", encoding="utf-8")  # longer, mtime restored below
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertNotEqual(deploy.code_fingerprint(self.root), first)
            deploy._CACHE.update(stamp=None, value=None)

    def test_settled_ignores_a_file_that_vanishes(self):
        gone = self.root / "gone.py"
        with patch.object(release, "_files", return_value=[gone, self.root / "a.py"]):
            self.assertTrue(release.settled(self.root, quiet=0))
