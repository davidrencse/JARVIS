"""A ChatGPT usage limit hit after part of the answer was spoken is not answered again."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.assistant import ask
from jarvis.memory import Memory
from jarvis.store import Store


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)


class SpokenLimitNoSecondAnswer(Temp):
    def test_limit_after_speaking_is_not_answered_again_by_the_stand_in(self):
        exc = RuntimeError("ChatGPT error: You've hit your usage limit.")
        exc.spoken = True

        class Spoke:
            def ask(self, *_, **__):
                raise exc
        mem = Memory(self.base / "s")
        store = Store(self.base / "s", lanes=1)
        with patch("jarvis.assistant._standin_reply", side_effect=AssertionError("answered twice")), \
                patch("jarvis.assistant._fastlane", side_effect=AssertionError("one-shot ran")):
            result = ask("what is the capital of Mongolia?", store, mem,
                         {"workspace_roots": ["."]}, self.base, lane=Spoke())
        self.assertIn("usage limit", result["reply"])


if __name__ == "__main__":
    unittest.main()
