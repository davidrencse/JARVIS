"""Phase 3 memory: daily consolidation, merges and replacements, correction lessons, the
per-request memory pack. The ChatGPT call is always a fake ask()."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from jarvis import consolidate as cons
from jarvis.interpret import interpret
from jarvis.memory import Memory, is_correction


def _backdate(memory, item_id, days):
    at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with memory.connect() as db:
        db.execute("UPDATE items SET created_at=? WHERE id=?", (at, item_id))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def said(self, who, text, days=1):
        item = self.memory.add("message" if who == "David" else "reply", text)
        _backdate(self.memory, item["id"], days)
        return item["id"]


class CutoffTest(unittest.TestCase):
    def test_midnight_is_right_on_a_dst_change_day(self):
        import os, time
        if not hasattr(time, "tzset"):
            self.skipTest("no tzset")
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            # 2026-11-01: clocks fall back at 02:00; local midnight is still EDT (UTC-4).
            now = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)  # 13:00 EST
            self.assertEqual(cons._cutoff(now), datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


class ConsolidateTest(Base):
    def test_items_are_stored_linked_and_checked(self):
        m1 = self.said("David", "My dentist is Dr Lee, appointment on Friday")
        r1 = self.said("JARVIS", "Noted.")
        seen = {}

        def ask(prompt):
            seen["prompt"] = prompt
            return json.dumps({"items": [
                {"kind": "fact", "text": "David's dentist is Dr Lee.", "sources": [m1]},
                {"kind": "loop", "text": "Dentist appointment on Friday.", "sources": [m1, r1]},
                {"kind": "fact", "text": "Invented.", "sources": ["message:nope"]},  # unknown id
                {"kind": "secret", "text": "Bad kind.", "sources": [m1]},
            ], "closed": ["loop:never-shown"]})
        summary = cons.consolidate(self.memory, ask)
        self.assertIn(m1, seen["prompt"])
        self.assertEqual((summary["added"], summary["closed"]), (2, 0))
        facts = self.memory.knowledge()
        self.assertEqual(sorted(f["kind"] for f in facts), ["fact", "loop"])
        fact = next(f for f in facts if f["kind"] == "fact")
        links = self.memory.get(fact["id"])["links"]
        self.assertIn({"src": fact["id"], "dst": m1, "rel": "from"}, links)

    def test_repeat_merges_and_contradiction_replaces(self):
        m1 = self.said("David", "I moved to Toronto", days=3)
        old, _ = self.memory.absorb("fact", "David lives in Ottawa.", [m1])
        rep, _ = self.memory.absorb("fact", "David uses the Dell as his laptop.", [m1])
        m2 = self.said("David", "Remember I live in Toronto now; the Dell is my laptop")

        def ask(prompt):
            self.assertIn(f"[{old}] (fact) David lives in Ottawa.", prompt)
            return json.dumps({"items": [
                {"kind": "fact", "text": "David lives in Toronto.", "sources": [m2],
                 "replaces": [old]},
                {"kind": "fact", "text": "The Dell is David's laptop.", "sources": [m2],
                 "same_as": rep}]})
        summary = cons.consolidate(self.memory, ask)
        self.assertEqual((summary["added"], summary["merged"], summary["replaced"]), (1, 1, 1))
        retired = self.memory.get(old)
        self.assertEqual((retired["kind"], retired["meta"]["was"]), ("replaced", "fact"))
        texts = [k["text"] for k in self.memory.knowledge()]
        self.assertNotIn("David lives in Ottawa.", texts)
        self.assertEqual(self.memory.get(rep)["meta"]["seen"], 2)
        self.assertFalse(any(h["id"] == old for h in self.memory.search("Ottawa")))

    def test_near_identical_wording_merges_without_same_as(self):
        m1 = self.said("David", "x")
        first, state1 = self.memory.absorb("preference", "David wants short spoken answers.", [m1])
        again, state2 = self.memory.absorb("preference", "David wants short, spoken answers!", [m1])
        self.assertEqual((first, state1, state2), (again, "added", "merged"))

    def test_closed_loops_retire(self):
        m1 = self.said("David", "remind me to call mum", days=3)
        loop, _ = self.memory.absorb("loop", "David needs to call his mum.", [m1])
        self.said("David", "I called mum, all good")
        summary = cons.consolidate(self.memory, lambda p: json.dumps({"items": [], "closed": [loop]}))
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(self.memory.get(loop)["kind"], "closed")

    def test_raw_corrections_are_rewritten_or_dropped(self):
        m1 = self.said("David", "open spotify on the dell", days=2)
        self.said("JARVIS", "Opened Spotify on your Dell.", days=2)
        with mock.patch("jarvis.memory._now", return_value=datetime.now(timezone.utc) - timedelta(days=2)):
            slip = self.memory.note_correction("no, I meant the zenbook")
            other = self.memory.note_correction("no, I meant the zenbook again")
        kept, _ = self.memory.absorb("lesson", "When David says the laptop he means the Dell.", [m1])
        seen = {}

        def ask(prompt):
            seen["prompt"] = prompt
            return json.dumps({"items": [], "closed": [],
                               "dropped": [slip, kept, "lesson:never-shown"]})
        summary = cons.consolidate(self.memory, ask)
        self.assertIn("David corrected JARVIS", seen["prompt"])
        self.assertEqual(summary["dropped"], 1)  # only a raw correction note can be dropped
        self.assertEqual(self.memory.get(slip)["kind"], "forgotten")
        self.assertEqual(self.memory.get(kept)["kind"], "lesson")
        self.assertEqual(self.memory.get(other)["kind"], "lesson")  # not accounted for: kept

    def test_window_runs_once_and_failure_records_nothing(self):
        self.said("David", "hello there")
        with self.assertRaises(RuntimeError):
            cons.consolidate(self.memory, mock.Mock(side_effect=RuntimeError("limit")))
        self.assertIsNone(cons.last_run(self.memory))
        ask = mock.Mock(return_value="not json")
        cons.consolidate(self.memory, ask)
        cons.consolidate(self.memory, ask)  # nothing new: no second call
        self.assertEqual(ask.call_count, 1)
        self.assertFalse(cons.due(self.memory))

    def test_today_waits_and_due_respects_hour(self):
        self.said("David", "today's chat", days=0)
        ask = mock.Mock()
        cons.consolidate(self.memory, ask)
        ask.assert_not_called()  # before today's local midnight only
        early = datetime.now().astimezone().replace(hour=cons.CONSOLIDATE_HOUR - 1)
        fresh = Memory(tempfile.mkdtemp(dir=self.tmp.name))
        self.assertFalse(cons.due(fresh, now=early))
        self.assertTrue(cons.due(fresh, now=early + timedelta(hours=2)))

    def test_off_by_default(self):
        self.assertFalse(cons.enabled({}))
        self.assertTrue(cons.enabled({"memory": {"consolidate": True}}))


class MalformedReplyTest(unittest.TestCase):
    def test_wrong_types_change_nothing_instead_of_raising(self):
        for reply in ('{"items":[{"kind":"fact","text":"x","sources":["m1"],"same_as":["a"]}]}',
                      '{"items":[{"kind":"fact","text":"x","sources":"m1"}]}',
                      '{"items":[{"kind":"fact","text":"x","sources":["m1"],"replaces":5}]}',
                      '{"items":5,"closed":7,"dropped":"d"}',
                      '{"items":[],"closed":{"a":1},"dropped":3}'):
            items, closed, dropped = cons.parse(reply, {"m1"}, [])
            self.assertEqual((closed, dropped), ([], []), reply)
            self.assertTrue(all(i["same_as"] is None and i["replaces"] == [] for i in items), reply)

    def test_list_same_as_keeps_the_item_without_a_merge(self):
        items, _, _ = cons.parse(
            '{"items":[{"kind":"fact","text":"x","sources":["m1"],"same_as":["a"]}]}', {"m1"}, [])
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["same_as"])


class CorrectionTest(Base):
    def test_detects_corrections_only(self):
        for text in ("no, I meant the Dell", "No I meant on the laptop", "nope, not that one",
                     "that's not what I asked", "wrong one", "I meant the other PC"):
            self.assertTrue(is_correction(text), text)
        for text in ("no", "no thanks", "not now", "what's the weather", "I said hello to mum"):
            self.assertFalse(is_correction(text), text)

    def test_lesson_links_the_corrected_exchange(self):
        asked = self.memory.add("message", "open chrome on the laptop")["id"]
        answered = self.memory.add("reply", "Opened Chrome on the ASUS.")["id"]
        lesson = self.memory.note_correction("no, I meant the Dell")
        item = self.memory.get(lesson)
        self.assertEqual(item["kind"], "lesson")
        self.assertIn("open chrome on the laptop", item["body"])
        self.assertEqual({l["dst"] for l in item["links"]}, {asked, answered})
        self.assertIsNone(self.memory.note_correction("what time is it"))

    def test_no_recent_exchange_no_lesson(self):
        self.said("David", "open chrome", days=1)
        self.assertIsNone(self.memory.note_correction("no, I meant the Dell"))


class PackTest(Base):
    def test_pack_has_loops_matching_facts_and_lessons(self):
        m = self.said("David", "x")
        self.memory.absorb("loop", "Send the thesis draft to Prof Kim.", [m])
        self.memory.absorb("fact", "David's laptop is the Dell zenbook.", [m])
        self.memory.absorb("fact", "David's sister lives in Vancouver.", [m])
        self.memory.absorb("lesson", "When David says laptop he means the Dell zenbook.", [m])
        lines = self.memory.pack("open spotify on my laptop")
        text = "\n".join(lines)
        self.assertIn("Open loop: Send the thesis draft", text)
        self.assertIn("laptop is the Dell", text)
        self.assertIn("he means the Dell", text)
        self.assertNotIn("Vancouver", text)

    def test_old_loops_drop_out(self):
        m = self.said("David", "x")
        loop, _ = self.memory.absorb("loop", "Renew the passport.", [m])
        _backdate(self.memory, loop, 30)
        self.assertFalse(any("passport" in line for line in self.memory.pack("hello")))

    def test_interpreter_prompt_carries_lessons(self):
        seen = {}

        def ask(prompt):
            seen["prompt"] = prompt
            return '{"mode": "converse"}'
        interpret("open chrome on the laptop", {"devices": {}}, ".", "asus", ask=ask,
                  lessons=["[lesson:1] When David says laptop he means the Dell."])
        self.assertIn("LESSONS from David's past corrections", seen["prompt"])
        self.assertIn("he means the Dell", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
