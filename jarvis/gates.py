"""L3 gates: the checks every command passes before it is sent (docs/jarvis-os-architecture.md).

Cheapest first, fail fast. Each check returns {"name", "pass", "why"} for the trace.
A command is dispatched only when every applicable check passes and its risk is
"allow". "confirm" commands wait for David; until the confirmation queue exists they
are held back with a reply saying so, never sent.

The checks that need the outside world take it as arguments (reachable, ready,
app_exists), so the gates themselves stay pure and testable.
"""

import json
import os
import posixpath
import time
import threading
import re
from pathlib import Path

from . import registry

AGENT_ORIGINS = frozenset({"agent", "system"})
DUPLICATE_WINDOW = 2.0   # seconds after a run finishes: a double trigger is merged, not resent
RUNNING_CAP = 300.0      # seconds: an unreleased run (a lost thread) stops blocking repeats


class InFlight:
    """Commands being executed now (U7, and the in-flight half of H3)."""

    def __init__(self):
        # key -> (time, request, running): running since `time`, or finished at `time`.
        self._lock, self._items = threading.Lock(), {}

    @staticmethod
    def key(command):
        return (command["device"], command["intent"],
                tuple(sorted((k, str(v)) for k, v in command["args"].items())))

    def claim(self, command, request=None):
        """True if this caller now owns the command; False if an identical one is still
        running (however long it takes: "open spotify on arch" can take 45 s over the
        cluster) or finished within DUPLICATE_WINDOW. Commands of the same `request` are
        never merged ("next and next" means twice); only another request's duplicate is.
        A run never released stops blocking after RUNNING_CAP, so nothing sticks forever."""
        key, now = self.key(command), time.monotonic()
        with self._lock:
            for old in [k for k, (at, _, run) in self._items.items()  # forget expired entries
                        if now - at >= (RUNNING_CAP if run else DUPLICATE_WINDOW)]:
                del self._items[old]
            since, owner, running = self._items.get(key, (None, None, False))
            busy = since is not None and now - since < (RUNNING_CAP if running else DUPLICATE_WINDOW)
            if busy and (request is None or owner is None or owner != request):
                return False
            self._items[key] = (now, request, True)
            return True

    def release(self, command, ok=True, request=None):
        """Done. A success keeps the merge window (a double trigger stays merged); a
        failure clears it, so retrying right away runs instead of "already doing that"."""
        with self._lock:
            if ok:
                self._items[self.key(command)] = (time.monotonic(), request, False)
            else:
                self._items.pop(self.key(command), None)


# rules.trusted_local in jarvis.config.json: David's own low-risk actions on the PC he is
# using skip the checks that cost time (the installed-app lookup, history, the on-screen
# wait). Schema validation and the risk policy still run: they cost microseconds and are
# what keep a bad argument or a risky command (close_app, agent tasks) from being sent.
TRUSTED_LOCAL = {"enabled": True,
                 "intents": ["open_app", "open_url", "focus_window", "media", "volume",
                             "scroll", "list_apps", "spotify_play"],
                 "skip": ["app_exists", "history", "observe"]}
SKIPPABLE = frozenset({"app_exists", "history", "observe", "dedupe"})


def asks(config):
    """rules.confirm in jarvis.config.json: "ask" (default) parks risky actions for David's
    OK; "never" runs them at once (his choice, 2026-11-01). Refusals (passwords, 2FA codes,
    card numbers; agent-origin actions) stay either way: they are not questions (hard_deny).
    Only exactly "never" (whitespace aside) turns asking off; any other value asks."""
    return str(((config or {}).get("rules") or {}).get("confirm", "ask")).strip() != "never"


_LIVE = {}  # config path -> (mtime, rules.confirm)


def asks_now(config, state=None):
    """asks(), but from jarvis.config.json as it is on disk now when it sits next to the
    state folder (the usual layout): the brain and browser gates are built once, so a
    switch back to "ask" would otherwise wait for a restart. Falls back to `config`."""
    if state is not None and Path(state).is_dir():
        path = Path(state).resolve().parent / "jarvis.config.json"
        try:
            mtime = path.stat().st_mtime
            cached = _LIVE.get(str(path))
            if not cached or cached[0] != mtime:
                rules = (json.loads(path.read_text(encoding="utf-8")) or {}).get("rules") or {}
                _LIVE[str(path)] = cached = (mtime, rules.get("confirm", "ask"))
            return asks({"rules": {"confirm": cached[1]}})
        except (OSError, ValueError, AttributeError):
            pass  # no file there, or it's mid-edit: the config the gate was built with
    return asks(config)


