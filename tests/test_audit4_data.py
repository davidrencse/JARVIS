"""Audit 4 data-layer fixes: vault additive writes, memory absorb/vectors, events roll,
credits, embed cooldown, knowledge atomic writes, locking, consolidate, store pruning."""

import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis import embed as embed_module
from jarvis import events
from jarvis.credits import Credits
from jarvis.locking import LockBusy, exclusive_lock
from jarvis.memory import Memory
from jarvis.store import Store

sys.path.insert(0, str(Path(__file__).parent))
from test_vault import VaultCase  # noqa: E402


class VaultAdditive(VaultCase):
    def test_read_only_guard_ignores_case(self):
        for rel in ("Archive/x.md", "TEMPLATES/daily.md", "Attachments/a.md"):
            with self.assertRaises(ValueError, msg=rel):
                self.vault._check_writable(rel)

    def test_create_never_clobbers_a_note_made_meanwhile(self):
        full = self.root / "notes" / "race.md"
        full.write_text("theirs", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.vault._save(full, "notes/race.md", "mine", before=None)
        self.assertEqual(full.read_text(encoding="utf-8"), "theirs")
        self.assertEqual([p.name for p in full.parent.glob(".race.md*")], [])

    def test_create_writes_new_note(self):
        full = self.root / "notes" / "fresh.md"
        self.vault._save(full, "notes/fresh.md", "# Fresh\n", before=None)
        self.assertEqual(full.read_text(encoding="utf-8"), "# Fresh\n")
        self.assertEqual([p.name for p in full.parent.glob(".fresh.md*")], [])


class MemoryAbsorb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory = Memory(tmp.name)

    def active(self):
        return [k["text"] for k in self.memory.knowledge(("fact",))]

    def test_changed_detail_replaces_instead_of_merging(self):
        old, _ = self.memory.absorb("fact", "David's exam is on Monday at 9am in Room 12 downtown campus building")
        new, state = self.memory.absorb("fact", "David's exam is on Tuesday at 9am in Room 12 downtown campus building")
        self.assertEqual(state, "added")
        self.assertEqual(self.active(), ["David's exam is on Tuesday at 9am in Room 12 downtown campus building"])
        self.assertNotEqual(old, new)

    def test_negation_is_a_change(self):
        self.memory.absorb("fact", "David likes spicy food very much indeed")
        _, state = self.memory.absorb("fact", "David does not like spicy food very much indeed")
        self.assertEqual(state, "added")
        self.assertEqual(len(self.active()), 1)

    def test_exact_duplicate_still_merges(self):
        first, _ = self.memory.absorb("fact", "David's exam is on Monday at 9am in Room 12 downtown campus building")
        again, state = self.memory.absorb("fact", "David's exam is on Monday at 9am in Room 12 downtown campus building.")
        self.assertEqual((again, state), (first, "merged"))


class MemoryVectors(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory = Memory(tmp.name)

    def test_prune_drops_stale_model_retired_and_orphan_vectors_only(self):
        keep = self.memory.add("fact", "keep me")["id"]
        gone = self.memory.add("fact", "retire me")["id"]
        old = self.memory.add("fact", "old model")["id"]
        self.memory.retire(gone, "forgotten")
        with self.memory.connect() as db:
            for item, model in ((keep, "m"), (gone, "m"), (old, "other"), ("ghost", "m")):
                db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?)", (item, model, b"\0" * 8))
        self.assertEqual(self.memory.prune_vectors("m"), 3)
        with self.memory.connect() as db:
            self.assertEqual([r["id"] for r in db.execute("SELECT id FROM vectors")], [keep])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)

    def test_fuse_skips_vector_ids_without_items(self):
        item = self.memory.add("fact", "thesis chapter")
        with mock.patch.object(self.memory, "similar",
                               return_value=[("ghost", 0.9), (item["id"], 0.8)]):
            keyword = [dict(self.memory.get(item["id"]), rank=0.0)] \
                if hasattr(self.memory, "get") else []
            out = self.memory._fuse("thesis", keyword, (), (), 5)
        if out is not None:
            self.assertNotIn("ghost", [i["id"] for i in out])


class EventsRoll(unittest.TestCase):
    def test_second_roller_does_not_destroy_rolled_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            feed, rolled = base / events.FEED, base / "events.1.jsonl"
            feed.write_text("x" * 100, encoding="utf-8")
            rolled.write_text("precious", encoding="utf-8")
            with mock.patch.object(events, "MAX_FEED", 50):
                real = events.exclusive_lock

                def lock_after_peer_rolled(path, timeout=0.0, poll=0.05):
                    feed.write_text("fresh", encoding="utf-8")  # peer rolled while we waited
                    return real(path, timeout=timeout, poll=poll)
                with mock.patch.object(events, "exclusive_lock", lock_after_peer_rolled):
                    events.emit(base, "turn.x")
            self.assertEqual(rolled.read_text(encoding="utf-8"), "precious")
            self.assertIn("turn.x", feed.read_text(encoding="utf-8"))

    def test_rolls_when_big(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / events.FEED).write_text("x" * 100, encoding="utf-8")
            with mock.patch.object(events, "MAX_FEED", 50):
                events.emit(base, "turn.x")
            self.assertEqual((base / "events.1.jsonl").read_text(encoding="utf-8"), "x" * 100)


