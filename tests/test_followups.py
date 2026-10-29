import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import autostart
from jarvis.actions import is_action
from jarvis.memory import Memory


class FollowUpTests(unittest.TestCase):
    def test_recent_turns_reach_the_next_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(tmp, semantic=False)
            memory.add("message", "what apps are on my Windows laptop?")
            memory.add("reply", "I can check it if you want.", source="jarvis")
            now = memory.add("message", "go check")
            talk = memory.conversation(skip={now["id"]})
        self.assertEqual(talk, ["David: what apps are on my Windows laptop?",
                                "JARVIS: I can check it if you want."])

    def test_app_questions_and_go_check_are_actions(self):
        for text in ("what apps are on my Windows laptop?", "is spotify installed on my windows laptop",
                     "go check", "list the apps on arch"):
            self.assertTrue(is_action(text), text)
        for text in ("what is the capital of france", "tell me a joke"):
            self.assertFalse(is_action(text), text)

    def test_autostart_prefers_the_project_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            venv.parent.mkdir(parents=True)
            venv.write_text("")
            with mock.patch.object(autostart, "ROOT", root):
                argv = autostart.interpreter([sys.executable, "-m", "jarvis"])
                other = autostart.interpreter(["/opt/custom/python", "-m", "jarvis"])
        self.assertEqual(argv[0], str(venv))
        self.assertEqual(other[0], "/opt/custom/python")  # an explicit interpreter is kept


if __name__ == "__main__":
    unittest.main()
