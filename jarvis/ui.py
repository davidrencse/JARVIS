"""Local web console: `python -m jarvis ui` serves it on http://127.0.0.1:7777.

Standard library only. It binds to loopback, rejects foreign Host headers (DNS
rebinding) and requires an `X-JARVIS` header on every POST, which a cross-site
page can't send without a preflight this server never approves.

Endpoints read the same state the CLI uses (.jarvis/), so the console and
`python -m jarvis ...` always agree. /api/events streams what every channel is doing
(console, voice listener, CLI) as Server-Sent Events; see events.py.
"""

import ast
import json
import os
from .platforms import on_windows
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import parse_qs, urlparse

from . import __version__
from .credits import Credits
from .events import Bus, Turn, Watcher
from .fsio import read_json as _read_json
from .memory import Memory
from .probe import Prober
from .store import Store, lanes_from

WEB = Path(__file__).resolve().parent / "web"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}


def _host_name(header):
    """Host header -> bare lowercase hostname; handles "[::1]:7777"."""
    header = header.strip()
    if header.startswith("["):
        return header[1:].partition("]")[0].lower()
    return header.split(":")[0].lower()


class _Counted:
    """Wraps a body stream, counting bytes read, so the unread rest can be drained."""
    def __init__(self, stream):
        self.stream, self.n = stream, 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.n += len(data)
        return data


MAX_AUDIO = 16000 * 4 * 45  # 45 s of float32 mono at 16 kHz
# Tags the assistant puts on every exchange; as graph hubs they'd cluster unrelated items.
BOOKKEEPING_TAGS = {"inbound", "outbound", "simple", "medium", "complex", "plan", "result"}
SKIP = {".git", "__pycache__", ".jarvis", "jarvis.egg-info", "node_modules", ".pytest_cache",
        ".venv", ".ruff_cache", ".playwright-mcp", ".codex", ".claude", ".learnings"}

# External systems a module talks to, detected from its source text.
EXTERNALS = (
    ("ext:claude", "Claude Code CLI", "provider", r'which\("claude"\)|which\(provider\)'),
    ("ext:codex", "Codex CLI", "provider", r"which\(provider\)"),
    ("ext:elevenlabs", "ElevenLabs", "provider", r"elevenlabs\.io"),
    ("ext:whisper", "faster-whisper (RTX 4060)", "local-model", r"faster_whisper"),
    ("ext:sherpa", "sherpa-onnx voiceprint", "local-model", r"sherpa_onnx"),
    ("ext:memdb", "memory.sqlite3", "state", r"memory\.sqlite3"),
    ("ext:taskdb", "tasks.sqlite3", "state", r"tasks\.sqlite3"),
    ("ext:ssh", "Zenbook over SSH", "device", r'which\("ssh"\)'),
    ("ext:lanmouse", "Lan Mouse", "device", r"lan-mouse|lan_mouse"),
    ("ext:apis", "Owner APIs", "provider", r"def call_api"),
    ("ext:deepseek", "DeepSeek API", "provider", r"api\.deepseek\.com"),
    ("ext:openrouter", "OpenRouter API", "provider", r"openrouter\.ai"),
    ("ext:codexlogs", "~/.codex session logs", "state", r'"\.codex"'),
    ("ext:claudelogs", "~/.claude/projects logs", "state", r'"\.claude" / "projects"'),
)


_MODULES = {}  # str(path) -> ((mtime_ns, size), text, node or None, import targets)
_MODULES_LOCK = threading.Lock()


class _Ttl:
    """Tiny thread-safe TTL cache. Only successful results are stored, never exceptions."""

    def __init__(self, clock=time.monotonic):
        self._clock, self._lock, self._items = clock, threading.Lock(), {}

    def get(self, key, ttl, compute):
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = compute()  # outside the lock: it can take a second and must not block other keys
        with self._lock:
            self._items[key] = (self._clock(), value)
        return value

    def clear(self):
        with self._lock:
            self._items.clear()


_LINES = {}  # str(path) -> ((mtime_ns, size), line count), so /api/tree rereads only changed files
_LINES_LOCK = threading.Lock()
_EXT_USERS = {}  # one entry: (hash of every module's text) -> {ext_id: [users]}


def _parse_module(path):
    """(text, graph node, relative-import targets) for one module; re-parsed only when it changed."""
    key = str(path)
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    with _MODULES_LOCK:
        hit = _MODULES.get(key)
    if hit and hit[0] == stamp:
        return hit[1], hit[2], hit[3]
    text = path.read_text(encoding="utf-8")
    node, imports = None, []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                and not n.name.startswith("_")]
        node = {"id": f"mod:{path.stem}", "label": f"{path.stem}.py", "kind": "module",
                "loc": text.count("\n") + 1, "doc": doc[0] if doc else "", "defs": defs[:12]}
        for found in ast.walk(tree):
            if isinstance(found, ast.ImportFrom) and found.level == 1:
                imports += [found.module] if found.module else [a.name for a in found.names]
    with _MODULES_LOCK:
        _MODULES[key] = (stamp, text, node, imports)
    return text, node, imports


