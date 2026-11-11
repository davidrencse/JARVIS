"""Obsidian archive storage: permanent regressions for the audit findings (AUDIT-OBSIDIAN.md).

Each test asserts the CORRECT behaviour of a bug the audit reproduced: baseline backfill, the
oversize head, two ledgers in one vault, tombstone disagreement, plus the parity / file-store /
privacy contract that makes the archive a trustworthy copy. Temp fixtures only, no vault, no models.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import SecondBrain  # noqa: E402
from jarvis.store import Store  # noqa: E402

SECRET = "sk-" + "a" * 30


def rows(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state, self.vault = self.tmp / "state", self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state.mkdir()
        self.brain = SecondBrain(self.vault)

    def drain(self, **kw):
        return q.drain(self.state, self.vault, **kw)

    def drain_all(self, limit=50, rounds=40):
        report = {}
        for _ in range(rounds):
            report = self.drain(limit=limit)
            if not report["remaining"]:
                break
        return report

    def rid(self, kind, key, store="memory"):
        return f"{store}/{kind}/{key}"

    def config(self, **extra):
        return {"vault": {"enabled": True, "path": str(self.vault), "write": False},
                "second_brain": {"enabled": True, **extra}}


class BaselineBackfill(Base):
    """Audit 1: configure did not seed rows that already existed."""

    def test_rows_present_before_capture_are_archived_not_missed(self):
        mem = Memory(self.state)
        early = mem.add("note", "written before capture existed")
        task = Store(self.state).add({"objective": "pre-existing task"})
        self.assertTrue(q.configure(self.state)["ok"])
        status = q.status(self.state)
        self.assertGreaterEqual(status["baseline_pending"], 2)   # visible work, not silence
        self.assertTrue(status["baseline_seeded"])
        report = self.drain_all()
        self.assertEqual((report["remaining"], report["held"], report["failed"]), (0, 0, 0))
        self.assertEqual(self.brain.latest(self.rid("memory-item", early["id"]))["fields"]["body"], early["body"])
        self.assertIsNotNone(self.brain.latest(self.rid("task", task["id"], "tasks")))
        self.assertEqual(q.status(self.state)["baseline_pending"], 0)

    def test_rows_in_the_window_after_a_bulk_snapshot_are_captured(self):
        """C1 snapshot, rows added while capture was off, restart: the window rows must not stay missing."""
        mem = Memory(self.state)
        first = mem.add("note", "in the snapshot")
        self.assertTrue(self.brain.sync(self.state)["ok"])
        window = mem.add("note", "added between snapshot and capture")
        q.configure(self.state)
        self.drain_all()
        self.assertEqual(self.brain.latest(self.rid("memory-item", window["id"]))["fields"]["body"], window["body"])
        self.assertIsNotNone(self.brain.latest(self.rid("memory-item", first["id"])))
        parity = self.brain.parity(self.state, stores=["memory"])
        self.assertTrue(parity["in_sync"], parity)

    def test_baseline_is_bounded_and_resumable(self):
        mem = Memory(self.state)
        for n in range(30):
            mem.add("note", f"row {n}")
        q.configure(self.state)
        first = self.drain(limit=10)
        self.assertLessEqual(first["baselined"], 10)             # one drain never scans everything
        self.assertGreater(first["baseline_pending"], 0)
        self.assertGreater(first["remaining"], 0)
        report = self.drain_all(limit=10, rounds=100)
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(q.status(self.state)["heads"], 30)

    def test_empty_drain_before_baseline_is_not_current(self):
        Memory(self.state).add("note", "existing")
        q.configure(self.state)
        out = rt.status(self.config(), self.state)
        self.assertNotEqual(out["state"], "current")
        self.assertGreater(out["archive_baseline_pending"], 0)
        self.drain_all()
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["archive_baseline_pending"], 0)
        self.assertEqual(out["state"], "current")

    def test_database_adopted_late_is_seeded_too(self):
        q.configure(self.state)                     # tasks.sqlite3 does not exist yet
        Store(self.state).add({"objective": "late db"})   # first write creates and adopts it
        self.drain_all()
        self.assertEqual(rows(self.state / "tasks.sqlite3", "SELECT COUNT(*) FROM archive_head")[0][0], 1)

    def test_rescan_requeues_everything_without_rewriting(self):
        Memory(self.state).add("note", "stable")
        q.configure(self.state)
        self.drain_all()
        before = sorted(p.name for p in self.vault.rglob("*.md"))
        q.rescan(self.state)
        report = self.drain_all()
        self.assertGreaterEqual(report["baselined"] + report["acked"], 0)
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(sorted(p.name for p in self.vault.rglob("*.md")), before)


class Oversize(Base):
    """Audit 2: a legal 2M-char memory was held forever and blocked every later revision."""

    def test_previously_held_oversize_rows_are_recovered_without_loss(self):
        q.configure(self.state)
        item = Memory(self.state).add("note", "\U0001F600" * 2_000_000, title="big")
        # simulate the pre-fix state: the head was parked as held 'oversize' under the old 4 MiB cap
        db = sqlite3.connect(self.state / "memory.sqlite3")
        db.execute("UPDATE archive_outbox SET state='held', held_reason='oversize'")
        db.commit()
        db.close()
        self.assertEqual(q.status(self.state)["held_reasons"], {"oversize": 1})
        Memory(self.state).merge_meta(item["id"], {"later": 1})   # a later revision is queued behind it
        report = self.drain_all()
        self.assertEqual((report["held"], report["remaining"]), (0, 0))
        self.assertGreaterEqual(report["released_oversize"], 1)
        note = self.brain.latest(self.rid("memory-item", item["id"]))
        self.assertEqual(note["fields"]["body"], item["body"])
        self.assertEqual(sorted(r for revs in self.brain._scan()[1].values() for r in [x["rev"] for x in revs]),
                         [1, 2])   # EVERY queued revision published

    def test_secret_like_hold_stays_visible_after_the_cap_change(self):
        q.configure(self.state)
        Memory(self.state).add("note", f"token {SECRET}")
        self.drain_all()
        self.assertEqual(q.status(self.state)["held"], 1)
        self.assertEqual(q.status(self.state)["held_reasons"], {"secret-like": 1})
        self.assertEqual(list(self.vault.rglob("*-r0001-*.md")), [])
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["state"], "held")
        self.assertNotIn(SECRET, json.dumps(out))


class SeveralLedgers(Base):
    """Audit 3: two task ledgers in one vault made strict restore fail on events.seq."""

    def _install(self, name, body):
        directory = self.tmp / name
        directory.mkdir()
        Store(directory).add({"objective": body})
        return directory

    def test_two_installs_restore_with_remapped_event_seq_and_both_tasks(self):
        a, b = self._install("a", "from a"), self._install("b", "from b")
        self.assertTrue(self.brain.sync(a)["ok"])
        self.assertTrue(self.brain.sync(b)["ok"])
        target = self.tmp / "rebuilt"
        restored = self.brain.restore(target)
        self.assertTrue(restored["ok"], restored["errors"])
        self.assertEqual(restored["restored"]["tasks"], 2)
        self.assertEqual(restored.get("seq_remapped_tasks"), 1)
        seqs = [r[0] for r in rows(target / "tasks.sqlite3", "SELECT seq FROM events ORDER BY seq")]
        self.assertEqual(len(seqs), len(set(seqs)))
        mapping = json.loads((target / "archive-restore-map.json").read_text())
        self.assertEqual(len(mapping["tasks"]), 1)    # original source + seq kept as provenance

    def test_single_ledger_roundtrip_keeps_original_event_seq(self):
        a = self._install("a", "only")
        store = Store(a)
        tid = rows(a / "tasks.sqlite3", "SELECT id FROM tasks")[0][0]
        store.transition(tid, "queued", "running")
        store.transition(tid, "running", "finished", {"note": "ok"})   # terminal: restore adds no event
        before = rows(a / "tasks.sqlite3", "SELECT seq FROM events ORDER BY seq")
        self.brain.sync(a)
        target = self.tmp / "rebuilt"
        self.assertTrue(self.brain.restore(target)["ok"])
        self.assertEqual(rows(target / "tasks.sqlite3", "SELECT seq FROM events ORDER BY seq"), before)
        self.assertFalse((target / "archive-restore-map.json").exists())

    def test_same_memory_id_with_different_content_is_an_explicit_collision(self):
        for name, body in (("a", "from-install-a"), ("b", "from-install-b")):
            directory = self.tmp / name
            directory.mkdir()
            with Memory(directory).connect() as db:
                db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                           ("note:shared", "note", "T", body, "", "owner", 0.5, "{}", "d",
                            "2026-01-01T00:00:00+00:00"))
        self.assertTrue(self.brain.sync(self.tmp / "a", stores=["memory"])["ok"])
        clash = self.brain.sync(self.tmp / "b", stores=["memory"])
        self.assertFalse(clash["ok"])
        self.assertIn("identity-collision", clash["failures"][0]["error"])
        self.assertEqual(self.brain.latest(self.rid("memory-item", "note:shared"))["fields"]["body"], "from-install-a")

    def test_queue_holds_an_identity_collision_visibly(self):
        a = self.tmp / "a"
        a.mkdir()
        with Memory(a).connect() as db:
            db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("note:shared", "note", "T", "theirs", "", "owner", 0.5, "{}", "d",
                        "2026-01-01T00:00:00+00:00"))
        self.brain.sync(a, stores=["memory"])
        q.configure(self.state)
        with Memory(self.state).connect() as db:
            db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("note:shared", "note", "T", "mine", "", "owner", 0.5, "{}", "d",
                        "2026-01-01T00:00:00+00:00"))
        self.drain_all()
        self.assertEqual(q.status(self.state)["held_reasons"], {"identity-collision": 1})
        self.assertEqual(self.brain.latest(self.rid("memory-item", "note:shared"))["fields"]["body"], "theirs")


class TombstoneAgreement(Base):
    """Audit 4: filename flag, frontmatter and payload had to agree; validate said OK, restore failed."""

    def _tombstoned(self):
        mem = Memory(self.state)
        item = mem.add("note", "real body")
        self.assertTrue(self.brain.sync(self.state, stores=["memory"])["ok"])
        self.brain.tombstone(self.rid("memory-item", item["id"]), "owner forgot it")
        return item, next(p for p in self.brain.area.rglob("*-t.md"))

    def test_consistent_tombstone_validates_and_restore_skips_the_row(self):
        item, _ = self._tombstoned()
        self.assertTrue(self.brain.validate()["ok"])
        target = self.tmp / "out"
        restored = self.brain.restore(target)
        self.assertTrue(restored["ok"], restored["errors"])
        self.assertEqual(restored["tombstoned"], 1)
        self.assertEqual(rows(target / "memory.sqlite3", "SELECT COUNT(*) FROM items")[0][0], 0)

    def test_filename_flag_that_disagrees_is_rejected_by_validate_and_restore(self):
        item, note = self._tombstoned()
        note.rename(note.with_name(note.name.replace("-t.md", ".md")))
        valid = self.brain.validate()
        self.assertFalse(valid["ok"])
        self.assertIn("tombstone", json.dumps(valid))
        target = self.tmp / "out"
        restored = self.brain.restore(target)
        self.assertFalse(restored["ok"])
        self.assertFalse(target.exists())      # nothing applied, nothing left behind

    def test_frontmatter_that_disagrees_is_rejected(self):
        _, note = self._tombstoned()
        text = note.read_text(encoding="utf-8")
        self.assertIn("jarvis_tombstone", text)
        note.write_text(text.replace("jarvis_tombstone: true", "jarvis_tombstone: false"), encoding="utf-8")
        self.assertFalse(self.brain.validate()["ok"])
        self.assertFalse(self.brain.restore(self.tmp / "out")["ok"])

    def test_tombstone_history_is_never_resurrected_by_sync(self):
        item, _ = self._tombstoned()
        again = self.brain.sync(self.state, stores=["memory"])
        self.assertEqual(again["written"], 0)
        self.assertGreaterEqual(again["tombstoned_skipped"], 1)


class Privacy(Base):
    def test_no_archive_records_are_counted_never_queued_or_written(self):
        mem = Memory(self.state)
        mem.add("note", "keep on this pc", tags=["no-archive"])
        mem.add("note", "private by meta", meta={"archive": False})
        shared = mem.add("note", "fine to archive")
        q.configure(self.state)
        report = self.drain_all()
        self.assertEqual(q.status(self.state)["heads"], 1)
        self.assertGreaterEqual(q.status(self.state)["excluded"], 2)
        self.assertEqual(len(list(self.vault.rglob("*-r0001-*.md"))), 1)
        self.assertIsNotNone(self.brain.latest(self.rid("memory-item", shared["id"])))
        text = json.dumps(report) + json.dumps(q.status(self.state))
        self.assertNotIn("keep on this pc", text)
        self.assertNotIn("private by meta", text)
        sync = self.brain.sync(self.state, stores=["memory"])
        self.assertEqual(sync["written"], 0)
        self.assertEqual(len(sync["excluded"]), 2)

    def test_status_and_parity_carry_counts_not_bodies(self):
        Memory(self.state).add("note", f"password {SECRET}", title="leaky")
        parity = self.brain.parity(self.state, stores=["memory"])
        self.assertEqual(parity["stores"]["memory"]["skipped_secret_like"], 1)
        self.assertNotIn(SECRET, json.dumps(parity))
        self.assertFalse(parity["in_sync"])    # a skipped record is never reported as parity


class Parity(Base):
    def test_typed_parity_missing_stale_vault_only_and_in_sync(self):
        mem = Memory(self.state)
        keep, gone = mem.add("note", "stays"), mem.add("note", "deleted later")
        self.assertTrue(self.brain.sync(self.state, stores=["memory"])["ok"])
        self.assertTrue(self.brain.parity(self.state, stores=["memory"])["in_sync"])
        mem.merge_meta(keep["id"], {"changed": 1})                  # stale in the vault
        new = mem.add("note", "never archived")                     # missing from the vault
        with mem.connect() as db:                                   # gone from the source
            db.execute("DELETE FROM items WHERE id=?", (gone["id"],))
        parity = self.brain.parity(self.state, stores=["memory"])
        store = parity["stores"]["memory"]
        self.assertEqual((store["missing"], store["stale"], parity["vault_only"]), (1, 1, 1))
        self.assertFalse(parity["in_sync"])
        # a missing source row is not a deletion: the archived note is untouched by a later sync
        self.brain.sync(self.state, stores=["memory"])
        self.assertIsNotNone(self.brain.latest(self.rid("memory-item", gone["id"])))
        self.assertIsNotNone(self.brain.latest(self.rid("memory-item", new["id"])))


class FileStoresAndCli(Base):
    """Plans/runs/knowledge have no trigger: they get a scoped sync that stays legal under capture."""

    def ns(self, action, **kw):
        base = {"action": action, "apply": False, "limit": 100, "target": None, "files": False,
                "rescan": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def cli(self, action, **kw):
        return rt.cli(self.ns(action, **kw), self.config(), self.state)

    def test_files_only_sync_runs_while_capture_is_active_and_db_sync_is_still_refused(self):
        (self.state / "plans").mkdir()
        (self.state / "plans" / "plan-0123abcd.json").write_text(json.dumps({"state": "done", "steps": []}))
        Memory(self.state).add("note", "db row")
        q.configure(self.state)
        refused, code = self.cli("sync", apply=True)
        self.assertEqual((code, refused["error"]), (1, "capture-active-use-drain"))
        out, code = self.cli("sync", apply=True, files=True)
        self.assertEqual(code, 0, out)
        self.assertEqual((out["scope"], out["written"], out["ok"]), ("files", 1, True))
        self.assertEqual([p for p in self.vault.rglob("*.md") if "/memory/" in str(p)], [])   # DB untouched
        again, _ = self.cli("sync", apply=True, files=True)
        self.assertEqual(again["written"], 0)

    def test_status_is_partial_until_file_stores_are_verified(self):
        (self.state / "plans").mkdir()
        (self.state / "plans" / "plan-0123abcd.json").write_text("{}")
        Memory(self.state).add("note", "row")
        q.configure(self.state)
        self.drain_all()
        self.assertEqual(rt.status(self.config(), self.state)["state"], "partial")
        self.cli("sync", apply=True, files=True)
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["state"], "current")
        self.assertIsNotNone(out["files"]["last_sync"])

    def test_periodic_file_sync_runs_inside_the_drain_cycle(self):
        (self.state / "plans").mkdir()
        (self.state / "plans" / "plan-0123abcd.json").write_text("{}")
        Memory(self.state).add("note", "row")
        seen = []
        d = rt.Drainer(self.config(), self.state, file_sync_s=3600, files=lambda st, root: seen.append(root))
        self.assertTrue(d.prime())
        d.cycle()
        d.cycle()
        self.assertEqual(len(seen), 1)               # first cycle only; the interval gates the rest

    def test_a_failing_file_sync_never_stops_the_drain(self):
        Memory(self.state).add("note", "row")

        def boom(st, root):
            raise RuntimeError("nope")
        d = rt.Drainer(self.config(), self.state, file_sync_s=1, files=boom)
        d.prime()
        d.cycle()
        self.assertEqual(d.info["files_error"], "RuntimeError")
        self.assertIsNotNone(d.info["last_success"])

    def test_verify_is_read_only_and_reports_typed_parity(self):
        Memory(self.state).add("note", "row")
        before = sorted(p.name for p in self.vault.rglob("*"))
        out, code = self.cli("verify")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertFalse(out["canonical"])
        self.assertEqual(out["parity"]["stores"]["memory"]["missing"], 1)
        self.assertEqual(sorted(p.name for p in self.vault.rglob("*")), before)
        self.brain.sync(self.state)
        out, code = self.cli("verify")
        self.assertEqual((code, out["ok"]), (0, True))

    def test_drain_rescan_flag_requeues_and_drains(self):
        Memory(self.state).add("note", "row")
        q.configure(self.state)
        self.drain_all()
        out, code = self.cli("drain", rescan=True)
        self.assertEqual(code, 0)
        self.assertEqual(out["remaining"] or 0, 0)

    def test_status_last_success_is_iso_utc(self):
        Memory(self.state).add("note", "row")
        q.configure(self.state)
        self.drain_all()
        out = rt.status(self.config(), self.state)
        self.assertRegex(out["last_success"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


class Durability(Base):
    def test_two_simultaneous_drains_one_wins_the_other_is_busy(self):
        Memory(self.state).add("note", "row")
        q.configure(self.state)
        from jarvis.locking import exclusive_lock
        with exclusive_lock(self.state / q.LOCK, timeout=0):
            out = self.drain()
        self.assertTrue(out["busy"])
        self.assertTrue(self.drain_all()["ok"])

    def test_missing_vault_keeps_everything_pending_and_nothing_current(self):
        Memory(self.state).add("note", "row")
        q.configure(self.state)
        gone = self.tmp / "unmounted"
        out = q.drain(self.state, gone)
        self.assertFalse(out["vault_ok"])
        self.assertEqual(q.status(self.state)["pending"] + q.status(self.state)["baseline_pending"] > 0, True)
        cfg = {"vault": {"enabled": True, "path": str(gone), "write": False}, "second_brain": {"enabled": True}}
        self.assertNotEqual(rt.status(cfg, self.state)["state"], "current")


if __name__ == "__main__":
    unittest.main()
