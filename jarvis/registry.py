"""The Intent Registry: every action JARVIS can take, in one table.

Each intent says which executor runs it, how risky it is, how to observe that it
happened, how long to wait for that, and whether a certain failure may escalate to
Grok. Gates, verification and escalation all read this table, so they never disagree
(docs/jarvis-os-architecture.md, "The Intent Registry").

The parser and the interpreter both produce commands in one shape:
    {"id": "c1", "intent": "open_app", "device": "dell", "args": {...}, "depends_on": []}
validate() is the strict check both must pass. It never decides risk from the text:
risk() reads the table, except for agent tasks, whose risk the gates ratchet.
"""

import re
from dataclasses import dataclass

RISKS = ("allow", "confirm", "deny")
MEDIA = ("play_pause", "play", "pause", "next", "previous", "stop")
# App deep links a command may open besides http(s); anything else is refused.
DEEP_LINKS = ("spotify",)
# Apps whose windows hold no unsaved work, so closing them needs no confirmation.
SAFE_TO_CLOSE = frozenset({"spotify", "calculator", "grok bot"})

# Outcome states (L5). Only pre_effect_failure is certain enough to escalate.
# partial_effect: a multi-step action changed something, then a later step failed
# (Spotify opened, play failed). It failed, but not "nothing happened": never escalated.
# unchanged: the requested state holds, but it already did before the action ran.
OUTCOMES = ("pre_effect_failure", "partial_effect", "acknowledged", "observed", "unchanged",
            "unconfirmed", "uncertain", "agent_claimed", "user_confirmed", "user_rejected")
CERTAIN = frozenset({"pre_effect_failure", "observed", "user_confirmed", "user_rejected"})

MAX_TEXT = 4000
SPOTIFY = re.compile(r"(?:liked|(?:playlist|album|track|artist):[A-Za-z0-9]{1,64})\Z")
URL = re.compile(r"(?:https?://[^\s\"'<>]{1,2000}|spotify:[A-Za-z0-9:]{1,200})\Z")
LEVEL = re.compile(r"(?:up|down|mute|unmute|\d{1,3})\Z")
SCROLL_DIRECTIONS = ("down", "up")
SCROLL_MODES = ("once", "auto", "stop")
EVERY = re.compile(r"\d{1,3}(?:\.\d)?\Z")
BROWSE_ACTIONS = ("open", "scroll", "back", "forward", "reload", "close")


