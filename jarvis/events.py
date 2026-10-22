"""Live event feed: what every JARVIS process is doing, pushed to the console.

The console keeps a Bus (in-process pub/sub with a short replay buffer, served as
Server-Sent Events on /api/events). Other processes (the voice listener, `jarvis
chat`, `jarvis ask`) can't reach it, so they append to .jarvis/events.jsonl and the
console's Watcher tails that file. The Watcher also notices changes anyone makes:
new memory items, task lifecycle events, plan progress, the listener going up or down.

Every request, from any channel, is one `turn`:
  turn.start    {channel, message, text, tier, reasons, job?}
  turn.sentence {channel, message, text, job?}   streamed as the brain answers
  turn.done     {channel, message, reply_id, reply, tier, seconds, plan?, task?, job?}
  turn.error    {channel, message?, error, job?}
"""

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

FEED = "events.jsonl"
MAX_FEED = 1_000_000  # bytes before the feed rolls over to events.1.jsonl
RELAYED = ("turn.", "voice.", "kb.")  # kinds other processes may put on the feed


class Bus:
    def __init__(self, replay=300):
        self.lock = threading.Lock()
        self.seq, self.recent, self.subs = 0, deque(maxlen=replay), set()

    def publish(self, kind, data=None):
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "kind": kind, "at": time.time(), "data": data or {}}
            self.recent.append(event)
            for sub in list(self.subs):
                try:
                    sub.put_nowait(event)
                except queue.Full:  # a stalled client; it resyncs from the API on reconnect
                    pass
        return event

    def subscribe(self, after=None):
        """(queue, backlog): backlog is what a reconnecting client missed after `after`."""
        sub = queue.Queue(maxsize=2000)
        with self.lock:
            backlog = ([e for e in self.recent if e["seq"] > after]
                       if after is not None and after <= self.seq else [])
            self.subs.add(sub)
        return sub, backlog

    def unsubscribe(self, sub):
        with self.lock:
            self.subs.discard(sub)


def emit(state, kind, **data):
    """Append one event for the console to relay. Never raises: a missing console or a
    full disk must not break a spoken turn."""
    try:
        path = Path(state).resolve() / FEED
        try:
            if path.is_file() and path.stat().st_size > MAX_FEED:
                path.replace(path.with_name("events.1.jsonl"))
        except OSError:  # the console is mid-read (Windows); roll over next time
            pass
        line = json.dumps({"kind": kind, "at": time.time(), "pid": os.getpid(), "data": data},
                          default=str)
        with open(path, "a", encoding="utf-8") as feed:
            feed.write(line + "\n")
    except Exception:
        pass


class Turn:
    """One request's lifecycle as turn.* events, sent through `send(kind, data)`."""

    def __init__(self, send, channel, text, job=None):
        self.send, self.base = send, {"channel": channel, **({"job": job} if job else {})}
        self.text, self.message, self.t0 = text, None, time.time()

    def _send(self, kind, **data):
        try:
            self.send(kind, {**self.base, "message": self.message, **data})
        except Exception:  # reporting must never fail the turn itself
            pass

    def start(self, info):
        self.message = info.get("message")
        route = info.get("route") or {}
        self._send("turn.start", text=self.text, tier=route.get("tier"),
                   reasons=route.get("reasons", []))

    def sentence(self, text):
        self._send("turn.sentence", text=text)

    def done(self, result):
        memory = result.get("memory") or [None, None]
        self._send("turn.done", reply_id=memory[-1], reply=result.get("reply", ""),
                   tier=result.get("tier"), seconds=round(time.time() - self.t0, 1),
                   **{k: result[k] for k in ("plan", "task", "adapter", "streamed") if k in result})

    def error(self, exc):
        self._send("turn.error", error=f"{type(exc).__name__}: {exc}",
                   seconds=round(time.time() - self.t0, 1))


def feed_sender(state):
    return lambda kind, data: emit(state, kind, **data)


