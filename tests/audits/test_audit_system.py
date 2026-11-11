"""Audit fixes: Windows/cluster storage, listener detached from systemd units, player reaping."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from jarvis import daemon, speech
from jarvis.storage import Storage

WIN_ROOT = "C:\\Users\\David"
CONFIG = {
    "devices": {
        "dell": {"transport": "cluster", "label": "Dell G15, Windows 11"},
        "zenbook": {"transport": "ssh", "target": "jdoe@host"},
    },
}


class WindowsStorage(unittest.TestCase):
    def storage(self):
        store = Storage(CONFIG)
        store.is_remote = lambda device_id: device_id == "dell"
        return store

    def listing(self, path):
        return {"path": path, "dir": True, "size": 0, "roots": [WIN_ROOT], "sep": "\\",
                "entries": [{"name": "a.txt", "dir": False, "size": 1, "mtime": 0}]}

    def test_windows_parent_listed(self):
        store = self.storage()
        with patch.object(store, "resolve", return_value=self.listing(WIN_ROOT + "\\Docs")):
            res = store.list("dell", WIN_ROOT + "\\Docs")
        self.assertEqual(res["parent"], WIN_ROOT)
        self.assertEqual(res["sep"], "\\")

    def test_windows_root_has_no_parent(self):
        store = self.storage()
        with patch.object(store, "resolve", return_value=self.listing(WIN_ROOT)):
            self.assertIsNone(store.list("dell", WIN_ROOT)["parent"])

    def test_cluster_device_copy_refused(self):
        store = self.storage()
        store.is_remote = lambda device_id: device_id == "dell"
        with patch.object(store, "resolve") as resolve, patch("subprocess.run") as run:
            with self.assertRaises(ValueError) as caught:
                store.copy("dell", WIN_ROOT + "\\a.txt", "local", "/tmp")
        self.assertIn("needs SSH", str(caught.exception))
        resolve.assert_not_called()
        run.assert_not_called()

    def test_windows_name_split(self):
        store = self.storage()
        store.is_remote = lambda device_id: device_id == "zenbook"
        CONFIG_SSH = {"devices": {"zenbook": {"transport": "ssh", "target": "jdoe@host"}}}
        store.config = CONFIG_SSH
        src = {"path": "C:\\Users\\David\\Docs\\a.txt", "dir": False, "size": 1, "sep": "\\"}
        dst = {"path": "/tmp", "dir": True, "size": 0}
        seen = []

        def resolve(device_id, path, action="resolve"):
            if action == "exists":
                return {"exists": False}
            return src if device_id == "zenbook" else dst

        with patch.object(store, "resolve", side_effect=resolve), \
                patch("shutil.which", return_value="/usr/bin/scp"), \
                patch("jarvis.storage._check_target"), \
                patch("subprocess.run", side_effect=lambda *a, **k: seen.append(a[0]) or
                      MagicMock(returncode=0)):
            res = store.copy("zenbook", src["path"], "local", "/tmp")
        self.assertEqual(res["copied"], "a.txt")


class SystemdScope(unittest.TestCase):
    CMD = ["python", "-m", "jarvis", "voice", "listen"]

    def run_case(self, platform, env, which):
        with patch.object(daemon.sys, "platform", platform), \
                patch.dict(daemon.os.environ, env, clear=True), \
                patch("jarvis.daemon.shutil.which", return_value=which), \
                patch("pathlib.Path.read_text", side_effect=OSError):
            return daemon._detach_from_unit(list(self.CMD))

    def test_wrapped_under_unit(self):
        cmd = self.run_case("linux", {"INVOCATION_ID": "x"}, "/usr/bin/systemd-run")
        self.assertEqual(cmd[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertTrue(any(a.startswith("--unit=jarvis-voice-") for a in cmd))
        self.assertEqual(cmd[-len(self.CMD):], self.CMD)

    def test_cgroup_detection(self):
        with patch.object(daemon.sys, "platform", "linux"), \
                patch.dict(daemon.os.environ, {}, clear=True), \
                patch("jarvis.daemon.shutil.which", return_value="/usr/bin/systemd-run"), \
                patch("pathlib.Path.read_text",
                      return_value="0::/user.slice/user@1000.service/app.slice/jarvis-cluster.service"):
            self.assertEqual(daemon._detach_from_unit(list(self.CMD))[0], "systemd-run")

    def test_unchanged_otherwise(self):
        self.assertEqual(self.run_case("linux", {"INVOCATION_ID": "x"}, None), self.CMD)
        self.assertEqual(self.run_case("linux", {}, "/usr/bin/systemd-run"), self.CMD)
        self.assertEqual(self.run_case("win32", {"INVOCATION_ID": "x"}, "/x"), self.CMD)
        self.assertEqual(self.run_case("darwin", {"INVOCATION_ID": "x"}, "/x"), self.CMD)


class PlayerTimeout(unittest.TestCase):
    def test_timeout_kills_and_reaps(self):
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("pw-play", 120), 0]
        with patch.object(speech.sys, "platform", "linux"), \
                patch.object(speech, "_playing", None), \
                patch("jarvis.speech.shutil.which", return_value="/usr/bin/pw-play"), \
                patch("jarvis.speech.subprocess.Popen", return_value=proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                speech.play_wav("x.wav", wait=True)
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