# What a brain terminal command may never do, asked or not (hard_deny): enter credentials.
CREDENTIAL_CMD = re.compile(
    r"\bsudo\s+(?:-\w*S\w*|--stdin)\b|--password\b|--passwd\b|--passphrase\b|--with-token\b|"
    r"\bpasswd\b|\bchpasswd\b|\bsshpass\b|\bgh\s+auth\s+(?:login|token)\b|\bssh-keygen\b|"
    r"\bssh-add\b|\bsecret-tool\s+store\b|\bgit\s+credential\b|\bpass\s+(?:insert|edit)\b", re.I)
RM_NAMES = frozenset({"rm", "rmdir", "remove-item", "ri", "rd", "del", "erase", "unlink"})
SEPARATORS = re.compile(r"&&|\|\||[;|\n]")


def _vault_paths(config):
    path = ((config or {}).get("vault") or {}).get("path")
    paths = [v for v in path.values()] if isinstance(path, dict) else [path]
    return [str(p) for p in paths if p]


def _abs(path, here):
    text = os.path.expanduser(os.path.expandvars(str(path).replace("\\", "/")))
    if not (text.startswith("/") or re.match(r"[A-Za-z]:", text)):
        text = posixpath.join(str(here).replace("\\", "/"), text)
    return posixpath.normpath(text).rstrip("/") or "/"


def _under(path, root):
    path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root.rstrip("/") + "/")


def _terminal_denial(args, config, base):
    from . import terminal
    command = str((args or {}).get("command") or "")
    cwd_arg = (args or {}).get("cwd")
    if terminal.read_only(command, cwd_arg):
        return None
    if CREDENTIAL_CMD.search(command):
        return "JARVIS never enters passwords, tokens or keys"
    repo = Path(base).resolve() if base else Path(__file__).resolve().parent.parent
    repo_s = str(repo).replace("\\", "/")
    home = _abs("~", "/")
    vaults = [_abs(v, "/") for v in _vault_paths(config)]
    own = [repo_s + "/jarvis.config.json", repo_s + "/.jarvis", repo_s + "/JARVIS.md",
           home + "/.ssh"] + [h + "/.jarvis" for h in (home,)]
    here = _abs(cwd_arg, terminal.cwd()) if cwd_arg else _abs(terminal.cwd(), "/")
    for step in SEPARATORS.split(command):
        words = terminal._words(step) or [(w, w) for w in step.split()]
        if not words:
            continue
        values = [v for v, _ in words]
        name = values[0].lower()
        if name == "sudo" and len(values) > 1:
            name = values[1].lower()
        targets = []
        for value in values[1:]:
            # option=path forms (--out=~/x), and quoted/spaced pieces
            for piece in {value, value.split("=", 1)[-1]}:
                if piece and not (piece.startswith("-") and "=" not in piece):
                    targets.append(piece)
        paths = [_abs(t, here) for t in targets] + [here]
        for path in paths:
            if any(_under(path, v) for v in vaults):
                return "the Obsidian vault is read-only"
            if any(_under(path, o) for o in own) or \
                    re.search(r"(?:^|/)(?:\.jarvis|jarvis\.config\.json|JARVIS\.md|\.ssh)(?:/|$)",
                              path, re.I):
                return "JARVIS's own config, state and keys are off limits"
        if name in terminal.CD and values[1:]:
            here = paths[-2] if len(paths) > 1 else here
        if name in RM_NAMES:
            recursive = any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|-recurse|/s", v, re.I)
                            or v == "--no-preserve-root" for v in values[1:])
            if recursive or name in ("rmdir", "rd"):
                guarded = [repo_s, home, "/"] + vaults
                for t in targets[:]:
                    path = _abs(re.sub(r"/\*+$", "", t.replace("\\", "/")) or "/", here)
                    if any(_under(g, path) for g in guarded):
                        return "that delete would wipe JARVIS, its vault or the home folder"
    return None


def hard_deny(name, args, config, base=None):
    """Why a brain tool call is refused whatever rules.confirm says, or None. These are
    not questions to ask David: credentials are never typed, the vault is read-only, JARVIS
    doesn't edit its own gates or state. Checks: terminal commands that aren't
    terminal.read_only (credential-entry commands; any path naming the vault, repo
    jarvis.config.json, .jarvis/, JARVIS.md or ~/.ssh after ~, ./, //, .. and cd are
    resolved; recursive deletes covering /, home, the repo or the vault); grok_agent
    tasks that agent_risk() denies; browser type/fill/click calls (browser_gate.blocked)."""
    args = args if isinstance(args, dict) else {}
    if name == "terminal":
        return _terminal_denial(args, config, base)
    if name == "grok_agent":
        if agent_risk(str(args.get("task") or "")) == "deny":
            return "JARVIS never handles passwords, 2FA codes or card numbers"
        return None
    if re.match(r"(?:agent_)?browser_", str(name or "")):
        from . import browser_gate
        return browser_gate.blocked(name, args)
    return None


