"""Second-brain archive: JARVIS personal records <-> additive Markdown in the Obsidian vault.

STATUS: this module is the storage engine. Nothing here runs on import or reads config. It is
wired in by `second_brain_queue.py` (C2 write-through outbox for memory/history/tasks) and
`second_brain_runtime.py` (the drainer the voice daemon starts, the scoped file sync for
plans/runs/knowledge, and `jarvis archive status|sync|drain|verify|validate|restore`).
SQLite/JSON stays the source for every caller except one opt-in read.
`iter_canonical_records()` yields one verified latest live payload at a time;
`canonical_records()` is that iterator collected into a list. `vault.recall` streams the
iterator only when `second_brain.canonical_read` is literal true and `vault.enabled` /
`vault.recall` are not literal false. That path is not a cutover. `parity()`, `validate()`
and `restore()` still prove the archive can rebuild.

What it does
- Streams personal rows/files from a per-store consistent snapshot (sqlite backup API into a
  temp file, never a plain copy of the live DB) into *immutable revision notes* under
  `<vault>/reference/jarvis-records/`.
- One record = one stable identity (`store/kind/key`). An unchanged source writes nothing; a
  changed source appends revision N+1. Existing notes are never opened for writing.
- `validate()` checks the archive; `restore()` rebuilds memory/history/tasks (+ plans, runs,
  knowledge files) into a NEW empty state directory from the notes alone.

What it does not do (see docs/design/mobile-continuity/OBSIDIAN-STORAGE-IMPLEMENTATION.md)
- No fleet checkpoint: each store is snapshotted separately, at its own instant.
- No deletion propagation. Source pruning (retention) is not deletion; the vault keeps the
  last revision. `tombstone()` is an explicit owner-intent marker only.
- No credentials, tokens, locks, live confirmations/grants, receipts, embeddings or caches.
  `inventory()` lists every state entry and whether it is captured, excluded, or unclassified.
- Restoring records never restores authority: queued/running tasks come back `interrupted`
  and running plans come back `blocked`.
"""

import base64
import errno
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path

SCHEMA = "jarvis-record/1"
AREA = ("reference", "jarvis-records")
INDEX_NOTE = "jarvis-records-index"
MAX_RECORD_BYTES = 16 * 1024 * 1024   # payload bytes (> Memory's 2M-char cap at 4 B/char); larger: reported, never truncated
# Overflow (opt-in, owner policy): a payload above MAX_RECORD_BYTES but within the overflow cap is
# published as immutable chunk files next to its note; the note's payload block then holds a
# hash-checked manifest. Readers always accept chunked notes up to MAX_OVERFLOW_BYTES. Writers
# produce them only when this brain's cap allows it: max_overflow_bytes, or overflow_by_store for
# the one brain sync/verify share across ledgers. A damaged chunk is never rewritten; a repair is
# the next revision and the old bytes stay as evidence.
CHUNK_BYTES = 4 * 1024 * 1024        # one chunk file
MAX_OVERFLOW_BYTES = 64 * 1024 * 1024  # hard ceiling for any chunked payload (writer and reader)
CHUNK_FORMAT = "jarvis-record-chunks/1"
BATCH = 500                          # rows fetched at a time from a snapshot
TMP_PREFIX = ".jr-tmp-"
CLAIM_STALE_S = 300   # a revision lock older than this belonged to a crashed writer
MAX_REL_PATH = 200                   # vault-relative path budget (Windows MAX_PATH is 260 total)
TERMINAL_TASK_STATES = ("finished", "failed", "timed_out", "interrupted")

NOTE_RE = re.compile(r"^(?P<stem>jr-[a-z0-9-]+-(?P<rid>[0-9a-f]{12}))-r(?P<rev>\d{4,})-"
                     r"(?P<payload>[0-9a-f]{12})(?P<tomb>-t)?\.md$")
CHUNK_RE = re.compile(r"^jrc-(?P<rid>[0-9a-f]{12})-r(?P<rev>\d{4,})-(?P<idx>\d{4})-(?P<sha>[0-9a-f]{12})\.jrc$")
PLAN_RE = re.compile(r"plan-[0-9a-f]{8}\.json")
SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
RUN_SUFFIXES = (".md", ".json", ".jsonl", ".txt")
SECRET_RES = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bsk-[A-Za-z0-9_-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}",
    r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+=]{16,}",
)]

# Every top-level state entry, classified. First match wins. `captured` entries are exported
# (with the per-table/per-file policy noted); everything else is deliberately not.
INVENTORY_RULES = [
    ("memory.sqlite3", "captured", "items, links; excludes vectors (rebuildable) and the FTS index"),
    ("history.sqlite3", "captured", "commands (+hops); excludes confirmations, confirmation_audit, "
                                    "site_grants (live authority/receipts) and decisions (telemetry)"),
    ("tasks.sqlite3", "captured", "tasks (+events)"),
    ("plans", "captured", "plan-*.json files"),
    ("runs", "captured", "per-task top-level .md/.json/.jsonl/.txt; excludes *.log and subfolders"),
    ("knowledge", "captured", "raw/, wiki/, checks/, bots/, engine.json, log.jsonl; excludes .lock"),
    ("vault.sqlite3", "excluded-derived", "search cache rebuilt from the vault"),
    ("cluster.sqlite3", "excluded-operational", "fleet jobs/nodes: execution state, not personal records"),
    ("cluster-*", "excluded-credential", "owner token / main lock"),
    ("code-signing.key*", "excluded-credential", "release signing key"),
    ("chrome-extension-token", "excluded-credential", "extension token"),
    ("phone.json", "excluded-credential", "phone link credentials"),
    ("*.lock", "excluded-operational", "process lock"),
    ("*.pid", "excluded-operational", "process id"),
    ("*.code", "excluded-credential", "pairing code"),
    ("locations.json", "excluded-unreviewed-personal", "personal location data; needs an owner decision"),
    ("camera.json", "excluded-unreviewed-personal", "needs an owner decision"),
    ("voice", "excluded-biometric", "voiceprints, models and enrolment audio"),
    ("audio", "excluded-media", "media"),
    ("*.jpg", "excluded-media", "media"),
    ("*.png", "excluded-media", "media"),
    ("browser-output", "excluded-media", "browser downloads and captures"),
    ("browser-profile", "excluded-credential", "browser profile (cookies/sessions)"),
    ("agent-browser-profile", "excluded-credential", "browser profile (cookies/sessions)"),
    ("app-window", "excluded-operational", "window capture cache"),
    ("code-release", "excluded-operational", "release staging"),
    ("code-release-failed.json", "excluded-operational", "release validation diagnostic"),
    ("events.jsonl", "excluded-operational", "event telemetry stream"),
    ("logs", "excluded-operational", "diagnostic logs"),
    ("eval", "excluded-derived", "scorecard output"),
    ("inventory", "excluded-operational", "hardware inventory"),
    ("clipboard*", "excluded-operational", "clipboard sync state"),
    ("lanmouse-*", "excluded-operational", "input-sharing guard state"),
    ("bluetooth-seen.json", "excluded-operational", "device sightings"),
    ("brain-budget.json", "excluded-operational", "quota counters"),
    ("tasks.pruned", "excluded-operational", "prune stamp"),
    ("archive-capture.json", "excluded-operational", "write-through capture marker (second_brain_queue)"),
    ("archive-files.json", "excluded-operational", "file-store archive sync status (second_brain_runtime)"),
]


class SecondBrainError(Exception):
    """Setup problem (bad vault, bad target). Per-record problems go in the report instead."""


class RevisionExists(SecondBrainError):
    """Another writer created this revision first."""


def _now():
    return datetime.now(timezone.utc)


def _slug(text, limit):
    text = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:limit].strip("-")
    return text or "x"


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _canonical(obj):
    """Deterministic JSON line (sorted keys, no spaces). ASCII fallback if text can't be UTF-8."""
    try:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return text


def _jsonable(value):
    return {"$b64": base64.b64encode(value).decode("ascii")} if isinstance(value, (bytes, bytearray)) else value


def _row(row):
    return {key: _jsonable(row[key]) for key in row.keys()}


# store -> the record kinds an archive revision of that store may carry (publish() validates it).
KINDS = {"memory": ("memory-item", "memory-links"), "history": ("history-command",), "tasks": ("task",),
         "plans": ("plan",), "runs": ("run-file",), "knowledge": ("knowledge-file",)}


def build_payload(store, kind, key, source, fields, children=None):
    """The one place a record payload is shaped: sync() and the write-through queue share it, so
    the same row state always renders to the same bytes (and so to 'unchanged')."""
    obj = {"schema": SCHEMA, "store": store, "kind": kind, "key": str(key),
           "source": source, "fields": fields}
    if children:
        obj["children"] = children
    return obj


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _strings(value)


def looks_secret(obj):
    return any(rx.search(text) for text in _strings(obj) for rx in SECRET_RES)


def excluded(obj):
    """Owner said this record stays PC-only: a `no-archive` tag or meta {"archive": false} on a
    memory item. Such records are never queued or written (earlier revisions, if any, stay)."""
    if obj.get("kind") != "memory-item":
        return False
    f = obj.get("fields") or {}
    if "no-archive" in str(f.get("tags") or "").split():
        return True
    try:
        meta = json.loads(f.get("meta") or "{}")
    except ValueError:
        return False
    return isinstance(meta, dict) and meta.get("archive") is False


