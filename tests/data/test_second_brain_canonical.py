"""Scoped archive read and published-head integrity.

Temp vault and temp state only. A plain note over 64 KiB is opened. A gap or a bad hash
is not current. canonical_read searches the latest archived payloads and does not fall
back to SQLite or to an older revision. vault.enabled and vault.recall, literal false,
close that path and ordinary recall. The canonical iterator yields one payload at a time.
"""

import json
import os
import re
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain as sb  # noqa: E402
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.history import History  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import SecondBrain  # noqa: E402
from jarvis.store import Store  # noqa: E402
from jarvis.vault import Vault, recall  # noqa: E402

OLD = "obsolete-canary-phrase-aaaa"
NEW = "current-canary-phrase-bbbb"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state, self.vault = self.tmp / "state", self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state.mkdir()
        self.brain = SecondBrain(self.vault)

    def config(self, canonical_read=False):
        raw = {"enabled": True}
        if canonical_read:
            raw["canonical_read"] = True
        return {"second_brain": raw, "vault": {"path": str(self.vault), "recall": True}}

    def drain_all(self):
        rep = {}
        for _ in range(20):
            rep = q.drain(self.state, self.vault, limit=50)
            if not rep["remaining"] or rep["held"] or rep["failed"]:
                break
        return rep

    def notes(self, kind="task"):
        return sorted(p for p in self.vault.rglob("*.md") if p.name.startswith(f"jr-{kind}-"))

    def plant(self, obj, stem, parts):
        """One revision note whose filename stem and folder are chosen by the test."""
        data, digest = self.brain._render(obj, 1, "", "unit", self.brain.clock())
        name = f"{stem}-r0001-{digest[:12]}.md"
        self.brain._write_new(tuple(parts), name, data)
        return name


class PlainHeads(Base):
    def _published_pair(self):
        store = Store(self.state, prune=False)
        task = store.add({"objective": "P" * 70000})
        q.configure(self.state, True)
        self.drain_all()  # publish the queued baseline before the state change
        store.transition(task["id"], "queued", "failed", {"why": "stop"})
        self.drain_all()
        notes = self.notes()
        self.assertEqual([int(re.search(r"-r(\d{4})-", path.name)[1]) for path in notes], [1, 2])
        self.assertGreater(notes[-1].stat().st_size, 64 * 1024)
        self.assertEqual(list(self.vault.rglob("*.jrc")), [])
        return store, task, notes

    def _head_sha(self):
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        try:
            return conn.execute("SELECT payload_sha FROM archive_head").fetchone()[0]
        finally:
            conn.close()

    def test_large_plain_hash_mismatch_is_not_current(self):
        _store, task, notes = self._published_pair()
        healthy = rt.status(self.config(True), self.state)
        self.assertEqual(healthy["state"], "current")
        self.assertGreaterEqual(healthy["archive_chunk_checked"], 1)
        self.assertEqual(healthy["archive_chunk_damaged"], 0)
        self.assertTrue(healthy["canonical"])
        self.assertTrue(self.brain.publication_sound("tasks", "task", task["id"], self._head_sha()))
        newest = notes[-1]
        newest.write_bytes(newest.read_bytes().replace(b"PPPPPPPPPP", b"QQQQQQQQQQ", 1))
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], self._head_sha()))
        bad = rt.status(self.config(True), self.state)
        self.assertEqual(bad["state"], "error")
        self.assertNotEqual(bad["state"], "current")
        self.assertGreaterEqual(bad["archive_chunk_damaged"], 1)
        self.assertFalse(bad["canonical"])
        with self.assertRaises(sb.SecondBrainError):
            self.brain.canonical_records(store="tasks", kind="task")
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, "PPPPPPPPPPPPPPPP"), [])

    def test_intact_large_plain_gap_is_not_sound_and_is_not_repaired(self):
        _store, task, notes = self._published_pair()
        notes[0].unlink()
        out = rt.status(self.config(True), self.state)
        self.assertEqual(out["state"], "conflict")
        self.assertNotEqual(out["state"], "current")
        self.assertGreaterEqual(out["archive_revision_conflict"], 1)
        self.assertEqual(out["archive_chunk_damaged"], 0)
        self.assertGreaterEqual(out["archive_chunk_checked"], 1)  # the large head was opened
        self.assertFalse(out["canonical"])
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], self._head_sha()))
        with self.assertRaises(sb.SecondBrainError) as raised:
            self.brain.canonical_records(store="tasks")
        self.assertIn("revision-gap", str(raised.exception))
        q.rescan(self.state)
        rep = self.drain_all()
        self.assertEqual((rep["repaired"], rep["failed"], rep["remaining"]), (0, 0, 0))
        self.assertEqual(len(self.notes()), 1)
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, "PPPPPPPPPPPPPPPP"), [])

    def test_duplicate_revision_number_is_not_current(self):
        _store, task, notes = self._published_pair()
        src = notes[-1]
        dst = src.with_name(re.sub(r"[0-9a-f]{12}\.md$", "ffffffffffff.md", src.name))
        dst.write_bytes(src.read_bytes())
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["state"], "conflict")
        self.assertNotEqual(out["state"], "current")
        self.assertFalse(out["canonical"])
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], self._head_sha()))
        with self.assertRaises(sb.SecondBrainError):
            self.brain.canonical_records()


