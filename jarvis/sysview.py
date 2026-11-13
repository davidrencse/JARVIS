"""The system design page's data: how JARVIS works, explained, and whether it works now.

Three parts, all read-only:
- stations: the path a spoken request takes (hear, decide, remember, think, act, speak,
  log), each in plain words, with the real modules behind it from the import graph;
- checks: every thing that has to be true for that path to work, with the command
  that fixes it when it isn't;
- stores: where JARVIS keeps what it knows, and how much is there.
"""

import ast
import importlib.util
import json
import os
import shutil
import sys
import threading
from pathlib import Path

from .platforms import on_windows

STATIONS = [
    {"id": "hear", "title": "Hear", "verb": "listens",
     "plain": "The listener records your voice through PipeWire, checks it is you against "
              "your voiceprint, and turns the speech into text with Parakeet (Whisper as backup) on this laptop.",
     "modules": ["listen", "fragments", "onboard", "daemon", "overlay", "voice_eval"]},
    {"id": "decide", "title": "Decide", "verb": "routes",
     "plain": "Simple rules, no AI, pick the cheapest way to answer: the clock or your vault "
              "instantly, a quick chat, a web task, or a multi-step plan.",
     "modules": ["router", "assistant", "instant", "briefing", "parse", "registry", "gates", "pipeline", "confirm", "decide", "interpret", "selfmodel", "scorecard"]},
    {"id": "remember", "title": "Remember", "verb": "recalls",
     "plain": "Before answering it gathers what it already knows: your Obsidian notes, wiki "
              "claims with their sources and dates, and past conversations.",
     "modules": ["vault", "knowledge", "memory", "second_brain", "second_brain_queue", "second_brain_runtime", "brainsync", "consolidate", "vault_mcp", "tasks", "history", "embed", "stemming", "situation"]},
    {"id": "think", "title": "Think", "verb": "answers",
     "plain": "The brain (ChatGPT through Codex, or Claude) writes the answer. Big jobs go to "
              "a planner that hands each step to a specialist agent.",
     "modules": ["chatgpt", "fastlane", "inference", "brain", "tools_mcp", "runner", "orchestrator", "prompts", "adapters",
                 "claude_auth", "grok_preflight", "usage"]},
    {"id": "act", "title": "Act", "verb": "does",
     "plain": "When you ask it to do something it opens apps and links, or drives its own "
              "Chrome. It asks before buying, posting or sending anything.",
     "modules": ["actions", "sysintents", "control", "app_ui", "terminal", "browser_gate", "browser_proxy", "agent_browser", "remote", "remote_view", "camera", "grokbot", "transfer", "storage", "thumbnails", "clipsync", "dropzone", "showcase", "observe", "sharedinput", "peripherals", "connections", "fleet_management", "screen"]},
    {"id": "speak", "title": "Speak", "verb": "replies",
     "plain": "The reply is spoken by ElevenLabs, falling back to Kokoro, a voice that "
              "runs on this laptop. ElevenLabs' agent also runs live calls from the console.",
     "modules": ["speech", "voice", "agent"]},
    {"id": "log", "title": "Log", "verb": "records",
     "plain": "Everything is written down: the conversation in memory, agent runs in the "
              "task ledger, and live events to this console.",
     "modules": ["store", "events", "locking", "config_lock", "cluster_store", "fsio", "pathsafe"]},
]
SIDE = [
    {"id": "surfaces", "title": "Surfaces",
     "plain": "Ways in: this console, the jarvis command, the Windows overlay, the phone app.",
     "modules": ["ui", "__main__", "phone", "sysview", "cluster_cli", "cluster_setup", "cluster_proxy", "cluster_tunnel", "cluster_recovery", "cluster_discovery", "linkfinder", "deploy", "release", "node", "platforms", "autostart", "settings", "rest"]},
    {"id": "machines", "title": "Machines",
     "plain": "Knows both laptops: hardware, the shared mouse and keyboard, and how much "
              "of each AI plan is left.",
     "modules": ["devices", "lanmouse_peer_ips", "inventory", "hostmeter", "probe", "desktop", "selftest", "credits",
                 "cluster", "topology", "locate",
                 "fleet", "fleet.legacy_topology", "fleet.readiness",
                 "continuity", "continuity.authority", "continuity.brain_channel", "continuity.cli",
                 "continuity.clock", "continuity.console", "continuity.executor", "continuity.gateway",
                 "continuity.hub_control", "continuity.ingress", "continuity.integration",
                 "continuity.lifecycle", "continuity.participant", "continuity.recovery",
                 "continuity.renewal", "continuity.repoint", "continuity.return_brain",
                 "continuity.runtime"]},
]
# Which station a request's tier lights up, in order.
PATHS = {
    "instant": ["hear", "decide", "remember", "speak", "log"],
    "simple": ["hear", "decide", "remember", "think", "speak", "log"],
    "medium": ["hear", "decide", "remember", "think", "act", "speak", "log"],
    "complex": ["hear", "decide", "remember", "think", "act", "log"],
}


