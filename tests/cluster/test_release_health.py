"""Post-apply health check on a signed bundle.

A fake check proves a bad result restores jarvis.prev, drops files the bundle added,
refuses to advance the installed version, and marks the release bad when state is
supplied. A real package still runs agent imports and local config/state checks.
Signature refusal and the staged _starts preflight stay in front of that check.
No network and no systemd.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import deploy, release
from tests.cluster.bundles import make_tree, pack

OLD_PROJECT = b'[project]\nname = "n"\nversion = "0"\ndependencies = []\n'
NEW_PROJECT = b'[project]\nname = "n"\nversion = "1"\ndependencies = []\n'
CODE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForHealthCheckOnly="
TOKEN = "t" * 24


def fingerprint(root, files):
    folder = Path(root)
    make_tree(folder, files)
    return deploy.code_fingerprint(folder / "jarvis")


def agent_files(extra=None, drop=()):
    files = {f"jarvis/{name}.py": b"# agent\n" for name in deploy.NODE_MODULES}
    files["jarvis/__init__.py"] = b""
    for name in drop:
        del files[f"jarvis/{name}.py"]
    if extra:
        files.update(extra)
    files["pyproject.toml"] = OLD_PROJECT
    return files


def node_config(enabled=True, local="dell"):
    return {"cluster": {"enabled": enabled, "main_device": "zenbook", "local_device": local,
                        "main_url": "http://127.0.0.1:7780"},
            "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"},
                        "dell": {"hostname": "dell", "label": "Dell"}}}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ReleaseHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo, self.state, self.main = root / "repo", root / "node", root / "main"
        self.state.mkdir()
        make_tree(self.repo, {"jarvis/a.py": b"old\n", "jarvis/kept.py": b"kept\n",
                              "jarvis/sub/kept.py": b"sub\n", "pyproject.toml": OLD_PROJECT})
        self.key = deploy.code_key(self.main)
        self.calls = []

    def files_of(self, body):
        return {"jarvis/a.py": body, "jarvis/sub/added.py": b"added\n", "pyproject.toml": NEW_PROJECT}

    def versioned(self, version, body):
        files = self.files_of(body)
        code = fingerprint(Path(self.tmp.name) / f"stage-{version}-{len(self.calls)}", files)
        return pack(files, code, self.main, version), code

    def apply(self, bundle, health, state=None):
        return deploy.apply_bundle(bundle, self.key, self.repo, state=state, health=health)

    def test_bad_health_restores_prev_removes_new_files_and_marks_bad(self):
        bundle, code = self.versioned(2000, b"new\n")
        seen = {}

        def health(repo, state):
            root = Path(repo)
            seen["live"] = (root / "jarvis/a.py").read_bytes()
            seen["added"] = (root / "jarvis/sub/added.py").is_file()
            seen["kept"] = (root / "jarvis/kept.py").exists()
            seen["state"] = Path(state)
            # Prove the restore reads jarvis.prev, not a private copy of the old bytes.
            (root / "jarvis.prev/a.py").write_bytes(b"from-prev\n")
            return "agent will not serve"

        with self.assertRaisesRegex(ValueError, r"health check \(agent will not serve\).*; restored"):
            self.apply(bundle, health, state=self.state)
        self.assertEqual(seen["live"], b"new\n")
        self.assertTrue(seen["added"])
        self.assertFalse(seen["kept"])
        self.assertEqual(seen["state"], self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"from-prev\n")
        self.assertEqual((self.repo / "jarvis/kept.py").read_bytes(), b"kept\n")
        self.assertEqual((self.repo / "jarvis/sub/kept.py").read_bytes(), b"sub\n")
        self.assertFalse((self.repo / "jarvis/sub/added.py").exists())
        self.assertEqual((self.repo / "pyproject.toml").read_bytes(), OLD_PROJECT)
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertEqual(deploy.installed_version(self.state), 0)
        self.assertTrue(release.is_bad(self.state, code))
        self.assertFalse(release.is_bad(self.state, "0" * 16))
        note = json.loads((self.state / release.BAD).read_text(encoding="utf-8"))
        self.assertEqual(note["code"], code)
        self.assertEqual(note["why"], "agent will not serve")
        self.assertIsInstance(note["at"], float)

    def test_bad_health_without_state_restores_and_does_not_mark_bad(self):
        bundle, _code = self.versioned(2000, b"new\n")

        def health(repo, state):
            self.assertIsNone(state)
            (Path(repo) / "jarvis.prev/a.py").write_bytes(b"from-prev\n")
            return "no"

        with self.assertRaisesRegex(ValueError, "restored the previous code"):
            self.apply(bundle, health, state=None)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"from-prev\n")
        self.assertFalse((self.repo / "jarvis/sub/added.py").exists())
        self.assertEqual((self.repo / "jarvis/kept.py").read_bytes(), b"kept\n")
        self.assertFalse((self.state / release.BAD).exists())
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())

    def test_version_advances_only_after_health_passes(self):
        good, good_code = self.versioned(2000, b"v2000\n")
        bad, bad_code = self.versioned(2001, b"v2001\n")

        def ok(repo, state):
            self.calls.append((Path(repo) / "jarvis/a.py").read_bytes())
            return None

        count = self.apply(good, ok, state=self.state)
        self.assertGreater(count, 0)
        self.assertEqual(self.calls, [b"v2000\n"])
        self.assertEqual(deploy.installed_version(self.state), 2000)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"v2000\n")
        self.assertFalse(release.is_bad(self.state, good_code))

        with self.assertRaisesRegex(ValueError, "restored the previous code"):
            self.apply(bad, lambda repo, state: "still sick", state=self.state)
        self.assertEqual(deploy.installed_version(self.state), 2000)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"v2000\n")
        self.assertTrue((self.repo / "jarvis/sub/added.py").is_file())  # the healthy bundle's file
        self.assertTrue(release.is_bad(self.state, bad_code))
        self.assertFalse(release.is_bad(self.state, good_code))

    def test_raising_health_rolls_back_without_recording_a_version(self):
        bundle, code = self.versioned(2000, b"new\n")

        def health(repo, state):
            raise RuntimeError("disk read failed")

        with self.assertRaisesRegex(ValueError, r"health check failed: disk read failed"):
            self.apply(bundle, health, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.repo / "jarvis/sub/added.py").exists())
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertTrue(release.is_bad(self.state, code))

    def test_patched_release_health_is_the_default_check(self):
        bundle, code = self.versioned(2000, b"new\n")
        with patch("jarvis.deploy.release_health", return_value="patched") as mocked:
            with self.assertRaisesRegex(ValueError, r"health check \(patched\)"):
                deploy.apply_bundle(bundle, self.key, self.repo, state=self.state)
        mocked.assert_called_once()
        self.assertEqual(Path(mocked.call_args[0][0]).resolve(), self.repo.resolve())
        self.assertEqual(Path(mocked.call_args[0][1]), self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertTrue(release.is_bad(self.state, code))

    def test_bad_signature_and_replay_never_reach_health(self):
        good, _code = self.versioned(2000, b"v2000\n")
        self.apply(good, lambda repo, state: self.calls.append("ok") or None, state=self.state)
        older, _older_code = self.versioned(1999, b"v1999\n")
        forged, _forged_code = self.versioned(2001, b"forged\n")
        forged["version"] = 2002  # vsig still covers 2001

        def refuse(repo, state):
            self.calls.append("health")
            return "no"

        with self.assertRaisesRegex(ValueError, "older"):
            self.apply(older, refuse, state=self.state)
        with self.assertRaisesRegex(ValueError, "isn't signed"):
            self.apply(forged, refuse, state=self.state)
        unsigned = dict(good)
        unsigned["vsig"] = None
        unsigned["version"] = None
        unsigned["signature"] = "not a signature"
        with self.assertRaisesRegex(ValueError, "no version"):
            self.apply(unsigned, refuse, state=self.state)
        self.assertEqual(self.calls, ["ok"])
        self.assertEqual(deploy.installed_version(self.state), 2000)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"v2000\n")
        self.assertFalse((self.state / release.BAD).exists())

    def test_preflight_still_rejects_before_apply_or_health(self):
        broken = {"jarvis/__init__.py": b"", "jarvis/deploy.py": b"FLAGS = NO_WINDOW\n",
                  "jarvis/cluster.py": b"from . import deploy\n", "pyproject.toml": OLD_PROJECT}
        code = fingerprint(Path(self.tmp.name) / "broken", broken)
        bundle = pack(broken, code, self.main, 2000)

        def health(repo, state):
            self.calls.append("health")
            return None

        with self.assertRaisesRegex(ValueError, "doesn't start"):
            self.apply(bundle, health, state=self.state)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.repo / "jarvis.prev").exists())
        self.assertFalse((self.repo / "jarvis/deploy.py").exists())
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertFalse((self.state / release.BAD).exists())

    def test_backup_failure_stops_before_replacing_files(self):
        bundle, _ = self.versioned(2000, b"new\n")
        with patch("jarvis.deploy._keep_previous", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply(bundle, lambda repo, state: None, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())

    def test_false_health_result_is_a_failure(self):
        bundle, _ = self.versioned(2000, b"new\n")
        with self.assertRaisesRegex(ValueError, "restored the previous code"):
            self.apply(bundle, lambda repo, state: False, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")

    def _package_bundle(self, files, version):
        code = fingerprint(Path(self.tmp.name) / f"pkg-{version}", files)
        return pack(files, code, self.main, version), code

    def test_missing_agent_module_rolls_back_instead_of_skipping(self):
        # _starts imports only the modules that exist, so a package missing one of
        # NODE_MODULES used to pass the preflight. The post-apply check must not.
        files = agent_files(extra={"jarvis/a.py": b"new\n"}, drop=("remote",))
        bundle, code = self._package_bundle(files, 2000)
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("jarvis.cluster_setup.doctor", side_effect=AssertionError("doctor")):
            with self.assertRaisesRegex(ValueError, r"agent modules missing: remote"):
                deploy.apply_bundle(bundle, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.repo / "jarvis/__init__.py").exists())
        self.assertFalse((self.repo / "jarvis/remote.py").exists())
        self.assertEqual((self.repo / "jarvis/kept.py").read_bytes(), b"kept\n")
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertTrue(release.is_bad(self.state, code))

    def test_real_package_doctor_failure_is_not_skipped(self):
        files = agent_files(extra={"jarvis/a.py": b"new\n"})
        bundle, code = self._package_bundle(files, 2000)
        write_json(self.repo / "jarvis.config.json", node_config(enabled=False))
        write_json(self.state / "cluster-node.json", {
            "node_id": "dell", "main_device": "zenbook", "token": TOKEN, "code_key": CODE_KEY})
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("jarvis.cluster_setup.doctor", side_effect=AssertionError("doctor")):
            with self.assertRaisesRegex(ValueError, "cluster.enabled is false"):
                deploy.apply_bundle(bundle, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"old\n")
        self.assertFalse((self.repo / "jarvis/__init__.py").exists())
        self.assertEqual((self.repo / "pyproject.toml").read_bytes(), OLD_PROJECT)
        self.assertFalse((self.state / deploy.VERSION_FILE).exists())
        self.assertTrue(release.is_bad(self.state, code))
        self.assertNotIn(TOKEN, (self.state / release.BAD).read_text(encoding="utf-8"))

    def test_real_package_healthy_local_checks_record_the_version(self):
        files = agent_files(extra={"jarvis/a.py": b"new\n", "jarvis/kept.py": b"kept\n"})
        bundle, code = self._package_bundle(files, 2000)
        write_json(self.repo / "jarvis.config.json", node_config())
        write_json(self.state / "cluster-node.json", {
            "node_id": "dell", "main_device": "zenbook", "token": TOKEN, "code_key": CODE_KEY})
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("jarvis.cluster_setup.doctor", side_effect=AssertionError("doctor")):
            deploy.apply_bundle(bundle, self.key, self.repo, state=self.state)
        self.assertEqual((self.repo / "jarvis/a.py").read_bytes(), b"new\n")
        self.assertTrue((self.repo / "jarvis/__init__.py").is_file())
        self.assertTrue((self.repo / "jarvis/remote.py").is_file())
        self.assertEqual(deploy.installed_version(self.state), 2000)
        self.assertFalse(release.is_bad(self.state, code))
        self.assertFalse((self.state / release.BAD).exists())

    def test_non_package_tree_does_not_pretend_to_be_a_release_check(self):
        self.assertIsNone(deploy.release_health(self.repo, self.state))


class LocalDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.state = Path(self.tmp.name) / "node"
        self.state.mkdir()
        write_json(self.repo / "jarvis.config.json", node_config())

    def note(self, **extra):
        body = {"node_id": "dell", "main_device": "zenbook", "token": TOKEN, "code_key": CODE_KEY}
        body.update(extra)
        write_json(self.state / "cluster-node.json", body)

    def check(self, state=None):
        with patch("socket.create_connection", side_effect=AssertionError("network")):
            return deploy._local_doctor(self.repo, self.state if state is None else state)

    def test_matching_node_credential_is_healthy(self):
        self.note()
        self.assertIsNone(self.check())

    def test_main_does_not_need_a_node_credential(self):
        write_json(self.repo / "jarvis.config.json", node_config(local="zenbook"))
        self.assertIsNone(self.check())

    def test_disabled_cluster_and_unreadable_config_fail(self):
        write_json(self.repo / "jarvis.config.json", node_config(enabled=False))
        self.assertEqual(self.check(), "cluster.enabled is false")
        (self.repo / "jarvis.config.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.check(), "jarvis.config.json isn't a JSON object")
        (self.repo / "jarvis.config.json").unlink()
        self.assertEqual(self.check(), "jarvis.config.json can't be read")

    def test_identity_and_credential_failures_stay_local(self):
        write_json(self.repo / "jarvis.config.json", node_config(local="missing"))
        self.assertIn("local_device", self.check())
        write_json(self.repo / "jarvis.config.json", node_config())
        self.assertEqual(self.check(), "node credential can't be read")
        self.note(node_id="other")
        self.assertEqual(self.check(), "node credential does not match this PC's configured identity")
        self.note(token="short")
        reason = self.check()
        self.assertEqual(reason, "node credential is missing a token")
        self.assertNotIn("short", reason)
        self.note(code_key="ssh-rsa AAAA")
        self.assertEqual(self.check(), "node credential is missing the main's code-signing key")
        self.assertEqual(self.check(state=self.repo / "missing-state"), "state folder is missing")

    def test_state_omitted_after_a_readable_identity(self):
        self.assertIsNone(deploy._local_doctor(self.repo, None))

    def test_hostname_lookup_is_not_a_connection(self):
        # local_device auto reads the hostname. That must not open a socket.
        write_json(self.repo / "jarvis.config.json", node_config(local="auto"))
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("socket.socket.connect", side_effect=AssertionError("network")):
            deploy._local_doctor(self.repo, None)


if __name__ == "__main__":
    unittest.main()
