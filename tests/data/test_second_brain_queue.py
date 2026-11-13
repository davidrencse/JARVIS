"""Second-brain write-through outbox (jarvis/second_brain_queue.py): transactional capture, exact
ordered publish, crash/ack safety, holds, prune protection, disabled parity, inert restore.

Fake vault and fake state in temp folders only: no real vault, state, model or network."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.history import History  # noqa: E402
from jarvis.locking import exclusive_lock  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import SecondBrain  # noqa: E402
from jarvis.store import Store  # noqa: E402

SK = "sk-" + "a" * 30
OUTCOME = {"ok": True, "state": "verified", "text": "done", "observed_ms": 5}
COMMAND = {"device": "main", "intent": "open", "args": {"app": "x"}}


def rows(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.vault = Path(tmp.name) / "vault"
        self.vault.mkdir()
        self.brain = SecondBrain(self.vault)

    def on(self):
        report = q.configure(self.state)
        self.assertTrue(report["ok"], report)
        return report

    def outbox(self, store="memory", where="1=1"):
        return rows(self.state / q.STORE_FILES[store],
                    f"SELECT seq,kind,key,state,held_reason,origin,payload FROM archive_outbox WHERE {where} ORDER BY seq")

    def drain(self, **kw):
        return q.drain(self.state, self.vault, **kw)

    def revisions(self):
        latest, revs, _, _ = self.brain._scan()
        return {stem: sorted(r["rev"] for r in rs) for stem, rs in revs.items()}


class ConfigureStatus(Base):
    def test_configure_creates_all_stores_and_reports_typed_keys(self):
        report = self.on()
        self.assertEqual(set(report["stores"]), {"memory", "history", "tasks"})
        for info in report["stores"].values():
            self.assertTrue(info["configured"] and info["enabled"])
        self.assertTrue(report["marker"])
        self.assertEqual(q.configure(self.state)["errors"], 0)   # idempotent
        st = q.status(self.state)
        for key in ("ok", "enabled", "configured", "capture_complete", "pending", "held", "errors",
                    "oldest_age_s", "last_success", "unreconciled", "stores"):
            self.assertIn(key, st)
        self.assertTrue(st["capture_complete"])

    def test_status_before_configure_is_clean_and_writes_nothing(self):
        st = q.status(self.state)
        self.assertTrue(st["ok"])
        self.assertFalse(st["enabled"] or st["configured"] or st["capture_complete"])
        self.assertFalse(self.state.exists())

    def test_store_created_after_activation_is_adopted(self):
        """configure() before the History db exists must still capture its first command."""
        self.state.mkdir()
        (self.state / q.MARKER).write_text("{}")   # marker only: nothing configured yet
        Memory(self.state).add("note", "x")        # memory adopts on construction
        self.assertEqual(len(self.outbox("memory")), 1)
        History(self.state).record("r", COMMAND, OUTCOME, text="t")
        self.assertEqual(len(self.outbox("history")), 1)
        Store(self.state).add({"objective": "o"})
        self.assertEqual(len(self.outbox("tasks")), 1)

    def test_status_has_no_bodies(self):
        self.on()
        Memory(self.state).add("note", "PRIVATE BODY TEXT", title="PRIVATE TITLE")
        self.assertNotIn("PRIVATE", json.dumps(q.status(self.state)))

    def test_disable_keeps_events_and_marker_removed(self):
        self.on()
        Memory(self.state).add("note", "kept")
        q.configure(self.state, enabled=False)
        self.assertFalse((self.state / q.MARKER).exists())
        Memory(self.state).add("note", "after")
        self.assertEqual(len(self.outbox("memory")), 1)   # nothing new while disabled
        self.assertFalse(q.status(self.state)["enabled"])
        self.assertEqual(q.status(self.state)["pending"], 1)


class DisabledParity(Base):
    def test_no_archive_structures_when_not_configured(self):
        mem = Memory(self.state)
        a = mem.add("note", "same", title="A")
        mem.merge_meta(a["id"], {"k": 1})
        History(self.state).record("r", COMMAND, OUTCOME, text="t", hops=[{"name": "p", "ms": 1}])
        task = Store(self.state).add({"objective": "o"})
        self.assertEqual(Store(self.state).prune(days=0), 0)
        for fname in q.STORE_FILES.values():
            names = [r[0] for r in rows(self.state / fname, "SELECT name FROM sqlite_master")]
            self.assertFalse([n for n in names if n.startswith("archive")], fname)
        self.assertEqual(Store(self.state).get(task["id"])["state"], "queued")
        self.assertEqual(q.drain(self.state, self.vault)["acked"], 0)

    def test_disabled_retention_still_prunes(self):
        Store(self.state)
        hist = History(self.state)
        hist.record("old", COMMAND, OUTCOME, text="t", now=1.0)
        hist.record("new", COMMAND, OUTCOME, text="t", now=1.0 + 200 * 86400)
        self.assertEqual(len(rows(self.state / "history.sqlite3", "SELECT id FROM commands")), 1)


class Capture(Base):
    def test_rapid_updates_make_immutable_ordered_revisions(self):
        self.on()
        mem = Memory(self.state)
        item = mem.add("note", "body", title="T")
        for n in range(1, 5):
            mem.merge_meta(item["id"], {"n": n})
        events = self.outbox("memory", "kind='memory-item'")
        self.assertEqual(len(events), 5)
        metas = [json.loads(json.loads(e[6])["fields"]["meta"]).get("n") for e in events]
        self.assertEqual(metas, [None, 1, 2, 3, 4])   # each event kept ITS state, not the latest row
        self.drain()
        notes = self.revisions()
        stem = next(iter(notes))
        self.assertEqual(notes[stem], [1, 2, 3, 4, 5])
        self.assertEqual(self.outbox("memory"), [])

    def test_duplicate_add_produces_no_false_event(self):
        self.on()
        mem = Memory(self.state)
        mem.add("note", "same text", title="T")
        mem.add("note", "same text", title="T")   # dedupe: INSERT OR IGNORE does nothing
        self.assertEqual(len(self.outbox("memory")), 1)

    def test_noop_update_makes_no_event(self):
        self.on()
        mem = Memory(self.state)
        item = mem.add("note", "x")
        mem.merge_meta(item["id"], {})
        self.assertEqual(len(self.outbox("memory")), 1)

    def test_links_retire_and_absorb(self):
        self.on()
        mem = Memory(self.state)
        a, b = mem.add("fact", "a"), mem.add("fact", "b")
        mem.link(a["id"], b["id"], "related")
        links = self.outbox("memory", "kind='memory-links'")
        self.assertEqual(len(links), 1)
        self.assertEqual(json.loads(links[0][6])["fields"]["links"][0]["dst"], b["id"])
        before = len(self.outbox("memory"))
        self.assertTrue(mem.retire(b["id"], "replaced", by=a["id"]))
        events = self.outbox("memory")[before:]
        self.assertEqual(sorted(e[1] for e in events), ["memory-item", "memory-links"])
        retired = next(json.loads(e[6]) for e in events if e[1] == "memory-item")
        self.assertEqual(retired["fields"]["kind"], "replaced")
        self.drain()
        self.assertEqual(q.status(self.state)["pending"], 0)

    def test_history_hops_and_task_transitions(self):
        self.on()
        cid = History(self.state).record("r", COMMAND, OUTCOME, text="t",
                                         hops=[{"name": "parse", "ms": 1}, {"name": "run", "ms": 2}])
        event = self.outbox("history")[0]
        self.assertEqual(event[2], cid)
        self.assertEqual([h["name"] for h in json.loads(event[6])["children"]["hops"]], ["parse", "run"])
        store = Store(self.state)
        task = store.add({"objective": "o"})
        store.transition(task["id"], "queued", "running")
        store.transition(task["id"], "running", "finished", {"note": "ok"})
        events = self.outbox("tasks")
        self.assertEqual([json.loads(e[6])["fields"]["state"] for e in events], ["queued", "running", "finished"])
        self.assertEqual([len(json.loads(e[6])["children"]["events"]) for e in events], [1, 2, 3])
        self.drain()
        self.assertEqual(sorted(sorted(v) for v in self.revisions().values()), [[1], [1, 2, 3]])

    def test_reap_stale_is_captured(self):
        self.on()
        store = Store(self.state)
        task = store.add({"objective": "o"})
        store.transition(task["id"], "queued", "running", {"pid": 1})
        with patch("jarvis.daemon._alive", return_value=False):
            self.assertEqual(store.reap_stale(), [task["id"]])
        last = json.loads(self.outbox("tasks")[-1][6])
        self.assertEqual(last["fields"]["state"], "failed")

    def test_enqueue_failure_rolls_the_mutation_back(self):
        self.on()
        for fname in q.STORE_FILES.values():
            conn = sqlite3.connect(self.state / fname)
            conn.execute("CREATE TRIGGER boom BEFORE INSERT ON archive_outbox "
                         "BEGIN SELECT RAISE(ABORT,'disk full'); END")
            conn.commit()
            conn.close()
        mem, store = Memory(self.state), Store(self.state)
        with self.assertRaises(q.ArchiveCaptureError):
            mem.add("note", "never stored")
        self.assertEqual(rows(self.state / "memory.sqlite3", "SELECT COUNT(*) FROM items"), [(0,)])
        self.assertEqual(rows(self.state / "memory.sqlite3", "SELECT COUNT(*) FROM archive_guard"), [(0,)])
        with self.assertRaises(q.ArchiveCaptureError):
            History(self.state).record("r", COMMAND, OUTCOME, text="t")
        self.assertEqual(rows(self.state / "history.sqlite3", "SELECT COUNT(*) FROM commands"), [(0,)])
        with self.assertRaises(q.ArchiveCaptureError):
            store.add({"objective": "o"})
        self.assertEqual(rows(self.state / "tasks.sqlite3", "SELECT COUNT(*) FROM tasks, events"), [(0,)])

    def test_failed_transition_enqueue_does_not_move_the_task(self):
        self.on()
        store = Store(self.state)
        task = store.add({"objective": "o"})
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON archive_outbox BEGIN SELECT RAISE(ABORT,'x'); END")
        conn.commit()
        conn.close()
        with self.assertRaises(q.ArchiveCaptureError):
            store.transition(task["id"], "queued", "running")
        self.assertEqual(store.get(task["id"])["state"], "queued")
        self.assertEqual(len(store.events(task["id"])), 1)

    def test_concurrent_producers(self):
        self.on()
        errors = []

        def work(n):
            try:
                mem = Memory(self.state)
                for i in range(8):
                    mem.add("note", f"thread {n} item {i}")
            except Exception as exc:   # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.outbox("memory")), 32)
        while self.drain(limit=10)["remaining"]:
            pass
        self.assertEqual(len(self.revisions()), 32)

    def test_legacy_writer_bypass_is_reconciled_not_lost(self):
        self.on()
        mem = Memory(self.state)
        item = mem.add("note", "x")
        self.drain()
        conn = sqlite3.connect(self.state / "memory.sqlite3")   # an older process: no capture hook
        conn.execute("UPDATE items SET importance=0.99 WHERE id=?", (item["id"],))
        conn.commit()
        conn.close()
        self.assertEqual(q.status(self.state)["unreconciled"], 1)
        report = self.drain()
        self.assertEqual(report["reconciled"], 1)
        self.assertEqual(report["acked"], 1)
        self.assertEqual(self.outbox("memory"), [])
        self.assertEqual(sorted(self.revisions().popitem()[1]), [1, 2])


class Publish(Base):
    def test_crash_after_publish_before_ack_retries_idempotently(self):
        self.on()
        Memory(self.state).add("note", "x")
        with patch.object(q, "_ack", side_effect=OSError("power cut")):
            crashed = self.drain()
        self.assertFalse(crashed["ok"])
        self.assertIn("power cut", crashed["error"])
        self.assertEqual(q.status(self.state)["pending"], 1)
        self.assertEqual(sorted(self.revisions().popitem()[1]), [1])
        report = self.drain()
        self.assertEqual((report["acked"], report["unchanged"], report["published"]), (1, 1, 0))
        self.assertEqual(sorted(self.revisions().popitem()[1]), [1])   # no duplicate revision
        self.assertEqual(q.status(self.state)["pending"], 0)

    def test_published_note_holds_the_exact_queued_revision(self):
        self.on()
        mem = Memory(self.state)
        item = mem.add("note", "v1", title="T")
        mem.merge_meta(item["id"], {"n": 1})
        mem.merge_meta(item["id"], {"n": 2})
        self.drain(limit=1)   # only the first revision
        self.assertEqual(q.status(self.state)["pending"], 2)
        latest, revs, _, _ = self.brain._scan()
        info = next(iter(latest.values()))
        self.assertEqual(info["rev"], 1)
        parsed = self.brain.parse_note(self.brain._read_note(Path(info["dir"]) / info["name"]))
        self.assertEqual(json.loads(parsed["obj"]["fields"]["meta"]), {})   # r1 = original, not newest row

    def test_vault_unavailable_keeps_events_pending(self):
        self.on()
        Memory(self.state).add("note", "x")
        report = q.drain(self.state, self.state / "missing-vault")
        self.assertFalse(report["ok"] or report["vault_ok"])
        self.assertEqual(report["acked"], 0)
        self.assertEqual(q.status(self.state)["pending"], 1)
        Memory(self.state).add("note", "captured while vault offline")   # capture needs no vault
        self.assertEqual(q.status(self.state)["pending"], 2)
        self.assertEqual(self.drain()["acked"], 2)

    def test_secret_like_is_held_visibly_and_never_published(self):
        self.on()
        Memory(self.state).add("note", f"token {SK}", title="leaky")
        report = self.drain()
        self.assertEqual((report["held"], report["acked"]), (1, 0))
        self.assertEqual(len(report["held_ids"]), 1)
        self.assertNotIn(SK, json.dumps(report))
        st = q.status(self.state)
        self.assertEqual((st["held"], st["held_reasons"]), (1, {"secret-like": 1}))
        self.assertNotIn(SK, json.dumps(st))
        self.assertEqual(self.revisions(), {})
        self.assertEqual(self.drain()["held"], 0)   # held events are not retried every drain
        released = self.outbox("memory")[0]
        rid = f"memory/memory-item/{released[2]}"
        self.assertEqual(self.drain(allow_secret_like=[rid])["acked"], 1)   # explicit owner release

    def test_held_head_blocks_later_revisions_of_same_record_only(self):
        self.on()
        mem = Memory(self.state)
        leaky = mem.add("note", f"token {SK}")
        ok = mem.add("note", "fine")
        mem.merge_meta(leaky["id"], {"n": 1})
        report = self.drain()
        self.assertEqual(report["acked"], 1)   # only the clean record
        self.assertEqual(len(self.outbox("memory")), 2)   # leaky r1 (held) + leaky r2 (pending, blocked)
        self.assertEqual(len(self.revisions()), 1)
        self.assertTrue(ok)

    def test_largest_legal_memory_is_archived_losslessly(self):
        """Audit: Memory allows 2M chars, the old 4 MiB cap held a legal 4-byte-UTF-8 record forever.
        The coherent cap must cover the worst legal record (also the 6-byte `\\u0001` escape) with
        every revision kept."""
        self.on()
        mem = Memory(self.state)
        worst = {"emoji": "\U0001F600" * 2_000_000, "control": "\x01" * 2_000_000}
        items = {name: mem.add("note", body, title=name) for name, body in worst.items()}
        mem.merge_meta(items["emoji"]["id"], {"n": 1})        # a later revision must not be blocked
        report = self.drain(limit=50)
        self.assertEqual((report["held"], report["failed"], report["remaining"]), (0, 0, 0))
        self.assertEqual(q.status(self.state)["held_reasons"], {})
        brain = SecondBrain(self.vault)
        for name, item in items.items():
            got = brain.latest(f"memory/memory-item/{item['id']}")
            self.assertEqual(got["fields"]["body"], worst[name])
        self.assertTrue(brain.validate()["ok"])
        target = self.state.parent / "rebuilt"
        self.assertTrue(brain.restore(target)["ok"])
        self.assertEqual(Memory(target).get(items["emoji"]["id"])["body"], worst["emoji"])

    def test_record_beyond_the_cap_is_rejected_before_mutation(self):
        """A bound still exists: a record the archive cannot hold is refused with the write rolled
        back (never silently truncated, never a poison row blocking later revisions)."""
        self.on()
        mem = Memory(self.state)
        with patch.object(q, "MAX_RECORD_BYTES", 1000):
            with self.assertRaises(q.ArchiveCaptureError):
                mem.add("note", "x" * 5000)
        self.assertEqual(sqlite3.connect(self.state / "memory.sqlite3").execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        self.assertEqual(self.outbox("memory"), [])

    def test_held_oversize_released_with_larger_bound(self):
        self.on()
        Memory(self.state).add("note", "x" * 4000)
        self.assertEqual(self.drain(max_record_bytes=1000)["held"], 1)
        self.assertEqual(q.release_held(self.state, reason="oversize"), 1)
        self.assertEqual(self.drain()["acked"], 1)

    def test_corrupt_spool_row_is_held(self):
        self.on()
        Memory(self.state).add("note", "x")
        conn = sqlite3.connect(self.state / "memory.sqlite3")
        conn.execute("UPDATE archive_outbox SET payload=payload||' '")
        conn.commit()
        conn.close()
        self.assertEqual(self.drain()["held"], 1)
        self.assertEqual(q.status(self.state)["held_reasons"], {"spool-corrupt": 1})

    def test_duplicate_and_busy_drains(self):
        self.on()
        Memory(self.state).add("note", "x")
        with exclusive_lock(self.state / q.LOCK):
            busy = self.drain()
            self.assertTrue(busy["busy"] and not busy["ok"])
            self.assertEqual(q.status(self.state)["pending"], 1)
        self.assertEqual(self.drain()["acked"], 1)
        again = self.drain()
        self.assertEqual((again["acked"], again["published"], again["remaining"]), (0, 0, 0))

    def test_limit_bounds_a_drain_and_dry_run_writes_nothing(self):
        self.on()
        mem = Memory(self.state)
        for i in range(6):
            mem.add("note", f"n{i}")
        dry = self.drain(dry_run=True, limit=2)
        self.assertEqual(dry["acked"], 0)
        self.assertFalse(any(self.vault.iterdir()))
        self.assertEqual(self.drain(limit=2)["acked"], 2)
        self.assertEqual(q.status(self.state)["pending"], 4)

    def test_report_keys_are_complete(self):
        self.on()
        report = self.drain()
        for key in ("ok", "busy", "vault_ok", "acked", "published", "unchanged", "held", "failed",
                    "reconciled", "remaining", "held_ids", "error_samples", "elapsed_ms", "stores"):
            self.assertIn(key, report)
        self.assertTrue(report["ok"] and report["vault_ok"])
        self.assertIsNotNone(q.status(self.state)["last_success"] or True)

    def test_last_success_set_after_ack(self):
        self.on()
        Memory(self.state).add("note", "x")
        self.assertIsNone(q.status(self.state)["last_success"])
        self.drain()
        self.assertIsNotNone(q.status(self.state)["last_success"])


class PruneProtection(Base):
    def test_history_not_pruned_while_capture_on(self):
        self.on()
        hist = History(self.state)
        hist.record("old", COMMAND, OUTCOME, text="t", now=1.0, hops=[{"name": "p", "ms": 1}])
        hist.record("new", COMMAND, OUTCOME, text="t", now=1.0 + 400 * 86400)
        self.assertEqual(len(rows(self.state / "history.sqlite3", "SELECT id FROM commands")), 2)
        self.assertEqual(len(rows(self.state / "history.sqlite3", "SELECT * FROM hops")), 1)

    def test_task_prune_suspended_while_capture_on(self):
        self.on()
        store = Store(self.state)
        task = store.add({"objective": "o"})
        store.transition(task["id"], "queued", "running")
        store.transition(task["id"], "running", "finished")
        self.assertEqual(store.prune(days=-1), 0)
        self.assertEqual(store.get(task["id"])["state"], "finished")
        self.assertEqual(Store(self.state).prune_if_due(), 0)


class InertRestore(Base):
    def test_restored_state_is_inert_and_has_no_capture(self):
        self.on()
        store = Store(self.state)
        done, running = store.add({"objective": "a"}), store.add({"objective": "b"})
        store.transition(running["id"], "queued", "running")
        Memory(self.state).add("note", "m")
        History(self.state).record("r", COMMAND, OUTCOME, text="t")
        self.assertEqual(self.drain()["failed"], 0)
        target = Path(self.state.parent) / "restored"
        result = self.brain.restore(target)
        self.assertTrue(result.get("ok", True), result)
        restored = Store(target)
        self.assertEqual({t["state"] for t in map(lambda i: restored.get(i["id"]), restored.list())}, {"interrupted"})
        self.assertFalse((target / q.MARKER).exists())
        self.assertFalse(q.status(target)["enabled"])
        self.assertTrue(self.brain.validate()["ok"])
        self.assertTrue(done)


class RawGuardPolicy(Base):
    """S16 and S17. Temp state and temp vault only."""

    def _cfg(self):
        (self.vault / ".obsidian").mkdir(exist_ok=True)
        return {"second_brain": {"enabled": True, "canonical_read": True},
                "vault": {"path": str(self.vault), "enabled": True, "recall": True}}

    def _runtime(self):
        return rt.status(self._cfg(), self.state)

    def test_scratch_delete_does_not_latch_and_recreated_source_heals_only_when_verified(self):
        self.on()
        mem = Memory(self.state)
        kept = mem.add("note", "keep me visible body")
        stayed = mem.add("note", "second archived body stays")
        self.assertEqual(self.drain()["failed"], 0)
        self.assertEqual(self._runtime()["state"], "current")
        self.assertEqual(q.status(self.state)["source_gone"], 0)
        raw = sqlite3.connect(self.state / "memory.sqlite3")
        raw.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("note:scratch", "note", "scratch", "scratch body", "", "test", 0.5, "{}",
             "digest-scratch", "2026-01-01T00:00:00+00:00"))
        raw.execute("DELETE FROM items WHERE id=?", ("note:scratch",))
        raw.commit()
        raw.close()
        scratch = self.drain()
        self.assertEqual(scratch["source_gone"], 0, scratch)
        self.assertEqual(q.status(self.state)["source_gone"], 0)
        self.assertEqual(self._runtime()["state"], "current")
        self.assertNotIn("note:scratch", " ".join(q.status(self.state)["source_gone_ids"]))

        raw = sqlite3.connect(self.state / "memory.sqlite3")
        saved = tuple(raw.execute("SELECT * FROM items WHERE id=?", (kept["id"],)).fetchone())
        raw.execute("DELETE FROM items WHERE id=?", (kept["id"],))
        raw.execute("DELETE FROM items WHERE id=?", (stayed["id"],))
        raw.commit()
        raw.close()
        other = mem.add("note", "unrelated normal note")
        gone = q.status(self.state)
        self.assertEqual(gone["source_gone"], 2, gone)
        self.assertEqual(gone["unreconciled"], 0)
        self.assertCountEqual(
            gone["source_gone_ids"],
            [f"memory/memory-item/{kept['id']}", f"memory/memory-item/{stayed['id']}"])
        self.assertEqual(mem.get(other["id"])["body"], "unrelated normal note")
        offline = q.drain(self.state, self.state / "missing-vault")
        self.assertFalse(offline["vault_ok"])
        self.assertFalse(offline["ok"])
        self.assertEqual(offline["source_gone"], 2, offline)
        self.assertGreaterEqual(offline["remaining"], 1)
        self.assertEqual(self.brain.latest(f"memory/memory-item/{kept['id']}")["fields"]["body"],
                         "keep me visible body")
        self.assertEqual(self._runtime()["state"], "pending")

        raw = sqlite3.connect(self.state / "memory.sqlite3")
        raw.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", saved)
        raw.commit()
        raw.close()
        still = q.drain(self.state, self.state / "missing-vault")
        self.assertFalse(still["vault_ok"])
        self.assertIn(f"memory/memory-item/{kept['id']}", q.status(self.state)["source_gone_ids"])
        self.assertEqual(self.brain.latest(f"memory/memory-item/{kept['id']}")["fields"]["body"],
                         "keep me visible body")
        self.assertEqual(q.acknowledge_absent(self.state, f"memory/memory-item/{stayed['id']}"), 1)
        after_ack = q.status(self.state)
        self.assertEqual(after_ack["source_gone_ids"], [f"memory/memory-item/{kept['id']}"])
        self.assertEqual(self.brain.latest(f"memory/memory-item/{stayed['id']}")["fields"]["body"],
                         "second archived body stays")
        self.assertFalse(any(path.name.endswith("-t.md") for path in self.vault.rglob("*.md")))
        healed = self.drain()
        self.assertTrue(healed["ok"], healed)
        self.assertEqual(healed["source_gone"], 0, healed)
        self.assertEqual(healed["remaining"], 0, healed)
        self.assertEqual(q.status(self.state)["source_gone"], 0)
        self.assertEqual(self.brain.latest(f"memory/memory-item/{kept['id']}")["fields"]["body"],
                         "keep me visible body")
        self.assertEqual(self.brain.latest(f"memory/memory-item/{stayed['id']}")["fields"]["body"],
                         "second archived body stays")
        self.assertFalse(any(path.name.endswith("-t.md") for path in self.vault.rglob("*.md")))
        self.assertTrue(self.brain.validate()["ok"])
        self.assertEqual(self._runtime()["state"], "current")
        self.assertEqual(self._runtime()["archive_source_gone"], 0)

    def test_raw_legacy_update_does_not_roll_back_an_unrelated_mutation(self):
        self.on()
        store = Store(self.state, prune=False)
        victim = store.add({"objective": "victim"})
        other = store.add({"objective": "hold the lane"})
        store.transition(other["id"], "queued", "running", {"pid": 1})
        self.assertEqual(self.drain()["failed"], 0)
        self.assertEqual(self.outbox("tasks"), [])
        raw = sqlite3.connect(self.state / "tasks.sqlite3")
        raw.execute("UPDATE tasks SET spec=? WHERE id=?",
                    (json.dumps({"objective": "Z" * 4000}), victim["id"]))
        raw.commit()
        raw.close()
        with patch.object(q, "MAX_RECORD_BYTES", 1500):
            added = store.add({"objective": "small unrelated"})
            with patch("jarvis.daemon._alive", return_value=False):
                reaped = store.reap_stale()
            present = {row["id"] for row in store.list()}
            with self.assertRaises(q.ArchiveCaptureError):
                store.add({"objective": "Y" * 4000})
            self.assertEqual({row["id"] for row in store.list()}, present)
        self.assertIn(added["id"], present)
        self.assertIn(victim["id"], present)
        self.assertEqual(reaped, [other["id"]])
        self.assertEqual(store.get(other["id"])["state"], "failed")
        self.assertEqual(store.get(victim["id"])["state"], "queued")
        held = [row for row in self.outbox("tasks") if row[2] == victim["id"]]
        self.assertEqual(len(held), 1, held)
        self.assertEqual(held[0][3:6], ("held", "oversize", "reconcile"))
        self.assertTrue(any(row[2] == added["id"] and row[3] == "pending" for row in self.outbox("tasks")))


if __name__ == "__main__":
    unittest.main()
