"""Archive overflow: records above the 16 MiB note bound (FINAL-ARCHIVE-REPAIRS.md).

A pre-existing legal record larger than MAX_RECORD_BYTES used to be held as the head of its record
forever, blocking every later (small) revision. With the owner's explicit overflow policy
(q.set_overflow) it is published losslessly as immutable chunk files plus a hash-checked manifest
note; without the policy it stays held and visible, never dropped. Temp fixtures only.
"""

import contextlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import second_brain as sb  # noqa: E402
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.second_brain import SecondBrain  # noqa: E402
from jarvis.store import Store  # noqa: E402

SECRET = "sk-" + "b" * 30
MIB = 1024 * 1024


def outbox(state, store="tasks"):
    db = sqlite3.connect(Path(state) / q.STORE_FILES[store])
    try:
        return db.execute("SELECT seq,state,held_reason,origin,bytes FROM archive_outbox ORDER BY seq").fetchall()
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

    def drain_all(self, rounds=20, **kw):
        rep = {}
        for _ in range(rounds):
            rep = q.drain(self.state, self.vault, limit=50, **kw)
            if not rep["remaining"] or rep["held"] or rep["failed"]:
                break
        return rep

    def notes(self, kind="task"):
        return sorted(p for p in self.vault.rglob("*.md") if p.name.startswith(f"jr-{kind}-"))

    def chunks(self):
        return sorted(self.vault.rglob("*.jrc"))

    def rid(self, task_id):
        return f"tasks/task/{task_id}"

    def config(self):
        return {"second_brain": {"enabled": True},
                "vault": {"path": str(self.vault), "allow_external": True}}


