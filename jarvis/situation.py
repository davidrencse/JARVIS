"""What David's computers are doing right now, as a few recall lines for the brain.

"Close that", "what's playing on the Dell", "is Discord open" need the live state of both
machines, which relevance recall can't find. Each machine reports observe.now(): the apps
with windows, the focused window's app and shortened title, and what's playing. Nothing is
polled while idle. A request reads the last snapshot at once; if it is older than FRESH
seconds, one background thread asks the machines again for the next request, so a request
never waits on a node. A machine that doesn't answer is left out, never guessed.
"""

import json
import threading
import time

FRESH = 30      # seconds a snapshot is reused before a request triggers a refresh
STALE = 120     # older than this (machine stopped answering), its line is dropped, not shown as now
NODE_WAIT = 8   # seconds a node gets to answer (it runs in the background anyway)
HEADER = "Your computers right now (live, read-only; refreshed every ~30 s):"

_STATE = {"at": 0.0, "machines": {}, "busy": False, "lock": threading.Lock()}


def _machines(config, state):
    """(id, is_this_pc) for this PC and every online node; this PC alone off-cluster."""
    from .topology import enabled, identity
    if not enabled(config):
        return [(None, True)]
    local, main = identity(config)
    found = [(local, True)]
    if local == main:
        try:
            from .cluster_store import Broker
            found += [(n["id"], False) for n in Broker(state).nodes()
                      if n.get("online") and n["id"] != local]
        except Exception:  # no cluster database yet
            pass
    return found


def _ask(config, state, device, here):
    if here:
        from .observe import now
        return now()
    from .cluster import dispatch
    result = dispatch(config, state, device, "desktop",
                      {"action": "observe", "arg": json.dumps({"now": True})}, timeout=NODE_WAIT)
    if not result.get("ok"):
        raise LookupError(str(result.get("text", ""))[:200])
    return json.loads(result.get("text") or "null")


def refresh(config, state, ask=None):
    """Ask every machine now (blocking). Machines that fail keep their last answer."""
    ask = ask or _ask
    seen = {}
    for device, here in _machines(config, state):
        try:
            report = ask(config, state, device, here)
        except Exception:  # offline, old code, no window tool: skip this machine
            continue
        if isinstance(report, dict):
            seen[device or "this PC"] = {"report": report, "at": time.time()}
    with _STATE["lock"]:
        _STATE["machines"].update(seen)
        _STATE["at"] = time.time()
        _STATE["busy"] = False


def _describe(name, report):
    parts = []
    focused = report.get("focused") or {}
    if focused.get("app"):
        title = f" \"{focused['title']}\"" if focused.get("title") else ""
        parts.append(f"focused {focused['app']}{title}")
    if report.get("apps") is not None:
        parts.append("open: " + (", ".join(report["apps"][:20]) or "nothing"))
    playing = report.get("playing") or {}
    if playing.get("status") == "Playing":
        track = " — ".join(p for p in str(playing.get("track") or "").split("|") if p)
        parts.append(f"playing {track}" if track else "playing media")
    return f"{name}: " + "; ".join(parts) if parts else None


def lines(config, state, now=None, start=None):
    """The snapshot as recall lines (possibly empty), and a background refresh when it has
    gone stale. `start` replaces threading for tests."""
    now = now or time.time()
    with _STATE["lock"]:
        due = now - _STATE["at"] > FRESH and not _STATE["busy"]
        if due:
            _STATE["busy"] = True
        machines = dict(_STATE["machines"])
    if due:
        def run():
            try:
                refresh(config, state)
            finally:
                _STATE["busy"] = False
        (start or (lambda fn: threading.Thread(target=fn, name="jarvis-situation", daemon=True).start()))(run)
    described = [_describe(name, m["report"]) for name, m in sorted(machines.items())
                 if now - m["at"] <= STALE]
    described = [d for d in described if d]
    return [HEADER + "\n  " + "\n  ".join(described)] if described else []


def reset():
    """Forget every snapshot (tests)."""
    with _STATE["lock"]:
        _STATE.update(at=0.0, machines={}, busy=False)
