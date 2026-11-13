"""Second-brain archive engine (jarvis/second_brain.py): idempotent sync, immutable revisions,
human edits, crash/race safety, hostile vault paths, secret holds, restore rehearsal, tombstones.

Everything runs against a fake vault and a fake state directory in a temp folder; no real vault,
state, model or network is touched."""

import errno
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis import second_brain as sbmod  # noqa: E402
from jarvis.history import History  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.second_brain import (RevisionExists, SecondBrain, SecondBrainError,  # noqa: E402
                                 inventory, looks_secret, safe_relpath)
from jarvis.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SK = "sk-" + "a" * 30
GHP = "ghp_" + "a" * 36
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"


def raw_looks_secret(obj):
    """What looks_secret meant before the literal fast path: every SECRET_RES on every string."""
    return any(rx.search(text) for text in sbmod._strings(obj) for rx in sbmod.SECRET_RES)


# -- fixtures --------------------------------------------------------------------------------
def make_state(state):
    """A small but complete fake state dir. Returns the ids the tests refer to."""
    state.mkdir(parents=True, exist_ok=True)
    mem = Memory(state)
    a = mem.add("note", "Alpha body\nsecond line", title="Alpha", tags=["t1", "t2"],
                meta={"k": [1, 2]}, importance=0.7)
    b = mem.add("fact", "Beta body", title="Beta")
    mem.link(a["id"], b["id"], "related")
    with mem.connect() as db:   # a link whose src has no item row
        db.execute("INSERT INTO links VALUES(?,?,?,?)", ("ghost:1", b["id"], "related", "2026-01-01T00:00:00+00:00"))
    hist = History(state)
    cid = hist.record("req-1", {"device": "main", "intent": "open", "args": {"app": "x"}},
                      {"ok": True, "state": "verified", "text": "done", "observed_ms": 5}, text="open x",
                      hops=[{"name": "parse", "ms": 1}, {"name": "run", "ms": 2, "state": "done"}], total_ms=9)
    tasks = Store(state)
    done = tasks.add({"objective": "finish me"})
    tasks.transition(done["id"], "queued", "running")
    tasks.transition(done["id"], "running", "finished", {"note": "ok"})
    queued = tasks.add({"objective": "still queued"})
    running = tasks.add({"objective": "was running"})
    tasks.transition(running["id"], "queued", "running")
    (state / "plans").mkdir()
    (state / "plans" / "plan-0123abcd.json").write_text(json.dumps({"state": "running", "steps": ["a"]}))
    (state / "plans" / "plan-89abcdef.json").write_text(json.dumps({"state": "done", "steps": []}))
    run = state / "runs" / done["id"]
    run.mkdir(parents=True)
    (run / "result.md").write_text("# résultat 😀\r\nline two\n", encoding="utf-8")
    (run / "out.json").write_text('{"a": 1}')
    (run / "blob.txt").write_bytes(b"\xff\xfe binary \x00 bytes \x80")
    know = state / "knowledge"
    for rel, text in (("engine.json", "{}"), ("log.jsonl", '{"e":1}\n'), ("raw/a.md", "raw a"),
                      ("wiki/b.md", "wiki b"), ("checks/c.json", "[]"), ("bots/d.json", "{}")):
        (know / rel).parent.mkdir(parents=True, exist_ok=True)
        (know / rel).write_text(text)
    return {"alpha": a["id"], "beta": b["id"], "cmd": cid, "done": done["id"], "queued": queued["id"],
            "running": running["id"]}


