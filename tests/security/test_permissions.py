"""Settings → Permissions (2026-11-07): what JARVIS may do, and loosening needs a yes."""
import json
import tempfile
import unittest
from pathlib import Path

from jarvis import settings


def config():
    return {"devices": {"main": {"hostname": "m"}}, "rules": {"confirm": "ask"},
            "actions": {"enabled": True}, "voice": {}}


class PermissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.state.mkdir()
        self.path = Path(tmp.name) / "jarvis.config.json"
        self.config = config()
        self.path.write_text(json.dumps(self.config))

    def fields(self):
        return {f["path"]: f for f in settings.read(self.config, self.state)["fields"]}

    def test_page_lists_the_switches_with_defaults(self):
        f = self.fields()
        perms = [p for p, v in f.items() if v["section"] == "permissions"]
        for p in ("rules.confirm", "actions.enabled", "actions.browser", "actions.grokbot",
                  "vault.write", "terminal.sandbox", "lan_mouse.enabled", "lan_mouse.off_when_apart"):
            self.assertIn(p, perms)
        self.assertIs(f["terminal.sandbox"]["value"], True)  # unset = on, as terminal.py treats it
        self.assertEqual(f["rules.confirm"]["loosens"], "never")
        info = settings.read(self.config, self.state)["permissions"]
        self.assertEqual(info["always_ask"], ["money", "messages"])
        self.assertTrue(info["never"] and info["stop"])

    def test_tightening_saves_without_asking(self):
        settings.write(self.config, self.path, self.state, {"actions.enabled": False})
        self.assertIs(json.loads(self.path.read_text())["actions"]["enabled"], False)

    def test_loosening_needs_confirmation(self):
        for key, value in (("rules.confirm", "never"), ("terminal.sandbox", False),
                           ("lan_mouse.off_when_apart", False)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "safety net"):
                    settings.write(self.config, self.path, self.state, {key: value})
                settings.write(self.config, self.path, self.state, {key: value}, confirmed=True)
                disk = json.loads(self.path.read_text())
                section, leaf = key.split(".")
                self.assertEqual(disk[section][leaf], value)

    def test_confirmed_must_be_exactly_true(self):
        with self.assertRaises(ValueError):
            settings.write(self.config, self.path, self.state, {"rules.confirm": "never"}, confirmed="yes")

    def test_always_ask_and_refusals_are_not_editable(self):
        with self.assertRaisesRegex(ValueError, "can't be changed"):
            settings.write(self.config, self.path, self.state, {"rules.always_ask": []})

    def test_brain_tool_switches_flag_a_listener_restart(self):
        out = settings.write(self.config, self.path, self.state, {"actions.browser": False})
        self.assertIn("actions.browser", out["restart"])


if __name__ == "__main__":
    unittest.main()