class Bound(Base):
    """Overflow mechanics with a shrunken bound (4 KiB note bound, 1 KiB chunks): fast."""

    def setUp(self):
        super().setUp()
        for target, name, value in ((q, "MAX_RECORD_BYTES", 4096), (sb, "CHUNK_BYTES", 1024)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.brain = SecondBrain(self.vault, max_record_bytes=4096)

    def huge_task(self, text=None):
        store = Store(self.state, prune=False)
        task = store.add({"objective": text or ("x" * 9000)})
        q.configure(self.state, True)
        return store, task

    def published(self):
        store, task = self.huge_task()
        q.set_overflow(self.state, 64 * 1024)
        self.drain_all()
        note = self.notes()[0]
        return task, note, self.brain.parse_note(note.read_bytes())


class Small(Bound):

    def test_policy_off_keeps_head_held_and_visible_never_dropped(self):
        store, task = self.huge_task()
        self.drain_all()
        self.assertEqual([r[1:3] for r in outbox(self.state)], [("held", "oversize")])
        with self.assertRaises(q.ArchiveCaptureError):   # new over-bound write: honest rollback
            store.transition(task["id"], "queued", "failed", {"why": "stop"})
        self.assertEqual(store.get(task["id"])["state"], "queued")
        st = q.status(self.state)
        self.assertEqual(st["overflow_max_bytes"], 0)
        self.assertGreater(st["held_oversize_bytes"], 4096)
        self.assertEqual(self.notes(), [])

    def test_policy_publishes_held_head_then_later_revisions_in_order(self):
        store, task = self.huge_task()
        self.drain_all()
        self.assertEqual(q.set_overflow(self.state, 64 * 1024)["stores"]["tasks"], True)
        store.transition(task["id"], "queued", "failed", {"why": "stop"})   # queued behind the held head
        self.assertEqual([r[1] for r in outbox(self.state)], ["held", "pending"])
        rep = self.drain_all()
        self.assertEqual((rep["held"], rep["failed"], rep["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(rep["released_oversize"], 1)
        self.assertEqual(rep["chunked"], 2)
        names = [p.name for p in self.notes()]
        self.assertEqual([int(re.search(r"-r(\d{4})-", n)[1]) for n in names], [1, 2])   # in order, none lost
        latest = self.brain.latest(self.rid(task["id"]))
        self.assertEqual(latest["fields"]["state"], "failed")
        r1 = self.brain._load(self.notes()[0])
        self.assertTrue(r1["chunked"] and r1["payload_ok"])
        self.assertIn("x" * 9000, json.dumps(r1["obj"]["fields"]["spec"]))   # original kept, whole
        self.assertEqual(r1["obj"]["fields"]["state"], "queued")
        self.assertTrue(self.brain.validate()["ok"])
        self.assertEqual(outbox(self.state), [])
        # a raw rewrite of the (immutable) spec is a different record, not a "shrink": it is held
        # visibly as an identity collision and the archived huge original is never overwritten
        db = sqlite3.connect(self.state / "tasks.sqlite3")
        db.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps({"objective": "small"}), task["id"]))
        db.commit()
        db.close()
        q.rescan(self.state)
        self.drain_all()
        self.assertEqual([r[1:3] for r in outbox(self.state)], [("held", "identity-collision")])
        self.assertEqual(len(self.notes()), 2)

    def test_secret_in_huge_payload_is_held_without_leaking(self):
        store, task = self.huge_task("y" * 9000 + f" token {SECRET}")
        q.set_overflow(self.state, 64 * 1024)
        rep = self.drain_all()
        self.assertEqual(rep["held"], 1)
        self.assertEqual(q.status(self.state)["held_reasons"], {"secret-like": 1})
        self.assertEqual((self.notes(), self.chunks()), ([], []))
        self.assertNotIn(SECRET, json.dumps(rep) + json.dumps(rt.status(self.config(), self.state)))
        rep = self.drain_all(allow_secret_like=[self.rid(task["id"])])   # owner's explicit allow
        self.assertEqual((rep["held"], rep["remaining"]), (0, 0))
        self.assertIn(SECRET, json.dumps(self.brain.latest(self.rid(task["id"]))["fields"]["spec"]))

    def test_crash_after_some_chunks_is_not_acked_and_retry_reuses_them(self):
        store, task = self.huge_task()
        q.set_overflow(self.state, 64 * 1024)
        real, calls = SecondBrain._write_new_locked, []

        def crash(brain, parts, name, data):
            if name.endswith(".jrc"):
                if calls:
                    raise OSError("simulated crash mid-publication")
                calls.append(name)
            return real(brain, parts, name, data)
        with patch.object(SecondBrain, "_write_new_locked", crash):
            rep = q.drain(self.state, self.vault, limit=50)
        self.assertEqual((rep["acked"], rep["failed"]), (0, 1))
        self.assertEqual(len(self.chunks()), 1)           # partial chunk set on disk...
        self.assertEqual(self.notes(), [])                # ...but no note: nothing claims it
        self.assertEqual([r[1] for r in outbox(self.state)], ["pending"])
        rep = self.drain_all()
        self.assertEqual((rep["acked"], rep["remaining"]), (1, 0))
        self.assertEqual(len(self.notes()), 1)
        self.assertTrue(self.brain.validate()["ok"])

    def test_foreign_bytes_at_a_chunk_name_fail_closed(self):
        store, task = self.huge_task()
        q.set_overflow(self.state, 64 * 1024)
        def no_note(brain, parts, name, data):   # every chunk lands, the note never does
            if name.endswith(".md") and name.startswith("jr-task-"):
                raise OSError("simulated crash before the note")
            return _real_write(brain, parts, name, data)
        with patch.object(SecondBrain, "_write_new_locked", no_note):
            q.drain(self.state, self.vault, limit=50)
        first = self.chunks()[0]
        os.chmod(first, 0o600)
        first.write_bytes(b"z" * first.stat().st_size)   # same size, other bytes
        rep = q.drain(self.state, self.vault, limit=50)
        self.assertEqual((rep["acked"], rep["failed"]), (0, 1))
        self.assertTrue(any("chunk-corrupt" in s for s in rep["error_samples"]))
        self.assertEqual(self.notes(), [])
        self.assertEqual([r[1] for r in outbox(self.state)], ["pending"])

    def rewrite_manifest(self, note, mutate):
        """Forge a manifest WITH consistent note hashes, so only chunk validation can catch it."""
        text = note.read_text()
        parsed = SecondBrain.parse_note(note.read_bytes())
        man = mutate(parsed["manifest"])
        old_line, new_line = sb._canonical(parsed["manifest"]), sb._canonical(man)
        text = text.replace(old_line, new_line).replace(sb._sha(old_line), sb._sha(new_line))
        os.chmod(note, 0o600)
        note.write_text(text)

    def test_truncated_chunk_breaks_read_validate_restore_then_repairs(self):
        task, note, _ = self.published()
        chunk = self.chunks()[-1]
        os.chmod(chunk, 0o600)
        chunk.write_bytes(chunk.read_bytes()[:-5])
        with self.assertRaises(ValueError):
            self.brain.latest(self.rid(task["id"]))
        problems = [e["problem"] for e in self.brain.validate()["errors"]]
        self.assertTrue(any("missing or truncated" in p for p in problems), problems)
        rest = self.brain.restore(self.tmp / "r1", strict=False)
        self.assertTrue(rest["errors"])
        self.assertFalse(self.brain.parity(self.state)["in_sync"])   # damage is visible read-only
        # explicit repair (capture paused, or an owner-run sync): needs the same overflow policy
        q.configure(self.state, False)
        off = SecondBrain(self.vault, max_record_bytes=4096).sync(self.state, stores=("tasks",))
        self.assertTrue(any("oversize" in str(x) for x in off["skipped"]), off["skipped"])   # honest, no write
        fix = SecondBrain(self.vault, max_record_bytes=4096, max_overflow_bytes=64 * 1024).sync(
            self.state, stores=("tasks",))
        self.assertEqual((fix["written"], fix["failures"]), (1, []))
        self.assertEqual(len(self.notes()), 2)            # repair is a NEW revision; r1 untouched
        self.assertIn("x" * 9000, json.dumps(self.brain.latest(self.rid(task["id"]))["fields"]["spec"]))

    def test_duplicate_reordered_foreign_and_unhashed_manifests_are_rejected(self):
        task, note, parsed = self.published()
        cases = {
            "duplicate": lambda m: {**m, "chunks": [m["chunks"][0]] * len(m["chunks"])},
            "reordered": lambda m: {**m, "chunks": list(reversed(m["chunks"]))},
            "foreign revision": lambda m: {**m, "chunks": [{**c, "name": c["name"].replace("-r0001-", "-r0002-")}
                                                           for c in m["chunks"]]},
            "too big": lambda m: {**m, "bytes": sb.MAX_OVERFLOW_BYTES + 1},
        }
        original = note.read_bytes()
        for label, mutate in cases.items():
            with self.subTest(label):
                os.chmod(note, 0o600)
                note.write_bytes(original)
                self.rewrite_manifest(note, mutate)
                with self.assertRaisesRegex(ValueError, "chunked-payload"):
                    self.brain._load(note)
        os.chmod(note, 0o600)
        note.write_bytes(original.replace(b'"format":"jarvis-record-chunks/1"', b'"format":"jarvis-record-chunks/9"'))
        with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):   # edited without fixing hashes
            self.brain._load(note)

    def test_restore_from_vault_only_and_old_notes_coexist(self):
        store = Store(self.state, prune=False)
        old = store.add({"objective": "old small task"})
        q.configure(self.state, True)
        self.drain_all()                                   # old-format (plain) note first
        plain = self.notes()[0]
        self.assertFalse(SecondBrain.parse_note(plain.read_bytes())["chunked"])
        q.set_overflow(self.state, 64 * 1024)
        huge = store.add({"objective": "q" * 9000})
        self.drain_all()
        rep = self.brain.restore(self.tmp / "rebuilt")   # vault only: the state dir is not read
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["restored"].get("tasks"), 2)
        rebuilt = Store(self.tmp / "rebuilt", prune=False)
        self.assertEqual(rebuilt.get(huge["id"])["spec"]["objective"], "q" * 9000)
        self.assertEqual(rebuilt.get(old["id"])["spec"]["objective"], "old small task")

    def test_parity_and_runtime_status_are_numeric_and_read_only(self):
        task, note, _ = self.published()
        before = sorted(p.name for p in self.vault.rglob("*"))
        par = self.brain.parity(self.state)
        self.assertTrue(par["in_sync"], par)
        out = rt.status(self.config(), self.state)
        self.assertIsInstance(out["archive_overflow_max_bytes"], int)
        self.assertEqual(out["archive_overflow_max_bytes"], 64 * 1024)
        self.assertEqual(out["archive_held_oversize_bytes"], 0)
        self.assertNotIn("xxxx", json.dumps(out))
        self.assertEqual(sorted(p.name for p in self.vault.rglob("*")), before)

    def test_set_overflow_bounds_and_cli(self):
        Store(self.state, prune=False)
        q.configure(self.state, True)
        for bad in (100, sb.MAX_OVERFLOW_BYTES + 1, "nope"):
            with self.assertRaises(ValueError):
                q.set_overflow(self.state, bad)
        args = type("A", (), {"action": "overflow", "mib": None})()
        out, code = rt.cli(args, self.config(), self.state)
        self.assertEqual((code, out["overflow_max_bytes"]), (0, 0))
        args.mib = 1
        out, code = rt.cli(args, self.config(), self.state)
        self.assertEqual((code, out["overflow_max_bytes"]), (0, MIB))
        args.mib = 999
        self.assertEqual(rt.cli(args, self.config(), self.state)[0]["error"], "overflow-limit-out-of-range")
        self.assertEqual(q.set_overflow(self.state, 0)["overflow_max_bytes"], 0)


