"""The settings the console can change, each with a type and limits.

Only these fields can be written; everything else in jarvis.config.json is edited by hand.
A save takes the managed config lock and patches only the keys it validated onto the file
as it is on disk (config_lock.patch_config). It does not write a snapshot it read earlier,
so a main or tunnel change that landed meanwhile stays. The running process is updated in
place except cluster.* , which applies at restart. The speaker-check threshold lives in
.jarvis/voice/voiceprint.json, not the config.
"""

import json
from pathlib import Path

from .cluster import save_json
from .gates import CONFIRM_DEFAULT


def _devices(config):
    return list((config.get("devices") or {}))


# path -> (section, label, help, kind, choices-or-range)
FIELDS = {
    "cluster.main_device": ("machines", "Main PC", "Runs the brain, memory and vault, and is "
                            "the default for actions. Every other computer becomes a node.",
                            "choice", _devices),
    "cluster.tunnel": ("machines", "Tunnel direction", "Reverse: the main dials each node "
                       "(nodes need an SSH server). Forward: nodes dial the main.",
                       "choice", lambda c: ["reverse", "forward"]),
    "voice.near_miss": ("voice", "Near-miss floor", "Voices scoring between this and the "
                        "threshold count only when they start with “Jarvis”.",
                        "number", (0.05, 0.36, 0.01)),
    "voice.kokoro_voice": ("voice", "Spoken voice", "The local Kokoro voice JARVIS speaks with.",
                           "choice", lambda c: ["bm_lewis", "bm_george", "am_michael", "am_adam",
                                                "af_heart", "af_bella", "bf_emma"]),
    "voice.kokoro_speed": ("voice", "Speaking speed", "1.0 is natural pace.",
                           "number", (0.7, 1.5, 0.05)),
    "voice.max_spoken_chars": ("voice", "Spoken reply length", "Longer replies are shown, "
                               "not read aloud, past this many characters.",
                               "number", (100, 1500, 50)),
    "routing.brain": ("intelligence", "Brain", "Answers conversation. The other one is the "
                      "automatic fallback on usage limits.",
                      "choice", lambda c: ["chatgpt", "claude"]),
    "routing.interpreter": ("intelligence", "Interpreter", "Lets ChatGPT turn requests the "
                            "parser can't read into actions (about 1,100 tokens each).",
                            "bool", None),
    "routing.auto_escalate": ("intelligence", "Auto-escalate", "Retry hard tasks on a "
                              "stronger tier without asking.", "bool", None),
    "decisions.shadow": ("intelligence", "Decider shadow mode", "The local decider logs what "
                         "it would choose so it can be calibrated. It never acts.",
                         "bool", None),
    "memory.semantic": ("memory", "Recall by meaning", "Search memory by meaning as well as "
                        "words, with a small local model (no tokens).", "bool", None),
    "vault.recall": ("memory", "Use my vault", "Bring relevant notes from your Obsidian vault "
                     "into answers.", "bool", None),
    # Permissions (David, 2026-11-07): what JARVIS may do on this PC, in one place.
    "rules.confirm": ("permissions", "Risky actions", "Ask first: risky actions wait in the gate "
                      "for your OK. Act at once: they run unasked, except the always-ask list below.",
                      "choice", lambda c: ["ask", "never"]),
    "actions.enabled": ("permissions", "Act on my computers", "Open and close apps, windows, "
                        "media, files and terminal commands, and control the desktop. Off: JARVIS "
                        "only talks and remembers.", "bool", None),
    "actions.browser": ("permissions", "Use the browser", "Drive Chrome and the JARVIS browser "
                        "for web tasks. Sending, posting and buying still wait for you.", "bool", None),
    "actions.grokbot": ("permissions", "Hand tasks to Grok Bot", "Let Grok Bot work inside "
                        "apps and accounts after its preflight checks.", "bool", None),
    "vault.write": ("permissions", "Write to my vault", "Allow adding notes when you ask. "
                    "Always additive.", "bool", None),
    "terminal.sandbox": ("permissions", "Sandbox terminal commands", "Linux: every command "
                         "runs in bubblewrap with secrets, browser profiles and JARVIS's own "
                         "files hidden or read-only. Windows has no sandbox.", "bool", None),
    "lan_mouse.enabled": ("permissions", "Share mouse and keyboard", "Lan Mouse on this PC. "
                          "Off: the guard never starts it.", "bool", None),
    "lan_mouse.off_when_apart": ("permissions", "Stop sharing when apart", "Turn Lan Mouse off "
                                 "when the other laptop isn't on this network.", "bool", None),
}

# A change to one of these values weakens a safety net: the console must say so and the
# save must carry confirmed: true (write() refuses it otherwise).
LOOSENS = {"rules.confirm": "never", "terminal.sandbox": False, "lan_mouse.off_when_apart": False}
DEFAULTS = {"rules.confirm": CONFIRM_DEFAULT, "actions.enabled": True, "actions.browser": True,
            "actions.grokbot": True, "terminal.sandbox": True, "lan_mouse.enabled": True,
            "lan_mouse.off_when_apart": True, "vault.write": False}
