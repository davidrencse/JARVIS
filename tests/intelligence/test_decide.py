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


class AuditThreeTests(DecideBase):
    def test_old_rows_pruned_at_most_hourly_with_an_index(self):
        decide._pruned.clear()
        self.addCleanup(decide._pruned.clear)
        decide.record(self.state, "mode", "first", None)  # creates the table and prunes
        db = sqlite3.connect(self.state / "history.sqlite3")
        try:
            db.execute("INSERT INTO decisions(id, at, job, text) VALUES('old', 1, 'mode', 'x')")
            db.commit()
            indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            db.close()
        self.assertIn("decisions_at", indexes)
        decide.record(self.state, "mode", "second", None)  # within the hour: no prune
        self.assertIn("old", {r["id"] for r in self.rows()})
        decide._pruned[str(self.state.resolve())] -= decide.PRUNE_EVERY_S + 1
        decide.record(self.state, "mode", "third", None)
        self.assertNotIn("old", {r["id"] for r in self.rows()})

    def test_vectors_built_outside_the_lock(self):
        held = []

        def embed(texts):
            held.append(decide._lock.locked())
            return fake_embed(texts)
        self.embed.side_effect = embed
        self.assertIsNotNone(decide._vectors("mode"))
        self.assertTrue(held)
        self.assertFalse(any(held))
        self.assertIn("mode", decide._cache)
        self.assertEqual(decide._building, {})

    def test_warm_builds_a_cueless_jobs_vectors_without_logging_a_failure(self):
        decide._cache.pop("recall", None)
        decide._warm_failed.pop("recall", None)
        self.addCleanup(decide._cache.pop, "recall", None)

        def choose(job, text):
            decide._cache[job] = {"notes": None, "none": None}
        with mock.patch.object(decide, "choose", side_effect=choose), \
                mock.patch.object(decide.threading, "Thread") as thread:
            decide.warm("recall")
            thread.call_args.kwargs["target"]()
            decide.warm("recall")  # built: no second thread
        self.assertEqual(thread.call_count, 1)
        self.assertNotIn("recall", decide._warm_failed)

    def test_wants_notes_never_waits_on_a_build_in_progress(self):
        decide._cache.pop("recall", None)
        decide._building["recall"] = 1
        self.addCleanup(decide._building.pop, "recall", None)
        with mock.patch.object(decide, "choose", side_effect=AssertionError("waited")):
            self.assertTrue(decide.wants_notes("hello there"))

    def test_wants_notes_on_a_cache_miss_answers_true_and_warms_in_the_background(self):
        decide._cache.pop("recall", None)
        with mock.patch.object(decide, "choose", side_effect=AssertionError("embedded on the request")), \
                mock.patch.object(decide, "warm") as warm:
            self.assertTrue(decide.wants_notes("what's the capital of France"))
        warm.assert_called_once_with("recall")


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
        for n, (d, label) in enumerate(rows):  # distinct texts: each counts once
            decide.record(self.state, job, f"request number {n}", d, label=label)

    def test_trained_texts_and_repeats_are_not_scored(self):
        # Audit 2026-11-01: calibrating on training texts (or repeats) overstates precision.
        trained = next(iter(decide.PROTOTYPES[self.JOB].values()))[0] if self.JOB in decide.PROTOTYPES else None
        rows = synth(n=decide.MIN_LABELS)
        for n, (d, label) in enumerate(rows):
            decide.record(self.state, self.JOB, "same words", d, label=label)
        self.assertEqual(decide.calibrate(self.state, self.JOB)["labels"], 1)
        if trained:
            decide.record(self.state, self.JOB, trained, rows[0][0], label=rows[0][1])
            self.assertEqual(len(decide._labelled(self.state, self.JOB)), 1)

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
            decide.record(self.state, "mode", f"synthetic request {i}", d,
                          label="act" if right else "converse")
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
        # The row's label is the one the caller passed. One fit step still writes it;
        # the full 1500-iteration build is the scorecard's job, not this path.
        fast_fit = {**decide.FIT, "iterations": 1}
        with mock.patch("jarvis.embed.embed", side_effect=fake_embed), \
                mock.patch.object(decide, "FIT", fast_fit):
            decide._cache.clear()
            decide._models.pop("mode", None)
            try:
                self.run_shadow(self.on, self.memory, "open spotify", "act")
            finally:
                decide._cache.clear()
                decide._models.pop("mode", None)
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


class StatementRecallTests(unittest.TestCase):
    """A plain first-person statement only pulls notes when notes clearly win (2026-11-01)."""

    def gate(self, text, none, notes):
        from unittest.mock import patch
        decide._cache["recall"] = {"notes": None, "none": None}  # built: the gate may ask
        self.addCleanup(decide._cache.pop, "recall", None)
        with patch.object(decide, "choose", return_value={"sims": {"none": none, "notes": notes}}):
            return decide.wants_notes(text)

    def test_statements_need_a_clear_win(self):
        self.assertFalse(self.gate("I'm going to sell my car.", 0.40, 0.45))   # close: no notes
        self.assertTrue(self.gate("I'm going to sell my car.", 0.30, 0.45))    # clear: notes
        self.assertFalse(self.gate("We got nothing.", 0.42, 0.40))

    def test_questions_and_notes_words_keep_the_usual_margin(self):
        for text in ("what did I write about my car", "my thesis is due friday",
                     "I need my notes on paging", "I'm behind, what's due?"):
            self.assertTrue(self.gate(text, 0.45, 0.42), text)  # within RECALL_MARGIN: notes
