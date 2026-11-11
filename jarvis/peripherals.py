"""Devices view: connect the computers, and share mouse, keyboard and clipboard between them.

Works the same from whichever PC is the main or a node. The link comes from
cluster_setup.doctor (on the main: every node; on a node: its own link to the main).
Each computer reports and switches its own Lan Mouse and clipboard half through the
`lan_mouse` and `clipboard` desktop actions, so nothing here assumes an OS.

    overview(config, path, state) -> {"here", "role", "ok", "checks", "devices": [...]}
    act(config, path, state, body) -> what the chosen action returned
"""

import json
from concurrent.futures import ThreadPoolExecutor

WHATS = {"lan_mouse": ("start", "stop"),
         "clipboard": ("start", "stop", "autostart-on", "autostart-off")}


def _ask(remote, device, action, arg):
    """(report dict or None, error text or None) from one desktop action."""
    try:
        ok, text = remote.call(device, action, arg)
    except Exception as exc:  # one silent computer must not blank the whole view
        return None, f"{type(exc).__name__}: {exc}"
    if not ok:
        return None, text or "no answer"
    try:
        report = json.loads(text)
    except ValueError:
        return None, "answered with something other than a report"
    return (report, None) if isinstance(report, dict) else (None, "answered with something other than a report")


def _online(checks):
    """{device: True/False} from the main's doctor checks ("dell: online")."""
    return {c["check"].rsplit(": ", 1)[0]: c["ok"] for c in checks if c["check"].endswith(": online")}


def _sharing(remote, device):
    lan, lan_error = _ask(remote, device, "lan_mouse", "status")
    clip, clip_error = _ask(remote, device, "clipboard", "status")
    config = (lan or {}).pop("config", None) or {}
    lan = lan and {**lan, "peers": [{"hostname": c.get("hostname"), "position": c.get("position")}
                                    for c in config.get("clients") or []]}
    return {"lan_mouse": lan, "lan_mouse_error": lan_error, "clipboard": clip, "clipboard_error": clip_error}


def overview(config, path, state, remote=None):
    from .cluster_setup import doctor
    from .remote import Remote
    remote = remote or Remote(config, state)
    link = doctor(config, path, state)
    here, online = remote.here(), _online(link["checks"])
    devices = config.get("devices") or {}
    main = (config.get("cluster") or {}).get("main_device")
    entries = []
    for key, device in devices.items():
        role = "main" if key == main else "node" if main else None
        reachable = key == here or online.get(key, link["ok"] if link.get("role") == "node" else True)
        entries.append({"id": key, "label": device.get("label") or key, "here": key == here,
                        "role": role, "online": True if key == here else online.get(key),
                        "reachable": reachable})
    # Ask every reachable computer at once; an offline one is skipped, not waited on.
    with ThreadPoolExecutor(max_workers=max(1, len(entries))) as pool:
        jobs = {e["id"]: pool.submit(_sharing, remote, e["id"]) for e in entries if e["reachable"]}
        for entry in entries:
            if entry["id"] in jobs:
                entry.update(jobs[entry["id"]].result())
            else:
                entry.update(lan_mouse=None, clipboard=None,
                             lan_mouse_error="offline", clipboard_error="offline")
    from .sharedinput import sharing_availability, with_switch
    return {"here": here, "main": main, "role": link.get("role"), "ok": link["ok"],
            "checks": link["checks"], "devices": entries,
            "sharing": with_switch(sharing_availability(state), config)}


def act(config, path, state, body, remote=None):
    """One button in the view. body: {"do": ...} with
    lan_mouse / clipboard: {"device", "action"}; pair: {"device", "position"};
    invite: {"device"} (main only); join: {"code"} (on a node)."""
    from .remote import Remote
    remote = remote or Remote(config, state)
    do = body.get("do")
    if do in WHATS:
        action = body.get("action")
        if action not in WHATS[do]:
            raise ValueError(f"{do} takes {', '.join(WHATS[do])}.")
        if do == "lan_mouse" and action == "start":
            from .sharedinput import start_blocked
            blocked = start_blocked(state, config)
            if blocked:
                raise RuntimeError(blocked)
        report, error = _ask(remote, body.get("device") or None, do, action)
        if error:
            raise RuntimeError(error)
        return report
    if do == "pair":
        from .sharedinput import pair, start_blocked
        blocked = start_blocked(state, config)
        if blocked:  # pairing restarts a running Lan Mouse; don't do that while apart
            raise RuntimeError(blocked)
        return pair(config, state, str(body.get("device") or ""), str(body.get("position") or ""), remote)
    if do == "invite":
        from .cluster_setup import invite
        report = invite(config, path, state, str(body.get("device") or ""))
        return {k: report[k] for k in ("invited", "addresses", "expires", "code", "next") if k in report}
    if do == "join":
        from .cluster_setup import PREFIX, decode, join
        code = str(body.get("code") or "").strip()
        if not code.startswith(PREFIX):  # never a path: decode() would read a file
            raise ValueError("Paste the whole invite code (it starts with jarvis-invite-1:).")
        return join(config, path, state, decode(code))
    raise ValueError("Unknown action.")