class CanonicalRead(Base):
    def _two_revisions(self):
        # body is part of the record identity, so a body edit is a collision hold, not a
        # new revision. title is mutable: the later archive payload must carry NEW only.
        q.configure(self.state, True)
        mem = Memory(self.state)
        item = mem.add("note", "stable-body-not-the-canary", title=OLD)
        self.drain_all()
        conn = sqlite3.connect(self.state / "memory.sqlite3")
        conn.execute("UPDATE items SET title=? WHERE id=?", (NEW, item["id"]))
        conn.commit()
        conn.close()
        self.drain_all()
        return item

    def test_latest_payload_is_served_after_the_database_is_gone(self):
        item = self._two_revisions()
        rid = f"memory/memory-item/{item['id']}"
        healthy = rt.status(self.config(True), self.state)
        self.assertEqual(healthy["state"], "current")
        self.assertTrue(healthy["canonical"])
        self.assertFalse(rt.status(self.config(False), self.state)["canonical"])
        live = self.brain.canonical_records(store="memory", kind="memory-item")
        self.assertEqual(len(live), 1)
        self.assertIn(NEW, json.dumps(live))
        self.assertNotIn(OLD, json.dumps(live))
        self.assertEqual(self.brain.latest(rid)["fields"]["title"], NEW)
        (self.state / "memory.sqlite3").unlink()
        self.assertFalse(rt.status(self.config(True), self.state)["canonical"])
        gone = self.brain.canonical_records(store="memory", kind="memory-item")
        self.assertEqual(gone[0]["fields"]["title"], NEW)
        self.assertNotIn(OLD, json.dumps(gone))
        note = self.vault / "notes" / "canary.md"
        note.parent.mkdir()
        note.write_text(f"---\ntitle: Canary\ntype: note\ntags: []\ncreated: 2026-01-01\n"
                        f"updated: 2026-01-01\n---\n\n{OLD}\n{NEW}\n", encoding="utf-8")
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            lines = recall(self.config(True), self.state, NEW)
        self.assertTrue(any(NEW in line for line in lines), lines)
        self.assertFalse(any(OLD in line for line in lines), lines)
        self.assertTrue(all(line.startswith(f"[vault:archive:{rid}]") for line in lines), lines)
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, "short"), [])

    def test_tombstone_is_skipped_and_gap_fails_closed(self):
        item = self._two_revisions()
        rid = f"memory/memory-item/{item['id']}"
        self.assertTrue(self.brain.tombstone(rid, "forget")["ok"])
        self.assertIsNone(self.brain.latest(rid))
        self.assertEqual(self.brain.canonical_records(store="memory", kind="memory-item"), [])
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, NEW), [])
        # A second live record with a gap must not be answered from SQLite either.
        other = Memory(self.state).add("note", "gap-canary-phrase-cccc", title="Gap")
        self.drain_all()
        Memory(self.state).merge_meta(other["id"], {"n": 2})
        self.drain_all()
        stems = {}
        for path in self.brain.area.rglob("jr-memory-item-*.md"):
            match = sb.NOTE_RE.match(path.name)
            if not match or match["tomb"]:
                continue
            stems.setdefault(match["stem"], []).append((int(match["rev"]), path))
        gapped = next(revs for revs in stems.values() if len(revs) >= 2)
        gapped.sort()
        gapped[0][1].unlink()
        with self.assertRaises(sb.SecondBrainError):
            self.brain.canonical_records(store="memory")
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, "gap-canary-phrase-cccc"), [])
        out = rt.status(self.config(True), self.state)
        self.assertNotEqual(out["state"], "current")
        self.assertFalse(out["canonical"])
        self.assertGreaterEqual(out["archive_revision_conflict"], 1)

    def test_a_conflict_outside_the_filter_does_not_hide_a_healthy_store(self):
        item = self._two_revisions()
        _store, task, notes = PlainHeads._published_pair(self)
        notes[0].unlink()
        memory = self.brain.canonical_records(store="memory", kind="memory-item")
        self.assertEqual(memory[0]["key"], item["id"])
        with self.assertRaises(sb.SecondBrainError):
            self.brain.canonical_records(store="tasks", kind="task")
        self.assertTrue(task)

    def test_vault_privacy_gates_bind_both_recall_paths(self):
        self._two_revisions()
        opened = recall(self.config(True), self.state, NEW)
        self.assertTrue(any(NEW in line for line in opened), opened)
        for key in ("enabled", "recall"):
            blocked = self.config(True)
            blocked["vault"][key] = False
            self.assertEqual(recall(blocked, self.state, NEW), [])
        weird = self.config(True)
        weird["vault"]["enabled"] = 0
        weird["vault"]["recall"] = 0
        self.assertTrue(any(NEW in line for line in recall(weird, self.state, NEW)))
        sentinel = ["[vault:notes/ordinary.md] ordinary-marker"]
        with patch.object(Vault, "context", return_value=sentinel) as ctx:
            self.assertEqual(recall(self.config(False), self.state, NEW), sentinel)
            self.assertEqual(ctx.call_count, 1)
            for key in ("enabled", "recall"):
                blocked = self.config(False)
                blocked["vault"][key] = False
                self.assertEqual(recall(blocked, self.state, NEW), [])
            self.assertEqual(ctx.call_count, 1)
            odd = self.config(False)
            odd["vault"]["enabled"] = 0
            odd["vault"]["recall"] = 0
            self.assertEqual(recall(odd, self.state, NEW), sentinel)
            self.assertEqual(ctx.call_count, 2)

    def test_iterator_loads_one_payload_at_a_time(self):
        q.configure(self.state, True)
        mem = Memory(self.state)
        for i in range(3):
            mem.add("note", f"lazy-body-{i}-xxxxxxxx", title=f"Lazy {i}")
        rep = self.drain_all()
        self.assertEqual(rep["remaining"], 0, rep)
        self.assertFalse(rep["failed"], rep)
        notes = sorted(p for p in self.brain.area.rglob("*.md") if p.name.startswith("jr-memory-item-"))
        self.assertEqual(len(notes), 3, [p.name for p in notes])
        loaded = []
        real_load = SecondBrain._load

        def spy(brain, path):
            loaded.append(Path(path).name)
            return real_load(brain, path)

        with patch.object(SecondBrain, "_load", spy):
            gen = self.brain.iter_canonical_records(store="memory", kind="memory-item")
            self.assertIsInstance(gen, types.GeneratorType)
            self.assertEqual(loaded, [])
            next(gen)
            self.assertEqual(len(loaded), 1)
            next(gen)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(len(list(gen)), 1)
            self.assertEqual(len(loaded), 3)
        found = self.brain.canonical_records(store="memory", kind="memory-item")
        self.assertIsInstance(found, list)
        self.assertEqual(len(found), 3)

    def test_later_bad_head_fails_closed_and_recall_drops_partial_lines(self):
        item = self._two_revisions()
        _store, _task, notes = PlainHeads._published_pair(self)
        report = {"queue": "absent"}
        with patch("jarvis.second_brain_runtime.status", return_value=report), \
                patch.object(Vault, "context", return_value=["legacy-should-not-run"]):
            healthy = recall(self.config(True), self.state, NEW, limit=1)
        self.assertTrue(any(NEW in line and line.startswith("[vault:archive:") for line in healthy), healthy)
        self.assertFalse(any("legacy" in line for line in healthy))
        gen = self.brain.iter_canonical_records()
        first = next(gen)
        self.assertEqual(first["key"], item["id"])
        notes[-1].write_bytes(notes[-1].read_bytes().replace(b"PPPPPPPPPP", b"QQQQQQQQQQ", 1))
        with self.assertRaises(sb.SecondBrainError):
            list(gen)
        with self.assertRaises(sb.SecondBrainError):
            self.brain.canonical_records()
        loaded = []
        real_load = SecondBrain._load

        def spy(brain, path):
            loaded.append(Path(path).name)
            return real_load(brain, path)

        with patch.object(SecondBrain, "_load", spy), \
                patch("jarvis.second_brain_runtime.status", return_value=report), \
                patch.object(Vault, "context", return_value=["legacy-should-not-run"]):
            self.assertEqual(recall(self.config(True), self.state, NEW, limit=1), [])
        self.assertTrue(any(name.startswith("jr-memory-item-") for name in loaded), loaded)
        self.assertTrue(any(name.startswith("jr-task-") for name in loaded), loaded)

    def test_output_budget_counts_separators_only_between_lines(self):
        # Two real archive hits. The cap is len("\n".join(lines)): an exact join fits,
        # and one character under that drops a whole later line.
        q.configure(self.state, True)
        mem = Memory(self.state)
        mem.add("note", "budgetcanary alpha archive body", title="Alpha budgetcanary")
        mem.add("note", "budgetcanary beta archive body", title="Beta budgetcanary")
        rep = self.drain_all()
        self.assertEqual(rep["remaining"], 0, rep)
        self.assertFalse(rep["failed"], rep)

        def run(cap):
            with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
                return recall(self.config(True), self.state, "budgetcanary", limit=8, max_chars=cap)

        wide = run(4000)
        self.assertGreaterEqual(len(wide), 2, wide)
        self.assertEqual(len(wide), len(set(wide)), wide)
        exact = len("\n".join(wide))
        self.assertGreaterEqual(exact, 80)
        self.assertLessEqual(exact, 4000)
        fitted = run(exact)
        self.assertEqual(fitted, wide)
        self.assertEqual(len("\n".join(fitted)), exact)
        self.assertLessEqual(len("\n".join(fitted)), exact)
        first, second = wide[0], wide[1]
        boundary = len(first) + len(second)
        self.assertEqual(len("\n".join([first, second])), boundary + 1)
        self.assertGreaterEqual(boundary, 80)
        held = run(boundary)
        self.assertEqual(held, [first])
        self.assertLessEqual(len("\n".join(held)), boundary)
        self.assertTrue(all(line in wide for line in held), held)
        tight = run(exact - 1)
        self.assertEqual(tight, wide[:-1])
        self.assertLessEqual(len("\n".join(tight)), exact - 1)
        self.assertTrue(all(line in wide for line in tight), tight)
        one = run(len(first))
        self.assertEqual(one, [first])
        self.assertEqual(len("\n".join(one)), len(first))

    def test_malformed_stem_is_an_identity_mismatch(self):
        obj = sb.build_payload("memory", "memory-item", "real-key", "unit",
                               {"title": "Mismatch", "text": "placed-identity-mismatch"})
        wrong, store, kind, digest = self.brain.stem_for("memory/memory-item/other-key")
        self.plant(obj, wrong, (store, sb._slug(kind, 20), digest[:2]))
        with self.assertRaises(sb.SecondBrainError) as raised:
            list(self.brain.iter_canonical_records(store="memory", kind="memory-item"))
        self.assertIn("record-identity-mismatch", str(raised.exception))

    def test_malformed_placement_is_an_identity_mismatch(self):
        obj = sb.build_payload("memory", "memory-links", "link-key", "unit",
                               {"title": "Bad place", "text": "bad-placed-identity"})
        stem, _store, _kind, digest = self.brain.stem_for("memory/memory-links/link-key")
        self.plant(obj, stem, ("memory", sb._slug("memory-item", 20), digest[:2]))
        with self.assertRaises(sb.SecondBrainError) as raised:
            list(self.brain.iter_canonical_records(store="memory", kind="memory-item"))
        self.assertIn("record-identity-mismatch", str(raised.exception))

    def test_a_future_kind_that_shares_a_slug_is_skipped(self):
        colliders = ("memory-item-collider-a", "memory-item-collider-b")
        shared = sb._slug(colliders[0], 20)
        self.assertEqual(shared, sb._slug(colliders[1], 20))
        self.assertNotEqual(shared, sb._slug("memory-item", 20))
        with patch.dict(sb.KINDS, {"memory": sb.KINDS["memory"] + colliders}):
            for kind, key, title in ((colliders[0], "ka", "Alpha"), (colliders[1], "kb", "Beta")):
                wrote = self.brain.publish(sb.build_payload(
                    "memory", kind, key, "unit", {"title": title, "text": title}))
                self.assertEqual(wrote["status"], "written", wrote)
            chosen = self.brain.canonical_records(store="memory", kind=colliders[0])
            self.assertEqual([row["kind"] for row in chosen], [colliders[0]])
            both = [row["kind"] for row in self.brain.iter_canonical_records(store="memory")]
            self.assertEqual(sorted(both), sorted(colliders))
            bad = sb.build_payload("memory", "memory-item", "bad-place", "unit",
                                   {"title": "Bad", "text": "bad-placed-identity"})
            stem, _store, _kind, digest = self.brain.stem_for("memory/memory-item/bad-place")
            self.plant(bad, stem, ("memory", shared, digest[:2]))
            with self.assertRaises(sb.SecondBrainError) as raised:
                self.brain.canonical_records(store="memory", kind=colliders[0])
            self.assertIn("record-identity-mismatch", str(raised.exception))


    def test_retired_records_stay_out_of_recall_and_column_names_are_not_hits(self):
        """S1/S2: opt-in recall hides forgotten, replaced, and closed bodies, and a
        column name is not a hit. Body, history, and task text still match. Every
        selected head is still loaded, including after the query matches nothing."""
        q.configure(self.state, True)
        mem = Memory(self.state)
        forgotten = mem.add("profile", "jasminecanaryquietmornings")
        replaced = mem.add("fact", "replacedfactcedarmoss")
        closed = mem.add("loop", "closedloopharborlight")
        active = mem.add("profile", "active cedar profile stays visible")
        noted = mem.add("note", "uniquebodytoken in the note text", title="Unrelated heading")
        History(self.state).record(
            "historytoken west window",
            {"device": "main", "intent": "open", "args": {"app": "x"}},
            {"ok": True, "state": "verified", "text": "done"}, text="ran")
        task = Store(self.state, prune=False).add({"objective": "taskbodytoken ship the ledger"})
        self.drain_all()
        self.assertTrue(mem.forget_profile(forgotten["id"]))
        self.assertTrue(mem.retire(replaced["id"], "replaced"))
        self.assertTrue(mem.retire(closed["id"], "closed"))
        rep = self.drain_all()
        self.assertEqual(rep["remaining"], 0, rep)
        self.assertFalse(rep["failed"], rep)
        status = rt.status(self.config(True), self.state)
        self.assertEqual(status["state"], "current", status)
        self.assertTrue(status["canonical"], status)
        self.assertEqual([row["text"] for row in mem.profile()], ["active cedar profile stays visible"])
        for item, kind in ((forgotten, "forgotten"), (replaced, "replaced"), (closed, "closed")):
            latest = self.brain.latest(f"memory/memory-item/{item['id']}")
            self.assertEqual(latest["fields"]["kind"], kind)
            self.assertIn(item["body"], latest["fields"]["body"])
        self.assertFalse(any(path.name.endswith("-t.md") for path in self.vault.rglob("*.md")))
        self.assertTrue(self.brain.validate()["ok"])

        def ask(text):
            with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
                return recall(self.config(True), self.state, text)

        self.assertEqual(ask("jasminecanaryquietmornings"), [])
        self.assertEqual(ask("replacedfactcedarmoss"), [])
        self.assertEqual(ask("closedloopharborlight"), [])
        self.assertEqual(ask("check the importance value now"), [])
        self.assertEqual(ask("created column is not content"), [])
        active_lines = ask("active cedar profile stays visible")
        self.assertTrue(any("active cedar profile stays visible" in line for line in active_lines), active_lines)
        body_lines = ask("uniquebodytoken")
        self.assertTrue(any("uniquebodytoken" in line for line in body_lines), body_lines)
        self.assertTrue(any(noted["id"] in line for line in body_lines), body_lines)
        self.assertTrue(any("historytoken" in line for line in ask("historytoken west window")))
        task_lines = ask("taskbodytoken ship the ledger")
        self.assertTrue(any("taskbodytoken" in line for line in task_lines), task_lines)
        self.assertTrue(any(task["id"] in line for line in task_lines), task_lines)
        latest, _, _, _ = self.brain._scan()
        expect = sorted(
            info["name"] for info in latest.values()
            if self.brain._placed(info)[0] in ("memory", "history", "tasks")
        )
        self.assertGreaterEqual(len(expect), 6, expect)
        loaded = []
        real_load = SecondBrain._load

        def spy(brain, path):
            loaded.append(Path(path).name)
            return real_load(brain, path)

        with patch.object(SecondBrain, "_load", spy), \
                patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            self.assertEqual(recall(self.config(True), self.state, "check the importance value now"), [])
        self.assertTrue(set(expect).issubset(loaded), (loaded, expect))


class KindSlugInvariant(unittest.TestCase):
    def test_no_per_store_kind_slug_collision(self):
        for store, kinds in sb.KINDS.items():
            slugs = [sb._slug(kind, 20) for kind in kinds]
            self.assertEqual(len(slugs), len(set(slugs)), (store, kinds, slugs))
            self.assertTrue(all(slugs), (store, slugs))


if __name__ == "__main__":
    unittest.main()