# fields that never change after a record is created (checked against the writers in memory.py,
# store.py, history.py): if they differ between revisions, two DIFFERENT records (e.g. two installs
# drawing the same short id) are being merged into one history. Mutable columns are not listed.
IDENTITY = {"memory-item": ("created_at", "body"), "task": ("created_at", "spec"),
            "history-command": ("at", "request")}


def safe_relpath(store, rel):
    """True if `rel` is an acceptable file key for a file-backed store (also used on restore)."""
    parts = str(rel).split("/")
    if not parts or any(not SEGMENT_RE.fullmatch(p) or p in (".", "..") for p in parts):
        return False
    if store == "plans":
        return len(parts) == 1 and bool(PLAN_RE.fullmatch(parts[0]))
    if store == "runs":
        return len(parts) == 2 and parts[1].endswith(RUN_SUFFIXES)
    if store == "knowledge":
        return len(parts) <= 3
    return False


def inventory(state_dir):
    """Classify every top-level state entry. `complete` is False while any is unclassified."""
    entries = []
    for entry in sorted(os.listdir(state_dir)):
        for pattern, kind, why in INVENTORY_RULES:
            if fnmatch.fnmatch(entry, pattern):
                entries.append({"name": entry, "class": kind, "reason": why})
                break
        else:
            entries.append({"name": entry, "class": "unclassified", "reason": "no policy yet"})
    return {"entries": entries,
            "unclassified": [e["name"] for e in entries if e["class"] == "unclassified"],
            "complete": all(e["class"] != "unclassified" for e in entries)}