def _name(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 200


def _text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= MAX_TEXT


def _url(value):
    return isinstance(value, str) and bool(URL.match(value)) and \
        (value.startswith(("http://", "https://")) or value.split(":", 1)[0] in DEEP_LINKS)


def _http(value):
    return _url(value) and value.startswith(("http://", "https://"))


def _level(value):
    return isinstance(value, str) and bool(LEVEL.match(value)) and \
        (not value.isdigit() or int(value) <= 100)


def _sys(name):
    """A sysintents validator, looked up when used (the module is imported lazily)."""
    def check(value):
        from . import sysintents
        return getattr(sysintents, name)(value)
    return check


@dataclass(frozen=True)
class Intent:
    name: str
    executor: str            # direct, grokbot, brain, vault, orchestrator
    risk: str                # allow / confirm; "ratchet" for agent tasks
    args: dict               # arg -> (validator, required)
    observer: str | None     # what the node checks; None = no observable effect
    settle: float | None     # seconds to wait for the observer
    escalate: bool = False   # a certain failure may go to Grok, once

    @property
    def effect(self):
        return self.observer is not None


INTENTS = {i.name: i for i in (
    Intent("open_app", "direct", "allow", {"name": (_name, True)},
           "a window of the app appears", 8),
    Intent("open_url", "direct", "allow",
           {"url": (_url, True), "browser": (_name, False)},
           "a browser window's title names the site", 6),
    Intent("focus_window", "direct", "allow", {"name": (_name, True)},
           "the focused window is the app", 2),
    Intent("close_app", "direct", "confirm", {"name": (_name, True)},
           "the app's windows are gone", 5),
    Intent("media", "direct", "allow", {"command": (lambda v: v in MEDIA, True)},
           "playback state matches", 3, escalate=True),
    Intent("volume", "direct", "allow", {"level": (_level, True)},
           "the volume reads back", 1),
    # System intents (sysintents.py): typed, run directly outside the terminal sandbox.
    Intent("brightness", "direct", "allow", {"level": (_sys("valid_level"), True)},
           "the brightness reads back", 1),
    Intent("screenshot", "direct", "allow",
           {"path": (_text, False), "window": (lambda v: isinstance(v, bool), False)}, None, None),
    Intent("system_status", "direct", "allow",
           {"what": (lambda v: v in ("battery", "wifi", "bluetooth", "windows", "all"), False)},
           None, None),
    Intent("workspace", "direct", "allow", {"n": (_sys("valid_workspace"), True)}, None, None),
    # pair and forget are security decisions: risk() makes them "confirm" (David's OK).
    Intent("bluetooth", "direct", "allow",
           {"action": (_sys("valid_bt_action"), True), "name": (_name, False),
            "unnamed": (lambda v: isinstance(v, bool), False)}, None, None),
    Intent("lock_screen", "direct", "allow", {}, None, None),
    Intent("reminder", "direct", "allow",
           {"minutes": (_sys("valid_minutes"), True), "text": (_text, False)}, None, None),
    Intent("weather", "direct", "allow",
           {"city": (_sys("valid_city"), False),
            "day": (lambda v: v in ("today", "tomorrow"), False)}, None, None),
    # auto keeps scrolling a window on its own until "stop", the window closes or 30 min.
    Intent("scroll", "direct", "allow",
           {"direction": (lambda v: v in SCROLL_DIRECTIONS, False),
            "mode": (lambda v: v in SCROLL_MODES, False), "target": (_name, False),
            "app": (_name, False), "url": (_url, False),
            "every": (lambda v: isinstance(v, str) and bool(EVERY.match(v))
                      and 2 <= float(v) <= 300, False)}, None, None),
    # The agent browser (agent_browser.py): JARVIS's own Chrome, shown in the console.
    # Its executor reads the page back itself, so there is no window to observe.
    Intent("browse", "direct", "allow",
           {"action": (lambda v: v in BROWSE_ACTIONS, True), "url": (_http, False),
            "direction": (lambda v: v in SCROLL_DIRECTIONS, False),
            "mode": (lambda v: v in SCROLL_MODES, False),
            "every": (lambda v: isinstance(v, str) and bool(EVERY.match(v))
                      and 2 <= float(v) <= 300, False)}, None, None),
    Intent("list_apps", "direct", "allow", {"query": (_name, True)}, None, None),
    # A shell command (terminal.py): ls/cd/cat-style looking runs, anything else is confirmed.
    Intent("terminal", "direct", "confirm",
           {"command": (_text, True), "cwd": (_text, False)}, None, None),
    Intent("spotify_play", "direct", "allow",
           {"target": (lambda v: isinstance(v, str) and bool(SPOTIFY.match(v)), True)},
           "Spotify reports playing", 6, escalate=True),
    Intent("agent_task", "grokbot", "ratchet", {"text": (_text, True)}, None, None),
    Intent("answer", "brain", "allow", {"text": (_text, True)}, None, None),
    Intent("vault_write", "vault", "allow",
           {"text": (_text, True), "target": (_name, False)},
           "the note contains the text", 1),
    Intent("build", "orchestrator", "confirm", {"goal": (_text, True)}, None, None),
)}


def get(name):
    if name not in INTENTS:
        raise KeyError(f"Unknown intent {name!r}.")
    return INTENTS[name]


def risk(command):
    """allow / confirm for a validated command. Agent tasks return "ratchet": the gates
    take the strictest of the keyword rules, the decider and the review."""
    intent = get(command["intent"])
    if intent.name == "close_app" and command["args"]["name"].strip().lower() in SAFE_TO_CLOSE:
        return "allow"
    if intent.name == "bluetooth" and command["args"].get("action") in ("pair", "forget"):
        return "confirm"
    if intent.name == "terminal":
        from .terminal import read_only
        args = command["args"]
        return "allow" if read_only(args["command"], args.get("cwd")) else "confirm"
    return intent.risk


def validate(commands, devices):
    """Errors in a command list (empty when it is valid). `devices`: configured IDs."""
    if not isinstance(commands, list) or not commands:
        return ["No commands."]
    errors, ids = [], set()
    for n, command in enumerate(commands):
        where = f"command {n + 1}"
        if not isinstance(command, dict):
            errors.append(f"{where} is not an object.")
            continue
        cid = command.get("id")
        if not isinstance(cid, str) or not cid or cid in ids:
            errors.append(f"{where} needs a unique id.")
        ids.add(cid)
        intent = INTENTS.get(command.get("intent"))
        if intent is None:
            errors.append(f"{where}: unknown intent {command.get('intent')!r}.")
            continue
        if command.get("device") not in devices:
            errors.append(f"{where}: unknown device {command.get('device')!r}.")
        args = command.get("args")
        if not isinstance(args, dict):
            errors.append(f"{where}: args must be an object.")
            continue
        for key in args:
            if key not in intent.args:
                errors.append(f"{where}: {intent.name} takes no {key!r}.")
        for key, (check, required) in intent.args.items():
            if key in args:
                if not check(args[key]):
                    errors.append(f"{where}: invalid {key} for {intent.name}.")
            elif required:
                errors.append(f"{where}: {intent.name} needs {key}.")
    for command in commands:
        if isinstance(command, dict):
            deps = command.get("depends_on", [])
            if not isinstance(deps, list) or any(d not in ids or d == command.get("id")
                                                 for d in deps):
                errors.append(f"command {command.get('id')}: bad depends_on.")
    if not errors:  # a dependency cycle never becomes runnable and would hang the turn
        deps = {c.get("id"): set(c.get("depends_on", [])) for c in commands}
        while deps:
            ready = [i for i, d in deps.items() if not d & deps.keys()]
            if not ready:
                errors.append("depends_on has a cycle: " + ", ".join(sorted(map(str, deps))) + ".")
                break
            for i in ready:
                del deps[i]
    return errors
