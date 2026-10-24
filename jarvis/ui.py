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
from .memory import Memory
from .probe import Prober
from .store import Store, lanes_from

WEB = Path(__file__).resolve().parent / "web"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
MAX_AUDIO = 16000 * 4 * 45  # 45 s of float32 mono at 16 kHz
# Tags the assistant puts on every exchange; as graph hubs they'd cluster unrelated items.
BOOKKEEPING_TAGS = {"inbound", "outbound", "simple", "medium", "complex", "plan", "result"}
SKIP = {".git", "__pycache__", ".jarvis", "jarvis.egg-info", "node_modules", ".pytest_cache"}

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


class Console:
    """Everything the HTTP layer exposes, kept separate so tests can call it directly."""

    def __init__(self, config, base, state):
        self.config, self.base, self.state = config, Path(base), Path(state).resolve()
        self.store = Store(self.state, lanes_from(config))
        self.memory = Memory(self.state)
        self.credits = Credits(self.state, config)
        self.prober = Prober(config)
        self.jobs, self._jobs_lock = {}, threading.Lock()
        self.max_jobs = 200  # the page only ever polls recent ones; older entries are dropped
        self._ears = self._stt = None
        self._voice_lock = threading.Lock()
        self.bus, self.watcher = Bus(), None
        self._lane = self._speaker = self._agent = self._markets = None
        self._lane_lock = threading.Lock()

    # ---- live: event stream, warm brain, speech ---------------------------------------
    def start_live(self, warm=True):
        """Start relaying shared-state changes, and warm the brain so the first ask is fast."""
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
        return self

    def close(self):
        if self.watcher:
            self.watcher.stop()
        if self._lane:
            self._lane.close()

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

    # ---- markets: True Markets recon + strategy lab (read-only) ----------------------
    def markets(self):
        with self._lane_lock:
            if self._markets is None:
                from .markets import TrueMarkets
                self._markets = TrueMarkets(self.state, self.config)
            return self._markets

    def strategies(self):
        from .strategy import Book
        return Book(self.state)

    def backtest(self, body):
        from .strategy import run
        spec = body.get("spec") or (self.strategies().get(body["id"]) if body.get("id") else None)
        if not spec:
            raise ValueError("Send a strategy (spec) or a saved strategy id.")
        return run(spec, self.markets())

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
        """The task board: agenda buckets plus what the page needs to link into Obsidian."""
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
        return {"brief": answer("brief me", tasks.vault, tasks), "world": tasks.vault.world()}

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
        return {
            "version": __version__, "now": time.time(),
            "devices": self.devices(),
            "desktop": self.config.get("desktop", {}),
            "agents": self.config.get("personal_agents", {}),
            "network": self.config.get("network", {}),
            "routing": self.config.get("routing", {}),
            "models": self.config.get("models", {}),
            "memory": {**self.memory.stats(), "recent": self.memory.search("", limit=8)},
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
                      "stt_model": self.config.get("voice", {}).get("stt_model")},
            "tools": {name: shutil.which(name) is not None
                      for name in ("claude", "codex", "ssh", "tailscale", "lan-mouse")},
        }

    def devices(self):
        out = []
        for name, device in self.config.get("devices", {}).items():
            inv = _read_json(self.state / "inventory" / f"{name}.json", {})
            hw = inv.get("hardware", {}) if isinstance(inv.get("hardware"), dict) else {}
            out.append({"id": name, "hostname": device.get("hostname"),
                        "transport": device.get("transport"), "status": device.get("status"),
                        "address": device.get("lan_ip") or device.get("target"),
                        "platform": inv.get("platform"), "collected_at": inv.get("collected_at"),
                        "model": _hw_model(hw), "cpu": _hw_cpu(hw), "ram": _hw_ram(hw),
                        "gpu": inv.get("nvidia") if isinstance(inv.get("nvidia"), str) else None,
                        "tools": inv.get("tools", {})})
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
        items = self.memory.search(query.get("q", ""), query.get("kind") or None,
                                   query.get("tag") or None, query.get("since") or None,
                                   200 if case else limit)
        if case:
            ids = self.case_ids(case)
            items = [i for i in items if i["id"] in ids]
        floor = float(query.get("min_importance") or 0)
        return [i for i in items if i["importance"] >= floor][:limit]

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
        for path in sorted(package.glob("*.py")):
            name = path.stem
            text = path.read_text(encoding="utf-8")
            sources[name] = text
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            doc = (ast.get_docstring(tree) or "").strip().splitlines()
            defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                    and not n.name.startswith("_")]
            nodes.append({"id": f"mod:{name}", "label": f"{name}.py", "kind": "module",
                          "loc": text.count("\n") + 1, "doc": doc[0] if doc else "",
                          "defs": defs[:12]})
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    targets = [node.module] if node.module else [a.name for a in node.names]
                    for target in targets:
                        if (package / f"{target}.py").is_file() and target != name:
                            edges.append({"src": f"mod:{name}", "dst": f"mod:{target}",
                                          "rel": "imports"})
        for ext_id, label, kind, pattern in EXTERNALS:
            # ui.py holds this pattern table itself, so it would match everything.
            users = [m for m, text in sources.items() if m != "ui" and re.search(pattern, text)]
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
        """Project layout with sizes and module summaries."""
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
                    info = {"name": child.name, "dir": False, "bytes": child.stat().st_size}
                    if child.suffix in (".py", ".md", ".js", ".css", ".html", ".json", ".toml"):
                        try:
                            info["lines"] = child.read_text(encoding="utf-8").count("\n") + 1
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
        route = decide(text or "", self.config, tier or None, conversational=True)
        if route["tier"] == "simple":
            route["handler"] = f"{self.brain()['name']} brain"
        elif route["tier"] == "medium":
            route["adapter"] = pick_adapter(text or "", self.config)
            route["handler"] = route["adapter"].get("name")
        else:
            route["handler"] = "specialist planner"
        return route

    def submit(self, text, tier=None, speak=False, case=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Say or type something for JARVIS to do.")
        if tier not in (None, "", "simple", "medium", "complex"):
            raise ValueError("tier must be simple, medium or complex.")
        job = {"id": uuid.uuid4().hex[:12], "text": text.strip(), "state": "running",
               "started": time.time(), "route": self.route(text.strip(), tier), "result": None,
               "error": None, "partial": "", "message": None}
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
            result = ask(job["text"], self.store, self.memory, self.config, self.base, tier,
                         lane=self.lane(), on_sentence=sentence, on_start=started,
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
    def transcribe(self, raw, verify=True):
        """Float32 mono 16 kHz from the browser → (owner check) → local Whisper text."""
        from .listen import RATE, Transcriber, VoicePrint, _np
        np = _np()
        if len(raw) < RATE * 4 * 0.3 or len(raw) % 4:
            raise ValueError("That clip was too short to hear anything.")
        audio = np.frombuffer(raw, dtype="<f4").astype("float32")
        with self._voice_lock:
            if self._stt is None:
                self._stt, self._ears = Transcriber(self.config), VoicePrint(self.state, self.config)
            owner, score = True, None
            if verify and (self.state / "voice" / "voiceprint.json").is_file():
                owner, score = self._ears.verify(audio)
            text = self._stt(audio) if owner else ""
        from .assistant import wake_word
        text = wake_word(text) or text  # push-to-talk needs no wake word; drop it if said
        return {"text": text, "owner": owner, "score": score,
                "device": getattr(self._stt, "device", None), "seconds": round(len(audio) / RATE, 2)}

    def send_file(self, stream, length, name, device=None):
        """Receive an uploaded file from the page and push it to the other laptop."""
        import tempfile
        from . import transfer
        if length <= 0:
            raise ValueError("That file is empty.")
        if length > transfer.MAX_BYTES:
            raise ValueError("That file is too large to send this way.")
        target = transfer.ssh_device(self.config, device or None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / transfer.safe_name(name)
            left = length
            with open(path, "wb") as out:
                while left:
                    block = stream.read(min(1 << 20, left))
                    if not block:
                        raise ValueError("The upload ended early.")
                    out.write(block)
                    left -= len(block)
            result = transfer.send(target.get("target"), target.get("identity_file"), path,
                                   path.name)
        self.bus.publish("transfer.sent", {"name": result["sent"], "bytes": result["bytes"]})
        return result

    def storage(self):
        from .storage import Storage
        return Storage(self.config)

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
        target = transfer.ssh_device(self.config, device or None)
        return transfer.inbox(target.get("target"), target.get("identity_file"))

    def grokbot(self):
        from .grokbot import Grokbot
        return Grokbot(self.config, self.state)

    def grokbot_command(self, body):
        """Devices view: a command for Grokbot, a terminal, or a browser."""
        result = self.grokbot().command(str(body.get("text", "")), body.get("mode") or None,
                                        body.get("device") or None)
        if "ask" in result:  # JARVIS's own browser runs through the brain's browser tools
            result["job"] = self.submit(result.pop("ask"))
        self.bus.publish("grokbot.command", {k: v for k, v in result.items() if k != "job"})
        return result

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


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


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
    console = None
    hosts = ()
    server_version = "JARVIS"

    def log_message(self, *_):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8", cache=False,
              disposition=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        if disposition:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", disposition)
            self.send_header("Content-Disposition", f'attachment; filename="jarvis-{safe}"')
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; connect-src 'self' https://api.open-meteo.com "
                             "wss://api.elevenlabs.io "
                             "https://geocoding-api.open-meteo.com; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        # Browsers label requests made by other sites; nothing here is for them.
        site = self.headers.get("Sec-Fetch-Site")
        return host in self.hosts and site in (None, "same-origin", "none")

    def _events(self, query):
        """Server-Sent Events: what a reconnecting tab missed (Last-Event-ID), then live."""
        after = self.headers.get("Last-Event-ID") or query.get("after") or ""
        sub, backlog = self.console.bus.subscribe(int(after) if after.isdigit() else None)

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
                    write(sub.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError:  # the tab closed or reloaded
            pass
        finally:
            self.console.bus.unsubscribe(sub)

    def _fail(self, exc):
        status = (HTTPStatus.NOT_FOUND if isinstance(exc, LookupError) else
                  HTTPStatus.BAD_REQUEST if isinstance(exc, ValueError) else
                  HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send(status, {"error": str(exc) or type(exc).__name__})

    def do_GET(self):
        if not self._allowed():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host header." if
                              self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")
                              else "Requests from other sites are refused."})
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        c = self.console
        routes = {
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
            "/api/agent": lambda: c.agent().status(),
            "/api/markets/status": lambda: c.markets().status(),
            "/api/markets/monitor": lambda: c.markets().monitor(min(int(query.get("limit", 40)), 100)),
            "/api/markets/intel": lambda: c.markets().intel(),
            "/api/markets/universe": lambda: c.markets().universe(),
            "/api/markets/quote": lambda: c.markets().quote(query.get("symbol", "").upper()),
            "/api/markets/history": lambda: c.markets().history(
                query.get("symbol", "").upper(), query.get("window", "1d"),
                query.get("resolution", "1h")),
            "/api/strategies": lambda: c.strategies().all(),
            "/api/strategies/templates": lambda: __import__(
                "jarvis.strategy", fromlist=["TEMPLATES"]).TEMPLATES,
            "/api/agent/voices": lambda: c.agent().voices(refresh=query.get("refresh") == "1"),
            "/api/grokbot": lambda: c.grokbot().status(),
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
            "/api/kb/graph": lambda: c.kb().graph(),
            "/api/kb/page": lambda: c.kb().page(query.get("slug", "")),
        }
        try:
            if url.path == "/api/events":
                return self._events(query)
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
        self._send(HTTPStatus.OK, target.read_bytes(), TYPES[target.suffix],
                   cache=target.suffix == ".woff2")  # the icon sprite changes with the UI

    def do_POST(self):
        if not self._allowed() or self.headers.get("X-JARVIS") != "1":
            return self._send(HTTPStatus.FORBIDDEN, {"error": "Refused."})
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "Bad Content-Length."})
        c = self.console
        if url.path == "/api/kb/upload":  # streamed to the knowledge inbox
            self.connection.settimeout(60)  # a stalled client can't pin this thread
            try:
                return self._send(HTTPStatus.OK, c.kb_upload(
                    self.rfile, length, parse_qs(url.query).get("name", [""])[-1]))
            except Exception as exc:
                return self._fail(exc)
        if url.path == "/api/send":  # streamed to disk, never held in memory
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                return self._send(HTTPStatus.OK, c.send_file(
                    self.rfile, length, query.get("name", ""), query.get("device")))
            except Exception as exc:
                return self._fail(exc)
        if length > MAX_AUDIO:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Too large."})
        raw = self.rfile.read(length) if length else b""
        try:
            if url.path == "/api/listen":
                verify = parse_qs(url.query).get("verify", ["1"])[-1] != "0"
                return self._send(HTTPStatus.OK, c.transcribe(raw, verify))
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            if url.path == "/api/ask":
                return self._send(HTTPStatus.OK, c.submit(body.get("text"), body.get("tier"),
                                                          bool(body.get("speak")),
                                                          body.get("case") or None))
            if url.path == "/api/voice":
                return self._send(HTTPStatus.OK, c.daemon(body.get("action")))
            if url.path == "/api/strategies":
                return self._send(HTTPStatus.OK, c.strategies().save(body))
            if url.path == "/api/strategies/delete":
                return self._send(HTTPStatus.OK, c.strategies().delete(str(body.get("id", ""))))
            if url.path == "/api/strategies/backtest":
                return self._send(HTTPStatus.OK, c.backtest(body))
            if url.path == "/api/agent/session":
                return self._send(HTTPStatus.OK, c.agent().session())
            if url.path == "/api/agent/voice":
                return self._send(HTTPStatus.OK, c.agent().set_voice(body.get("voice_id")))
            if url.path == "/api/agent/ask":
                return self._send(HTTPStatus.OK, c.agent_ask(body.get("text")))
            if url.path == "/api/grokbot":
                return self._send(HTTPStatus.OK, c.grokbot_command(body))
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


def app_window(url, size=(1480, 940)):
    """Open `url` as a standalone app window; a plain browser tab if no browser can."""
    for name in APP_BROWSERS:
        path = os.path.expandvars(name) if "\\" in name else shutil.which(name)
        if path and Path(path).is_file():
            subprocess.Popen([path, f"--app={url}", f"--window-size={size[0]},{size[1]}"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return path
    webbrowser.open(url)
    return None


def listening(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), timeout=0.5).close()
        return True
    except OSError:
        return False


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


def serve(config, base, state, port=7777, open_browser=False, app=False):
    url = f"http://127.0.0.1:{port}/"
    if app and listening(port):
        print(f"JARVIS console is already running on {url}; opening its window.", flush=True)
        app_window(url)
        return 0
    console = Console(config, base, state).start_live()
    handler = type("BoundHandler", (Handler,), {
        "console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
    server = ConsoleServer(("127.0.0.1", port), handler)
    print(f"JARVIS console on {url}  (Ctrl+C to stop)", flush=True)
    if app:
        app_window(url)
        threading.Thread(target=close_with_window, args=(server, console.bus),
                         daemon=True).start()
    elif open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.close()
        server.server_close()
    return 0
