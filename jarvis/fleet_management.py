"""Fleet management: guided onboarding of nodes (PCs, headless boxes, cloud servers) and a
read-only picture of the fleet, vault archive and GitHub link, for the Console and (a safe
subset) the paired phone.

Everything state-changing wraps the machinery that already exists and adds no new power:
  invite   -> cluster_setup.invite  (one restricted SSH key + one hub credential, one code)
  revoke   -> cluster_setup.revoke
  caps     -> Broker.set_capabilities (never rotates a credential)
Reads never call those, never spawn a shell and never touch the network. The one subprocess
is `git status`/`rev-parse` with fixed arguments and a timeout, on the configured vault only.

Rules (docs/design/mobile-continuity/FLEET-API.md):
  * The invite code is a password. It is returned once, in the explicit owner POST reply
    (the Console adds Cache-Control: no-store); it is never stored, logged, published on the
    bus or shown by inventory, preview or the phone.
  * A live node's credential is never rotated by accident: inviting a node that already
    joined is refused (revoke first); a still-unused invite is replaced only when asked.
  * Lost reply: the same request_id never issues a second code, including after a restart.
    The receipt file state/cluster-invite-receipts.json (request id, node, state, digest; no
    code and no key) is the only ledger: there is no in-memory copy and no second file.
    Every read-modify-write takes the fleet lock (config_mutation on fleet-manage.lock) and
    merges only that caller's delta onto the file as it is after the change, so a stale full
    snapshot cannot drop another writer's fence. Replacing an invite needs a new request_id.
    pending and uncertain receipts are not a way to rotate a credential that is still there,
    and an uncertain receipt blocks every new request for that node id until the node is
    revoked (the explicit reconciliation). The same file is the admission check for
    `jarvis cluster invite` and `jarvis cluster enroll`. Enroll checks again under that lock
    after the signing key and any device delta, and before it rotates a credential.
    `jarvis cluster revoke` and the console both clear that node's receipts only after
    Broker.revoke returns; if that revoke raises, the fence stays. A different node id is
    not blocked: this is the configured id, not a physical machine, and one uncertain
    receipt is not a global stop.
  * Every managed config write goes through jarvis/config_lock.py: the cross-process lock
    `<state>/fleet-manage.lock` (shared with Settings, `jarvis cluster configure|main|invite|enroll|join`,
    init and leave). The latest file is re-read under it and only the caller's keys are applied.
    Hand edits are not fenced.
  * MAIN is never an enroll/revoke/capability target and a cloud node is never MAIN eligible.
    Main-transfer preview is read-only. POST runs set_main only with an explicit config file
    and a bound safe handoff; otherwise it stays blocked and does not guess a config path.
"""

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cluster_store import CAPABILITIES, NODE_DEFAULT, ONLINE
from .topology import DEVICE_ID, identity

PROFILES = {
    "pc": {"label": "PC", "default_caps": sorted(NODE_DEFAULT), "display": True,
           "main_candidate": "later",
           "about": "A laptop or desktop you use. Gets screens, apps, files and a hardware report by default."},
    "headless": {"label": "Headless computer", "default_caps": ["inventory"], "display": False,
                 "main_candidate": "later",
                 "about": "A computer with no screen of its own (home server, mini PC). Inventory only until you grant more."},
    "cloud": {"label": "Cloud server", "default_caps": ["inventory"], "display": False,
              "main_candidate": "never",
              "about": "A rented server (for example Oracle Cloud). Inventory only by default, without personal "
                       "state or MAIN eligibility. Enrollment does not install a gateway service."},
}
OSES = ("linux", "macos", "windows")

# What each capability lets the main do on a node, in plain words, with its risk.
CAP_INFO = {
    "inventory": ("Hardware and status report", "low", "Lets the main read CPU, memory, disk and version."),
    "files": ("Files and clipboard", "medium", "Lets the main send and fetch files and share the clipboard."),
    "desktop": ("Screens, apps and mouse", "high", "Lets the main open apps, see the screen and use Lan Mouse."),
    "grokbot": ("Grok Bot", "medium", "Lets the main run Grok Bot browser tasks there."),
    "console": ("Use the console", "high", "Lets that node's console read the main's overview and approve requests."),
    "ask": ("Ask JARVIS", "medium", "Lets that node send requests to the main's brain (spends subscription quota)."),
    "camera": ("Webcam view", "high", "Lets the main look through its webcam, only after `jarvis camera allow on` on that node."),
    "locate": ("Find where it is", "high", "Lets the main ask for the node's location."),
}
assert set(CAP_INFO) == set(CAPABILITIES)

ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
LABEL_RE = re.compile(r"[^\x00-\x1f\x7f]{1,60}\Z")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
REQUEST_KEYS = {
    "preview": {"node", "profile", "os", "addresses", "capabilities", "label"},
    "invite": {"request_id", "node", "profile", "os", "label", "addresses", "capabilities",
               "acknowledge", "replace_pending"},
    "revoke": {"node", "confirm"},
    "capabilities": {"node", "capabilities", "acknowledge"},
    "main_transfer": {"target"},
}
MAIN_TRANSFER_MISSING = (
    ("authority_store", "A durable authority record (who is MAIN, with an epoch) that survives restarts. "
                        "Today the main is only cluster.main_device in a config file."),
    ("authority_lease", "A single compare-and-swap lease so two machines can never both act as MAIN."),
    ("fencing", "The old main must be fenced: stop writing, drain, and acknowledge before the new one starts."),
    ("target_ack", "The target must prove it has the data and acknowledge the new epoch."),
    ("executor_guards", "Every effect (jobs, memory, vault, queue) must check the epoch before it runs."),
    ("fresh_owner_proof", "A fresh passkey or equivalent owner proof for this one action; phone pairing is not fleet admin."),
    ("checkpoint_barrier", "Plans, runs and knowledge snapshots must be checkpointed so nothing is lost mid-move."),
)
MAIN_TRANSFER_PHASES = ("Check target readiness", "Fence the current main", "Copy and verify state",
                        "Switch the authority epoch", "Start the new main", "Confirm and re-pair nodes")

_LOCK = threading.RLock()
_GIT_CACHE = {}


class FleetError(ValueError):
    """A refusal with a stable `code` the frontend can switch on. Subclasses ValueError so
    anything that does not know it still answers 400 with the message."""

    def __init__(self, code, message, status=409, step=None, retryable=False, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, "code": code, "retryable": retryable,
                        **({"step": step} if step else {}), **extra}


class AdmissionBlocked(ValueError):
    """This node id has an uncertain receipt. Revoke it before inviting or enrolling again.

    The fence is that configured id in cluster-invite-receipts.json. It is not a physical
    machine and it does not stop a different id."""

    def __init__(self, node):
        self.node = node
        super().__init__(
            f"An earlier invite for {node} stopped after a credential change and its "
            f"cleanup could not be verified. Revoke {node} to reconcile it, then invite again.")


# --- small helpers ---------------------------------------------------------------------

def _clean(body, action):
    if not isinstance(body, dict):
        raise FleetError("bad_body", "Request body must be a JSON object.", 400)
    extra = sorted(set(body) - REQUEST_KEYS[action])
    if extra:
        raise FleetError("unknown_fields", "Unknown fields: " + ", ".join(str(k)[:40] for k in extra[:5]), 400)
    return body


