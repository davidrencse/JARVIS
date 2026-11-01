"""The decider (jarvis/decide.py) and the shadow hook in assistant.py. The embedding model is
never loaded: jarvis.embed.embed is replaced by a deterministic hashing bag-of-words."""

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from jarvis import assistant, decide

DIM = 384


def fake_embed(texts):
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9']+", str(text).lower()):
            idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM
            out[i, idx] += 1.0
        norm = np.linalg.norm(out[i])
        out[i, 0] = 1.0 if norm == 0 else out[i, 0]
        out[i] /= np.linalg.norm(out[i])
    return out


SMALL = {"mode": {
    "converse": ["hello there friend", "tell me a joke"],
    "act": ["open spotify now", "pause the music"],
    "agent": ["buy a charger", "check my email"],
}}


class DecideBase(unittest.TestCase):
    def setUp(self):
        self._protos = copy.deepcopy(decide.PROTOTYPES)
        self._temps = dict(decide._TEMPS)
        decide.PROTOTYPES.clear()
        decide.PROTOTYPES.update(copy.deepcopy(SMALL))
        decide._TEMPS.clear()
        decide._cache.clear()
        decide._models.clear()  # never keep a classifier trained on SMALL
        self.addCleanup(decide._models.clear)
        p = mock.patch("jarvis.embed.embed", side_effect=fake_embed)
        self.embed = p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        decide.PROTOTYPES.clear()
        decide.PROTOTYPES.update(self._protos)
        decide._TEMPS.clear()
        decide._TEMPS.update(self._temps)
        decide._cache.clear()

    def rows(self):
        db = sqlite3.connect(self.state / "history.sqlite3")
        try:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM decisions")]
        finally:
            db.close()


class ChooseTests(DecideBase):
    def test_shape_and_winner(self):
        got = decide.choose("mode", "please open spotify")
        self.assertEqual(got["winner"], "act")
        self.assertEqual(set(got), {"winner", "probs", "confidence", "similarity", "sims"})
        self.assertEqual(set(got["probs"]), {"converse", "act", "agent"})
        self.assertAlmostEqual(sum(got["probs"].values()), 1.0, places=2)
        self.assertEqual(got["confidence"], got["probs"]["act"])
        self.assertEqual(got["sims"]["act"], max(got["sims"].values()))
        self.assertAlmostEqual(got["similarity"], got["sims"]["act"], places=3)

    def test_winner_follows_closest_prototype(self):
        self.assertEqual(decide.choose("mode", "check my email")["winner"], "agent")
        self.assertEqual(decide.choose("mode", "tell me a joke")["winner"], "converse")

    def test_wake_word_is_ignored(self):
        self.assertEqual(decide.choose("mode", "Jarvis, open spotify now")["probs"],
                         decide.choose("mode", "open spotify now")["probs"])
        self.assertIsNotNone(decide.choose("mode", "jarvis"))  # a bare wake word still classifies

    def test_options_restrict(self):
        got = decide.choose("mode", "open spotify now", options=["converse", "agent", "nope"])
        self.assertEqual(set(got["probs"]), {"converse", "agent"})
        self.assertIn(got["winner"], {"converse", "agent"})
        self.assertAlmostEqual(sum(got["probs"].values()), 1.0, places=2)

    def test_lower_temperature_is_more_confident(self):
        text = "open spotify and check"
        hot = decide.choose("mode", text, temperature=0.5)
        cold = decide.choose("mode", text, temperature=0.02)
        self.assertEqual(hot["winner"], cold["winner"])
        self.assertGreater(cold["confidence"], hot["confidence"])

    def test_none_when_embed_unavailable(self):
        self.embed.side_effect = lambda texts: None
        decide._cache.clear()
        self.assertIsNone(decide.choose("mode", "open spotify"))

    def test_none_when_only_query_embed_fails(self):
        decide.choose("mode", "warm the cache")
        self.embed.side_effect = lambda texts: None
        self.assertIsNone(decide.choose("mode", "open spotify"))


