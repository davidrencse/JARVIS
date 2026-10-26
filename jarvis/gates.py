"""L3 gates: the checks every command passes before it is sent (docs/jarvis-os-architecture.md).

Cheapest first, fail fast. Each check returns {"name", "pass", "why"} for the trace.
A command is dispatched only when every applicable check passes and its risk is
"allow". "confirm" commands wait for David; until the confirmation queue exists they
are held back with a reply saying so, never sent.

The checks that need the outside world take it as arguments (reachable, ready,
app_exists), so the gates themselves stay pure and testable.
"""

import time
import threading
import re

from . import registry

AGENT_ORIGINS = frozenset({"agent", "system"})
DUPLICATE_WINDOW = 2.0   # seconds: a double-triggered utterance is merged, not resent


class InFlight:
    """Commands being executed now (U7, and the in-flight half of H3)."""

    def __init__(self):
        self._lock, self._items = threading.Lock(), {}

    @staticmethod
    def key(command):
        return (command["device"], command["intent"],
                tuple(sorted((k, str(v)) for k, v in command["args"].items())))

    def claim(self, command):
        """True if this caller now owns the command; False if an identical one is running
        or finished within DUPLICATE_WINDOW."""
        key, now = self.key(command), time.monotonic()
        with self._lock:
            started = self._items.get(key)
            if started is not None and now - started < DUPLICATE_WINDOW:
                return False
            self._items[key] = now
            return True

    def release(self, command, ok=True):
        """Done. A success keeps the merge window (a double trigger stays merged); a
        failure clears it, so retrying right away runs instead of "already doing that"."""
        with self._lock:
            if ok:
                self._items[self.key(command)] = time.monotonic()
            else:
                self._items.pop(self.key(command), None)


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
