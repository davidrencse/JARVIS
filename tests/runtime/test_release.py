"""Nodes get only tested code (release.cut), and a node rolls back code that won't start
(release.guard). The test suite itself is never run here: `tests` is a stand-in."""

import json
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
        for patcher in (
            patch.object(release, 'source_commit', side_effect=lambda repo=None: deploy.code_fingerprint(self.repo / 'jarvis')),
            patch.object(release, '_archive', side_effect=lambda repo, stage, commit: release._stage(repo, stage)),
            patch('jarvis.confirm.notify'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


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

    def test_dirty_file_quiet_period_does_not_delay_a_commit(self):
        report = self.cut(quiet=60)
        self.assertTrue(report['released'])
        self.assertEqual(len(self.ran), 1)

    def test_bundle_serves_the_release_not_the_live_tree(self):
        self.cut()
        tree(self.repo, {"jarvis/__init__.py": "broken(\n"})
        with patch("jarvis.deploy.sign", return_value="sig"):
            bundle = deploy.code_bundle(self.state)
        self.assertEqual(bundle["code"], release.served_code(self.state))

    def test_no_release_yet_serves_the_live_tree_as_before(self):
        self.assertEqual(release.served_code(self.state), deploy.code_fingerprint())
        self.assertEqual(release.source(self.state), deploy.PACKAGE)


class StageTests(unittest.TestCase):
    def test_nested_checkouts_are_not_staged(self):
        # Audit cluster-10: a stray repo copy (with its own secrets) rode into every stage.
        with tempfile.TemporaryDirectory() as tmp:
            repo, stage = Path(tmp, "repo"), Path(tmp, "stage")
            tree(repo, {"jarvis/__init__.py": "", "tests/test_x.py": "", "README.md": "r",
                        "stray/.git/HEAD": "ref", "stray/jarvis/x.py": "",
                        "skills/inner/.git/HEAD": "ref", "skills/inner/a.txt": "", "skills/b.txt": ""})
            stage.mkdir()
            release._stage(repo, stage)
            self.assertTrue((stage / "jarvis" / "__init__.py").exists())
            self.assertTrue((stage / "README.md").exists())
            self.assertTrue((stage / "skills" / "b.txt").exists())
            self.assertFalse((stage / "stray").exists())
            self.assertFalse((stage / "skills" / "inner").exists())


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
        # The script's first sleep lets the old process exit; these tests start no old
        # process, so the same script runs with the waits shortened.
        fast = (release.GUARD.replace("time.sleep(2)", "time.sleep(0.05)", 1)
                 .replace("time.sleep(1)", "time.sleep(0.02)", 1))
        guard = patch.object(release, "GUARD", fast)
        guard.start()
        self.addCleanup(guard.stop)

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


RELEASED = "0123456789abcdef"
LIVE = "fedcba9876543210"


class CodeParityTests(unittest.TestCase):
    """Transfer parity is a report. It must not cut a release, deploy, or update a node."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for name in ("cut", "backup", "guard", "install_dependencies"):
            patcher = patch.object(release, name, side_effect=AssertionError(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(release.subprocess, "run", side_effect=AssertionError("subprocess"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self):
        return sorted(path.relative_to(self.state).as_posix()
                      for path in self.state.rglob("*") if path.is_file())

    def codes(self, report):
        return [block["code"] for block in report["blocks"]]

    def release_at(self, code=RELEASED):
        root = self.state / "code-release"
        (root / "jarvis").mkdir(parents=True)
        (root / "release.json").write_text(
            json.dumps({"code": code, "at": 1, "tests": "OK"}) + "\n", encoding="utf-8")

    def parity(self, reported=RELEASED, live=RELEASED, settled=None):
        with patch.object(release, "code_fingerprint", return_value=live):
            if settled is None:
                return release.code_parity(self.state, reported)
            with patch.object(release, "settled", return_value=settled):
                return release.code_parity(self.state, reported)

    def test_live_tree_match_is_not_a_tested_release(self):
        before = self.names()
        with patch.object(release, "code_fingerprint", return_value=LIVE), \
                patch.object(release, "settled", side_effect=AssertionError("settled")):
            # served_code still falls back to the live tree. Parity does not treat that as a release.
            self.assertEqual(release.served_code(self.state), LIVE)
            report = release.code_parity(self.state, LIVE)
        self.assertFalse(report["matched"])
        self.assertFalse(report["authorizes"])
        self.assertIsNone(report["code"])
        self.assertEqual(report["live"], LIVE)
        self.assertEqual(self.codes(report), ["no_tested_release"])
        self.assertIn("jarvis cluster release", report["instruction"])
        self.assertIn("does not cut one", report["instruction"])
        self.assertIn("authorize a transfer", report["limits"])
        self.assertEqual(self.names(), before)

    def test_note_without_a_package_directory_is_not_a_release(self):
        (self.state / "code-release").mkdir()
        (self.state / "code-release" / "release.json").write_text(
            json.dumps({"code": RELEASED}) + "\n", encoding="utf-8")
        report = self.parity(reported=RELEASED, live=RELEASED)
        self.assertEqual(self.codes(report), ["no_tested_release"])

    def test_matching_fingerprints_do_not_authorize_a_transfer(self):
        self.release_at()
        with patch.object(release, "settled", side_effect=AssertionError("settled")):
            report = self.parity()
        self.assertTrue(report["matched"])
        self.assertFalse(report["authorizes"])
        self.assertEqual(report["instruction"], "")
        self.assertEqual(report["blocks"], [])
        self.assertEqual(report["code"], RELEASED)
        self.assertEqual(report["live"], RELEASED)
        self.assertEqual(report["reported"], RELEASED)

    def test_a_different_pc_names_update_and_does_not_run_it(self):
        self.release_at()
        report = self.parity(reported=LIVE, live=RELEASED)
        self.assertFalse(report["matched"])
        self.assertEqual(self.codes(report), ["code_mismatch"])
        self.assertIn(LIVE, report["instruction"])
        self.assertIn("jarvis cluster update <node>", report["instruction"])
        self.assertIn("does not update a node", report["instruction"])

    def test_unreleased_edits_say_whether_the_tree_has_settled(self):
        self.release_at()
        quiet = self.parity(reported=RELEASED, live=LIVE, settled=True)
        self.assertEqual(self.codes(quiet), ["unreleased_edits"])
        self.assertIn("quiet long enough", quiet["instruction"])
        self.assertIn("does not cut one", quiet["instruction"])
        recent = self.parity(reported=RELEASED, live=LIVE, settled=False)
        self.assertIn(str(release.QUIET), recent["instruction"])
        self.assertIn("Wait for it to settle", recent["instruction"])
        self.assertNotIn("code_mismatch", self.codes(recent))

    def test_failed_tests_are_named_and_not_retried(self):
        self.release_at()
        (self.state / release.FAILED).write_text(
            json.dumps({"code": LIVE, "tests": "FAIL: tests.runtime.test_release\n"}) + "\n",
            encoding="utf-8")
        with patch.object(release, "settled", side_effect=AssertionError("settled")):
            report = self.parity(reported=RELEASED, live=LIVE)
        self.assertEqual(self.codes(report)[0], "release_tests_failed")
        self.assertIn("FAIL: tests.runtime.test_release", report["instruction"])
        self.assertIn("does not cut one", report["instruction"])
        self.assertNotIn("unreleased_edits", self.codes(report))

    def test_a_missing_fingerprint_fails_closed(self):
        self.release_at()
        missing = self.parity(reported=None, live=RELEASED)
        self.assertEqual(self.codes(missing), ["reported_missing"])
        self.assertIn("does not contact", missing["instruction"])
        self.assertIsNone(missing["reported"])
        blank = self.parity(reported="", live=RELEASED)
        self.assertEqual(self.codes(blank), ["reported_missing"])
        self.assertIsNone(self.parity(reported=12, live=RELEASED)["reported"])
        with patch.object(release, "code_fingerprint", side_effect=OSError("unread")):
            unread = release.code_parity(self.state, RELEASED)
        self.assertIn("live_unreadable", self.codes(unread))
        self.assertFalse(unread["matched"])
        self.assertEqual(release.code_parity(None, RELEASED)["blocks"][0]["code"], "unreadable")

    def test_a_rolled_back_fingerprint_is_not_installed_again(self):
        self.release_at()
        (self.state / release.BAD).write_text(
            json.dumps({"code": RELEASED}) + "\n", encoding="utf-8")
        report = self.parity()
        self.assertFalse(report["matched"])
        self.assertEqual(self.codes(report), ["rolled_back"])
        self.assertIn("must not be installed again", report["instruction"])
        self.assertIn("does not deploy", report["instruction"])


if __name__ == "__main__":
    unittest.main()