class Console:
    """Everything the HTTP layer exposes, kept separate so tests can call it directly."""

    def __init__(self, config, base, state):
        from .topology import is_node
        if is_node(config):
            raise ValueError("A node uses the main PC's console proxy, not a local JARVIS brain.")
        self.config, self.base, self.state = config, Path(base), Path(state).resolve()
        self.ttl = _Ttl()  # slow read-only endpoints; any POST clears it
        self.store = Store(self.state, lanes_from(config))
        from .memory import semantic_recall
        self.memory = Memory(self.state, semantic=semantic_recall(config))
        self.credits = Credits(self.state, config)
        self.prober = Prober(config, state=state)
        self.jobs, self._jobs_lock = {}, threading.Lock()
        self.max_jobs = 200  # the page only ever polls recent ones; older entries are dropped
        self._ears = self._stt = None
        self._ears_used = 0.0
        self._voice_lock = threading.Lock()
        self.bus, self.watcher = Bus(), None
        self._lane = self._speaker = self._agent = self._setup = None
        self._lane_lock = threading.Lock()
        self._kb_lock = threading.Lock()

    # ---- live: event stream, warm brain, speech ---------------------------------------
    def start_live(self, warm=True):
        """Start relaying shared-state changes, and warm the brain so the first ask is fast."""
        from .second_brain_runtime import start as archive_start
        if not getattr(self, "_archive", None):
            self._archive = archive_start(self.config, self.state)
        vault = self.vault()
        self.watcher = Watcher(self.bus, self.state, self.memory, self.store,
                               vault=vault.root if vault else None).start()
        if warm:
            def warm_up():
                try:
                    self.lane().warm()
                    self.bus.publish("brain.state", self.brain())
                except Exception as exc:  # no codex/claude: asks fall back to one-shot runs
                    self.bus.publish("brain.state", {**self.brain(), "error": str(exc)})
            threading.Thread(target=warm_up, daemon=True).start()
            from .pipeline import warm
            warm()  # the installed-app list, so the first direct action is fast
            self.memory.warm_vectors()  # meaning-based recall index, in the background
            from .consolidate import start as consolidate_daily
            consolidate_daily(self.config, self.memory, self.base)  # memory.consolidate: daily
        return self

    def close(self):
        if getattr(self, "_archive", None):
            from .second_brain_runtime import stop as archive_stop
            archive_stop(self._archive)
            self._archive = None
        if self.watcher:
            self.watcher.stop()
        if self._lane:
            self._lane.close()

    def settings_snapshot(self):
        from .memory import semantic_recall
        acts = {k: v for k, v in (self.config.get("actions") or {}).items()
                if k in ("enabled", "browser", "grokbot")}
        return (self.config.get("routing", {}).get("brain", "chatgpt"), semantic_recall(self.config), acts)

    def settings_applied(self, before):
        """A saved brain or recall-mode change takes effect now, not at the next restart."""
        brain, semantic, acts = self.settings_snapshot()
        if brain != before[0] or acts != before[2]:  # the brain's tools are built with it
            with self._lane_lock:
                lane, self._lane = self._lane, None  # rebuilt lazily by lane()
            if lane:
                try:
                    lane.close()
                except Exception:
                    pass
            self.bus.publish("brain.state", self.brain())
        if semantic != before[1]:
            self.memory = Memory(self.state, semantic=semantic)
            if self.watcher and hasattr(self.watcher, "memory"):
                self.watcher.memory = self.memory
            if semantic:
                self.memory.warm_vectors()

    def lane(self):
        """The conversation brain the voice listener and `jarvis chat` use, kept warm."""
        with self._lane_lock:
            if self._lane is None:
                from .chatgpt import make_lane
                self._lane = make_lane(self.config, self.base, self.state)
            return self._lane

    def brain(self):
        name = self.config.get("routing", {}).get("brain", "chatgpt")
        proc = getattr(self._lane, "proc", None)
        return {"name": "Claude" if name == "claude" else "ChatGPT",
                "warm": bool(proc and proc.poll() is None)}

    def speaker(self):
        with self._lane_lock:
            if self._speaker is None:
                from .speech import SpeechQueue, Voice
                self._speaker = SpeechQueue(Voice(self.state, self.config))
            return self._speaker

    def voice_preview(self):
        """Say a sample line on this laptop in the voice and tuning now saved."""
        from .speech import Voice
        return Voice(self.state, self.config).say(
            "Good evening. This is how I'll sound from now on.", wait=False)

    # ---- ElevenLabs agent: the live voice front end --------------------------------
    def agent(self):
        with self._lane_lock:
            if self._agent is None:
                from .agent import Agent
                self._agent = Agent(self.state, self.config)
            return self._agent

    def agent_ask(self, text, wait=110):
        """The agent's ask_jarvis tool: run a normal console ask and return its reply."""
        job = self.submit(text)
        deadline = time.time() + wait  # under the tool's 120 s timeout
        while job["state"] == "running" and time.time() < deadline:
            time.sleep(0.1)
        if job["state"] == "running":
            return {"reply": "That's still running; the answer will appear in the console.",
                    "job": job["id"]}
        if job["state"] == "error":
            return {"reply": f"That failed: {job['error']}", "job": job["id"], "error": True}
        from .speech import spoken
        reply = job["result"].get("reply", "")
        return {"reply": spoken(reply, 1200), "job": job["id"]}

    # ---- vault tasks -----------------------------------------------------------------
    def vault(self):
        from .vault import vault_for
        try:
            return vault_for(self.config, self.state)
        except (ValueError, OSError):
            return None

    def tasks(self):
        from .tasks import tasks_for
        vault = self.vault()
        if vault is None:
            raise ValueError("No vault configured: set vault.path in jarvis.config.json.")
        return tasks_for(vault)

    def vault_tasks(self, query=""):
        """Vault task agenda buckets plus fields needed by console API clients."""
        tasks = self.tasks()
        agenda = tasks.agenda(query=query or "")
        for name in ("overdue", "today", "in_progress", "upcoming", "later", "anytime", "done"):
            agenda[name] = [{k: v for k, v in t.items() if k not in ("raw", "mark")}
                            for t in agenda[name]]
        return {**agenda, "vault": tasks.vault.root.name, "writable": tasks.vault.writable}

    def add_task(self, body):
        made = self.tasks().add(body.get("text"), body.get("due") or None,
                                priority=body.get("priority") or None,
                                note=body.get("note") or None, channel="console")
        self.bus.publish("vault.changed", {"task": made.get("id")})
        return {k: v for k, v in made.items() if k not in ("raw", "mark")}

    def update_task(self, task_id, body):
        result = self.tasks().update(task_id, body.get("action", ""), body.get("value") or None,
                                     channel="console")
        self.bus.publish("vault.changed", {"task": task_id})
        return result

    def vault_brief(self):
        """The spoken morning briefing (briefing.py) and the brain's world summary."""
        from .briefing import answer
        tasks = self.tasks()
        return {"brief": answer("brief me", tasks.vault, tasks, memory=self.memory),
                "world": tasks.vault.world()}

    def task_counts(self):
        try:
            counts = self.tasks().agenda()["counts"]
        except Exception:  # no vault, or a note mid-save: the header just omits it
            return None
        return {k: counts[k] for k in ("overdue", "today", "in_progress", "upcoming")}

    # ---- overview ----------------------------------------------------------------
    def overview(self):
        from . import daemon
        tasks = self.store.overview()
        try:
            voice = daemon.status(self.state)
        except Exception as exc:  # status must never take the console down
            voice = {"listening": False, "error": str(exc)}
        month = time.strftime("%Y-%m", time.gmtime())
        usage = _read_json(self.state / "voice-usage.json", {})
        from .topology import enabled, identity
        cluster = {"enabled": enabled(self.config)}
        if cluster["enabled"]:
            from .cluster import _HUBS
            local, main = identity(self.config)
            hub = _HUBS.get(str(self.state))
            cluster.update(local_device=local, main_device=main, role="main",
                           nodes=hub.broker.nodes() if hub else [])
        return {
            "version": __version__, "now": time.time(),
            "cluster": cluster,
            "devices": self.devices(),
            "desktop": self.config.get("desktop", {}),
            "agents": self.config.get("personal_agents", {}),
            "network": self.config.get("network", {}),
            "routing": self.config.get("routing", {}),
            "models": self.config.get("models", {}),
            "memory": {**self.memory.stats(), "recent": self.memory.search("", limit=8)},
            "archive": self.archive_status(),
            "tasks": tasks,
            "orchestration": {"lanes": self.store.lanes},
            "todo": self.task_counts(),
            "plans": self.plans(),
            "brain": self.brain(),
            "live": {"clients": len(self.bus.subs), "seq": self.bus.seq},
            "voice": {**voice, "voiceprint": (self.state / "voice" / "voiceprint.json").is_file(),
                      "chars_this_month": usage.get(month, 0),
                      "budget": self.config.get("voice", {}).get("monthly_char_budget", 30000),
                      "tts_model": self.config.get("voice", {}).get("model"),
                      "stt_model": self.config.get("voice", {}).get("stt_model"),
                      "stt_engine": self.config.get("voice", {}).get("stt_engine", "parakeet")},
            "tools": {name: shutil.which(name) is not None
                      for name in ("claude", "codex", "ssh", "tailscale", "lan-mouse")},
        }

    def archive_status(self):
        """Second-brain archive lifecycle: numbers and a state word only, never record text."""
        from . import second_brain_runtime
        try:
            return self.ttl.get("archive", 5, lambda: second_brain_runtime.status(self.config, self.state))
        except Exception as exc:  # status must never take the console down
            return {"state": "unverified", "error": type(exc).__name__}

    def devices(self):
        from .topology import enabled, identity
        local = main = None
        if enabled(self.config):
            try:
                local, main = identity(self.config)
            except ValueError:  # unknown identity: show every device as remote
                pass
        # Who this one can send files to: over the cluster, its peers (transfer.peers).
        reachable = ([n for n in self.config.get("devices", {}) if n != local] if local == main
                     else [main]) if main else []
        nodes = {}
        if main is not None:
            try:
                from .cluster import _HUBS
                hub = _HUBS.get(str(Path(self.state).resolve()))
                if hub is not None:
                    nodes = {n["id"]: n for n in hub.broker.nodes()}
            except Exception:
                nodes = {}
        from . import hostmeter
        from .connections import presence as node_presence, busy_nodes
        busy = busy_nodes(self.state)
        out = []
        for name, device in self.config.get("devices", {}).items():
            inv = _read_json(self.state / "inventory" / f"{name}.json", {})
            hw = inv.get("hardware", {}) if isinstance(inv.get("hardware"), dict) else {}
            node = nodes.get(name) or {}
            if name == local:
                presence, here = "online", True
            elif node:
                presence, here = node_presence(node.get("seen"), name in busy), False
            else:
                presence, here = None, False
            out.append({"id": name, "hostname": device.get("hostname"), "label": device.get("label"),
                        "here": name == local,
                        "role": None if main is None else "main" if name == main else "node",
                        # Same rule as transfer.peers / ssh_device.
                        "send": name != local and (name in reachable if main else
                                device.get("transport") == "ssh" and bool(device.get("target"))),
                        "transport": device.get("transport"), "status": device.get("status"),
                        "address": device.get("lan_ip") or device.get("target"),
                        "platform": inv.get("platform"), "collected_at": inv.get("collected_at"),
                        "model": _hw_model(hw), "cpu": _hw_cpu(hw), "ram": _hw_ram(hw),
                        "gpu": inv.get("nvidia") if isinstance(inv.get("nvidia"), str) else None,
                        "tools": inv.get("tools", {}),
                        "meters": hostmeter.summarize(device, inv, node, here=here, presence=presence)})
        return out

    def plans(self):
        out = []
        paths = sorted((self.state / "plans").glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for path in paths[:12]:
            out.append({**_plan_view(_read_json(path, {})), "updated_at": path.stat().st_mtime})
        return out

    def plan(self, plan_id):
        from .orchestrator import Orchestrator
        record = Orchestrator(self.store, self.memory, self.config, self.base).load(plan_id)
        return _plan_view(record, full=True)

    def run_plan(self, plan_id):
        from .orchestrator import Orchestrator
        orch = Orchestrator(self.store, self.memory, self.config, self.base)
        record = orch.load(plan_id)
        if record["state"] in ("finished", "blocked"):
            raise ValueError(f"Plan is {record['state']}; nothing left to run.")
        job = {"id": uuid.uuid4().hex[:12], "text": record["goal"], "plan": plan_id,
               "state": "running", "started": time.time(), "route": {"tier": "complex"},
               "result": None, "error": None}
        with self._jobs_lock:
            # Plans run in the task ledger; conversation runs on the brain and can go on.
            if any(j["state"] == "running" and j.get("plan") for j in self.jobs.values()):
                raise ValueError("Another plan is still running; run this one after it.")
            self._remember(job)

        def work():
            try:
                job.update(state="done", result=_plan_view(orch.run(plan_id), full=True))
            except Exception as exc:
                job.update(state="error", error=f"{type(exc).__name__}: {exc}")
            job["finished"] = time.time()
            self.bus.publish("plan.updated", {"id": plan_id, "state": (job["result"] or {})
                                              .get("state", "blocked"), "error": job["error"]})
        threading.Thread(target=work, daemon=True).start()
        return job

    # ---- cases: an investigation is a `case` item plus everything filed into it ----
    def cases(self):
        with self.memory.connect() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT id, title, tags, created_at FROM items WHERE kind='case' "
                "ORDER BY created_at DESC")]
        out = []
        for row in rows:
            slug = next((t[5:] for t in (row["tags"] or "").split() if t.startswith("case:")), None)
            if slug:
                out.append({"id": row["id"], "slug": slug, "title": row["title"],
                            "created_at": row["created_at"], "items": len(self.case_ids(slug)) - 1})
        return out

    def create_case(self, name):
        name = (name or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        if not slug:
            raise ValueError("Give the investigation a name.")
        if any(c["slug"] == slug for c in self.cases()):
            raise ValueError(f"An investigation called {slug} already exists.")
        item = self.memory.add("case", f"Investigation: {name}", title=name[:200],
                               tags=["case", f"case:{slug}"], importance=0.8,
                               meta={"slug": slug})
        return {**item, "slug": slug}

    def case_item(self, slug):
        with self.memory.connect() as db:
            row = db.execute("SELECT id FROM items WHERE kind='case' AND "
                             "(' ' || tags || ' ') LIKE ?", (f"% case:{slug} %",)).fetchone()
        if not row:
            raise LookupError(f"No investigation called {slug}.")
        return row["id"]

    def case_ids(self, slug):
        """The case item, items tagged case:<slug>, and items linked part-of the case."""
        with self.memory.connect() as db:
            ids = {r["id"] for r in db.execute(
                "SELECT id FROM items WHERE (' ' || tags || ' ') LIKE ?", (f"% case:{slug} %",))}
            for cid in [i for i in ids if i.startswith("case:")]:
                ids |= {r["src"] for r in db.execute(
                    "SELECT src FROM links WHERE dst=? AND rel='part-of'", (cid,))}
        return ids

    def file_into_case(self, slug, item_ids):
        case_id = self.case_item(slug)
        for item_id in item_ids:
            if item_id and item_id != case_id:
                self.memory.link(item_id, case_id, "part-of")

    # ---- knowledge engine ------------------------------------------------------------
    def kb(self):
        with self._kb_lock:  # two first requests must not build two engines with two locks
            if getattr(self, "_kb", None) is None:
                from .knowledge import kb_for
                self._kb = kb_for(self.config, self.state, on_event=lambda kind, data: self.bus.publish(kind, data))
            return self._kb

    def kb_command(self, text):
        """The knowledge console's command line. Runs in the background; progress and the
        result arrive as kb.* events. Local files are CLI-only (`jarvis kb ingest FILE`)."""
        text = re.sub(r"^\s*>\s*", "", str(text or "")).strip()
        words = text.split()
        if not words:
            raise ValueError("Type a command: ingest URL, note TEXT, ask QUESTION, check.")
        verb = words[0].lower()
        if verb in ("reader", "librarian", "checker") and len(words) > 1:
            words, verb = words[1:], words[1].lower()
        rest = " ".join(words[1:]).strip()
        kb = self.kb()
        if verb in ("ingest", "add", "read", "clip"):
            if not re.match(r"https?://", rest):
                raise ValueError("From the console the reader takes links; for files use "
                                 "`jarvis kb ingest FILE`, for text `note ...`.")
            job = lambda: kb.ingest(rest)
        elif verb in ("note", "remember"):
            job = lambda: kb.ingest(text=rest)
        elif verb in ("check", "audit"):
            job = kb.check
        elif verb in ("sync", "vault"):
            vault = self.vault()
            if vault is None:
                raise ValueError("No vault configured: set vault.path in jarvis.config.json.")
            job = lambda: kb.sync_vault(vault)
        elif verb == "digest":
            job = lambda: kb.digest(rest)
        elif verb == "resolve":
            if len(words) not in (3, 4):
                raise ValueError("resolve CLAIM keep|drop|both [AGAINST-CLAIM]")
            job = lambda: kb.resolve(words[1], words[2].lower(), against=(words[3:] or [None])[0])
        elif verb == "merge":
            if len(words) != 3:
                raise ValueError("merge FROM-PAGE INTO-PAGE")
            job = lambda: kb.merge(words[1], words[2])
        else:
            question = rest if verb in ("ask", "answer") else text
            job = lambda: kb.ask(question)

        def run():
            try:
                job()
            except Exception as exc:
                self.bus.publish("kb.error", {"bot": "jarvis", "action": "error",
                                              "message": str(exc) or type(exc).__name__})
        threading.Thread(target=run, daemon=True).start()
        return {"accepted": text}

    KB_UPLOAD_LIMIT = 200 * 1024 * 1024

    def kb_upload(self, stream, length, name):
        """A file dropped on the knowledge console: streamed to the engine's inbox, then
        handed to the reader in the background (PDFs and audio take a while)."""
        from .knowledge import AUDIO_KINDS, TEXT_KINDS
        name = Path(str(name or "")).name
        ext = Path(name).suffix.lower()
        if not name or ext not in TEXT_KINDS | AUDIO_KINDS | {".pdf"}:
            raise ValueError("Drop text, markdown, HTML, PDF or audio files.")
        if not 0 < length <= self.KB_UPLOAD_LIMIT:
            raise ValueError("Files up to 200 MB.")
        kb = self.kb()
        inbox = kb.root / "inbox"
        inbox.mkdir(exist_ok=True)
        target = inbox / f"{int(time.time() * 1000)}-{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}"
        left = length
        try:
            with open(target, "wb") as out:
                while left:
                    chunk = stream.read(min(1 << 20, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            if left:
                raise ValueError("The upload was cut short.")
        except BaseException:  # reset, stall timeout, short body: leave nothing in the inbox
            target.unlink(missing_ok=True)
            raise

        def run():
            try:
                kb.ingest(target, title=Path(name).stem.replace("-", " ").replace("_", " "))
            except Exception as exc:
                self.bus.publish("kb.error", {"bot": "reader", "action": "error",
                                              "message": f"{name}: {exc}"})
            finally:
                target.unlink(missing_ok=True)
        threading.Thread(target=run, daemon=True).start()
        return {"accepted": name, "bytes": length}

    def search(self, query):
        limit = max(1, min(int(query.get("limit", 40)), 200))
        case = query.get("case") or None
        floor = float(query.get("min_importance") or 0)
        # both filters run in the SQL, before LIMIT: filtering the newest `limit` rows
        # afterwards left "High only" and old case items nearly empty (audit SU-2)
        return self.memory.search(query.get("q", ""), query.get("kind") or None,
                                  query.get("tag") or None, query.get("since") or None,
                                  limit, min_importance=floor or None,
                                  ids=self.case_ids(case) if case else None)

    def capture(self, body):
        """Save a finding: kind, title, tags, importance, optional source URL and case."""
        kind = (body.get("kind") or "note").strip().lower()
        tags = [t.strip().lstrip("#") for t in body.get("tags") or [] if t and t.strip()]
        case = body.get("case") or None
        if case:
            self.case_item(case)
            tags.append(f"case:{case}")
        meta = {}
        url = (body.get("source_url") or "").strip()
        if url:
            if not re.match(r"https?://", url):
                raise ValueError("Source URL must start with http:// or https://.")
            meta["source_url"] = url[:2000]
        item = self.memory.add(kind, body.get("text"), title=body.get("title") or "",
                               tags=tags, importance=float(body.get("importance", 0.5)),
                               meta=meta)
        if case:  # also files a duplicate body, which keeps its original tags
            self.file_into_case(case, [item["id"]])
        return self.memory.get(item["id"])

    def history(self, limit=40):
        """Requests and their replies from every channel: console, voice listener, CLI."""
        out = []
        with self.memory.connect() as db:
            msgs = [dict(r) for r in db.execute(
                "SELECT id, body, tags, meta, created_at FROM items WHERE kind='message' "
                "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))]
            for m in msgs:
                reply = db.execute(
                    "SELECT i.id, i.body, i.meta, i.created_at FROM links l JOIN items i "
                    "ON i.id = l.src WHERE l.dst=? AND l.rel='answers' ORDER BY i.created_at "
                    "LIMIT 1", (m["id"],)).fetchone()
                tags = (m["tags"] or "").split()
                entry = {"id": m["id"], "text": m["body"][:4000], "at": m["created_at"],
                         "tier": next((t for t in tags if t in ("simple", "medium", "complex")),
                                      None), "reply": None,
                         "channel": json.loads(m["meta"] or "{}").get("channel")}
                if reply:
                    entry["reply"] = {"id": reply["id"], "body": reply["body"][:8000],
                                      "at": reply["created_at"],
                                      "meta": json.loads(reply["meta"] or "{}")}
                out.append(entry)
        return list(reversed(out))

    def export(self, slug):
        """One investigation as Markdown: every item, its source, tags and links."""
        items = sorted((self.memory.get(i) for i in self.case_ids(slug)),
                       key=lambda i: i["created_at"])
        if not items:
            raise LookupError(f"No investigation called {slug}.")
        case = next((i for i in items if i["kind"] == "case"), None)
        lines = [f"# {case['title'] if case else slug}", "",
                 f"Exported from JARVIS {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"{len(items) - bool(case)} items", ""]
        for item in items:
            if item["kind"] == "case":
                continue
            tags = " ".join("#" + t for t in item["tags"] if not t.startswith("case:"))
            lines += [f"## {item['title']}", "",
                      f"`{item['id']}` · {item['kind']} · "
                      f"{item['created_at'][:16].replace('T', ' ')} UTC · importance "
                      f"{item['importance']}" + (f" · {tags}" if tags else ""), ""]
            if item["meta"].get("source_url"):
                lines += [f"Source: {item['meta']['source_url']}", ""]
            lines += [item["body"].strip(), ""]
            links = [l for l in item["links"] if l["src"] == item["id"] and l["rel"] != "part-of"]
            if links:
                lines += ["Links: " + ", ".join(f"{l['rel']} → `{l['dst']}`" for l in links), ""]
        return "\n".join(lines)

    # ---- graphs ------------------------------------------------------------------
    def system_graph(self):
        """Module graph from the real import statements, plus what each module touches."""
        package = self.base / "jarvis"
        nodes, edges, sources = [], [], {}
        paths = sorted(package.glob("*.py"))
        live = {str(p) for p in paths}
        with _MODULES_LOCK:
            for key in [k for k in _MODULES if k.startswith(f"{package}{os.sep}") and k not in live]:
                del _MODULES[key]  # deleted modules drop out of the cache
        for path in paths:
            name = path.stem
            text, node, imports = _parse_module(path)
            sources[name] = text
            if node is None:
                continue
            nodes.append(node)
            for target in imports:
                if (package / f"{target}.py").is_file() and target != name:
                    edges.append({"src": f"mod:{name}", "dst": f"mod:{target}", "rel": "imports"})
        # The regex sweep over every module is the slow part; redo it only when some text changed.
        stamp = tuple((m, hash(t)) for m, t in sources.items())
        if _EXT_USERS.get("stamp") != stamp:
            _EXT_USERS["stamp"] = None
            _EXT_USERS["users"] = {
                # ui.py holds this pattern table itself, so it would match everything.
                e[0]: [m for m, text in sources.items() if m != "ui" and re.search(e[3], text)]
                for e in EXTERNALS}
            _EXT_USERS["stamp"] = stamp
        for ext_id, label, kind, pattern in EXTERNALS:
            users = _EXT_USERS["users"][ext_id]
            if users:
                nodes.append({"id": ext_id, "label": label, "kind": kind})
                edges += [{"src": f"mod:{m}", "dst": ext_id, "rel": "uses"} for m in users]
        for agent in self.specialists():
            nodes.append({"id": f"agent:{agent['name']}", "label": agent["name"],
                          "kind": "specialist", "doc": agent["description"],
                          "power": agent["power"], "model": agent["model"]})
            edges.append({"src": "mod:orchestrator", "dst": f"agent:{agent['name']}",
                          "rel": "delegates"})
        for name, device in self.config.get("devices", {}).items():
            nodes.append({"id": f"dev:{name}", "label": device.get("hostname", name),
                          "kind": "device", "doc": device.get("status", "")})
            edges.append({"src": "mod:devices" if device.get("transport") == "ssh"
                          else "mod:inventory", "dst": f"dev:{name}", "rel": "reaches"})
        for name, agent in self.config.get("personal_agents", {}).items():
            nodes.append({"id": f"pa:{name}", "label": name.capitalize(), "kind": "personal-agent",
                          "doc": f"{agent.get('status', '')} · {agent.get('integration', '')}"})
            edges.append({"src": "mod:router", "dst": f"pa:{name}", "rel": "routes"})
        ids = {n["id"] for n in nodes}
        return {"nodes": nodes, "edges": [e for e in edges if e["src"] in ids and e["dst"] in ids]}

    def specialists(self):
        out = []
        for path in sorted((self.base / ".claude" / "agents").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            meta = dict(re.findall(r"^(\w+):\s*(.+)$", text.split("---")[1] if text.startswith(
                "---") else "", re.M))
            out.append({"name": meta.get("name", path.stem),
                        "description": meta.get("description", ""),
                        "model": meta.get("model"), "power": meta.get("power")})
        return out

    def heap_graph(self, limit=400, case=None):
        """Memory items, their typed links, and hub nodes for kinds and tags.

        With `case`, only that investigation's items plus one hop over their links."""
        cols = "id, kind, title, tags, importance, created_at, source"
        with self.memory.connect() as db:
            links = [dict(r) for r in db.execute("SELECT src, dst, rel FROM links")]
            if case:
                keep = self.case_ids(case)
                keep |= {l["dst"] for l in links if l["src"] in keep}
                keep |= {l["src"] for l in links if l["dst"] in keep}
                marks = ",".join("?" * len(keep)) or "''"
                items = [dict(r) for r in db.execute(
                    f"SELECT {cols} FROM items WHERE id IN ({marks})", list(keep))]
            else:
                items = [dict(r) for r in db.execute(
                    f"SELECT {cols} FROM items ORDER BY created_at DESC LIMIT ?", (limit,))]
        nodes, edges, hubs = [], [], set()
        for item in items:
            nodes.append({"id": item["id"], "label": item["title"][:60], "kind": "item",
                          "group": item["kind"], "importance": item["importance"],
                          "created_at": item["created_at"], "source": item["source"]})
            hub = f"kind:{item['kind']}"
            hubs.add((hub, item["kind"], "kind"))
            edges.append({"src": item["id"], "dst": hub, "rel": "is"})
            for tag in (item["tags"] or "").split():
                if tag in BOOKKEEPING_TAGS or tag.startswith("plan-"):
                    continue
                hubs.add((f"tag:{tag}", f"#{tag}", "tag"))
                edges.append({"src": item["id"], "dst": f"tag:{tag}", "rel": "tagged"})
        nodes += [{"id": h, "label": label, "kind": kind} for h, label, kind in sorted(hubs)]
        ids = {n["id"] for n in nodes}
        edges += [l for l in links if l["src"] in ids and l["dst"] in ids]
        return {"nodes": nodes, "edges": edges}

    def tree(self):
        """Project layout with sizes and module summaries (cached 30 s)."""
        return self.ttl.get("tree", 30, self._tree)

    def _tree(self):
        def lines(child, st):
            stamp, key = (st.st_mtime_ns, st.st_size), str(child)
            with _LINES_LOCK:
                hit = _LINES.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            n = child.read_text(encoding="utf-8").count("\n") + 1
            with _LINES_LOCK:
                _LINES[key] = (stamp, n)
            return n

        def walk(path, depth=0):
            entries = []
            for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if child.name in SKIP or (child.name.startswith(".env") and child.name != ".env.example"):
                    continue
                if child.suffix in (".pyc", ".woff2") or child.name == "secrets.json":
                    continue
                if child.is_dir():
                    if depth < 3:
                        entries.append({"name": child.name, "dir": True,
                                        "children": walk(child, depth + 1)})
                else:
                    st = child.stat()
                    info = {"name": child.name, "dir": False, "bytes": st.st_size}
                    if child.suffix in (".py", ".md", ".js", ".css", ".html", ".json", ".toml"):
                        try:
                            info["lines"] = lines(child, st)
                        except (UnicodeDecodeError, OSError):
                            pass
                    entries.append(info)
            return entries
        return {"root": self.base.name, "children": walk(self.base)}

    # ---- asking ------------------------------------------------------------------
    def route(self, text, tier=None):
        """What would happen to `text`, decided exactly as ask() will decide it."""
        from .assistant import decide
        from .router import pick_adapter
        if tier not in (None, "", "simple", "medium", "complex"):
            raise ValueError("tier must be simple, medium or complex.")
        route = decide(text or "", self.config, tier or None, conversational=True, state=self.state)
        if route.get("instant"):
            route["handler"] = {"clock": "instant answer", "vault": "vault (no model)"}[route["instant"]]
        elif route["tier"] == "simple":
            route["handler"] = f"{self.brain()['name']} brain"
        elif route["tier"] == "medium":
            route["adapter"] = pick_adapter(text or "", self.config)
            route["handler"] = route["adapter"].get("name")
        else:
            route["handler"] = "specialist planner"
        return route

    def submit(self, text, tier=None, speak=False, case=None, origin_device=None, write=False, approval_device=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Say or type something for JARVIS to do.")
        if tier not in (None, "", "simple", "medium", "complex"):
            raise ValueError("tier must be simple, medium or complex.")
        job = {"id": uuid.uuid4().hex[:12], "text": text.strip(), "state": "running",
               "started": time.time(), "route": self.route(text.strip(), tier), "result": None,
               "error": None, "partial": "", "message": None}
        from .topology import ORIGIN
        job["origin_device"] = origin_device or ORIGIN.get()
        job["approval_device"] = approval_device
        job["write"] = bool(write)
        if case:
            self.case_item(case)
            job["case"] = case
        with self._jobs_lock:
            if any(j["state"] == "running" and not j.get("plan") for j in self.jobs.values()):
                raise ValueError("JARVIS is still on the last request.")
            self._remember(job)
        threading.Thread(target=self._work, args=(job, tier or None, speak), daemon=True).start()
        return job

    def _work(self, job, tier, speak):
        from .assistant import ask
        turn = Turn(self.bus.publish, "console", job["text"], job=job["id"])
        try:
            voice = self.speaker() if speak else None
        except Exception:  # speech isn't set up; answer silently
            voice = None
        budget = [voice.voice.limit if voice else 0]

        def started(info):
            job.update(message=info["message"], route={**job["route"], **info["route"]})
            turn.start(info)

        def sentence(text):
            job["partial"] = f"{job['partial']} {text}".strip()
            turn.sentence(text)
            if voice and budget[0] > 0:  # speak as it streams, up to the spoken cap
                budget[0] -= len(text)
                voice.put(text)

        try:
            from .topology import request_from, approvals_on
            with request_from(job.get("origin_device")), approvals_on(job.get("approval_device")):
                result = ask(job["text"], self.store, self.memory, self.config, self.base, tier,
                             write=job.get("write", False),
                             lane=None if job.get("write") else self.lane(),
                             on_sentence=sentence, on_start=started,
                             on_step=turn.step, channel="console")
            if job.get("case"):
                ids = list(result.get("memory", []))
                if result.get("plan"):
                    ids.append(self.plan(result["plan"]).get("memory_id"))
                self.file_into_case(job["case"], ids)
            if voice:
                from .speech import spoken
                if not result.get("streamed"):
                    voice.put(spoken(result["reply"], voice.voice.limit))
                result["voice"] = {"source": voice.voice.engine,
                                   "streamed": bool(result.get("streamed"))}
            job.update(state="done", result=result, finished=time.time())
            turn.done(result)
        except Exception as exc:
            job.update(state="error", error=f"{type(exc).__name__}: {exc}", finished=time.time())
            turn.error(exc)

    def _remember(self, job):
        """Add a job and forget the oldest finished ones. Caller holds _jobs_lock."""
        self.jobs[job["id"]] = job
        if len(self.jobs) > self.max_jobs:
            done = [k for k, j in self.jobs.items() if j["state"] != "running"]
            for key in done[:len(self.jobs) - self.max_jobs]:  # dicts keep insertion order
                del self.jobs[key]

    def job(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
        if not job:
            raise LookupError("Unknown job.")
        return job

    # ---- hearing -----------------------------------------------------------------
    def transcribe(self, raw, verify=True, wake=False):
        """Float32 mono 16 kHz from the browser → (owner check) → local Whisper text.
        `wake` is the always-on mic: only speech that starts with "Jarvis" comes back;
        anything else is dropped here, so the page never even sees the words."""
        from . import daemon
        from .listen import RATE, Transcriber, VoicePrint, _np, wake as wake_word
        np = _np()
        if len(raw) < RATE * 4 * 0.3 or len(raw) % 4:
            raise ValueError("That clip was too short to hear anything.")
        audio = np.frombuffer(raw, dtype="<f4").astype("float32")
        seconds = round(len(audio) / RATE, 2)
        # The background listener shares the mic and answers every "Jarvis, ..." itself;
        # answering here too would run each request twice.
        # A node's console forwards its mic here: the main's listener and voiceprint belong to
        # the main's own mic, so neither judges (or learns from) a clip from another laptop.
        from .topology import ORIGIN, identity
        try:
            main = identity(self.config)[1]
        except (ValueError, KeyError, TypeError):
            main = None
        from_node = ORIGIN.get() not in (None, main)
        listener = daemon._pid(self.state) is not None and not from_node
        if from_node:
            verify = False
        if wake and listener:
            return {"text": "", "listener": True, "seconds": seconds}
        with self._voice_lock:
            self._ears_used = time.monotonic()
            if self._stt is None:
                self._stt, self._ears = Transcriber(self.config), VoicePrint(self.state, self.config)
                self._free_ears_later()
            owner, score, near = True, None, False
            if verify and (self.config.get("voice") or {}).get("speaker_check", True) is False:
                owner, near = False, True  # check off: wake word for always-on, as the listener
            elif verify and (self.state / "voice" / "voiceprint.json").is_file():
                # Score only: browser audio (echo cancel, noise suppression, auto gain) is a
                # different chain from the listener's mic, so it never teaches the voiceprint.
                owner, score = self._ears.verify(audio, learn=False)
                if not owner:
                    # A near miss (far from the mic, quieter) counts for push-to-talk, a
                    # deliberate key press here; always-on needs "Jarvis" too, like the listener.
                    from .daemon import BORDERLINE
                    near = score >= (self.config.get("voice") or {}).get("near_miss", BORDERLINE)
                    owner = near and not wake
            text = self._stt(audio) if owner or near else ""
        request = wake_word(text)
        if near and wake:
            owner = request is not None
            text = text if owner else ""
        heard = {"owner": owner, "score": score, "addressed": request is not None,
                 "device": getattr(self._stt, "device", None), "seconds": seconds}
        if request is not None and listener:
            return {**heard, "text": "", "listener": True}
        if request is None:  # push-to-talk needs no wake word; always-on does
            request = "" if wake else text
        return {**heard, "text": request}

    EARS_IDLE = 300  # seconds of no page audio before Whisper and speaker ID are freed

    def _free_ears_later(self):
        """The hub mostly serves; Whisper + speaker ID (~1 GB) are only needed while the
        page is the one hearing (no background listener, or push-to-talk)."""
        def check():
            with self._voice_lock:
                idle = time.monotonic() - self._ears_used
                if idle >= self.EARS_IDLE:
                    self._stt = self._ears = None
                    return
            self._free_ears_later()
        timer = threading.Timer(self.EARS_IDLE, check)
        timer.daemon = True
        timer.start()

    def send_file(self, stream, length, name, device=None):
        """Receive an uploaded file from the page and push it to the other laptop."""
        import tempfile
        from . import transfer
        if length <= 0:
            raise ValueError("That file is empty.")
        if length > transfer.MAX_BYTES:
            raise ValueError("That file is too large to send this way.")
        uploads = Path(self.state) / "uploads"  # on disk: /tmp may be a RAM tmpfs (2 GB cap)
        uploads.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=uploads) as folder:
            path = Path(folder) / transfer.safe_name(name)
            left = length
            with open(path, "wb") as out:
                while left:
                    block = stream.read(min(1 << 20, left))
                    if not block:
                        raise ValueError("The upload ended early.")
                    out.write(block)
                    left -= len(block)
            result = transfer.send_file(self.config, self.state, path, device or None, path.name)
        self.bus.publish("transfer.sent", {"name": result["sent"], "bytes": result["bytes"]})
        return result

    def storage(self):
        from .storage import Storage
        return Storage(self.config, self.state)

    def fs_list(self, device, path=""):
        return self.storage().list(device or "", path or "")

    def fs_copy(self, body):
        result = self.storage().copy(body.get("src_device"), body.get("src_path") or "",
                                     body.get("dst_device"), body.get("dst_dir") or "")
        self.bus.publish("transfer.sent", {"name": result["as"], "from": result["from"],
                                           "to": result["to"]})
        return result

    def inbox(self, device=None):
        from . import transfer
        return transfer.list_inbox(self.config, self.state, device or None)

    def grokbot(self):
        from .grokbot import Grokbot
        return Grokbot(self.config, self.state)

    def confirmations(self):
        from .confirm import ConfirmQueue
        from .assistant import confirmation_origin
        return ConfirmQueue(self.state).listing(confirmation_origin(self.config, self.state))

    def confirmation(self, body):
        from .assistant import confirm_pending, confirmation_origin
        from .confirm import ConfirmQueue
        if body.get("action") == "revoke":
            site, verb = body.get("site"), body.get("verb")
            if not isinstance(site, str) or not isinstance(verb, str) or not site or not verb:
                raise ValueError("Choose the site and the kind of action to stop allowing.")
            removed = ConfirmQueue(self.state).revoke(site, verb)
            self.bus.publish("turn.confirm", {"handled": None})
            return {"reply": f"No longer allowing {verb} on {site}.", "revoked": bool(removed)}
        if not isinstance(body.get("id"), str):
            raise ValueError("Choose a confirmation ID.")
        if body.get("action") == "grant":
            if not isinstance(body.get("hash"), str) or not body["hash"]:
                raise ValueError("Allowing a site needs the hash of the question you saw.")
            command, info = ConfirmQueue(self.state).grant_item(
                body["id"], confirmation_origin(self.config, self.state), hash=body.get("hash"))
            self.bus.publish("turn.confirm", {"handled": body["id"]})
            return {"reply": f"Okay, allowing {info['verb']} on {info['site']} for 10 minutes.",
                    "commands": [command], "outcomes": [],
                    "grant": {"site": info["site"], "verb": info["verb"]}}
        result = confirm_pending(self.config, self.state, body["id"], body.get("action"),
                                 hash=body.get("hash"))
        self.bus.publish("turn.confirm", {"handled": body["id"]})
        return result

    def agent_screen(self):
        """The agent browser's page as a JPEG data URL, for the console's live box.
        {"open": False} while it's closed; looking never launches it."""
        import base64
        from .agent_browser import frame
        shot = frame(self.state)
        image = shot.pop("image", None)
        if image:
            shot["image"] = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        return shot

    def agent_screen_command(self, body):
        """The live box's buttons: back, forward, reload, stop (scrolling) or close."""
        from .agent_browser import browse
        action = body.get("action")
        if action not in ("back", "forward", "reload", "stop", "close"):
            raise ValueError("Choose back, forward, reload, stop or close.")
        spec = {"action": "scroll", "mode": "stop"} if action == "stop" else {"action": action}
        ok, text = browse(spec, self.state)
        return {"ok": ok, "text": text}

    def connections(self):
        """Connections view: node links and this computer's peripherals, live."""
        from .connections import snapshot
        return snapshot(self.config, self.state)

    def connection_action(self, body):
        from .connections import act
        result = act(self.config, self.state, body)
        self.bus.publish("connections.changed", {k: v for k, v in body.items()
                                                 if k in ("target", "part", "action")})
        return result

    # -- Fleet: guided node onboarding (fleet_management.py). Owner console only; the hub
    # default-denies /api/fleet* to node tokens and the phone server has no write route.
    def fleet(self):
        from . import fleet_management
        return fleet_management.inventory(self.config, self.state)

    def fleet_vault(self):
        from . import fleet_management
        return fleet_management.vault_status(self.config, self.state)

    def fleet_preview(self, body):
        from . import fleet_management
        return fleet_management.preview_setup(self.config, self.state, body)

    def fleet_main_preview(self, body):
        from . import fleet_management
        return fleet_management.main_transfer(self.config, self.state, body)

    def fleet_main_transfer(self, body):
        from . import fleet_management
        return fleet_management.main_transfer_refuse(self.config, self.state, body)

    def fleet_action(self, kind, body):
        """invite | revoke | capabilities. The reply to an invite holds the secret code: the
        handler sends it no-store, and only the node and action (never the code) reach the bus."""
        from . import fleet_management as fm
        if kind == "invite":
            result = fm.issue_invite(self.config, self.base / "jarvis.config.json", self.state, body)
            if result.get("config_changed") and isinstance(self.config.get("devices"), dict):
                self.config["devices"][result["node"]] = dict(result["device"])
        elif kind == "revoke":
            result = fm.revoke_node(self.config, self.state, body, config_path=self.base / "jarvis.config.json")
        else:
            result = fm.set_node_capabilities(self.config, self.state, body, config_path=self.base / "jarvis.config.json")
        self.bus.publish("fleet.changed", {"action": kind, "node": result.get("node")})
        return result

    def shared_input(self):
        """Both computers' screens and Lan Mouse configs, cross-checked (read-only)."""
        from .sharedinput import overview
        return overview(self.config, self.state)

    def command_history(self, limit=12):
        """Recent fast-path requests with their commands and hops, for the tree's replay."""
        from .history import History
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("limit must be a number.")
        return {"requests": History(self.state).requests(limit)}

    def screen(self, device):
        """Devices view: a snapshot of a device's screen, as (bytes, mime)."""
        import base64
        from .remote import Remote
        ok, text = Remote(self.config, self.state).call(device or None, "screen", "")
        if not ok:
            raise RuntimeError(text or "No picture came back.")
        shot = json.loads(text)
        return base64.b64decode(shot["b64"], validate=True), shot["mime"]

    # -- Remote view: David drives another computer (remote_view.py) ---------------------
    def _hub(self):
        from .cluster import _HUBS
        hub = _HUBS.get(str(Path(self.state).resolve()))
        if hub is None:
            raise LookupError("Remote control runs on the main computer, with JARVIS's cluster hub up.")
        return hub

    def remote_status(self):
        """Computers David can take over, whether each is ready now, and the session."""
        from .cluster import FRESH
        from .deploy import code_fingerprint
        from .release import served_code
        from .topology import enabled, identity
        live = code_fingerprint()
        served = served_code(self.state)
        release = {"live": live, "served": served}
        if not enabled(self.config):
            return {"devices": [], "session": None, "why": "The cluster is off, so there are no other computers to reach.", "release": release}
        local, main = identity(self.config)
        if local != main:
            return {"devices": [], "session": None, "why": "Open the Remote view on the main computer.", "release": release}
        try:
            hub = self._hub()
        except LookupError as exc:
            return {"devices": [], "session": None, "why": str(exc), "release": release}
        nodes = {n["id"]: n for n in hub.broker.nodes()}
        from .connections import presence as node_presence, busy_nodes
        busy = busy_nodes(self.state)
        from .release import current, release_dir
        # Only a release without remote control blocks it; one merely older than the live
        # tree (cut again automatically once edits settle) still works. Before the first
        # release, nodes get the live tree itself.
        shipped = not current(self.state) or \
            (release_dir(self.state) / "jarvis" / "remote_view.py").is_file()
        failed_path = Path(self.state) / "code-release-failed.json"
        try:
            failed = json.loads(failed_path.read_text(encoding="utf-8"))
            if not isinstance(failed, dict):
                failed = {}
        except (OSError, ValueError):
            failed = {}
        out = []
        for name, device in self.config.get("devices", {}).items():
            if name == main:
                continue
            node = nodes.get(name)
            presence = node_presence((node or {}).get("seen"), name in busy)
            online = presence == "online"
            why = None
            if node is None:
                why = "Not paired with this computer. Open Connections → Connect a computer or server."
            elif "desktop" not in node.get("capabilities", []):
                why = "Not enrolled for desktop actions."
            elif presence == "busy":
                why = "Busy, awaiting contact. Wait for its current job to finish."
            elif not online:
                why = "Offline: start JARVIS on it and check its link (jarvis cluster doctor)."
            elif node.get("code") != served:
                why = f"Runs older JARVIS code. Run: jarvis cluster update {name}"
            elif not shipped:  # the release nodes get predates remote control
                if failed.get("code") == live:
                    why = "This computer's latest code failed its release tests, so nodes can't get it yet. Fix the failing test, then run: jarvis cluster release"
                    tests = str(failed.get("tests") or "").strip()
                    if tests:
                        why += f" ({tests[-160:]})"
                else:
                    why = "Nodes don't have this computer's latest code yet. Run: jarvis cluster release, then jarvis cluster update " + name
            out.append({"id": name, "label": device.get("label") or name, "online": online,
                        "presence": presence, "ready": why is None, "why": why, "platform": (node or {}).get("platform")})
        return {"devices": out, "session": hub.remote.current(), "why": None,
                "release": release}

    def remote_start(self, body):
        device, quality = body.get("device"), body.get("quality") or "medium"
        if not isinstance(device, str) or not isinstance(quality, str):
            raise ValueError("Pick a computer to control.")
        hub = self._hub()
        try:
            return hub.remote_open(device, quality)
        except (OSError, RuntimeError) as exc:  # offline, busy, old code: say it plainly
            if "Nodes execute only" in str(exc):
                raise ValueError(f"That computer runs older JARVIS code that can't do remote control yet. Run: jarvis cluster update {device}") from None
            raise ValueError(str(exc)) from None

    def remote_frame(self, query):
        """(seq, jpeg, width, height) or None; raises remote_view.Ended."""
        try:
            after = int(query.get("after") or 0)
        except ValueError:
            raise ValueError("after must be a frame number.")
        return self._hub().remote.wait_frame(query.get("session"), after)

    def remote_input(self, body):
        return {"ok": True, "queued": self._hub().remote.push(body.get("session"), body.get("events"))}

    def remote_stop(self, body):
        hub = self._hub()
        session = hub.remote_close(body.get("session"))
        return {"ok": True, "session": session}

    def grokbot_command(self, body):
        """Devices view: a command for Grokbot, a terminal, or a browser."""
        result = self.grokbot().command(str(body.get("text", "")), body.get("mode") or None,
                                        body.get("device") or None)
        if "ask" in result:  # JARVIS's own browser runs through the brain's browser tools
            result["job"] = self.submit(result.pop("ask"))
        self.bus.publish("grokbot.command", {k: v for k, v in result.items() if k != "job"})
        return result

    def voice_setup(self):
        """The guided mic check + enrollment (onboard.py), one per console."""
        with self._lane_lock:
            if self._setup is None:
                from .onboard import VoiceSetup
                self._setup = VoiceSetup(self.state, self.config)
            return self._setup

    def voice_setup_action(self, body):
        setup, action = self.voice_setup(), body.get("action")
        if action == "start":
            return setup.start(body.get("device"), mic_only=bool(body.get("mic_only")))  # "" is the system default
        if action == "cancel":
            return setup.cancel()
        if action == "autostart":
            return setup.autostart(bool(body.get("on")))
        raise ValueError("action must be start, cancel or autostart.")

    def daemon(self, action):
        from . import daemon
        if action not in ("start", "stop", "status"):
            raise ValueError("action must be start, stop or status.")
        return getattr(daemon, action)(self.state)


def _plan_view(record, full=False):
    """A plan with each step's real state, read from the orchestrator's results."""
    results, tasks = record.get("results", {}), record.get("tasks", {})
    steps = []
    for sub in record.get("subtasks", []):
        sid = sub.get("id")
        result = results.get(sid, {})
        state = result.get("state") or ("running" if sid in tasks and record.get("state") ==
                                        "running" else "pending")
        step = {"id": sid, "specialist": sub.get("specialist"), "power": sub.get("power"),
                "objective": sub.get("objective", ""), "depends_on": sub.get("depends_on", []),
                "state": state, "task": tasks.get(sid)}
        if full:
            step["acceptance"] = sub.get("acceptance_criteria", [])
            step["answer"] = (result.get("answer") or "")[:4000]
        steps.append(step)
    return {"id": record.get("id"), "goal": record.get("goal", "")[:400],
            "summary": record.get("summary", "")[:600], "state": record.get("state", "planned"),
            "memory_id": record.get("memory_id"), "steps": steps}


def _hw_model(hw):
    computer = hw.get("computer") or {}
    return computer.get("Model") or hw.get("model")


def _hw_cpu(hw):
    cpu = hw.get("cpu") or {}
    if cpu.get("Name"):
        return cpu["Name"].replace("13th Gen Intel(R) Core(TM) ", "Intel ")
    for row in cpu.get("lscpu", []):
        if row.get("field") == "Model name:":
            return row.get("data")
    return None


def _hw_ram(hw):
    total = (hw.get("computer") or {}).get("TotalPhysicalMemory") or hw.get("memory_bytes")
    if isinstance(total, (int, float)):
        return f"{total / 2**30:.0f} GB"
    mem = hw.get("memory")
    if isinstance(mem, dict) and mem.get("MemTotal"):
        return str(mem["MemTotal"])
    if isinstance(mem, str):  # Linux sends /proc/meminfo verbatim: "MemTotal:  16222180 kB"
        found = re.search(r"^MemTotal:\s+(\d+)\s*kB", mem, re.M)
        if found:
            return f"{int(found.group(1)) * 1024 / 2**30:.0f} GB"
    return None


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # a stalled client can't pin a thread in a body read; /events lifts it
    console = None
    hosts = ()
    server_version = "JARVIS"

    def log_message(self, *_):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8", cache=False,
              disposition=None, close=False, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        if disposition:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", disposition)
            self.send_header("Content-Disposition", f'attachment; filename="jarvis-{safe}"')
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; connect-src 'self' https://api.open-meteo.com "
                             "wss://api.elevenlabs.io "
                             "https://geocoding-api.open-meteo.com; img-src 'self' data: blob:; "
                             "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        host = _host_name(self.headers.get("Host") or "")
        # Browsers label requests made by other sites; nothing here is for them.
        site = self.headers.get("Sec-Fetch-Site")
        return host in self.hosts and site in (None, "same-origin", "none")

    # Rejected-body protocol. A request we refuse still has its body in flight; closing the socket
    # over unread bytes makes the kernel send a reset, and the client's send fails (BrokenPipe /
    # ConnectionReset) before it can read our status. So after the error is sent we half-close
    # (FIN) and read-and-discard the rest, never keeping it, within hard bounds:
    #   bytes   REJECT_DRAIN_BYTES: MAX_AUDIO + 1 MiB, so any body up to a little over the largest
    #           accepted JSON/audio body is drained whole and its 413 is always delivered; an
    #           unauthenticated refusal drains at most REFUSE_DRAIN_BYTES (64 KiB).
    #   time    REJECT_IDLE_S per read and REJECT_DEADLINE_S in total, so a stalled or dribbling
    #           client holds a thread for at most REJECT_DEADLINE_S.
    #   memory  64 KiB per read, nothing retained.
    #   threads at most DRAINS (8) drains at once; past that the connection just closes.
    # A declared length beyond the byte bound is still refused up front (nothing is allocated for
    # it); its client may see a reset instead of the 413. That is the deliberate cost of the cap.
    REJECT_DRAIN_BYTES = MAX_AUDIO + (1 << 20)
    REFUSE_DRAIN_BYTES = 64 * 1024
    REJECT_IDLE_S = 2.0
    REJECT_DEADLINE_S = 8.0
    DRAINS = threading.BoundedSemaphore(8)

    def _content_length(self):
        """The request body length, 0 if none, or None if the framing is unusable: a
        Transfer-Encoding body (not supported), repeated disagreeing Content-Length headers, or
        anything but plain ASCII digits (int() also takes '+5', '1_0', unicode digits)."""
        if self.headers.get("Transfer-Encoding") is not None:
            return None
        values = {v.strip() for v in self.headers.get_all("Content-Length") or []}
        if not values:
            return 0
        raw = values.pop()
        if values or not (raw.isascii() and raw.isdigit()):
            return None
        return int(raw) if len(raw) <= 18 else 10 ** 18  # absurd, but never an int() on a huge string

    def _refuse(self):
        """403 a POST. A small unread body is drained first: closing a socket with unread
        data makes Windows reset it, and the client sees an abort instead of the 403."""
        length = self._content_length()
        self.close_connection = True
        try:
            self._send(HTTPStatus.FORBIDDEN, {"error": "Refused."}, close=True)
        except OSError:  # the client already left; nothing to tell it
            return
        if length:
            self._drain(length, self.REFUSE_DRAIN_BYTES)

    def _drain(self, left, cap=None):
        """Half-close, then read and discard an unread request body within the bounds above, so
        the client receives the error we already sent instead of a reset. Never returns data."""
        self.close_connection = True
        if left <= 0 or not self.DRAINS.acquire(blocking=False):
            return
        try:
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            left = min(left, self.REJECT_DRAIN_BYTES if cap is None else cap)
            end = time.monotonic() + self.REJECT_DEADLINE_S
            while left > 0:
                wait = end - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(min(self.REJECT_IDLE_S, wait))
                block = self.rfile.read1(min(1 << 16, left))
                if not block:
                    break
                left -= len(block)
        except OSError:  # includes the idle timeout
            pass
        finally:
            self.DRAINS.release()

    def _reject_body(self, status, message, length):
        self.close_connection = True
        try:
            self._send(status, {"error": message}, close=True)
        except OSError:
            return
        self._drain(length)

    EVENT_STREAMS = threading.BoundedSemaphore(16)  # each stream pins a thread

    def _events(self, query):
        """Server-Sent Events: what a reconnecting tab missed (Last-Event-ID), then live."""
        if not self.EVENT_STREAMS.acquire(blocking=False):
            self.close_connection = True
            return self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Too many open event streams."}, close=True)
        try:
            self._stream_events(query)
        finally:
            self.EVENT_STREAMS.release()

    def _stream_events(self, query):
        self.connection.settimeout(None)  # the stream idles between keep-alives
        after = self.headers.get("Last-Event-ID") or query.get("after") or ""
        sub, backlog = self.console.bus.subscribe(int(after) if re.fullmatch(r"\d+", after, re.ASCII) else None)

        def write(event, with_id=True):
            head = f"id: {event['seq']}\n" if with_id else ""
            self.wfile.write(f"{head}data: {json.dumps(event, default=str)}\n\n".encode())

        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            write({"seq": self.console.bus.seq, "kind": "hello", "at": time.time(),
                   "data": {"version": __version__, "brain": self.console.brain()}}, False)
            for event in backlog:
                write(event)
            self.wfile.flush()
            while True:
                try:
                    event = sub.get(timeout=15)
                    if event is None:  # queue overflow: reconnect and reload authoritative state
                        break
                    write(event)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError:  # the tab closed or reloaded
            pass
        finally:
            self.console.bus.unsubscribe(sub)

    def _fail(self, exc, close=False):
        payload = getattr(exc, "payload", None)  # fleet_management.FleetError: a stable code + step
        if isinstance(payload, dict) and isinstance(getattr(exc, "status", None), int):
            return self._send(exc.status, payload, close=close,
                              headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
        status = (HTTPStatus.NOT_FOUND if isinstance(exc, LookupError) else
                  HTTPStatus.BAD_REQUEST if isinstance(exc, ValueError) else
                  HTTPStatus.INTERNAL_SERVER_ERROR)
        if status == HTTPStatus.INTERNAL_SERVER_ERROR:
            import sys
            import traceback
            traceback.print_exception(exc, file=sys.stderr)
            if isinstance(exc, RuntimeError) and str(exc):  # written for David to read
                message = str(exc)
            elif isinstance(exc, OSError):  # no errno text or file names: they carry paths
                message = "Couldn't reach a file or device it needed \u2014 see the JARVIS log."
            else:
                message = "Something went wrong \u2014 see the JARVIS log."
        else:
            message = str(exc) or type(exc).__name__
        self._send(status, {"error": message}, close=close)

    def do_GET(self):
        if not self._allowed():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host header." if
                              self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")
                              else "Requests from other sites are refused."})
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        c = self.console
        routes = {
            "/api/confirm": lambda: c.confirmations(),
            "/api/overview": c.overview,
            "/api/credits": lambda: c.credits.all(refresh=query.get("refresh") == "1"),
            "/api/graph/system": c.system_graph,
            "/api/graph/heap": lambda: c.heap_graph(case=query.get("case") or None),
            "/api/tree": c.tree,
            "/api/specialists": c.specialists,
            "/api/memory": lambda: c.search(query),
            "/api/history": lambda: c.history(int(query.get("limit", 40))),
            "/api/cases": c.cases,
            "/api/memory/item": lambda: c.memory.get(query.get("id", "")),
            "/api/route": lambda: c.route(query.get("text", ""), query.get("tier") or None),
            "/api/voice": lambda: c.daemon("status"),
            "/api/voice/level": lambda: __import__("jarvis.daemon", fromlist=["level"]).level(c.state),
            "/api/voice/setup": lambda: c.voice_setup().status(),
            "/api/voice/devices": lambda: {"devices": c.voice_setup().devices()},
            "/api/settings": lambda: __import__("jarvis.settings", fromlist=["read"]).read(c.config, c.state),
            "/api/agent": lambda: c.agent().status(),
            "/api/agent/voices": lambda: c.agent().voices(refresh=query.get("refresh") == "1"),
            "/api/grokbot": lambda: c.grokbot().status(),
            "/api/agent-browser": c.agent_screen,
            "/api/history/commands": lambda: c.command_history(query.get("limit", 12)),
            "/api/input/shared": lambda: c.ttl.get("shared", 12, c.shared_input),
            "/api/connections": c.connections,
            "/api/fleet": c.fleet,
            "/api/fleet/vault": c.fleet_vault,
            "/api/peripherals": lambda: c.ttl.get("peripherals", 12, lambda: __import__(
                "jarvis.peripherals", fromlist=["overview"]).overview(
                    c.config, c.base / "jarvis.config.json", c.state)),
            "/api/probe": lambda: c.prober.get(refresh=query.get("refresh") == "1"),
            "/api/usage": lambda: c.credits.claude_hourly(int(query.get("hours", 24))),
            "/api/inbox": lambda: c.inbox(query.get("device")),
            "/api/fs": lambda: c.fs_list(query.get("device"), query.get("path")),
            "/api/fs/devices": lambda: c.storage().devices(),
            "/api/vault/tasks": lambda: c.vault_tasks(query.get("q", "")),
            "/api/vault/graph": lambda: c.tasks().vault.graph(),
            "/api/vault/brief": c.vault_brief,
            "/api/kb/state": lambda: c.kb().snapshot(),
            "/api/system/design": lambda: __import__("jarvis.sysview", fromlist=["design"]).design(c),
            "/api/system/components": lambda: __import__("jarvis.sysview", fromlist=["components"]).components(c),
            "/api/kb/graph": lambda: c.kb().graph(),
            "/api/kb/page": lambda: c.kb().page(query.get("slug", "")),
        }
        try:
            if url.path == "/api/events":
                return self._events(query)
            if url.path == "/api/fs/thumbnail":
                import base64
                result = c.storage().resolve(query.get("device"), query.get("path") or "", "thumbnail")
                return self._send(HTTPStatus.OK, base64.b64decode(result["image"], validate=True), "image/png")
            if url.path == "/api/screen":
                data, mime = c.screen(query.get("device"))
                return self._send(HTTPStatus.OK, data, mime)
            if url.path == "/api/remote":
                return self._send(HTTPStatus.OK, c.remote_status())
            if url.path == "/api/remote/frame":
                from .remote_view import Ended
                try:
                    frame = c.remote_frame(query)
                except Ended as exc:
                    return self._send(HTTPStatus.GONE, {"error": str(exc)})
                if frame is None:  # the screen hasn't changed; say how lately the node checked in
                    return self._send(HTTPStatus.NO_CONTENT, b"", "text/plain", headers={
                        "X-Node-Ago": f"{c._hub().remote.node_ago(query.get('session')):.1f}"})
                seq, jpeg, width, height = frame
                return self._send(HTTPStatus.OK, jpeg, "image/jpeg", headers={
                    "X-Frame-Seq": seq, "X-Frame-Width": width, "X-Frame-Height": height})
            if url.path in routes:
                return self._send(HTTPStatus.OK, routes[url.path]())
            if url.path == "/api/export":
                slug = query.get("case", "")
                return self._send(HTTPStatus.OK, c.export(slug).encode("utf-8"),
                                  "text/markdown; charset=utf-8", disposition=f"{slug}.md")
            if url.path.startswith("/api/plans/"):
                return self._send(HTTPStatus.OK, c.plan(url.path.rsplit("/", 1)[-1]))
            if url.path.startswith("/api/jobs/"):
                return self._send(HTTPStatus.OK, c.job(url.path.rsplit("/", 1)[-1]))
            if url.path.startswith("/api/tasks/"):
                task_id = url.path.rsplit("/", 1)[-1]
                return self._send(HTTPStatus.OK, {**c.store.get(task_id),
                                                  "events": c.store.events(task_id)})
            return self._static(url.path)
        except Exception as exc:
            self._fail(exc)

    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / name).resolve()
        if WEB not in target.parents or not target.is_file() or target.suffix not in TYPES:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        if target.suffix == ".woff2":
            return self._send(HTTPStatus.OK, target.read_bytes(), TYPES[target.suffix], cache=True)
        # Live on reload, but revalidated: an unchanged file answers 304 with no body.
        st = target.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        tags = [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]
        validators = {"ETag": etag, "Last-Modified": self.date_time_string(st.st_mtime)}
        if etag in tags or "*" in tags or any(t.startswith("W/") and t[2:] == etag for t in tags):
            return self._send(HTTPStatus.NOT_MODIFIED, b"", TYPES[target.suffix],
                              headers={**validators, "Cache-Control": "no-cache"})
        self._send(HTTPStatus.OK, target.read_bytes(), TYPES[target.suffix],
                   headers={**validators, "Cache-Control": "no-cache"})

    def do_POST(self):
        if not self._allowed() or self.headers.get("X-JARVIS") != "1":
            return self._refuse()
        url = urlparse(self.path)
        length = self._content_length()
        if length is None:  # the body can't be framed, so it can't be drained: answer and close
            self.close_connection = True
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "Bad Content-Length."}, close=True)
        c = self.console
        c.ttl.clear()  # any action may change peripherals or shared input; the next GET recomputes
        if url.path == "/api/kb/upload":  # streamed to the knowledge inbox
            self.connection.settimeout(60)  # a stalled client can't pin this thread
            stream = _Counted(self.rfile)
            try:
                return self._send(HTTPStatus.OK, c.kb_upload(
                    stream, length, parse_qs(url.query).get("name", [""])[-1]))
            except Exception as exc:
                self.close_connection = True
                self._fail(exc, close=True)
                return self._drain(length - stream.n)
        if url.path == "/api/send":  # streamed to disk, never held in memory
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            stream = _Counted(self.rfile)
            try:
                return self._send(HTTPStatus.OK, c.send_file(
                    stream, length, query.get("name", ""), query.get("device")))
            except Exception as exc:
                self.close_connection = True
                self._fail(exc, close=True)
                return self._drain(length - stream.n)
        if length > MAX_AUDIO:
            return self._reject_body(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too large.", length)
        raw = self.rfile.read(length) if length else b""
        try:
            if url.path == "/api/listen":
                query = parse_qs(url.query)
                verify = query.get("verify", ["1"])[-1] != "0"
                wake = query.get("wake", ["0"])[-1] == "1"
                return self._send(HTTPStatus.OK, c.transcribe(raw, verify, wake))
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            if url.path == "/api/confirm":
                return self._send(HTTPStatus.OK, c.confirmation(body))
            if url.path == "/api/ask":
                return self._send(HTTPStatus.OK, c.submit(body.get("text"), body.get("tier"),
                                                          bool(body.get("speak")),
                                                          body.get("case") or None))
            if url.path == "/api/voice":
                return self._send(HTTPStatus.OK, c.daemon(body.get("action")))
            if url.path == "/api/voice/setup":
                return self._send(HTTPStatus.OK, c.voice_setup_action(body))
            if url.path == "/api/connections":
                return self._send(HTTPStatus.OK, c.connection_action(body))
            if url.path == "/api/fleet/preview":
                return self._send(HTTPStatus.OK, c.fleet_preview(body))
            if url.path == "/api/fleet/main-transfer/preview":
                return self._send(HTTPStatus.OK, c.fleet_main_preview(body))
            if url.path == "/api/fleet/main-transfer":
                return self._send(HTTPStatus.OK, c.fleet_main_transfer(body))
            if url.path in ("/api/fleet/invite", "/api/fleet/revoke", "/api/fleet/capabilities"):
                # The invite reply carries a one-time secret: never cached, never stored by the browser.
                return self._send(HTTPStatus.OK, c.fleet_action(url.path.rsplit("/", 1)[-1], body),
                                  headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
            if url.path == "/api/settings":
                from .settings import write
                before = c.settings_snapshot()
                try:
                    return self._send(HTTPStatus.OK, write(c.config, c.base / "jarvis.config.json",
                                                           c.state, body.get("changes"),
                                                           confirmed=body.get("confirmed") is True))
                finally:
                    c.settings_applied(before)
            if url.path == "/api/agent/session":
                return self._send(HTTPStatus.OK, c.agent().session())
            if url.path == "/api/agent/voice":
                return self._send(HTTPStatus.OK, c.agent().set_voice(
                    body.get("voice_id"), body.get("voice"), body.get("speed"),
                    body.get("stability")))
            if url.path == "/api/agent/voice/preview":
                return self._send(HTTPStatus.OK, c.voice_preview())
            if url.path == "/api/agent/ask":
                return self._send(HTTPStatus.OK, c.agent_ask(body.get("text")))
            if url.path == "/api/agent-browser":
                return self._send(HTTPStatus.OK, c.agent_screen_command(body))
            if url.path == "/api/grokbot":
                return self._send(HTTPStatus.OK, c.grokbot_command(body))
            if url.path in ("/api/remote/start", "/api/remote/input", "/api/remote/stop"):
                from .remote_view import Ended
                try:
                    return self._send(HTTPStatus.OK, {"/api/remote/start": c.remote_start,
                                                      "/api/remote/input": c.remote_input,
                                                      "/api/remote/stop": c.remote_stop}[url.path](body))
                except Ended as exc:
                    return self._send(HTTPStatus.GONE, {"error": str(exc)})
            if url.path == "/api/peripherals":
                from .peripherals import act
                try:
                    result = act(c.config, c.base / "jarvis.config.json", c.state, body)
                finally:
                    c.ttl.clear()  # again after: a GET during the action may have cached the old state
                return self._send(HTTPStatus.OK, result)
            if url.path == "/api/fs/copy":
                return self._send(HTTPStatus.OK, c.fs_copy(body))
            if url.path == "/api/overlay":
                overlay = getattr(c, "overlay", None)
                if overlay is None:
                    raise LookupError("This console isn't running as the overlay.")
                return self._send(HTTPStatus.OK, overlay.request(body.get("action") or "hide"))
            if url.path == "/api/credits/manual":
                return self._send(HTTPStatus.OK, c.credits.set_manual(
                    body.get("provider"), body.get("remaining"), body.get("total"),
                    body.get("unit") or "USD", body.get("note") or ""))
            if url.path == "/api/kb/resolve":
                return self._send(HTTPStatus.OK, c.kb().resolve(
                    str(body.get("claim", "")), str(body.get("decision", "")),
                    str(body.get("note", "")), body.get("against") or None))
            if url.path == "/api/kb/command":
                return self._send(HTTPStatus.OK, c.kb_command(body.get("text")))
            if url.path == "/api/memory":
                return self._send(HTTPStatus.OK, c.capture(body))
            if url.path == "/api/memory/link":
                c.memory.link(body.get("src", ""), body.get("dst", ""),
                              body.get("rel") or "related")
                return self._send(HTTPStatus.OK, c.memory.get(body.get("src", "")))
            if url.path == "/api/cases":
                return self._send(HTTPStatus.OK, c.create_case(body.get("name")))
            if url.path.startswith("/api/plans/") and url.path.endswith("/run"):
                return self._send(HTTPStatus.OK, c.run_plan(url.path.split("/")[3]))
            if url.path == "/api/vault/tasks":
                return self._send(HTTPStatus.OK, c.add_task(body))
            if url.path.startswith("/api/vault/tasks/"):
                return self._send(HTTPStatus.OK, c.update_task(url.path.rsplit("/", 1)[-1], body))
            self._send(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except Exception as exc:
            self._fail(exc)


APP_BROWSERS = (  # Chromium browsers with --app: a window with no tabs or address bar
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    "msedge", "google-chrome", "chromium", "brave", "chrome",
    # Arch package names
    "google-chrome-stable", "chromium-browser", "brave-browser", "microsoft-edge-stable",
    "vivaldi-stable")


def app_window(url, size=(1480, 940), profile=None):
    """Open `url` as a standalone app window; a plain browser tab if no browser can.
    `profile` gives the window its own browser profile: sharing the everyday one let a
    keyring prompt, crash-restore or stuck instance swallow the window on a cold start."""
    for name in APP_BROWSERS:
        path = os.path.expandvars(name) if "\\" in name else shutil.which(name)
        if path and Path(path).is_file():
            extra = [f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                     "--password-store=basic"] if profile else []
            subprocess.Popen([path, f"--app={url}", f"--window-size={size[0]},{size[1]}", *extra],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             start_new_session=not on_windows())  # outlive an SSH caller
            return path
    webbrowser.open(url)
    return None


def listening(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), timeout=0.5).close()
        return True
    except OSError:
        return False


def ensure_window(url, bus, wait=20):
    """If no app window has connected after `wait` seconds, open a normal browser tab
    rather than leave a server running with nothing on screen."""
    time.sleep(wait)
    if not bus.subs:
        print("No app window connected; opening the console in the default browser.", flush=True)
        webbrowser.open(url)


def close_with_window(server, bus, grace=12, tick=1.0):
    """App mode: once a window has connected, stop the server when the last one leaves.
    A reload reconnects within ~2 s, well inside the grace period."""
    seen, empty_since = False, None
    while True:
        time.sleep(tick)
        if bus.subs:
            seen, empty_since = True, None
        elif seen:
            empty_since = empty_since or time.time()
            if time.time() - empty_since >= grace:
                server.shutdown()
                return


class ConsoleServer(ThreadingHTTPServer):
    # A page load opens ~15 connections at once; Python's default backlog of 5 makes
    # Windows refuse the overflow, and a refused script leaves the page half-booted.
    request_queue_size = 64
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A page that reloads or navigates away mid-poll closes its socket before the
        reply is written; that is routine, not worth a traceback in the console log."""
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError,
                                           ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def focus_app_window():
    """Bring an open console app window forward (its title is "JARVIS · <view>", with no
    " — Browser" suffix, unlike a tab). True if one was found and focused."""
    try:
        from .actions import open_windows, raise_window
        found = [w for w in open_windows() if (w["title"].startswith("JARVIS · ")
                 or w["title"] == "JARVIS Console") and " — " not in w["title"]]
        return len(found) == 1 and raise_window(found[0])
    except (LookupError, OSError, ValueError, subprocess.SubprocessError):
        return False


def serve(config, base, state, port=7777, open_browser=False, app=False):
    url = f"http://127.0.0.1:{port}/"
    profile = Path(state).resolve() / "app-window"
    if app and listening(port):
        # A hotkey (Hyprland's SUPER+SHIFT+J) runs this each press: raise the open window
        # rather than stack up copies.
        if focus_app_window():
            print("JARVIS console window brought forward.", flush=True)
            return 0
        print(f"JARVIS console is already running on {url}; opening its window.", flush=True)
        app_window(url, profile=profile)
        return 0
    from .topology import enabled
    runtime = None
    if enabled(config):
        from .cluster_proxy import local_server
        server, runtime = local_server(config, base, state, port)
        console = runtime.console
    else:
        console = Console(config, base, state).start_live()
        from .grokbot import autostart_tunnel
        autostart_tunnel(config, state)
        handler = type("BoundHandler", (Handler,), {
            "console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
        server = ConsoleServer(("127.0.0.1", port), handler)
    print(f"JARVIS console on {url}  (Ctrl+C to stop)", flush=True)
    if app:
        app_window(url, profile=profile)
        if not enabled(config):
            threading.Thread(target=close_with_window, args=(server, console.bus),
                             daemon=True).start()
            threading.Thread(target=ensure_window, args=(url, console.bus), daemon=True).start()
    elif open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runtime:
            runtime.close()
        else:
            console.close()
        server.server_close()
    return 0