class SecondBrain:
    def __init__(self, vault_root, max_record_bytes=MAX_RECORD_BYTES, clock=_now, max_overflow_bytes=0,
                 overflow_by_store=None):
        self.root = Path(vault_root).expanduser().resolve()
        self.max_record_bytes = int(max_record_bytes)
        # 0 = never write chunked notes (default). Clamped to the hard ceiling.
        self.max_overflow_bytes = max(0, min(int(max_overflow_bytes or 0), MAX_OVERFLOW_BYTES))
        self.overflow_by_store = {}
        for store, value in (overflow_by_store or {}).items():
            self.overflow_by_store[str(store)] = max(0, min(int(value or 0), MAX_OVERFLOW_BYTES))
        self.clock = clock
        self.area_parts = AREA

    def _overflow_for(self, store):
        """Bytes this store may publish as chunks. A per-store policy wins over the brain default,
        including an explicit 0 (off) for that store."""
        value = self.overflow_by_store.get(store, self.max_overflow_bytes)
        return max(0, min(int(value or 0), MAX_OVERFLOW_BYTES))

    # -- vault access ----------------------------------------------------------------------
    @property
    def area(self):
        return self.root.joinpath(*self.area_parts)

    def check_vault(self):
        """{ok, reason}. Never creates anything."""
        if not self.root.is_dir():
            return {"ok": False, "reason": "vault-unavailable: root is not a directory"}
        if not os.access(self.root, os.W_OK | os.X_OK):
            return {"ok": False, "reason": "vault-unavailable: root is not writable"}
        cur = self.root
        for part in self.area_parts:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(st.st_mode) or getattr(os.path, "isjunction", lambda p: False)(cur) \
                    or not stat.S_ISDIR(st.st_mode):
                return {"ok": False, "reason": f"unsafe-path: {part} is a link or not a directory"}
        return {"ok": True, "reason": ""}

    def _dir(self, parts):
        """Create/verify each component under the vault root; reject links and non-directories."""
        cur = self.root
        for part in parts:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part) or ".." in part:
                raise SecondBrainError(f"unsafe path component {part!r}")
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur)
                st = os.lstat(cur)
            if stat.S_ISLNK(st.st_mode) or getattr(os.path, "isjunction", lambda p: False)(cur) \
                    or not stat.S_ISDIR(st.st_mode):
                raise SecondBrainError(f"{part} is a link or not a directory")
        return cur

    def _claim(self, folder, stem, rev):
        """Serialise writers of one record's next revision: an O_EXCL lock file (named like a
        temp file, so scans ignore it and sweep_stale_tmp clears crash leftovers), then refuse
        if any revision >= `rev` already exists. The payload hash is in the note name, so
        os.link alone only stops identical content."""
        lock = folder / f"{TMP_PREFIX}lock-{stem}"
        for attempt in (0, 1):
            try:
                os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644))
                break
            except FileExistsError:
                try:
                    stale = time.time() - os.lstat(lock).st_mtime > CLAIM_STALE_S
                except OSError:
                    stale = True
                if attempt or not stale:
                    raise SecondBrainError("another writer holds this record's revision claim; retry") from None
                try:
                    os.unlink(lock)   # crashed writer
                except FileNotFoundError:
                    pass
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    m = NOTE_RE.match(entry.name)
                    if m and m["stem"] == stem and int(m["rev"]) >= rev:
                        raise RevisionExists(f"{stem}-r{rev:04d}")
        except BaseException:
            self._unlock(lock)
            raise
        return lock

    @staticmethod
    def _unlock(lock):
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass

    def _write_new(self, parts, name, data, chunks=()):
        """Create `name` exclusively (never replace). Temp file + hard link: readers never see a
        partial note, and an existing note is never touched. A revision note name also takes the
        record's revision claim, so a second writer cannot take the same number with different
        content. `chunks` [(name, bytes)] of an overflow note are written first, under the same
        claim: the note (the only thing scans see) appears only after every chunk is durable."""
        lock, note = None, NOTE_RE.match(name)
        if note:
            lock = self._claim(self._dir(self.area_parts + tuple(parts)), note["stem"], int(note["rev"]))
        elif chunks:
            raise SecondBrainError("chunks belong to a revision note")
        try:
            for chunk_name, blob in chunks:
                self._write_chunk(parts, chunk_name, blob)
            return self._write_new_locked(parts, name, data)
        finally:
            if lock:
                self._unlock(lock)

    def _write_new_locked(self, parts, name, data):
        rel = "/".join(self.area_parts + tuple(parts) + (name,))
        if len(rel) > MAX_REL_PATH:
            raise SecondBrainError("path too long for a Windows vault")
        folder = self._dir(self.area_parts + tuple(parts))
        final, tmp = folder / name, folder / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            self._write_file(tmp, data)
            try:
                os.link(tmp, final)
            except FileExistsError:
                raise RevisionExists(name) from None
            except (OSError, NotImplementedError, AttributeError) as exc:
                if isinstance(exc, OSError) and exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV,
                                                                   errno.EACCES, errno.EINVAL):
                    raise
                # No hard links on this filesystem. os.rename can replace on POSIX, so create the
                # final name exclusively instead. A crash mid-write leaves a damaged note at a
                # revision name: validate flags it and the next sync appends a repair revision.
                try:
                    self._write_file(final, data)
                except FileExistsError:
                    raise RevisionExists(name) from None
            self._fsync_dir(folder)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return final

    def _write_chunk(self, parts, name, blob):
        """Immutable chunk: created once. A file already at this name (a crashed earlier attempt
        at the same revision) is reused only if it holds exactly these bytes."""
        try:
            self._write_new_locked(parts, name, blob)
        except RevisionExists:
            path = self.area.joinpath(*parts) / name
            have = self._read_chunk(path, len(blob))
            if have is None or hashlib.sha256(have).digest() != hashlib.sha256(blob).digest():
                raise SecondBrainError(f"chunk-corrupt: {name} exists with other bytes; owner must resolve") from None

    @staticmethod
    def _read_chunk(path, size):
        """Bytes of a regular chunk file of exactly `size` bytes (<= CHUNK_BYTES), else None."""
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size != size or size > CHUNK_BYTES:
                return None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(size + 1)
        except OSError:
            return None
        return data if len(data) == size else None

    @staticmethod
    def _write_file(path, data):
        """Exclusive create, owner-only (personal archive), flushed to disk."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.unlink(path)   # our own half-written file
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_dir(folder):
        """Make the new directory entry durable (POSIX). Windows cannot open a directory: skipped."""
        if os.name == "nt":
            return
        try:
            fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _read_note(self, path):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise SecondBrainError("not a regular file")
        if st.st_size > self.max_record_bytes * 4 + 65536:
            raise SecondBrainError("note larger than the record bound")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            return stream.read()

    # -- archive index (filenames only) ------------------------------------------------------
    def _scan(self):
        """{stem: {rev, payload, tomb, name, dir}} for the newest revision of each record, plus
        every revision list and anomalies. Reads directory entries only, skips links."""
        latest, revisions, anomalies, tmp = {}, {}, [], []
        if not self.area.is_dir() or os.path.islink(self.area):
            return latest, revisions, anomalies, tmp
        stack = [self.area]
        while stack:
            folder = stack.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        anomalies.append({"path": entry.path, "problem": "symlink-skipped"})
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.name.startswith(TMP_PREFIX):
                        tmp.append(entry.path)
                    else:
                        match = NOTE_RE.match(entry.name)
                        if not match:
                            continue   # human notes (index, manifests) live here too
                        rev = int(match["rev"])
                        info = {"stem": match["stem"], "rev": rev, "payload": match["payload"],
                                "tomb": bool(match["tomb"]), "name": entry.name, "dir": folder}
                        revisions.setdefault(match["stem"], []).append(info)
                        if match["stem"] not in latest or rev > latest[match["stem"]]["rev"]:
                            latest[match["stem"]] = info
        for stem, revs in revisions.items():   # duplicate or missing revision numbers: no safe "latest"
            if sorted(r["rev"] for r in revs) != list(range(1, len(revs) + 1)):
                latest[stem]["conflict"] = True
        return latest, revisions, anomalies, tmp

    def sweep_stale_tmp(self, older_than_s=3600):
        """Remove leftover `.jr-tmp-*` files (crashed writers) in the archive area only."""
        removed, cutoff = 0, self.clock().timestamp() - older_than_s
        for path in self._scan()[3]:
            try:
                if os.lstat(path).st_mtime < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        return removed

    # -- records -----------------------------------------------------------------------------
    @staticmethod
    def record_id(store, kind, key):
        return f"{store}/{kind}/{key}"

    @staticmethod
    def stem_for(record_id):
        store, kind, key = record_id.split("/", 2)
        digest = _sha(record_id)[:12]
        return f"jr-{_slug(kind, 20)}-{_slug(key, 24)}-{digest}", store, kind, digest

    def _payload(self, store, kind, key, source, fields, children=None):
        return build_payload(store, kind, key, source, fields, children)

    def _render(self, obj, rev, prev, run, stamp, tomb=False, chunks_out=None):
        """-> (note bytes, full payload sha256). Over max_record_bytes the payload goes to chunk
        files (appended to `chunks_out` as (name, bytes)) when the caller accepts chunks and the
        overflow policy allows the size; otherwise ValueError('oversize: ...')."""
        line = _canonical(obj)
        payload = line.encode("utf-8")
        digest = _sha(payload)
        rid = self.record_id(obj["store"], obj["kind"], obj["key"])
        chunk_meta = []
        if len(payload) > self.max_record_bytes:
            if chunks_out is None or tomb or len(payload) > self._overflow_for(obj["store"]):
                raise ValueError(f"oversize: {len(payload)} bytes")
            manifest = self._split(payload, digest, rid, rev, chunks_out)
            line = _canonical(manifest)
            chunk_meta = ["jarvis_payload_chunked: true", f"jarvis_payload_bytes: {len(payload)}",
                          f"jarvis_manifest_sha256: {_sha(line)}"]
        label = _slug(obj["store"], 20)
        fields = obj.get("fields") or {}
        text = str(fields.get("title") or fields.get("text") or fields.get("relpath") or "")
        title = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:80]
        heading = re.sub(r"[\x00-\x1f\x7f]+", " ", f"JARVIS {obj['kind']} {obj['key'][:60]}") \
            + (f": {title}" if title else "")
        preview = "" if tomb else str(fields.get("body") or fields.get("text") or fields.get("content")
                                       or fields.get("request") or "")[:1500]
        day = stamp.strftime("%Y-%m-%d")
        body_lines = [
            f"# {heading}", "",
            f"Archived by JARVIS from `{obj['source']}`. See [[{INDEX_NOTE}]]."
            + (f" Previous revision: [[{prev}]]." if prev else ""), "",
            f"- record: `{rid}`", f"- revision: {rev}" + (" (tombstone)" if tomb else ""), ""]
        if preview:
            fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", preview)), default=0) + 1)
            body_lines += ["## Preview (first 1500 characters, read-only view)", "", fence, preview, fence, ""]
        if chunk_meta:
            body_lines += [f"Payload of {len(payload)} bytes kept in {len(manifest['chunks'])} `.jrc` chunk files "
                           "next to this note; the block below is their hash-checked manifest.", ""]
        body_lines += ["## Payload (authoritative; edits here void restore for this revision)",
                       "<!-- jarvis-payload-begin -->", "```json", line, "```", "<!-- jarvis-payload-end -->", ""]
        body = "\n".join(body_lines)
        meta = [
            "---", f"title: {json.dumps(heading, ensure_ascii=False)}", "type: reference",
            f"tags: [reference, jarvis-record, jarvis-{label}]", f"created: {day}", f"updated: {day}",
            f"jarvis_schema: {SCHEMA}", f"jarvis_record: {json.dumps(rid, ensure_ascii=False)}",
            f"jarvis_revision: {rev}", f"jarvis_payload_sha256: {digest}", *chunk_meta,
            f"jarvis_body_sha256: {_sha(body)}",
            f"jarvis_prev: {json.dumps(prev)}", f"jarvis_tombstone: {'true' if tomb else 'false'}",
            f"jarvis_run: {run}", f"jarvis_synced_at: {stamp.isoformat(timespec='seconds')}", "---", ""]
        return ("\n".join(meta) + body).encode("utf-8"), digest

    @staticmethod
    def _split(payload, digest, rid, rev, chunks_out):
        """Cut a payload into CHUNK_BYTES pieces; -> manifest. Names carry record, revision,
        index and chunk hash, so a chunk can belong to exactly one revision of one record."""
        rid12, view, entries = _sha(rid)[:12], memoryview(payload), []
        for idx, off in enumerate(range(0, len(payload), CHUNK_BYTES)):
            blob = bytes(view[off:off + CHUNK_BYTES])
            csha = _sha(blob)
            name = f"jrc-{rid12}-r{rev:04d}-{idx:04d}-{csha[:12]}.jrc"
            chunks_out.append((name, blob))
            entries.append({"name": name, "bytes": len(blob), "sha256": csha})
        return {"format": CHUNK_FORMAT, "bytes": len(payload), "sha256": digest, "chunks": entries}

    def _load(self, path):
        """THE note reader: parse_note plus, for a chunked note, every chunk read back, bounded and
        verified against the manifest and the frontmatter. Raises ValueError on any doubt."""
        path = Path(path)
        parsed = self.parse_note(self._read_note(path))
        if parsed.get("chunked"):
            text = self._assemble(path, parsed)
            parsed.update(payload_text=text, obj=json.loads(text), payload_ok=True)
        return parsed

    def _assemble(self, path, parsed):
        def bad(why):
            raise ValueError(f"chunked-payload: {why}")
        note, meta, man = NOTE_RE.match(path.name), parsed["meta"], parsed["manifest"]
        if not note:
            bad("note name")
        if not parsed["manifest_ok"]:
            bad("manifest hash mismatch")
        if not isinstance(man, dict) or man.get("format") != CHUNK_FORMAT:
            bad("unknown manifest format")
        total, full, items = man.get("bytes"), man.get("sha256"), man.get("chunks")
        if type(total) is not int or not 0 < total <= MAX_OVERFLOW_BYTES:
            bad("size out of bounds")
        if full != meta.get("jarvis_payload_sha256") or meta.get("jarvis_payload_bytes") != total:
            bad("manifest disagrees with frontmatter")
        if not isinstance(items, list) or len(items) != -(-total // CHUNK_BYTES):
            bad("chunk count")
        whole, seen = hashlib.sha256(), set()
        buf = bytearray()
        for idx, item in enumerate(items):
            name = item.get("name") if isinstance(item, dict) else None
            m = CHUNK_RE.match(name) if isinstance(name, str) else None
            csha, size = (item.get("sha256"), item.get("bytes")) if m else (None, None)
            if (not m or name in seen or m["rid"] != note["rid"] or m["rev"] != note["rev"]
                    or int(m["idx"]) != idx or not isinstance(csha, str) or csha[:12] != m["sha"]
                    or size != min(CHUNK_BYTES, total - idx * CHUNK_BYTES)):
                bad(f"chunk {idx} entry")
            seen.add(name)
            blob = self._read_chunk(path.parent / name, size)
            if blob is None:
                bad(f"chunk {idx} missing or truncated")
            if _sha(blob) != csha:
                bad(f"chunk {idx} hash mismatch")
            whole.update(blob)
            buf += blob
        if whole.hexdigest() != full:
            bad("payload hash mismatch")
        return buf.decode("utf-8")

    @staticmethod
    def parse_note(data):
        """-> dict(meta, payload_text, obj, payload_ok, body_ok). Raises ValueError if unreadable."""
        text = data.decode("utf-8")
        lines = text.split("\n")
        if not lines or lines[0].rstrip("\r") != "---":
            raise ValueError("no frontmatter")
        end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---"), None)
        if end is None:
            raise ValueError("unterminated frontmatter")
        meta = {}
        for line in lines[1:end]:
            m = re.match(r"^(jarvis_[a-z0-9_]+): (.*?)\r?$", line)
            if m:
                try:
                    meta[m[1]] = json.loads(m[2])
                except ValueError:
                    meta[m[1]] = m[2]
        body = "\n".join(lines[end + 1:])
        try:
            # the real block is last: a preview of user text could contain a look-alike sentinel
            start = max(i for i, l in enumerate(lines) if l.rstrip("\r") == "<!-- jarvis-payload-begin -->")
            stop = max(i for i, l in enumerate(lines) if l.rstrip("\r") == "<!-- jarvis-payload-end -->")
            payload_line = lines[start + 2].rstrip("\r")
            if lines[start + 1].rstrip("\r") != "```json" or stop <= start + 2:
                raise ValueError
        except (ValueError, IndexError):
            raise ValueError("payload block missing or damaged") from None
        obj = json.loads(payload_line)
        body_ok = _sha(body) == meta.get("jarvis_body_sha256")
        if meta.get("jarvis_payload_chunked") is True:
            # payload lives in chunk files: only SecondBrain._load can verify and assemble it
            return {"meta": meta, "payload_text": None, "obj": None, "payload_ok": False, "chunked": True,
                    "manifest": obj, "manifest_ok": _sha(payload_line) == meta.get("jarvis_manifest_sha256"),
                    "body_ok": body_ok}
        payload_ok = _sha(payload_line) == meta.get("jarvis_payload_sha256")
        return {"meta": meta, "payload_text": payload_line, "obj": obj, "payload_ok": payload_ok,
                "chunked": False, "body_ok": body_ok}

    # -- sources -------------------------------------------------------------------------------
    def _snapshot(self, db_path, workdir):
        """Transactionally consistent copy via the sqlite backup API (read-only source)."""
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            dest = sqlite3.connect(Path(workdir) / f"{uuid.uuid4().hex}.sqlite3")
            try:
                src.backup(dest)
            except Exception:
                dest.close()
                raise
        finally:
            src.close()
        dest.row_factory = sqlite3.Row
        return dest

    @staticmethod
    def _stream(db, sql, args=()):
        cur = db.execute(sql, args)
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                return
            yield from rows

    @staticmethod
    def _has(db, table):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _memory_records(self, db, counts):
        if self._has(db, "items"):
            for r in self._stream(db, "SELECT * FROM items ORDER BY rowid"):
                counts["items"] = counts.get("items", 0) + 1
                yield "memory-item", r["id"], self._payload("memory", "memory-item", r["id"],
                                                            "memory.sqlite3#items", _row(r))
        if self._has(db, "links"):
            rows = self._stream(db, "SELECT src,dst,rel,created_at FROM links ORDER BY src,dst,rel")
            for src, group in groupby(rows, key=lambda r: r["src"]):
                links = [_row(r) for r in group]
                counts["links"] = counts.get("links", 0) + len(links)
                yield "memory-links", src, self._payload("memory", "memory-links", src,
                                                         "memory.sqlite3#links", {"src": src, "links": links})

    def _history_records(self, db, counts):
        if not self._has(db, "commands"):
            return
        has_hops = self._has(db, "hops")
        for r in self._stream(db, "SELECT * FROM commands ORDER BY at, id"):
            counts["commands"] = counts.get("commands", 0) + 1
            hops = []
            if has_hops:
                hops = [_row(h) for h in db.execute(
                    "SELECT command,name,ms,state FROM hops WHERE command=? ORDER BY name", (r["id"],))]
                counts["hops"] = counts.get("hops", 0) + len(hops)
            yield "history-command", r["id"], self._payload(
                "history", "history-command", r["id"], "history.sqlite3#commands", _row(r), {"hops": hops})

    def _task_records(self, db, counts):
        if not self._has(db, "tasks"):
            return
        has_events = self._has(db, "events")
        for r in self._stream(db, "SELECT * FROM tasks ORDER BY created_at, id"):
            counts["tasks"] = counts.get("tasks", 0) + 1
            events = []
            if has_events:
                events = [_row(e) for e in db.execute(
                    "SELECT seq,task_id,at,kind,detail FROM events WHERE task_id=? ORDER BY seq", (r["id"],))]
                counts["events"] = counts.get("events", 0) + len(events)
            yield "task", r["id"], self._payload("tasks", "task", r["id"], "tasks.sqlite3#tasks",
                                                  _row(r), {"events": events})

    def _file_record(self, store, kind, key, path, source, skipped):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > self.max_record_bytes // 2:   # base64/JSON headroom
            skipped.append({"record": self.record_id(store, kind, key), "reason": "oversize",
                            "bytes": st.st_size})
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(self.max_record_bytes + 1)
        try:
            content, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(raw).decode("ascii"), "base64"
        fields = {"relpath": key, "size": len(raw), "sha256": _sha(raw), "encoding": encoding,
                  "content": content}
        return self._payload(store, kind, key, source, fields)

    def _walk_files(self, store, base, rel_filter):
        """Yield (key, path) for regular, non-link files directly selected by the policy."""
        if not base.is_dir() or os.path.islink(base):
            return
        stack = [(base, ())]
        while stack:
            folder, prefix = stack.pop()
            with os.scandir(folder) as entries:
                listing = sorted(entries, key=lambda e: e.name)
            for entry in listing:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if len(prefix) < 2:
                        stack.append((Path(entry.path), prefix + (entry.name,)))
                elif entry.is_file(follow_symlinks=False):
                    key = "/".join(prefix + (entry.name,))
                    if rel_filter(key):
                        yield key, Path(entry.path)

    def _plan_records(self, state, counts, skipped):
        for key, path in self._walk_files("plans", state / "plans", lambda k: safe_relpath("plans", k)):
            rec = self._file_record("plans", "plan", key, path, "plans/", skipped)
            if rec:
                counts["plans"] = counts.get("plans", 0) + 1
                yield "plan", key, rec

    def _run_records(self, state, counts, skipped):
        for key, path in self._walk_files("runs", state / "runs", lambda k: safe_relpath("runs", k)):
            rec = self._file_record("runs", "run-file", key, path, "runs/", skipped)
            if rec:
                counts["files"] = counts.get("files", 0) + 1
                yield "run-file", key, rec

    def _knowledge_records(self, root, counts, skipped):
        def want(key):
            parts = key.split("/")
            if not safe_relpath("knowledge", key):
                return False
            if len(parts) == 1:
                return key in ("engine.json", "log.jsonl")
            return parts[0] in ("raw", "wiki", "checks", "bots") and len(parts) == 2
        for key, path in self._walk_files("knowledge", root, want):
            rec = self._file_record("knowledge", "knowledge-file", key, path, "knowledge/", skipped)
            if rec:
                counts["files"] = counts.get("files", 0) + 1
                yield "knowledge-file", key, rec

    # -- sync ----------------------------------------------------------------------------------
    STORES = ("memory", "history", "tasks", "plans", "runs", "knowledge")

    def sync(self, state_dir, stores=None, knowledge_dir=None, dry_run=False, allow_secret_like=()):
        """Archive state into the vault. Returns a report; never raises for per-record trouble.

        Safe to rerun: unchanged records write nothing. Source data is only read (through a
        snapshot). Any vault failure is listed in report['failures'] and the same call can be
        repeated later. Reports hold record ids and counts, never record bodies."""
        started = self.clock()
        run = uuid.uuid4().hex[:12]
        wanted = tuple(stores or self.STORES)
        report = {"ok": True, "complete": False, "run": run, "started": started.isoformat(timespec="seconds"),
                  "dry_run": bool(dry_run), "stores": {}, "written": 0, "unchanged": 0,
                  "tombstoned_skipped": 0, "tombstoned_source_changed": [], "repaired": [], "skipped": [], "failures": [], "absent_from_source": 0,
                  "bytes": 0, "warnings": [],
                  "scope": "per-store snapshots at different instants; not an atomic fleet checkpoint"}
        bad = [s for s in wanted if s not in self.STORES]
        if bad:
            raise SecondBrainError(f"unknown stores: {bad}")
        status = self.check_vault()
        if not status["ok"]:
            report.update(ok=False, error=status["reason"])
            return report
        state = Path(state_dir).expanduser().resolve()
        if not state.is_dir():
            report.update(ok=False, error="state-unavailable")
            return report
        latest, _, anomalies, _ = self._scan()
        report["warnings"] += [f"{a['problem']}" for a in anomalies[:20]]
        seen, allow = set(), set(allow_secret_like)
        with tempfile.TemporaryDirectory(prefix="jr-snap-") as work:
            for store in wanted:
                info = report["stores"][store] = {"status": "synced", "rows": {}}
                try:
                    source = self._open_store(store, state, knowledge_dir, work, info, report["skipped"])
                except (sqlite3.Error, OSError) as exc:
                    info.update(status="error", error=f"{type(exc).__name__}: {exc}"[:200])
                    report["ok"] = False
                    continue
                if source is None:
                    continue
                try:
                    for kind, key, obj in source:
                        self._archive(kind, key, obj, latest, seen, report, run, dry_run, allow)
                except (sqlite3.Error, OSError) as exc:
                    info.update(status="error", error=f"{type(exc).__name__}: {exc}"[:200])
                    report["ok"] = False
                finally:
                    close = getattr(source, "_db", None)
                    if close:
                        close.close()
        wanted_stores = set(wanted)
        report["absent_from_source"] = sum(
            1 for stem, rec in latest.items() if stem not in seen and not rec["tomb"]
            and self._store_of(rec) in wanted_stores
            and report["stores"][self._store_of(rec)]["status"] == "synced")
        report["unclassified_state"] = inventory(state)["unclassified"]
        report["complete"] = (report["ok"] and not report["skipped"] and not report["failures"]
                              and not report["unclassified_state"]
                              and all(s["status"] in ("synced", "absent") for s in report["stores"].values())
                              and set(wanted) == set(self.STORES))
        if report["written"] and not dry_run:
            self._write_aux(report, run, started)
        return report

    def parity(self, state_dir, stores=None, knowledge_dir=None):
        """C3 verify: how the archive compares with the live data, read-only (a dry-run sync).

        Typed per store: in_sync / missing (never archived) / stale (archived, source changed) /
        excluded (no-archive) / skipped_secret_like / skipped_other / vault_only (archived, gone from
        source: NOT a deletion, kept) / tombstoned. `in_sync` overall means every wanted store
        matches; it never says the archive can replace SQLite (C3 is not cut over)."""
        rep = self.sync(state_dir, stores=stores, knowledge_dir=knowledge_dir, dry_run=True)
        out = {"ok": rep["ok"], "error": rep.get("error"), "stores": {}, "failures": len(rep["failures"]),
               "vault_only": rep.get("absent_from_source", 0), "tombstoned": rep.get("tombstoned_skipped", 0),
               "scope": rep.get("scope")}
        skipped = {}
        for item in rep["skipped"]:
            skipped.setdefault(self._store_of_id(item.get("record")), []).append(item.get("reason"))
        for name, info in rep["stores"].items():
            reasons = skipped.get(name, [])
            out["stores"][name] = {
                "status": info.get("status"), "rows": info.get("rows"),
                "in_sync": info.get("unchanged", 0), "missing": info.get("missing", 0),
                "stale": info.get("stale", 0), "excluded": info.get("excluded", 0),
                "skipped_secret_like": sum(1 for r in reasons if r == "secret-like"),
                "skipped_other": sum(1 for r in reasons if r != "secret-like")}
        out["in_sync"] = bool(rep["ok"] and not rep["failures"] and not rep["written"]
                              and not rep["skipped"] and not rep.get("absent_from_source")
                              and all(v["status"] in ("synced", "absent") for v in rep["stores"].values()))
        return out

    @staticmethod
    def _store_of_id(rid):
        return str(rid or "").split("/", 1)[0]

    def _store_of(self, rec):
        try:
            return Path(rec["dir"]).relative_to(self.area).parts[0]
        except (ValueError, IndexError):
            return ""

    def _open_store(self, store, state, knowledge_dir, work, info, skipped):
        info["snapshot_at"] = self.clock().isoformat(timespec="seconds")
        files = {"memory": "memory.sqlite3", "history": "history.sqlite3", "tasks": "tasks.sqlite3"}
        if store in files:
            path = state / files[store]
            if not path.is_file():
                info["status"] = "absent"
                return None
            db = self._snapshot(path, work)
            gen = {"memory": self._memory_records, "history": self._history_records,
                   "tasks": self._task_records}[store](db, info["rows"])
            return _Source(gen, db)
        if store == "plans":
            return _Source(self._plan_records(state, info["rows"], skipped))
        if store == "runs":
            return _Source(self._run_records(state, info["rows"], skipped))
        root = Path(knowledge_dir).expanduser().resolve() if knowledge_dir else state / "knowledge"
        if not root.is_dir():
            info["status"] = "absent"
            return None
        return _Source(self._knowledge_records(root, info["rows"], skipped))

    def _archive(self, kind, key, obj, latest, seen, report, run, dry_run, allow):
        rid = self.record_id(obj["store"], kind, key)
        stem, _, _, digest12 = self.stem_for(rid)
        seen.add(stem)
        tally = (report.get("stores") or {}).get(obj["store"])
        try:
            if excluded(obj):
                report.setdefault("excluded", []).append(rid)
                if tally is not None:
                    tally["excluded"] = tally.get("excluded", 0) + 1
                return
            if looks_secret(obj) and rid not in allow:
                report["skipped"].append({"record": rid, "reason": "secret-like"})
                return
            for attempt in range(4):
                current = latest.get(stem)
                line = _canonical(obj)
                p_full = _sha(line)
                if current and current.get("conflict") and not current.get("tomb"):
                    # Missing or duplicate numbers stay missing. Do not synthesize them.
                    # A damaged newest note may still be repaired by appending the next
                    # revision from this payload; the gap remains the owner's.
                    if not self._damaged_matching_head(current, p_full):
                        report["failures"].append({"record": rid, "error": "revision-gap-or-duplicate: owner must resolve"})
                        report["ok"] = False
                        return
                if current and current["tomb"]:
                    # Never resurrected by sync, changed source or not. Reviving needs an explicit
                    # owner action that does not exist yet (open blocker C1).
                    report["tombstoned_skipped"] += 1
                    if self._tomb_supersedes(current) != p_full:
                        report["tombstoned_source_changed"].append(rid)
                    return
                repair = False
                if current and current["payload"] == p_full[:12]:
                    if self._note_intact(current, p_full):
                        report["unchanged"] += 1
                        if tally is not None:
                            tally["unchanged"] = tally.get("unchanged", 0) + 1
                        return
                    repair = True   # filename says current, the note says otherwise: append, never rewrite
                elif current and self._identity_clash(current, obj):
                    report["failures"].append({"record": rid, "error": "identity-collision: id already names "
                                               "a different record in the archive; owner must resolve"})
                    report["ok"] = False
                    return
                rev = current["rev"] + 1 if current else 1
                prev = current["name"][:-3] if current else None
                chunks = []
                data, p_full = self._render(obj, rev, prev, run, self.clock(), chunks_out=chunks)
                name = f"{stem}-r{rev:04d}-{p_full[:12]}.md"
                if tally is not None:
                    tally["written"] = tally.get("written", 0) + 1
                    if dry_run:   # parity: never archived vs archived but out of date
                        gap = "missing" if current is None else "stale"
                        tally[gap] = tally.get(gap, 0) + 1
                if dry_run:
                    report["written"] += 1
                    if repair:
                        report["repaired"].append(rid)  # the revision that would be appended, not written
                    return
                try:
                    folder = self._write_new((obj["store"], _slug(kind, 20), digest12[:2]), name, data,
                                             **({"chunks": chunks} if chunks else {}))
                except RevisionExists:
                    self._refresh(latest, stem, obj["store"], kind, digest12)
                    continue
                latest[stem] = {"stem": stem, "rev": rev, "payload": p_full[:12], "tomb": False,
                                "name": name, "dir": folder.parent}
                report["written"] += 1
                report["bytes"] += len(data) + sum(len(blob) for _, blob in chunks)
                if chunks:
                    report["chunked"] = report.get("chunked", 0) + 1
                if repair:
                    report["repaired"].append(rid)
                return
            report["failures"].append({"record": rid, "error": "revision race not resolved; retry"})
            report["ok"] = False
        except ValueError as exc:   # oversize / NaN in source
            report["skipped"].append({"record": rid, "reason": str(exc)[:80]})
        except (OSError, SecondBrainError) as exc:   # vault trouble: source untouched, retryable
            report["failures"].append({"record": rid, "error": f"{type(exc).__name__}: {exc}"[:200]})
            report["ok"] = False

    def _identity_clash(self, current, obj):
        """True when the newest archived revision names a different record under the same id."""
        fields = IDENTITY.get(obj["kind"])
        if not fields or current["tomb"]:
            return False
        try:
            old = self._load(Path(current["dir"]) / current["name"])["obj"]
        except (OSError, ValueError, SecondBrainError):
            return False   # unreadable newest note: repair path, not a collision verdict
        old_f, new_f = old.get("fields") or {}, obj.get("fields") or {}
        return any(f in old_f and f in new_f and old_f[f] != new_f[f] for f in fields)

    def _note_intact(self, info, p_full):
        """The newest note really holds this payload (readable, payload hash matches, frontmatter
        agrees with the filename). Body-only human edits do not matter here. This is the only
        read-back: queue ack and rescan both go through _load here."""
        try:
            parsed = self._load(Path(info["dir"]) / info["name"])
        except (OSError, ValueError, SecondBrainError):
            return False
        meta = parsed["meta"]
        return bool(parsed["payload_ok"] and meta.get("jarvis_payload_sha256") == p_full
                    and meta.get("jarvis_revision") == info["rev"] and not meta.get("jarvis_tombstone"))

    def _latest_info(self, store, kind, key):
        rid = self.record_id(store, kind, key)
        stem, _, _, digest12 = self.stem_for(rid)
        try:
            return self._record_state(stem, store, kind, digest12).get(stem)
        except (OSError, SecondBrainError):
            return None

    def publication_sound(self, store, kind, key, payload_sha):
        """True when the queue may leave this payload acknowledged without a new revision.

        A gap or a duplicate revision number is never sound, tombstone or not. A contiguous
        tombstone stays acknowledged and is not opened, so a damaged chunk cannot block a
        deletion. Any other newest note is read through _load. False does not by itself
        append a revision: `repair_withheld` keeps an intact gap and any tombstone fail-closed."""
        info = self._latest_info(store, kind, key)
        if not info or info.get("conflict"):
            return False
        if info.get("tomb"):
            return True
        return self._note_intact(info, payload_sha)

    def repair_withheld(self, store, kind, key, payload_sha):
        """True when a false `publication_sound` must not queue another revision.

        A tombstone is never resurrected. An intact newest note behind a gap or a duplicate
        number stays as the owner left it; the missing numbers are not synthesized. A damaged
        newest note is not withheld, so the next drain can append a revision and leave the
        old bytes in place."""
        info = self._latest_info(store, kind, key)
        if not info:
            return False
        if info.get("tomb"):
            return True
        return bool(info.get("conflict") and self._note_intact(info, payload_sha))

    def _damaged_matching_head(self, current, payload_sha):
        """The newest filename claims this payload, and _load says the note does not hold it.
        A tombstone is not damage to repair. A gap alone is not damage."""
        return bool(current and not current.get("tomb")
                    and current.get("payload") == payload_sha[:12]
                    and not self._note_intact(current, payload_sha))

    def published_readable(self, store, kind, key, payload_sha):
        """Head check for status. True/False from _load. Never skips a large plain note.

        A tombstone is True and is not opened, so a damaged chunk cannot block a deletion.
        Every other published head is opened through _load, including a gap, a duplicate
        revision number, and a plain note larger than 64 KiB. True means that note holds
        this payload. It does not mean the revision list is 1..n; revision_conflict is that
        fact. A missing note is False. Damaged bytes of an older revision are not opened
        here; validate() lists those."""
        info = self._latest_info(store, kind, key)
        if not info:
            return False
        if info.get("tomb"):
            return True
        return self._note_intact(info, payload_sha)

    def revision_conflict(self, store, kind, key):
        """True when this record's note names are not 1..n. Filenames only; the head bytes
        are published_readable's job. A missing record is False."""
        info = self._latest_info(store, kind, key)
        return bool(info and info.get("conflict"))

    def _refresh(self, latest, stem, store, kind, digest12):
        folder = self.area / store / _slug(kind, 20) / digest12[:2]
        if not folder.is_dir():
            return
        with os.scandir(folder) as entries:
            for entry in entries:
                m = NOTE_RE.match(entry.name)
                if m and m["stem"] == stem and not entry.is_symlink():
                    rev = int(m["rev"])
                    if stem not in latest or rev > latest[stem]["rev"]:
                        latest[stem] = {"stem": stem, "rev": rev, "payload": m["payload"],
                                        "tomb": bool(m["tomb"]), "name": entry.name, "dir": folder}

    def _tomb_supersedes(self, info):
        try:
            parsed = self._load(Path(info["dir"]) / info["name"])
            return (parsed["obj"].get("tombstone") or {}).get("supersedes")
        except (OSError, ValueError, SecondBrainError):
            return None

    # -- per-record publish (write-through queue) ---------------------------------------------
    def _record_state(self, stem, store, kind, digest12):
        """{stem: info} for ONE record's newest revision, scanning only its folder and creating
        nothing. Same conflict rule as _scan (gaps/duplicates mean no safe 'latest')."""
        cur = self.root
        for part in AREA + (store, _slug(kind, 20), digest12[:2]):
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return {}
            if stat.S_ISLNK(st.st_mode) or getattr(os.path, "isjunction", lambda p: False)(cur) \
                    or not stat.S_ISDIR(st.st_mode):
                raise SecondBrainError(f"{cur.name} is a link or not a directory")
        revs = []
        with os.scandir(cur) as entries:
            for entry in entries:
                m = NOTE_RE.match(entry.name)
                if m and m["stem"] == stem and not entry.is_symlink():
                    revs.append({"stem": stem, "rev": int(m["rev"]), "payload": m["payload"],
                                 "tomb": bool(m["tomb"]), "name": entry.name, "dir": cur})
        if not revs:
            return {}
        info = max(revs, key=lambda r: r["rev"])
        if sorted(r["rev"] for r in revs) != list(range(1, len(revs) + 1)):
            info["conflict"] = True
        return {stem: info}

    def publish(self, payload, allow_secret_like=False, dry_run=False):
        """Archive ONE exact payload as the next revision of its record (never re-reads sources).

        Returns {ok, status, record, revision, name, bytes, reason}. status is one of
        'written' (new revision, read back and verified), 'unchanged' (newest note already holds
        exactly this payload), 'would-write' (dry_run), 'held' (needs the owner: secret-like,
        oversize, tombstoned, revision conflict, invalid payload; reason says which) or 'retry'
        (vault trouble; same call may be repeated). Nothing is acknowledged here: the caller
        acks only on 'written' or 'unchanged'."""
        out = {"ok": False, "status": "held", "record": None, "revision": None, "name": None,
               "bytes": 0, "reason": ""}
        try:
            obj = self._valid_payload(payload)
        except ValueError as exc:
            out["reason"] = f"invalid: {exc}"[:120]
            return out
        store, kind, key = obj["store"], obj["kind"], obj["key"]
        rid = out["record"] = self.record_id(store, kind, key)
        status = self.check_vault()
        if not status["ok"]:
            out.update(status="retry", reason=status["reason"])
            return out
        stem, _, _, digest12 = self.stem_for(rid)
        report = {"ok": True, "skipped": [], "failures": [], "written": 0, "unchanged": 0, "bytes": 0,
                  "repaired": [], "tombstoned_skipped": 0, "tombstoned_source_changed": []}
        try:
            if not dry_run:
                self.ensure_index()
            latest = self._record_state(stem, store, kind, digest12)
            self._archive(kind, key, obj, latest, set(), report, "q-" + uuid.uuid4().hex[:10], dry_run,
                          {rid} if allow_secret_like else set())
        except (OSError, SecondBrainError) as exc:
            out.update(status="retry", reason=f"{type(exc).__name__}: {exc}"[:160])
            return out
        if report["skipped"]:
            why = report["skipped"][0]["reason"]
            out["reason"] = "secret-like" if why == "secret-like" else (
                "oversize" if why.startswith("oversize") else why)
            return out
        if report["tombstoned_skipped"]:
            out["reason"] = "tombstoned"
            return out
        if report.get("excluded"):
            out.update(ok=True, status="unchanged", reason="no-archive")
            return out
        if report["failures"]:
            err = report["failures"][0]["error"]
            if err.startswith("revision-gap"):
                out["reason"] = "conflict"
            elif err.startswith("identity-collision"):
                out["reason"] = "identity-collision"
            else:
                out.update(status="retry", reason=err[:160])
            return out
        if dry_run:
            out.update(ok=True, status="would-write" if report["written"] else "unchanged")
            return out
        info = latest.get(stem)
        try:
            parsed = self._load(Path(info["dir"]) / info["name"])
        except (OSError, ValueError, SecondBrainError, TypeError, KeyError) as exc:
            out.update(status="retry", reason=f"verify: {type(exc).__name__}"[:80])
            return out
        meta = parsed["meta"]
        if not (parsed["payload_ok"] and parsed["payload_text"] == _canonical(obj)
                and meta.get("jarvis_payload_sha256") == _sha(_canonical(obj))
                and meta.get("jarvis_revision") == info["rev"]):
            out.update(status="retry", reason="verify: note does not hold the payload")
            return out
        out.update(ok=True, status="written" if report["written"] else "unchanged",
                   revision=info["rev"], name=info["name"], bytes=report["bytes"],
                   chunked=bool(parsed.get("chunked")),
                   repaired=bool(report.get("repaired")))
        return out

    @staticmethod
    def _valid_payload(payload):
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("not a jarvis-record payload")
        store, kind, key = payload.get("store"), payload.get("kind"), payload.get("key")
        if store not in KINDS or kind not in KINDS[store]:
            raise ValueError("unknown store/kind")
        if not isinstance(key, str) or not key or len(key) > 200 or re.search(r"[\x00-\x1f\x7f]", key):
            raise ValueError("bad key")
        if store in ("plans", "runs", "knowledge") and not safe_relpath(store, key):
            raise ValueError("unsafe file key")
        if not isinstance(payload.get("source"), str) or not isinstance(payload.get("fields"), dict):
            raise ValueError("source/fields missing")
        if "tombstone" in payload or set(payload) - {"schema", "store", "kind", "key", "source", "fields", "children"}:
            raise ValueError("unexpected keys")
        return payload

    @staticmethod
    def _index_text(day):
        return (f"---\ntitle: JARVIS records archive\ntype: index\ntags: [index, reference, jarvis-record]\n"
                f"created: {day}\nupdated: {day}\n---\n\n# JARVIS records archive\n\n"
                "Machine-written, additive archive of JARVIS memory, command history, tasks, plans, runs "
                "and knowledge files. One immutable note per revision; do not edit payload blocks. "
                "Credentials, tokens, locks, live approvals and caches are never written here. "
                "Status: archive only; the PC state files remain authoritative until cutover is accepted.\n")

    def ensure_index(self):
        """Create the index note once (never replaced). Raises OSError/SecondBrainError."""
        if (self.area / f"{INDEX_NOTE}.md").exists():
            return
        try:
            self._write_new((), f"{INDEX_NOTE}.md", self._index_text(self.clock().strftime("%Y-%m-%d")).encode("utf-8"))
        except RevisionExists:
            pass

    def _write_aux(self, report, run, started):
        """Index note (created once) and a per-run manifest. Failures here are warnings."""
        day = started.strftime("%Y-%m-%d")
        index = self._index_text(day)
        try:
            self._write_new((), f"{INDEX_NOTE}.md", index.encode("utf-8"))
        except RevisionExists:
            pass
        except (OSError, SecondBrainError) as exc:
            report["warnings"].append(f"index note: {exc}"[:160])
        lines = [f"---\ntitle: JARVIS sync {run}\ntype: reference\ntags: [reference, jarvis-record, meta]\n"
                 f"created: {day}\nupdated: {day}\n---\n\n# JARVIS sync {run}\n\nPart of [[{INDEX_NOTE}]]. "
                 f"Written {report['written']} revision(s). {report['scope']}.\n"]
        for name, info in report["stores"].items():
            lines.append(f"- {name}: {info['status']}, snapshot {info.get('snapshot_at')}, rows {info['rows']}")
        stamp = started.strftime("%Y%m%dt%H%M%Sz")
        try:
            self._write_new(("manifests",), f"jarvis-sync-{stamp}-{run[:8]}.md", ("\n".join(lines) + "\n").encode())
        except (OSError, SecondBrainError) as exc:
            report["warnings"].append(f"manifest note: {exc}"[:160])

    # -- explicit amendments -------------------------------------------------------------------
    def tombstone(self, record_id, reason):
        """Append a tombstone revision (owner intent). Source rows are not touched; sync never
        resurrects it, even if the source changes (it lists the id in `tombstoned_source_changed`).
        There is no revive API yet. Returns {ok, status}."""
        stem, store, kind, digest = self.stem_for(record_id)
        status = self.check_vault()
        if not status["ok"]:
            return {"ok": False, "status": status["reason"]}
        latest, _, _, _ = self._scan()
        current = latest.get(stem)
        if not current:
            return {"ok": False, "status": "unknown-record"}
        if current["tomb"]:
            return {"ok": True, "status": "already-tombstoned"}
        key = record_id.split("/", 2)[2]
        stamp = self.clock()
        obj = {"schema": SCHEMA, "store": store, "kind": kind, "key": key, "source": "tombstone",
               "fields": {"text": f"tombstone: {str(reason)[:200]}"},
               "tombstone": {"reason": str(reason)[:500], "at": stamp.isoformat(timespec="seconds"),
                             "supersedes": None}}
        try:
            parsed = self.parse_note(self._read_note(Path(current["dir"]) / current["name"]))   # meta only: a damaged chunk must not block deletion
            obj["tombstone"]["supersedes"] = parsed["meta"].get("jarvis_payload_sha256")
            data, p_full = self._render(obj, current["rev"] + 1, current["name"][:-3], "tombstone", stamp, tomb=True)
            name = f"{stem}-r{current['rev'] + 1:04d}-{p_full[:12]}-t.md"
            self._write_new((store, _slug(kind, 20), digest[:2]), name, data)
        except (OSError, ValueError, SecondBrainError) as exc:
            return {"ok": False, "status": f"failed: {type(exc).__name__}: {exc}"[:200]}
        return {"ok": True, "status": "tombstoned"}

    # -- read / validate -----------------------------------------------------------------------
    def _placed(self, info):
        """(store, kind slug) from a note's directory, or ('', '') when it is not under the area."""
        try:
            parts = Path(info["dir"]).relative_to(self.area).parts
        except (ValueError, IndexError):
            return "", ""
        if len(parts) < 2:
            return "", ""
        return parts[0], parts[1]

    def _canonical_payload(self, info, rec_store, kind_slug):
        """One latest live payload, or SecondBrainError. Does not keep the note bytes.

        `_load` checks the hash (and, for a chunked note, the MAX_OVERFLOW_BYTES ceiling).
        `_valid_payload` checks the schema, the kind against KINDS, and the key.
        The folder must be that store and `_slug(kind, 20)`. The filename stem must be
        `stem_for(store/kind/key)`, the same identity `validate()` uses."""
        path = Path(info["dir"]) / info["name"]
        try:
            parsed = self._load(path)
        except (OSError, ValueError, SecondBrainError) as exc:
            raise SecondBrainError(f"unreadable: {exc}") from exc
        obj = parsed.get("obj")
        if not parsed.get("payload_ok") or not isinstance(obj, dict):
            raise SecondBrainError("payload hash mismatch")
        try:
            self._valid_payload(obj)
        except ValueError as exc:
            raise SecondBrainError("record-identity-mismatch") from exc
        if obj.get("store") != rec_store or _slug(obj.get("kind"), 20) != kind_slug:
            raise SecondBrainError("record-identity-mismatch")
        rid = self.record_id(obj.get("store"), obj.get("kind"), obj.get("key"))
        if self.stem_for(rid)[0] != info.get("stem"):
            raise SecondBrainError("record-identity-mismatch")
        return obj

    def iter_canonical_records(self, store=None, kind=None):
        """Yield one validated latest live payload at a time. Skips tombstones. Does not read SQLite.

        Raises SecondBrainError on a revision gap, a duplicate revision number, or a head
        that does not _load (including a payload hash mismatch) or that fails
        `_canonical_payload`. An older revision is never returned in place of a bad head.
        `store` and `kind` limit which records are considered; a problem outside that limit
        is not this call's error. A tombstone is skipped even when its revision list has a
        gap, and it is not opened.

        The scan keeps filenames, not bodies. Each selected note is loaded, checked, and
        then yielded or discarded before the next note is opened. A chunked payload above
        MAX_OVERFLOW_BYTES never loads. KINDS has no two kinds in one store that share a
        `_slug(kind, 20)` folder. A kind filter selects that folder. If a later valid kind
        shared the slug, this filter skips that other valid payload instead of calling it
        an identity mismatch. A malformed kind, folder, or stem still raises."""
        if store is not None and store not in KINDS:
            raise SecondBrainError("unknown store")
        if kind is not None and not any(kind in kinds for kinds in KINDS.values()):
            raise SecondBrainError("unknown kind")
        if store is not None and kind is not None and kind not in KINDS[store]:
            raise SecondBrainError("unknown kind")
        want_slug = _slug(kind, 20) if kind is not None else None
        latest, _, _, _ = self._scan()
        for _stem, info in sorted(latest.items()):
            rec_store, kind_slug = self._placed(info)
            if store is not None and rec_store != store:
                continue
            if want_slug is not None and kind_slug != want_slug:
                continue
            if info.get("tomb"):
                continue
            if info.get("conflict"):
                raise SecondBrainError("revision-gap-or-duplicate")
            obj = self._canonical_payload(info, rec_store, kind_slug)
            # Folder match is by slug. A different valid kind with that same slug is outside
            # this filter. `_canonical_payload` already rejected a bad kind, folder, or stem.
            if kind is not None and obj.get("kind") != kind:
                continue
            yield obj

    def canonical_records(self, store=None, kind=None):
        """Verified latest live payloads. Skips tombstones. Does not read SQLite.

        List form of `iter_canonical_records`: the same selection, the same errors, every
        selected payload retained. Recall uses the iterator so it does not."""
        return list(self.iter_canonical_records(store=store, kind=kind))

    def latest(self, record_id):
        """Payload dict of the newest non-tombstone revision, or None. Raises on damaged notes."""
        stem = self.stem_for(record_id)[0]
        info = self._scan()[0].get(stem)
        if not info or info["tomb"]:
            return None
        if info.get("conflict"):
            raise SecondBrainError("revision-gap-or-duplicate")
        parsed = self._load(Path(info["dir"]) / info["name"])
        if not parsed["payload_ok"]:
            raise SecondBrainError("payload hash mismatch")
        return parsed["obj"]

    @staticmethod
    def _tomb_agreement(info, parsed):
        """Filename flag (-t), frontmatter flag and payload `tombstone` block must say the same
        thing. Returns a problem string or None. validate() and restore() share this, so a note
        either passes both or fails both."""
        flags = (bool(info["tomb"]), parsed["meta"].get("jarvis_tombstone") is True,
                 isinstance(parsed["obj"].get("tombstone"), dict))
        if len(set(flags)) != 1:
            return "tombstone-flag-disagrees (filename/frontmatter/payload)"
        if flags[0] and not isinstance((parsed["obj"]["tombstone"]).get("supersedes", None), (str, type(None))):
            return "tombstone-block-malformed"
        return None

    def validate(self, deep=True, limit=100):
        """Check the archive. Errors break restore; human_edited is informational."""
        latest, revisions, anomalies, tmp = self._scan()
        errors, edited = [], []

        def err(path, problem):
            errors.append({"path": str(Path(path).relative_to(self.root)), "problem": problem})
        for stem, revs in revisions.items():
            revs.sort(key=lambda r: r["rev"])
            expect = list(range(1, len(revs) + 1))
            if [r["rev"] for r in revs] != expect:
                err(revs[0]["dir"] / revs[0]["name"], "revision-gap-or-duplicate")
            for info in revs:
                path = Path(info["dir"]) / info["name"]
                if not deep:
                    continue
                try:
                    parsed = self._load(path)
                except (OSError, ValueError, SecondBrainError) as exc:
                    err(path, f"unreadable: {exc}"[:120])
                    continue
                meta = parsed["meta"]
                if not parsed["payload_ok"]:
                    err(path, "payload-hash-mismatch")
                    continue
                if not parsed["body_ok"]:
                    edited.append(str(path.relative_to(self.root)))
                if meta.get("jarvis_revision") != info["rev"] or not str(meta.get("jarvis_payload_sha256", "")).startswith(info["payload"]):
                    err(path, "frontmatter-disagrees-with-filename")
                    continue
                obj = parsed["obj"]
                rid = self.record_id(obj.get("store"), obj.get("kind"), obj.get("key"))
                if self.stem_for(rid)[0] != info["stem"] or obj.get("schema") != SCHEMA:
                    err(path, "record-identity-mismatch")
                    continue
                problem = self._tomb_agreement(info, parsed)
                if problem:
                    err(path, problem)
        return {"ok": not errors, "records": len(latest), "revisions": sum(len(v) for v in revisions.values()),
                "errors": errors[:limit], "error_count": len(errors), "human_edited": edited[:limit],
                "human_edited_count": len(edited), "stale_tmp": len(tmp), "link_anomalies": len(anomalies)}

    # -- restore -------------------------------------------------------------------------------
    def restore(self, target_dir, stores=None, strict=True):
        """Rebuild state from archive notes alone into a NEW, empty directory. Never touches an
        existing state dir and never reads the original databases. Credentials/receipts/grants/
        embeddings are not part of the archive, so they are not recreated. Queued/running tasks
        return `interrupted`; running plans return `blocked`. strict: any bad record aborts and
        creates nothing."""
        wanted = tuple(stores or self.STORES)
        target = Path(target_dir).expanduser()
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise SecondBrainError("restore target must be a new or empty directory")
        parent = target.resolve().parent
        if not parent.is_dir():
            raise SecondBrainError("restore target parent does not exist")
        staging = parent / f".{target.name}.restore-{uuid.uuid4().hex[:8]}"
        os.mkdir(staging)
        report = {"ok": True, "restored": {}, "tombstoned": 0, "errors": [], "partial": False,
                  "nonterminal_tasks": 0, "target": str(target)}
        try:
            self._restore_into(staging, wanted, report)
            if report["errors"] and strict:
                report["ok"] = False
                raise _Abort
            report["partial"] = bool(report["errors"])
            if target.exists():
                target.rmdir()
            os.rename(staging, target)
            staging = None
        except _Abort:
            pass
        finally:
            if staging is not None:
                import shutil
                shutil.rmtree(staging, ignore_errors=True)
        return report

    def _restore_into(self, staging, wanted, report):
        from contextlib import ExitStack
        from .history import History   # lazy: nothing imported or created before an explicit restore
        from .memory import Memory
        from .store import Store
        makers = {"memory": lambda: Memory(staging), "history": lambda: History(staging),
                  "tasks": lambda: Store(staging)}
        nonterminal = []
        with ExitStack() as stack:
            conns = {}
            for name, make in makers.items():
                if name in wanted:
                    conns[name] = stack.enter_context(make().connect())
            (staging / "tasks.pruned").unlink(missing_ok=True)   # Store() stamps a prune clock
            tasks = []   # (stem, obj): applied after the loop so ledger seq collisions can be resolved
            latest, _, _, _ = self._scan()
            for stem, info in sorted(latest.items()):
                store = self._store_of(info)
                if store not in wanted:
                    continue
                path = Path(info["dir"]) / info["name"]
                try:
                    if info.get("conflict"):
                        raise ValueError("revision-gap-or-duplicate")
                    parsed = self._load(path)
                    if not parsed["payload_ok"]:
                        raise ValueError("payload-hash-mismatch")
                    if parsed["meta"].get("jarvis_revision") != info["rev"]:
                        raise ValueError("frontmatter-disagrees-with-filename")
                    obj = parsed["obj"]
                    rid = self.record_id(obj["store"], obj["kind"], obj["key"])
                    if obj.get("schema") != SCHEMA or self.stem_for(rid)[0] != stem or obj["store"] != store:
                        raise ValueError("record-identity-mismatch")
                    problem = self._tomb_agreement(info, parsed)
                    if problem:
                        raise ValueError(problem)
                    if info["tomb"]:   # verified deleted-by-owner: not restored, counted
                        report["tombstoned"] += 1
                        continue
                    self._apply(obj, staging, conns, nonterminal, tasks)
                    report["restored"][store] = report["restored"].get(store, 0) + 1
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SecondBrainError) as exc:
                    report["errors"].append({"path": str(path.relative_to(self.root)),
                                             "problem": f"{type(exc).__name__}: {exc}"[:160]})
            if "tasks" in conns and tasks:
                self._apply_tasks(staging, conns["tasks"], tasks, nonterminal, report)
            if "tasks" in conns and nonterminal:
                stamp = self.clock().isoformat()
                for task_id, original in nonterminal:   # after all originals: seq stays collision-free
                    conns["tasks"].execute("UPDATE tasks SET state='interrupted' WHERE id=?", (task_id,))
                    conns["tasks"].execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                                           (task_id, stamp, "restored", json.dumps({"original_state": original})))
                report["nonterminal_tasks"] = len(nonterminal)

    def _apply_tasks(self, staging, db, tasks, nonterminal, report):
        """Insert archived tasks. Event `seq` is a per-install counter: two ledgers archived into one
        vault both start at 1. A task whose seq numbers are free keeps them (a single ledger round-trips
        exactly); a task that collides is renumbered whole (order kept) above every archived seq, and
        the original numbers are written to `archive-restore-map.json` next to the databases. The
        result is deterministic (sorted record stems) and nothing is silently merged: collisions are
        counted in report['seq_remapped_tasks']."""
        used, top, remap = set(), 0, {}
        for _, obj in tasks:
            for ev in (obj.get("children") or {}).get("events", []):
                top = max(top, int(ev["seq"]))
        for _, obj in sorted(tasks, key=lambda t: t[0]):
            events = sorted((obj.get("children") or {}).get("events", []), key=lambda e: int(e["seq"]))
            f = obj["fields"]
            db.execute("INSERT INTO tasks(id,spec,state,created_at,updated_at) "
                       "VALUES(:id,:spec,:state,:created_at,:updated_at)", f)
            clash = any(int(e["seq"]) in used for e in events)
            mapping = []
            for e in events:
                seq = int(e["seq"])
                if clash:
                    top += 1
                    seq = top
                used.add(seq)
                mapping.append([int(e["seq"]), seq])
                db.execute("INSERT INTO events(seq,task_id,at,kind,detail) VALUES(?,?,?,?,?)",
                           (seq, e["task_id"], e["at"], e["kind"], e["detail"]))
            if clash:
                remap[f["id"]] = {"source": obj.get("source"), "events": mapping}
        report["seq_remapped_tasks"] = len(remap)
        if remap:
            report.setdefault("warnings", []).append(
                f"{len(remap)} task(s) had event seq numbers already used by another archived ledger; "
                "renumbered, originals kept in archive-restore-map.json")
            (staging / "archive-restore-map.json").write_text(
                json.dumps({"schema": 1, "tasks": remap}, sort_keys=True), encoding="utf-8")

    def _apply(self, obj, staging, conns, nonterminal, tasks=None):
        kind, f, children = obj["kind"], obj["fields"], obj.get("children") or {}
        if kind == "memory-item":
            conns["memory"].execute(
                "INSERT INTO items(id,kind,title,body,tags,source,importance,meta,digest,created_at) "
                "VALUES(:id,:kind,:title,:body,:tags,:source,:importance,:meta,:digest,:created_at)", f)
        elif kind == "memory-links":
            conns["memory"].executemany(
                "INSERT INTO links(src,dst,rel,created_at) VALUES(:src,:dst,:rel,:created_at)", f["links"])
        elif kind == "history-command":
            conns["history"].execute(
                "INSERT INTO commands(id,request,at,origin,origin_class,text,key,device,intent,args,state,ok,"
                "result,error_class,observed_ms,total_ms) VALUES(:id,:request,:at,:origin,:origin_class,:text,"
                ":key,:device,:intent,:args,:state,:ok,:result,:error_class,:observed_ms,:total_ms)", f)
            conns["history"].executemany("INSERT INTO hops(command,name,ms,state) VALUES(:command,:name,:ms,:state)",
                                         children.get("hops", []))
        elif kind == "task":
            tasks.append((self.stem_for(self.record_id(obj["store"], kind, obj["key"]))[0], obj))
            if f["state"] not in TERMINAL_TASK_STATES:
                nonterminal.append((f["id"], f["state"]))
        elif kind in ("plan", "run-file", "knowledge-file"):
            store = obj["store"]
            rel = f["relpath"]
            if rel != obj["key"] or not safe_relpath(store, rel):
                raise ValueError("unsafe file key")
            raw = f["content"].encode("utf-8") if f["encoding"] == "utf-8" else base64.b64decode(f["content"], validate=True)
            if _sha(raw) != f["sha256"]:
                raise ValueError("file hash mismatch")
            if kind == "plan":
                try:
                    plan = json.loads(raw)
                    if isinstance(plan, dict) and plan.get("state") == "running":
                        plan.update(state="blocked", restored_from_state="running")
                        raw = json.dumps(plan, indent=2).encode("utf-8")
                except ValueError:
                    pass
            dest = Path(staging, store, *rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as stream:   # exclusive: never overwrite
                stream.write(raw)
        else:
            raise ValueError(f"unknown record kind {kind!r}")


class _Abort(Exception):
    pass


class _Source:
    """Generator wrapper that remembers the snapshot connection to close."""
    def __init__(self, gen, db=None):
        self._gen, self._db = gen, db

    def __iter__(self):
        return self._gen
