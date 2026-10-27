"""Memory heap: one append-mostly store for everything JARVIS sees, says, or does.

Every item gets a readable ID (`kind:YYYYMMDD:hex6`), full-text search (SQLite FTS5,
BM25 ranking blended with importance and recency), tags, and typed links between
items. Identical content within a kind is stored once, except conversation events
(messages and replies): asking the same thing twice is two requests. No third-party
dependencies.
"""

import hashlib
import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

KIND = re.compile(r"[a-z][a-z0-9_-]{0,23}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}")
EVENT_KINDS = {"message", "reply"}


def _now():
    return datetime.now(timezone.utc)


def _fts_query(text):
    """Turn free text into a safe FTS5 query: quoted terms, OR-joined, prefix-matched."""
    terms = re.findall(r"[\w][\w.@#'-]*", text.lower())
    return " OR ".join(f'"{t.replace(chr(34), "")}"*' for t in terms[:32])


def semantic_recall(config):
    """memory.semantic in jarvis.config.json: meaning-based recall for JARVIS's own answers.
    Off when unset, or without the local model: recall stays keyword-only."""
    import os
    if os.environ.get("JARVIS_SEMANTIC") == "0":  # tests, or a quick opt-out
        return False
    return bool((config.get("memory") or {}).get("semantic", False))