def _module_name(path, package):
    """Import path relative to the jarvis package. None for the top-level ``__init__``."""
    parts = Path(path).relative_to(package).with_suffix("").parts
    if parts == ("__init__",):
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_targets(tree, package_parts):
    """Jarvis-relative names this file imports, including imports inside functions."""
    found = []
    if tree is None:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jarvis" or alias.name.startswith("jarvis."):
                    tail = alias.name.split(".", 1)[1] if "." in alias.name else ""
                    if tail:
                        found.append(tail)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package_parts) - (node.level - 1)
            if keep < 0:
                continue
            base = package_parts[:keep]
            if node.module:
                found.append(".".join((*base, node.module)))
            else:
                found.extend(".".join((*base, alias.name)) for alias in node.names)
            continue
        module = node.module or ""
        if module == "jarvis":
            found.extend(alias.name for alias in node.names)
        elif module.startswith("jarvis."):
            tail = module.split(".", 1)[1]
            found.append(tail)
            found.extend(f"{tail}.{alias.name}" for alias in node.names)
    return found


# str(path) -> ((mtime_ns, size), node, import targets). The tree is not kept.
_NESTED = {}
_NESTED_LOCK = threading.Lock()


def _read_nested(path, package_parts):
    text = path.read_text(encoding="utf-8")
    doc, defs, tree = "", [], None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        lines = (ast.get_docstring(tree) or "").strip().splitlines()
        doc = lines[0] if lines else ""
        defs = [n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")][:12]
    node = {"loc": text.count("\n") + 1, "doc": doc, "defs": defs}
    return node, _import_targets(tree, package_parts)


def _publish_nested(node, targets):
    """design() keeps the node and puts defs on the response, so each call gets its own lists."""
    return {"loc": node["loc"], "doc": node["doc"], "defs": list(node["defs"])}, list(targets)


def _forget_nested(package, live):
    """Drop cache entries under package that this scan did not see (deleted files)."""
    prefix = f"{package}{os.sep}"
    with _NESTED_LOCK:
        for key in [k for k in _NESTED if k.startswith(prefix) and k not in live]:
            del _NESTED[key]


def _nested_modules(package):
    """Subpackage modules. ``system_graph`` globs only ``jarvis/*.py``, so continuity/ and fleet/ never appear there.

    A package ``__init__.py`` is named after the package (``continuity``), the same way a
    module is named after its file. A file is parsed again only when its mtime or size changes.
    """
    nodes, imports = {}, {}
    package = Path(package)
    if not package.is_dir():
        _forget_nested(package, set())
        return nodes, imports
    live = set()
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package)
        if len(rel.parts) == 1:
            continue
        name = _module_name(path, package)
        if not name:
            continue
        key = str(path)
        live.add(key)
        qual = tuple(name.split("."))
        parts = qual if rel.name == "__init__.py" else qual[:-1]
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        with _NESTED_LOCK:
            hit = _NESTED.get(key)
        if hit is not None and hit[0] == stamp:
            node, targets = hit[1], hit[2]
        else:
            node, targets = _read_nested(path, parts)
            with _NESTED_LOCK:
                _NESTED[key] = (stamp, node, targets)
        nodes[name], imports[name] = _publish_nested(node, targets)
    _forget_nested(package, live)
    return nodes, imports