class CreditsManual(unittest.TestCase):
    def test_set_manual_atomic_and_concurrent_updates_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            credits = Credits(tmp)
            providers = ["claude", "codex"]
            threads = [threading.Thread(target=credits.set_manual, args=(p, 5)) for p in providers]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(json.loads(credits.manual_path.read_text())), providers)
            self.assertEqual([p.name for p in Path(tmp).glob(".credits-manual*tmp")], [])


class EmbedCooldown(unittest.TestCase):
    def setUp(self):
        saved = dict(embed_module._state)
        embed_module._state.update(missing=False, retry_at=0.0, paths=None, logged=None)
        self.addCleanup(lambda: (embed_module._state.clear(), embed_module._state.update(saved)))
        p = mock.patch.dict(sys.modules, {"onnxruntime": mock.Mock()})
        p.start()
        self.addCleanup(p.stop)

    def test_retries_after_cooldown_and_logs_once(self):
        with mock.patch("huggingface_hub.hf_hub_download",
                        side_effect=FileNotFoundError("gone")), \
                self.assertLogs("jarvis.embed", "WARNING") as logs:
            self.assertFalse(embed_module.available())
            self.assertFalse(embed_module.available())
            self.assertEqual(len(logs.output), 1)
            self.assertIn("gone", logs.output[0])
        embed_module._state["retry_at"] = time.monotonic() - 1
        with mock.patch("huggingface_hub.hf_hub_download", return_value=__file__):
            self.assertTrue(embed_module.available())
        self.assertFalse(embed_module._state["missing"])

    def test_resolved_paths_are_cached(self):
        with mock.patch("huggingface_hub.hf_hub_download", return_value=__file__) as dl:
            embed_module.available()
            embed_module.available()
            self.assertEqual(dl.call_count, 2)  # model + tokenizer, once

    def test_transient_error_has_short_cooldown(self):
        with mock.patch("huggingface_hub.hf_hub_download", side_effect=OSError("busy")), \
                self.assertLogs("jarvis.embed", "WARNING"):
            embed_module.available()
        wait = embed_module._state["retry_at"] - time.monotonic()
        self.assertLessEqual(wait, embed_module.RETRY_ERROR)


class KnowledgeAtomic(unittest.TestCase):
    def test_write_raw_atomic_and_read_only(self):
        from jarvis.knowledge import KnowledgeBase
        with tempfile.TemporaryDirectory() as tmp:
            kb = KnowledgeBase(Path(tmp, "kb"))
            path = Path(tmp, "kb", "raw-test.md")
            kb._write_raw(path, b"hello")
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0)
            self.assertEqual([p.name for p in path.parent.glob("*.tmp")], [])


class LockBusyTests(unittest.TestCase):
    def test_busy_lock_names_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "consolidate.lock")
            with exclusive_lock(path):
                with self.assertRaises(LockBusy) as caught:
                    with exclusive_lock(path):
                        pass
            self.assertIn("consolidate.lock", str(caught.exception))
            self.assertIsInstance(caught.exception, ValueError)
            self.assertNotIn("Plan", str(caught.exception))

    def test_run_due_returns_none_when_locked(self):
        from jarvis import consolidate
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(tmp)
            with mock.patch.object(consolidate, "due", return_value=True), \
                    exclusive_lock(Path(tmp, "consolidate.lock")):
                self.assertIsNone(consolidate.run_due({}, memory, tmp, ask=lambda t: "{}"))


class ConsolidateReplaced(unittest.TestCase):
    def test_replaced_counts_only_successful_retires(self):
        from jarvis import consolidate
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(tmp)
            msg = memory.add("message", "I moved to Berlin")["id"]
            old = memory.add("fact", "David lives in Paris")["id"]
            ghost_kind = memory.add("lesson", "x lesson")["id"]
            memory.retire(ghost_kind, "forgotten")  # no longer active: retire() returns False
            reply = json.dumps({"items": [{"kind": "fact", "text": "David lives in Berlin",
                                           "sources": [msg], "replaces": [old, ghost_kind]}]})
            with mock.patch.object(consolidate, "parse", return_value=(
                    [{"kind": "fact", "text": "David lives in Berlin", "sources": [msg],
                      "same_as": None, "replaces": [old, ghost_kind]}], [], [])):
                summary = consolidate.consolidate(
                    memory, lambda p: reply, now=datetime.now(timezone.utc) + timedelta(days=3))
            self.assertEqual(summary["replaced"], 1)


class StorePrune(unittest.TestCase):
    def test_prunes_old_finished_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            ids = {s: store.add({"objective": s})["id"]
                   for s in ("old-done", "old-queued", "new-done", "old-running")}
            old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
            with store.connect() as db:
                for name, state in (("old-done", "finished"), ("old-queued", "queued"),
                                    ("new-done", "failed"), ("old-running", "running")):
                    db.execute("UPDATE tasks SET state=? WHERE id=?", (state, ids[name]))
                for name in ("old-done", "old-queued", "old-running"):
                    db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, ids[name]))
                db.execute("UPDATE events SET at=?", (old,))
            self.assertEqual(store.prune(), 1)
            left = {t["id"] for t in store.list()}
            self.assertEqual(left, {ids["old-queued"], ids["new-done"], ids["old-running"]})
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE task_id=?",
                                            (ids["old-done"],)).fetchone()[0], 0)

    def test_prune_runs_at_most_daily_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            Store(tmp)
            with mock.patch.object(Store, "prune") as prune:
                Store(tmp)
                prune.assert_not_called()
                stamp = Path(tmp, "tasks.pruned")
                os.utime(stamp, (time.time() - 90000,) * 2)
                Store(tmp)
                prune.assert_called_once()


if __name__ == "__main__":
    unittest.main()