class LearnTests(DecideBase):
    def test_learn_changes_choice_and_invalidates_cache(self):
        text = "zebra xylophone"
        before = decide.choose("mode", text)
        self.assertIn("mode", decide._cache)
        decide.learn("mode", "agent", text)
        self.assertNotIn("mode", decide._cache)
        after = decide.choose("mode", text)
        self.assertEqual(after["winner"], "agent")
        self.assertAlmostEqual(after["sims"]["agent"], 1.0, places=3)
        self.assertLess(before["sims"]["agent"], after["sims"]["agent"])


class RobustnessTests(DecideBase):
    def test_a_bad_label_line_is_skipped_not_fatal(self):
        labels = self.state / "labels.jsonl"
        labels.write_text('{"text": "zebra", "label": "act"}\nnot json{\n[1]\n', encoding="utf-8")
        decide.PROTOTYPES["mode"].setdefault("act", [])
        with mock.patch.object(decide, "LABELS", labels), \
                mock.patch("jarvis.scorecard.load", side_effect=OSError), \
                mock.patch("builtins.print"):
            self.assertEqual(decide.examples("mode"), [("zebra", "act")])

    def test_a_model_fitted_before_learn_is_not_stored(self):
        real = decide.fit

        def fit_then_learn(*args, **kwargs):
            decide.learn("mode", "agent", "zebra xylophone")  # lands mid-fit
            return real(*args, **kwargs)

        with mock.patch.object(decide, "fit", side_effect=fit_then_learn):
            decide._model("mode")
        self.assertNotIn("mode", decide._models)


class LogTests(DecideBase):
    def test_record_and_label(self):
        d = decide.choose("mode", "open spotify now")
        did = decide.record(self.state, "mode", "open spotify now", d, label="act", source="rules")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["id"], r["job"], r["winner"], r["label"], r["source"]),
                         (did, "mode", "act", "act", "rules"))
        blob = json.loads(r["probs"])
        self.assertEqual(blob["probs"], d["probs"])
        self.assertEqual(blob["sims"], d["sims"])
        self.assertEqual(r["confidence"], d["confidence"])

        other = decide.record(self.state, "mode", "x", d)
        self.assertIsNone([r for r in self.rows() if r["id"] == other][0]["label"])
        decide.label(self.state, other, "agent")
        row = [r for r in self.rows() if r["id"] == other][0]
        self.assertEqual((row["label"], row["source"]), ("agent", "outcome"))
        decide.label(self.state, other, "build", source="user")
        row = [r for r in self.rows() if r["id"] == other][0]
        self.assertEqual((row["label"], row["source"]), ("build", "user"))

    def test_record_with_no_decision(self):
        decide.record(self.state, "grok_preflight", "t", None, label="allow")
        r = self.rows()[0]
        self.assertIsNone(r["winner"])
        self.assertEqual(json.loads(r["probs"]), {"probs": None, "sims": None})

    def test_db_file_removable_after_use(self):
        did = decide.record(self.state, "mode", "hi", {"winner": "act", "probs": {}, "sims": {}})
        decide.label(self.state, did, "act")
        decide._labelled(self.state, "mode")
        decide.calibrate(self.state, "mode")
        os.remove(self.state / "history.sqlite3")
        self.assertFalse((self.state / "history.sqlite3").exists())


def synth(winner_is_label=True, n=60, label_override=None):
    """n decisions whose sims favour a rotating option."""
    names = ["converse", "act", "agent"]
    out = []
    for i in range(n):
        top = names[i % 3]
        sims = {o: 0.2 + 0.01 * (j % 2) for j, o in enumerate(names)}
        sims[top] = 0.9
        if label_override:
            label = label_override(top, names, i)
        else:
            label = top if winner_is_label else names[(i + 1) % 3]
        out.append(({"winner": top, "probs": {}, "sims": sims}, label))
    return out


