"""pipeline-core audit 2026-11-04: router.classify and the wake-word checks run on every
request; long pastes must not take seconds."""
import time
import unittest

from jarvis import assistant, router


class LongInputTests(unittest.TestCase):
    def test_classify_long_brief_is_fast(self):
        start = time.monotonic()
        result = router.classify("build " * 4000)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result["tier"], "complex")
        self.assertEqual(router.classify("build me a dashboard app")["tier"], "complex")
        self.assertEqual(router.classify("what time is it")["tier"], "simple")

    def test_wake_word_on_long_whitespace_is_fast(self):
        for text in ("x" + " " * 20000, " " * 20000 + "x" + " " * 20000, "x" + " " * 20000 + ", Jarvis?"):
            start = time.monotonic()
            assistant.wake_word(text)
            assistant.confirmation_words(text)
            self.assertLess(time.monotonic() - start, 0.3, len(text))
        self.assertEqual(assistant.wake_word("x" + " " * 20000 + ", Jarvis?"), "x")
        self.assertEqual(assistant.wake_word("what time is it, Jarvis?\n\n"), "what time is it")
        self.assertIsNone(assistant.wake_word("Who is Jarvis?"))
        self.assertTrue(assistant.confirmation_words("Jarvis, yes"))


if __name__ == "__main__":
    unittest.main()