def trusted_rules(config):
    """The trusted_local rule, defaults filled in from TRUSTED_LOCAL."""
    rule = {**TRUSTED_LOCAL, **((config or {}).get("rules") or {}).get("trusted_local", {})}
    rule["skip"] = [s for s in rule.get("skip") or () if s in SKIPPABLE]
    rule["never_ask"] = not asks(config)
    return rule


def skips(command, origin, origin_class, rule):
    """The checks this command may skip: none unless it's the owner's own request, runs
    on the PC it came from, is a trusted intent and needs no confirmation."""
    if (not rule or not rule.get("enabled") or origin_class != "owner"
            or command.get("device") != origin
            or command.get("intent") not in (rule.get("intents") or ())):
        return frozenset()
    try:
        if registry.risk(command) != "allow":
            return frozenset()
    except KeyError:
        return frozenset()
    return frozenset(rule["skip"])


def _result(name, ok, why=""):
    return {"name": name, "pass": bool(ok), "why": why}


def check(command, devices, origin_class="owner", reachable=None, ready=None,
          app_exists=None, confirmed=False):
    """Run U1-U10 for one command. Returns {"pass", "risk", "checks", "why"}.

    reachable(device) -> bool, ready(executor, device) -> bool and
    app_exists(device, name) -> bool | None (None = can't tell here: the node decides)
    default to permissive stand-ins so callers opt in to each live check."""
    checks = []

    def fail(name, why):
        checks.append(_result(name, False, why))
        return {"pass": False, "risk": None, "checks": checks, "why": why}

    errors = registry.validate([{**command, "depends_on": []}], devices)  # deps: graph level
    if errors:
        return fail("U1 schema_valid", errors[0])
    checks.append(_result("U1 schema_valid", True))
    intent = registry.get(command["intent"])
    checks.append(_result("U2 device_resolved", True, command["device"]))

    if reachable is not None and not reachable(command["device"]):
        return fail("U3 device_reachable", f"{command['device']} isn't reachable right now.")
    checks.append(_result("U3 device_reachable", True))

    if ready is not None and not ready(intent.executor, command["device"]):
        return fail("U4 executor_ready",
                    f"I can't run {intent.name} on {command['device']} yet.")
    checks.append(_result("U4 executor_ready", True, intent.executor))

    checks.append(_result("U5 args_safe", True))  # enforced by the registry validators

    if intent.name in ("open_app", "close_app", "focus_window") and app_exists is not None:
        found = app_exists(command["device"], command["args"]["name"])
        if found is False:
            return fail("U6 target_exists",
                        f"{command['args']['name']} isn't installed on {command['device']}.")
        checks.append(_result("U6 target_exists", True,
                              "checked" if found else "left to the node"))

    if origin_class in AGENT_ORIGINS and (intent.effect or intent.name == "agent_task"):
        return fail("U9 loop_free", "Actions requested by an agent need David's own request.")
    checks.append(_result("U9 loop_free", True))

    if intent.effect and intent.observer is None:
        return fail("U10 success_defined", f"{intent.name} has no way to confirm it worked.")
    checks.append(_result("U10 success_defined", True))

    risk = registry.risk(command)
    if confirmed and risk != "deny":
        checks.append(_result("policy", True, "owner confirmed"))
        return {"pass": True, "risk": risk, "checks": checks, "why": ""}
    checks.append(_result("policy", risk == "allow", risk))
    if risk != "allow":
        return {"pass": False, "risk": risk, "checks": checks,
                "why": "needs your OK first" if risk == "confirm" else "needs a review first"}
    return {"pass": True, "risk": risk, "checks": checks, "why": ""}


def agent_risk(text, preflight=None):
    """Risk can only rise; credentials are refused before any model is contacted."""
    if re.search(r"\b(password|passcode|2fa\s+code|card\s+number)\b", text, re.I):
        return "deny"
    risky = (r"\b(buy|pay|order|book|send|message|reply|text|email|post|tweet|comment|"
             r"delete|remove|unsubscribe|submit|apply|sign|accept|transfer|install|uninstall)\b|"
             r"\bchange\s+settings\b")
    risk = "confirm" if re.search(risky, text, re.I) else "allow"
    if preflight:
        candidate = preflight.get("risk", preflight.get("decision"))
        if candidate == "deny":
            return "deny"
        if candidate == "confirm":
            risk = "confirm"
        task = preflight.get("task")
        if task:
            task_risk = agent_risk(task)
            if task_risk == "deny" or task_risk == "confirm" and risk == "allow":
                risk = task_risk
    return risk
