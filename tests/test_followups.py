import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import autostart
from jarvis.actions import is_action
from jarvis.memory import Memory, MemoryTools, is_memory_request


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

    def test_how_many_apps_is_an_action(self):
        # Went to the tool-less talk lane, which answered "I can't inspect your computers".
        self.assertTrue(is_action("how many apps are on my desktop and my Dell computer?"))
        self.assertTrue(is_action("count the shortcuts on my dell"))

    def test_memory_tools_remember_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, text = MemoryTools(Memory(tmp, semantic=False)).call(
                "memory_remember", {"text": "My locker code is 4512"})
            self.assertTrue(ok, text)
            tools = MemoryTools(Memory(tmp, semantic=False))  # a new process, same state
            ok, found = tools.call("memory_search", {"query": "locker code"})
            self.assertTrue(ok)
            self.assertIn("4512", found)
            ok, empty = tools.call("memory_remember", {"text": "  "})
            self.assertFalse(ok)

    def test_conversation_recent_reaches_past_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(tmp, semantic=False)
            memory.add("message", "how many apps are on my dell?")
            old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            with memory.connect() as db:  # older than the 20-minute recall window
                db.execute("UPDATE items SET created_at=?", (old,))
            memory.add("reply", "Forty-two.", source="jarvis")
            ok, text = MemoryTools(memory).call("conversation_recent", {"turns": 5})
        self.assertTrue(ok)
        self.assertIn("David: how many apps are on my dell?", text)
        self.assertIn("JARVIS: Forty-two.", text)

    def test_memory_questions(self):
        for text in ("what did I tell you about my exam", "do you remember my locker code",
                     "what do you know about me"):
            self.assertTrue(is_memory_request(text), text)
        self.assertFalse(is_memory_request("open spotify"))

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
