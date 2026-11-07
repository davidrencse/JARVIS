"""voice.stt_engine "parakeet" (default) / "moonshine": the fast engine first, Whisper whenever its words lack the wake
word or look like noise (listen.Transcriber, listen.plausible). Fakes only: no models."""
import unittest

import numpy as np

from jarvis.listen import PARAKEET_UNSURE_LOGPROB, Parakeet, Transcriber, plausible, unsure

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

    def test_default_is_parakeet_and_construction_loads_nothing(self):
        t = Transcriber({})
        self.assertIsInstance(t.fast, Parakeet)
        self.assertIsNone(t.fast._model)
        self.assertIsInstance(Transcriber({"voice": {"language": "de"}}).fast, Parakeet)
        self.assertIsNone(Transcriber({"voice": {"language": "ja"}}).fast)

    def test_whisper_engine_and_moonshine_language(self):
        self.assertIsNone(Transcriber({"voice": {"stt_engine": "whisper"}}).fast)
        self.assertIsNone(Transcriber({"voice": {"stt_engine": "moonshine", "language": "de"}}).fast)

    def parakeet(self, out, whisper_text="Jarvis, open chrome", error=None):
        t = Transcriber({})
        calls = []

        def fast(audio):
            calls.append("parakeet")
            if error:
                raise error
            return out

        def whisper(audio):
            calls.append("whisper")
            return whisper_text
        t.fast, t._whisper = fast, whisper
        return t, calls

    def test_parakeet_tuple_used_with_stats(self):
        t, calls = self.parakeet(("Jarvis open chrome", -0.1))
        self.assertEqual(t(CLIP), "Jarvis open chrome")
        self.assertEqual(calls, ["parakeet"])
        self.assertEqual(t.last_engine, "parakeet")
        self.assertEqual(t.last, {"logprob": -0.1, "no_speech": None, "engine": "parakeet"})
        self.assertFalse(unsure(t.stats(CLIP)))

    def test_parakeet_without_wake_word_falls_to_whisper(self):
        t, calls = self.parakeet(("Do I just open chrome", -0.1))
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertEqual(calls, ["parakeet", "whisper"])
        self.assertEqual(t.last_engine, "whisper")

    def test_parakeet_error_falls_back_for_good(self):
        t, calls = self.parakeet(("", None), error=ImportError("no onnx_asr"))
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertIsNone(t.fast)
        t(CLIP)
        self.assertEqual(calls, ["parakeet", "whisper", "whisper"])

    def test_path_goes_straight_to_whisper(self):
        t, calls = self.parakeet(("Jarvis open chrome", -0.1))
        self.assertEqual(t("/tmp/clip.wav"), "Jarvis, open chrome")
        self.assertEqual(calls, ["whisper"])
        self.assertIsNotNone(t.fast)

    def test_parakeet_unsure_threshold(self):
        base = {"no_speech": None, "engine": "parakeet"}
        self.assertTrue(unsure({**base, "logprob": PARAKEET_UNSURE_LOGPROB - 0.1}))
        self.assertFalse(unsure({**base, "logprob": -0.5}))
        self.assertFalse(unsure({**base, "logprob": None}))
        # -0.95 is fine for Whisper's rule only above -1.0; parakeet flags it
        self.assertTrue(unsure({**base, "logprob": -0.95}))
        self.assertFalse(unsure({"logprob": -0.95, "no_speech": 0.0}))

    def test_parakeet_loads_in_background_and_whisper_answers_meanwhile(self):
        import threading
        from jarvis import listen
        gate, done = threading.Event(), threading.Event()

        class Model:
            def recognize(self, audio, sample_rate):
                return type("R", (), {"text": "Jarvis open chrome", "logprobs": [-0.1]})()

        p = Parakeet()

        def load():
            gate.wait(2)
            p._model = Model()
            with p._lock:
                p._loading = None
            done.set()
        p._load = load
        self.assertIsNone(p(CLIP))  # still loading: caller uses Whisper
        gate.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(p(CLIP), ("Jarvis open chrome", -0.1))

    def test_parakeet_failed_load_retries_later_missing_package_is_final(self):
        p = Parakeet()
        loads = []

        def fail():
            loads.append(1)
            p.error, p._failed_at = OSError("offline"), __import__("time").monotonic()
            p._loading = None
        p._load = fail
        p._loading = None
        import threading
        orig = threading.Thread

        class Now:  # run the load inline
            def __init__(self, target, **kw):
                self.target = target

            def start(self):
                self.target()
        threading.Thread = Now
        try:
            self.assertIsNone(p(CLIP))
            self.assertIsNone(p(CLIP))  # inside the retry wait: no second load
            self.assertEqual(len(loads), 1)
            p._failed_at -= Parakeet.RETRY + 1
            p(CLIP)
            self.assertEqual(len(loads), 2)
        finally:
            threading.Thread = orig
        p.missing = ImportError("no onnx_asr")
        with self.assertRaises(ImportError):
            p(CLIP)

    def test_transient_parakeet_error_keeps_it_on(self):
        t, calls = self.parakeet(("", None), error=ValueError("bad clip"))
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertIsNotNone(t.fast)

    def test_still_loading_goes_to_whisper(self):
        t, calls = self.parakeet(None)
        self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertEqual(calls, ["parakeet", "whisper"])

    def test_no_wake_needed_trusts_sure_parakeet(self):
        t, calls = self.parakeet(("then check it", -0.1))
        t.need_wake = False
        self.assertEqual(t(CLIP), "then check it")
        self.assertEqual(calls, ["parakeet"])

    def test_no_wake_needed_unsure_parakeet_goes_to_whisper(self):
        t, calls = self.parakeet(("then check it", PARAKEET_UNSURE_LOGPROB - 1))
        t.need_wake = False
        t(CLIP)
        self.assertEqual(calls, ["parakeet", "whisper"])

    def test_cuda_node_never_uses_parakeet(self):
        from unittest import mock
        from jarvis import listen
        t = Transcriber({})
        t._whisper = lambda audio: "Jarvis, open chrome"
        with mock.patch.object(listen, "_has_cuda", return_value=True):
            self.assertEqual(t(CLIP), "Jarvis, open chrome")
        self.assertIsNone(t.fast)

    def test_parakeet_short_clip_is_empty(self):
        self.assertEqual(Parakeet()(np.zeros(100, dtype="float32")), ("", None))

    def test_plausible(self):
        self.assertTrue(plausible("Jarvis open chrome", 1.5))
        self.assertFalse(plausible("", 2))
        self.assertFalse(plausible(" ".join(["word"] * 40), 2))


if __name__ == "__main__":
    unittest.main()
