"""Offline voice eval harness (jarvis.voice_eval): metrics, augmentation and the speaker /
wake scoring on a 3-clip fixture with a stub embedder and stub engines. No models, no mic."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from jarvis import voice_eval as ve


class MetricTests(unittest.TestCase):
    def test_rates_eer_and_frr_at_far(self):
        pos, neg = [0.6, 0.7, 0.8, 0.4], [0.1, 0.2, 0.3, 0.45]
        self.assertEqual(ve.rates(pos, neg, 0.5), (0.0, 0.25))
        rate, t = ve.eer(pos, neg)
        self.assertAlmostEqual(rate, 0.25)
        frr, t1 = ve.frr_at_far(pos, neg, 0.0)
        self.assertEqual((frr, t1), (0.25, 0.6))
        self.assertEqual(ve.eer([0.9], [0.1])[0], 0.0)

    def test_wer(self):
        self.assertEqual(ve.wer("Jarvis, stop.", "jarvis stop"), 0.0)
        self.assertAlmostEqual(ve.wer("Jarvis open chrome", "Drivers open chrome"), 1 / 3)

    def test_mix_noise_hits_the_snr(self):
        rng = np.random.default_rng(0)
        tone = (0.1 * np.sin(np.arange(16000) / 5)).astype("float32")
        for kind in ("white", "pink"):
            noisy = ve.mix_noise(tone, 9.4, rng, kind)
            snr = 20 * np.log10(ve.rms(tone) / ve.rms(noisy - tone))
            self.assertAlmostEqual(snr, 9.4, delta=0.2)

    def test_reverb_keeps_length_and_peak(self):
        rng = np.random.default_rng(0)
        a = np.zeros(8000, dtype="float32")
        a[100] = 0.5
        wet = ve.reverb(a, 0.3, rng)
        self.assertEqual(len(wet), len(a))
        self.assertAlmostEqual(float(np.abs(wet).max()), 0.5, places=5)
        self.assertGreater(float(np.abs(wet[2000:]).sum()), 0)  # a tail exists


def unit(v):
    return (v / np.linalg.norm(v)).astype("float32")


class FixtureTests(unittest.TestCase):
    """Clips are fake: an 'audio' array whose first 32 values are the embedding."""

    def setUp(self):
        rng = np.random.default_rng(5)
        me = rng.normal(size=32)
        self.enrolled_audio = [np.concatenate([unit(me + rng.normal(scale=0.4, size=32)),
                                               np.zeros(10)]).astype("float32") for _ in range(3)]
        self.stranger = np.concatenate([unit(rng.normal(size=32)), np.zeros(10)]).astype("float32")
        self.embed = lambda a: unit(np.asarray(a[:32], dtype="float32"))

    def test_speaker_report(self):
        enrolled = [self.embed(a) for a in self.enrolled_audio]
        positives = [("clean", i, a) for i, a in enumerate(self.enrolled_audio)]
        out = ve.evaluate_speaker(enrolled, positives, [("kokoro:x", self.stranger)], self.embed,
                                  profile=[e.tolist() for e in enrolled] + [self.embed(self.stranger).tolist()] * 3,
                                  pinned=3)
        self.assertEqual((out["positives"], out["negatives"]), (3, 1))
        self.assertEqual(out["eer"], 0.0)
        self.assertEqual(out["at_chosen"]["far"], 0.0)
        self.assertIn("kokoro", out["negative_max_by_source"])
        live = out["live_profile"]
        self.assertEqual(live["trusted_learned"], 0)  # the stranger's learned copies are ignored
        self.assertEqual(live["far_anchored"], 0.0)
        self.assertEqual(live["far_old_scoring"], 1.0)  # the old rule accepted him

    def test_wake_report(self):
        clip = np.zeros(16000, dtype="float32")
        fast_out = {"a": ("Try this stall.", -0.2), "b": ("Jarvis, stop.", -0.1),
                    "c": ("Drivers are late.", -0.1), "d": ("Open the window.", -0.1)}
        whisper_out = {"a": "Jarvis, stop.", "b": "Jarvis, stop.", "c": "Drivers are late.",
                       "d": "Open the window."}
        items, order = [], []
        for key, ref in (("a", "Jarvis, stop."), ("b", "Jarvis, stop."),
                         ("c", "Drivers are late."), ("d", "Open the window.")):
            items.append((key, clip, ref))
            order.append(key)
        calls = iter(order)
        current = {}

        def fast(audio):
            current["k"] = next(calls)
            return fast_out[current["k"]]
        out = ve.evaluate_wake(items, fast, lambda audio: whisper_out[current["k"]])
        self.assertEqual(out["parakeet"]["recall"], 0.5)
        self.assertEqual(out["parakeet_fuzzy_whisper"]["recall"], 1.0)
        self.assertEqual(out["owner_path"]["recall"], 1.0)
        self.assertEqual(out["parakeet_fuzzy_whisper"]["false_wake"], 0.0)
        self.assertEqual(out["fuzzy_hints"], 2)  # "Try this", "Drivers"
        self.assertEqual(out["parakeet_misses"][0]["clip"], "a")
        self.assertEqual(out["wer"]["whisper"], 0.0)

    def test_wake_report_without_whisper(self):
        out = ve.evaluate_wake([("x", np.zeros(10), "Jarvis, stop.")], lambda a: "Jarvis stop")
        self.assertNotIn("whisper", out)
        self.assertEqual(out["parakeet"]["recall"], 1.0)

    def test_enrollment_and_libri_loaders(self):
        from jarvis.listen import save_wav
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                save_wav(Path(d) / "voice" / "enroll" / f"0{i + 1}.wav", np.zeros(1600, dtype="float32"))
            clips = ve.enrollment(d)
            self.assertEqual([c[0] for c in clips], ["01", "02", "03"])
            self.assertTrue(clips[0][2].startswith("Jarvis"))
            self.assertEqual(ve.libri(d), [])  # no dataset: no negatives, no crash
            self.assertEqual(ve.own_voice(d), [])


if __name__ == "__main__":
    unittest.main()