def dump(state):
    """Typed rows of every captured table, for exact comparison."""
    out = {}
    spec = (("memory.sqlite3", (("items", "id"), ("links", "src,dst,rel"))),
            ("history.sqlite3", (("commands", "id"), ("hops", "command,name"))),
            ("tasks.sqlite3", (("tasks", "id"), ("events", "seq"))))
    for fname, tables in spec:
        path = Path(state) / fname
        if not path.exists():
            continue
        db = sqlite3.connect(path)
        try:
            for table, order in tables:
                out[table] = [tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        finally:
            db.close()
    return out


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    """{relpath: (bytes, mtime_ns)} of every file under root."""
    out = {}
    for base, _, names in os.walk(root):
        for name in names:
            p = Path(base) / name
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def tamper_payload(path, old, new):
    """Edit only the one-line JSON payload of a note."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    at = max(i for i, l in enumerate(lines) if l == "<!-- jarvis-payload-begin -->") + 2
    assert old in lines[at]
    lines[at] = lines[at].replace(old, new)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.state = self.base / "state"
        self.sb = SecondBrain(self.vault)
        self.area = self.vault / "reference" / "jarvis-records"

    def notes(self, vault=None):
        area = (vault or self.vault) / "reference" / "jarvis-records"
        return sorted(p for p in area.rglob("jr-*.md")) if area.exists() else []

    def note_for(self, rid, rev=None):
        stem = SecondBrain.stem_for(rid)[0]
        found = sorted(p for p in self.notes() if p.name.startswith(stem + "-"))
        return found[-1] if rev is None else [p for p in found if f"-r{rev:04d}-" in p.name][0]

    def parsed(self, path):
        return SecondBrain.parse_note(Path(path).read_bytes())

    def payload_keys(self):
        keys = set()
        for p in self.notes():
            obj = self.parsed(p)["obj"]
            keys.add((obj["store"], obj["kind"], obj["key"]))
        return keys

    def synced(self, **kw):
        report = self.sb.sync(self.state, **kw)
        self.assertTrue(report["ok"], report)
        return report

    def rid(self, kind, key, store=None):
        store = store or {"memory-item": "memory", "memory-links": "memory", "history-command": "history",
                          "task": "tasks", "plan": "plans", "run-file": "runs",
                          "knowledge-file": "knowledge"}[kind]
        return SecondBrain.record_id(store, kind, key)


# -- 1. idempotency ----------------------------------------------------------------------------
class Idempotency(Base):
    def test_second_sync_writes_nothing_and_touches_nothing(self):
        make_state(self.state)
        first = self.synced()
        self.assertTrue(first["complete"], first)
        written = first["written"]
        self.assertGreaterEqual(written, 15)
        before = tree(self.vault)
        second = self.synced()
        self.assertEqual((second["written"], second["unchanged"]), (0, written))
        self.assertEqual(tree(self.vault), before)   # same files, same bytes, same mtimes

    def test_dry_run_writes_nothing_anywhere(self):
        make_state(self.state)
        report = self.synced(dry_run=True)
        self.assertGreaterEqual(report["written"], 15)
        self.assertEqual(os.listdir(self.vault), [])   # not even the area folder
        self.synced()
        self.assertEqual(self.synced(dry_run=True)["written"], 0)

    def test_unknown_or_partial_store_selection(self):
        make_state(self.state)
        with self.assertRaises(SecondBrainError):
            self.sb.sync(self.state, stores=["bogus"])
        partial = self.synced(stores=["memory"])
        self.assertFalse(partial["complete"])   # only a full-store run can claim completeness
        self.assertEqual(set(partial["stores"]), {"memory"})
        self.assertEqual({k[0] for k in self.payload_keys()}, {"memory"})


# -- 2. amendments / 3. human edits ------------------------------------------------------------
class Revisions(Base):
    def test_source_change_appends_revision_and_never_touches_the_old_one(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        r1 = self.note_for(rid, 1)
        r1_bytes = r1.read_bytes()
        Memory(self.state).merge_meta(ids["alpha"], {"edited": True})
        report = self.synced()
        self.assertEqual((report["written"], report["unchanged"] > 0), (1, True))
        r2 = self.note_for(rid, 2)
        self.assertRegex(r2.name, r"-r0002-")
        self.assertEqual(self.parsed(r2)["meta"]["jarvis_prev"], r1.name[:-3])
        self.assertEqual(r1.read_bytes(), r1_bytes)
        latest = self.sb.latest(rid)
        self.assertIn('"edited": true', latest["fields"]["meta"])
        self.assertEqual(self.sb.validate()["error_count"], 0)

    def test_task_state_change_is_a_new_revision_with_its_events(self):
        ids = make_state(self.state)
        self.synced()
        Store(self.state).transition(ids["queued"], "queued", "failed", {"why": "x"})
        self.assertEqual(self.synced()["written"], 1)
        obj = self.sb.latest(self.rid("task", ids["queued"]))
        self.assertEqual(obj["fields"]["state"], "failed")
        self.assertEqual([e["kind"] for e in obj["children"]["events"]], ["queued", "failed"])

    def test_human_body_edit_is_kept_and_reported_not_rewritten(self):
        ids = make_state(self.state)
        self.synced()
        note = self.note_for(self.rid("memory-item", ids["alpha"]))
        with open(note, "a", encoding="utf-8") as f:
            f.write("\nDavid's own annotation\n")
        edited = note.read_bytes()
        mtime = note.stat().st_mtime_ns
        before = tree(self.vault)
        report = self.synced()
        self.assertEqual(report["written"], 0)
        self.assertEqual(note.read_bytes(), edited)
        self.assertEqual(note.stat().st_mtime_ns, mtime)
        self.assertEqual(tree(self.vault), before)
        check = self.sb.validate()
        self.assertTrue(check["ok"])
        self.assertEqual(check["human_edited_count"], 1)
        self.assertTrue(check["human_edited"][0].endswith(note.name))
        self.assertEqual(self.sb.latest(self.rid("memory-item", ids["alpha"]))["key"], ids["alpha"])

    def test_edited_payload_is_an_error_and_blocks_strict_restore(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        tamper_payload(self.note_for(rid), "Alpha body", "Alpha b0dy")
        check = self.sb.validate()
        self.assertFalse(check["ok"])
        self.assertEqual([e["problem"] for e in check["errors"]], ["payload-hash-mismatch"])
        with self.assertRaises(SecondBrainError):
            self.sb.latest(rid)
        target = self.base / "restored"
        strict = self.sb.restore(target)
        self.assertFalse(strict["ok"])
        self.assertFalse(target.exists())
        self.assertEqual([p.name for p in self.base.iterdir() if ".restore-" in p.name], [])
        loose = self.sb.restore(target, strict=False)
        self.assertTrue(loose["partial"])
        self.assertEqual(len(loose["errors"]), 1)
        restored_ids = {r[0] for r in dump(target)["items"]}
        self.assertEqual(restored_ids, {ids["beta"]})   # damaged record skipped, the rest restored

    def test_missing_revision_is_flagged(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        Memory(self.state).merge_meta(ids["alpha"], {"v": 2})
        self.synced()
        self.note_for(rid, 1).unlink()
        problems = [e["problem"] for e in self.sb.validate()["errors"]]
        self.assertIn("revision-gap-or-duplicate", problems)


# -- 4. crash / partial / race -----------------------------------------------------------------
class CrashSafety(Base):
    def test_stale_tmp_is_ignored_and_only_old_ones_inside_the_area_are_swept(self):
        make_state(self.state)
        self.synced()
        shard = next(p for p in self.area.rglob("*") if p.is_dir() and re.fullmatch(r"[0-9a-f]{2}", p.name))
        old, new = shard / ".jr-tmp-old", shard / ".jr-tmp-new"
        outside = self.vault / ".jr-tmp-outside"
        for p in (old, new, outside):
            p.write_bytes(b"half a note")
        past = old.stat().st_mtime - 7200
        os.utime(old, (past, past))
        os.utime(outside, (past, past))
        check = self.sb.validate()
        self.assertTrue(check["ok"])
        self.assertEqual(check["stale_tmp"], 2)
        self.assertEqual(self.synced()["written"], 0)
        self.assertEqual(self.sb.sweep_stale_tmp(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(outside.exists())   # never reaches outside the archive area

    def test_garbage_at_a_final_name_fails_validate_and_strict_restore(self):
        make_state(self.state)
        self.synced()
        shard = self.area / "memory" / "memory-item" / "aa"
        shard.mkdir(parents=True, exist_ok=True)
        (shard / ("jr-memory-item-trunc-" + "a" * 12 + "-r0001-" + "b" * 12 + ".md")).write_bytes(b"---\ntitle: cu")
        check = self.sb.validate()
        self.assertFalse(check["ok"])
        self.assertTrue(check["errors"][0]["problem"].startswith("unreadable"))
        target = self.base / "r"
        self.assertFalse(self.sb.restore(target)["ok"])
        self.assertFalse(target.exists())

    def test_vault_write_failure_is_reported_and_retry_completes_without_duplicates(self):
        make_state(self.state)
        live = {n: sha(self.state / n) for n in ("memory.sqlite3", "history.sqlite3", "tasks.sqlite3")}
        orig = SecondBrain._write_new

        def flaky(self_, parts, name, data):
            if parts and parts[0] == "memory":
                raise OSError(errno.EIO, "disk says no")
            return orig(self_, parts, name, data)
        with patch.object(SecondBrain, "_write_new", flaky):
            bad = self.sb.sync(self.state)
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["complete"])
        self.assertEqual(len(bad["failures"]), 4)   # two items + two links records
        self.assertTrue(all(f["record"].startswith("memory/") for f in bad["failures"]))
        self.assertGreater(bad["written"], 5)
        self.assertEqual({n: sha(self.state / n) for n in live}, live)   # source untouched
        good = self.synced()
        self.assertTrue(good["complete"])
        self.assertEqual(good["written"], 4)
        self.assertEqual(good["unchanged"], bad["written"])
        names = [p.name for p in self.notes()]
        self.assertEqual(len(names), len({n.rsplit("-r", 1)[0] for n in names}))   # one revision each
        self.assertTrue(self.sb.validate()["ok"])

    def _rival_state(self, alpha_id, body):
        """A second state dir holding only this item: unchanged copy of the row, or other content."""
        other = self.base / "other"
        Memory(other)
        with sqlite3.connect(self.state / "memory.sqlite3") as src, sqlite3.connect(other / "memory.sqlite3") as db:
            row = list(src.execute("SELECT * FROM items WHERE id=?", (alpha_id,)).fetchone())
            if body is not None:
                row[3] = body
                row[8] = "d" * 64
            db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        return other

    def _race(self, theirs_body, raise_exists):
        ids = make_state(self.state)
        other = self._rival_state(ids["alpha"], theirs_body)
        rival, fired, orig = SecondBrain(self.vault), [], SecondBrain._write_new
        mine = self.rid("memory-item", ids["alpha"])

        def racing(self_, parts, name, data):
            if not fired and parts and parts[0] == "memory" and name.startswith(SecondBrain.stem_for(mine)[0]):
                fired.append(name)
                rival.sync(other, stores=["memory"])   # the other writer gets there first
                if raise_exists:
                    raise RevisionExists(name)
            return orig(self_, parts, name, data)
        with patch.object(SecondBrain, "_write_new", racing):
            report = self.sb.sync(self.state, stores=["memory"])
        self.assertEqual(len(fired), 1)
        return ids, mine, report

    def test_race_with_other_content_is_an_explicit_identity_collision(self):
        """Another install won the revision with a DIFFERENT record under the same id: never merged
        into one history (audit: cross-install collapse). The winner stays intact, the clash is reported."""
        ids, mine, report = self._race("their body", raise_exists=True)
        self.assertFalse(report["ok"])
        self.assertEqual([f["record"] for f in report["failures"]], [mine])
        self.assertIn("identity-collision", report["failures"][0]["error"])
        r1 = self.note_for(mine, 1)
        self.assertIn("their body", self.parsed(r1)["obj"]["fields"]["body"])   # winner intact
        with self.assertRaises(IndexError):   # no second revision was appended
            self.note_for(mine, 2)
        self.assertTrue(self.sb.validate()["ok"])

    def test_race_with_identical_content_ends_unchanged(self):
        ids, mine, report = self._race(None, raise_exists=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(len([p for p in self.notes() if SecondBrain.stem_for(mine)[0] in p.name]), 1)
        self.assertEqual(self.sb.latest(mine)["fields"]["body"], "Alpha body\nsecond line")
        self.assertTrue(self.sb.validate()["ok"])

    def test_two_writers_with_different_content_never_share_a_revision_number(self):
        ids, mine, report = self._race("their body", raise_exists=False)
        self.assertEqual(self.sb.validate()["errors"], [])


# -- 5. unavailable / hostile vault ------------------------------------------------------------
class VaultTrouble(Base):
    def test_missing_root_and_file_root_report_instead_of_writing(self):
        make_state(self.state)
        for root in (self.base / "nope", self.base / "afile"):
            if root.name == "afile":
                root.write_text("x")
            report = SecondBrain(root).sync(self.state)
            self.assertFalse(report["ok"])
            self.assertTrue(report["error"].startswith("vault-unavailable"), report)
            self.assertEqual(report["written"], 0)
        self.assertFalse((self.base / "nope").exists())
        self.assertEqual((self.base / "afile").read_text(), "x")
        self.assertFalse(SecondBrain(self.base / "nope").check_vault()["ok"])

    def test_read_only_root_is_refused(self):
        make_state(self.state)
        self.vault.chmod(0o555)
        self.addCleanup(self.vault.chmod, 0o755)
        if os.access(self.vault, os.W_OK):
            self.skipTest("permissions not enforced (root?)")
        report = self.sb.sync(self.state)
        self.assertFalse(report["ok"])
        self.assertIn("not writable", report["error"])
        self.assertEqual(os.listdir(self.vault), [])

    def test_symlinked_area_or_parent_is_not_followed(self):
        make_state(self.state)
        outside = self.base / "outside"
        outside.mkdir()
        (self.vault / "reference").mkdir()
        os.symlink(outside, self.area)
        report = self.sb.sync(self.state)
        self.assertFalse(report["ok"])
        self.assertTrue(report["error"].startswith("unsafe-path"))
        self.assertEqual(os.listdir(outside), [])
        self.assertFalse(self.sb.tombstone("memory/memory-item/x", "r")["ok"])
        os.unlink(self.area)
        os.rmdir(self.vault / "reference")
        os.symlink(outside, self.vault / "reference")
        self.assertTrue(self.sb.sync(self.state)["error"].startswith("unsafe-path"))
        self.assertEqual(os.listdir(outside), [])

    def test_symlinked_shard_directory_is_a_failure_not_a_detour(self):
        state = self.state
        state.mkdir()
        item = Memory(state).add("note", "only item")
        outside = self.base / "outside"
        outside.mkdir()
        rid = self.rid("memory-item", item["id"])
        shard = self.area / "memory" / "memory-item" / SecondBrain.stem_for(rid)[3][:2]
        shard.parent.mkdir(parents=True)
        os.symlink(outside, shard)
        report = self.sb.sync(state, stores=["memory"])
        self.assertFalse(report["ok"])
        self.assertEqual([f["record"] for f in report["failures"]], [rid])
        self.assertIn("link", report["failures"][0]["error"])
        self.assertEqual(os.listdir(outside), [])
        self.assertTrue(any("symlink" in w for w in report["warnings"]))


# -- 7. boundaries -----------------------------------------------------------------------------
class Boundaries(Base):
    def test_oversize_records_are_reported_never_truncated(self):
        state = self.state
        state.mkdir()
        mem = Memory(state)
        small, big = mem.add("note", "small"), mem.add("note", "B" * 5000)
        (state / "runs" / "t1").mkdir(parents=True)
        (state / "runs" / "t1" / "huge.txt").write_bytes(b"z" * 1500)
        sb = SecondBrain(self.vault, max_record_bytes=2000)
        report = sb.sync(state)
        reasons = {s["record"]: s["reason"] for s in report["skipped"]}
        self.assertTrue(reasons[self.rid("memory-item", big["id"])].startswith("oversize"))
        self.assertEqual(reasons[self.rid("run-file", "t1/huge.txt")], "oversize")
        self.assertFalse(report["complete"])
        keys = self.payload_keys()
        self.assertIn(("memory", "memory-item", small["id"]), keys)
        self.assertNotIn(("memory", "memory-item", big["id"]), keys)
        self.assertNotIn("BBBBB", b"".join(p.read_bytes() for p in self.notes()).decode())
        self.assertNotIn("BBBBB", json.dumps(report))

    def test_hostile_text_round_trips_exactly(self):
        state = self.state
        state.mkdir()
        fake = ("before\n<!-- jarvis-payload-begin -->\n```json\n{\"fake\":1}\n```\n"
                "<!-- jarvis-payload-end -->\r\n```` ``` ````\r\nüñí 😀   end\r\n")
        mem = Memory(state)
        item = mem.add("note", fake, title="t\nwith\nlines 😀", tags=["x"])
        self.synced()
        self.assertEqual(self.sb.latest(self.rid("memory-item", item["id"]))["fields"]["body"], fake)
        target = self.base / "back"
        self.assertTrue(self.sb.restore(target)["ok"])
        self.assertEqual(Memory(target).get(item["id"])["body"], fake)
        self.assertEqual(Memory(target).get(item["id"])["title"], item["title"])
        self.assertTrue(self.sb.validate()["ok"])

    def _weird_state(self):
        state = self.state
        state.mkdir()
        Memory(state), History(state), Store(state)
        weird = ["a b/../c\nd", "../../etc/passwd", "x" * 600, "CON", "ünï:cødé 😀", "...", "NUL.txt", "a" * 130,
                 "UPPER Case/Path"]
        db = sqlite3.connect(state / "memory.sqlite3")
        for i, key in enumerate(weird):
            db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", (key, "note", "T" * 3000, f"body {i}", "",
                                                                         "owner", 0.5, "{}", f"dig{i}", "2026-01-01"))
        db.commit()
        db.close()
        db = sqlite3.connect(state / "history.sqlite3")
        for i, key in enumerate(weird):
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, "r", float(i), None, "owner", "t", "k", "main", "i", "{}", "done", 1, "ok", "", None, None))
        db.commit()
        db.close()
        db = sqlite3.connect(state / "tasks.sqlite3")
        for i, key in enumerate(weird):
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (key, "{}", "finished", f"2026-01-0{i + 1}", "x"))
            db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)", (key, "t", "finished", "{}"))
        db.commit()
        db.close()
        return weird

    def test_hostile_ids_stay_inside_the_area_with_short_safe_names(self):
        weird = self._weird_state()
        report = self.synced()
        self.assertEqual(report["written"], 3 * len(weird))
        area = self.area.resolve()
        stems = set()
        for path in self.notes():
            self.assertTrue(path.resolve().is_relative_to(area), path)
            rel = str(path.relative_to(self.vault))
            self.assertLessEqual(len(rel), 200, rel)
            self.assertRegex(path.name, r"^[a-z0-9._-]+$")
            stems.add(path.name.rsplit("-r", 1)[0])
        self.assertEqual(len(stems), 3 * len(weird))   # slug collisions are resolved by the digest
        target = self.base / "back"
        self.assertTrue(self.sb.restore(target)["ok"])
        self.assertEqual(dump(target)["items"], dump(self.state)["items"])
        self.assertEqual(dump(target)["commands"], dump(self.state)["commands"])
        self.assertEqual({r[0] for r in dump(target)["tasks"]}, set(weird))

    def test_file_policy_exports_only_named_shapes_and_skips_links(self):
        state = self.state
        state.mkdir()
        outside = self.base / "outside.txt"
        outside.write_text("outside secret-free text")
        (state / "plans").mkdir()
        for name in ("plan-0123abcd.json", "other.json", "plan-ZZZZZZZZ.json", "plan-0123abcd.json.bak", "x y.json"):
            (state / "plans" / name).write_text("{}")
        os.symlink(outside, state / "plans" / "plan-aaaaaaaa.json")
        run = state / "runs" / "t1"
        (run / "sub").mkdir(parents=True)
        for name in ("ok.md", "x.log", ".hidden.md", "sp ace.md", "sub/deep.md", "bad.bin", "..md"):
            (run / name).write_text("x")
        (state / "runs" / "top.md").write_text("x")
        os.symlink(outside, run / "link.md")
        os.symlink(run, state / "runs" / "linked-run")
        know = state / "knowledge"
        for rel in ("engine.json", ".lock", "notes.md", "raw/a.md", "raw/sub/deep.md", "other/z.md", "wiki/.x.md"):
            (know / rel).parent.mkdir(parents=True, exist_ok=True)
            (know / rel).write_text("x")
        os.symlink(outside, know / "raw" / "link.md")
        self.synced()
        files = {k[2] for k in self.payload_keys()}
        self.assertEqual(files, {"plan-0123abcd.json", "t1/ok.md", "engine.json", "raw/a.md"})
        self.assertNotIn("outside secret-free text", b"".join(p.read_bytes() for p in self.notes()).decode())

    def test_safe_relpath_rules(self):
        self.assertTrue(safe_relpath("runs", "t1/a.md"))
        self.assertFalse(safe_relpath("runs", "t1/../a.md"))
        self.assertFalse(safe_relpath("runs", "../a.md"))
        self.assertFalse(safe_relpath("runs", "a.md"))
        self.assertFalse(safe_relpath("plans", "plan-0123abcd.json/x"))
        self.assertFalse(safe_relpath("knowledge", "a/b/c/d.md"))
        self.assertFalse(safe_relpath("memory", "x"))
        self.assertFalse(safe_relpath("runs", "t1/C:evil.md"))

    def test_secret_like_records_are_held_unless_allowed(self):
        state = self.state
        state.mkdir()
        mem = Memory(state)
        held = [mem.add("note", f"has {SK} inside"), mem.add("note", f"token {GHP}"),
                mem.add("note", f"{PEM}\nMIIE...")]
        fine = mem.add("note", "ordinary note")
        (state / "runs" / "t1").mkdir(parents=True)
        (state / "runs" / "t1" / "a.md").write_text(f"file with {SK}")
        report = self.sb.sync(state)
        by_reason = {s["record"]: s["reason"] for s in report["skipped"]}
        for item in held:
            self.assertEqual(by_reason[self.rid("memory-item", item["id"])], "secret-like")
        self.assertEqual(by_reason[self.rid("run-file", "t1/a.md")], "secret-like")
        self.assertFalse(report["complete"])
        self.assertEqual(report["written"], 1)
        blob = b"".join(p.read_bytes() for p in self.vault.rglob("*") if p.is_file()).decode()
        for needle in (SK, GHP, PEM):
            self.assertNotIn(needle, blob)
            self.assertNotIn(needle, json.dumps(report))
        self.assertIn(("memory", "memory-item", fine["id"]), self.payload_keys())
        allowed = self.rid("memory-item", held[0]["id"])
        again = self.sb.sync(state, allow_secret_like=(allowed,))
        self.assertEqual(again["written"], 1)
        self.assertEqual(len(again["skipped"]), 3)
        self.assertIn(SK, self.sb.latest(allowed)["fields"]["body"])
        self.assertNotIn(SK, json.dumps(again))   # reports carry ids and counts, never bodies

    def test_looks_secret_scans_nested_values(self):
        self.assertTrue(looks_secret({"a": [{"b": f"x {SK}"}]}))
        self.assertTrue(looks_secret(["password = hunter2hunter2hunter2"]))
        self.assertFalse(looks_secret({"a": "plain text", "n": 5}))

    def test_looks_secret_matches_the_raw_patterns(self):
        """Every pattern, the near misses, and a seeded fuzz agree with SECRET_RES.search."""
        tail = "a" * 16
        jwt = "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10
        cases = {
            "sk": (f"x {SK} y", True),
            "sk-short": ("sk-" + "a" * 19, False),
            "sk-boundary": ("ask-" + "a" * 30, False),
            "sk-later": ("sk-" + "a" * 10 + " sk-" + "b" * 20, True),
            "ghp": (f"token {GHP}", True),
            "ghx": ("ghx_" + "a" * 36, False),
            "gho": ("gho_" + "a" * 30, True),
            "xox": ("xoxb-" + "a" * 10, True),
            "xox-short": ("xoxb-" + "a" * 9, False),
            "xoxc": ("xoxc-" + "a" * 12, False),
            "akia": ("AKIA" + "A" * 16, True),
            "akia-long": ("AKIA" + "A" * 17, False),
            "akia-lower": ("akia" + "a" * 16, False),
            "aiza": ("AIza" + "a" * 35, True),
            "aiza-short": ("AIza" + "a" * 34, False),
            "jwt": (jwt, True),
            "jwt-later": ("eyJ" + "a" * 9 + "!" + jwt, True),
            "jwt-short": ("eyJ" + "a" * 10 + "." + "b" * 10, False),
            "pem": (PEM, True),
            "pem-public": ("-----BEGIN PUBLIC KEY-----", False),
            "pem-lower": ("-----begin rsa private key-----", False),
            "bearer": ("Bearer " + "a" * 24, True),
            "bearer-short": ("bearer " + "a" * 23, False),
            "bearer-boundary": ("xbearer " + "a" * 24, False),
            "password": ("password = hunter2hunter2hunter2", True),
            "password-case": ("PassWord = " + tail, True),
            "api_key": ("api_key = " + tail, True),
            "api-key": ("API-KEY:" + tail, True),
            "apikey": ('apikey="' + tail + '"', True),
            "secret-quote": ("secret: '" + tail + "'", True),
            "passwd": ("passwd=" + tail, True),
            "token-inside": ("mytoken=" + tail, True),
            "token-short": ("token = " + "a" * 15, False),
            "token-word": ("the token is not assigned", False),
            "ws": ("token" + " " * 40 + "=" + tail, True),
            "kelvin": ("to\u212aen = " + tail, True),
            "nbsp": ("token\u00a0=\u00a0" + tail, True),
            "emoji-sk": ("héllo " + SK, True),
            "empty": ("", False),
            "plain": ("ordinary note", False),
        }
        for name, (text, want) in cases.items():
            got = looks_secret(text)
            self.assertEqual(got, want, name)
            self.assertEqual(got, raw_looks_secret(text), name)
        nested = {"a": [{"b": f"x {SK}"}], "n": 5, "t": ("password = " + tail,)}
        self.assertTrue(looks_secret(nested))
        self.assertEqual(looks_secret(nested), raw_looks_secret(nested))
        self.assertFalse(looks_secret({"a": "plain", "n": 5, "b": None}))
        blob = "z" * 50_000
        late = blob + " " + SK
        early = SK + blob
        self.assertFalse(looks_secret(blob))
        self.assertEqual(looks_secret(blob), raw_looks_secret(blob))
        self.assertTrue(looks_secret(late) and looks_secret(early))
        self.assertEqual(looks_secret(late), raw_looks_secret(late))
        rng = random.Random(339420)
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-=:.'\"/\n"
        fragments = ["", "z", "sk-", "token", "password", "Bearer ", "eyJ", "AKIA", "-----BEGIN ",
                     "ghp_", "xoxb-", "AIza", "=", ":", "secret", "api_key", "\u212a", "é", "\u00a0"]
        for i in range(80):
            parts = [rng.choice(fragments) if i % 2 else rng.choice(alphabet) for _ in range(rng.randint(1, 24))]
            if i % 5 == 0:
                parts.append(rng.choice((SK, GHP, PEM, jwt, "Bearer " + "c" * 24, "token = " + tail)))
            text = "".join(parts)
            self.assertEqual(looks_secret(text), raw_looks_secret(text), text[:80])
            wrapped = {"k": [text, {"n": i}]}
            self.assertEqual(looks_secret(wrapped), raw_looks_secret(wrapped), i)

    def test_looks_secret_bulk_ascii_beats_the_raw_scan(self):
        """Relative to SECRET_RES on the same run. A fixed clock budget flakes across machines."""
        blob = "z" * (512 * 1024)
        obj = {"spec": blob, "n": 1}
        looks_secret(obj)
        raw_looks_secret(obj)

        def best(fn):
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                self.assertFalse(fn())
                samples.append(time.perf_counter() - started)
            return min(samples)

        fast, slow = best(lambda: looks_secret(obj)), best(lambda: raw_looks_secret(obj))
        self.assertGreater(slow, fast * 5, f"fast={fast:.4f}s slow={slow:.4f}s")
        planted = {"spec": blob + " " + SK}
        self.assertTrue(looks_secret(planted))
        self.assertTrue(raw_looks_secret(planted))

    def test_looks_secret_jwt_needle_repeat_stays_linear(self):
        """'-eyJ' is a word boundary at every needle, and the class then eats the rest.

        A match at each hit backtracks over that suffix (quadratic, and SECRET_RES.search
        does too). The scan agrees with the raw pattern and stays one pass.
        """
        body = "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10
        tricky = (
            "-eyJ" * 200,
            "-eyJ" * 200 + body,
            "-eyJ" * 40 + "!" + body,
            "_" + body,
            "x" + body,
            "é" + body,
            "\u212a" + body,
            "\u00a0" + body,
            "\n" + body,
            "-" + body,
            body,
            "eyJ" + "a" * 9 + "." + "b" * 10 + "." + "c" * 10,
            "eyJ" + "a" * 10 + "." + "b" * 9 + "." + "c" * 10,
            "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 9,
            "eyJaaa.eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10,
            "eyJ" + "a" * 10 + "." + "bb!eyJ" + "c" * 10 + "." + "d" * 10 + "." + "e" * 10,
        )
        for text in tricky:
            self.assertEqual(looks_secret(text), raw_looks_secret(text), text[:70])

        # 16 KB of '-eyJ'. SECRET_RES.search backtracks over the suffix at every needle.
        # Same-run ratio, same reason as the bulk test: a fixed cutoff flakes.
        text = "-eyJ" * 4000

        def best(fn):
            fn(text)
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                self.assertFalse(fn(text))
                samples.append(time.perf_counter() - started)
            return min(samples)

        fast, slow = best(looks_secret), best(raw_looks_secret)
        self.assertGreater(slow, fast * 20, f"fast={fast:.4f}s slow={slow:.4f}s")


# -- 8. snapshot consistency -------------------------------------------------------------------
class Snapshots(Base):
    def test_sync_reads_a_snapshot_and_leaves_live_databases_alone(self):
        ids = make_state(self.state)
        live = {n: sha(self.state / n) for n in ("memory.sqlite3", "history.sqlite3", "tasks.sqlite3")}
        orig, seen = SecondBrain._snapshot, []

        def spy(self_, db_path, workdir):
            db = orig(self_, db_path, workdir)
            seen.append((Path(db_path).name, [r[2] for r in db.execute("PRAGMA database_list")][0]))
            if Path(db_path).name == "memory.sqlite3":   # the live DB moves on after the snapshot
                Memory(self_.last_state).add("note", "late arrival", title="late")
            return db
        self.sb.last_state = self.state
        with patch.object(SecondBrain, "_snapshot", spy):
            report = self.sb.sync(self.state)
        self.assertTrue(report["ok"])
        self.assertEqual([n for n, _ in seen], ["memory.sqlite3", "history.sqlite3", "tasks.sqlite3"])
        self.assertTrue(all("jr-snap-" in path and str(self.state) not in path for _, path in seen))
        self.assertEqual({k[2] for k in self.payload_keys() if k[0] == "memory" and k[1] == "memory-item"},
                         {ids["alpha"], ids["beta"]})   # the late item is not in this run
        self.assertEqual(sha(self.state / "history.sqlite3"), live["history.sqlite3"])
        self.assertEqual(sha(self.state / "tasks.sqlite3"), live["tasks.sqlite3"])
        self.assertEqual([p.name for p in self.state.iterdir() if "journal" in p.name or "wal" in p.name], [])
        self.assertEqual(self.synced()["written"], 1)   # the next run picks it up

    def test_report_states_scope_and_per_store_snapshot_time(self):
        make_state(self.state)
        report = self.synced()
        self.assertIn("not an atomic", report["scope"])
        for name in ("memory", "history", "tasks", "plans", "runs", "knowledge"):
            self.assertTrue(report["stores"][name]["snapshot_at"], name)
        manifest = next((self.area / "manifests").glob("jarvis-sync-*.md")).read_text()
        self.assertIn("snapshot", manifest)

    def test_database_with_an_open_writer_still_syncs_committed_rows(self):
        ids = make_state(self.state)
        writer = sqlite3.connect(self.state / "memory.sqlite3", timeout=1, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO items VALUES('note:uncommitted','note','u','u','','o',0.5,'{}','uu','t')")
            report = self.sb.sync(self.state, stores=["memory"])
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        self.assertTrue(report["ok"], report)
        self.assertEqual({k[2] for k in self.payload_keys() if k[1] == "memory-item"}, {ids["alpha"], ids["beta"]})


# -- 9. restore --------------------------------------------------------------------------------
class Restore(Base):
    def test_full_rebuild_from_the_vault_alone(self):
        ids = make_state(self.state)
        with sqlite3.connect(self.state / "memory.sqlite3") as db:   # derived data that must not travel
            db.execute("INSERT INTO vectors VALUES('v1','m',?)", (b"VECTORMARK",))
        before, files = dump(self.state), {}
        for sub in ("plans", "runs", "knowledge"):
            for p in (self.state / sub).rglob("*"):
                if p.is_file():
                    files[str(p.relative_to(self.state))] = p.read_bytes()
        self.synced()
        shutil.rmtree(self.state)
        target = self.base / "rebuilt"
        report = self.sb.restore(target)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["nonterminal_tasks"], 2)
        after = dump(target)
        for table in ("items", "links", "commands", "hops"):
            self.assertEqual(after[table], before[table], table)   # ids, bodies, digest, created_at...
        self.assertTrue(any(r[0] == "ghost:1" for r in after["links"]))
        nonterminal = {ids["queued"], ids["running"]}
        want_tasks = [(r[0], r[1], "interrupted" if r[0] in nonterminal else r[2], r[3], r[4]) for r in before["tasks"]]
        self.assertEqual(after["tasks"], want_tasks)
        original = [e for e in after["events"] if e[3] != "restored"]
        self.assertEqual(original, before["events"])
        restored = [e for e in after["events"] if e[3] == "restored"]
        self.assertEqual({e[1] for e in restored}, nonterminal)
        self.assertEqual({json.loads(e[4])["original_state"] for e in restored}, {"queued", "running"})
        for rel, raw in files.items():   # runs and knowledge byte-equal; plans except the running one
            got = (target / rel).read_bytes()
            if rel == "plans/plan-0123abcd.json":
                plan = json.loads(got)
                self.assertEqual((plan["state"], plan["restored_from_state"], plan["steps"]), ("blocked", "running", ["a"]))
            else:
                self.assertEqual(got, raw, rel)
        mem = Memory(target)
        self.assertEqual(mem.get(ids["alpha"])["tags"], ["t1", "t2"])
        self.assertEqual(mem.get(ids["alpha"])["meta"], {"k": [1, 2]})
        self.assertEqual([m["id"] for m in mem.search("Beta")], [ids["beta"]])   # FTS index rebuilt by triggers
        with mem.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)
        self.assertFalse((target / "tasks.pruned").exists())   # checked before Store() re-stamps it
        self.assertEqual(Store(target).get(ids["done"])["state"], "finished")
        self.assertEqual(History(target).hops(ids["cmd"])[1]["name"], "run")

    def test_target_rules(self):
        make_state(self.state)
        self.synced()
        full = self.base / "full"
        full.mkdir()
        (full / "x").write_text("x")
        file_target = self.base / "file"
        file_target.write_text("x")
        for target in (full, file_target):
            with self.assertRaises(SecondBrainError):
                self.sb.restore(target)
        with self.assertRaises(SecondBrainError):
            self.sb.restore(self.base / "no" / "parent" / "t")
        self.assertEqual((full / "x").read_text(), "x")
        empty = self.base / "empty"
        empty.mkdir()
        self.assertTrue(self.sb.restore(empty)["ok"])
        self.assertTrue((empty / "memory.sqlite3").exists())
        self.assertEqual([p.name for p in self.base.iterdir() if ".restore-" in p.name], [])

    def test_restore_of_a_subset_of_stores(self):
        make_state(self.state)
        self.synced()
        target = self.base / "mem-only"
        report = self.sb.restore(target, stores=["memory"])
        self.assertTrue(report["ok"])
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["memory.sqlite3"])

    def test_authority_and_credentials_never_reach_the_vault(self):
        make_state(self.state)
        marks = {"vectors": "VECTORMARK", "audit": "AUDITMARK", "conf": "CONFMARK", "grant": "GRANTMARK",
                 "decision": "DECISIONMARK", "token": "TOKENMARK", "phone": "PHONEMARK"}
        with sqlite3.connect(self.state / "memory.sqlite3") as db:
            db.execute("INSERT INTO vectors VALUES('v1','m',?)", (marks["vectors"].encode(),))
        with sqlite3.connect(self.state / "history.sqlite3") as db:
            db.execute("INSERT INTO confirmation_audit(confirmation,at,origin,action,hash) VALUES(?,?,?,?,?)",
                       (marks["audit"], 1.0, "o", "a", "h"))
            db.execute("CREATE TABLE confirmations(id TEXT, note TEXT)")
            db.execute("INSERT INTO confirmations VALUES('c1',?)", (marks["conf"],))
            db.execute("CREATE TABLE site_grants(site TEXT, who TEXT)")
            db.execute("INSERT INTO site_grants VALUES('s',?)", (marks["grant"],))
            db.execute("CREATE TABLE decisions(id TEXT, why TEXT)")
            db.execute("INSERT INTO decisions VALUES('d',?)", (marks["decision"],))
        (self.state / "cluster-owner.token").write_text(marks["token"])
        (self.state / "phone.json").write_text(json.dumps({"key": marks["phone"]}))
        report = self.synced()
        self.assertTrue(report["complete"], report["unclassified_state"])
        blob = b"".join(p.read_bytes() for p in self.vault.rglob("*") if p.is_file())
        for name, mark in marks.items():
            self.assertNotIn(mark.encode(), blob, name)
        shutil.rmtree(self.state)
        target = self.base / "rebuilt"
        self.assertTrue(self.sb.restore(target)["ok"])
        db = sqlite3.connect(target / "history.sqlite3")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"confirmations", "site_grants", "decisions"}.isdisjoint(tables))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM confirmation_audit").fetchone()[0], 0)
        db.close()
        self.assertFalse((target / "phone.json").exists() or (target / "cluster-owner.token").exists())


# -- 10. tombstones ----------------------------------------------------------------------------
class Tombstones(Base):
    def test_tombstone_is_never_resurrected_by_sync(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        self.assertEqual(self.sb.tombstone(rid, "x"), {"ok": True, "status": "tombstoned"})
        self.assertRegex(self.note_for(rid).name, r"-r0002-[0-9a-f]{12}-t\.md$")
        self.assertEqual(self.sb.tombstone(rid, "again")["status"], "already-tombstoned")
        self.assertIsNone(self.sb.latest(rid))
        report = self.synced()
        self.assertEqual((report["written"], report["tombstoned_skipped"]), (0, 1))
        self.assertEqual(report["absent_from_source"], 0)
        target = self.base / "t"
        restore = self.sb.restore(target)
        self.assertEqual(restore["tombstoned"], 1)
        self.assertEqual({r[0] for r in dump(target)["items"]}, {ids["beta"]})
        # a changed source does NOT revive it: sync reports it, an owner action (not built) must
        Memory(self.state).merge_meta(ids["alpha"], {"back": 1})
        report = self.synced()
        self.assertEqual((report["written"], report["tombstoned_skipped"]), (0, 1))
        self.assertEqual(report["tombstoned_source_changed"], [rid])
        self.assertRegex(self.note_for(rid).name, r"-r0002-.*-t\.md$")
        self.assertIsNone(self.sb.latest(rid))
        self.assertTrue(self.sb.validate()["ok"])

    def test_unknown_record(self):
        make_state(self.state)
        self.synced()
        self.assertEqual(self.sb.tombstone("memory/memory-item/nope", "x"), {"ok": False, "status": "unknown-record"})
        self.assertEqual(len(self.notes()), len(self.payload_keys()))

    def test_deleted_source_rows_are_counted_but_not_propagated(self):
        ids = make_state(self.state)
        self.synced()
        with sqlite3.connect(self.state / "memory.sqlite3") as db:
            db.execute("DELETE FROM items WHERE id=?", (ids["beta"],))
        report = self.synced()
        self.assertEqual((report["absent_from_source"], report["written"]), (1, 0))
        rid = self.rid("memory-item", ids["beta"])
        self.assertEqual(self.sb.latest(rid)["fields"]["body"], "Beta body")
        target = self.base / "t"
        self.assertTrue(self.sb.restore(target)["ok"])
        self.assertIn(ids["beta"], {r[0] for r in dump(target)["items"]})


# -- 11. inventory -----------------------------------------------------------------------------
class Inventory(Base):
    def test_classification_and_unclassified_flag(self):
        self.state.mkdir()
        for name in ("memory.sqlite3", "vault.sqlite3", "x.lock", "cluster-owner.token", "phone.json",
                     "mystery.bin", "tasks.pruned", "events.jsonl"):
            (self.state / name).write_text("")
        (self.state / "plans").mkdir()
        got = {e["name"]: e["class"] for e in inventory(self.state)["entries"]}
        self.assertEqual(got["memory.sqlite3"], "captured")
        self.assertEqual(got["plans"], "captured")
        self.assertEqual(got["vault.sqlite3"], "excluded-derived")
        self.assertEqual(got["x.lock"], "excluded-operational")
        self.assertEqual(got["cluster-owner.token"], "excluded-credential")
        self.assertEqual(got["phone.json"], "excluded-credential")
        self.assertEqual(got["mystery.bin"], "unclassified")
        result = inventory(self.state)
        self.assertEqual((result["unclassified"], result["complete"]), (["mystery.bin"], False))

    def test_unknown_state_file_makes_the_run_incomplete_but_still_archives(self):
        make_state(self.state)
        (self.state / "surprise.dat").write_text("?")
        report = self.synced()
        self.assertFalse(report["complete"])
        self.assertEqual(report["unclassified_state"], ["surprise.dat"])
        self.assertGreater(report["written"], 0)
        self.assertNotIn(b"surprise", b"".join(p.read_bytes() for p in self.vault.rglob("*") if p.is_file()))


# -- 12. vault schema / Windows safety ---------------------------------------------------------
class VaultSchema(Base):
    RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

    def test_note_names_paths_and_frontmatter(self):
        make_state(self.state)
        self.synced()
        names = []
        for path in self.vault.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.vault))
            names.append(path.name)
            self.assertLessEqual(len(rel), 200, rel)
            self.assertNotRegex(rel, r'[:*?"<>|\\]')
            self.assertFalse(path.name.endswith((".", " ")))
            self.assertNotIn(path.name.split(".")[0].lower(), self.RESERVED)
        self.assertEqual(len(names), len(set(names)), "basenames must be unique across the vault")
        self.assertTrue(self.notes())
        for path in self.notes():
            self.assertRegex(path.name, r"^[a-z0-9._-]+$")
            text = path.read_text(encoding="utf-8")
            front = text.split("\n---\n", 1)[0]
            self.assertTrue(front.startswith("---\n"))
            for key in ("title", "type", "tags", "created", "updated"):
                self.assertRegex(front, rf"(?m)^{key}: ")
            self.assertIn("[[jarvis-records-index]]", text)

    def test_manifest_and_index_follow_the_vault_conventions(self):
        make_state(self.state)
        self.synced()
        manifests = list((self.area / "manifests").glob("*.md"))
        self.assertEqual(len(manifests), 1)
        text = manifests[0].read_text()
        self.assertTrue(text.startswith("---\ntitle: "))
        self.assertIn("[[jarvis-records-index]]", text)
        index = self.area / "jarvis-records-index.md"
        self.assertTrue(index.read_text().startswith("---\ntitle: "))
        stamp = (index.read_bytes(), index.stat().st_mtime_ns)
        Memory(self.state).add("note", "one more")
        self.synced()
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), stamp)   # created once
        self.assertEqual(len(list((self.area / "manifests").glob("*.md"))), 2)

    def test_manifest_filename_is_lowercase_kebab(self):
        make_state(self.state)
        self.synced()
        name = next((self.area / "manifests").glob("*.md")).name
        self.assertRegex(name, r"^[a-z0-9._-]+$")

    def test_a_humans_index_note_is_never_overwritten(self):
        self.area.mkdir(parents=True)
        human = self.area / "jarvis-records-index.md"
        human.write_text("---\ntitle: mine\n---\nHUMAN TEXT\n")
        make_state(self.state)
        self.synced()
        self.assertEqual(human.read_text(), "---\ntitle: mine\n---\nHUMAN TEXT\n")


# -- 13. import --------------------------------------------------------------------------------
class ImportHasNoSideEffects(unittest.TestCase):
    def test_import_creates_nothing_and_loads_no_stores(self):
        with tempfile.TemporaryDirectory() as cwd:
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import jarvis.second_brain; "
                    "bad = [m for m in ('jarvis.memory', 'jarvis.history', 'jarvis.store') if m in sys.modules]; "
                    "print(','.join(bad))")
            env = {**os.environ, "JARVIS_NO_MODELS": "1"}
            done = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT)], cwd=cwd, env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "")
            self.assertEqual(os.listdir(cwd), [])


if __name__ == "__main__":
    unittest.main()


# -- 14. integrity of "unchanged", restore choice, write hardening ------------------------------
class Integrity(Base):
    def test_unchanged_is_not_trusted_when_latest_note_is_damaged(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        note = self.note_for(rid)
        tamper_payload(note, "Alpha body", "Alpha b0dy")
        damaged = note.read_bytes()
        report = self.synced()
        self.assertEqual(report["repaired"], [rid])
        self.assertEqual(report["written"], 1)
        self.assertEqual(note.read_bytes(), damaged)            # never rewritten in place
        self.assertRegex(self.note_for(rid).name, r"-r0002-")
        self.assertTrue(self.sb.latest(rid)["fields"]["body"].startswith("Alpha body"))
        again = self.synced()
        self.assertEqual((again["written"], again["repaired"]), (0, []))
        target = self.base / "restored"   # latest revision is sound; only old r0001 is damaged
        self.assertTrue(self.sb.restore(target)["ok"])
        problems = [e["problem"] for e in self.sb.validate()["errors"]]
        self.assertEqual(problems, ["payload-hash-mismatch"])

    def test_unreadable_latest_note_is_repaired_not_trusted(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["beta"])
        note = self.note_for(rid)
        note.write_bytes(note.read_bytes()[:40])
        report = self.synced()
        self.assertEqual(report["repaired"], [rid])
        self.assertEqual(self.sb.latest(rid)["key"], ids["beta"])

    def test_duplicate_revision_number_blocks_sync_latest_and_restore(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        note = self.note_for(rid)
        twin = note.with_name(re.sub(r"-r0001-[0-9a-f]{12}", "-r0001-" + "f" * 12, note.name))
        twin.write_bytes(note.read_bytes())
        report = self.sb.sync(self.state)
        self.assertFalse(report["ok"])
        self.assertTrue(any("revision-gap-or-duplicate" in f["error"] for f in report["failures"]))
        with self.assertRaises(SecondBrainError):
            self.sb.latest(rid)
        target = self.base / "restored"
        self.assertFalse(self.sb.restore(target)["ok"])
        self.assertFalse(target.exists())
        loose = self.sb.restore(target, strict=False)
        self.assertTrue(loose["partial"])
        self.assertNotIn(ids["alpha"], {r[0] for r in dump(target)["items"]})

    def test_gap_in_revisions_blocks_restore_of_that_record(self):
        ids = make_state(self.state)
        self.synced()
        Memory(self.state).merge_meta(ids["alpha"], {"v": 2})
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        self.note_for(rid, 1).unlink()
        self.assertFalse(self.sb.restore(self.base / "r1")["ok"])

    def test_notes_are_owner_only(self):
        if os.name == "nt":
            self.skipTest("POSIX modes")
        make_state(self.state)
        self.synced()
        for path in self.notes():
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)

    def test_no_hardlink_fallback_creates_exclusively_and_never_clobbers(self):
        ids = make_state(self.state)
        with patch.object(os, "link", side_effect=OSError(errno.EPERM, "no links")):
            self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        note = self.note_for(rid)
        self.assertTrue(self.sb.validate()["ok"])
        before = note.read_bytes()
        shard = note.parent
        with patch.object(os, "link", side_effect=OSError(errno.EPERM, "no links")):
            with self.assertRaises(RevisionExists):
                self.sb._write_new(shard.relative_to(self.area).parts, note.name, b"intruder")
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual([p for p in shard.iterdir() if p.name.startswith(".jr-tmp-")], [])

    def test_revision_claim_blocks_second_writer_and_stale_claim_is_broken(self):
        ids = make_state(self.state)
        self.synced()
        rid = self.rid("memory-item", ids["alpha"])
        stem = SecondBrain.stem_for(rid)[0]
        shard = self.note_for(rid).parent
        lock = shard / f".jr-tmp-lock-{stem}"
        lock.write_bytes(b"")
        Memory(self.state).merge_meta(ids["alpha"], {"v": 2})
        busy = self.sb.sync(self.state)
        self.assertFalse(busy["ok"])
        self.assertTrue(lock.exists())                         # a live claim is not stolen
        old = lock.stat().st_mtime - sbmod.CLAIM_STALE_S - 10
        os.utime(lock, (old, old))
        self.assertTrue(self.synced()["written"] >= 1)         # crashed writer's claim is broken
        self.assertFalse(lock.exists())