class Memory:
    def __init__(self, directory, semantic=False):
        """semantic: also rank by meaning with local embeddings (jarvis/embed.py), merged
        with the keyword ranking; quietly keyword-only when the model isn't available."""
        self.semantic = semantic
        self.path = Path(directory).resolve() / "memory.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items(
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                  body TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
                  importance REAL NOT NULL, meta TEXT NOT NULL, digest TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS items_digest ON items(kind, digest);
                CREATE INDEX IF NOT EXISTS items_kind_time ON items(kind, created_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                  title, body, tags, content='items', content_rowid='rowid',
                  tokenize='porter unicode61');
                CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
                  INSERT INTO items_fts(rowid, title, body, tags)
                  VALUES (new.rowid, new.title, new.body, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
                  INSERT INTO items_fts(items_fts, rowid, title, body, tags)
                  VALUES ('delete', old.rowid, old.title, old.body, old.tags);
                END;
                CREATE TABLE IF NOT EXISTS links(
                  src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL,
                  created_at TEXT NOT NULL, PRIMARY KEY(src, dst, rel));
                CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
                CREATE TABLE IF NOT EXISTS vectors(
                  id TEXT PRIMARY KEY, model TEXT NOT NULL, vec BLOB NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, kind, body, title="", tags=(), source="owner", importance=0.5,
            meta=None, links=()):
        """Store an item, or return the existing one with identical kind+content."""
        if not KIND.fullmatch(kind or ""):
            raise ValueError("kind must be lowercase letters/digits, e.g. note, task, voice.")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Memory body must be non-empty text.")
        if len(body) > 2_000_000:
            raise ValueError("Memory body exceeds 2 MB; store a file path instead.")
        tags = sorted({t for t in tags if t})
        for tag in tags:
            if not TAG.fullmatch(tag):
                raise ValueError(f"Invalid tag: {tag!r}")
        if not 0 <= float(importance) <= 1:
            raise ValueError("importance must be between 0 and 1.")
        title = (title or body.strip().splitlines()[0])[:200]
        created = _now()
        item_id = f"{kind}:{created:%Y%m%d}:{secrets.token_hex(3)}"
        salt = item_id if kind in EVENT_KINDS else ""
        digest = hashlib.sha256((body + salt).encode("utf-8")).hexdigest()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM items WHERE kind=? AND digest=?",
                                  (kind, digest)).fetchone()
            if existing:
                item_id = existing["id"]
            else:
                db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (item_id, kind, title, body, " ".join(tags), source,
                            float(importance), json.dumps(meta or {}), digest,
                            created.isoformat()))
            for rel, dst in links:
                self._link(db, item_id, dst, rel)
        return self.get(item_id)

    def _link(self, db, src, dst, rel):
        if not re.fullmatch(r"[a-z][a-z_-]{0,31}", rel):
            raise ValueError("Link relation must be a short lowercase word.")
        if not db.execute("SELECT 1 FROM items WHERE id=?", (dst,)).fetchone():
            raise ValueError(f"Unknown memory item: {dst}")
        db.execute("INSERT OR IGNORE INTO links VALUES(?,?,?,?)",
                   (src, dst, rel, _now().isoformat()))

    def link(self, src, dst, rel="related"):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM items WHERE id=?", (src,)).fetchone():
                raise ValueError(f"Unknown memory item: {src}")
            self._link(db, src, dst, rel)

    @staticmethod
    def _row(row, body=True):
        item = dict(row)
        item["tags"] = item["tags"].split() if item["tags"] else []
        item["meta"] = json.loads(item["meta"])
        item.pop("digest", None)
        item.pop("rowid", None)
        if not body:
            text = item.pop("body")
            item["preview"] = text[:240] + ("…" if len(text) > 240 else "")
        return item

    def get(self, item_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown memory item: {item_id}")
            item = self._row(row)
            item["links"] = [dict(r) for r in db.execute(
                "SELECT src, dst, rel FROM links WHERE src=? OR dst=? ORDER BY created_at",
                (item_id, item_id))]
        return item

    def search(self, text="", kind=None, tag=None, since=None, limit=10):
        """Ranked search. Empty text lists newest items matching the filters."""
        limit = max(1, min(int(limit), 200))
        where, args = [], []
        if kind:
            where.append("i.kind=?")
            args.append(kind)
        if tag:
            where.append("(' ' || i.tags || ' ') LIKE ?")
            args.append(f"% {tag} %")
        if since:
            where.append("i.created_at >= ?")
            args.append(since)
        query = _fts_query(text or "")
        with self.connect() as db:
            if query:
                sql = ("SELECT i.*, bm25(items_fts, 4.0, 1.0, 2.0) AS rank FROM items_fts "
                       "JOIN items i ON i.rowid = items_fts.rowid WHERE items_fts MATCH ?"
                       + "".join(" AND " + w for w in where) + " ORDER BY rank LIMIT ?")
                rows = db.execute(sql, [query, *args, limit * 5]).fetchall()
            else:
                sql = ("SELECT i.*, 0.0 AS rank FROM items i"
                       + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY i.created_at DESC LIMIT ?")
                rows = db.execute(sql, [*args, limit]).fetchall()
        now = _now()
        scored = []
        for row in rows:
            age_days = (now - datetime.fromisoformat(row["created_at"])).total_seconds() / 86400
            # bm25 is lower-is-better; convert to a positive relevance and blend.
            relevance = -row["rank"] if query else 0.0
            score = relevance + 1.5 * row["importance"] + math.exp(-age_days / 30)
            item = self._row(row, body=False)
            item.pop("rank", None)
            item["score"] = round(score, 4)
            scored.append(item)
        if query:
            scored.sort(key=lambda i: i["score"], reverse=True)
            if self.semantic:
                fused = self._fuse(text, scored, where, args, limit)
                if fused is not None:
                    return fused[:limit]
        return scored[:limit]

    # -- meaning-based ranking (local embeddings) -------------------------------------------
    SEMANTIC_FLOOR = 0.3  # cosine below this is noise for MiniLM
    RRF_K = 60            # reciprocal-rank-fusion constant

    def _vector_text(self, row):
        return f"{row['title']}\n{row['body'][:1500]}"

    def index_vectors(self, limit=500):
        """Embed up to `limit` items that have no vector yet (newest first). Returns how many."""
        from .embed import MODEL, embed
        with self.connect() as db:
            rows = db.execute("SELECT i.id, i.title, i.body FROM items i LEFT JOIN vectors v "
                              "ON v.id = i.id AND v.model = ? WHERE v.id IS NULL "
                              "ORDER BY i.created_at DESC LIMIT ?", (MODEL, limit)).fetchall()
        if not rows:
            return 0
        vectors = embed([self._vector_text(r) for r in rows])
        if vectors is None:
            return 0
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO vectors(id, model, vec) VALUES(?,?,?)",
                           [(r["id"], MODEL, v.tobytes()) for r, v in zip(rows, vectors)])
        return len(rows)

    def warm_vectors(self):
        """Index everything not yet embedded, in a background thread (startup), so no
        request ever waits for the first full index. No-op when semantic recall is off."""
        if not self.semantic:
            return

        def run():
            try:
                while self.index_vectors(limit=200):
                    pass
            except Exception:
                pass  # recall falls back to keyword search
        import threading
        threading.Thread(target=run, name="jarvis-memory-index", daemon=True).start()

    def similar(self, text, limit=20, where=(), args=()):
        """[(id, cosine)] by meaning, best first; None when embeddings are unavailable."""
        from .embed import MODEL, embed
        import numpy as np
        query = embed([text])
        if query is None:
            return None
        self.index_vectors(limit=25)  # new items only; warm_vectors() does the bulk in the background
        sql = ("SELECT v.id, v.vec FROM vectors v JOIN items i ON i.id = v.id WHERE v.model=?"
               + "".join(" AND " + w for w in where))
        with self.connect() as db:
            rows = db.execute(sql, [MODEL, *args]).fetchall()
        if not rows:
            return []
        matrix = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = matrix @ query[0]
        best = np.argsort(-scores)[:limit]
        return [(rows[i]["id"], float(scores[i])) for i in best if scores[i] >= self.SEMANTIC_FLOOR]

    def _fuse(self, text, keyword, where, args, limit):
        """Merge keyword and meaning rankings (reciprocal rank fusion); importance and
        recency stay as small tiebreakers. None leaves the keyword ranking as it was."""
        try:
            meaning = self.similar(text, limit=max(limit * 3, 20), where=where, args=args)
        except Exception:  # recall must never break a request
            return None
        if not meaning:
            return None
        by_id = {item["id"]: item for item in keyword}
        missing = [i for i, _ in meaning if i not in by_id]
        if missing:
            with self.connect() as db:
                marks = ",".join("?" * len(missing))
                for row in db.execute(f"SELECT *, 0.0 AS rank FROM items WHERE id IN ({marks})", missing):
                    item = self._row(row, body=False)
                    item.pop("rank", None)
                    by_id[item["id"]] = item
        fused = {}
        for rank, item in enumerate(keyword):
            fused[item["id"]] = 1 / (self.RRF_K + rank + 1)
        for rank, (item_id, cosine) in enumerate(meaning):
            fused[item_id] = fused.get(item_id, 0) + 1 / (self.RRF_K + rank + 1)
            by_id[item_id]["similarity"] = round(cosine, 3)
        now = _now()
        for item_id, score in fused.items():
            item = by_id[item_id]
            age = (now - datetime.fromisoformat(item["created_at"])).total_seconds() / 86400
            fused[item_id] = score + 0.002 * item["importance"] + 0.001 * math.exp(-age / 30)
            item["score"] = round(fused[item_id] * 100, 4)
        return sorted((by_id[i] for i in fused), key=lambda i: i["score"], reverse=True)

    def context(self, text, limit=5, max_chars=4000):
        """Compact recall block for injecting into an agent prompt as untrusted evidence."""
        lines, used = [], 0
        for item in self.search(text, limit=limit):
            line = f"[{item['id']}] {item['title']} — {item['preview']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return lines

    def stats(self):
        with self.connect() as db:
            kinds = {r["kind"]: r["n"] for r in db.execute(
                "SELECT kind, COUNT(*) AS n FROM items GROUP BY kind ORDER BY n DESC")}
            links = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        return {"items": sum(kinds.values()), "kinds": kinds, "links": links,
                "path": str(self.path)}