def _node_id(value, config=None, *, allow_main=False):
    if not isinstance(value, str) or not DEVICE_ID.fullmatch(value):
        raise FleetError("bad_node", "Node ids use letters, digits, - and _ (up to 64 characters).", 400)
    try:
        main = (config or {}).get("cluster", {}).get("main_device")
    except AttributeError:
        main = None
    if value == main and not allow_main:
        raise FleetError("main_not_allowed", "The main can't be enrolled, revoked or changed here.", 409)
    return value


def _profile(value, default="pc"):
    value = default if value is None else value
    if value not in PROFILES:
        raise FleetError("bad_profile", "profile must be pc, headless or cloud.", 400)
    return value


def _os_name(value, profile):
    if value is None:
        return "linux" if profile in ("cloud", "headless") else None
    if value not in OSES:
        raise FleetError("bad_os", "os must be linux, macos or windows.", 400)
    return value


def _caps(value, profile, acknowledge):
    """(sorted caps, extra beyond the profile's default). Anything beyond the default must
    be named in `acknowledge`: nothing sensitive is granted by default or by accident."""
    default = set(PROFILES[profile]["default_caps"])
    if value is None:
        return sorted(default), []
    if not isinstance(value, list) or not value or not all(isinstance(c, str) for c in value):
        raise FleetError("bad_capabilities", "capabilities must be a non-empty list of names.", 400)
    caps = set(value)
    unknown = caps - CAPABILITIES
    if unknown:
        raise FleetError("bad_capabilities", "Unknown capabilities: " + ", ".join(sorted(x[:30] for x in unknown)), 400)
    extra = sorted(caps - default)
    if acknowledge is not None:
        if not isinstance(acknowledge, list) or not all(isinstance(c, str) for c in acknowledge):
            raise FleetError("bad_acknowledge", "acknowledge must be a list of capability names.", 400)
    missing = sorted(set(extra) - set(acknowledge or ()))
    if missing:
        raise FleetError("capability_ack_required",
                         "These go beyond the " + profile + " default and need explicit approval: " + ", ".join(missing),
                         400, step="capabilities", needs_ack=missing)
    return sorted(caps), extra


