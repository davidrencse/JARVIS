"""A node with no version file must still take a versioned bundle from this release.

Signature-only bundles stay acceptable only until LEGACY_UNVERSIONED_UNTIL, and only
while the node has never installed a versioned one. The window is simulated by patching
deploy.unversioned_allowed; nothing here sleeps or moves the clock.
"""

import calendar
import importlib
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from jarvis import deploy
from tests.cluster.bundles import make_tree, pack

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = b'[project]\nname = "bundle-upgrade"\nversion = "0"\ndependencies = []\n'
BUNDLE_KEYS = {"code", "zip", "signature", "version", "vsig"}
CLOSED = ("The code bundle carries no version; unversioned bundles are no longer accepted. "
          "Update from the main (versioned bundles) or re-join.")


def load_head_deploy(directory):
    """Import `git show HEAD:jarvis/deploy.py` as its own package (the release nodes run today)."""
    name = "jarvis_head_deploy_" + uuid.uuid4().hex
    parent = Path(directory) / "headsrc"
    package = parent / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "platforms.py").write_text(
        "import subprocess\nNO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n",
        encoding="utf-8")
    (package / "release.py").write_text(
        "SOURCE = None\n\n"
        "def source(state):\n"
        "    return SOURCE\n\n"
        "def install_dependencies(*args, **kwargs):\n"
        "    return None\n",
        encoding="utf-8")
    (package / "fsio.py").write_text(
        "from pathlib import Path\n\n"
        "def atomic_write(path, data, mode=None, newline=None, fsync=False, prepare=None):\n"
        "    path = Path(path)\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    text = data if isinstance(data, str) else bytes(data).decode('utf-8')\n"
        "    path.write_text(text, encoding='utf-8')\n",
        encoding="utf-8")
    try:
        raw = subprocess.check_output(["git", "show", "HEAD:jarvis/deploy.py"], cwd=REPO,
                                      stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        # The release cut tests a copy of the tree without .git (release._stage): no HEAD to
        # read there, and failing here blocked every release (2026-11-13).
        raise unittest.SkipTest("no git history here (release stage copy)") from None
    (package / "deploy.py").write_bytes(raw)
    sys.path.insert(0, str(parent))
    try:
        return importlib.import_module(name + ".deploy")
    finally:
        sys.path.remove(str(parent))


class BundleUpgradePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo, self.state, self.main = root / "repo", root / "node", root / "main"
        self.state.mkdir()
        make_tree(self.repo, {"jarvis/a.py": b"old\n", "pyproject.toml": PYPROJECT})
        self.key = deploy.code_key(self.main)
        starts = patch("jarvis.deploy._starts", return_value=None)
        starts.start()
        self.addCleanup(starts.stop)

    def files(self, body):
        return {"jarvis/a.py": body, "pyproject.toml": PYPROJECT}

    def code_of(self, files, name):
        folder = Path(self.tmp.name) / name
        make_tree(folder, files)
        return deploy.code_fingerprint(folder / "jarvis")

    def versioned(self, version, body):
        files = self.files(body)
        code = self.code_of(files, f"v{version}-{uuid.uuid4().hex[:6]}")
        return pack(files, code, self.main, version)

    def unversioned(self, body):
        files = self.files(body)
        return pack(files, self.code_of(files, f"legacy-{uuid.uuid4().hex[:6]}"), self.main)

    def test_head_bundle_upgrades_a_node_with_no_version_then_a_newer_one(self):
        """UPGRADE PATH: HEAD's code_bundle (version + vsig + signature) lands on a node
        that has no version file, and a later version still applies after the window."""
        head = load_head_deploy(self.tmp.name)
        stage = Path(self.tmp.name) / "head-release"
        make_tree(stage, self.files(b"from-head\n"))
        release = importlib.import_module(head.__package__ + ".release")
        release.SOURCE = stage / "jarvis"
        bundle = head.code_bundle(self.main)
        self.assertEqual(set(bundle), BUNDLE_KEYS)
        self.assertIsInstance(bundle["version"], int)
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        deploy.apply_bundle(bundle, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"from-head\n")
        self.assertEqual(deploy.installed_version(self.state), bundle["version"])
        newer = self.versioned(bundle["version"] + 1, b"newer\n")
        with patch("jarvis.deploy.unversioned_allowed", return_value=False):
            deploy.apply_bundle(newer, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"newer\n")
        self.assertEqual(deploy.installed_version(self.state), bundle["version"] + 1)

    def test_current_release_accepts_the_new_code_bundle(self):
        """A node still running HEAD, with no version file, installs what code_bundle serves now."""
        head = load_head_deploy(self.tmp.name)
        stage = Path(self.tmp.name) / "new-release"
        make_tree(stage, self.files(b"from-new\n"))
        self.assertEqual(deploy.installed_version(self.state), 0)
        with patch("jarvis.release.source", return_value=stage / "jarvis"), \
                patch.object(head, "_starts", return_value=None):
            bundle = deploy.code_bundle(self.main)
            self.assertEqual(set(bundle), BUNDLE_KEYS)
            head.apply_bundle(bundle, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"from-new\n")
        self.assertEqual(deploy.installed_version(self.state), bundle["version"])
        self.assertEqual(head.installed_version(self.state), bundle["version"])

    def test_unversioned_bundle_follows_the_window_on_a_never_versioned_node(self):
        cutoff = deploy.LEGACY_UNVERSIONED_UNTIL
        self.assertEqual(cutoff, calendar.timegm((2027, 1, 15, 0, 0, 0)))
        self.assertTrue(deploy.unversioned_allowed(now=cutoff - 1))
        self.assertFalse(deploy.unversioned_allowed(now=cutoff))
        legacy = self.unversioned(b"legacy\n")
        with patch("jarvis.deploy.unversioned_allowed", return_value=True) as allowed:
            deploy.apply_bundle(legacy, self.key, self.repo, state=self.state)
        allowed.assert_called()
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"legacy\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertEqual(deploy.installed_version(self.state), 0)
        (self.repo / "jarvis/a.py").write_bytes(b"old\n")
        with patch("jarvis.deploy.unversioned_allowed", return_value=False):
            with self.assertRaisesRegex(ValueError, "^" + re.escape(CLOSED) + "$"):
                deploy.apply_bundle(legacy, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())

    def test_unversioned_bundle_refused_when_versioned_was_installed_in_both_windows(self):
        deploy.apply_bundle(self.versioned(2000, b"v2000\n"), self.key, self.repo, state=self.state)
        legacy = self.unversioned(b"legacy\n")
        for open_window in (True, False):
            with patch("jarvis.deploy.unversioned_allowed", return_value=open_window):
                with self.assertRaisesRegex(ValueError, "already installed a versioned one"):
                    deploy.apply_bundle(legacy, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"v2000\n")
        self.assertEqual(deploy.installed_version(self.state), 2000)

    def test_older_version_is_refused_as_replay(self):
        deploy.apply_bundle(self.versioned(2000, b"v2000\n"), self.key, self.repo, state=self.state)
        with self.assertRaisesRegex(ValueError, "older"):
            deploy.apply_bundle(self.versioned(1999, b"v1999\n"), self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"v2000\n")
        self.assertEqual(deploy.installed_version(self.state), 2000)

    def test_bad_vsig_is_refused(self):
        forged = self.versioned(1000, b"forged\n")
        forged["version"] = 10 ** 12  # vsig still covers 1000
        with self.assertRaisesRegex(ValueError, "isn't signed"):
            deploy.apply_bundle(forged, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())


if __name__ == "__main__":
    unittest.main()