class Watcher:
    """Polls shared state and the feed, publishing what changed. Starts at 'now': only
    changes after the console started are announced."""

    def __init__(self, bus, state, memory, store, interval=0.75, vault=None):
        """vault: the Obsidian vault folder; edits there (from Obsidian or any JARVIS
        process) are announced as vault.changed, so the task board stays current."""
        self.bus, self.state, self.memory, self.store = bus, Path(state).resolve(), memory, store
        self.interval, self.stopped = interval, threading.Event()
        self.vault = Path(vault) if vault else None
        self._vault_at, self._vault_mark = 0, self._vault_stamp()
        self.feed = self.state / FEED
        self.offset = self.feed.stat().st_size if self.feed.is_file() else 0
        self.item_row = self._scalar(self.memory, "SELECT COALESCE(MAX(rowid), 0) FROM items")
        self.event_seq = self._scalar(self.store, "SELECT COALESCE(MAX(seq), 0) FROM events")
        self.plans = self._plan_mtimes()
        self.listening, self._voice_at = None, 0

    @staticmethod
    def _scalar(db_owner, sql):
        with db_owner.connect() as db:
            return db.execute(sql).fetchone()[0]

    def _plan_mtimes(self):
        try:
            return {p.stem: p.stat().st_mtime for p in (self.state / "plans").glob("plan-*.json")}
        except OSError:
            return {}

    def start(self):
        threading.Thread(target=self._loop, name="jarvis-watcher", daemon=True).start()
        return self

    def stop(self):
        self.stopped.set()

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self.poll()
            except Exception:  # a locked DB or a half-written file: try again next tick
                pass

    def poll(self):
        self._poll_feed()
        self._poll_memory()
        self._poll_tasks()
        self._poll_plans()
        self._poll_voice()
        self._poll_vault()

    def _vault_stamp(self):
        """(notes, newest mtime) over the vault's Markdown: changes when any note does."""
        if not self.vault:
            return None
        count, newest = 0, 0.0
        for folder, dirs, names in os.walk(self.vault):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.endswith(".md"):
                    count += 1
                    try:
                        newest = max(newest, os.stat(os.path.join(folder, name)).st_mtime)
                    except OSError:  # saved or deleted while we were walking
                        continue
        return count, newest

    def _poll_vault(self):
        if not self.vault or time.time() - self._vault_at < 3:  # a folder walk: every 3 s
            return
        self._vault_at = time.time()
        mark = self._vault_stamp()
        if mark != self._vault_mark:
            self._vault_mark = mark
            self.bus.publish("vault.changed", {"notes": mark[0]})

    def _poll_feed(self):
        if not self.feed.is_file():
            self.offset = 0
            return
        size = self.feed.stat().st_size
        if size < self.offset:  # rolled over
            self.offset = 0
        if size == self.offset:
            return
        with open(self.feed, "rb") as feed:
            feed.seek(self.offset)
            chunk = feed.read(size - self.offset)
        complete = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
        self.offset += complete
        for raw in chunk[:complete].splitlines():
            try:
                event = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            kind = event.get("kind") if isinstance(event, dict) else None
            if isinstance(kind, str) and len(kind) <= 40 and kind.startswith(RELAYED) \
                    and isinstance(event.get("data"), dict):
                self.bus.publish(kind, event["data"])

    def _poll_memory(self):
        with self.memory.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT rowid, id, kind, title, source, tags, created_at FROM items "
                "WHERE rowid > ? ORDER BY rowid LIMIT 200", (self.item_row,))]
        for row in rows:
            self.item_row = row.pop("rowid")
            row["tags"] = (row["tags"] or "").split()
            self.bus.publish("memory.added", row)

    def _poll_tasks(self):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT seq, task_id, kind, at FROM events WHERE seq > ? ORDER BY seq LIMIT 200",
                (self.event_seq,))]
        for row in rows:
            self.event_seq = row["seq"]
            self.bus.publish("task.event", {"task_id": row["task_id"], "state": row["kind"],
                                            "at": row["at"]})

    def _poll_plans(self):
        now = self._plan_mtimes()
        for plan_id, mtime in now.items():
            if self.plans.get(plan_id) != mtime:
                state = None
                try:
                    state = json.loads((self.state / "plans" / f"{plan_id}.json")
                                       .read_text(encoding="utf-8")).get("state")
                except (OSError, ValueError):
                    pass
                self.bus.publish("plan.updated", {"id": plan_id, "state": state})
        self.plans = now

    def _poll_voice(self):
        if time.time() - self._voice_at < 2:  # a process check, not a DB read: every 2 s
            return
        self._voice_at = time.time()
        from .daemon import _pid
        pid = _pid(self.state)
        if (pid is not None) != self.listening:
            first = self.listening is None
            self.listening = pid is not None
            if not first:
                self.bus.publish("voice.state", {"listening": self.listening, "pid": pid})