class CalibrateTests(DecideBase):
    JOB = "tool"  # no classifier: calibrated by temperature over the logged similarities

    def fill(self, rows, job=None):
        job = job or self.JOB
        for d, label in rows:
            decide.record(self.state, job, "t", d, label=label)

    def test_too_few_labels(self):
        self.fill(synth(n=decide.MIN_LABELS - 1))
        rep = decide.calibrate(self.state, self.JOB)
        self.assertFalse(rep["ready"])
        self.assertEqual(rep["labels"], decide.MIN_LABELS - 1)
        self.assertIn("needs", rep["note"])
        self.assertNotIn(self.JOB, decide._TEMPS)

    def test_unlabelled_rows_do_not_count(self):
        for d, _ in synth(n=80):
            decide.record(self.state, "mode", "t", d)
        self.assertEqual(decide.calibrate(self.state, self.JOB)["labels"], 0)

    def test_perfect_labels_ready(self):
        self.fill(synth(n=60))
        rep = decide.calibrate(self.state, self.JOB)
        self.assertTrue(rep["ready"])
        self.assertIsNotNone(rep["threshold"])
        self.assertEqual(rep["accuracy"], 1.0)
        self.assertEqual(rep["labels"], 60)
        grid = [0.01 * k for k in range(1, 31)]
        self.assertTrue(any(abs(rep["temperature"] - g) < 1e-9 for g in grid))
        self.assertEqual(decide.temperature_for(self.JOB), rep["temperature"])
        self.assertGreater(rep["coverage"], 0)

    def test_disagreeing_labels_not_ready(self):
        self.fill(synth(winner_is_label=False, n=60))
        rep = decide.calibrate(self.state, self.JOB)
        self.assertFalse(rep["ready"])
        self.assertIsNone(rep["threshold"])
        self.assertEqual(rep["accuracy"], 0.0)

    def test_labels_outside_sims_are_ignored(self):
        self.fill(synth(n=60))
        self.fill([({"winner": "act", "probs": {}, "sims": {"act": 0.9, "agent": 0.1}}, "ghost")] * 5)
        rep = decide.calibrate(self.state, self.JOB)
        self.assertEqual(rep["labels"], 60)
        self.assertTrue(rep["ready"])

    def test_only_requested_job(self):
        self.fill(synth(n=60), job="other")
        self.assertFalse(decide.calibrate(self.state, self.JOB)["ready"])
        self.assertTrue(decide.calibrate(self.state, "other")["ready"])

    def test_classifier_job_scores_its_logged_probabilities(self):
        # mode has a classifier (decide.CUES): its own logged confidence is what's scored.
        for i in range(60):
            right = i % 3 != 0  # 40 right at 0.95, 20 wrong at 0.6
            d = {"winner": "act", "probs": {"act": 0.95 if right else 0.6, "converse": 0.05},
                 "sims": {"act": 0.5, "converse": 0.5}}
            decide.record(self.state, "mode", "t", d, label="act" if right else "converse")
        rep = decide.calibrate(self.state, "mode")
        self.assertTrue(rep["ready"])
        self.assertEqual(rep["threshold"], 0.95)  # the 0.6 picks are the wrong ones
        self.assertEqual(rep["accuracy"], 0.667)

    def test_all_labels_unusable_does_not_crash(self):
        self.fill([({"winner": "act", "probs": {}, "sims": {"act": 0.9}}, "ghost")] * 55)
        rep = decide.calibrate(self.state, self.JOB)
        self.assertFalse(rep["ready"])


class ShadowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory = mock.Mock()
        self.memory.path = Path(tmp.name) / "memory.db"
        self.state = Path(tmp.name)
        self.on = {"decisions": {"shadow": True}}

    @staticmethod
    def wait():
        for t in threading.enumerate():
            if t.name == "jarvis-decider-shadow":
                t.join(5)

    def run_shadow(self, config, memory, *args, **kw):
        with mock.patch.object(threading, "Thread", wraps=threading.Thread) as th:
            assistant._shadow(config, memory, *args, **kw)
            started = th.call_count
        self.wait()
        return started

    def test_off_without_config_or_memory(self):
        with mock.patch("jarvis.decide.record") as rec, mock.patch("jarvis.decide.choose") as ch:
            self.assertEqual(self.run_shadow({}, self.memory, "hi", "act"), 0)
            self.assertEqual(self.run_shadow({"decisions": {}}, self.memory, "hi", "act"), 0)
            self.assertEqual(self.run_shadow({"decisions": {"shadow": False}}, self.memory, "hi", "act"), 0)
            self.assertEqual(self.run_shadow(self.on, None, "hi", "act"), 0)
        rec.assert_not_called()
        ch.assert_not_called()

    def test_records_guess_and_label(self):
        guess = {"winner": "agent", "probs": {"agent": 1.0}, "sims": {"agent": 0.5}, "confidence": 1.0}
        with mock.patch("jarvis.decide.choose", return_value=guess) as ch, \
                mock.patch("jarvis.decide.record") as rec:
            self.assertEqual(self.run_shadow(self.on, self.memory, "buy it", "act"), 1)
        ch.assert_called_once_with("mode", "buy it")
        rec.assert_called_once_with(self.state, "mode", "buy it", guess, label="act", source="rules")

    def test_undecided_records_none_guess(self):
        with mock.patch("jarvis.decide.choose") as ch, mock.patch("jarvis.decide.record") as rec:
            self.run_shadow(self.on, self.memory, "pw", "allow", job="grok_preflight", decided=False)
        ch.assert_not_called()
        rec.assert_called_once_with(self.state, "grok_preflight", "pw", None, label="allow", source="rules")

    def test_exception_never_propagates(self):
        with mock.patch("jarvis.decide.choose", side_effect=RuntimeError("boom")), \
                mock.patch("jarvis.decide.record") as rec:
            self.run_shadow(self.on, self.memory, "x", "act")
        rec.assert_not_called()
        with mock.patch("jarvis.decide.choose", return_value=None), \
                mock.patch("jarvis.decide.record", side_effect=OSError("disk")):
            self.run_shadow(self.on, self.memory, "x", "act")

    def test_real_rows_end_to_end(self):
        with mock.patch("jarvis.embed.embed", side_effect=fake_embed):
            decide._cache.clear()
            self.run_shadow(self.on, self.memory, "open spotify", "act")
            decide._cache.clear()
        db = sqlite3.connect(self.state / "history.sqlite3")
        try:
            rows = db.execute("SELECT job, label, source FROM decisions").fetchall()
        finally:
            db.close()
        self.assertEqual(rows, [("mode", "act", "rules")])


class FakeMemory:
    def __init__(self, path):
        self.path, self.added = path, []

    def add(self, kind, text, **kw):
        self.added.append((kind, text, kw))
        return {"id": f"{kind}-{len(self.added)}"}

    def context(self, text):
        return []


class AskWiringTests(unittest.TestCase):
    def test_fast_path_shadows_act(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = FakeMemory(Path(tmp.name) / "memory.db")
        config = {"devices": {"dell": {"hostname": "WinLaptop", "aliases": ["windows"]}},
                  "cluster": {"enabled": False}}
        done = {"reply": "Opened Chrome.", "commands": [{"id": "c1"}],
                "outcomes": [{"id": "c1", "state": "acknowledged", "ok": True}], "checks": []}
        with mock.patch("jarvis.pipeline.fast_path", return_value=done), \
                mock.patch.object(assistant, "_shadow") as sh:
            got = assistant.ask("open chrome", None, memory, config, memory.path.parent)
        self.assertEqual(got["lane"], "parser")
        sh.assert_called_once_with(config, memory, "open chrome", "act")


if __name__ == "__main__":
    unittest.main()