# Restart-free in this process but built once in the voice listener's brain.
LISTENER_RESTART = ("routing.brain", "memory.semantic", "actions.enabled", "actions.browser",
                    "actions.grokbot")
# What no setting can change (gates.hard_deny and rules.always_ask), shown read-only.
NEVER = ["Type passwords, 2FA codes or card numbers",
         "Delete or wipe your home, projects, vault or system folders",
         "Edit its own code, config, keys or state",
         "Change your shell, Hyprland or startup files",
         "Reach your private accounts' credentials or browser profiles",
         "Talk out loud on a node PC (only the main speaks)"]


def permissions(config):
    """Read-only facts for the Permissions page."""
    rules = config.get("rules") or {}
    from .platforms import on_windows
    return {"always_ask": list(rules.get("always_ask") or ["money", "messages"]),
            "never": NEVER, "sandbox_here": not on_windows(),
            "stop": ["Super+Shift+Esc or `jarvis control stop` stops desktop control",
                     "Disconnect or closing the tab ends remote control and releases held keys",
                     "Ctrl+Shift+Super+Alt releases Lan Mouse; `jarvis lanmouse stop` turns it off",
                     "`jarvis sleep --no-suspend` pauses all of JARVIS on this PC"]}
VOICE_THRESHOLD = (0.20, 0.60)


def _get(config, path):
    value = config
    for key in path.split("."):
        value = (value or {}).get(key) if isinstance(value, dict) else None
    return value


def _put(doc, key, value):
    """Set one dotted key on `doc`. Parents that are missing or not objects are created."""
    parts = key.split(".")
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


_SEEN = {}  # config file -> mtime refresh() last copied from


_WRITE_LOCK = __import__("threading").Lock()


def refresh(config, path):
    """Pick up the console's saves in another process (the voice listener keeps its own
    config): every field that needs no restart is copied from disk into `config` in place,
    so e.g. vault.write switched off in the console reaches the voice brain too. Cheap: the
    file is reread only when its mtime changes. True when something was reread."""
    try:
        stamp = Path(path).stat().st_mtime_ns
        if _SEEN.get(str(path)) == stamp:
            return False
        disk = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    _SEEN[str(path)] = stamp
    for key in FIELDS:
        if key.startswith(("cluster.", "voice.")):  # these say "restart needed" in the console
            continue
        *parents, leaf = key.split(".")
        node = config
        for part in parents:
            node = node.setdefault(part, {})
        value = _get(disk, key)
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return True


def _voiceprint(state):
    return Path(state) / "voice" / "voiceprint.json"


_SAVED_CLUSTER = {}  # state dir -> cluster.* values saved to disk that apply at restart


def read(config, state):
    """Every editable field with its current value and limits, grouped by section."""
    from .daemon import BORDERLINE
    saved = _SAVED_CLUSTER.get(str(state))
    if saved:  # show what was saved, not the running value, or a save looks like it failed
        config = {**config, "cluster": {**(config.get("cluster") or {}), **saved}}
    defaults = {"voice.near_miss": BORDERLINE, "cluster.tunnel": "reverse", **DEFAULTS}
    fields = []
    for path, (section, label, help_, kind, extra) in FIELDS.items():
        item = {"path": path, "section": section, "label": label, "help": help_, "kind": kind,
                "value": _get(config, path) if _get(config, path) is not None else defaults.get(path)}
        if kind == "choice":
            item["choices"] = extra(config)
        elif kind == "number":
            item["min"], item["max"], item["step"] = extra
        if path in LOOSENS:
            item["loosens"] = LOOSENS[path]
        fields.append(item)
    try:
        threshold = json.loads(_voiceprint(state).read_text(encoding="utf-8"))["threshold"]
    except (OSError, ValueError, KeyError):
        threshold = None
    fields.insert(2, {"path": "voiceprint.threshold", "section": "voice", "label": "Speaker threshold",
                      "help": "How closely a voice must match yours to be accepted outright. "
                              "Your enrolled clips score about 0.8; other voices under 0.25.",
                      "kind": "number", "value": threshold, "min": VOICE_THRESHOLD[0],
                      "max": VOICE_THRESHOLD[1], "step": 0.01, "disabled": threshold is None})
    from .platforms import label
    return {"fields": fields, "machines": machines(config, state), "this_os": label(),
            "permissions": permissions(config)}


