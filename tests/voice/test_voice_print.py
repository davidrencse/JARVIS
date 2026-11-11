"""Speaker check (listen.VoicePrint): scoring never learns, learning needs proof and an
enrollment anchor, learned clips only nudge, calibration respects negatives. Fake embeddings."""
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.listen import VoicePrint

DIM = 64


def unit(v):
    return (v / np.linalg.norm(v)).astype("float32")


class VoicePrintTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.me = self.rng.normal(size=DIM)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vp = VoicePrint(self.tmp.name)
        patcher = patch.object(self.vp, "_embed", side_effect=lambda c: c)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clips = [self.voice(self.me, 0.6) for _ in range(6)]
        self.vp.enroll(self.clips)

    def voice(self, base, noise):
        return unit(base + self.rng.normal(scale=noise, size=DIM))

    def profile(self):
        return json.loads(self.vp.path.read_text())["profile"]

    def test_verify_never_changes_the_profile(self):
        before = self.vp.path.read_text()
        for _ in range(5):
            ok, score = self.vp.verify(self.voice(self.me, 0.2))
            self.assertTrue(ok)
            self.assertGreater(score, 0.48)  # the old self-teaching cutoff
        self.assertEqual(self.vp.path.read_text(), before)

    def test_learn_needs_a_confident_score(self):
        self.vp.verify(self.voice(self.me, 0.2))
        self.assertTrue(self.vp.learn())
        self.assertEqual(len(self.profile()), 7)
        self.assertFalse(self.vp.learn())  # used up: one learn per verified clip
        # just above the cutoff: accepted, but not by LEARN_MARGIN
        threshold = json.loads(self.vp.path.read_text())["threshold"]
        self.vp.last_embedding = self.voice(self.me, 0.2)
        self.vp.last_score, self.vp.last_limit = threshold + 0.01, threshold
        self.assertFalse(self.vp.learn())

    def test_add_refuses_a_clip_that_does_not_match_the_enrollment(self):
        stranger = unit(self.rng.normal(size=DIM))
        data = json.loads(self.vp.path.read_text())
        self.assertLess(VoicePrint._match(stranger, data["profile"][:data["clips"]]), VoicePrint.ANCHOR)
        self.assertFalse(self.vp._add(data, stranger))
        self.assertEqual(len(self.profile()), 6)

    def test_learned_copies_of_a_stranger_cannot_carry_him(self):
        stranger = unit(self.rng.normal(size=DIM))
        data = json.loads(self.vp.path.read_text())
        # A profile poisoned the old way: three learned near-copies of one stranger.
        data["profile"] += [self.voice(stranger, 0.05).tolist() for _ in range(3)]
        self.vp._save(data)
        before = VoicePrint._match(stranger, data["profile"])
        self.assertGreater(before, 0.9)  # the old score accepted him outright
        ok, score = self.vp.verify(stranger)
        self.assertFalse(ok)
        self.assertLess(score, data["threshold"])

    def test_learned_clips_only_nudge(self):
        data = json.loads(self.vp.path.read_text())
        pinned = data["clips"]
        # A learned clip that does match the enrollment, and an embedding near it only.
        edge = unit(self.me + self.rng.normal(scale=1.4, size=DIM))
        profile = data["profile"] + [edge.tolist()] * 3
        base = VoicePrint._match(edge, profile[:pinned])
        if base < VoicePrint.ANCHOR:
            self.skipTest("random edge clip fell below the anchor")
        self.assertLessEqual(VoicePrint.score(edge, profile, pinned), base + VoicePrint.NUDGE + 1e-6)
        self.assertGreaterEqual(VoicePrint.score(edge, profile, pinned), base)

    def test_near_duplicates_replace_each_other(self):
        twin = self.voice(self.me, 0.2)
        for _ in range(4):
            self.vp.verify(unit(twin + self.rng.normal(scale=0.01, size=DIM)))
            self.vp.learn()
        self.assertEqual(len(self.profile()), 7)  # one voice, one session: one slot

    def test_adapt_off_and_forget_learned(self):
        self.vp.verify(self.voice(self.me, 0.2))
        self.vp.learn()
        self.assertEqual(self.vp.forget_learned(), 1)
        self.assertTrue(np.allclose(self.profile(), self.clips))  # enrollment kept as it was
        off = VoicePrint(self.tmp.name, {"voice": {"adapt": False}})
        with patch.object(off, "_embed", side_effect=lambda c: c):
            off.verify(self.voice(self.me, 0.2))
            self.assertFalse(off.learn())
        self.assertEqual(len(self.profile()), 6)

    def test_old_learn_true_form_learns_only_when_confident(self):
        self.vp.verify(unit(self.rng.normal(size=DIM)), learn=True)  # stranger
        self.assertEqual(len(self.profile()), 6)
        self.vp.verify(self.voice(self.me, 0.2), learn=True)
        self.assertEqual(len(self.profile()), 7)


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)
        self.me = self.rng.normal(size=DIM)

    def clips(self, noise, n=7):
        return [unit(self.me + self.rng.normal(scale=noise, size=DIM)) for _ in range(n)]

    def test_far_negatives_leave_the_clamped_threshold(self):
        pos = self.clips(0.5)
        neg = [unit(self.rng.normal(size=DIM)) for _ in range(20)]
        report = {}
        _, plain, _ = VoicePrint.calibrate(pos)
        _, threshold, own = VoicePrint.calibrate(pos, neg, report)
        self.assertEqual(threshold, plain)
        self.assertEqual(threshold, VoicePrint.CEILING)
        self.assertLess(report["negative_max"], threshold)
        self.assertEqual(report["false_accept"], 0.0)
        self.assertEqual(report["false_reject"], 0.0)
        self.assertFalse(report["negatives_too_close"])

    def test_close_negatives_raise_it_within_the_ceiling(self):
        V = VoicePrint
        for seed in range(20):
            rng = np.random.default_rng(seed)
            pos = [unit(self.me + rng.normal(scale=1.3, size=DIM)) for _ in range(7)]
            neg = [unit(self.me * 0.3 + rng.normal(scale=1.0, size=DIM)) for _ in range(5)]
            report = {}
            _, threshold, own = V.calibrate(pos, neg, report)
            base = min(V.CEILING, max(V.FLOOR, min(own) - V.MARGIN))
            cap = max(V.FLOOR, min(V.CEILING, min(own) - V.MARGIN))
            need = report["negative_max"] + V.NEG_GAP
            self.assertAlmostEqual(threshold, max(base, min(need, cap)), places=2)
            self.assertLessEqual(threshold, V.CEILING)
            self.assertGreaterEqual(threshold, V.FLOOR)

    def test_negatives_above_the_ceiling_never_lift_it_past(self):
        pos = self.clips(0.3)
        neg = [unit(self.me * 0.6 + self.rng.normal(scale=1.0, size=DIM)) for _ in range(10)]
        report = {}
        _, threshold, own = VoicePrint.calibrate(pos, neg, report)
        self.assertGreater(report["negative_max"] + VoicePrint.NEG_GAP, VoicePrint.CEILING)
        self.assertEqual(threshold, VoicePrint.CEILING)  # David live scores 0.38-0.45
        self.assertTrue(report["negatives_too_close"])

    def test_negatives_above_him_are_reported_not_obeyed(self):
        pos = self.clips(1.2)
        neg = [p.copy() for p in pos[:2]]  # "negatives" identical to his own clips
        report = {}
        _, threshold, own = VoicePrint.calibrate(pos, neg, report)
        self.assertTrue(report["negatives_too_close"])
        self.assertLessEqual(threshold, max(VoicePrint.FLOOR, min(own) - VoicePrint.MARGIN) + 1e-6)

    def test_enroll_records_calibration(self):
        with tempfile.TemporaryDirectory() as d:
            vp = VoicePrint(d)
            with patch.object(vp, "_embed", side_effect=lambda c: c):
                out = vp.enroll(self.clips(0.5), negatives=[unit(self.rng.normal(size=DIM))])
            data = json.loads(vp.path.read_text())
            self.assertEqual(data["calibration"]["negatives"], 1)
            self.assertIn("date", data["calibration"])
            self.assertEqual(out["calibration"]["threshold"], round(data["threshold"], 3))


class ScoreTextTests(unittest.TestCase):
    def test_a_miss_never_reads_as_a_pass(self):
        from jarvis.listen import score_text
        self.assertEqual(score_text(0.3596, 0.36), "0.359 < 0.36")
        self.assertEqual(score_text(0.4, 0.36), "0.400 >= 0.36")
        self.assertEqual(score_text(None, 0.36), "unchecked")


if __name__ == "__main__":
    unittest.main()
