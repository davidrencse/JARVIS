"""voice.stt_engine "moonshine": Moonshine first, Whisper whenever its words lack the wake
word or look like noise (listen.Transcriber, listen.plausible). Fakes only: no models."""
import unittest

import numpy as np

from jarvis.listen import Transcriber, plausible, unsure

CLIP = np.zeros(32000, dtype="float32")  # 2 s


class FastFirstTests(unittest.TestCase):
    def make(self, fast_text, whisper_text="Jarvis, open chrome", fast_error=None):
        t = Transcriber({"voice": {"stt_engine": "moonshine"}})
        calls = []

        def fast(audio):
            calls.append("moonshine")
            if fast_error:
                raise fast_error
            return fast_text

        def whisper(audio):
            calls.append("whisper")
            return whisper_text
        t.fast, t._whisper = fast, whisper
        return t, calls

    def test_wake_word_heard_fast_skips_whisper(self):
        t, calls = self.make("Jarvis open chrome")
        self.assertEqual(t(CLIP), "Jarvis open chrome")
        self.assertEqual(calls, ["moonshine"])
        self.assertEqual(t.last_engine, "moonshine")
        self.assertFalse(unsure(t.stats(CLIP)))

    def test_no_wake_word_goes_to_whisper(self):
        t, calls = self.make("Drive a star new plan")
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertEqual(calls, ["moonshine", "whisper"])
        self.assertEqual(t.last_engine, "whisper")

    def test_noise_loop_goes_to_whisper(self):
        t, calls = self.make("Jarvis the the the the the the")
        t(CLIP)
        self.assertEqual(calls, ["moonshine", "whisper"])

    def test_broken_moonshine_falls_back_for_good(self):
        t, calls = self.make("", fast_error=ImportError("no moonshine_onnx"))
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertIsNone(t.fast)
        t(CLIP)
        self.assertEqual(calls, ["moonshine", "whisper", "whisper"])

    def test_off_by_default_and_for_other_languages(self):
        self.assertIsNone(Transcriber({}).fast)
        self.assertIsNone(Transcriber({"voice": {"stt_engine": "moonshine", "language": "de"}}).fast)

    def test_plausible(self):
        self.assertTrue(plausible("Jarvis open chrome", 1.5))
        self.assertFalse(plausible("", 2))
        self.assertFalse(plausible(" ".join(["word"] * 40), 2))


if __name__ == "__main__":
    unittest.main()
