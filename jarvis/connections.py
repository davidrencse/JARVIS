"""The console's Connections view (Devices): every node PC's link to the main, and this
computer's voice listener and drop folder, live. Lan Mouse, clipboard and pairing are
peripherals.py ("Connect & share"). Read from the broker and local status calls; nothing
here opens a port or starts anything until David presses a button."""

import time

from . import daemon, transfer
from .topology import enabled, identity

# What each capability lets the main ask a node to do, in David's words.
CAPS = {"desktop": "open apps, screens, Lan Mouse", "files": "files and clipboard",
        "grokbot": "Grokbot", "inventory": "hardware report", "console": "use the console"}
HERE = {"voice": ("start", "stop", "restart")}


def _safely(fn, fallback=None):
    try:
        return fn()
    except Exception as exc:  # one broken part must not blank the whole view
        return fallback if fallback is not None else {"error": f"{type(exc).__name__}: {exc}"}


def _nodes(config, state, main):
    from .cluster_store import Broker
    from .deploy import code_fingerprint
    broker, code = Broker(state), code_fingerprint()
    out = []
    for node in broker.nodes():
        device = (config.get("devices") or {}).get(node["id"], {})
        seen = node.get("seen")
        out.append({
            "id": node["id"], "label": device.get("label") or node["id"],
            "hostname": node.get("hostname") or device.get("hostname"),
            "platform": node.get("platform"), "online": node["online"], "seen": seen,
            "silent_for": round(time.time() - seen) if seen else None,
            "code_ok": node.get("code") == code, "code": node.get("code"),
            "capabilities": [{"id": c, "does": CAPS.get(c, c)} for c in node["capabilities"]],
            "running": broker.running(node["id"]), "jobs_24h": broker.delivery(node["id"]),
            "address": device.get("lan_ip"),
        })
    return {"main": main, "code": code, "nodes": out}


def _voice(state):
    s = daemon.status(state)
    return {"running": s["listening"], "pid": s["pid"], "enrolled": s["enrolled"],
            "supervised": daemon._supervised(), "autostart": s["autostart"],
            "recent": [line for line in s["recent"] if "error" in line.lower()][-2:]}


def _drop():
    folder = transfer.drop_folder()
    files = []
    for path in folder.iterdir():
        if path.name.startswith(".jarvis-upload-"):
            continue  # a transfer still on its way
        try:
            info = path.stat()
        except OSError:
            continue
        files.append({"name": path.name, "bytes": info.st_size, "at": info.st_mtime,
                      "dir": path.is_dir()})
    files.sort(key=lambda f: f["at"], reverse=True)
    return {"folder": str(folder), "count": len(files), "latest": files[:6]}


def snapshot(config, state):
    """Everything the Connections view shows, in one call (~50 ms; no network)."""
    local = main = None
    if enabled(config):
        try:
            local, main = identity(config)
        except ValueError:
            pass
    cluster = ({"enabled": False} if main is None else
               {"enabled": True, "local": local, "role": "main" if local == main else "node",
                **_safely(lambda: _nodes(config, state, main), {"nodes": []})})
    return {"checked_at": time.time(), "cluster": cluster,
            "here": {"device": local, "voice": _safely(lambda: _voice(state)),
                     "drop": _safely(_drop)}}


def act(config, state, body):
    """One button: {"target": "here", "part": "voice", "action": start|stop|restart}."""
    target, part, action = body.get("target") or "here", body.get("part"), body.get("action")
    if target != "here" or action not in HERE.get(part, ()):
        raise ValueError(f"{part} can't {action} here.")
    if action in ("stop", "restart"):
        daemon.stop(state)
    result = daemon.start(state) if action != "stop" else {"stopped": True}
    return {"ok": True, "result": result, "voice": _voice(state)}
