"""The settings the console can change, each with a type and limits.

Only these fields can be written; everything else in jarvis.config.json is edited by hand.
Saves are atomic (cluster.save_json), and the running console's config is updated in place.
The speaker-check threshold lives in .jarvis/voice/voiceprint.json, not the config.
"""

import json
from pathlib import Path

from .cluster import save_json


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
    "vault.write": ("memory", "Write to my vault", "Allow adding notes when you ask. "
                    "Always additive.", "bool", None),
}
VOICE_THRESHOLD = (0.20, 0.60)


def _get(config, path):
    value = config
    for key in path.split("."):
        value = (value or {}).get(key) if isinstance(value, dict) else None
    return value


def _voiceprint(state):
    return Path(state) / "voice" / "voiceprint.json"


def read(config, state):
    """Every editable field with its current value and limits, grouped by section."""
    from .daemon import BORDERLINE
    defaults = {"voice.near_miss": BORDERLINE, "cluster.tunnel": "reverse"}
    fields = []
    for path, (section, label, help_, kind, extra) in FIELDS.items():
        item = {"path": path, "section": section, "label": label, "help": help_, "kind": kind,
                "value": _get(config, path) if _get(config, path) is not None else defaults.get(path)}
        if kind == "choice":
            item["choices"] = extra(config)
        elif kind == "number":
            item["min"], item["max"], item["step"] = extra
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
    return {"fields": fields, "machines": machines(config, state), "this_os": label()}


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
        from .cluster_store import Broker
        from .deploy import code_fingerprint
        nodes, code = {n["id"]: n for n in Broker(state).nodes()}, code_fingerprint()
    out = []
    for key, d in (config.get("devices") or {}).items():
        item = {"id": key, "label": (d or {}).get("label") or key,
                "role": "main" if key == main else "node", "this": key == local}
        if key != main and (d or {}).get("transport") == "cluster" and local == main:
            node = nodes.get(key)
            item["paired"] = bool(node)
            if node:
                item.update(online=node["online"], seen=node["seen"],
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


def write(config, path, state, changes):
    """Apply {path: value} after validating every change; nothing is saved if one is bad."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send at least one setting to change.")
    clean, threshold = {}, None
    for key, value in changes.items():
        if key == "voiceprint.threshold":
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not VOICE_THRESHOLD[0] <= value <= VOICE_THRESHOLD[1]:
                raise ValueError("Speaker threshold must be between 0.2 and 0.6.")
            threshold = round(float(value), 3)
        elif key in FIELDS:
            clean[key] = _valid(config, key, value)
        else:
            raise ValueError(f"'{key}' can't be changed here.")
    if clean:
        disk = json.loads(Path(path).read_text(encoding="utf-8"))
        for target in (disk, config):
            for key, value in clean.items():
                *parents, leaf = key.split(".")
                node = target
                for part in parents:
                    node = node.setdefault(part, {})
                node[leaf] = value
        save_json(path, disk)
    if threshold is not None:
        print_path = _voiceprint(state)
        data = json.loads(print_path.read_text(encoding="utf-8"))
        data["threshold"] = threshold
        save_json(print_path, data)
    return {**read(config, state), "saved": sorted([*clean, *(["voiceprint.threshold"] if threshold else [])]),
            "restart": [k for k in clean if k.startswith(("cluster.", "voice."))]
                       + (["voiceprint.threshold"] if threshold else [])}
