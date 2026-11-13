"""Archive status must not re-open an unchanged published note.

Temp vault and temp state only. The note cache and the shard-directory cache are
in-memory. A same-size edit with a new mtime_ns is read again. A deleted note
leaves the intact-head count and is dropped from the cache.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain as sb  # noqa: E402
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis import second_brain_runtime as rt  # noqa: E402
from jarvis.memory import Memory  # noqa: E402


def _archive_opens(paths):
    """Note and chunk files opened through the archive reader."""
    out = []
    for path in paths:
        name = os.path.basename(path)
        if name.startswith(("jr-", "jrc-")):
            out.append(path)
    return out


class StatusReadCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(sb._clear_archive_read_cache)
        self.tmp = Path(tmp.name)
        self.state, self.vault = self.tmp / "state", self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        q.configure(self.state, True)
        mem = Memory(self.state)
        self.bodies = [f"cache-body-{i}-ALPHA" for i in range(3)]
        for i, body in enumerate(self.bodies):
            mem.add("note", body, title=f"Cache {i}")
        rep = {}
        for _ in range(20):
            rep = q.drain(self.state, self.vault, limit=50)
            if not rep["remaining"] or rep["held"] or rep["failed"]:
                break
        self.assertEqual((rep["failed"], rep["held"], rep["remaining"]), (0, 0, 0), rep)
        self.notes = sorted(
            p for p in self.vault.rglob("*.md") if p.name.startswith("jr-memory-item-"))
        self.assertEqual(len(self.notes), 3, [p.name for p in self.notes])
        sb._clear_archive_read_cache()

    def config(self):
        return {"second_brain": {"enabled": True},
                "vault": {"path": str(self.vault), "recall": True, "allow_external": True}}

    def assert_status_equal(self, left, right):
        self.assertEqual(set(left), set(right))
        for key in left:
            if key == "oldest_pending_age_s":
                a, b = left[key], right[key]
                if a is None or b is None:
                    self.assertEqual(a, b, key)
                else:
                    self.assertLessEqual(abs(int(a) - int(b)), 2, key)
                continue
            self.assertEqual(left[key], right[key], key)

    def status_opens(self):
        opened = []
        real_open = os.open

        def counting_open(path, flags, mode=0o777, *, dir_fd=None):
            opened.append(os.fspath(path))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch("jarvis.second_brain.os.open", side_effect=counting_open):
            out = rt.status(self.config(), self.state)
        return out, _archive_opens(opened)

    def fresh_status(self):
        sb._clear_archive_read_cache()
        out, _opened = self.status_opens()
        return out

    def test_second_status_does_not_reopen_unchanged_notes(self):
        first, opened = self.status_opens()
        self.assertEqual(first["gate"], "ok")
        self.assertEqual(first["state"], "current")
        self.assertEqual(first["archive_chunk_checked"], 3)
        self.assertEqual(first["archive_chunk_damaged"], 0)
        self.assertEqual(sorted(opened), sorted(os.fspath(p) for p in self.notes))
        for note in self.notes:
            row = sb._NOTE_READ_CACHE[os.fspath(note)]
            st = note.stat()
            self.assertEqual(row[0], st.st_mtime_ns)
            self.assertEqual(row[1], st.st_size)
            self.assertIsInstance(row[2], dict)
            self.assertTrue(row[2]["payload_ok"])
        listings = {}
        for note in self.notes:
            key = os.fspath(note.parent)
            sig, listing = sb._DIR_LIST_CACHE[key]
            with os.scandir(note.parent) as entries:
                kids = list(entries)
                count = len(kids)
                max_ns = max(entry.stat(follow_symlinks=False).st_mtime_ns for entry in kids)
            self.assertEqual(sig, (note.parent.stat().st_mtime_ns, count, max_ns))
            listings[key] = listing

        second, again = self.status_opens()
        self.assertEqual(again, [])
        self.assert_status_equal(second, first)
        for key, listing in listings.items():
            self.assertIs(sb._DIR_LIST_CACHE[key][1], listing)

        touched = self.notes[0]
        before = touched.stat()
        os.utime(touched, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
        self.assertEqual(touched.stat().st_size, before.st_size)
        self.assertNotEqual(touched.stat().st_mtime_ns, before.st_mtime_ns)
        retimed, retimed_opens = self.status_opens()
        self.assertEqual(retimed_opens, [os.fspath(touched)])
        self.assertEqual(sb._NOTE_READ_CACHE[os.fspath(touched)][0], touched.stat().st_mtime_ns)
        self.assert_status_equal(retimed, self.fresh_status())

        rewritten = self.notes[1]
        raw = rewritten.read_bytes()
        self.assertGreaterEqual(raw.count(b"ALPHA"), 1)
        rewritten.write_bytes(raw.replace(b"ALPHA", b"BRAVO"))
        self.assertEqual(rewritten.stat().st_size, len(raw))
        changed, changed_opens = self.status_opens()
        self.assertEqual(changed_opens, [os.fspath(rewritten)])
        self.assertEqual(changed["archive_chunk_damaged"], 1)
        self.assertEqual(changed["state"], "error")
        self.assertEqual(changed["archive_chunk_checked"], 3)
        self.assert_status_equal(changed, self.fresh_status())

        removed = self.notes[2]
        removed_path = os.fspath(removed)
        intact_before = changed["archive_chunk_checked"] - changed["archive_chunk_damaged"]
        removed.unlink()
        deleted, deleted_opens = self.status_opens()
        self.assertEqual(deleted_opens, [])
        self.assertNotIn(removed_path, sb._NOTE_READ_CACHE)
        self.assertEqual(deleted["archive_chunk_checked"], 3)
        self.assertEqual(deleted["archive_chunk_damaged"], changed["archive_chunk_damaged"] + 1)
        intact_after = deleted["archive_chunk_checked"] - deleted["archive_chunk_damaged"]
        self.assertEqual(intact_after, intact_before - 1)
        self.assert_status_equal(deleted, self.fresh_status())

    def test_note_cache_stays_bounded(self):
        previous = sb._NOTE_READ_MAX
        self.addCleanup(setattr, sb, "_NOTE_READ_MAX", previous)
        sb._NOTE_READ_MAX = 1
        capped, opened = self.status_opens()
        self.assertEqual(sorted(opened), sorted(os.fspath(p) for p in self.notes))
        self.assertLessEqual(len(sb._NOTE_READ_CACHE), 1)
        self.assertLessEqual(sb._NOTE_READ_BYTES[0], sb._NOTE_READ_MAX_BYTES)
        sb._NOTE_READ_MAX = previous
        self.assert_status_equal(capped, self.fresh_status())

    def test_concurrent_status_reads_do_not_raise(self):
        self.status_opens()
        errors = []

        def run():
            try:
                rt.status(self.config(), self.state)
            except Exception as exc:  # the cache lock must keep this quiet
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
