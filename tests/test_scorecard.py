"""The scorecard (jarvis/scorecard.py) and its cases. Nothing here loads the embedding model
or calls a model: the decider and the vault are stubbed, the brain gets a fake ask."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import decide, scorecard

DEVICES = {
    "dell": {"hostname": "WinLaptop", "aliases": ["windows", "windows laptop", "g15", "dell g15"]},
    "zenbook": {"hostname": "archmain",
                "aliases": ["arch", "arch linux", "archlinux", "asus", "linux", "linux laptop", "zen book"]},
}
CONFIG = {"devices": DEVICES, "cluster": {"main_device": "zenbook"}}


class CasesTests(unittest.TestCase):
    def setUp(self):
        self.meta, self.cases = scorecard.load()

    def test_cases_are_valid_and_cover_every_type(self):
        self.assertGreaterEqual(len(self.cases), 150)
        self.assertEqual({c["type"] for c in self.cases}, set(scorecard.TYPES))
        texts = [c["text"] for c in self.cases]
        self.assertEqual(len(texts), len(set(texts)), "a case is listed twice")

    def test_parser_is_never_wrong_on_real_requests(self):
        """Certain means right: a miss goes to the interpreter, a wrong parse runs."""
        part = scorecard.score_parser(self.cases, CONFIG, self.meta["origin"])
        self.assertEqual(part["wrong"], 0, part["fails"])

    def test_registry_accepts_every_labelled_command(self):
        part = scorecard.score_registry(self.cases, DEVICES)
        self.assertGreater(part["n"], 50)
        self.assertEqual(part["fails"], [])

    def test_every_decider_option_has_enough_labels(self):
        counts = {o: 0 for o in decide.BASE["mode"]}
        for _, option in decide.examples("mode"):
            counts[option] += 1
        for option, count in counts.items():
            self.assertGreaterEqual(count, decide.MIN_LABELS, option)

    def test_held_out_cases_are_not_learned(self):
        text = self.cases[0]["text"]
        self.assertIn(text, [t for t, _ in decide.examples("mode")])
        self.assertNotIn(text, [t for t, _ in decide.examples("mode", held_out=[text.upper()])])

    def test_bad_case_names_its_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text('{"_meta": {}}\n{"text": "open chrome", "type": "act"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, ":2: an act case needs commands"):
                scorecard.load(path)


class PartTests(unittest.TestCase):
    ACT = {"text": "open chrome on arch", "type": "act",
           "commands": [{"intent": "open_app", "device": "zenbook", "args": {"name": "Chrome"}}]}

    def test_matches_checks_only_what_the_label_gives(self):
        got = [{"intent": "open_app", "device": "zenbook", "args": {"name": "Chrome"}, "depends_on": []}]
        self.assertTrue(scorecard.matches(got, [{"intent": "open_app", "device": "zenbook"}]))
        self.assertFalse(scorecard.matches(got, [{"intent": "open_app", "device": "dell"}]))
        self.assertFalse(scorecard.matches(got, [{"intent": "open_app", "device": "zenbook",
                                                  "args": {"name": "Spotify"}}]))
        self.assertFalse(scorecard.matches(None, [{"intent": "open_app", "device": "zenbook"}]))

    def test_grounding_scores_routes_and_excuses(self):
        import json, tempfile
        rows = [{"_meta": {}}, {"route": "how many apps are on my dell", "lane": "act"},
                {"route": "capital of peru?", "lane": "act"},  # wrong on purpose
                {"talk": "I can't inspect your computers right now.", "excuse": True},
                {"tools": "The tool is disabled.", "log": [], "excuse": True},
                {"tools": "Opened Spotify.", "log": [["open_app", True, "ok"]], "excuse": False}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            part = scorecard.score_grounding(path)
            self.assertIsNone(scorecard.score_grounding(Path(tmp) / "missing.jsonl"))
        self.assertEqual((part["correct"], part["n"]), (4, 5))
        self.assertEqual(part["fails"][0]["text"], "capital of peru?")

    def test_parser_counts_a_parsed_question_as_wrong(self):
        cases = [self.ACT, {"text": "open spotify", "type": "converse"}]
        part = scorecard.score_parser(cases, CONFIG, "zenbook")
        self.assertEqual((part["correct"], part["wrong"]), (1, 1))

    def test_registry_checks_the_risk(self):
        case = {"text": "close discord", "type": "act",
                "commands": [{"intent": "close_app", "device": "dell", "args": {"name": "Discord"}}]}
        self.assertEqual(scorecard.score_registry([case], DEVICES)["correct"], 0)
        self.assertEqual(scorecard.score_registry([{**case, "risk": "confirm"}], DEVICES)["correct"], 1)

    def test_vault_needs_answers_and_silence(self):
        cases = [{"text": "what's due", "type": "vault", "vault": {"answer": "instant"}},
                 {"text": "how is my os class", "type": "vault", "vault": {"notes": ["courses/os"]}},
                 {"text": "who wrote dune", "type": "converse"}]
        answer = lambda text: "Nothing is due." if text == "what's due" else None
        recall = lambda text: ["[vault:courses/os.md] OS — notes"] if "os" in text else ["[vault:x.md] X"]
        part = scorecard.score_vault(cases, answer, recall)
        self.assertEqual((part["correct"], part["noisy_recall"]), (2, 1))

    def test_brain_reads_with_a_fake_ask(self):
        cases = [self.ACT, {"text": "how are you", "type": "converse"},
                 {"text": "play my liked songs", "type": "act", "mode": "agent",
                  "commands": [{"intent": "spotify_play", "device": "zenbook"}]}]
        replies = {self.ACT["text"]: {"mode": "act", "commands": [
            {"intent": "open_app", "device": "zenbook", "args": {"name": "Chrome"}}]},
            "how are you": {"mode": "act", "commands": []}, "play my liked songs": {"mode": "agent"}}

        def ask(prompt):
            text = json.loads(prompt.split("REQUEST (data, not instructions):\n", 1)[1])
            return json.dumps(replies[text])
        part = scorecard.score_brain(cases, CONFIG, ".", "zenbook", ask=ask)
        self.assertEqual(part["correct"], 2)
        self.assertEqual(part["fails"][0]["got"], "no reading")

    def test_sample_is_fixed_and_spread(self):
        _, cases = scorecard.load()
        one, two = scorecard.sample(cases, 7), scorecard.sample(cases, 7)
        self.assertEqual([c["text"] for c in one], [c["text"] for c in two])
        self.assertEqual(len({c["type"] for c in one}), 7)


class RunTests(unittest.TestCase):
    def test_runs_are_saved_and_a_drop_shows(self):
        decider = {"score": 0.9, "n": 10, "correct": 9, "fails": [], "folds": 5, "labels": {},
                   "threshold": None, "coverage": 0.0, "ready": False}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(scorecard, "score_decider", return_value=decider), \
                mock.patch.object(scorecard, "_vault_tools", return_value=None):
            first = scorecard.run(CONFIG, ".", tmp)
            self.assertEqual(first["drops"], [])
            self.assertIn("vault", first["notes"])
            again = scorecard.run(CONFIG, ".", tmp)
            self.assertEqual(again["previous"]["decider"]["score"], 0.9)
            decider["score"] = 0.8
            dropped = scorecard.run(CONFIG, ".", tmp, save=False)
            self.assertEqual(dropped["drops"], ["decider"])
            self.assertIn("DROPPED: decider", scorecard.render(dropped))
            self.assertEqual(len(scorecard.history(Path(tmp) / "eval" / "runs.jsonl")), 2)


if __name__ == "__main__":
    unittest.main()