_real_write = SecondBrain._write_new_locked


class Recovery(Bound):
    """A damaged chunk must not read as current, and rescan/drain repairs by appending a revision.
    Temp vault only. The shrunken bound keeps truncate and crash fast; RealSize covers production sizes."""

    def _damage(self, chunk, mode):
        os.chmod(chunk, 0o600)
        raw = chunk.read_bytes()
        if mode == "flip":
            chunk.write_bytes(bytes([raw[0] ^ 0xFF]) + raw[1:])
        else:
            chunk.write_bytes(raw[:-5])
        return chunk.read_bytes()

    def test_corrupt_chunk_is_not_current_and_rescan_appends_a_revision(self):
        task, note, _ = self.published()
        original = note.read_bytes()
        chunk = self.chunks()[0]
        damaged = self._damage(chunk, "flip")
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["gate"], "ok")
        self.assertEqual(out["state"], "error")
        self.assertGreaterEqual(out["archive_chunk_damaged"], 1)
        self.assertNotIn("xxxx", json.dumps(out))
        q.rescan(self.state)
        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(rep["repaired"], 1)
        self.assertEqual(chunk.read_bytes(), damaged)          # immutable: the bad bytes stay
        self.assertEqual(note.read_bytes(), original)          # the old note stays too
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("x" * 9000, json.dumps(self.brain.latest(self.rid(task["id"]))["fields"]["spec"]))
        self.assertEqual(outbox(self.state), [])               # acked only after the new note read back
        again = rt.status(self.config(), self.state)
        self.assertEqual(again["state"], "current")
        self.assertEqual(again["archive_chunk_damaged"], 0)
        self.assertFalse(self.brain.validate()["ok"])          # the damaged revision is still evidence

    def test_truncated_chunk_repairs_without_growing_the_old_file(self):
        task, _, _ = self.published()
        chunk = self.chunks()[-1]
        damaged = self._damage(chunk, "truncate")
        self.assertNotEqual(rt.status(self.config(), self.state)["state"], "current")
        q.rescan(self.state)
        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["remaining"]), (0, 0))
        self.assertEqual(chunk.read_bytes(), damaged)
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("x" * 9000, json.dumps(self.brain.latest(self.rid(task["id"]))["fields"]["spec"]))

    def test_crash_before_the_note_is_not_acked_and_retry_reuses_chunks(self):
        store, task = self.huge_task()
        q.set_overflow(self.state, 64 * 1024)

        def crash(brain, parts, name, data):
            if name.endswith(".jrc") and crash.n:
                raise OSError("simulated crash mid-publication")
            if name.endswith(".jrc"):
                crash.n += 1
            return _real_write(brain, parts, name, data)
        crash.n = 0
        with patch.object(SecondBrain, "_write_new_locked", crash):
            rep = q.drain(self.state, self.vault, limit=50)
        self.assertEqual((rep["acked"], rep["failed"]), (0, 1))
        self.assertEqual(self.notes(), [])
        self.assertEqual([row[1] for row in outbox(self.state)], ["pending"])
        self.assertNotEqual(rt.status(self.config(), self.state)["state"], "current")
        names = {p.name for p in self.chunks()}
        rep = self.drain_all()
        self.assertEqual((rep["acked"], rep["remaining"]), (1, 0))
        self.assertTrue(names <= {p.name for p in self.chunks()})  # the partial chunk was reused, not replaced
        self.assertTrue(self.brain.validate()["ok"])
        self.assertEqual(rt.status(self.config(), self.state)["state"], "current")
        self.assertIn("x" * 9000, json.dumps(self.brain.latest(self.rid(task["id"]))["fields"]["spec"]))

    def test_recreated_ledger_keeps_the_owner_overflow_policy(self):
        Store(self.state, prune=False)
        q.configure(self.state, True)
        self.assertEqual(q.set_overflow(self.state, 64 * 1024)["stores"]["history"], True)
        (self.state / "history.sqlite3").unlink()
        from jarvis.history import History
        History(self.state)
        policy = q.overflow_policy(self.state)
        self.assertEqual(policy["history"], 64 * 1024)
        self.assertEqual(policy["memory"], 64 * 1024)
        self.assertEqual(q.status(self.state)["overflow_max_bytes"], 64 * 1024)

    def _revs(self):
        return [int(re.search(r"-r(\d{4,})-", path.name)[1]) for path in self.notes()]

    def test_gap_and_corrupt_head_is_not_current_and_rescan_does_not_fill_the_gap(self):
        """A missing earlier revision must not hide a damaged head, and the repair must
        not invent that revision. Temp vault, shrunken bound, same shape as the audit."""
        store, task = self.huge_task()
        q.set_overflow(self.state, 64 * 1024)
        self.drain_all()
        store.transition(task["id"], "queued", "failed", {"why": "stop"})
        self.drain_all()
        self.assertEqual(self._revs(), [1, 2])
        rev1 = next(path for path in self.notes() if "-r0001-" in path.name)
        rev2 = next(path for path in self.notes() if "-r0002-" in path.name)
        original = rev2.read_bytes()
        rev1.unlink()
        chunk = next(path for path in self.chunks() if "-r0002-" in path.name)
        damaged = self._damage(chunk, "flip")
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["gate"], "ok")
        self.assertEqual(out["state"], "error")
        self.assertNotEqual(out["state"], "current")
        self.assertGreaterEqual(out["archive_chunk_checked"], 1)
        self.assertGreaterEqual(out["archive_chunk_damaged"], 1)
        self.assertGreaterEqual(out["archive_revision_conflict"], 1)
        self.assertNotIn("xxxx", json.dumps(out))
        q.rescan(self.state)
        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(rep["repaired"], 1)
        self.assertEqual(self._revs(), [2, 3])          # rev 1 was not synthesized
        self.assertFalse(any("-r0001-" in path.name for path in self.notes()))
        self.assertEqual(chunk.read_bytes(), damaged)
        self.assertEqual(rev2.read_bytes(), original)
        newest = max(self.notes(), key=lambda path: path.name)
        parsed = self.brain._load(newest)
        self.assertTrue(parsed["payload_ok"])
        self.assertIn("x" * 9000, parsed["payload_text"])
        with self.assertRaises(sb.SecondBrainError):
            self.brain.latest(self.rid(task["id"]))     # the gap is still a gap
        problems = [item["problem"] for item in self.brain.validate()["errors"]]
        self.assertTrue(any("revision-gap-or-duplicate" in item for item in problems), problems)
        self.assertTrue(any("chunked-payload" in item for item in problems), problems)
        again = rt.status(self.config(), self.state)
        self.assertEqual(again["state"], "conflict")
        self.assertNotEqual(again["state"], "current")
        self.assertEqual(again["archive_chunk_damaged"], 0)   # the new head reads back on its own
        self.assertGreaterEqual(again["archive_revision_conflict"], 1)
        with contextlib.closing(sqlite3.connect(self.state / "tasks.sqlite3")) as db:
            head_sha = db.execute("SELECT payload_sha FROM archive_head").fetchone()[0]
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], head_sha))
        q.rescan(self.state)
        second = self.drain_all()
        self.assertEqual((second["repaired"], second["failed"], second["remaining"]), (0, 0, 0))
        self.assertEqual(self._revs(), [2, 3])          # a sound head does not grow another revision

    def test_oversize_reason_is_counted_as_oversize(self):
        view = rt._sync_view({"ok": True, "written": 1, "unchanged": 0, "repaired": ["tasks/task/1"],
                              "skipped": [{"reason": "oversize: 99 bytes"}, {"reason": "secret-like"}],
                              "stores": {}})
        self.assertEqual(view["skipped"], {"oversize": 1, "secret-like": 1})
        self.assertEqual(view["repaired"], 1)


