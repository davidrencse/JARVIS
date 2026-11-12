"""Let the main PC sleep: pause JARVIS, suspend, and bring it all back on wake.

`jarvis sleep` (David, 2026-11-07: "ensure jarvis can be shutdown temporarily to allow my
laptop to sleep; the node PCs stay on"):
1. stops JARVIS's own user services on this PC (voice listener, hub, clipboard, Lan Mouse
   and its IP watcher), so nothing holds the mic, the hub or the shared input;
2. stops, for the night only, any other user service holding a *block* sleep lock (on the
   Zenbook that is the Instabox listener, whose lock makes every suspend "inhibited");
3. asks logind to suspend (the lid's own action, suspend-then-hibernate, by default);
4. after the wake, starts exactly what it stopped, in reverse order.

The restore runs in its own transient user unit, so closing the terminal (or the voice
listener that asked for it being one of the stopped services) never strands JARVIS off.
Nodes stay on: they keep polling with backoff and reconnect within ~15 s of the wake.
`jarvis sleep --no-suspend` only pauses; `jarvis wake` restores by hand.

Linux (systemd) only. Never touches system units, and only stops a user unit that is
running now; the record of what was stopped lives in .jarvis/rest.json.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .clipsync import UNIT_NAME as CLIPBOARD_UNIT
from .daemon import UNIT_NAME as VOICE_UNIT
from .platforms import run_quiet, systemctl_user

RECORD = "rest.json"
# JARVIS's own units. The IP watcher goes first: it is the guard that would restart
# Lan Mouse the moment Lan Mouse stops.
OWN_UNITS = ("lan-mouse-ipwatch.service", "lan-mouse.service", "lan-mouse-fallback.service",
             CLIPBOARD_UNIT, VOICE_UNIT, "jarvis-cluster.service")
WAKE_UNIT = "jarvis-wake"
SLEEP_ACTIONS = ("suspend", "suspend-then-hibernate", "hibernate", "hybrid-sleep")


def _run(argv, timeout=30):
    try:
        out = run_quiet(argv, timeout)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _user(*args, timeout=30):
    return systemctl_user(*args, timeout=timeout)


def supported():
    return os.name != "nt" and bool(shutil.which("systemctl")) and bool(shutil.which("systemd-inhibit"))


def active(unit):
    return _user("is-active", "--quiet", unit, timeout=10)[0] == 0


def _unit_of(pid):
    """The user service a process runs in (…/app.slice/x.service), or None."""
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    for part in reversed(cgroup.strip().split("/")):
        if part.endswith(".service") and "user@" not in part:
            return part
    return None


def _inhibitors():
    """logind's locks as [(what, who, why, mode, uid, pid)] (structured: WHO may hold spaces)."""
    code, out = _run(["busctl", "--json=short", "call", "org.freedesktop.login1",
                      "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListInhibitors"], 10)
    if code:
        return []
    try:
        return [tuple(row) for row in json.loads(out)["data"][0]]
    except (ValueError, KeyError, IndexError, TypeError):
        return []


def sleep_blockers(locks=None):
    """User services holding a *block* sleep lock, as [(unit, who, why)]. Delay locks
    (NetworkManager, hypridle, …) only postpone sleep and are left alone, as are other users'."""
    found, uid = [], os.getuid()
    for lock in (_inhibitors() if locks is None else locks):
        try:
            what, who, why, mode, owner, pid = lock
        except ValueError:
            continue
        if mode != "block" or owner != uid or "sleep" not in str(what).split(":"):
            continue
        unit = _unit_of(pid)
        if unit and unit not in OWN_UNITS and unit not in (u for u, *_ in found):
            found.append((unit, who, why))
    return found


def _record_path(state):
    return Path(state) / RECORD


def pause(state):
    """Stop JARVIS and the user services blocking sleep; record what was running."""
    if not supported():
        raise RuntimeError("jarvis sleep needs Linux with systemd; on Windows just use Sleep.")
    stopped, failed = [], []
    blockers = sleep_blockers()
    for unit in [*OWN_UNITS, *(u for u, *_ in blockers)]:
        if not active(unit):
            continue
        code, out = _user("stop", unit, timeout=60)
        (stopped if code == 0 else failed).append(unit if code == 0 else f"{unit}: {out.strip()[:120]}")
    record = {"since": time.time(), "stopped": stopped,
              "blockers": [{"unit": u, "who": w, "why": y} for u, w, y in blockers]}
    path = _record_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the units of an earlier pause that was never undone: wake must restart them too.
    try:
        old = json.loads(path.read_text(encoding="utf-8")).get("stopped") or []
    except (OSError, ValueError):
        old = []
    record["stopped"] = [*[u for u in old if u not in stopped], *stopped]
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"paused": True, "stopped": record["stopped"], "failed": failed,
            "blockers": record["blockers"]}


