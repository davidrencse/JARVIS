"""Wake word: Parakeet's mishearings of "Jarvis" (listen.wake, listen.fuzzy_wake) and the
Whisper second opinion they trigger in listen.Transcriber. Fakes only: no models."""
import unittest

import numpy as np

from jarvis.listen import Transcriber, fuzzy_wake, wake

CLIP = np.zeros(32000, dtype="float32")

# What Parakeet / Kokoro runs produced for a spoken "Jarvis ..." (2026-11-07 measurements).
MISHEARD = ["Try this stall.", "Drivers summarized my unread email.", "Okay, Javi, open Spotify.",
            "Java Star.", "jar this, stop", "Jarvi, what time is it", "Jorvis, stop",
            "Darvis start a new plan", "Garbus turn the volume down", "Driver start a new plan",
            "Who Jarvis, what time is it?", "Well please Jarvis summarized my email"]
EXACT_VARIANTS = {"Okay, Jovers, how much did I spend?": "how much did I spend?",
                  "Jarbus, open Spotify.": "open Spotify.", "Jovis, stop.": "stop.",
                  "Hey Jarvis, stop": "stop", "Jervis open chrome": "open chrome"}
# Ordinary openings that must not even ask Whisper (follow-ups, push-to-talk, the room).
PLAIN = ["then check it", "yes please", "open spotify", "what time is it", "turn off the lights",
         "the weather tomorrow", "does it work", "do I just start", "service is slow",
         "I was talking to Jarvis", "so what do you think", "play some music", "no thanks"]
# Real words that sound like it: may ask Whisper, but are never the wake word themselves.
NEVER_WAKE = ["Travis, open chrome", "Drivers are late", "Java is a language", "Try this one",
              "jars of jam", "I was talking to Jarvis", "Jarvis's voice is odd", "service, please"]


class WakeTableTests(unittest.TestCase):
    def test_misheard_openings_ask_whisper(self):
        for text in MISHEARD:
            with self.subTest(text=text):
                self.assertTrue(fuzzy_wake(text))

    def test_exact_variants_count(self):
        for text, request in EXACT_VARIANTS.items():
            with self.subTest(text=text):
                self.assertEqual(wake(text), request)

    def test_plain_openings_stay_plain(self):
        for text in PLAIN:
            with self.subTest(text=text):
                self.assertFalse(fuzzy_wake(text))

    def test_sound_alikes_are_never_the_wake_word(self):
        for text in NEVER_WAKE:
            with self.subTest(text=text):
                self.assertIsNone(wake(text))

    def test_only_the_start_counts(self):
        self.assertFalse(fuzzy_wake("I told my drivers"))
        self.assertTrue(fuzzy_wake("um, drivers summarized"))


class StandInAssistantTests(unittest.TestCase):
    """A stand-in assistant module without WAKE_LEAD / WAKE_WORDS (tests elsewhere stub it)
    must not break fuzzy_wake: it falls back to common fillers and the plain name."""

    def test_fuzzy_wake_without_wake_lead(self):
        import sys
        import types
        from unittest.mock import patch
        stub = types.ModuleType("jarvis.assistant")
        stub.wake_word = lambda text, names=None: None
        with patch.dict(sys.modules, {"jarvis.assistant": stub}):
            self.assertTrue(fuzzy_wake("Okay, Jovers, open Spotify"))
            self.assertTrue(fuzzy_wake("um, drivers summarized"))
            self.assertFalse(fuzzy_wake("open spotify"))


class SecondOpinionTests(unittest.TestCase):
    def make(self, fast_out, whisper_text):
        t = Transcriber({})
        calls = []

        def fast(audio):
            calls.append("parakeet")
            return fast_out

        def whisper(audio):
            calls.append("whisper")
            return whisper_text
        t.fast, t._whisper = fast, whisper
        return t, calls

    def test_fuzzy_goes_to_whisper(self):
        t, calls = self.make(("Okay, Javi, open chrome", -0.1), "Okay Jarvis, open chrome")
        self.assertEqual(t(CLIP), "Okay Jarvis, open chrome")
        self.assertEqual(calls, ["parakeet", "whisper"])
        self.assertTrue(t.fuzzy)
        self.assertEqual((t.rescued, t.second_opinions), (1, 1))

    def test_whisper_agreeing_on_a_real_word_is_rejected(self):
        t, calls = self.make(("Drivers are late", -0.1), "Drivers are late.")
        out = t(CLIP)
        self.assertEqual(calls, ["parakeet", "whisper"])
        self.assertIsNone(wake(out))
        self.assertFalse(t.fuzzy)
        self.assertEqual((t.rescued, t.second_opinions), (0, 1))

    def test_travis_stays_rejected(self):
        t, calls = self.make(("Travis, open chrome", -0.1), "Travis, open Chrome.")
        self.assertIsNone(wake(t(CLIP)))
        self.assertEqual(calls, ["parakeet", "whisper"])

    def test_exact_variant_needs_no_second_pass(self):
        t, calls = self.make(("Okay, Jovers, open chrome", -0.2), "unused")
        self.assertEqual(t(CLIP), "Okay, Jovers, open chrome")
        self.assertEqual(calls, ["parakeet"])
        self.assertIsNone(t.fuzzy)

    def test_sure_words_without_the_name_never_load_whisper(self):
        # Every clip near the mic without "Jarvis" used to go through Whisper as well:
        # both models stayed loaded (~2 GB) and the listener was OOM-killed at its 3 GB cap.
        t, calls = self.make(("pass me the salt", -0.1), "unused")
        self.assertIsNone(wake(t(CLIP)))
        self.assertEqual(calls, ["parakeet"])

    def test_cpu_whisper_is_dropped_after_a_second_opinion(self):
        t, calls = self.make(("Okay, Javi, open chrome", -0.1), "Okay Jarvis, open chrome")
        t.fast._model = object()  # Parakeet is loaded
        t.device, t._model = "cpu", object()
        t(CLIP)
        self.assertIsNone(t._model)

    def test_cpu_whisper_loaded_during_parakeet_warmup_is_dropped(self):
        t, calls = self.make(("Jarvis, open chrome", -0.1), "unused")
        t.fast._model = object()
        t.device, t._model = "cpu", object()  # answered clips while Parakeet loaded
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertIsNone(t._model)

    def test_plain_follow_up_still_trusts_parakeet(self):
        t, calls = self.make(("then check it", -0.1), "unused")
        self.assertEqual(t(CLIP), "then check it")
        self.assertEqual(calls, ["parakeet"])
        self.assertIsNone(t.fuzzy)


if __name__ == "__main__":
    unittest.main()