class Supersede(Bound):
    """A held oversize head must not block a later revision that fits. The old bytes stay."""

    def _spec(self, task_id, spec):
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        try:
            conn.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), task_id))
            conn.commit()
        finally:
            conn.close()

    def _rows(self):
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        try:
            return conn.execute(
                "SELECT state, held_reason, payload, bytes FROM archive_outbox ORDER BY seq").fetchall()
        finally:
            conn.close()

    def test_shrink_then_transition_publishes_and_keeps_the_oversize_bytes(self):
        store = Store(self.state, prune=False)
        huge = "H" * 9000
        task = store.add({"objective": huge})
        q.configure(self.state, True)
        self.drain_all()
        self.assertEqual([row[1:3] for row in outbox(self.state)], [("held", "oversize")])
        self.assertEqual(self.notes(), [])
        with self.assertRaises(q.ArchiveCaptureError):
            store.transition(task["id"], "queued", "failed", {"why": "still huge"})
        self.assertEqual(store.get(task["id"])["state"], "queued")
        self._spec(task["id"], {"objective": "small now"})
        before = [row[:2] for row in self._rows()]
        dry = q.drain(self.state, self.vault, dry_run=True)
        self.assertEqual(dry["acked"], 0)
        self.assertEqual(dry["superseded"], 0)
        self.assertEqual(self.notes(), [])
        self.assertEqual([row[:2] for row in self._rows()], before)   # dry-run does not supersede
        first = self.drain_all()
        self.assertGreaterEqual(first["superseded"], 1)
        self.assertEqual(len(self.notes()), 1)
        store.transition(task["id"], "queued", "failed", {"why": "stop"})
        note_names = sorted(path.name for path in self.notes())
        queued = [row[:2] for row in self._rows()]
        dry = q.drain(self.state, self.vault, dry_run=True)
        self.assertEqual(dry["acked"], 0)
        self.assertEqual(sorted(path.name for path in self.notes()), note_names)
        self.assertEqual([row[:2] for row in self._rows()], queued)
        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0))
        self.assertEqual(len(self.notes()), 2)
        kept = [row for row in self._rows() if row[0] == "superseded"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][1], "superseded-oversize")
        self.assertIn(huge, kept[0][2])
        self.assertGreater(kept[0][3], 4096)
        st = q.status(self.state)
        self.assertEqual((st["pending"], st["held"]), (0, 0))
        self.assertGreaterEqual(st["superseded"], 1)
        self.assertNotIn("oversize", st["held_reasons"])
        out = rt.status(self.config(), self.state)
        self.assertEqual(out["state"], "current")
        self.assertEqual(out["archive_held"], 0)
        self.assertGreaterEqual(out["archive_superseded"], 1)
        self.assertFalse(out["canonical"])   # opt-in is off
        latest = self.brain.latest(self.rid(task["id"]))
        self.assertEqual(latest["fields"]["state"], "failed")
        self.assertIn("small now", latest["fields"]["spec"])
        self.assertNotIn(huge, json.dumps(latest))
        q.rescan(self.state)
        again = self.drain_all()
        self.assertEqual((again["repaired"], again["remaining"], len(self.notes())), (0, 0, 2))

    def test_secret_and_conflict_holds_are_not_superseded(self):
        store = Store(self.state, prune=False)
        task = store.add({"objective": "H" * 9000})
        q.configure(self.state, True)
        self.drain_all()
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        conn.execute("UPDATE archive_outbox SET held_reason='secret-like'")
        conn.commit()
        conn.close()
        self._spec(task["id"], {"objective": "small now"})
        rep = self.drain_all()
        self.assertEqual(rep["superseded"], 0)
        self.assertEqual(self.notes(), [])
        self.assertEqual([row[1:3] for row in outbox(self.state) if row[1] == "held"],
                         [("held", "secret-like")])
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        conn.execute("UPDATE archive_outbox SET held_reason='conflict' WHERE state='held'")
        conn.commit()
        conn.close()
        rep = self.drain_all()
        self.assertEqual(rep["superseded"], 0)
        self.assertEqual(q.status(self.state)["held_reasons"], {"conflict": 1})
        self.assertEqual(self.notes(), [])