def _addresses(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise FleetError("bad_address", "addresses is a list of up to 4 hostnames or IPs.", 400)
    out = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if not HOST_RE.fullmatch(text) or text.startswith("127.") or text == "localhost":
            raise FleetError("bad_address", "Addresses must be a hostname or IP, not loopback.", 400)
        if text not in out:
            out.append(text)
    return out


def _label(value, node):
    if value is None:
        return node
    if not isinstance(value, str) or not LABEL_RE.fullmatch(value.strip()):
        raise FleetError("bad_label", "label is up to 60 plain characters.", 400)
    return value.strip()


def _cap_list(ids, default=()):
    return [{"id": c, "title": CAP_INFO[c][0], "risk": CAP_INFO[c][1], "explain": CAP_INFO[c][2],
             "default": c in default} for c in ids if c in CAP_INFO]


def _db_rows(state):
    """Read-only look at the broker's table: no token hashes, no creation of the file."""
    path = Path(state) / "cluster.sqlite3"
    if not path.is_file():
        return {}, {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return {}, {}
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id,capabilities,seen,info,invite_expires FROM nodes ORDER BY id").fetchall()
        jobs = {}
        try:
            for node, count in db.execute("SELECT node, COUNT(*) FROM jobs WHERE state IN ('pending','running') "
                                          "GROUP BY node"):
                jobs[node] = count
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return {}, {}
    finally:
        db.close()
    out = {}
    for r in rows:
        try:
            info = json.loads(r["info"] or "{}")
            caps = json.loads(r["capabilities"] or "[]")
        except ValueError:
            info, caps = {}, []
        out[r["id"]] = {"capabilities": [c for c in caps if c in CAPABILITIES], "seen": r["seen"],
                        "invite_expires": r["invite_expires"], "info": info if isinstance(info, dict) else {}}
    return out, jobs


def link_state(row, now=None):
    """not_enrolled | invite_pending | invite_expired | waiting_for_first_contact | online | offline"""
    now = now or time.time()
    if row is None:
        return "not_enrolled"
    expires, seen = row.get("invite_expires"), row.get("seen")
    if expires is not None and seen is None:
        return "invite_pending" if expires >= now else "invite_expired"
    if not seen:
        return "waiting_for_first_contact"
    return "online" if now - seen < ONLINE else "offline"


def _identity(config):
    try:
        return identity(config)
    except (ValueError, AttributeError):
        return None, (config.get("cluster") or {}).get("main_device") if isinstance(config, dict) else None


def _served_code(state):
    try:
        from .release import served_code
        return served_code(state)
    except Exception:
        return None


def _profile_of(device):
    value = (device or {}).get("profile")
    return value if value in PROFILES else "pc"


# --- inventory -------------------------------------------------------------------------

def _explicit_config_file(config_path):
    """An absolute path to a file the caller already named. Relative paths are refused
    so a test cwd cannot select the checkout's jarvis.config.json."""
    if config_path is None or isinstance(config_path, bool) or not isinstance(config_path, (str, Path)):
        return None
    path = Path(config_path)
    if not path.is_absolute():
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _handoff_configured(state):
    from .cluster_cli import configured_handoff
    try:
        return configured_handoff(state) is not None
    except Exception:
        return False


def _actions(owner, local_is_main, state=None, config_path=None):
    """Every action the frontend may show, as supported | guide | blocked | unsupported.

    main_transfer is supported only for the owner on the local main, and only when this
    process has a bound handoff and the caller passed an explicit config file. Inventory
    forwards that path when it has one."""
    def entry(id_, support, why, **extra):
        return {"id": id_, "support": support, "why": why, **extra}
    if not owner:
        why = "Needs the future fleet-admin step (fresh owner proof). Use the console on the main PC."
        return [entry(i, "blocked", why) for i in ("invite", "revoke", "capabilities", "main_transfer")] + [
            entry("setup_guide", "guide", "Read-only instructions for adding a PC or server.")]
    gate = None if local_is_main else "Open the console on the main PC to do this."
    ready = bool(gate is None and state is not None and _explicit_config_file(config_path) is not None
                 and _handoff_configured(state))
    return [
        entry("preview", "supported", "Shows exactly what an invite would change. Changes nothing.",
              method="POST", path="/api/fleet/preview"),
        entry("invite", "supported" if gate is None else "blocked", gate or "Creates a one-time invite code (24 h).",
              method="POST", path="/api/fleet/invite", owner_only=True, secret_in_reply=True),
        entry("revoke", "supported" if gate is None else "blocked", gate or "Cuts a node off: credential and SSH key.",
              method="POST", path="/api/fleet/revoke", owner_only=True, needs_confirm=True),
        entry("capabilities", "supported" if gate is None else "blocked",
              gate or "Changes what an enrolled node may do. The credential stays the same.",
              method="POST", path="/api/fleet/capabilities", owner_only=True),
        entry("main_transfer_preview", "supported", "Shows readiness. Changes nothing.",
              method="POST", path="/api/fleet/main-transfer/preview"),
        entry("main_transfer", "supported" if ready else "blocked",
              "Moves MAIN through the configured safe handoff." if ready else
              (gate or "Safe main handoff is not configured, so nothing can be moved."),
              method="POST", path="/api/fleet/main-transfer", owner_only=True),
        entry("cloud_provisioning", "unsupported", "JARVIS does not create cloud servers or install an OS. "
              "Create the server yourself, then add it as a node."),
        entry("setup_guide", "guide", "Step-by-step instructions to run on the new machine."),
    ]


def inventory(config, state, audience="owner", now=None, config_path=None):
    """The fleet as the frontend shows it. audience "phone" drops addresses, hostnames,
    code versions and write actions. No subprocess, no network, no writes."""
    now = now or time.time()
    owner = audience == "owner"
    local, main = _identity(config)
    enabled = bool((config.get("cluster") or {}).get("enabled"))
    rows, jobs = _db_rows(state)
    from .connections import busy_nodes, presence  # one liveness word for every surface
    busy = busy_nodes(state, now)
    code = _served_code(state) if owner else None
    devices = config.get("devices") or {}
    handoff_configured = _explicit_config_file(config_path) is not None and _handoff_configured(state)
    out = []
    for node_id, device in devices.items():
        if not isinstance(device, dict):
            continue
        profile = _profile_of(device)
        spec = PROFILES[profile]
        is_main = node_id == main
        row = rows.get(node_id)
        info = (row or {}).get("info") or {}
        if is_main:
            eligible_why = "This is the current main."
        elif profile == "cloud":
            eligible_why = "Inventory only by default and never becomes the main."
        elif handoff_configured:
            eligible_why = ("Safe handoff is configured. That is not operational proof; "
                            "the coordinator must still verify readiness, lease expiry and the safety barrier.")
        else:
            eligible_why = "Safe handoff is not configured, so this machine cannot become MAIN."
        item = {"id": node_id, "label": str(device.get("label") or node_id), "role": "main" if is_main else "node",
                "profile": profile, "profile_label": spec["label"],
                "platform": info.get("platform") or device.get("platform"),
                "main_eligible": False if (is_main or profile == "cloud") else "blocked",
                "main_eligible_why": eligible_why}
        if is_main:
            item.update(status="main", online=True, seen=None, capabilities=[])
        else:
            state_name = link_state(row, now)
            caps = (row or {}).get("capabilities") or []
            # Only an enrolled node that checked in before can be "busy" (a job still inside its deadline,
            # no check-in for ONLINE seconds). `online` stays proof; the phone keeps the proved word in
            # `status` and gets the same projection in `presence`.
            shown = state_name
            if state_name in ("online", "offline"):
                live = presence((row or {}).get("seen"), node_id in busy, now)
                shown = live if owner else state_name
                item["presence"] = live
            item.update(status=shown, online=state_name == "online", seen=(row or {}).get("seen"),
                        capabilities=_cap_list(caps, spec["default_caps"]) if owner else [c for c in caps if c != "locate"],
                        step=_step_for(state_name))
            if owner:
                item.update(invite_expires=(row or {}).get("invite_expires") if state_name in ("invite_pending", "invite_expired") else None,
                            code_ok=(info.get("code") == code) if info.get("code") and code else None,
                            hostname=info.get("hostname") or device.get("hostname"),
                            active_jobs=jobs.get(node_id, 0), address=device.get("lan_ip"))
        out.append(item)
    for node_id, row in rows.items():  # enrolled on the hub but missing from the config
        if node_id not in devices:
            out.append({"id": node_id, "label": node_id, "role": "node", "profile": "pc", "status": "unconfigured",
                        "online": False, "seen": row.get("seen"), "capabilities": [], "main_eligible": "blocked",
                        "step": "Add this device to the config or revoke it."})
    out.sort(key=lambda d: (d["role"] != "main", d["id"]))
    return {"audience": audience, "can_write": bool(owner and local and local == main and enabled),
            "cluster": {"enabled": enabled, "local": local if owner else None, "main": main,
                        "local_is_main": bool(local and local == main)},
            "profiles": [{"id": k, "label": v["label"], "about": v["about"], "default_capabilities": v["default_caps"],
                          "main_candidate": v["main_candidate"]} for k, v in PROFILES.items()],
            "capabilities": [{"id": k, "title": v[0], "risk": v[1], "explain": v[2]}
                             for k, v in CAP_INFO.items() if owner or k != "locate"],
            "devices": out,
            "actions": _actions(owner, bool(local and local == main and enabled), state, config_path),
            "guide": onboarding_guide(),
            "checked_at": now}


def _step_for(state_name):
    return {"not_enrolled": "Create an invite for this node.",
            "invite_pending": "Run the join command on the node before the invite expires.",
            "invite_expired": "The invite expired unused. Create a new one (replace_pending).",
            "waiting_for_first_contact": "The node has the credential but has not connected yet. Check its network.",
            "offline": "The node was seen before but is silent now. Check it is on and online.",
            "online": "Connected."}.get(state_name, "")


def onboarding_guide():
    """Read-only, safe for the phone: the shape of adding a machine, no secrets, no node ids."""
    return [
        {"id": "choose", "title": "Choose a profile", "who": "you",
         "body": "PC (has a screen), headless computer, or cloud server. Cloud servers get inventory only."},
        {"id": "preview", "title": "Preview", "who": "console on the main",
         "body": "Shows what the invite changes: one device entry, one restricted SSH key, one 24-hour credential."},
        {"id": "invite", "title": "Create the invite", "who": "console on the main",
         "body": "You get one code. It is a password for that node: copy it once, never paste it into chat."},
        {"id": "join", "title": "Join on the new machine", "who": "you, on the node",
         "body": "Run the join command from the preview in that machine's JARVIS folder."},
        {"id": "verify", "title": "Check the link", "who": "console on the main",
         "body": "The node turns Online. Then review its capabilities; sensitive ones stay off until you grant them."},
    ]


# --- preview ---------------------------------------------------------------------------

def _join_commands(os_name):
    if os_name == "windows":
        return [{"label": "Join (PowerShell, in the JARVIS folder)",
                 "command": '.venv\\Scripts\\python.exe -m jarvis cluster join "<INVITE CODE>"'},
                {"label": "Check", "command": ".venv\\Scripts\\python.exe -m jarvis cluster doctor"}]
    return [{"label": "Join (shell, in the JARVIS folder)", "command": '.venv/bin/python -m jarvis cluster join "<INVITE CODE>"'},
            {"label": "Check", "command": ".venv/bin/python -m jarvis cluster doctor"}]


def _check(id_, ok, why):
    return {"id": id_, "state": "ok" if ok is True else "problem" if ok is False else "unknown", "why": why}


def main_readiness(config, probe=True):
    """Cheap checks on the main for onboarding. The only I/O is a loopback TCP probe."""
    from . import cluster_setup
    checks = []
    local, main = _identity(config)
    cluster = config.get("cluster") or {}
    checks.append(_check("cluster", bool(cluster.get("enabled")), "Cluster is on." if cluster.get("enabled")
                         else 'Turn on "cluster.enabled" first.'))
    checks.append(_check("is_main", bool(local and local == main), "This is the main PC." if local and local == main
                         else "Invites are created on the main PC."))
    forward = cluster.get("tunnel") == "forward"
    checks.append(_check("tunnel_forward", forward, "Nodes dial the main (forward tunnel)." if forward
                         else 'Invites need "cluster.tunnel": "forward".'))
    if probe:
        up = cluster_setup._sshd_up()
        checks.append(_check("sshd", up, "This PC's SSH server answers on port 22." if up else
                             "This PC's SSH server isn't answering on port 22. " +
                             cluster_setup.SSHD_FIX.get(cluster_setup._os(), "")))
    else:
        checks.append(_check("sshd", None, "Checked when you preview or invite."))
    checks.append(_check("addresses", None, "The main's addresses are collected when the invite is made."))
    return checks


def preview_setup(config, state, body, probe=True):
    """What an invite WOULD do. No state change; no invite() call; no secrets."""
    body = _clean(body, "preview")
    node = _node_id(body.get("node"), config)
    profile = _profile(body.get("profile"))
    os_name = _os_name(body.get("os"), profile)
    caps, extra = _caps(body.get("capabilities"), profile, body.get("capabilities") if body.get("capabilities") else None)
    addresses = _addresses(body.get("addresses"))
    label = _label(body.get("label"), node)
    rows, _ = _db_rows(state)
    row = rows.get(node)
    link = link_state(row)
    device = (config.get("devices") or {}).get(node)
    problems = []
    if device is not None and _profile_of(device) != profile and "profile" in device:
        problems.append({"code": "profile_mismatch", "why": f"{node} is already a {_profile_of(device)} in the config."})
    if link in ("online", "offline", "waiting_for_first_contact"):
        problems.append({"code": "node_enrolled", "why": f"{node} already has a live credential. Revoke it first; "
                         "inviting again would silently cut it off."})
    elif link == "invite_pending":
        problems.append({"code": "invite_pending", "why": f"{node} has an unused invite. Replace it only on purpose "
                         "(replace_pending)."})
    checks = main_readiness(config, probe=probe)
    ready = all(c["state"] != "problem" for c in checks) and not any(p["code"] != "invite_pending" for p in problems)
    steps = [
        {"id": "ready", "title": "Main is ready", "kind": "auto",
         "state": "done" if all(c["state"] != "problem" for c in checks) else "blocked"},
        {"id": "invite", "title": "Create the invite", "kind": "action", "action": "invite",
         "state": ("done" if link in ("online", "offline", "waiting_for_first_contact") else
                   "pending" if link == "invite_pending" else "todo")},
        {"id": "join", "title": "Run the join command on the node", "kind": "guide",
         "state": "done" if link in ("online", "offline") else "todo"},
        {"id": "contact", "title": "Node connects", "kind": "auto",
         "state": "done" if link == "online" else "waiting" if link in ("waiting_for_first_contact", "invite_pending") else "todo"},
        {"id": "capabilities", "title": "Review capabilities", "kind": "action", "action": "capabilities",
         "state": "todo"},
    ]
    limits = ["JARVIS does not create servers or install an operating system."] if profile == "cloud" else []
    return {"node": node, "label": label, "profile": profile, "os": os_name, "ready": ready, "problems": problems,
            "checks": checks, "steps": steps, "link": link,
            "capabilities": _cap_list(caps, PROFILES[profile]["default_caps"]),
            "needs_acknowledge": extra,
            "would_change": {
                "config": f"Adds devices.{node} (profile {profile}) after a backup" if device is None
                          or "profile" not in device else "Nothing in the config",
                "authorized_keys": f"Adds one restricted key for {node}: forwards to the hub only, no shell",
                "credential": "Creates one hub credential valid 24 hours or until first use",
                "network": "Nothing opens; no new port"},
            "addresses": addresses,
            "node_commands": _join_commands(os_name) if os_name else
                             [{"label": "Pick the node's OS to see commands", "command": None}],
            "limits": limits, "mutated": False}


# --- mutations -------------------------------------------------------------------------

def _config_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FleetError("config_unreadable", "The config file can't be read; nothing was changed.", 500) from exc
    if not isinstance(data, dict):
        raise FleetError("config_unreadable", "The config file isn't a JSON object; nothing was changed.", 500)
    return data


def _translate(exc):
    from . import config_lock
    if isinstance(exc, config_lock.ConfigBusy):
        return FleetError("busy", "Another configuration or fleet change is running. Try again in a few "
                          "seconds.", 409, retryable=True)
    if isinstance(exc, config_lock.ConfigDrift):
        return FleetError("config_changed", str(exc), 409, retryable=True)
    return FleetError("config_unreadable", str(exc), 500)


def patch_config_file(state, path, change, *, guard=None, backup=None, create=False):
    """config_lock.patch_config with FleetError codes (busy, config_changed, config_unreadable)."""
    from . import config_lock
    try:
        return config_lock.patch_config(state, path, change, guard=guard, backup=backup, create=create)
    except (config_lock.ConfigBusy, config_lock.ConfigDrift, config_lock.ConfigUnreadable) as exc:
        raise _translate(exc) from exc


def _write_device(config, path, node, entry, state=None, guard=None):
    """Add/extend devices.<node> in the config file only: the change is applied to the file as
    it is on disk under the managed config lock (config_lock), every other key is kept, and the
    previous bytes are kept as `<file>.pre-fleet-onboarding-<stamp>-<ns>-<rand>`.
    guard(doc) runs on the latest file first (role and tunnel checks). Returns the device as
    stored, and True when the file changed."""
    path = Path(path)
    state = Path(state) if state is not None else path.parent / ".jarvis"
    stored = {}

    def change(doc):
        devices = doc.get("devices")
        if not isinstance(devices, dict):
            devices = doc["devices"] = {}
        current = devices.get(node)
        merged = {**(current if isinstance(current, dict) else {}), **{k: v for k, v in entry.items()
                  if k == "profile" or not (isinstance(current, dict) and k in current)}}
        stored.update(merged)
        if current != merged:
            devices[node] = merged

    _doc, changed = patch_config_file(state, path, change, guard=guard, backup="fleet-onboarding")
    return dict(stored), changed


@contextlib.contextmanager
def _mutation_lock(state):
    """The managed config lock (config_lock.config_mutation, file <state>/fleet-manage.lock),
    re-entrant per thread. Every managed config/role writer takes it: this module, Settings,
    `jarvis cluster configure|main|init|invite|enroll|join|leave`. A local file lock is not a
    lease and does not fence another machine or an editor. Same-thread re-entry is how an
    invite calls cluster_setup while it already holds the lock; a settings save on that
    thread is applied as its own keys, and the invite re-reads the file before it returns a code."""
    from . import config_lock
    try:
        cm = config_lock.config_mutation(state)
        cm.__enter__()
    except config_lock.ConfigBusy as exc:
        raise _translate(exc) from exc
    except OSError as exc:
        raise FleetError("state_unwritable", "The state folder can't be written.", 500) from exc
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _require_main(config):
    local, main = _identity(config)
    if not (config.get("cluster") or {}).get("enabled"):
        raise FleetError("cluster_off", "The cluster is off.", 409, step="ready")
    if not local or local != main:
        raise FleetError("not_main", "Do this on the main PC's console.", 409, step="ready")
    return main


_RECEIPT_NAME = "cluster-invite-receipts.json"  # matches the archive rule cluster-* (excluded-credential)
_RECEIPT_FIELDS = {"node", "state", "digest", "at"}
_SECRET_MARKS = ("PRIVATE KEY", "BEGIN OPENSSH", "jarvis-invite-1:")


def _receipt_path(state):
    return Path(state) / _RECEIPT_NAME


def _public_failure(exc, fallback="The invite could not be created."):
    text = str(exc or "").strip()
    if not text or any(mark in text for mark in _SECRET_MARKS):
        return fallback
    return text[:300]


def _digest(request_id, node):
    return hashlib.sha256(f"{request_id}\n{node}\nissued".encode()).hexdigest()


def _load_receipts(state):
    """The on-disk issuance ledger. A missing file is empty. A file that cannot be read,
    or that is not the expected shape, is a hard stop: treating it as empty would issue again."""
    path = _receipt_path(state)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise FleetError("invite_failed",
                         "The invite receipt file can't be read, so this request was not retried.",
                         409, step="invite", retryable=False) from exc
    if any(mark in raw for mark in _SECRET_MARKS):
        raise FleetError("invite_failed",
                         "The invite receipt file is not usable, so this request was not retried.",
                         409, step="invite", retryable=False)
    receipts = data.get("receipts") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(receipts, dict):
        raise FleetError("invite_failed",
                         "The invite receipt file is damaged, so this request was not retried.",
                         409, step="invite", retryable=False)
    for request_id, item in receipts.items():
        if (not isinstance(request_id, str) or not isinstance(item, dict)
                or not set(item) <= _RECEIPT_FIELDS or item.get("state") not in ("pending", "issued", "uncertain")
                or not isinstance(item.get("node"), str)
                or (item["state"] == "issued" and not isinstance(item.get("digest"), str))):
            raise FleetError("invite_failed",
                             "The invite receipt file is damaged, so this request was not retried.",
                             409, step="invite", retryable=False)
    return receipts


def _store_receipts(state, receipts):
    """Atomic, fsynced, mode 0600. The payload is request id, node, state and a digest. No code, no key."""
    clean = {}
    for request_id, item in receipts.items():
        if not isinstance(request_id, str) or not isinstance(item, dict):
            raise FleetError("invite_failed", "Refusing to store an invite receipt that is not a record.",
                             409, step="invite", retryable=False)
        kept = {"node": item["node"], "state": item["state"], "at": item.get("at")}
        if item["state"] == "issued":
            kept["digest"] = item["digest"]
        clean[request_id] = kept
    blob = json.dumps({"version": 1, "receipts": clean}, indent=2, sort_keys=True)
    if any(mark in blob for mark in _SECRET_MARKS):
        raise FleetError("invite_failed", "Refusing to store an invite receipt that contains a secret.",
                         409, step="invite", retryable=False)
    from .fsio import atomic_write
    path = _receipt_path(state)
    atomic_write(path, blob, mode=0o600, fsync=True)
    try:  # the rename is durable once the directory entry is too; a failure here is not fatal
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError:
        pass


_MISSING = object()


def _merge_receipt_delta(latest, before, after):
    """Apply only the edits from `before` to `after` onto `latest`.

    A key the caller did not change keeps the value in `latest`, including a fence
    stored after the caller read. A key the caller removed is removed only when
    `latest` is still the value they saw. A third value on a key they changed is
    left as `latest`, so a stale pending or issued write cannot cover an uncertain row.
    """
    merged = copy.deepcopy(latest)
    for key in set(before) | set(after):
        old = before.get(key, _MISSING)
        new = after.get(key, _MISSING)
        if old == new:
            continue
        current = merged.get(key, _MISSING)
        if new is _MISSING:
            if current == old:
                merged.pop(key, None)
            continue
        if current is _MISSING or current == old or current == new:
            merged[key] = copy.deepcopy(new)
    return merged


def mutate_receipts(state, change):
    """Read-modify-write the one receipt file under the fleet lock.

    `change(receipts)` mutates a copy of the file as it was read under the lock.
    The file is read again after `change` returns. Only this caller's delta is
    written, so a same-thread re-entry that already stored a fence is kept.
    The caller's earlier full snapshot is never what hits the disk.
    """
    from .config_lock import config_mutation
    with config_mutation(state):
        before = _load_receipts(state)
        after = copy.deepcopy(before)
        change(after)
        latest = _load_receipts(state)
        merged = _merge_receipt_delta(latest, before, after)
        if merged != latest:
            _store_receipts(state, merged)
        return merged


def _drop_receipt(state, request_id):
    def change(receipts):
        receipts.pop(request_id, None)
    mutate_receipts(state, change)


def _put_receipt(state, request_id, item):
    def change(receipts):
        receipts[request_id] = item
    mutate_receipts(state, change)


def clear_node_receipts(state, node):
    """Drop every receipt for this node id. Other ids stay. One ledger, no second file."""
    def change(receipts):
        for key in [k for k, item in receipts.items()
                    if isinstance(item, dict) and item.get("node") == node]:
            del receipts[key]
    mutate_receipts(state, change)


def uncertain_for(receipts, node):
    """True when this node id has an uncertain receipt. Other ids are ignored."""
    return any(isinstance(item, dict) and item.get("node") == node and item.get("state") == "uncertain"
               for item in receipts.values())


def assert_node_admitted(state, node):
    """Refuse issuance for this node id when an uncertain receipt is on the one ledger.

    The read takes the fleet lock, same as every receipt write. A different node id is
    admitted. Nothing here stops every invite on the machine."""
    from .config_lock import config_mutation
    with config_mutation(state):
        if uncertain_for(_load_receipts(state), node):
            raise AdmissionBlocked(node)


def note_uncertain_attempt(state, node):
    """Record one uncertain receipt for this node id when cleanup could not be verified.

    Same file as issue_invite, under the fleet lock. No second ledger, no code, no key.
    A receipt that is already uncertain for the id is left as it is. A fence another
    writer stored during this call is not replaced."""
    def change(receipts):
        if uncertain_for(receipts, node):
            return
        receipts["cli-" + hashlib.sha256(f"{node}\n{time.time_ns()}".encode()).hexdigest()[:24]] = {
            "node": node, "state": "uncertain", "at": time.time()}
    mutate_receipts(state, change)


def _node_has_row(state, node):
    rows, _jobs = _db_rows(state)
    return node in rows


def issue_invite(config, path, state, body):
    """Owner-only. Returns the invite (with its secret `code`) once; see the module docstring."""
    from . import cluster_setup
    body = _clean(body, "invite")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
        raise FleetError("bad_request_id", "request_id is required: 8-64 letters, digits, - or _.", 400)
    node = _node_id(body.get("node"), config)
    profile = _profile(body.get("profile"))
    os_name = _os_name(body.get("os"), profile)
    caps, _extra = _caps(body.get("capabilities"), profile, body.get("acknowledge"))
    addresses = _addresses(body.get("addresses"))
    label = _label(body.get("label"), node)
    replace = body.get("replace_pending", False)
    if not isinstance(replace, bool):
        raise FleetError("bad_replace", "replace_pending must be true or false.", 400)
    main = _require_main(config)
    if node == main:
        raise FleetError("main_not_allowed", "The main can't be enrolled here.", 409)
    runtime_checks = main_readiness(config)
    lost = ("An invite for " + node + " was already made for this request. The code is shown only "
            "once and can't be recovered. Create a new invite with a new request_id and replace_pending.")
    with _LOCK, _mutation_lock(state):
        # A cached console config is not evidence that this PC is still the configured MAIN.
        config = _config_file(path)
        main = _require_main(config)
        if node == main:
            raise FleetError("main_not_allowed", "The main can't be enrolled here.", 409)
        receipts = _load_receipts(state)
        prior = receipts.get(request_id)
        if prior is not None and prior["node"] != node:
            raise FleetError("already_issued", "That request_id was used for another node.", 409,
                             step="invite", retryable=False)
        if prior is not None and prior["state"] == "issued":
            if prior.get("digest") != _digest(request_id, node):
                def mark(found, _request_id=request_id, _node=node):
                    item = found.get(_request_id)
                    if (isinstance(item, dict) and item.get("state") == "issued"
                            and item.get("digest") != _digest(_request_id, _node)):
                        item["state"] = "uncertain"
                        item.pop("digest", None)
                mutate_receipts(state, mark)
                raise FleetError("invite_failed",
                                 "This request's receipt does not match. It was not retried.",
                                 409, step="invite", retryable=False)
            # replace_pending does not mint a second code for an id that already issued one.
            raise FleetError("already_issued", lost, 409, step="invite", retryable=False)
        if prior is not None and prior["state"] == "uncertain":
            raise FleetError("invite_failed",
                             "This request stopped after a credential change and the cleanup could not be "
                             "verified. It was not atomic and it was not retried.",
                             409, step="invite", retryable=False)
        if prior is not None and prior["state"] == "pending" and _node_has_row(state, node):
            raise FleetError("already_issued", lost, 409, step="invite", retryable=False)
        if uncertain_for(receipts, node):
            # An attempt whose cleanup could not be verified may have left a credential or key
            # line. A new request_id is not a way around that: revoking the node reconciles it.
            raise FleetError("invite_failed",
                             f"An earlier invite for {node} stopped after a credential change and its "
                             f"cleanup could not be verified. Revoke {node} to reconcile it, then invite again.",
                             409, step="invite", retryable=False, recover="revoke")
        rows, _ = _db_rows(state)
        link = link_state(rows.get(node))
        if link in ("online", "offline", "waiting_for_first_contact"):
            raise FleetError("node_enrolled", f"{node} already has a live credential; inviting again would cut it "
                             "off. Revoke it first if you mean to re-pair it.", 409, step="invite")
        if link == "invite_pending" and not replace:
            raise FleetError("invite_pending", f"{node} has an unused invite that hasn't expired. Use "
                             "replace_pending and a new request_id to replace it (the old code stops working).",
                             409, step="invite")
        for check in runtime_checks + main_readiness(config):
            if check["state"] == "problem" and check["id"] in ("tunnel_forward", "sshd"):
                raise FleetError(check["id"], check["why"], 409, step="ready", retryable=check["id"] == "sshd")
        existing = (config.get("devices") or {}).get(node)
        if isinstance(existing, dict) and "profile" in existing and _profile_of(existing) != profile:
            raise FleetError("profile_mismatch", f"{node} is already a {_profile_of(existing)}.", 409)
        entry = {"label": label, "profile": profile}
        if os_name:
            entry["os"] = os_name
        wrote_pending = False
        try:
            _put_receipt(state, request_id, {"node": node, "state": "pending", "at": time.time()})
            wrote_pending = True
            def still_main(doc):  # runs on the file as it is on disk, under the config lock
                current = _require_main(doc)
                if node == current:
                    raise FleetError("main_not_allowed", "The main can't be enrolled here.", 409)
                for check in main_readiness(doc):
                    if check["state"] == "problem" and check["id"] in ("tunnel_forward", "sshd"):
                        raise FleetError(check["id"], check["why"], 409, step="ready",
                                         retryable=check["id"] == "sshd")

            stored, changed = _write_device(config, path, node, entry, state=state, guard=still_main)
            # Settings takes this same lock and writes only its own keys. This re-read catches
            # an editor. cluster_setup.invite checks the file again after authorize, still under
            # this lock, so a same-thread tunnel save cannot return a code.
            fresh = _config_file(path)
            main = _require_main(fresh)
            if node == main:
                raise FleetError("main_not_allowed", "The main can't be enrolled here.", 409)
            for check in main_readiness(fresh):
                if check["state"] == "problem" and check["id"] in ("tunnel_forward", "sshd"):
                    raise FleetError(check["id"], check["why"], 409, step="ready",
                                     retryable=check["id"] == "sshd")
            live = copy.deepcopy(fresh)
            live.setdefault("devices", {})[node] = stored
            try:
                report = cluster_setup.invite(live, path, state, node, extra=addresses, capabilities=caps)
            except cluster_setup.InviteIncomplete as exc:
                if not exc.retryable:
                    _put_receipt(state, request_id, {"node": node, "state": "uncertain", "at": time.time()})
                    wrote_pending = False
                raise FleetError("invite_failed", _public_failure(exc), 409, step="invite",
                                 retryable=bool(exc.retryable)) from exc
            except AdmissionBlocked as exc:
                raise FleetError("invite_failed", str(exc), 409, step="invite",
                                 retryable=False, recover="revoke") from exc
            except ValueError as exc:
                raise FleetError("invite_failed", _public_failure(exc), 409,
                                 step="invite", retryable=True) from exc
            except OSError as exc:
                raise FleetError("invite_failed", "A file the invite needed could not be written.", 500,
                                 step="invite", retryable=True) from exc
            except RuntimeError as exc:
                raise FleetError("invite_failed", _public_failure(exc), 409,
                                 step="invite", retryable=True) from exc
            issued = {"node": node, "state": "issued", "digest": _digest(request_id, node), "at": time.time()}
            try:
                _put_receipt(state, request_id, issued)
            except (OSError, FleetError) as exc:
                # The credential exists and the client has not been given the code. Leave the
                # pending receipt so the same id cannot rotate it.
                raise FleetError("invite_failed",
                                 "The invite was created but its receipt could not be saved. The code was "
                                 "not returned, and retrying this request_id would not be safe.",
                                 409, step="invite", retryable=False) from exc
            wrote_pending = False
        except FleetError as exc:
            # A pending receipt is dropped when nothing of this attempt is left: either no row
            # for the node, or a failure whose cleanup was verified (retryable). A receipt that
            # could not be finalised after issuing (retryable False) stays pending.
            if wrote_pending and (exc.payload.get("retryable") or not _node_has_row(state, node)):
                try:
                    _drop_receipt(state, request_id)
                except (OSError, FleetError):
                    pass
            raise
    expires = time.time() + cluster_setup.INVITE_TTL
    return {"ok": True, "node": node, "profile": profile, "label": label, "capabilities": _cap_list(
                report.get("capabilities") or caps, PROFILES[profile]["default_caps"]),
            "addresses": report.get("addresses") or [], "expires_at": expires, "expires": report.get("expires"),
            "code": report["code"], "secret": True,
            "node_commands": _join_commands(os_name) if os_name else _join_commands("linux"),
            "config_changed": changed, "device": stored,
            "next": "Run the join command on the node and paste the code. It works once, for 24 hours.",
            "step": "join"}


def revoke_node(config, state, body, *, config_path=None):
    from . import cluster_setup
    body = _clean(body, "revoke")
    node = _node_id(body.get("node"), config)
    if body.get("confirm") != node:
        raise FleetError("confirm_required", "Type the node's id as confirm to revoke it.", 400, step="revoke")
    _require_main(config)
    with _LOCK, _mutation_lock(state):
        if config_path is not None:
            config = _config_file(config_path)
            _require_main(config)
            _node_id(node, config)
        rows, jobs = _db_rows(state)
        was = node in rows
        # Credential first, then this node's receipts, inside cluster_setup.revoke.
        # The CLI calls that same function. A raise from Broker.revoke leaves the fence.
        result = cluster_setup.revoke(state, node)
    return {"ok": True, "node": node, "was_enrolled": was, "ssh_key_removed": bool(result.get("ssh_key_removed")),
            "active_jobs_cancelled": jobs.get(node, 0),
            **({"warning": "The credential is gone but the SSH key could not be removed. Remove its "
                           "jarvis-node line from authorized_keys."} if result.get("error") else {}),
            "step": "invite"}


def set_node_capabilities(config, state, body, *, config_path=None):
    from .cluster_store import Broker
    body = _clean(body, "capabilities")
    node = _node_id(body.get("node"), config)
    _require_main(config)
    device = (config.get("devices") or {}).get(node)
    if not isinstance(device, dict):
        raise FleetError("unknown_node", "That node isn't in the config.", 404)
    profile = _profile_of(device)
    caps, extra = _caps(body.get("capabilities") if body.get("capabilities") is not None else [], profile,
                        body.get("acknowledge"))
    with _LOCK, _mutation_lock(state):
        if config_path is not None:
            config = _config_file(config_path)
            _require_main(config)
            _node_id(node, config)
            fresh_device = (config.get("devices") or {}).get(node)
            if not isinstance(fresh_device, dict):
                raise FleetError("unknown_node", "That node isn't in the config.", 404)
            profile = _profile_of(fresh_device)
            caps, extra = _caps(body.get("capabilities") if body.get("capabilities") is not None else [],
                                profile, body.get("acknowledge"))
        rows, _ = _db_rows(state)
        if node not in rows:
            raise FleetError("not_enrolled", f"{node} has no invite or credential yet.", 404, step="invite")
        before = rows[node]["capabilities"]
        if before == caps:
            return {"ok": True, "node": node, "changed": False, "credential_rotated": False,
                    "capabilities": _cap_list(caps, PROFILES[profile]["default_caps"])}
        Broker(state).set_capabilities(node, caps)
    return {"ok": True, "node": node, "changed": True, "credential_rotated": False,
            "added": sorted(set(caps) - set(before)), "removed": sorted(set(before) - set(caps)),
            "capabilities": _cap_list(caps, PROFILES[profile]["default_caps"]),
            "note": "Camera also needs `jarvis camera allow on` on that node." if "camera" in caps else None}


def _preview_can_run(config, state, readiness, config_path):
    """True when POST would call the bound handoff for this target. Preview still writes nothing."""
    local, main = _identity(config)
    if not (local and local == main and (config.get("cluster") or {}).get("enabled")):
        return False
    if _explicit_config_file(config_path) is None or not _handoff_configured(state):
        return False
    needed = {"known_device", "not_cloud", "not_current_main"}
    return all(item["state"] == "ok" for item in readiness if item["id"] in needed)


def main_transfer(config, state, body, now=None, *, config_path=None):
    """Read-only preview of a main transfer. Never changes cluster.main_device.

    Without an explicit config file and a bound handoff, status and support stay `blocked`.
    With both, and a known non-cloud target that is not already main, on the local main,
    support is `supported` and status is `ready`. The preview still does not call the handoff."""
    body = _clean(body, "main_transfer")
    target = _node_id(body.get("target"), config, allow_main=True)
    _local, main = _identity(config)
    now = now or time.time()
    device = (config.get("devices") or {}).get(target)
    rows, _ = _db_rows(state)
    row = rows.get(target)
    readiness = [
        _check("known_device", isinstance(device, dict), "In the config." if isinstance(device, dict) else "Not a known device."),
        _check("not_current_main", target != main, "Different from the current main." if target != main
               else "It already is the main."),
        _check("not_cloud", _profile_of(device) != "cloud" if isinstance(device, dict) else None,
               "Not a cloud server." if _profile_of(device) != "cloud" else "Cloud servers never become the main."),
        _check("online", link_state(row, now) == "online" if target != main else None,
               "Connected now." if link_state(row, now) == "online" else "Not connected right now."),
    ]
    link = link_state(row, now) if target != main else None
    offline = link == "offline"
    configured = _preview_can_run(config, state, readiness, config_path)
    runnable = configured and not offline
    if configured:
        missing = [{"id": item["id"], "why": item["why"]} for item in readiness if item["state"] == "problem"]
    else:
        missing = [{"id": i, "why": w} for i, w in MAIN_TRANSFER_MISSING]
    if offline and configured:
        why = ("This target is offline. A missed check-in is not readiness, and this preview "
               "has not verified it. Nothing was changed.")
    elif runnable:
        why = ("This preview changed nothing. Safe handoff is configured for this target. "
               "That is not proof the target can take MAIN. POST runs the coordinator, which checks "
               "candidate readiness, lease expiry and the safety barrier.")
    else:
        why = "Safe main handoff is not configured, so nothing was changed."
    return {"status": "ready" if runnable else "blocked", "support": "supported" if configured else "blocked",
            "mutated": False, "target": target, "readiness": readiness,
            "missing": missing,
            "phases": list(MAIN_TRANSFER_PHASES),
            "risks": ["Two machines acting as the main could both write memory and run jobs.",
                      "Editing cluster.main_device by hand moves no data and fences nothing."],
            "why": why}


def _blocked_transfer(result, message=None):
    return FleetError("blocked", message or result["why"], 409, step="main_transfer", **{k: result[k] for k in
                      ("missing", "readiness", "phases", "risks", "target", "mutated")})


def main_transfer_refuse(config, state, body, *, config_path=None):
    """POST /api/fleet/main-transfer.

    Runs set_main when `config_path` is an absolute path to an existing file and this PC
    is the enabled local main. A missing path fails closed and does not look for a checkout
    config. The handoff's pending or failed result does not change the role. Success returns
    the handoff status; it does not suggest a restart."""
    result = main_transfer(config, state, body, config_path=config_path)
    path = _explicit_config_file(config_path)
    if path is None:
        raise _blocked_transfer(result)
    _require_main(config)
    from .cluster_cli import set_main
    from .config_lock import ConfigBusy, ConfigDrift, ConfigUnreadable
    try:
        outcome = set_main(config, path, result["target"], state=state)
    except (ConfigBusy, ConfigDrift, ConfigUnreadable) as exc:
        raise _translate(exc) from exc
    except ValueError as exc:
        raise _blocked_transfer(result, str(exc)) from exc
    if outcome.get("ok") is True and outcome.get("mutated") is True and outcome.get("main") == result["target"]:
        return {"ok": True, "status": outcome.get("status"), "mutated": True, "target": result["target"],
                "main": outcome.get("main"), "main_device": outcome.get("main_device"),
                "transition_id": outcome.get("transition_id"), "epoch": outcome.get("epoch"),
                "publication": outcome.get("publication"), "authority": outcome.get("authority")}
    if outcome.get("status") == "publication_conflict":
        raise FleetError("publication_conflict", outcome.get("error") or "Authority may have moved.",
                         409, step="main_transfer", mutated=False, target=result["target"],
                         authority=outcome.get("authority"), publication="conflict")
    if outcome.get("status") == "publication_failed":
        raise FleetError("publication_failed", outcome.get("error") or "The role was not published.",
                         409, step="main_transfer", retryable=True, mutated=False, target=result["target"],
                         authority=outcome.get("authority"), publication=outcome.get("publication"))
    code = "pending" if outcome.get("status") == "pending" else "blocked"
    raise FleetError(code, "Safe handoff did not move MAIN. cluster.main_device was not changed.",
                     409, step="main_transfer", mutated=False, target=result["target"],
                     handoff={"ok": False, "status": outcome.get("status"),
                              "transition_id": outcome.get("transition_id"),
                              "main": outcome.get("main"), "epoch": outcome.get("epoch")},
                     missing=result["missing"], readiness=result["readiness"])


# --- vault + GitHub status -------------------------------------------------------------

def _git(binary, root, args, timeout=5):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT", "LANG")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C",
               GIT_CEILING_DIRECTORIES=str(Path(root).parent), GIT_CONFIG_NOSYSTEM="1")
    try:
        proc = subprocess.run([binary, "-C", str(root), "--no-optional-locks", "-c", "core.fsmonitor=false",
                               "-c", "core.sshCommand=", *args], capture_output=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def scrub_remote(url):
    """{"provider", "host", "repo", "credentials_embedded"} from a remote URL, never the URL."""
    text = str(url or "").strip()
    host = repo = None
    embedded = False
    scp = re.fullmatch(r"(?:[\w.-]+@)?([A-Za-z0-9.-]+):(?!//)(.+)", text)
    if "://" in text:
        try:
            parts = urlsplit(text)
            host = (parts.hostname or "").lower() or None
            embedded = bool(parts.password) or (parts.username not in (None, "git", "ssh") and parts.scheme in ("http", "https"))
            repo = parts.path.strip("/") or None
        except ValueError:
            host = None
    elif scp:
        host, repo = scp.group(1).lower(), scp.group(2).strip("/")
    if repo and repo.endswith(".git"):
        repo = repo[:-4]
    if repo is not None and not re.fullmatch(r"[\w.-]+(?:/[\w.-]+){0,2}", repo):
        repo = None
    return {"provider": "github" if host == "github.com" else "other" if host else None, "host": host,
            "repo": repo if host == "github.com" else None, "credentials_embedded": embedded}


def _count_dirty(text):
    staged = modified = untracked = conflicted = 0
    for entry in text.split("\0"):
        if len(entry) < 3:
            continue
        x, y = entry[0], entry[1]
        if x == "?" and y == "?":
            untracked += 1
        elif x == "!" or entry[:2] == "  ":
            continue
        elif x == "U" or y == "U" or entry[:2] in ("AA", "DD"):
            conflicted += 1
        else:
            staged += x not in " ?"
            modified += y not in " ?"
    return {"staged": staged, "modified": modified, "untracked": untracked, "conflicted": conflicted}


def _vault_dir(config):
    from .vault import vault_path
    settings = config.get("vault") or {}
    value = vault_path(settings)
    if not value or settings.get("enabled", True) is False:
        return None, settings
    return Path(value).expanduser(), settings


_ARCHIVE_KEYS = ("enabled", "stage", "state", "gate", "running_here", "archive_pending", "archive_held",
                 "archive_errors", "capture_enabled", "capture_complete", "archive_unreconciled",
                 "archive_source_gone", "oldest_pending_age_s", "last_success", "queue", "drain_failures",
                 "files_stale", "archive_baseline_pending", "archive_baseline_seeded")


def archive_status(config, state):
    try:
        from . import second_brain_runtime
        full = second_brain_runtime.status(config, state)
    except Exception as exc:
        return {"state": "unverified", "error": type(exc).__name__}
    out = {k: full.get(k) for k in _ARCHIVE_KEYS if k in full}
    out["has_error"] = bool(full.get("last_error"))
    return out


def vault_status(config, state, *, ttl=15.0, now=None):
    """Numeric, body-free: the configured vault's git link and the personal archive. Local git
    only (no fetch, no network); results cached for `ttl` seconds so polling stays cheap."""
    now = now or time.time()
    root, settings = _vault_dir(config)
    out = {"checked_at": now, "network": False, "archive": archive_status(config, state),
           "vault": {"configured": root is not None, "exists": False, "write_enabled": bool(settings.get("write")),
                     "is_git": False},
           "git": None,
           "visibility": {"value": "unknown", "note": "Whether the repo is private needs a network call, which status "
                          "never makes. Check it once on GitHub."},
           "problems": []}
    if root is None:
        out["problems"].append("No vault is configured." if not settings else "The vault is off or has no path here.")
        return out
    out["vault"]["exists"] = root.is_dir()
    if not root.is_dir():
        out["problems"].append("The vault folder doesn't exist on this computer.")
        return out
    out["vault"]["is_git"] = (root / ".git").exists()
    if not out["vault"]["is_git"]:
        out["problems"].append("The vault isn't a git repository.")
        return out
    cached = _GIT_CACHE.get(str(root))
    if cached and now - cached[0] < ttl:
        out["git"] = cached[1]
        return out
    binary = shutil.which("git")
    if not binary:
        out["problems"].append("git isn't installed.")
        return out
    git = {"available": True}
    branch = _git(binary, root, ["rev-parse", "--abbrev-ref", "HEAD"])
    git["branch"] = (branch or "").strip() or None
    git["detached"] = git["branch"] == "HEAD"
    remote = _git(binary, root, ["config", "--get", "remote.origin.url"])
    git["remote"] = {"name": "origin", **scrub_remote(remote)} if remote else None
    counts = _git(binary, root, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    git["upstream_set"] = counts is not None
    try:
        ahead, behind = (int(x) for x in counts.split()) if counts else (None, None)
    except ValueError:
        ahead = behind = None
    git["ahead"], git["behind"] = ahead, behind
    git["ahead_behind_as_of"] = "last fetch"
    try:
        git["last_fetch_at"] = (root / ".git" / "FETCH_HEAD").stat().st_mtime
    except OSError:
        git["last_fetch_at"] = None
    dirty = _git(binary, root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], timeout=8)
    git["dirty"] = _count_dirty(dirty) if dirty is not None else None
    git["clean"] = (None if dirty is None else not any(git["dirty"].values()))
    git["connected"] = bool(git["remote"] and git["remote"].get("provider") == "github")
    if git["remote"] and git["remote"]["credentials_embedded"]:
        out["problems"].append("The remote URL embeds a credential. Switch it to SSH or a credential helper.")
    if dirty is None:
        out["problems"].append("git status could not be read (timeout or permissions).")
    _GIT_CACHE[str(root)] = (now, git)
    out["git"] = git
    return out
