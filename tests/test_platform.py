"""Running on the Arch laptop: config written from the Dell's side must still work."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import daemon, overlay
from jarvis.actions import DesktopTools, desktop_entries
from jarvis.devices import devices_here
from jarvis.vault import vault_path

DEVICES = {
    "dell": {"transport": "local", "hostname": "WinLaptop"},
    "zenbook": {"transport": "ssh", "target": "jdoe@172.1.42.215", "hostname": "archmain",
                "identity_file": "~/.ssh/jarvis_ed25519", "host_fingerprint": "SHA256:x"},
}


class DevicesHereTests(unittest.TestCase):
    def test_on_the_dell_nothing_changes(self):
        self.assertEqual(devices_here(DEVICES, "winlaptop"), DEVICES)

    def test_unknown_host_keeps_the_config(self):
        self.assertEqual(devices_here(DEVICES, "elsewhere"), DEVICES)

    def test_on_the_zenbook_it_is_local_and_the_dell_has_no_route(self):
        here = devices_here(DEVICES, "archmain")
        self.assertEqual(list(here), ["zenbook"])
        self.assertEqual(here["zenbook"]["transport"], "local")
        self.assertNotIn("target", here["zenbook"])
        self.assertEqual(DEVICES["zenbook"]["transport"], "ssh")  # config left alone


class VaultPathTests(unittest.TestCase):
    def test_per_platform_paths(self):
        paths = {"path": {"windows": r"C:\Users\jdoe\Documents\Vault", "linux": "~/Vault"}}
        self.assertEqual(vault_path(paths),
                         r"C:\Users\jdoe\Documents\Vault" if os.name == "nt" else "~/Vault")

    @unittest.skipIf(os.name == "nt", "POSIX behaviour")
    def test_windows_path_is_unconfigured_off_windows(self):
        self.assertIsNone(vault_path({"path": r"C:\Users\jdoe\Documents\Vault"}))
        self.assertIsNone(vault_path({"path": {"windows": r"C:\Vault"}}))
        self.assertEqual(vault_path({"path": "~/Vault"}), "~/Vault")


@unittest.skipIf(os.name == "nt", "XDG desktop entries")
class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        apps = Path(self.tmp.name, "applications")
        (apps / "kde").mkdir(parents=True)
        entries = {
            "firefox.desktop": "Type=Application\nName=Firefox\nExec=firefox %u",
            "kde/okular.desktop": "Type=Application\nName=Okular\nExec=okular",
            "hidden.desktop": "Type=Application\nName=Secret\nNoDisplay=true",
            "link.desktop": "Type=Link\nName=A link",
        }
        for name, body in entries.items():
            (apps / name).write_text(f"[Desktop Entry]\n{body}\n")
        (apps / "broken.desktop").write_text("no section header\n")
        self.env = patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name,
                                           "XDG_DATA_DIRS": self.tmp.name + "/none"})
        self.env.start()
        self.dirs = patch("jarvis.actions._application_dirs", return_value=[apps])
        self.dirs.start()

    def tearDown(self):
        self.dirs.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_lists_visible_applications_with_desktop_ids(self):
        apps = {a["Name"]: a["AppID"] for a in desktop_entries()}
        self.assertEqual(apps, {"Firefox": "firefox.desktop", "Okular": "kde-okular.desktop"})

    @patch("jarvis.actions.shutil.which", return_value="/usr/bin/gio")
    @patch("jarvis.actions.subprocess.Popen")
    def test_open_app_launches_the_entry_file(self, popen, _which):
        ok, text = DesktopTools().call("open_app", {"name": "firefox"})
        self.assertTrue(ok, text)
        self.assertEqual(popen.call_args[0][0][:2], ["gio", "launch"])
        self.assertTrue(popen.call_args[0][0][2].endswith("firefox.desktop"))


@unittest.skipIf(os.name == "nt", "systemd user unit")
class LinuxAutostartTests(unittest.TestCase):
    def test_voice_autostart_writes_and_removes_a_user_unit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
                patch("jarvis.daemon._systemctl", return_value=True) as systemctl:
            made = daemon.autostart(Path(tmp, "state"), True)
            unit = Path(made["unit"]).read_text()
            self.assertIn("-m jarvis --state", unit)
            self.assertIn("voice listen", unit)  # systemd watches the listener itself
            self.assertIn("Type=simple", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("WantedBy=default.target", unit)
            systemctl.assert_any_call("enable", daemon.UNIT_NAME)
            self.assertTrue(daemon.status(Path(tmp, "state"))["autostart"])
            self.assertEqual(daemon.autostart(Path(tmp, "state"), False), {"autostart": False})
            self.assertFalse(Path(made["unit"]).exists())

    def test_overlay_autostart_refuses_instead_of_writing_a_vbs(self):
        with self.assertRaises(RuntimeError):
            overlay.autostart("state", True)



@unittest.skipIf(os.name == "nt", "Windows relies on the profile's ACL")
class PrivateStateTests(unittest.TestCase):
    def test_state_folder_is_closed_to_other_accounts(self):
        from jarvis.platforms import private_dir
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / ".jarvis"
            state.mkdir(mode=0o755)
            state.chmod(0o755)
            private_dir(state)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            private_dir(Path(d) / "fresh")  # created private too
            self.assertEqual((Path(d) / "fresh").stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