class RealSize(Base):
    """The audit case at real size: a 20 MiB pre-existing task, default 16 MiB bound, 4 MiB chunks."""

    def test_twenty_mib_baseline_task_is_published_and_restored_losslessly(self):
        store = Store(self.state, prune=False)
        task = store.add({"objective": "z" * (20 * MIB)})
        q.configure(self.state, True)
        self.drain_all()
        self.assertEqual([r[1:3] for r in outbox(self.state)], [("held", "oversize")])   # policy off: held
        q.set_overflow(self.state, 32 * MIB)
        store.transition(task["id"], "queued", "failed", {"why": "stop"})   # transition while huge
        rep = self.drain_all()
        self.assertEqual((rep["held"], rep["failed"], rep["remaining"]), (0, 0, 0))
        self.assertEqual(len(self.notes()), 2)
        self.assertEqual(len(self.chunks()), 2 * 6)                      # 20 MiB + envelope -> 6 x 4 MiB
        self.assertTrue(all(p.stat().st_size <= sb.CHUNK_BYTES for p in self.chunks()))
        self.assertTrue(all(p.stat().st_size < 64 * 1024 for p in self.notes()))   # notes stay small
        rest = self.brain.restore(self.tmp / "rebuilt")
        self.assertEqual(rest["errors"], [])
        got = Store(self.tmp / "rebuilt", prune=False).get(task["id"])
        self.assertEqual((len(got["spec"]["objective"]), got["state"]), (20 * MIB, "failed"))

    def test_fullsize_corrupt_chunk_is_not_current_and_rescan_repairs(self):
        """Audit finding at the real 16 MiB bound: one flipped chunk byte. Temp vault only."""
        store = Store(self.state, prune=False)
        task = store.add({"objective": "x" * (16 * MIB + 4096)})
        q.configure(self.state, True)
        q.set_overflow(self.state, 32 * MIB)
        rep = self.drain_all()
        self.assertEqual((rep["published"], rep["chunked"], rep["remaining"]), (1, 1, 0))
        self.assertGreaterEqual(len(self.chunks()), 5)
        chunk = self.chunks()[0]
        note = self.notes()[0]
        os.chmod(chunk, 0o600)
        raw = bytearray(chunk.read_bytes())
        raw[0] ^= 0xFF
        damaged = bytes(raw)
        chunk.write_bytes(damaged)
        original_note = note.read_bytes()
        out = rt.status(self.config(), self.state)
        self.assertEqual((out["gate"], out["state"]), ("ok", "error"))
        self.assertGreaterEqual(out["archive_chunk_damaged"], 1)
        self.assertEqual(out["archive_overflow_max_bytes"], 32 * MIB)
        args = type("A", (), {"action": "sync", "apply": False, "files": False, "rescan": False,
                              "limit": 100, "target": None, "mib": None})()
        view, code = rt.cli(args, self.config(), self.state)
        self.assertEqual(view["failures"], 0)
        self.assertGreaterEqual(view["repaired"], 1)
        self.assertGreaterEqual(view["written"], 1)
        self.assertEqual(view["skipped"].get("oversize", 0), 0)
        self.assertEqual(code, 0)
        args.apply = True
        refused, code = rt.cli(args, self.config(), self.state)
        self.assertEqual((code, refused["error"]), (1, "capture-active-use-drain"))
        q.rescan(self.state)
        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(rep["repaired"], 1)
        self.assertEqual(chunk.read_bytes(), damaged)
        self.assertEqual(note.read_bytes(), original_note)
        self.assertEqual(len(self.notes()), 2)
        self.assertEqual(outbox(self.state), [])
        latest = self.brain.latest(self.rid(task["id"]))
        spec = latest["fields"]["spec"]
        spec = json.loads(spec) if isinstance(spec, str) else spec
        self.assertEqual(len(spec["objective"]), 16 * MIB + 4096)
        again = rt.status(self.config(), self.state)
        self.assertEqual((again["state"], again["archive_chunk_damaged"]), ("current", 0))
        self.assertFalse(self.brain.validate()["ok"])


if __name__ == "__main__":
    unittest.main()