def _node_rows(state):
    """The paired nodes without Broker(state): its constructor runs schema DDL (a write lock
    on a database the hub is using). Use the running hub's broker, else read the file."""
    import sqlite3
    import time
    from .cluster import _HUBS
    hub = _HUBS.get(str(state)) or _HUBS.get(str(Path(state).resolve()))
    if hub is not None and getattr(hub, "broker", None) is not None:
        return hub.broker.nodes()
    from .cluster_store import ONLINE
    db = sqlite3.connect(Path(Path(state) / "cluster.sqlite3").resolve().as_uri() + "?mode=ro",
                         uri=True, timeout=2)
    try:
        rows = db.execute("SELECT id,capabilities,seen,info FROM nodes ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [{"id": r[0], "capabilities": json.loads(r[1]), "seen": r[2],
             "online": bool(r[2] and time.time() - r[2] < ONLINE), **json.loads(r[3] or "{}")}
            for r in rows]


def machines(config, state):
    """Every configured computer with its role, plus live link status when this PC is the
    main: whether the node is paired, online, when it last checked in, and if it runs our code."""
    from .topology import identity
    main = _get(config, "cluster.main_device")
    try:
        local = identity(config)[0]
    except ValueError:
        local = None
    nodes, code = {}, None
    if local and local == main and (Path(state) / "cluster.sqlite3").is_file():
        from .release import served_code
        nodes, code = {n["id"]: n for n in _node_rows(state)}, served_code(state)
    from .connections import presence, busy_nodes
    busy = busy_nodes(state)
    out = []
    for key, d in (config.get("devices") or {}).items():
        item = {"id": key, "label": (d or {}).get("label") or key,
                "role": "main" if key == main else "node", "this": key == local}
        if key != main and (d or {}).get("transport") == "cluster" and local == main:
            node = nodes.get(key)
            item["paired"] = bool(node)
            if node:
                item.update(online=node["online"], presence=presence(node["seen"], key in busy), seen=node["seen"],
                            same_code=bool(node.get("code")) and node.get("code") == code)
        out.append(item)
    return out


def _valid(config, path, value):
    section, label, _, kind, extra = FIELDS[path]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be on or off.")
        return value
    if kind == "choice":
        if value not in extra(config):
            raise ValueError(f"{label} must be one of: {', '.join(extra(config))}.")
        return value
    low, high, _ = extra
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return int(value) if float(low).is_integer() and float(value).is_integer() else round(float(value), 3)


def write(config, path, state, changes, confirmed=False):
    """Apply {path: value} after validating every change; nothing is saved if one is bad.
    A change that loosens a safety net (LOOSENS) needs confirmed=True."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send at least one setting to change.")
    clean, threshold = {}, None
    for key, value in changes.items():
        if key == "cluster.main_device":
            raise ValueError("Moving MAIN is unavailable until safe handoff is implemented. See Connections for readiness.")
        if key == "voiceprint.threshold":
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not VOICE_THRESHOLD[0] <= value <= VOICE_THRESHOLD[1]:
                raise ValueError("Speaker threshold must be between 0.2 and 0.6.")
            threshold = round(float(value), 3)
        elif key in FIELDS:
            clean[key] = _valid(config, key, value)
        else:
            raise ValueError(f"'{key}' can't be changed here.")
    def current(key):
        node = config
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node
    # Saving a value it already has (a slider dropped back) changes nothing to restart for.
    changed = [k for k in clean if current(k) != clean[k]]
    risky = [k for k in changed if k in LOOSENS and clean[k] == LOOSENS[k]]
    if risky and confirmed is not True:
        raise ValueError(f"{FIELDS[risky[0]][1]}: this weakens a safety net. Confirm it in the console.")
    if clean:
        from .config_lock import ConfigDrift, patch_config

        def change(doc):
            for key, value in clean.items():
                _put(doc, key, value)

        # Re-read under the fleet lock and write only these keys. A main or tunnel edit that
        # landed after this process loaded its copy is not written back. A byte change between
        # the read and the replace (a same-thread re-entrant writer, or an editor) is retried
        # against the latest file a few times, then refused.
        drift = None
        for _attempt in range(3):
            try:
                patch_config(state, path, change, backup="settings")
            except ConfigDrift as exc:
                drift = exc
                continue
            else:
                try:
                    Path(path).chmod(0o600)
                except OSError:
                    pass
                break
        else:
            raise drift
        for key, value in clean.items():
            if key.startswith("cluster."):
                continue  # a new tunnel applies at restart, not mid-request
            _put(config, key, value)
        moved = {k.split(".", 1)[1]: v for k, v in clean.items() if k.startswith("cluster.")}
        if moved:
            _SAVED_CLUSTER.setdefault(str(state), {}).update(moved)
    if threshold is not None:
        with _WRITE_LOCK:  # two threshold saves must not overwrite each other
            print_path = _voiceprint(state)
            data = json.loads(print_path.read_text(encoding="utf-8"))
            data["threshold"] = threshold
            save_json(print_path, data)
    return {**read(config, state), "saved": sorted([*clean, *(["voiceprint.threshold"] if threshold else [])]),
            "restart": [k for k in changed if k.startswith(("cluster.", "voice."))
                        or k in LISTENER_RESTART]  # the voice listener can't rebuild
                       + (["voiceprint.threshold"] if threshold else [])}
