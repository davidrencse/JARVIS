"""Second-brain audit regressions (secret hold, PC-only links, malformed task payloads, vault reads
outside the write lock). Temp fixtures only: no real vault, state, model or network."""

import json
import os
import contextlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain as sbmod  # noqa: E402
from jarvis import second_brain_queue as q  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import SecondBrain, build_payload, looks_secret  # noqa: E402
from jarvis.store import Store  # noqa: E402


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
        self.tmp = Path(tmp.name)
        self.state, self.vault = self.tmp / "state", self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        self.state.mkdir()
        self.brain = SecondBrain(self.vault)

    def drain_all(self, rounds=20):
        report = {}
        for _ in range(rounds):
            report = q.drain(self.state, self.vault, limit=50)
            if not report["remaining"]:
                break
        return report


class SecretHold(unittest.TestCase):
    """Forms jarvis.phone.redact treats as secret must not reach the vault."""

    SECRETS = {
        "github fine-grained": "github_pat_" + "A1b2" * 8,
        "aws session key": "key ASIA" + "A1" * 8,
        "google oauth": "ya29." + "a0Af" * 8,
        "publishable key": "pk-" + "a1" * 12,
        "restricted key": "rk-" + "b2" * 12,
        "stripe live": "pk_live_" + "a1" * 10,
        "cookie assign": "cookie=sessionid1234",
        "cookie header": "Cookie: sid=abcd1234; theme=dark",
        "authorization bearer": "authorization: Bearer abc123",
        "authorization basic": "Authorization: Basic dXNlcjpwYXNz",
        "url credentials": "https://user:s3cret@example.com/path",
        "short password": "password=hunter2",
        "punctuated password": "password=CorrectHorse!Battery",
        "quoted password": 'password: "correct horse battery"',
        "json password": '{"password": "p@ss!w0rd"}',
        "aws secret": "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "punctuated token": "token=abc!def@ghi#jkl$mno%pq",
    }
    PROSE = [
        "reset your password soon", "the password is a long story", "password: str", "password=required",
        "password=true", "password: <hidden>", "cookie: chocolate chip", "I ate a cookie today",
        "authorization: required", "Authorization: Bearer", "the token expires soon",
        "token=${TOKEN_FROM_ENV_VARIABLE}", "secret: ask Bob",
        "see https://example.com:8080/path and ssh://git@github.com:org/repo",
        "ASIAN markets rally", "pk-1 and rk-2", "plain note with no secrets",
    ]

    def test_each_form_is_held(self):
        for name, text in self.SECRETS.items():
            with self.subTest(name):
                self.assertTrue(looks_secret(text), text)
                self.assertTrue(looks_secret({"body": ["x", {"n": text}]}))

    def test_ordinary_prose_is_not_held(self):
        for text in self.PROSE:
            with self.subTest(text):
                self.assertFalse(looks_secret(text), text)

    def test_new_rules_agree_with_the_raw_patterns(self):
        for text in list(self.SECRETS.values()) + self.PROSE:
            raw = any(rx.search(text) for rx in sbmod.SECRET_RES)
            self.assertEqual(looks_secret(text), raw, text)

    def test_one_mib_adversarial_text_stays_fast(self):
        seeds = ["://" + "a" * 255, "password=\"", "token:" + "a" * 15 + " ", "authorization: " + "a" * 10 + " ",
                 "cookie=" + "a" * 5 + " ", "://a:", "password:", "secret_key", "pk_" + "a" * 5]
        for seed in seeds:
            for prefix in ("", "é "):   # ASCII fast path and the non-ASCII search path
                text = (prefix + seed) * (1048576 // (len(seed) + len(prefix)) + 1)
                start = time.monotonic()
                looks_secret(text[:1048576])
                self.assertLess(time.monotonic() - start, 5, seed)


class PcOnlyLinks(Base):
    def _items(self):
        mem = Memory(self.state)
        private = mem.add("note", "keep on this pc", tags=["no-archive"])
        shared = mem.add("note", "fine to archive")
        mem.link(private["id"], shared["id"], "related")
        mem.link(shared["id"], private["id"], "related")
        return private, shared

    def test_queue_never_publishes_links_of_a_no_archive_item(self):
        private, shared = self._items()
        q.configure(self.state)
        self.drain_all()
        keys = {(r[0], r[1]) for r in rows(self.state / "memory.sqlite3",
                                           "SELECT kind,key FROM archive_head")}
        self.assertNotIn(("memory-links", private["id"]), keys)
        self.assertNotIn(("memory-item", private["id"]), keys)
        self.assertIn(("memory-item", shared["id"]), keys)
        self.assertIn(("memory-links", shared["id"]), keys)   # the other side is not private
        self.assertEqual(rows(self.state / "memory.sqlite3", "SELECT COUNT(*) FROM archive_absent"), [(0,)])
        leaked = [p.name for p in self.vault.rglob("*.md")
                  if '"kind":"memory-links"' in (t := p.read_text(encoding="utf-8"))
                  and f'"key":"{private["id"]}"' in t]
        self.assertEqual(leaked, [])

    def test_a_later_link_on_a_no_archive_item_is_not_queued(self):
        private, shared = self._items()
        q.configure(self.state)
        self.drain_all()
        other = Memory(self.state).add("note", "another")
        Memory(self.state).link(private["id"], other["id"], "cites")
        self.drain_all()
        queued = rows(self.state / "memory.sqlite3",
                      "SELECT key FROM archive_head WHERE kind='memory-links'")
        self.assertNotIn((private["id"],), queued)

    def test_sync_skips_links_of_a_no_archive_item(self):
        private, shared = self._items()
        report = self.brain.sync(self.state, stores=["memory"])
        self.assertTrue(report["ok"], report)
        self.assertIsNone(self.brain.latest(f"memory/memory-links/{private['id']}"))
        self.assertIsNotNone(self.brain.latest(f"memory/memory-links/{shared['id']}"))


class MalformedTasks(Base):
    def _payload(self, key, events, **extra):
        fields = {"id": key, "spec": "{}", "state": "finished", "created_at": "t", "updated_at": "t"}
        return build_payload("tasks", "task", key, "tasks.sqlite3#tasks", fields, {"events": events, **extra})

    @staticmethod
    def _event(seq, task_id="t1"):
        return {"seq": seq, "task_id": task_id, "at": "t", "kind": "k", "detail": "{}"}

    def test_publish_refuses_a_task_with_a_non_integer_seq(self):
        for bad in ("oops", None, True, 1.5):
            out = self.brain.publish(self._payload("t1", [self._event(bad)]))
            self.assertEqual(out["status"], "held", bad)
            self.assertTrue(out["reason"].startswith("invalid"), out)
        self.assertEqual(list(self.vault.rglob("*-r0001-*.md")), [])

    def test_publish_refuses_non_object_children(self):
        for children in ([], "x", {"events": "x"}, {"events": ["x"]}, {"hops": {}}):
            payload = self._payload("t1", [])
            payload["children"] = children
            out = self.brain.publish(payload)
            self.assertEqual((out["status"], out["reason"][:7]), ("held", "invalid"), children)

    def test_publish_still_accepts_a_good_task(self):
        out = self.brain.publish(self._payload("t1", [self._event(1)]))
        self.assertEqual(out["status"], "written", out)
        self.assertTrue(self.brain.validate()["ok"])

    def _target(self):
        directory = self.tmp / "target"
        directory.mkdir()
        Store(directory)   # schema only
        return sqlite3.connect(directory / "tasks.sqlite3"), directory

    def test_restore_isolates_a_task_it_cannot_apply(self):
        db, directory = self._target()
        good = self._payload("good", [self._event(1, "good")])
        bad_seq = self._payload("bad1", [self._event("oops", "bad1")])
        bad_shape = self._payload("bad2", [{"seq": 2, "at": "t"}])   # no task_id/kind/detail
        bad_kids = self._payload("bad3", [])
        bad_kids["children"] = "x"
        tasks = [(f"stem-{o['key']}", o) for o in (bad_seq, good, bad_shape, bad_kids)]
        nonterminal = [("bad1", "running"), ("good", "running"), ("bad2", "queued")]
        report = {"errors": []}
        self.brain._apply_tasks(directory, db, tasks, nonterminal, report)
        db.commit()
        self.assertEqual(sorted(r[0] for r in db.execute("SELECT id FROM tasks")), ["good"])
        self.assertEqual([r[0] for r in db.execute("SELECT seq FROM events")], [1])
        self.assertEqual(len(report["errors"]), 3)
        self.assertEqual(nonterminal, [("good", "running")])
        self.assertTrue(all(e["problem"] for e in report["errors"]))
        db.close()


class VaultReadsOutsideTheWriteLock(Base):
    def _writable(self):
        conn = sqlite3.connect(self.state / "memory.sqlite3", timeout=0.05)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def _published_item(self):
        item = Memory(self.state).add("note", "fine to archive")
        q.configure(self.state)
        self.drain_all()
        return item

    def _drain_probing(self):
        probes = []

        def sound(store, kind, key, digest):
            probes.append(self._writable())
            return True

        with patch.object(SecondBrain, "publication_sound", side_effect=sound):
            q.drain(self.state, self.vault, limit=50)
        return probes

    def test_backfill_reads_notes_before_it_takes_the_write_lock(self):
        item = self._published_item()
        with contextlib.closing(sqlite3.connect(self.state / "memory.sqlite3")) as db, db:
            db.execute("INSERT OR IGNORE INTO archive_backfill VALUES('memory','memory-item',?)", (item["id"],))
        probes = self._drain_probing()
        self.assertTrue(probes, "the backfill never read the note")
        self.assertTrue(all(probes), "a note was read inside BEGIN IMMEDIATE")
        self.assertEqual(rows(self.state / "memory.sqlite3", "SELECT COUNT(*) FROM archive_backfill"), [(0,)])

    def test_heal_absent_reads_notes_before_it_takes_the_write_lock(self):
        item = self._published_item()
        with contextlib.closing(sqlite3.connect(self.state / "memory.sqlite3")) as db, db:
            db.execute("INSERT INTO archive_absent VALUES('memory','memory-item',?,'2026-01-01')", (item["id"],))
        probes = self._drain_probing()
        self.assertTrue(probes, "heal never read the note")
        self.assertTrue(all(probes), "a note was read inside BEGIN IMMEDIATE")
        self.assertEqual(rows(self.state / "memory.sqlite3", "SELECT COUNT(*) FROM archive_absent"), [(0,)])


if __name__ == "__main__":
    unittest.main()