def wake(state):
    """Start what pause() stopped, in reverse (the hub before the voice, Lan Mouse last)."""
    path = _record_path(state)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"woke": False, "note": "Nothing to restore: JARVIS wasn't paused by `jarvis sleep`."}
    started, failed = [], []
    # Only units this module may stop: its own, and the sleep blockers it recorded by name.
    # A record edited by anything else can't start an arbitrary unit.
    blockers = {b.get("unit") for b in record.get("blockers") or [] if isinstance(b, dict)}
    allowed = set(OWN_UNITS) | blockers
    for unit in reversed(record.get("stopped") or []):
        if not isinstance(unit, str) or unit not in allowed:
            continue
        code, out = _user("start", unit, timeout=90)
        (started if code == 0 else failed).append(unit if code == 0 else f"{unit}: {out.strip()[:120]}")
    if not failed:
        path.unlink(missing_ok=True)
    return {"woke": True, "started": started, "failed": failed}


def _clocks():
    """(boottime, monotonic): monotonic stops while suspended, boottime doesn't."""
    return time.clock_gettime(time.CLOCK_BOOTTIME), time.monotonic()


def wait_for_resume(started_gap, limit=None, poll=1.0, clocks=_clocks, sleep=time.sleep):
    """Block until the PC has been asleep (the boottime-monotonic gap grew by > 3 s).
    limit: give up after this many monotonic seconds awake (the suspend never happened)."""
    begun = clocks()[1]
    while True:
        boot, mono = clocks()
        if boot - mono - started_gap > 3:
            return True
        if limit is not None and mono - begun > limit:
            return False
        sleep(poll)


def _gap():
    boot, mono = _clocks()
    return boot - mono


def after_suspend(state, gap, limit=120):
    """Body of the transient wake unit: wait for the resume, then restore everything."""
    slept = wait_for_resume(gap, limit=limit)
    result = wake(state)
    result["slept"] = slept
    return result


def sleep(state, base, action="suspend-then-hibernate", suspend=True, launch=None, request=None):
    """Pause JARVIS, arm the wake restore, then ask logind to suspend."""
    if action not in SLEEP_ACTIONS:
        raise ValueError(f"Sleep action must be one of: {', '.join(SLEEP_ACTIONS)}.")
    try:
        from .continuity.integration import refuse_unfenced, release_for_suspend
    except ImportError:  # continuity is optional; without it the legacy path is the only one
        refuse_unfenced = None
        release_for_suspend = None
    if refuse_unfenced is not None:
        refuse_unfenced("sleep")
    if release_for_suspend is not None:
        release_for_suspend(state)
    result = pause(state)
    if not suspend:
        result["note"] = "JARVIS is paused on this PC. Run `jarvis wake` to bring it back."
        return result
    gap = _gap()
    import sys
    argv = [sys.executable, "-m", "jarvis", "--state", str(Path(state).resolve()),
            "sleep", "--restore-after", f"{gap:.3f}"]
    # A transient unit outlives this terminal and the services it just stopped.
    launch = launch or (lambda: _run(
        ["systemd-run", "--user", "--collect", f"--unit={WAKE_UNIT}-{int(time.time())}",
         f"--working-directory={base}", *argv], 15))
    code, out = launch()
    if code:
        wake(state)
        raise RuntimeError(f"Couldn't arm the wake restore, so nothing was put to sleep: {out.strip()[:200]}")
    request = request or (lambda: _run(["systemctl", action], 30))
    code, out = request()
    if code:
        # Still inhibited (a root or delay-only lock we left alone) or not allowed: undo now.
        # The armed unit gives up after its limit; wake() here has already restored.
        undone = wake(state)
        raise RuntimeError(f"The PC refused to {action} ({out.strip()[:200]}). JARVIS is back on: "
                           f"{', '.join(undone.get('started') or []) or 'nothing was stopped'}.")
    result["sleeping"] = action
    result["note"] = ("Going to sleep. JARVIS comes back by itself after the wake; nodes stay on "
                      "and reconnect within ~15 s.")
    return result