def design(console):
    graph = console.system_graph()
    mods = {n["id"][4:]: n for n in graph["nodes"] if n["kind"] == "module"}
    nested, nested_imports = _nested_modules(Path(console.base) / "jarvis")
    for name, node in nested.items():
        mods.setdefault(name, node)
    home = {m: s["id"] for s in STATIONS + SIDE for m in s["modules"]}
    imports, used_by = {}, {}
    for e in graph["edges"]:
        if e["rel"] == "imports":
            a, b = e["src"][4:], e["dst"][4:]
            imports.setdefault(a, []).append(b)
            used_by.setdefault(b, []).append(a)
    known = set(mods)
    for src, targets in nested_imports.items():
        if src not in known:
            continue
        for dst in targets:
            if dst not in known or dst == src:
                continue
            bucket = imports.setdefault(src, [])
            if dst not in bucket:
                bucket.append(dst)
            back = used_by.setdefault(dst, [])
            if src not in back:
                back.append(src)
    modules = [{"name": name, "station": home.get(name, "other"), "lines": n.get("loc", 0),
                "doc": n.get("doc", ""), "public": n.get("defs", []),
                "imports": sorted(imports.get(name, [])), "used_by": sorted(used_by.get(name, []))}
               for name, n in sorted(mods.items())]
    flows = {}
    for m in modules:
        for target in m["imports"]:
            pair = (m["station"], home.get(target, "other"))
            if pair[0] != pair[1]:
                flows[pair] = flows.get(pair, 0) + 1
    stations = []
    for s in STATIONS + SIDE:
        owned = [m for m in modules if m["station"] == s["id"]]
        stations.append({**s, "side": s in SIDE, "lines": sum(m["lines"] for m in owned),
                         "modules": [m["name"] for m in owned]})
    return {"stations": stations, "modules": modules, "paths": PATHS,
            "flows": [{"src": a, "dst": b, "imports": n} for (a, b), n in sorted(flows.items())],
            "checks": checks(console), "stores": stores(console), "setup": setup(console)}


def _check(station, label, ok, detail, fix="", warn=False):
    return {"station": station, "label": label, "state": "ok" if ok else "warn" if warn else "fail",
            "detail": detail, "fix": "" if ok else fix}


