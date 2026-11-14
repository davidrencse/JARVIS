"""Automatic Oracle gateway deploy after a release (opt-in: cluster.auto_gateway_deploy).

The gateway at /opt/jarvis is not a pull node, so it drifts from the release. The hub's
`_keep_up` loop calls `step(hub)`; when the gateway is behind the served release, nothing live
is using the phone/handoff path, and the retry window is open, it runs
`continuity.gateway_deploy.deploy(root=<release snapshot>, restart=True)` (backup, health check,
auto-rollback live there). One desktop notice on a failed release; never more than one attempt
per MIN_GAP seconds, longer after repeated failures.

State (`<state>/gateway-auto.json`): {"deployed": code last shipped, "fails", "next_ok",
"last_attempt", "notified": code the failure notice went out for, "error"}. The gateway has no
version endpoint, so "behind" means "this hub has not shipped the served release to it yet".
A manual deploy is not recorded here, so the first run after enabling ships once.

`due`, `gate` and `record_attempt` are pure; `step` is the only part that touches disk, ssh
(through the injectable `deploy`) or notify-send.
"""

import time
from pathlib import Path

MIN_GAP = 600                          # s between attempts, whatever happened
BACKOFF = (600, 1800, 3600, 7200)      # s to wait after the 1st, 2nd, ... failure
PHONE_LIVE = 120                       # s since a phone's last_seen that counts as in use
STATE_FILE = "gateway-auto.json"


def enabled(config):
    return bool(((config or {}).get("cluster") or {}).get("auto_gateway_deploy", False))


def due(rec, code, now):
    """(True, "") when the gateway should get `code` now, else (False, why)."""
    if not code:
        return False, "no release"
    if (rec or {}).get("deployed") == code:
        return False, "current"
    if now < float((rec or {}).get("next_ok") or 0):
        return False, "retry window"
    return True, ""


def gate(busy, gateway_status):
    """None when a deploy may run, else why it must wait. A deploy restarts the gateway,
    so any live phone session or continuity handoff holds it."""
    if busy:
        return "busy"
    status = gateway_status or {}
    if status.get("handoff_live"):
        return "handoff live"
    if status.get("phone_live"):
        return "phone session live"
    return None


def record_attempt(rec, code, ok, now, error=None):
    """The next record. ok=None reserves the attempt (before ssh runs, so a crash mid-deploy
    still waits MIN_GAP); True marks `code` shipped; False backs off and counts the failure."""
    rec = dict(rec or {})
    rec["last_attempt"] = now
    if ok is None:
        rec["next_ok"] = now + MIN_GAP
    elif ok:
        rec.update(deployed=code, fails=0, next_ok=now + MIN_GAP, error=None, notified=None)
    else:
        fails = int(rec.get("fails") or 0) + 1
        rec.update(fails=fails, error=str(error or "failed")[:300],
                   next_ok=now + max(MIN_GAP, BACKOFF[min(fails, len(BACKOFF)) - 1]))
    return rec


def gateway_status(state, now=None):
    """What the main can see of the phone path: {"phone_live": any phone seen < PHONE_LIVE s ago}.
    The handoff journals are covered by release_runtime.busy()."""
    now = time.time() if now is None else now
    try:
        from .phone import listing
        live = any(now - float(p.get("last_seen") or 0) < PHONE_LIVE for p in listing(state))
    except Exception:
        live = False
    return {"phone_live": live}


def _read(state):
    from .fsio import read_json
    rec = read_json(Path(state) / STATE_FILE, {})
    return rec if isinstance(rec, dict) else {}


def _write(state, rec):
    import json
    from .fsio import atomic_write
    atomic_write(Path(state) / STATE_FILE, json.dumps(rec, indent=2))


def _default_deploy(hub, code):
    from .continuity import gateway_deploy as gd
    from .release import release_dir
    opts = ((hub.config.get("cluster") or {}).get("gateway_deploy") or {})
    lines = []
    rc = gd.deploy(opts.get("host") or gd.DEFAULT_HOST, opts.get("key") or gd.DEFAULT_KEY,
                   restart=True, root=release_dir(hub.state), out=lines.append)
    return rc, "\n".join(lines[-3:])


def step(hub, *, deploy=None, notify=None, now=None, busy_fn=None, status_fn=None):
    """One check from the hub loop. Returns what happened: "off", "current", "wait: <why>",
    "deployed" or "failed". `deploy(hub, code)` returns (exit code, note)."""
    if not enabled(hub.config):
        return "off"
    from .release import served_code
    now = time.time() if now is None else now
    rec = _read(hub.state)
    code = served_code(hub.state)
    ok, why = due(rec, code, now)
    if not ok:
        return "current" if why == "current" else f"wait: {why}"
    if busy_fn is None:
        from .release_runtime import busy as busy_fn
    status = (status_fn or gateway_status)(hub.state)
    held = gate(busy_fn(hub.state, hub=hub), status)
    if held:
        return f"wait: {held}"
    _write(hub.state, record_attempt(rec, code, None, now))
    try:
        rc, note = (deploy or _default_deploy)(hub, code)
    except Exception as exc:  # DeployError, ssh timeout: backs off like any failure
        rc, note = 1, str(exc)
    done = rc == 0
    rec = record_attempt(rec, code, done, now, None if done else f"exit {rc}: {note}")
    if not done and rec.get("notified") != code:
        rec["notified"] = code
        if notify is None:
            from .confirm import notify
        notify(f"Gateway deploy of {code} failed ({rec['error']}). Retrying with backoff; "
               "run python -m jarvis.continuity.gateway_deploy --restart to see why.")
    _write(hub.state, rec)
    print(f"JARVIS: gateway deploy {code}: {'ok' if done else rec['error']}", flush=True)
    return "deployed" if done else "failed"
