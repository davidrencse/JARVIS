"""The system design page's data: how JARVIS works, explained, and whether it works now.

Three parts, all read-only:
- stations: the path a spoken request takes (hear, decide, remember, think, act, speak,
  log), each in plain words, with the real modules behind it from the import graph;
- checks: every thing that has to be true for that path to work, with the command
  that fixes it when it isn't;
- stores: where JARVIS keeps what it knows, and how much is there.
"""

import importlib.util
import os
import shutil
import sys
from pathlib import Path

STATIONS = [
    {"id": "hear", "title": "Hear", "verb": "listens",
     "plain": "The listener records your voice through PipeWire, checks it is you against "
              "your voiceprint, and turns the speech into text with Whisper on this laptop.",
     "modules": ["listen", "daemon", "overlay"]},
    {"id": "decide", "title": "Decide", "verb": "routes",
     "plain": "Simple rules, no AI, pick the cheapest way to answer: the clock or your vault "
              "instantly, a quick chat, a web task, or a multi-step plan.",
     "modules": ["router", "assistant", "instant", "briefing", "parse", "registry", "gates", "pipeline", "confirm", "decide", "interpret", "selfmodel"]},
    {"id": "remember", "title": "Remember", "verb": "recalls",
     "plain": "Before answering it gathers what it already knows: your Obsidian notes, wiki "
              "claims with their sources and dates, and past conversations.",
     "modules": ["vault", "knowledge", "memory", "vault_mcp", "tasks", "history", "embed"]},
    {"id": "think", "title": "Think", "verb": "answers",
     "plain": "The brain (ChatGPT through Codex, or Claude) writes the answer. Big jobs go to "
              "a planner that hands each step to a specialist agent.",
     "modules": ["chatgpt", "fastlane", "runner", "orchestrator", "prompts", "adapters",
                 "claude_auth", "grok_preflight", "usage"]},
    {"id": "act", "title": "Act", "verb": "does",
     "plain": "When you ask it to do something it opens apps and links, or drives its own "
              "Chrome. It asks before buying, posting or sending anything.",
     "modules": ["actions", "agent_browser", "remote", "grokbot", "transfer", "storage", "thumbnails", "clipsync", "showcase", "observe", "sharedinput"]},
    {"id": "speak", "title": "Speak", "verb": "replies",
     "plain": "The reply is spoken by ElevenLabs, falling back to Kokoro, a voice that "
              "runs on this laptop. ElevenLabs' agent also runs live calls from the console.",
     "modules": ["speech", "voice", "agent"]},
    {"id": "log", "title": "Log", "verb": "records",
     "plain": "Everything is written down: the conversation in memory, agent runs in the "
              "task ledger, and live events to this console.",
     "modules": ["store", "events", "locking", "cluster_store"]},
]
SIDE = [
    {"id": "surfaces", "title": "Surfaces",
     "plain": "Ways in: this console, the jarvis command, the Windows overlay.",
     "modules": ["ui", "__main__", "sysview", "cluster_cli", "cluster_proxy", "cluster_tunnel", "deploy", "node", "platforms", "autostart", "settings"]},
    {"id": "machines", "title": "Machines",
     "plain": "Knows both laptops: hardware, the shared mouse and keyboard, and how much "
              "of each AI plan is left.",
     "modules": ["devices", "inventory", "probe", "desktop", "selftest", "credits",
                 "cluster", "topology"]},
]
# Which station a request's tier lights up, in order.
PATHS = {
    "instant": ["hear", "decide", "remember", "speak", "log"],
    "simple": ["hear", "decide", "remember", "think", "speak", "log"],
    "medium": ["hear", "decide", "remember", "think", "act", "speak", "log"],
    "complex": ["hear", "decide", "remember", "think", "act", "log"],
}


def design(console):
    graph = console.system_graph()
    mods = {n["id"][4:]: n for n in graph["nodes"] if n["kind"] == "module"}
    home = {m: s["id"] for s in STATIONS + SIDE for m in s["modules"]}
    imports, used_by = {}, {}
    for e in graph["edges"]:
        if e["rel"] == "imports":
            a, b = e["src"][4:], e["dst"][4:]
            imports.setdefault(a, []).append(b)
            used_by.setdefault(b, []).append(a)
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
                      "numpy, sounddevice, Whisper, speaker ID and Kokoro are importable" if not deps
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
    if os.name != "nt" and "sounddevice" not in deps:
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
    scheduled = sched.get("installed") or sched.get("kind") == "schtasks"
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
        or (os.name == "nt" and "chrome")
    out.append(_check("act", "Browser for web tasks", bool(shutil.which("npx") and chrome),
                      "Playwright and agent-browser drive Chrome with their own profiles" if shutil.which("npx") and chrome
                      else "needs Node (npx) and Google Chrome", "install nodejs, npm and google-chrome"))
    kokoro = all((voice_dir / "kokoro" / f).is_file() for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"))
    out.append(_check("speak", "Kokoro voice files", kokoro,
                      "local voice ready" if kokoro else "replies fall back to espeak or text",
                      "see README: Running on Arch (Kokoro download)"))
    player = os.name == "nt" or any(shutil.which(p) for p in ("pw-play", "paplay", "aplay"))
    out.append(_check("speak", "Speaker output", player, "audio player found" if player else
                      "no audio player", "sudo pacman -S pipewire"))
    usage = console.store.overview(limit=1)
    out.append(_check("log", "Task ledger readable", True, f"{sum(usage['states'].values()) if isinstance(usage.get('states'), dict) else 0} agent runs recorded"))
    return out


def stores(console):
    state = console.state

    def size(path):
        path = Path(path)
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0

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
            "stt": voice.get("stt_model"), "platform": sys.platform,
            "python": sys.version.split()[0], "state": str(console.state)}
