"""WS3 archive integrity regressions.

Temp vault and temp state only. Real task and memory stores, real queue drain, and
real notes. A healthy verdict is never mocked. Production code is not under this file.
"""

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain as sb  # noqa: E402
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import SecondBrain, SecondBrainError  # noqa: E402
from jarvis.store import Store  # noqa: E402
from jarvis.vault import Vault, recall  # noqa: E402

CANARY = "ws3privacy-canary-phrase-zzzz"
HUGE = "H" * 9000
OTHER = "other-key-stable-payload"


class TempArchive(unittest.TestCase):
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

    def vault_files(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                for p in sorted(self.vault.rglob("*")) if p.is_file()}

    def outbox(self):
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        try:
            return conn.execute(
                "SELECT seq, key, state, held_reason, payload, payload_sha, bytes "
                "FROM archive_outbox ORDER BY seq").fetchall()
        finally:
            conn.close()

    def head_sha(self, key, db="tasks.sqlite3"):
        conn = sqlite3.connect(self.state / db)
        try:
            row = conn.execute(
                "SELECT payload_sha FROM archive_head WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, key)
        return row[0]


class SmallBound(TempArchive):
    """4 KiB note bound and 1 KiB chunks, so a short task still publishes as .jrc files."""

    def setUp(self):
        super().setUp()
        for target, name, value in ((q, "MAX_RECORD_BYTES", 4096), (sb, "CHUNK_BYTES", 1024)):
            active = patch.object(target, name, value)
            active.start()
            self.addCleanup(active.stop)
        self.brain = SecondBrain(self.vault, max_record_bytes=4096)

    def config(self):
        return {"second_brain": {"enabled": True},
                "vault": {"path": str(self.vault), "allow_external": True}}

    def revs(self):
        return [int(re.search(r"-r(\d{4,})-", path.name)[1]) for path in self.notes()]


class GapAndChunkDamage(SmallBound):
    def test_removed_revision_and_flipped_chunk_are_both_reported(self):
        """Two chunked revisions, then a missing rev 1 and a flipped rev 2 chunk.

        Status is not current and counts both the gap and the damage. The head is not
        sound and canonical_records refuses it. Rescan and drain may append the next
        revision; they must leave the bad bytes in place and must not claim current
        while the gap remains.
        """
        store = Store(self.state, prune=False)
        task = store.add({"objective": "x" * 9000})
        q.configure(self.state, True)
        q.set_overflow(self.state, 64 * 1024)
        first = self.drain_all()
        self.assertEqual((first["failed"], first["held"], first["remaining"]), (0, 0, 0))
        store.transition(task["id"], "queued", "failed", {"why": "stop"})
        second = self.drain_all()
        self.assertEqual((second["failed"], second["held"], second["remaining"]), (0, 0, 0))
        self.assertEqual(self.revs(), [1, 2])
        self.assertTrue(all(b"jarvis_payload_chunked: true" in path.read_bytes() for path in self.notes()))
        self.assertTrue(any("-r0001-" in path.name for path in self.chunks()))
        self.assertTrue(any("-r0002-" in path.name for path in self.chunks()))

        rev1 = next(path for path in self.notes() if "-r0001-" in path.name)
        rev2 = next(path for path in self.notes() if "-r0002-" in path.name)
        original_note = rev2.read_bytes()
        rev1.unlink()
        chunk = next(path for path in self.chunks() if "-r0002-" in path.name)
        os.chmod(chunk, 0o600)
        raw = chunk.read_bytes()
        damaged = bytes([raw[0] ^ 0xFF]) + raw[1:]
        chunk.write_bytes(damaged)
        sha = self.head_sha(task["id"])

        out = rt.status(self.config(), self.state)
        self.assertEqual(out["gate"], "ok")
        self.assertNotEqual(out["state"], "current")
        self.assertEqual(out["state"], "error")
        self.assertGreaterEqual(out["archive_chunk_checked"], 1)
        self.assertGreaterEqual(out["archive_revision_conflict"], 1)
        self.assertGreaterEqual(out["archive_chunk_damaged"], 1)
        self.assertNotIn("xxxx", json.dumps(out))
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], sha))
        with self.assertRaises(SecondBrainError) as raised:
            self.brain.canonical_records(store="tasks", kind="task")
        self.assertIn("revision-gap", str(raised.exception))

        q.rescan(self.state)
        repaired = self.drain_all()
        self.assertEqual((repaired["failed"], repaired["held"], repaired["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(repaired["repaired"], 1)
        self.assertEqual(self.revs(), [2, 3])
        self.assertFalse(any("-r0001-" in path.name for path in self.notes()))
        self.assertEqual(chunk.read_bytes(), damaged)
        self.assertEqual(rev2.read_bytes(), original_note)
        again = rt.status(self.config(), self.state)
        self.assertNotEqual(again["state"], "current")
        self.assertEqual(again["state"], "conflict")
        self.assertGreaterEqual(again["archive_revision_conflict"], 1)
        self.assertEqual(again["archive_chunk_damaged"], 0)
        self.assertFalse(self.brain.publication_sound("tasks", "task", task["id"], self.head_sha(task["id"])))
        with self.assertRaises(SecondBrainError) as still:
            self.brain.canonical_records(store="tasks", kind="task")
        self.assertIn("revision-gap", str(still.exception))
        problems = [item["problem"] for item in self.brain.validate()["errors"]]
        self.assertTrue(any("revision-gap-or-duplicate" in item for item in problems), problems)
        self.assertTrue(any("chunked-payload" in item for item in problems), problems)

        q.rescan(self.state)
        follow = self.drain_all()
        self.assertEqual((follow["repaired"], follow["failed"], follow["remaining"]), (0, 0, 0))
        self.assertEqual(self.revs(), [2, 3])
        self.assertEqual(chunk.read_bytes(), damaged)
        final = rt.status(self.config(), self.state)
        self.assertNotEqual(final["state"], "current")
        self.assertGreaterEqual(final["archive_revision_conflict"], 1)


class CanonicalRecallGates(TempArchive):
    def _cfg(self, enabled, recall_flag):
        return {"second_brain": {"enabled": True, "canonical_read": True},
                "vault": {"path": str(self.vault), "enabled": enabled, "recall": recall_flag,
                          "allow_external": True}}

    def test_canonical_recall_obeys_vault_enabled_and_vault_recall(self):
        """vault.enabled and vault.recall both stay off-switches when canonical_read is true.

        The open gate is the control: the same archive does serve the canary. Each closed
        gate must return no lines and must not fall through to ordinary note search.
        """
        q.configure(self.state, True)
        item = Memory(self.state).add("note", CANARY, title="Privacy gate")
        published = self.drain_all()
        self.assertEqual((published["failed"], published["held"], published["remaining"]), (0, 0, 0))
        rid = f"memory/memory-item/{item['id']}"
        open_cfg = self._cfg(True, True)
        healthy = rt.status(open_cfg, self.state)
        self.assertEqual(healthy["state"], "current")
        self.assertTrue(healthy["canonical"])
        with patch.object(Vault, "context", side_effect=AssertionError("legacy recall")):
            served = recall(open_cfg, self.state, CANARY)
        self.assertTrue(any(line.startswith(f"[vault:archive:{rid}]") and CANARY in line
                            for line in served), served)

        for enabled, recall_flag, gate in ((False, True, "vault.enabled"),
                                           (True, False, "vault.recall"),
                                           (False, False, "both")):
            with self.subTest(gate=gate):
                # A raised context is swallowed by recall() and would look like an empty
                # result. A sentinel shows a fallthrough to ordinary note search.
                with patch.object(Vault, "context", return_value=["LEGACY-LEAK"]):
                    leaked = recall(self._cfg(enabled, recall_flag), self.state, CANARY)
                self.assertEqual(leaked, [], f"{gate} still served lines")


class SupersededHeldHead(SmallBound):
    def _spec(self, task_id, spec):
        conn = sqlite3.connect(self.state / "tasks.sqlite3")
        try:
            conn.execute("UPDATE tasks SET spec=? WHERE id=?", (json.dumps(spec), task_id))
            conn.commit()
        finally:
            conn.close()

    def test_superseded_held_head_keeps_payload_bytes_sha_and_other_key(self):
        """A later fitting revision supersedes a held oversize head without rewriting it.

        The original payload bytes and sha stay on that row. A second key is not rewritten.
        A dry-run changes neither the outbox nor the vault.
        """
        store = Store(self.state, prune=False)
        held = store.add({"objective": HUGE})
        other = store.add({"objective": OTHER})
        q.configure(self.state, True)
        self.drain_all()
        # Backfill inserts an oversize row as held, so the drain report's held counter
        # stays 0. The outbox row is the proof. The other key is published in that drain.
        self.assertEqual([row[1:4] for row in self.outbox()], [(held["id"], "held", "oversize")])
        original = self.outbox()[0]
        original_bytes = original[4].encode("utf-8")
        original_sha = original[5]
        self.assertEqual(sb._sha(original_bytes), original_sha)
        self.assertIn(HUGE, original[4])
        self.assertGreater(original[6], 4096)
        other_sha = self.head_sha(other["id"])
        other_notes = [path for path in self.notes() if other["id"] in path.read_text(encoding="utf-8")]
        self.assertEqual(len(other_notes), 1)
        other_note = other_notes[0].read_bytes()
        self.assertIn(OTHER.encode("utf-8"), other_note)
        self.assertNotIn(held["id"].encode("utf-8"), other_note)

        self._spec(held["id"], {"objective": "small now"})
        store.transition(held["id"], "queued", "failed", {"why": "fits"})
        queued = self.outbox()
        self.assertEqual([(row[1], row[2], row[3]) for row in queued],
                         [(held["id"], "held", "oversize"), (held["id"], "pending", None)])
        self.assertEqual(queued[0][4].encode("utf-8"), original_bytes)
        self.assertEqual(queued[0][5], original_sha)
        before_rows = self.outbox()
        before_files = self.vault_files()

        dry = q.drain(self.state, self.vault, dry_run=True)
        self.assertEqual(dry["acked"], 0)
        self.assertEqual(dry["superseded"], 0)
        self.assertEqual(self.outbox(), before_rows)
        self.assertEqual(self.vault_files(), before_files)
        self.assertEqual(self.head_sha(other["id"]), other_sha)

        rep = self.drain_all()
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0))
        self.assertGreaterEqual(rep["superseded"], 1)
        kept = [row for row in self.outbox() if row[2] == "superseded"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][1], held["id"])
        self.assertEqual(kept[0][3], "superseded-oversize")
        self.assertEqual(kept[0][4].encode("utf-8"), original_bytes)
        self.assertEqual(kept[0][5], original_sha)
        self.assertEqual(sb._sha(kept[0][4]), original_sha)
        self.assertEqual(kept[0][6], original[6])
        self.assertNotIn(OTHER, kept[0][4])
        self.assertEqual(self.head_sha(other["id"]), other_sha)
        self.assertEqual(other_notes[0].read_bytes(), other_note)
        for path, data in before_files.items():
            self.assertEqual((self.vault / path).read_bytes(), data, path)
        latest = self.brain.latest(f"tasks/task/{held['id']}")
        self.assertIn("small now", latest["fields"]["spec"])
        self.assertNotIn(HUGE, json.dumps(latest))
        later = [path for path in self.notes() if other["id"] in path.read_text(encoding="utf-8")]
        self.assertEqual(later, other_notes)


if __name__ == "__main__":
    unittest.main()