def _has(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def config_check(base):
    """jarvis.config.json is read once at start: a bad edit since then breaks the next start."""
    path = Path(base) / "jarvis.config.json"
    fix = "fix the JSON by hand, or restore one of the jarvis.config.json.pre-* backups"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _check("decide", "Config file reads cleanly", False, f"{path.name} is missing", fix)
    except (OSError, ValueError) as e:
        return _check("decide", "Config file reads cleanly", False, f"won't parse: {e}"[:160], fix)
    if not isinstance(data, dict):
        return _check("decide", "Config file reads cleanly", False, "the top level isn't an object", fix)
    return _check("decide", "Config file reads cleanly", True, f"{len(data)} sections; the next start will read it")


def checks(console):
    from . import daemon
    state, config, out = console.state, console.config, []
    py = ("jarvis" if shutil.which("jarvis") else
          ".venv/bin/python -m jarvis" if ".venv" in Path(sys.executable).parts else "python -m jarvis")
    voice = daemon.status(state)
    voice_dir = state / "voice"
    deps = [m for m in ("numpy", "sounddevice", "faster_whisper", "sherpa_onnx", "kokoro_onnx")
            if not _has(m)]
    out.append(_check("hear", "Voice packages installed", not deps,
                      "numpy, sounddevice, Parakeet/Whisper, speaker ID and Kokoro are importable" if not deps
                      else "missing: " + ", ".join(deps),
                      "uv pip install --python .venv/bin/python -e '.[voice]'"))
    enrolled = (voice_dir / "voiceprint.json").is_file()
    out.append(_check("hear", "Your voiceprint is enrolled", enrolled,
                      "JARVIS knows your voice on this machine" if enrolled else
                      "without it every voice is rejected, yours included",
                      f"{py} voice enroll"))
    speaker = any(voice_dir.glob("*.onnx"))
    out.append(_check("hear", "Speaker-ID model present", speaker,
                      "voice check model is downloaded" if speaker else "needed to tell your voice apart",
                      "the download link is printed by: " + f"{py} voice enroll"))
    out.append(_check("hear", "Listener running", voice.get("listening"),
                      f"pid {voice.get('pid')}" if voice.get("listening") else "nobody is listening",
                      f"{py} voice start", warn=not enrolled))
    if not on_windows() and "sounddevice" not in deps:
        try:
            import sounddevice
            names = {d["name"] for d in sounddevice.query_devices() if d["max_input_channels"] > 0}
            routed = "pipewire" in names or "pulse" in names
            out.append(_check("hear", "Microphone reachable through PipeWire", routed,
                              "recording goes through PipeWire" if routed else
                              "only raw ALSA devices; the laptop mic may read silence",
                              "sudo pacman -S pipewire-alsa"))
        except Exception as exc:  # no audio server in this session
            out.append(_check("hear", "Microphone reachable", False, str(exc), "check PipeWire is running"))
    vault = console.vault()
    notes = len(vault.notes()) if vault else 0
    vault_off = (config.get("vault") or {}).get("enabled", True) is False
    if vault_off:
        out.append(_check("remember", "Obsidian vault off by choice", True, "vault.enabled is false"))
    else:
        out.append(_check("remember", "Obsidian vault found", vault is not None,
                          f"{notes} notes at {vault.root}" if vault else "vault.path doesn't point at a folder",
                          "set vault.path.linux in jarvis.config.json"))
    snap = console.kb().snapshot()
    stats = snap["stats"]
    out.append(_check("remember", "Wiki built from your sources", stats["sources"] > 0,
                      f"{stats['pages']} pages · {stats['claims']} claims from {stats['sources']} sources"
                      if stats["sources"] else "the wiki is empty",
                      f"{py} kb ingest FILE-OR-URL" if vault is None else f"{py} kb sync", warn=True))
    open_conflicts = len(snap.get("open_conflicts") or [])
    out.append(_check("remember", "No contradictions waiting on you", not open_conflicts,
                      "nothing to rule on" if not open_conflicts else f"{open_conflicts} to rule on",
                      f"{py} kb conflicts   (or the Wiki page)", warn=True))
    sched = snap["checker"]["schedule"]
    scheduled = bool(sched.get("installed"))
    out.append(_check("remember", "Nightly wiki check scheduled", scheduled,
                      f"runs at {sched['at']}" if scheduled else "only runs when you ask",
                      f"{py} kb schedule on", warn=True))
    brain = config.get("routing", {}).get("brain", "chatgpt")
    cli = "claude" if brain == "claude" else "codex"
    out.append(_check("think", f"{'Claude' if cli == 'claude' else 'ChatGPT (Codex)'} CLI installed",
                      shutil.which(cli) is not None,
                      f"{cli} at {shutil.which(cli)}" if shutil.which(cli) else f"{cli} not on PATH",
                      f"install the {cli} CLI and sign in"))
    other = "codex" if cli == "claude" else "claude"
    out.append(_check("think", f"{other} CLI for agent runs", shutil.which(other) is not None,
                      "available for tasks and plans" if shutil.which(other) else "plans using it will fail",
                      f"install the {other} CLI", warn=True))
    warm = console.brain().get("warm")
    out.append(_check("think", "Brain warmed up", warm, "first answer will be fast" if warm else
                      "starts on the first request (a few seconds slower once)", warn=True))
    chrome = next((b for b in ("google-chrome-stable", "google-chrome", "chrome") if shutil.which(b)), None) \
        or (on_windows() and "chrome")
    out.append(_check("act", "Browser for web tasks", bool(shutil.which("npx") and chrome),
                      "Playwright and agent-browser drive Chrome with their own profiles" if shutil.which("npx") and chrome
                      else "needs Node (npx) and Google Chrome", "install nodejs, npm and google-chrome"))
    kokoro = all((voice_dir / "kokoro" / f).is_file() for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"))
    out.append(_check("speak", "Kokoro voice files", kokoro,
                      "local voice ready" if kokoro else "replies fall back to espeak or text",
                      "see README: Running on Arch (Kokoro download)"))
    player = on_windows() or any(shutil.which(p) for p in ("pw-play", "paplay", "aplay"))
    out.append(_check("speak", "Speaker output", player, "audio player found" if player else
                      "no audio player", "sudo pacman -S pipewire"))
    out.append(config_check(console.base))
    usage = console.store.overview(limit=1)
    out.append(_check("log", "Task ledger readable", True, f"{sum(usage['states'].values()) if isinstance(usage.get('states'), dict) else 0} agent runs recorded"))
    return out


def stores(console):
    state = console.state

    def size(path):
        path = Path(path)
        try:
            if path.is_file():
                return path.stat().st_size
        except OSError:
            return 0
        if not path.is_dir():
            return 0
        total = 0
        for p in path.rglob("*"):
            try:  # a file deleted mid-walk is simply skipped
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return total

    mem = console.memory.stats()
    kb = console.kb().snapshot()["stats"]
    vault = console.vault()
    rows = [
        {"name": "Conversation memory", "path": str(state / "memory.sqlite3"),
         "holds": "everything you said and JARVIS replied, searchable",
         "count": f"{mem['items']} items · {mem['links']} links", "bytes": size(state / "memory.sqlite3")},
        {"name": "Wiki", "path": str(console.kb().root),
         "holds": "raw sources (read-only) and the pages built from them",
         "count": f"{kb['sources']} sources · {kb['pages']} pages · {kb['claims']} claims",
         "bytes": size(console.kb().root)},
        {"name": "Obsidian vault", "path": str(vault.root) if vault else "not found",
         "holds": "your own notes; JARVIS reads them and only ever appends",
         "count": f"{len(vault.notes())} notes" if vault else "—", "bytes": size(vault.root) if vault else 0},
        {"name": "Task ledger", "path": str(state / "tasks.sqlite3"),
         "holds": "every agent run and plan, with its events",
         "count": "", "bytes": size(state / "tasks.sqlite3")},
        {"name": "Voice", "path": str(state / "voice"),
         "holds": "voiceprint, speaker-ID model and the Kokoro voice",
         "count": "", "bytes": size(state / "voice")},
        {"name": "Event feed", "path": str(state / "events.jsonl"),
         "holds": "what the listener and agents are doing, for this console",
         "count": "", "bytes": size(state / "events.jsonl")},
    ]
    return rows


def setup(console):
    cfg = console.config
    voice = cfg.get("voice", {})
    models = cfg.get("models", {})
    return {"brain": console.brain()["name"], "chat_model": models.get("chatgpt", {}).get("model"),
            "claude": {k: v.get("model") for k, v in models.get("claude", {}).items()},
            "voice": voice.get("engine"), "kokoro_voice": voice.get("kokoro_voice"),
            "stt": voice.get("stt_model"), "stt_engine": voice.get("stt_engine", "parakeet"), "platform": sys.platform,
            "python": sys.version.split()[0], "state": str(console.state)}


# -- components: the live parts, one card each (ARGONA-style: title, 3-5 key -> value rows) --------
def _card(cid, title, station, state, rows, note=""):
    """state: ok (working), warn (needs a look), off (switched off on purpose), fail."""
    return {"id": cid, "title": title, "station": station, "state": state,
            "rows": [[str(k), str(v)] for k, v in rows], "note": note}


def _safe(build):
    try:
        return build()
    except Exception as exc:  # one broken card never hides the others
        return _card(getattr(build, "__name__", "card").lstrip("_"), getattr(build, "__name__", "?").strip("_"),
                     "other", "fail", [["error", type(exc).__name__]], str(exc)[:160])


def components(console):
    """Read-only, quick (no network, no models): what each part is doing right now."""
    import json as _json
    import shutil as _shutil
    config, state = console.config, Path(console.state)

    def last_eval():
        try:
            lines = (state / "eval" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            runs = [_json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError):
            return {}, {}
        latest = runs[-1].get("parts", {}) if runs else {}
        brain = next((r["parts"]["brain"] for r in reversed(runs) if "brain" in r.get("parts", {})), {})
        return latest, brain

    def _brain():
        from .inference import LEVELS
        from .brain import Budget, settings
        models = config.get("models") or {}
        name = lambda level: (models.get(LEVELS[level]["chatgpt"]) or LEVELS[level]["default"])
        orch = settings(config)
        left = Budget(state, orch["l3_per_5h"]).left()
        rows = [[f"L{n}", f"{name(n).get('model')} {name(n).get('effort')} / {(models.get('claude') or {}).get(LEVELS[n]['claude'], {}).get('model', LEVELS[n]['claude'])}"]
                for n in (1, 2, 3)]
        rows.append(["L3 left / 5h", f"{left} / {orch['l3_per_5h']}"])
        return _card("brain", "Brain levels", "think", "ok" if orch["enabled"] else "off", rows,
                     "Hard requests are planned at L3, steps run on L1-L3, checked at L1.")

    def _judge():
        latest, brain = last_eval()
        judge = latest.get("judge") or {}
        parser = latest.get("parser") or {}
        rows = [["judge", f"{judge.get('correct', '?')} / {judge.get('n', '?')}"],
                ["parser", f"{parser.get('correct', '?')} / {parser.get('n', '?')}"],
                ["brain sample", f"{brain.get('correct', '?')} / {brain.get('n', '?')}" if brain else "not run"]]
        ok = judge.get("score") == 1.0 and parser.get("wrong", 1) == 0
        return _card("scorecard", "Scorecard", "decide", "ok" if ok else "warn", rows, "jarvis eval (free); --brain uses quota.")

    def _control():
        from . import control
        try:
            backend = type(control.backend()).__name__.lower()
        except LookupError as exc:
            backend = f"none ({str(exc)[:40]})"
        stopped = control._stopped(state)
        recent = 0
        try:
            import time as _t
            for line in (state / "logs" / "control.jsonl").read_text(encoding="utf-8").splitlines()[-400:]:
                at = _json.loads(line).get("at", "")
                if at and _t.mktime(_t.strptime(at, "%Y-%m-%dT%H:%M:%S")) > _t.time() - 600:
                    recent += 1
        except (OSError, ValueError):
            pass
        return _card("control", "Desktop control", "act", "warn" if stopped else "ok",
                     [["backend", backend], ["state", "stopped" if stopped else "ready"],
                      ["actions / 10 min", f"{recent} / {control.MAX_ACTIONS}"], ["stop", "Super+Shift+Esc"]],
                     "Screenshots, mouse and keyboard. Pauses when you move the mouse.")

    def _chrome():
        from .actions import MY_CHROME_OFF_LIMITS
        actions = config.get("actions") or {}
        on = "chrome" in (actions.get("browser_engines") or [])
        token = (state / "chrome-extension-token").is_file()
        return _card("chrome", "Your Chrome", "act", ("ok" if token else "warn") if on else "off",
                     [["engine", "on" if on else "off"], ["extension token", "saved" if token else "missing"],
                      ["off-limits sites", len(MY_CHROME_OFF_LIMITS) + len(actions.get("my_chrome_off_limits") or [])],
                      ["fallback", "JARVIS profile"]], "Playwright MCP through the Bridge extension, with your sign-ins.")

    def _sandbox():
        from . import terminal
        args = terminal.sandbox("/tmp")
        hidden = args.count("--tmpfs") + sum(1 for i, a in enumerate(args) if a == "--ro-bind" and args[i + 1] == "/dev/null")
        readonly = sum(1 for i, a in enumerate(args) if a == "--ro-bind" and args[i + 1] != "/dev/null")
        if args:
            return _card("sandbox", "Terminal sandbox", "act", "ok",
                         [["engine", "bubblewrap"], ["hidden", f"{hidden} paths"], ["read-only", f"{readonly} paths"],
                          ["network", "internet, no localhost" if args[0].endswith("pasta")
                           else "none (install passt)"]],
                         "Secrets hidden; JARVIS's code, config and the vault read-only.")
        why = "switched off" if (config.get("terminal") or {}).get("sandbox") is False else \
            "no bwrap" if not _shutil.which("bwrap") else "not on this OS"
        return _card("sandbox", "Terminal sandbox", "act", "off" if why == "switched off" else "warn",
                     [["engine", "none"], ["why", why]], "Only the pattern refusals protect this computer.")

    def _gates():
        from .gates import CONFIRM_DEFAULT, asks_now
        rules = config.get("rules") or {}
        always = rules.get("always_ask", ["money", "messages"])
        return _card("gates", "Gates", "decide", "ok",
                     [["confirm", rules.get("confirm", CONFIRM_DEFAULT)], ["always ask", ", ".join(always) or "nothing"],
                      ["asks now", "yes" if asks_now(config, state) else "no (full auto)"],
                      ["hard refusals", "credentials, wipes, own files"]],
                     "What runs unasked, and what never runs.")

    def _cluster():
        from .release import served_code, current
        main = (config.get("cluster") or {}).get("main_device", "?")
        from .cluster import _HUBS
        hub = _HUBS.get(str(state))
        nodes = hub.broker.nodes() if hub is not None and getattr(hub, "broker", None) else []
        want = served_code(state)
        online = [n for n in nodes if n.get("online")]
        same = [n for n in online if n.get("code") == want]
        failed = {}
        try:
            failed = _json.loads((state / "code-release-failed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        rows = [["main", main], ["nodes online", f"{len(online)} / {len(nodes)}" if nodes else "hub not here"],
                ["same code", f"{len(same)} / {len(online)}" if online else "-"], ["release", (want or "?")[:12]]]
        bad = failed.get("code") and failed.get("code") != want and failed.get("at", 0) > (current(state).get("at") or 0)
        return _card("cluster", "Cluster", "machines", "warn" if bad else "ok", rows,
                     "Jobs to nodes are signed; nodes only get code that passed the tests.")

    def _lanmouse():
        try:
            memo = _json.loads((state / "lanmouse-guard.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            memo = {}
        lm = config.get("lan_mouse") or {}
        reading = "together" if memo.get("together") else "apart" if memo.get("apart") else "unknown"
        return _card("lanmouse", "Lan Mouse guard", "machines", "ok" if memo else "off",
                     [["laptops", reading], ["stopped by guard", "yes" if memo.get("stopped_by_guard") else "no"],
                      ["autostart", "on" if lm.get("autostart", True) else "off"],
                      ["off when apart", "on" if lm.get("off_when_apart", True) else "off"]],
                     "Sharing on when both PCs are together, off when apart.")

    def _memory():
        vault = config.get("vault") or {}
        size = lambda p: (state / p).stat().st_size if (state / p).is_file() else 0
        return _card("memory", "Memory & vault", "remember", "ok",
                     [["memory db", f"{size('memory.sqlite3') / 1e6:.1f} MB"],
                      ["vault", "on" if vault.get("enabled", True) and vault.get("path") else "off"],
                      ["vault writes", "on" if vault.get("write") is True else "off"],
                      ["recall", "BM25 + vectors"]], "Daily consolidation turns talk into facts, lessons and open loops.")

    def _spotify():
        players = _shutil.which("playerctl") is not None
        return _card("spotify", "Spotify", "act", "ok" if players else "warn",
                     [["play", "track / album / playlist / liked"], ["search", "open.spotify.com in your Chrome"],
                      ["driver", "MPRIS (playerctl)" if players else "playerctl missing"]],
                     "Plays exactly what was asked and reports what started.")

    return [_safe(b) for b in (_brain, _judge, _gates, _control, _chrome, _sandbox, _spotify,
                               _memory, _cluster, _lanmouse)]
