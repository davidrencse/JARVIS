"""One main JARVIS, authenticated outbound node agents, and a local console proxy.

Nodes poll for allowlisted work. Dispatched actions are never automatically replayed.
Only the main creates model lanes, writes shared memory, or accepts control requests.
"""

import hmac
import json
import math
import os
import platform
import secrets
import select
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from .fsio import read_json
from .platforms import NO_WINDOW, on_windows
from . import __version__
from .deploy import code_fingerprint
from .release import served_code
from .fsio import atomic_write
from .cluster_store import BULK, Broker, CAPABILITIES, OWNER_TOKEN, TERMINAL
from .topology import DEFAULT_PORT, endpoint, identity, is_node, request_from

_HUBS = {}
MAX_PROTOCOL = 65536
MAX_RESULT = 4_000_000
HANDSHAKE_TIMEOUT = 10  # seconds a TLS client has to finish its handshake
POLL_WAIT = 25
WAIT_HOLD = 20  # seconds a requester asks the hub to hold a job-result request
FRESH = 10  # seconds after a check-in that a node counts as alive between polls
_PROCESS_RECORD = "cluster-process.json"
_STOP_REQUEST = "cluster-stop-request.json"
_STOP_ACK = "cluster-stop-ack.json"
_PROCESS_SCHEMA = 1


def _local_record(path):
    """Read one regular local lifecycle record; never follow a symlink."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _local_pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:  # process exists, but the caller cannot signal it
        return True
    except (ProcessLookupError, OSError):
        return False


def request_node_worker_stop(config_path, state, *, timeout=35.0):
    """Request a bounded cooperative stop of this exact local NodeWorker.

    The local registration nonce binds the request to the worker's PID, config,
    and state directory. No OS signal, owner token, shell command, or remote
    control channel is used. A timeout leaves the request for a safe retry.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 45):
        raise ValueError("cooperative stop timeout must be in (0, 45] seconds")
    config = Path(config_path).expanduser().resolve(strict=True)
    state = Path(state).expanduser().resolve(strict=True)
    registration_path = state / _PROCESS_RECORD
    registration = _local_record(registration_path)
    required = {"schema", "kind", "pid", "nonce", "config", "state"}
    if (not isinstance(registration, dict) or set(registration) != required
            or registration.get("schema") != _PROCESS_SCHEMA
            or registration.get("kind") != "node-worker"
            or registration.get("config") != str(config)
            or registration.get("state") != str(state)
            or not isinstance(registration.get("nonce"), str)
            or len(registration["nonce"]) != 48
            or any(c not in "0123456789abcdef" for c in registration["nonce"])):
        raise RuntimeError("matching local NodeWorker registration is unavailable")
    if not _local_pid_alive(registration["pid"]):
        raise RuntimeError("registered NodeWorker is not running")

    request_path, ack_path = state / _STOP_REQUEST, state / _STOP_ACK
    from .fsio import atomic_write
    from .locking import exclusive_lock
    with exclusive_lock(state / "cluster-stop-request.lock"):
        existing = _local_record(request_path)
        if existing is not None:
            if (set(existing) != {"schema", "kind", "pid", "nonce", "config", "state", "request_id"}
                    or any(existing.get(k) != registration.get(k) for k in
                           ("schema", "kind", "pid", "nonce", "config", "state"))):
                raise RuntimeError("a different cooperative stop request is already pending")
            request = existing
        else:
            if request_path.is_symlink() or ack_path.is_symlink():
                raise RuntimeError("cooperative stop receipt is unsafe")
            request = {**registration, "request_id": uuid.uuid4().hex}
            atomic_write(request_path, json.dumps(request, sort_keys=True), mode=0o600, fsync=True)

    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        ack = _local_record(ack_path)
        if (isinstance(ack, dict) and set(ack) == {"schema", "kind", "pid", "nonce", "config", "state",
                                                  "request_id", "status"}
                and all(ack.get(k) == request.get(k) for k in
                        ("schema", "kind", "pid", "nonce", "config", "state", "request_id"))
                and ack.get("status") == "stopped"):
            return {"ok": True, "status": "stopped", "request_id": request["request_id"]}
        current = _local_record(registration_path)
        if current is None and not _local_pid_alive(registration["pid"]):
            raise RuntimeError("NodeWorker exited without a matching cooperative stop receipt")
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("NodeWorker did not cooperatively stop before the bounded timeout")
# The console API a node (with "console") may reach on the main; the owner token, held only
# by the main's own windows, reaches all of it. Left out on purpose: settings, files, screen,
# memory, uploads, plan runs and Grokbot commands (its /terminal opens a shell here), so a
# compromised node can't use the main to take over the cluster. Also left out, because their
# bodies carry other requests' text, replies, recalled memory or task evidence: /api/events,
# /api/history*, /api/cases, /api/tasks/*, /api/plans/*. What remains is metadata (status, usage,
# repo layout) plus the two views below. Read-only does not mean content-free.
NODE_API_READ = frozenset({"/api/overview", "/api/confirm", "/api/route", "/api/tree",
                           "/api/specialists", "/api/graph/system", "/api/usage", "/api/credits",
                           "/api/voice", "/api/agent", "/api/grokbot"})
NODE_API_READ_UNDER = ("/api/jobs/",)  # + one id segment; only the node's own job (node_job)
# Routes a node reads through an allowlist of top-level fields: the full body also holds memory
# excerpts, task and plan text, config and other origins' confirmation outcomes.
NODE_API_FIELDS = {
    "/api/overview": ("version", "now", "cluster", "devices", "routing", "models", "brain",
                      "live", "voice", "tools"),
    "/api/confirm": ("pending", "grants"),  # pending is already scoped to the caller's origin
}
NODE_API_SUBMIT = frozenset({"/api/ask", "/api/listen", "/api/confirm"})  # also needs "ask"
# Job kinds refused on /v1/action and /v1/control for every caller, the owner token too (so is
# "remote"): only in-process callers (the console, the phone server) start them, through
# Hub.remote_open or Hub.dispatch. No shell or agent holding a credential opens a camera or a fix.
IN_PROCESS_ONLY = {"camera": "Only the main's console or phone can turn a camera on.",
                   "locate": "Only the main's console or phone can ask where a computer is."}


def _name(node, config):
    return str((config.get("devices", {}).get(node) or {}).get("name") or node).title()


def _label(action):
    return str(action).replace("_", " ")


CLIP_CHECK = 60  # seconds between "is the clipboard watcher up?" checks


def _refresh_clipboard(state, config=None, stop=None):
    """Restart the clipboard watcher if it runs older code, then keep it running: a reboot
    or a crash used to leave copy/paste between the laptops dead until someone typed
    `jarvis clipboard start` (2026-11-04: down from a 10:44 reboot until evening). Runs
    in the background (never delays the hub or the node starting). The watcher pushes
    over the hub, so an offline peer just skips copies until it's back.
    clipboard.autostart: false in jarvis.config.json turns the keep-alive off."""
    from . import _under_tests
    keep = ((config or {}).get("clipboard") or {}).get("autostart", True) and not _under_tests()

    def run():
        from . import clipsync
        try:
            clipsync.refresh(str(state))
        except Exception as exc:
            print(f"JARVIS: clipboard watcher not refreshed: {exc}", flush=True)
        while keep:
            try:
                # A `clipboard stop` by hand sticks until the next boot (audit cluster-02).
                if not clipsync._pid(str(state)) and not clipsync.stopped_by_hand(str(state)):
                    result = clipsync.start(str(state))
                    print(f"JARVIS: clipboard watcher was down; started: {result.get('started')}",
                          flush=True)
            except Exception as exc:  # never let the keep-alive take the hub or node down
                print(f"JARVIS: clipboard watcher not started: {exc}", flush=True)
            if stop is not None and stop.wait(CLIP_CHECK):
                return
            if stop is None:
                time.sleep(CLIP_CHECK)
    threading.Thread(target=run, name="jarvis-clip-keepalive", daemon=True).start()


def _keepalive_after_update(state, config=None, stop=None, delay=3):
    """After a release restart, re-check clipboard and Lan Mouse soon. The minute
    guard and ensure_dependents's tunnel-health edge both miss that restart.
    No-op under tests, same as the clipboard keep-alive."""
    from . import _under_tests
    if _under_tests():
        return
    threading.Thread(target=_run_keepalive_after_update, name="jarvis-post-update-keepalive",
                     args=(state, config, stop, delay), daemon=True).start()


def _run_keepalive_after_update(state, config, stop, delay):
    if stop is not None:
        stop.wait(delay)
    else:
        time.sleep(delay)
    try:
        from .cluster_recovery import ensure_dependents
        ensure_dependents(config or {}, state)
    except Exception as exc:  # a failed re-check must not take the new process down
        print(f"JARVIS node: post-update keep-alive failed: {exc}", flush=True)


class NodeOffline(ConnectionError):
    """An enrolled node was offline before any job was enqueued; SSH is safe."""


# Queries that change nothing, allowed even when a node's code is out of date, so Settings
# and status can still say what it is.
# observe only reads windows and playback (the brain's snapshot asks it every request).
READ_ONLY_DESKTOP = frozenset({"input_info", "list_apps", "screen", "observe"})


def _read_only(kind, payload):
    return kind == "inventory" or (kind == "desktop" and payload.get("action") in READ_ONLY_DESKTOP)


def _connected(sock):
    """Is the other end of a held poll still there? A node that stopped closes its side,
    which makes the socket readable with nothing to read (EOF). A poll's request body has
    already been read, so a readable socket here means a hang-up, not data."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable or isinstance(sock, ssl.SSLSocket):
            return True  # TLS can't peek; its liveness rests on FRESH and running jobs
        return bool(sock.recv(1, socket.MSG_PEEK))
    except (OSError, ValueError):
        return False


def _systemd_service(name):
    """True only when this process is the systemd unit `name` itself. INVOCATION_ID alone
    is inherited by anything a unit starts (a terminal, a shell), and a node started by hand
    from one would exit for systemd to restart, which then never happens."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:  # not Linux, or no /proc
        return False
    return any(line.rstrip().endswith(f"/{name}.service") for line in cgroup.splitlines())


def save_json(path, value):
    atomic_write(path, json.dumps(value, indent=2), mode=0o600)


def redeem_invite(config, state, client=None):
    """If this node still runs on its invite's credentials, swap them for its own (once):
    a new hub token, and a new SSH key when the main can install it. A no-op afterwards.
    Returns what changed, or None when there was nothing to redeem."""
    path = Path(state) / "cluster-node.json"
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not note.get("redeem"):
        return None
    from .cluster_setup import _private_file, keypair
    # Save what we propose before asking: if the main's reply is lost, the main already
    # holds these, and the invite is dead, so the retry must offer the same ones.
    pending = note.get("pending")
    if not isinstance(pending, dict) or not all(isinstance(pending.get(k), str) for k in
                                                ("token", "private", "public")):
        private, public = keypair(note["node_id"])
        pending = note["pending"] = {"token": secrets.token_urlsafe(32), "private": private, "public": public}
        save_json(path, note)
    # The main swaps the invite's SSH key for ours before it replies. If the reply is lost
    # the tunnel must still get in, so the proposed key sits beside the invite key (the
    # tunnel offers both) until the redeem is confirmed.
    pending_key = Path(state) / PENDING_KEY
    try:
        if not pending_key.exists() or pending_key.read_text(encoding="ascii") != pending["private"]:
            _private_file(pending_key, pending["private"])
    except OSError:
        pass
    client = client or Client(config, state)
    body = {"public_key": pending["public"], "token": pending["token"]}
    try:
        answer = client.request("/v1/redeem", body, timeout=15)
    except HubRefused as exc:
        if exc.status != 403:
            raise
        # The invite may be spent by our own earlier, unanswered attempt: try our proposal.
        invite, client.token = client.token, pending["token"]
        try:
            answer = client.request("/v1/redeem", body, timeout=15)
        except HubRefused:
            client.token = invite
            raise exc from None
    if answer.get("token") != pending["token"]:
        raise ValueError("The main sent no credential.")
    if answer.get("key_rotated"):  # the tunnel's next connection uses it; this one stays up
        _private_file(Path((note.get("link") or {}).get("identity_file")
                           or Path(state) / "cluster-node.key"), pending["private"])
    note.pop("redeem", None)
    note.pop("pending", None)
    pending_key.unlink(missing_ok=True)
    note["token"] = client.token = answer["token"]
    client.invite = False
    save_json(path, note)
    return {"token": True, "ssh_key": bool(answer.get("key_rotated"))}


PENDING_KEY = "cluster-node-pending.key"  # read by cluster_tunnel.command


class HubRefused(RuntimeError):
    """The main answered with an HTTP error; `status` says which (400/404 are final)."""
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class LocalClient:
    """What a session on the main itself uses in place of a node's Client: the same
    /v1/remote/* requests, answered in this process by Hub.session_call. A refusal comes back
    as HubRefused with the status the HTTP route would give (404, 403, 400), so the session
    ends at once: a bare PermissionError is an OSError, which would read as a lost link."""

    def __init__(self, hub):
        self.hub, self.main = hub, hub.main

    def request(self, path, body=None, timeout=45):
        try:
            return self.hub.session_call(self.hub.main, path, {} if body is None else body)
        except PermissionError as exc:
            raise HubRefused(f"Main PC refused the request: {exc}", 403) from None
        except LookupError as exc:
            raise HubRefused(f"Main PC refused the request: {exc}", 404) from None
        except (ValueError, TypeError) as exc:
            raise HubRefused(f"Main PC refused the request: {exc}", 400) from None


def _screen_here(state):
    """(ok, why): can a session drive this computer's own screen now?"""
    from .control import _stopped
    if _stopped(state):
        return False, "Desktop control is stopped on this computer (jarvis control resume)."
    try:
        from . import control, platforms
        if platforms.detect()["os"] == "macos":
            return False, "Remote control isn't built for macOS computers yet."
        control.backend()  # LookupError when this desktop has no injector
    except LookupError as exc:
        return False, f"Remote control can't run on this computer: {exc}"
    from .remote_view import desktop_problem
    problem = desktop_problem()
    return (False, problem) if problem else (True, None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # credentials must never follow redirects


class Client:
    def __init__(self, config, state, owner=False):
        self.config, self.state, self.owner = config, Path(state), owner
        # The real pairing lives in the checkout's .jarvis; under tests it would let an ask
        # reach the live main and its ChatGPT brain (see jarvis/__init__.py).
        if os.environ.get("JARVIS_NO_MODELS") == "1" and \
                self.state.resolve() == (Path(__file__).resolve().parent.parent / ".jarvis").resolve():
            raise PermissionError("Tests may not use this PC's real cluster credentials (JARVIS_NO_MODELS).")
        from .continuity.repoint import effective_config
        config = effective_config(config, self.state)
        self.config = config
        self.local, self.main = identity(config)
        self.url = endpoint(config)
        if owner:
            if self.local != self.main:
                raise ValueError("Only the configured main PC can use its owner credential.")
            path = self.state / OWNER_TOKEN
            if not path.is_file():
                raise ValueError("Start JARVIS on the main PC first (jarvis cluster serve).")
            self.token = path.read_text(encoding="ascii").strip()
        else:
            path = self.state / "cluster-node.json"
            if not path.is_file():
                raise ValueError("This node is not paired. Run jarvis cluster join PAIRING_FILE.")
            note = json.loads(path.read_text(encoding="utf-8"))
            if note.get("node_id") != self.local or note.get("main_device") != self.main:
                raise ValueError("Node credentials do not match the configured main/local PC. Pair again.")
            self.token = note.get("token", "")
            self.invite = bool(note.get("redeem"))  # may be swapped by another process
        if not isinstance(self.token, str) or len(self.token) < 20:
            raise ValueError("Invalid cluster credential; pair this node again.")
        ca = config.get("cluster", {}).get("ca_file")
        context = ssl.create_default_context(cafile=str(Path(ca).expanduser()) if ca else None)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                         urllib.request.HTTPSHandler(context=context), _NoRedirect())

    def open(self, path, body=None, timeout=45, headers=None, method=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Cluster paths must be relative to the configured main.")
        request = urllib.request.Request(self.url + path, data=body, method=method,
            headers={**(headers or {}), "Authorization": f"Bearer {self.token}",
                     "X-JARVIS-Main": self.main})
        return self.opener.open(request, timeout=timeout)

    def request(self, path, body=None, timeout=45):
        try:
            return self._request(path, body, timeout)
        except HubRefused as exc:
            # Another process on this node redeemed the invite and saved a new token
            # (redeem_invite): pick it up once instead of failing until restarted.
            if exc.status != 403 or not getattr(self, "invite", False) or not self._reload():
                raise
            return self._request(path, body, timeout)

    def _reload(self):
        try:
            token = json.loads((self.state / "cluster-node.json").read_text(encoding="utf-8")).get("token")
        except (OSError, ValueError):
            return False
        if not isinstance(token, str) or len(token) < 20 or token == self.token:
            return False
        self.token, self.invite = token, False
        return True

    def _request(self, path, body=None, timeout=45):
        try:
            with self.open(path, None if body is None else json.dumps(body).encode(), timeout,
                           {"Content-Type": "application/json"}) as response:
                data = response.read(MAX_RESULT + 1)
                if len(data) > MAX_RESULT:
                    raise ValueError("Main PC response exceeded the protocol limit.")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            code = None
            try:
                note = json.loads(exc.read(MAX_PROTOCOL))
                detail = note.get("error", f"HTTP {exc.code}")
                code = note.get("code")
            except (ValueError, AttributeError):
                detail = f"HTTP {exc.code}"
            finally:
                exc.close()
            if code == "node_offline":
                raise NodeOffline(detail) from None
            raise HubRefused(f"Main PC refused the request: {detail}", exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(f"Main PC {self.main} is unavailable at {self.url}: {exc}") from None

    def wait(self, job_id, timeout):
        deadline = time.monotonic() + timeout
        fast = 0
        while True:
            # The hub holds this request up to WAIT_HOLD s for the result; an older hub ignores
            # ?wait and answers at once, which the pause below turns into gentle polling.
            left = max(1, deadline - time.monotonic())
            hold = min(WAIT_HOLD, int(left))
            began = time.monotonic()
            result = self.request(f"/v1/jobs/{job_id}?wait={hold}", timeout=min(hold + 15, left + 15))
            if result["state"] in TERMINAL:
                if result["state"] == "done":
                    return result["result"]
                error = (result.get("result") or {}).get("error")
                raise RuntimeError(error or f"Request {job_id} is {result['state']}; do not automatically retry it.")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Request {job_id} has no confirmed result. It may have run; do not automatically retry.")
            # Answered at once (old hub, or a hub that cut the hold short): back off, 0.15 s
            # growing to 2 s, instead of hammering it.
            if time.monotonic() - began < 0.5:
                fast = min(fast * 2, 2.0) if fast else 0.15
                time.sleep(min(fast, max(0, deadline - time.monotonic())))
            else:
                fast = 0

    def ask(self, text, tier=None, write=False, timeout=600, request_id=None):
        started = self.request("/v1/ask", {"text": text, "tier": tier, "write": write,
                               "timeout": timeout, "request_id": request_id or uuid.uuid4().hex})
        return self.wait(started["id"], timeout + 5)


def node_api(path, post, caps):
    """Refuse console API calls a node may not make on the main (see NODE_API_READ)."""
    if post:
        allowed = path in NODE_API_SUBMIT and "ask" in caps
    else:
        allowed = path in NODE_API_READ or any(
            path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]
            for prefix in NODE_API_READ_UNDER)
    if not allowed:
        raise PermissionError(f"Nodes can't use {path} on the main PC.")


def node_job(console, node, path):
    """A node may poll only jobs its own requests started (submit() stamps origin_device); a
    plan run, another node's job or the main's own turn holds text the node never sent."""
    try:
        job = console.job(path.rsplit("/", 1)[-1])
    except LookupError:
        job = None
    if not job or job.get("origin_device") != node:
        raise PermissionError(f"Nodes can't use {path} on the main PC.")


def node_fields(path, body):
    """Cut a node's copy of a route down to NODE_API_FIELDS (nothing nested is kept by name)."""
    keep = NODE_API_FIELDS.get(path)
    if keep is None or not isinstance(body, dict):
        return body
    return {key: body[key] for key in keep if key in body}


def validate_action(kind, payload):
    if not isinstance(payload, dict):
        raise ValueError("Node payload must be an object.")
    if kind == "desktop":
        from .remote import ACTIONS
        if payload.get("action") not in ACTIONS or not isinstance(payload.get("arg"), str):
            raise ValueError("Unknown desktop action or invalid argument.")
        if len(payload["arg"]) > 12000:
            raise ValueError("Desktop argument is too long.")
    elif kind == "grokbot":
        if not isinstance(payload.get("text"), str) or not 0 < len(payload["text"]) <= 16000:
            raise ValueError("Invalid Grokbot prompt.")
    elif kind == "storage":
        if payload.get("action") not in ("list", "resolve", "exists", "thumbnail") or not isinstance(payload.get("path"), str):
            raise ValueError("Invalid node storage query.")
    elif kind == "continuity":
        from .continuity.participant import validate
        validate(payload)
    elif kind == "transfer":
        from .transfer import validate
        validate(payload)
    elif kind == "remote":  # David's remote-control session (remote_view.py)
        from .remote_view import validate
        validate(payload)
    elif kind == "camera":  # a live webcam view for David's phone or console (camera.py)
        from .camera import validate
        validate(payload)
    elif kind == "locate":  # where this computer is (locate.py)
        from .locate import validate
        validate(payload)
    elif kind != "inventory":
        raise ValueError("Nodes execute only desktop, inventory, or Grokbot operations, and file transfers.")


def _execute_local_body(kind, payload, config, state):
    if kind == "continuity":
        raise ValueError("Continuity RPCs require the private coordinator channel.")
    if kind == "desktop":
        from .remote import run_here
        extras = {}
        try:
            from .continuity.integration import local_desktop_args
            extras = local_desktop_args()
        except ImportError:
            extras = {}
        ok, text = run_here(payload["action"], payload["arg"], **extras)
        return {"ok": ok, "text": text}
    if kind == "inventory":
        from .inventory import collect
        return collect()
    if kind == "storage":
        from .storage import Storage
        return Storage(config, state)._local(identity(config)[0], payload["path"], payload["action"])
    if kind == "transfer":
        from .transfer import run_here
        return run_here(payload, state, config)
    if kind == "remote":
        from .remote_view import run
        return run(payload, config, state)
    if kind == "camera":
        from .camera import run
        return run(payload, config, state)
    if kind == "locate":
        from .locate import run_here
        return run_here(payload, config, state)
    from .grokbot import Grokbot
    return Grokbot(config, state).send(payload["text"], device=identity(config)[0])


def execute_local(kind, payload, config, state):
    validate_action(kind, payload)
    try:
        from .continuity.integration import open_local_effect, refuse_unfenced
    except ImportError:  # continuity is optional; without it the legacy path is the only one
        open_local_effect = None
        refuse_unfenced = None
    if refuse_unfenced is not None:
        refuse_unfenced("cluster_local")
    if open_local_effect is None:
        return _execute_local_body(kind, payload, config, state)
    return open_local_effect(
        state, config, kind, payload, lambda: _execute_local_body(kind, payload, config, state))


# A valid owner or node token is always served. Backoff counts only failed
# credentials, and a good token clears that source. Failures older than the
# window are forgotten, so the delay falls on its own. The hub is on loopback
# behind SSH tunnels, so most clients share one address; the global counter
# is what bounds a flood from that shared address.
class AuthBackoff:
    """Per-source and global exponential delay after repeated bad credentials."""

    _MAX_SOURCES = 1024

    def __init__(self, clock=time.monotonic, free=5, global_free=50, base=1.0, cap=60.0, window=300.0):
        self._clock = clock
        self._free = free
        self._global_free = global_free
        self._base = base
        self._cap = cap
        self._window = window
        self._lock = threading.Lock()
        self._hits = {}  # source -> deque of failure times, oldest first; insertion order
        self._global = deque()
        self._keep = self._keep_of(free)
        self._global_keep = self._keep_of(global_free)

    def _keep_of(self, free):
        """Newest stamps that can still change the delay. At this count the delay is at the cap."""
        try:
            free_n = int(free)
        except (TypeError, ValueError):
            free_n = 0
        if free_n < 0:
            free_n = 0
        try:
            base, cap = float(self._base), float(self._cap)
        except (TypeError, ValueError):
            return min(4096, max(1, free_n))
        if base <= 0 or cap <= 0 or base >= cap:
            return min(4096, max(1, free_n))
        steps = min(63, max(0, math.ceil(math.log2(cap / base))))
        return min(4096, max(1, free_n + steps))

    def _key(self, source):
        if source is None:
            return "?"
        return source if isinstance(source, str) else str(source)

    def _delay(self, count, free):
        # delay = min(cap, base * 2**(n-free)) once n reaches free; else none.
        try:
            if count < free or not (self._base > 0) or not (self._cap > 0):
                return 0.0
        except TypeError:
            return 0.0
        extra = count - free
        if extra >= 63:
            return float(self._cap)
        delay = float(self._base) * (2.0 ** extra)
        if not math.isfinite(delay) or delay >= self._cap:
            return float(self._cap)
        return delay

    def _trim(self, stamps, now, keep):
        window = self._window
        while stamps and now - stamps[0] > window:
            stamps.popleft()
        while keep >= 0 and len(stamps) > keep:
            stamps.popleft()

    def _expire(self, now):
        self._trim(self._global, now, self._global_keep)
        stale = []
        for key, stamps in self._hits.items():
            self._trim(stamps, now, self._keep)
            if not stamps:
                stale.append(key)
        for key in stale:
            self._hits.pop(key, None)

    def retry_after(self, source):
        try:
            now = self._clock()
            with self._lock:
                self._expire(now)
                stamps = self._hits.get(self._key(source))
                local = self._delay(len(stamps), self._free) if stamps else 0.0
                glob = self._delay(len(self._global), self._global_free)
                return float(max(local, glob))
        except Exception:
            return 0.0

    def fail(self, source):
        now = self._clock()
        with self._lock:
            self._expire(now)
            key = self._key(source)
            stamps = self._hits.pop(key, None)
            if stamps is None:
                while len(self._hits) >= self._MAX_SOURCES:
                    self._hits.pop(next(iter(self._hits)))
                stamps = deque()
            stamps.append(now)
            self._trim(stamps, now, self._keep)
            self._hits[key] = stamps
            self._global.append(now)
            self._trim(self._global, now, self._global_keep)

    def ok(self, source):
        with self._lock:
            self._hits.pop(self._key(source), None)


class AuthBackoffError(PermissionError):
    """A failed credential from a source that must wait. No token or node detail."""

    def __init__(self, retry_after):
        super().__init__("Too many failed authentications.")
        try:
            seconds = int(math.ceil(float(retry_after)))
        except (TypeError, ValueError, OverflowError):
            seconds = 1
        self.retry_after = seconds if seconds > 0 else 1


class Hub:
    def __init__(self, config, base, state, console=None, ask_handler=None):
        self.config, self.base, self.state = config, Path(base), Path(state).resolve()
        self.local, self.main = identity(config)
        if self.local != self.main:
            raise ValueError("Only the configured main PC may host JARVIS.")
        self._addresses = []
        self.backoff = AuthBackoff()
        self.broker = Broker(state)
        self.owner = self.broker.owner_token()
        self.console, self.ask_handler = console, ask_handler
        self.stop_event = threading.Event()
        self._condition = threading.Condition()
        self._polling = {}  # node -> sockets of polls held open now (guarded by _condition)
        self._response_context = threading.local()
        self._active_responses = {}
        self._active_responses_lock = threading.RLock()
        self._orphaned_response_callbacks = []
        self.server = self.thread = self.worker = self._lock = None
        self.started = False
        from .remote_view import Sessions
        from . import _under_tests
        pause = None
        if not _under_tests():  # tests never touch the real Lan Mouse
            from .sharedinput import ControllerPause
            pause = ControllerPause(state).set  # David's input stays here while he drives a node
        self.remote = Sessions(on_busy=pause)  # David's remote-control sessions, in memory only
        self._pause = pause  # its leftover-pause recovery waits for start(): only the lock holder may
        # Sessions whose target is the main itself (run in this process): id -> kind. Tracked
        # here, not read from remote_view._ACTIVE, which is per process, not per hub.
        self._here, self._here_lock = {}, threading.Lock()

    def redeem(self, node, public_key=None, token=None):
        """A newly joined node trades its invite for its own credential: a fresh hub token
        and, when this PC lets JARVIS write authorized_keys, its own SSH key in place of
        the invite's. Either way the invite code is dead afterwards. Repeating it with the
        same proposed token (the node lost our reply) gives the same answer but authorizes
        nothing: only the first redeem's key is ever written, so a node's own token can't
        swap in a new SSH key later."""
        token, first = self.broker.redeem(node, token)
        rotated = False
        if public_key and first:
            from .cluster_setup import DEFAULT_PORT, authorize, pinned_from
            port = int((self.config.get("cluster") or {}).get("listen_port") or DEFAULT_PORT)
            try:
                authorize(node, str(public_key), port, from_addr=pinned_from(self.config, node))
                rotated = True
            except (OSError, ValueError):  # e.g. Windows admin keys need an elevated shell
                pass
        elif public_key:  # a retry: report whether the first redeem wrote this very key
            from .cluster_setup import authorized
            try:
                rotated = authorized(node, public_key=str(public_key))
            except OSError:
                pass
        return {"token": token, "key_rotated": rotated}

    def defer_after_response(self, callback):
        """Schedule a process pivot after this request's result is written."""
        if not callable(callback):
            raise RuntimeError("response_ack_unavailable")
        request = getattr(self._response_context, "request", None)
        if request is not None:
            with self._active_responses_lock:
                if request.get("written"):
                    run_now = True
                else:
                    request["callbacks"].append(callback)
                    return True
            if run_now:
                callback()
                return True
        with self._active_responses_lock:
            candidates = [item for item in self._active_responses.values()
                          if item.get("role_transition") and not item.get("written")]
            if not candidates:
                orphaned = getattr(self, "_orphaned_response_callbacks", None)
                if orphaned:
                    orphaned.append(callback)
                    return True
                return False
            request = candidates[-1]
            request["callbacks"].append(callback)
            return True

    def authenticate(self, headers, capability=None, source=None):
        if headers.get("X-JARVIS-Main") != self.main:
            raise PermissionError("This endpoint is not the configured main PC; update cluster config.")
        if source is None:
            source = "?"
        auth = headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        presented, owner = token.encode(), self.owner.encode()
        if (token and len(presented) == len(owner) and 20 <= len(token) <= 256
                and hmac.compare_digest(presented, owner)):
            self.backoff.ok(source)
            return self.main, CAPABILITIES | {"owner"}
        found = self.broker.authenticate(token)
        if found and (not capability or capability in found[1]):
            self.backoff.ok(source)
            return found
        # A real token that merely lacks this capability is not a credential failure,
        # and backoff never applies to it. Only a bad, missing, or revoked token counts.
        if not found:
            self.backoff.fail(source)
            wait = self.backoff.retry_after(source)
            if wait > 0:
                raise AuthBackoffError(wait)
        raise PermissionError("Node credential is missing, revoked, or lacks this capability.")

    def _keep_up(self, settle=20, every=30):
        """After start and then every `every` s: cut a tested release once the main's code
        has changed and settled (release.py), then bring SSH nodes up to it. Nodes only ever
        get code the test suite passed on, never a file saved mid-edit."""
        from . import _under_tests
        from .release import FAST_QUIET, QUIET, cut, due
        if self.stop_event.wait(settle):  # let nodes reconnect and report their code
            return
        self._release_started_code = code_fingerprint()
        auto = (self.config.get("cluster") or {}).get("auto_release", True)
        while True:
            try:
                # Never under tests: a release runs the whole suite.
                quiet = QUIET
                if auto and not _under_tests():
                    live = code_fingerprint()  # a node online and behind gets the fast path
                    if any(n["online"] and n.get("code") != live for n in self.broker.nodes()):
                        quiet = FAST_QUIET
                if auto and not _under_tests() and due(self.state, quiet=quiet):
                    report = cut(self.state, quiet=quiet)
                    print(f"JARVIS: release {report.get('code')}: "
                          f"{'released' if report.get('released') else report.get('note')}", flush=True)
                self._auto_update(settle=0)
                from .release_runtime import maybe_restart
                maybe_restart(self)
            except Exception as exc:  # a file saved mid-copy must not end auto-update for good
                print(f"JARVIS: auto-update step failed: {exc}", flush=True)
            if self.stop_event.wait(every):
                return

    def _auto_update(self, settle=20):
        """Bring every online node that runs other code up to the release (jarvis cluster
        update), so nodes follow the main without git or a manual step. Each release is
        pushed to a node once: one that rolled back stays on its code until the next release.
        Failures are logged; actions to that node stay refused until it matches. Nodes
        without SSH (forward tunnel) pull the code themselves, so they are skipped here."""
        if self.stop_event.wait(settle):  # let nodes reconnect and report their code
            return
        from .deploy import is_ssh_node, update
        from .release_runtime import busy
        if busy(self.state, hub=self):
            return
        want = served_code(self.state)
        pushed = self.__dict__.setdefault("_pushed", {})
        for status in self.broker.nodes():
            if (not status["online"] or status.get("code") == want or status.get("pulls") == "yes"
                    or not is_ssh_node(self.config, status["id"]) or pushed.get(status["id"]) == want):
                continue
            pushed[status["id"]] = want
            try:
                report = update(self.config, self.state, status["id"])
                print(f"JARVIS: updated {status['id']} to {report.get('after')}", flush=True)
            except Exception as exc:  # never takes the hub down
                print(f"JARVIS: couldn't update {status['id']}: {exc}", flush=True)

    def alive(self, node, status):
        """Ready to take a job now: holding a poll open, checked in moments ago (between
        polls), or running a job. A stopped node is none of these within FRESH seconds, so
        callers fall back to SSH at once instead of queueing a job nobody will claim.
        (status["online"], the 75 s window, only says it was seen recently.)"""
        if self.broker.busy_job(node):
            return True  # single-threaded executor: no polling while a long job runs
        if not status.get("online"):
            return False
        with self._condition:
            held = list(self._polling.get(node, ()))
        if any(_connected(sock) for sock in held):
            return True
        if status.get("seen") and time.time() - status["seen"] < FRESH:
            return True
        return False

    def _await_poll(self, node, status, seconds=3):
        """Seen lately but between polls (it just sent a result and is reconnecting): give
        it a moment to open the next poll instead of calling it offline. Nothing is queued
        yet, so waiting can't run anything twice."""
        if not status.get("online"):
            return False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            with self._condition:
                self._condition.wait(0.2)
            fresh = next((n for n in self.broker.nodes() if n["id"] == node), status)
            if self.alive(node, fresh):
                return True
        return False

    def dispatch(self, node, kind, payload, timeout=45):
        if kind == "continuity":
            raise ValueError("Continuity RPCs use the private coordinator channel.")
        validate_action(kind, payload)
        try:
            from .continuity.integration import refuse_unfenced, require_production_authority
        except ImportError:  # continuity is optional; without it the legacy path is the only one
            refuse_unfenced = None
            require_production_authority = None
        if refuse_unfenced is not None:
            refuse_unfenced("hub_dispatch")
        if require_production_authority is not None:
            require_production_authority(self.state, self.config, "hub_dispatch", target=node)
        if node not in self.config.get("devices", {}):
            raise ValueError("Unknown node.")
        if node == self.main:
            if kind == "remote":
                raise ValueError("Remote control is for another computer, not the main itself.")
            if kind == "camera":  # the main's own camera runs through remote_open(allow_main=True)
                raise ValueError("The main's camera opens through its own session, not a job.")
            return execute_local(kind, payload, self.config, self.state)
        status = next((n for n in self.broker.nodes() if n["id"] == node), None)
        capability = ("files" if kind in ("storage", "transfer") else
                      "desktop" if kind == "remote" else kind)
        if not status or capability not in status["capabilities"]:
            raise ValueError(f"Node {node} is not enrolled for {kind} actions.")
        if not self.alive(node, status) and not self._await_poll(node, status):
            raise NodeOffline(f"Node {node} is offline. Start jarvis cluster node on that PC.")
        if kind == "desktop" and isinstance(payload, dict) and payload.get("action") == "terminal":
            from .terminal import TIMEOUT as terminal_timeout
            timeout = max(timeout, terminal_timeout + 15)  # never give up before the command can
        busy = self.broker.busy_job(node)
        if busy:
            raise RuntimeError(f"{_name(node, self.config)} is busy finishing {_label(busy['action'])}; "
                               "try again in a moment.")
        if not _read_only(kind, payload) and status.get("code") != served_code(self.state):
            # Mismatched code can misread a newer job; refuse rather than guess.
            raise ValueError(f"{node} runs different JARVIS code ({status.get('code') or 'unknown'}, "
                             f"main {served_code(self.state)}). Run: jarvis cluster update {node}")
        try:
            from .continuity.integration import seal_job_payload
        except ImportError:
            seal_job_payload = None
        if seal_job_payload is not None:
            # After the job is known to be queueable. The envelope rides inside the payload
            # the poll-time job signature already covers.
            payload = seal_job_payload(self.state, node, kind, payload)
        with self._condition:
            job_id = self.broker.enqueue(node, self.main, kind, payload, timeout)
            self._condition.notify_all()
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set():
            with self._condition:
                job = self.broker.get(job_id)
                if job["state"] in TERMINAL:
                    if kind in BULK:
                        self.broker.compact(job_id)
                    if job["state"] == "done":
                        return job["result"]
                    raise RuntimeError((job.get("result") or {}).get("error") or
                                       f"Node action {job_id} is {job['state']}; do not automatically retry.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if kind in BULK:
                        self.broker.compact(job_id)
                    action = payload.get("action", kind) if isinstance(payload, dict) else kind
                    raise TimeoutError(f"The {_label(action)} action on {_name(node, self.config)} has no confirmed "
                                       "result. It may still be running, so don't repeat it "
                                       f"(job {job_id}).")
                self._condition.wait(remaining)
        raise RuntimeError(f"Main stopped while waiting for {job_id}; action outcome is uncertain.")

    def remote_open(self, node, quality="medium", *, kind="screen", opener="console", allow_main=False):
        """Open a session on `node`: register it, then hand the node its signed start job (a
        `remote` job for kind "screen", a `camera` job for "camera"). The node answers at once
        and streams on its own. opener: "console" or "phone:<id>" (a phone's screen session
        makes the target show a notification). allow_main=True (the phone server only): the
        main itself may be the target, run in this process (_open_here)."""
        if node not in self.config.get("devices", {}) or (node == self.main and not allow_main):
            raise ValueError("Pick one of your other computers.")
        if kind not in ("screen", "camera"):
            raise ValueError("Session kind must be screen or camera.")
        if node == self.main:
            return self._open_here(quality, kind, opener)
        session = self.remote.open(node, quality, pending=True, kind=kind, opener=opener)
        # key: the session's secret, only ever in this signed job; the node checks every reply's MAC
        payload = {"op": "start", "session": session["id"], "quality": quality,
                   "key": self.remote.key(session["id"])}
        if kind == "screen" and opener != "console":
            payload["by"] = "phone"
        try:
            result = self.dispatch(node, "remote" if kind == "screen" else "camera", payload, timeout=20)
        except TimeoutError:  # dispatch's own text is about jobs; the session is closed, so retry is right
            why = (f"{_name(node, self.config)} didn't answer within 20 s. "
                   "Check it's awake, then press Connect again.")
            self.remote.close(session["id"], why)
            raise TimeoutError(why) from None
        except Exception as exc:
            self.remote.close(session["id"], str(exc))
            raise
        try:
            self.remote.touch(session["id"])  # the console waited on this request: it's watching
        except LookupError:
            pass
        if not (result or {}).get("ok"):
            why = (result or {}).get("text") or "The computer didn't start the session."
            self.remote.close(session["id"], why)
            raise RuntimeError(why)
        return self.remote.get(session["id"])

    def remote_close(self, session_id, opener=None):
        """End a session here at once; the node hears it on its next frame or input poll.
        opener: only a session that opener opened (another's reads as unknown); None: any."""
        return self.remote.close(session_id, "Disconnected.", opener=opener)

    def _open_here(self, quality, kind, opener):
        """A session on the main itself: the same code a node runs, in this process, talking to
        this hub through LocalClient. The hub's ControllerPause owns the main's Lan Mouse, so
        the screen session leaves it alone (lan_mouse=False)."""
        session = self.remote.open(self.main, quality, pending=True, kind=kind, opener=opener)
        sid = session["id"]
        payload = {"op": "start", "session": sid, "quality": quality, "key": self.remote.key(sid)}
        self._prune_here()
        with self._here_lock:
            self._here[sid] = kind
        try:
            if kind == "screen":
                from . import remote_view
                if opener != "console":
                    payload["by"] = "phone"
                result = remote_view.run(payload, self.config, self.state, client=LocalClient(self),
                                         lan_mouse=False)
            else:
                from . import camera
                result = camera.run(payload, self.config, self.state, client=LocalClient(self))
        except Exception as exc:
            with self._here_lock:
                self._here.pop(sid, None)
            self.remote.close(sid, str(exc)[:300] or "The session didn't start.")
            raise
        try:
            self.remote.touch(sid)  # the opener waited on this request: it's watching
        except LookupError:
            pass
        if not (result or {}).get("ok"):
            with self._here_lock:
                self._here.pop(sid, None)
            why = (result or {}).get("text") or "This computer didn't start the session."
            self.remote.close(sid, why)
            raise RuntimeError(why)
        return self.remote.get(sid)

    def _local_session(self, sid, kind):
        from . import camera, remote_view
        if kind != "screen":
            return camera._ACTIVE.get(sid)
        # an ended session still retrying refused ups counts: shutdown must let go of them
        return remote_view._ACTIVE.get(sid) or remote_view._LATE.get(sid)

    def _prune_here(self):
        with self._here_lock:
            for sid, kind in list(self._here.items()):
                if self._local_session(sid, kind) is None:
                    self._here.pop(sid, None)

    def _end_here(self, why, wait=3):
        """End every session whose target is the main itself, here and now: keys and buttons
        come up and ffmpeg stops before the hub goes (a node would hear it on its next poll;
        the main's own sessions die with this process, so they can't wait for one)."""
        with self._here_lock:
            here, self._here = dict(self._here), {}
        sessions = [s for s in (self._local_session(sid, kind) for sid, kind in here.items()) if s is not None]
        for session in sessions:
            session.end(why)
        for sid in here:
            try:
                self.remote.close(sid, why)  # wakes a held input poll at once
            except (LookupError, ValueError):
                pass
        for session in sessions:
            session.join(wait)

    def session_frame(self, session_id, after, opener):
        """The opener's newest frame past `after`: (seq, jpeg, width, height), or None when none
        came within the hold. Raises remote_view.Ended (gone), LookupError (unknown, or another
        opener's)."""
        return self.remote.wait_frame(session_id, after, opener=opener)

    def session_input(self, session_id, events, opener):
        """Queue the opener's input events; returns how many were queued. remote_view.WrongKind
        (a ValueError) for a camera session, LookupError for another opener's."""
        return self.remote.push(session_id, events, opener=opener)

    def sessions(self, opener):
        """Views of every session `opener` opened that the hub still knows (ended ones say why)."""
        return self.remote.views(opener)

    def session_view(self, session_id, opener):
        """One of the opener's sessions (LookupError for another opener's)."""
        return self.remote.get(session_id, opener=opener)

    def session_node_ago(self, session_id, opener=None):
        """Seconds since the target last sent a frame or keep-alive (for a 204's X-Node-Ago)."""
        return self.remote.node_ago(session_id, opener=opener)

    def end_opener(self, opener, why="That phone was signed out, so its sessions ended."):
        """End every live session one opener opened (a phone revoked or logged out)."""
        return self.remote.end_opener(opener, why)

    def target_caps(self, device_id):
        """What a session can do with one computer now: {"remote", "camera", "locate"} ->
        (ok, why). The main has no capability row: remote needs its desktop backend, camera its
        own consent, ffmpeg and a camera, and locate is always on. A node must be paired, online,
        on the main's code and enrolled for the capability (desktop/camera/locate); its own
        camera consent is only known when a start asks it."""
        def every(ok, why):
            return {k: (ok, why) for k in ("remote", "camera", "locate")}
        if device_id not in self.config.get("devices", {}):
            return every(False, "Unknown computer.")
        if device_id == self.main:
            from . import camera
            try:
                cam = camera.ready(self.config, self.state)
            except Exception as exc:  # one broken probe must not blank the device list
                cam = (False, f"Couldn't check the camera: {exc}")
            return {"remote": _screen_here(self.state), "camera": cam, "locate": (True, None)}
        status = next((n for n in self.broker.nodes() if n["id"] == device_id), None)
        if status is None:
            return every(False, f"Not paired with this computer (jarvis cluster invite {device_id}).")
        if not (status.get("online") or self.alive(device_id, status)):
            return every(False, "Offline: start JARVIS on it and check its link (jarvis cluster doctor).")
        if status.get("code") != served_code(self.state):
            return every(False, f"Runs older JARVIS code. Run: jarvis cluster update {device_id}")
        caps = set(status.get("capabilities") or ())
        grant = (f" Grant it on the main: jarvis cluster caps {device_id} --capability ... (list every "
                 "capability it should keep; the command replaces the set).")
        return {"remote": (True, None) if "desktop" in caps else (False, "Not enrolled for desktop actions." + grant),
                "camera": (True, None) if "camera" in caps else (False, "Not enrolled for the camera." + grant),
                "locate": (True, None) if "locate" in caps else (False, "Not enrolled for locate." + grant)}

    def session_call(self, node, path, data, caps=None):
        """A computer's request about its own live session: /v1/remote/frame (a frame, a
        keep-alive, or {"end": why}) or /v1/remote/input (its input poll). Answered here for both
        callers: the HTTP handler (a node; caps = its capabilities) and LocalClient (the main
        itself; caps None, it has no capability row). The capability must fit the session's
        kind: desktop for a screen session, camera for a camera session."""
        import base64
        if not isinstance(data, dict):
            raise ValueError("Cluster body must be an object.")
        sid = data.get("session")
        if caps is not None:
            if not caps & {"desktop", "camera"}:
                raise PermissionError("This node isn't enrolled for desktop actions.")
            if self.remote.kind(node, sid) == "camera":
                if "camera" not in caps:
                    raise PermissionError("This node isn't enrolled for the camera.")
            elif "desktop" not in caps:
                raise PermissionError("This node isn't enrolled for desktop actions.")
        if path == "/v1/remote/frame" and data.get("end") is not None:
            return self.remote.node_end(node, sid, data["end"])
        if path == "/v1/remote/input":
            return self.remote.seal(sid, "input", self.remote.take_input(
                node, sid, data.get("after", 0), stop_event=self.stop_event))
        if path != "/v1/remote/frame":
            raise LookupError("Unknown session endpoint.")
        jpeg = None
        if data.get("jpeg") is not None:
            if not isinstance(data["jpeg"], str):
                raise ValueError("Frame must be base64 text.")
            jpeg = base64.b64decode(data["jpeg"], validate=True)
        return self.remote.seal(sid, "frame", self.remote.frame(
            node, sid, jpeg, data.get("width"), data.get("height")))

    def _linkfinder(self):
        from .linkfinder import main_tick
        while not self.stop_event.is_set():
            try:
                from .cluster_setup import addresses
                self._addresses = addresses(self.config, self.main)
                main_tick(self)
            except Exception as exc:
                print(f"JARVIS: main discovery: {exc}", flush=True)
            self.stop_event.wait(10)

    def _offline_alerts(self):
        from .linkfinder import OfflineAlerts
        from .confirm import notify
        alerts = OfflineAlerts()
        while not self.stop_event.is_set():
            try:
                from .linkfinder import role
                _, local, main = role(self.config, self.state)
                if local != main:  # a node: nothing to alert on, but it may be promoted later
                    self.stop_event.wait(10)
                    continue
                alerts.check(self.broker.nodes(), time.monotonic(), notify)
            except Exception as exc:
                print(f"JARVIS: offline monitor: {exc}", flush=True)
            self.stop_event.wait(10)

    def _asks(self):
        while not self.stop_event.is_set():
            try:
                self._ask_once()
            except Exception as exc:  # e.g. sqlite "database is locked": keep serving asks
                print(f"JARVIS: ask worker error: {exc}", flush=True)
                self.stop_event.wait(1)

    def _ask_once(self):
        with self._condition:
            job = self.broker.claim(self.main, {"ask"})
            if not job and not self.stop_event.is_set():
                self._condition.wait()
                return
        if not job:
            return
        try:
            with request_from(job["requester"]):
                if self.ask_handler:
                    result = self.ask_handler(job["payload"], job["requester"])
                else:
                    result = self._console_ask(job)
            self.broker.finish(job["id"], self.main, result)
            with self._condition:
                self._condition.notify_all()
        except Exception as exc:
            try:
                self.broker.finish(job["id"], self.main, {"error": str(exc)}, failed=True)
                with self._condition:
                    self._condition.notify_all()
            except (LookupError, ValueError):
                pass  # shutdown/recovery made this outcome explicitly uncertain

    def _console_ask(self, job):
        if self.console is None:
            raise RuntimeError("The main console runtime is not available.")
        payload = job["payload"]
        while True:
            if self.stop_event.is_set() or time.time() >= job["deadline"]:
                raise TimeoutError("Main PC request expired before a model run could start.")
            try:
                turn = self.console.submit(payload["text"], payload.get("tier"),
                            origin_device=job["requester"], write=payload.get("write", False))
                break
            except ValueError as exc:
                if "still on the last request" not in str(exc):
                    raise
                self.stop_event.wait(0.2)
        while turn["state"] == "running":
            if self.stop_event.wait(0.1) or time.time() >= job["deadline"]:
                raise TimeoutError("Request may still be running on the main PC; inspect it before retrying.")
        if turn["state"] != "done":
            raise RuntimeError(turn.get("error") or "Main PC request failed.")
        return turn["result"]

    def start(self, host=None, port=None):
        from .locking import exclusive_lock
        settings = self.config.get("cluster", {})
        host = host or settings.get("listen_host", "127.0.0.1")
        port = int(settings.get("listen_port", DEFAULT_PORT) if port is None else port)
        cert, key = settings.get("tls_cert"), settings.get("tls_key")
        if host not in ("127.0.0.1", "localhost", "::1") and not (cert and key):
            raise ValueError("A network-facing main endpoint requires cluster.tls_cert and tls_key; or use SSH forwarding.")
        self._lock = exclusive_lock(self.state / "cluster-main.lock")
        try:
            self._lock.__enter__()
        except BaseException:
            self._lock = None
            raise RuntimeError("The main runtime is already running for this state directory.") from None
        if getattr(self, "_pause", None) is not None:
            # A pause the last hub left behind (crash, restart) ends now. Only after the lock:
            # a second hub that is about to fail must not adopt and end a live session's pause.
            self._pause(False)
        try:  # the last main process died mid-session on this computer itself: let go of its keys
            from .remote_view import recover
            recover(self.state)
        except Exception as exc:  # never keeps the hub down
            print(f"JARVIS: remote-control recovery: {exc}", flush=True)
        _refresh_clipboard(self.state, self.config)  # the hub restarts with every code change here
        try:
            self.server = make_server(self, host, port)
            if cert or key:
                if not (cert and key):
                    raise ValueError("Set both cluster.tls_cert and cluster.tls_key.")
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(cert, key)
                # Handshake in the request thread (ui.ConsoleServer), not in accept(): one
                # silent client must not stall the whole hub.
                self.server.socket = context.wrap_socket(self.server.socket, server_side=True,
                                                         do_handshake_on_connect=False)
            self.broker.recover()
            _HUBS[str(self.state)] = self
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.worker = threading.Thread(target=self._asks, daemon=True)
            self.thread.start()
            self.worker.start()
            threading.Thread(target=self._offline_alerts, name="node-offline-alerts", daemon=True).start()
            self.started = True
            from . import _under_tests
            if not _under_tests():
                threading.Thread(target=self._linkfinder, name="main-linkfinder", daemon=True).start()
            if not getattr(self, "continuity_standby", False):
                # Build managed adapters with the real Hub context before the renewer
                # starts. Owner-only coordinator methods stay gated by owner proof;
                # an installation key alone is never promoted to owner.
                from .continuity.startup import bind_managed
                bind_managed(self.config, self.state, self.base, hub=self)
                from .continuity.renewal import start_bound
                self._continuity_renewer = start_bound(self.state, self.config, self.base)
            if settings.get("auto_update", True):
                threading.Thread(target=self._keep_up, name="jarvis-auto-update",
                                 daemon=True).start()
        except BaseException:
            if self.server:
                self.server.server_close()
            self._lock.__exit__(None, None, None)
            self._lock = None
            raise
        return self

    def close(self):
        if getattr(self, "_here", None):  # first: the main's own sessions let go while the hub answers
            self._end_here("JARVIS on the main stopped.")
        self.stop_event.set()
        renewer = getattr(self, "_continuity_renewer", None)
        if renewer is not None:
            renewer.close()
        self.remote.close_all("JARVIS on the main stopped.")  # nodes let go on their next poll
        with self._condition:
            self._condition.notify_all()
        if self.server and self.thread:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
        if self.worker:
            self.worker.join(timeout=2)
        if self.started:
            self.broker.recover()
            _HUBS.pop(str(self.state), None)
            self.started = False
        if self._lock:
            self._lock.__exit__(None, None, None)
            self._lock = None


def hub_hosts(bound, config):
    """Names the hub will answer as Host. Loopback, the bind address, and the hostname
    in cluster.main_url. Binding 0.0.0.0 does not allow every Host header."""
    allowed = {"127.0.0.1", "localhost", "::1"}
    if isinstance(bound, str) and bound.strip():
        allowed.add(bound.strip().lower())
    url = str(((config or {}).get("cluster") or {}).get("main_url") or "")
    if url:
        name = urlsplit(url).hostname
        if name:
            allowed.add(name.lower())
    return tuple(sorted(allowed))


def _remote_pinned_host_match(host, main_url):
    """Match a non-loopback Host only to the exact pinned URL authority."""
    pinned = urlsplit(str(main_url or ""))
    if not pinned.hostname or pinned.hostname.lower() in {"127.0.0.1", "localhost", "::1"}:
        return None
    try:
        incoming = urlsplit("//" + str(host))
        actual_port = incoming.port
    except ValueError:
        return False
    if (not incoming.hostname or incoming.hostname.lower() != pinned.hostname.lower()
            or incoming.username is not None or incoming.password is not None
            or incoming.path or incoming.query or incoming.fragment):
        return None
    expected = pinned.port or (443 if pinned.scheme == "https" else 80)
    actual = actual_port or (443 if pinned.scheme == "https" else 80)
    return actual == expected


def make_server(hub, host, port):
    from .ui import ConsoleServer, Handler

    class MainHandler(Handler):
        console = hub.console

        def _host_ok(self):
            found = self._header_all("Host")
            if len(found) != 1:
                return False
            match = _remote_pinned_host_match(
                found[0], (hub.config.get("cluster") or {}).get("main_url"))
            if match is not None:
                return match
            return Handler._host_ok(self)

        def _allowed(self):
            try:
                self.node_id, self.capabilities = hub.authenticate(
                    self.headers, "console", self.client_address[0])
                return True
            except AuthBackoffError as exc:
                # Unsupported methods call _refuse after a False here. One response only.
                self._send(429, {"error": "Too many failed authentications."}, close=True,
                           headers={"Retry-After": str(exc.retry_after)})
                self._backoff_sent = True
                return False
            except PermissionError:
                return False

        def _refuse(self):
            if getattr(self, "_backoff_sent", False):
                length = self._content_length()
                self.close_connection = True
                if length:
                    self._drain(length, self.REFUSE_DRAIN_BYTES)
                return None
            return Handler._refuse(self)

        def do_GET(self):
            self._request(False)

        def do_POST(self):
            self._request(True)

        _node_path = None

        def _send(self, status, body, *args, **kwargs):
            # A node's answer: top-level fields only (see NODE_API_FIELDS); errors pass through.
            if self._node_path and status == 200 and isinstance(body, dict):
                body = node_fields(self._node_path, body)
            try:
                result = super()._send(status, body, *args, **kwargs)
            except BaseException:
                response_context = getattr(hub, "_response_context", None)
                request = None if response_context is None else getattr(response_context, "request", None)
                if request is not None:
                    with hub._active_responses_lock:
                        request["written"] = False
                        hub._active_responses.pop(id(request), None)
                        hub._orphaned_response_callbacks.extend(request["callbacks"])
                        request["callbacks"] = []
                    response_context.request = None
                raise
            response_context = getattr(hub, "_response_context", None)
            request = None if response_context is None else getattr(response_context, "request", None)
            pending = []
            if request is not None:
                with hub._active_responses_lock:
                    request["written"] = True
                    pending, request["callbacks"] = request["callbacks"], []
                    hub._active_responses.pop(id(request), None)
                response_context.request = None
            for callback in pending or ():
                try:
                    callback()
                except Exception as exc:
                    print(f"JARVIS deferred role action failed: {type(exc).__name__}", flush=True)
            return result

        def _request(self, post):
            request = {"callbacks": [], "written": False, "role_transition": False}
            response_context = getattr(hub, "_response_context", None)
            if response_context is not None:
                response_context.request = request
                with hub._active_responses_lock:
                    hub._active_responses[id(request)] = request
            try:
                self._node_path = None  # one handler serves a whole keep-alive connection
                if not Handler._allowed(self):  # this class's _allowed is the console credential
                    site = self.headers.get("Sec-Fetch-Site")
                    detail = ("Unexpected Host header."
                              if site in (None, "same-origin", "none")
                              else "Requests from other sites are refused.")
                    return self._send(403, {"error": detail})
                if self.path.startswith("/api/"):
                    self.node_id, caps = hub.authenticate(
                        self.headers, "console", self.client_address[0])
                    if "owner" not in caps:
                        path = urlparse(self.path).path
                        node_api(path, post, caps)
                        if not post and path.startswith("/api/jobs/"):
                            node_job(hub.console, self.node_id, path)
                        self._node_path = None if post else path
                    with request_from(self.node_id):
                        return Handler.do_POST(self) if post else Handler.do_GET(self)
                node, caps = hub.authenticate(self.headers, source=self.client_address[0])
                request["role_transition"] = (post and "owner" in caps
                                                and self.path == "/v1/continuity/main")
                if request["role_transition"]:
                    response_lock = getattr(hub, "_active_responses_lock", None)
                    if response_lock is not None:
                        with response_lock:
                            orphaned = getattr(hub, "_orphaned_response_callbacks", [])
                            if orphaned:
                                request["callbacks"].extend(orphaned)
                                orphaned.clear()
                if self.path == "/v1/status" and not post:
                    return self._send(200, {"main_device": hub.main, "code": served_code(hub.state),
                                            "nodes": hub.broker.nodes()})
                if self.path.startswith("/v1/code/chunk?") and not post and "owner" not in caps:
                    from .release_runtime import serve_chunk
                    return self._send(200, serve_chunk(hub, parse_qs(urlparse(self.path).query)))
                if self.path == "/v1/code" and not post and "owner" not in caps:
                    from .deploy import code_bundle  # nodes pull the main's code over their own channel
                    return self._send(200, code_bundle(hub.state))
                if self.path.startswith("/v1/jobs/") and not post:
                    # ?wait=N holds the request until the job is terminal or N seconds pass
                    # (<= POLL_WAIT), so a requester needn't poll; no wait answers at once.
                    url = urlparse(self.path)
                    try:
                        hold = min(POLL_WAIT, max(0, float(parse_qs(url.query).get("wait", ["0"])[0])))
                    except ValueError:
                        hold = 0
                    who = None if "owner" in caps else node
                    until = time.monotonic() + hold
                    while True:
                        job = hub.broker.get(url.path.rsplit("/", 1)[-1], who)
                        remaining = until - time.monotonic()
                        if job["state"] in TERMINAL or remaining <= 0 or hub.stop_event.is_set():
                            return self._send(200, job)
                        with hub._condition:  # woken by results; the slice catches lazy expiry
                            hub._condition.wait(min(1, remaining))
                if not post:
                    return self._send(404, {"error": "Unknown cluster endpoint."})
                length = self._content_length()
                # Results, and file chunks on their way to a node (BULK jobs), may be large.
                bulk = ("/v1/result", "/v1/action", "/v1/control", "/v1/remote/frame")
                if length is None or not 0 < length <= (MAX_RESULT if self.path in bulk else MAX_PROTOCOL):
                    raise ValueError("Invalid cluster request length.")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Cluster body must be an object.")
                if self.path == "/v1/link-health" and "owner" not in caps:
                    from .cluster_recovery import signature
                    return self._send(200, {"signature": signature(
                        self.headers.get("Authorization", "")[7:], hub.main, node, data.get("nonce"), [])})
                if self.path in ("/v1/action", "/v1/control") and data.get("kind") == "transfer":
                    from .continuity.brain_channel import ACTIONS as brain_actions
                    payload = data.get("payload")
                    if (isinstance(payload, dict) and payload.get("action") in brain_actions
                            and "owner" not in caps):
                        raise PermissionError("Brain transfer operations require the owner credential.")
                if self.path == "/v1/continuity/endpoint":
                    if "files" not in caps or "owner" in caps or set(data) != {"nonce"}:
                        raise PermissionError("An enrolled target credential is required.")
                    from .continuity.repoint import endpoint_receipt
                    from .continuity.runtime import RuntimeRefusal
                    try:
                        return self._send(200, endpoint_receipt(hub, node, data['nonce']))
                    except RuntimeRefusal as exc:
                        return self._send(409, {'error': exc.code})
                if self.path == "/v1/continuity/recover":
                    if "owner" not in caps:
                        raise PermissionError("Recovery requires the local owner credential.")
                    from .continuity.hub_control import run_recover
                    return self._send(200, run_recover(hub, data, self.client_address[0]))
                if self.path == "/v1/continuity/main":
                    if "owner" not in caps:
                        raise PermissionError("MAIN transitions require the local owner credential.")
                    from .continuity.hub_control import run_main
                    return self._send(200, run_main(hub, data, self.client_address[0]))
                if self.path == "/v1/redeem" and "owner" not in caps:
                    return self._send(200, hub.redeem(node, data.get("public_key"), data.get("token")))
                if self.path == "/v1/poll" and "owner" not in caps:
                    hub.broker.heartbeat(node, data)
                    kinds = caps & {"desktop", "inventory", "grokbot", "camera", "locate"}
                    if "desktop" in caps:
                        kinds |= {"remote"}
                    if "files" in caps:
                        kinds |= {"storage", "transfer", "continuity"}
                    deadline = time.monotonic() + (2 if data.get("code") != served_code(hub.state) else POLL_WAIT)
                    with hub._condition:
                        hub._polling.setdefault(node, set()).add(self.connection)
                        try:
                            job = hub.broker.claim(node, kinds)
                            while job is None and not hub.stop_event.is_set():
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    break
                                hub._condition.wait(remaining)
                                if not _connected(self.connection):
                                    break  # the node hung up: a job claimed now would be lost
                                job = hub.broker.claim(node, kinds)
                        finally:
                            hub._polling[node].discard(self.connection)
                    if job is not None:
                        from .deploy import sign_job
                        try:
                            job = sign_job(job, node, hub.state)  # the node runs only what verifies
                        except Exception as exc:  # don't leave the job running and the node busy
                            with hub._condition:
                                try:
                                    hub.broker.finish(job["id"], node, {"error": f"couldn't sign the job: {exc}"},
                                                      failed=True)
                                except (LookupError, ValueError):
                                    pass
                                hub._condition.notify_all()
                            raise
                    from .linkfinder import offer
                    return self._send(200, {"job": job, "main_device": hub.main, "code_chunks": True,
                        "code": served_code(hub.state), "addresses": offer(hub.config, hub.main, node,
                            self.headers.get("Authorization", "")[7:], data.get("address_nonce"), hub._addresses)})
                if self.path in ("/v1/remote/frame", "/v1/remote/input") and "owner" not in caps:
                    if not caps & {"desktop", "camera"}:  # before any session lookup: no id oracle
                        raise PermissionError("This node isn't enrolled for desktop actions.")
                    return self._send(200, hub.session_call(node, self.path, data, caps))
                if self.path == "/v1/result" and "owner" not in caps:
                    with hub._condition:
                        hub.broker.finish(data.get("id"), node, data.get("result"), bool(data.get("failed")))
                        hub.broker.touch(node)
                        hub._condition.notify_all()
                    return self._send(200, {"accepted": True})
                if ((self.path == "/v1/action" and "owner" in caps) or
                        (self.path == "/v1/control" and "console" in caps) or
                        # A node with "files" may hand the main a file or a clipboard copy
                        # (jarvis send / clipboard sync), but nothing else.
                        (self.path == "/v1/control" and "files" in caps and
                         data.get("kind") == "transfer" and data.get("node") == hub.main)):
                    # Sessions and locating start only in-process (the console and the phone
                    # server call the hub directly): never over HTTP, not even with the owner token.
                    kind = data.get("kind")
                    if kind == "remote":
                        raise PermissionError("Only the main's console can start remote control.")
                    if kind in ("camera", "locate"):
                        raise PermissionError(IN_PROCESS_ONLY[kind])
                    if self.path == "/v1/control" and "owner" not in caps:
                        # A node is never the owner: it may not run things on the main (only
                        # hand it a transfer) nor start a shell anywhere.
                        if data.get("node") == hub.main and data.get("kind") != "transfer":
                            raise PermissionError("Nodes can't run actions on the main PC.")
                        payload = data.get("payload")
                        if "console" not in caps and data.get("kind") == "transfer" \
                                and isinstance(payload, dict):
                            from .transfer import is_path_op
                            if is_path_op(payload):  # storage copies are the main console's, not a node's
                                raise PermissionError("Nodes can't read or write folders on the main PC.")
                        if (data.get("kind") == "desktop" and isinstance(payload, dict)
                                and payload.get("action") == "terminal"):
                            raise PermissionError("Nodes can't run terminal commands.")
                    timeout = data.get("timeout", 45)
                    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
                        raise ValueError("Action timeout must be 1-1800 seconds.")
                    return self._send(200, hub.dispatch(data.get("node"), data.get("kind"),
                                        data.get("payload"), timeout=timeout))
                if self.path == "/v1/ask" and "ask" in caps:
                    text, tier = data.get("text"), data.get("tier")
                    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                        raise ValueError("A request needs 1-20000 characters of text.")
                    if tier not in (None, "simple", "medium", "complex"):
                        raise ValueError("Unknown request tier.")
                    timeout = data.get("timeout", 600)
                    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
                        raise ValueError("Request timeout must be 1-1800 seconds.")
                    if data.get("write") and "owner" not in caps:
                        # A write run changes code that then deploys to every node.
                        raise PermissionError("Write runs start only on the main PC.")
                    payload = {"text": text.strip(), "tier": tier, "write": bool(data.get("write"))}
                    with hub._condition:
                        job_id = hub.broker.enqueue(hub.main, node, "ask", payload, timeout,
                                                   data.get("request_id"))
                        hub._condition.notify_all()
                    return self._send(202, {"id": job_id})
                raise PermissionError("This node cannot use that endpoint.")
            except NodeOffline as exc:
                self._send(409, {"error": str(exc), "code": "node_offline"})
            except AuthBackoffError as exc:
                self._send(429, {"error": "Too many failed authentications."},
                           headers={"Retry-After": str(exc.retry_after)})
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
            except LookupError as exc:
                self._send(404, {"error": str(exc)})
            except OSError:
                self._send(400, {"error": "Cluster request failed."})
            except (ValueError, TypeError, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})

    MainHandler.hosts = hub_hosts(host, hub.config)

    class HubServer(ConsoleServer):
        def finish_request(self, request, client_address):
            # TLS handshake here, in the per-request thread and with a deadline (see Hub.start).
            if isinstance(request, ssl.SSLSocket):
                request.settimeout(HANDSHAKE_TIMEOUT)
                request.do_handshake()  # raises (and the request is dropped) on a stalled client
                request.settimeout(None)
            super().finish_request(request, client_address)

    return HubServer((host, port), MainHandler)


def dispatch(config, state, node, kind, payload, timeout=45):
    local, main = identity(config)
    if local != main:
        raise ValueError("Node agents cannot route directly to other nodes; submit the request to the main PC.")
    hub = _HUBS.get(str(Path(state).resolve()))
    if hub:
        return hub.dispatch(node, kind, payload, timeout)
    return Client(config, state, owner=True).request("/v1/action",
               {"node": node, "kind": kind, "payload": payload, "timeout": timeout},
               timeout=timeout + 5)


class ProxyLane:
    """Voice callers keep their local microphone/speaker; cognition stays on the main."""
    def __init__(self, config, state):
        self.config, self.state = config, Path(state)

    def warm(self):
        pass

    def close(self):
        pass


class NodeWorker:
    def _speaks(self):
        """'no' when this node's JARVIS is muted (only the main talks), shown in cluster status."""
        from .speech import may_speak
        return "yes" if may_speak(self.config) else "no"

    def __init__(self, config, state, executor=None, config_path=None):
        from .linkfinder import role
        config, _, _ = role(config, state)
        if not is_node(config):
            raise ValueError("Run the node agent on a PC other than the configured main.")
        self.config, self.state = config, Path(state).resolve()
        self.client = Client(config, state)
        self.config_path = config_path or Path(__file__).resolve().parent.parent / "jarvis.config.json"
        self.executor = executor or (lambda kind, payload: execute_local(kind, payload, config, state))
        self._acts_locally = executor is None  # a test's stand-in executor: don't touch real Lan Mouse
        self.stop_event = threading.Event()
        self.outbox = self.state / "cluster-outbox.json"
        self.thread = None
        self.restarting = False  # set once new code is installed; run() then re-execs
        # The code this process loaded. Files changed since (an unrestarted update) must
        # not make the node claim code it isn't running.
        self.code = code_fingerprint()
        # Link health for `cluster doctor`: polls that reached the main or didn't, and
        # results that needed resending from the outbox.
        # outages: separate runs of failed polls. The main restarting fails a few polls in a
        # row once; a bad link fails them again and again.
        self.stats = {"started": time.time(), "polls_ok": 0, "polls_failed": 0, "outages": 0,
                      "results_sent": 0, "results_resent": 0, "last_ok": None, "last_error": None}
        self._down = False
        self._process_registration = None
        self._cooperative_request = None
        self.cooperative_stop = False

    def _register_local_process(self):
        from .fsio import atomic_write
        config = Path(self.config_path).expanduser().resolve(strict=True)
        record = {"schema": _PROCESS_SCHEMA, "kind": "node-worker", "pid": os.getpid(),
                  "nonce": secrets.token_hex(24), "config": str(config), "state": str(self.state)}
        path = self.state / _PROCESS_RECORD
        if path.is_symlink():
            raise RuntimeError("unsafe local process registration")
        atomic_write(path, json.dumps(record, sort_keys=True), mode=0o600, fsync=True)
        self._process_registration = record
        return record

    def _cooperative_stop_requested(self):
        registration = self._process_registration
        if registration is None:
            return False
        request = _local_record(self.state / _STOP_REQUEST)
        keys = {"schema", "kind", "pid", "nonce", "config", "state", "request_id"}
        if not isinstance(request, dict) or set(request) != keys:
            return False
        if (any(request.get(key) != registration.get(key) for key in
                ("schema", "kind", "pid", "nonce", "config", "state"))
                or not isinstance(request.get("request_id"), str)
                or len(request["request_id"]) != 32
                or any(c not in "0123456789abcdef" for c in request["request_id"])):
            return False
        self._cooperative_request = request
        self.cooperative_stop = True
        self.stop_event.set()
        return True

    def _finish_local_process(self):
        registration = self._process_registration
        request = self._cooperative_request
        if registration is None:
            return
        registration_path = self.state / _PROCESS_RECORD
        current = _local_record(registration_path)
        if current == registration and not registration_path.is_symlink():
            registration_path.unlink()
        if request is not None:
            from .fsio import atomic_write
            ack = {**request, "status": "stopped"}
            path = self.state / _STOP_ACK
            if path.is_symlink():
                return
            atomic_write(path, json.dumps(ack, sort_keys=True), mode=0o600, fsync=True)
            pending_path = self.state / _STOP_REQUEST
            if _local_record(pending_path) == request and not pending_path.is_symlink():
                pending_path.unlink()

    def _record(self, **change):
        for key, value in change.items():
            self.stats[key] = self.stats[key] + value if isinstance(value, int) else value
        try:
            save_json(self.state / "cluster-link-stats.json", self.stats)
        except OSError:
            pass  # health notes must never stop the node

    def _send_result(self, message):
        """Deliver a saved result. Only the main's final answer (unknown or no-longer-running
        job: HTTP 400/404) drops it; connection errors, timeouts and 5xx keep it for retry."""
        try:
            self.client.request("/v1/result", message)
        except HubRefused as exc:
            if exc.status not in (400, 404):
                raise
            print(f"JARVIS node: main rejected result {message.get('id')} for good ({exc}); dropped.", flush=True)
        self.outbox.unlink(missing_ok=True)

    def _can_pull(self):
        """This node pulls signed releases itself (it pinned the main's code key), so the main
        needn't push them over SSH too: both at once raced over the same folders."""
        try:
            return bool(json.loads((self.state / "cluster-node.json").read_text(encoding="utf-8")).get("code_key"))
        except (OSError, ValueError, AttributeError):
            return False

    def _pull_update(self, want):
        """The main runs other code: fetch, verify and install it, then restart. Tried once
        per target fingerprint per 10 minutes so a bad package can't loop the node, and never
        again once that code failed to start here and was rolled back (release.guard)."""
        from .deploy import apply_bundle
        from .release_runtime import busy, fetch
        if busy(self.state):
            return False
        from .release import backup, is_bad
        if is_bad(self.state, want):
            return False
        mark = self.state / "cluster-update.json"
        last = read_json(mark, {})
        if last.get("code") == want and time.time() - last.get("at", 0) < 15:
            return False
        save_json(mark, {"code": want, "at": time.time()})
        try:  # the main's code-signing key, pinned from the invite (cluster_setup.join)
            key = json.loads((self.state / "cluster-node.json").read_text(encoding="utf-8")).get("code_key")
        except (OSError, ValueError):
            key = None
        if not key:
            return False  # apply_bundle would refuse an unpinned bundle; don't fetch and back up for it
        if getattr(self, '_chunked_release', False):
            bundle = fetch(self.client, self.state, want)
        else:
            bundle = self.client.request("/v1/code", timeout=60)
        if busy(self.state):
            return False
        backup(self.state)  # what the watchdog restores if the new code won't start
        count = apply_bundle(bundle, key, state=self.state)  # installs changed requirements before its start check
        self.installed = bundle.get("code")
        print(f"JARVIS node: installed main's code {bundle.get('code')} ({count} files); restarting.", flush=True)
        return True

    def _rejoined(self):
        """`jarvis cluster join` wrote a new credential (or main) since this process loaded
        its own: polling on with the stale one only fails. Our own redeem updates
        client.token too, so that never counts."""
        try:
            note = json.loads((self.state / "cluster-node.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (isinstance(note.get("token"), str) and note["token"] != self.client.token) or \
            note.get("main_device", self.client.main) != self.client.main

    def once(self):
        # A crash after execution can resend the result; it never reexecutes the action.
        if self.outbox.exists():
            note = json.loads(self.outbox.read_text(encoding="utf-8"))
            if note.get("main_device") != self.client.main:
                # Never the new main's job: set it aside so polling goes on.
                old = str(note.get("main_device") or "unknown").replace("/", "_")
                aside = self.outbox.with_name(f"{self.outbox.stem}.{old}{self.outbox.suffix}")
                self.outbox.replace(aside)
                print(f"JARVIS node: unsent result belonged to previous main {old}; moved to {aside.name}.", flush=True)
            else:
                self._send_result(note["message"])
                self._record(results_resent=1)
        nonce = secrets.token_hex(24)
        body = {"address_nonce": nonce, "hostname": platform.node(),
                "platform": platform.system(), "version": __version__,
                "code": self.code, "pulls": "yes" if self._can_pull() else "no",
                "speaks": self._speaks()}
        try:
            from .hostmeter import cached_sample
            reading = cached_sample()
            if isinstance(reading, dict):
                body["meters"] = reading
        except Exception:
            pass  # a meter failure must not stop the node from polling
        try:
            response = self.client.request("/v1/poll", body, timeout=POLL_WAIT + 10)
        except (OSError, ValueError, RuntimeError) as exc:
            self._record(polls_failed=1, outages=0 if self._down else 1,
                         last_error=f"{time.strftime('%H:%M:%S')} {exc}"[:300])
            self._down = True
            raise
        if not isinstance(response, dict) or not isinstance(response.get("job") or {}, dict):
            # Not the main's shape (something squatting the tunnel port): fail this poll
            # instead of crashing the agent on job.get (audit 2026-11-07).
            self._record(polls_failed=1, last_error=f"{time.strftime('%H:%M:%S')} malformed poll reply")
            raise ValueError("The main sent a malformed poll reply; ignored.")
        from .linkfinder import learn
        learn(self.client, response, nonce)
        self._record(polls_ok=1, last_ok=time.time())
        self._down = False
        if not getattr(self, "_confirmed", False):
            from .release import confirm
            confirm(self.state, self.code)  # this code starts and reaches the main: keep it
            self._confirmed = True
        received = time.monotonic()
        self._chunked_release = response.get("code_chunks") is True
        want = response.get("code")
        if want and want != self.code:
            job = response.get("job")
            try:
                if self._pull_update(want):
                    self.restarting = True
                    if job:
                        # This poll also claimed a job, built for code we're about to replace.
                        # Say so at once (not run, safe to ask again) instead of dropping it.
                        message = {"id": job["id"], "failed": True, "result": {
                            "error": "Not run: this node was updating its code. Try again in a minute."}}
                        save_json(self.outbox, {"main_device": self.client.main, "message": message})
                        self._send_result(message)
                    return False
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"JARVIS node: code update failed: {exc}", flush=True)
            if job and not _read_only(job.get("kind"), job.get("payload") or {}):
                # No update happened, but the main built this job for different code: only
                # read-only jobs are safe to run on ours.
                message = {"id": job["id"], "failed": True, "result": {
                    "error": "Not run: this node's code differs from the main's. Try again in a minute."}}
                save_json(self.outbox, {"main_device": self.client.main, "message": message})
                self._send_result(message)
                self._record(results_sent=1)
                return True
        job = response.get("job")
        if not job:
            return False
        refusal = self._untrusted(job)
        if refusal:  # never ran: report it if this really is the main, else nobody listens
            print(f"JARVIS node: refused job {str(job.get('id'))[:40]}: {refusal}", flush=True)
            if isinstance(job.get("id"), str):
                message = {"id": job["id"], "failed": True,
                           "result": {"error": f"Not run: the node refused it ({refusal})."}}
                self._send_result(message)
            return True
        try:
            # Time the job on our own clock: the main's wall clock may differ from ours.
            if "remaining" in job:
                expired = received + job["remaining"] <= time.monotonic()
            else:
                expired = time.time() >= job["deadline"]
            if expired:
                raise TimeoutError("Node action expired before execution.")
        except Exception as exc:  # nothing ran yet: a plain failure
            message = {"id": job["id"], "result": {"error": str(exc)}, "failed": True}
        else:
            try:
                from .continuity.integration import (
                    IntegrationError, managed_node_disposition, run_admitted_node_job)
            except ImportError:
                IntegrationError = None
                managed_node_disposition = None
                run_admitted_node_job = None
            disposition = "legacy"
            if managed_node_disposition is not None:
                # After the code-key check above. A payload boolean cannot admit this job.
                disposition = managed_node_disposition(self.state, job)
            if job['kind'] == 'continuity':
                # _untrusted already checked the pinned code-key signature. RPCs
                # check their own live transition instead of an ordinary ticket.
                self._mark_seen(job['id'])
                try:
                    from .continuity.participant import execute_job
                    result = execute_job(self.state, job['payload'], job['requester'])
                    message = {'id': job['id'], 'failed': False, 'result': result}
                except Exception as exc:
                    message = {'id': job['id'], 'failed': True, 'result': {
                        'error': 'Continuity callback refused', 'code': getattr(exc, 'code', 'callback_failed')}}
            elif disposition == "refuse":
                message = {"id": job["id"], "failed": True, "result": {
                    "error": "Not run: managed continuity admission is missing."}}
            elif disposition == "admit":
                try:
                    from .continuity.authority import AuthorityError
                except ImportError:
                    AuthorityError = None
                try:
                    result = run_admitted_node_job(self.state, job, self.executor)
                except IntegrationError as exc:
                    # Refused before the effect, including a guard that returned without running.
                    # The same id can be repaired and offered again.
                    message = {"id": job["id"], "failed": True, "result": {"error": f"Not run: {exc.code}"}}
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    ambiguous = not (AuthorityError is not None and isinstance(exc, AuthorityError)
                                     and code not in ("uncertain", "crash"))
                    if ambiguous:
                        self._mark_seen(job["id"])
                        message = {"id": job["id"], "failed": True, "result": {
                            "error": f"{exc} (the node crashed mid-action; it may have run, so it wasn't retried)"}}
                    else:
                        message = {"id": job["id"], "failed": True, "result": {"error": f"Not run: {code}"}}
                else:
                    message = {"id": job["id"], "result": result, "failed": False}
                    self._mark_seen(job["id"])
            else:
                try:
                    result = self.executor(job["kind"], job["payload"])
                    message = {"id": job["id"], "result": result, "failed": False}
                    try:
                        from .continuity.integration import node_seen_mode
                        if node_seen_mode() == "after":
                            self._mark_seen(job["id"])
                    except ImportError:
                        pass
                except Exception as exc:
                    # The executor started, so part of the action may already have happened.
                    message = {"id": job["id"], "failed": True, "result": {
                        "error": f"{exc} (the node crashed mid-action; it may have run, so it wasn't retried)"}}
        if len(json.dumps(message).encode()) > MAX_RESULT - 1000:
            # The main answers an oversized body 400 and the result would be dropped unseen.
            message = {"id": message["id"], "failed": True, "result": {
                "error": "result too large to send back (it may have run)"}}
        save_json(self.outbox, {"main_device": self.client.main, "message": message})
        self._send_result(message)
        self._record(results_sent=1)
        return True

    def _untrusted(self, job):
        """Why a polled job must not run, or None. Each job must carry the main's signature for
        this node (made with the code key the node pinned at join), be fresh, and not have run
        before (a captured job replayed by a port squatter). A node with no pinned key refuses."""
        pinned = read_json(self.state / 'cluster-node.json', {})
        key, me = pinned.get("code_key"), pinned.get("node_id")
        if not key:
            # Paired before code signing existed: nothing to check a job against, so it can't
            # be told from one a port squatter made up. Fail closed.
            return ("this node never pinned the main's signing key (paired before job signing); "
                    "re-pair it: `jarvis cluster invite <node>` on the main, then `jarvis cluster join "
                    "<code>` on the node")
        from .deploy import verify_job
        why = verify_job(job, me, key)
        if why:
            return why
        seen_path = self.state / "cluster-jobs-seen.json"
        seen = read_json(seen_path, {})
        now = time.time()
        seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and now - v < 3600}
        if job["id"] in seen:
            return "already run once (replayed)"
        try:
            from .continuity.integration import IntegrationError, continuity_state, node_seen_mode
            after = node_seen_mode() == "after"
        except ImportError:
            after = False
            continuity_state = None
            IntegrationError = None
        managed = False
        if continuity_state is not None:
            try:
                managed = continuity_state(self.state) == "managed"
            except IntegrationError:
                return "continuity state is quarantined"
        if after or managed:
            # Legacy "after" waits for success. A managed job waits for the guard: a missing
            # ticket is not seen, so the same id can be repaired. Accepted work is marked
            # by the caller, and the guard receipt blocks a second effect.
            return None
        seen[job["id"]] = now
        save_json(seen_path, seen)
        return None

    def _mark_seen(self, job_id):
        seen_path = self.state / "cluster-jobs-seen.json"
        seen = read_json(seen_path, {})
        now = time.time()
        seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and now - v < 3600}
        seen[job_id] = now
        save_json(seen_path, seen)

    def _announce_listener(self):
        from .linkfinder import listen
        while not self.stop_event.is_set():
            try:
                listen(self.config, self.state, self.stop_event)
            except Exception as exc:
                print(f"JARVIS node: announce listener: {exc}", flush=True)
            self.stop_event.wait(5)

    def _tunnel_guard(self):
        from .linkfinder import ensure_tunnel, ensure_dependents, CHECK, role
        from .cluster_tunnel import status
        connected = False
        while not self.stop_event.is_set():
            try:
                current, local, main = role(self.config, self.state)
                if local == main:  # promoted: nothing to supervise now; keep watching for a demotion
                    connected = False
                    # Role no longer matches an operator stop. ensure_tunnel drops that
                    # record and returns before any start while this machine is the main.
                    ensure_tunnel(current, self.state, self.config_path)
                    self.stop_event.wait(CHECK)
                    continue
                ensure_tunnel(current, self.state, self.config_path)
                healthy = bool(status(self.state).get("hub_healthy"))
                if healthy and not connected:
                    ensure_dependents(self.config, self.state)
                connected = healthy
            except Exception as exc:
                print(f"JARVIS node: tunnel supervisor: {exc}", flush=True)
            self.stop_event.wait(CHECK)

    def _input_guard(self):
        """Lan Mouse off while this laptop and its peer are apart (sharedinput.guard), checked
        once a minute on its own thread so a slow check never delays a poll."""
        from . import sharedinput
        while not self.stop_event.wait(60):
            try:
                sharedinput.guard_for(self.config, self.state)
            except Exception as exc:  # never let the guard take the node down
                print(f"JARVIS node: Lan Mouse guard: {exc}", flush=True)

    def _poll_loop(self):
        # Only the agent holding the lock guards Lan Mouse (a console's in-process
        # worker that loses the lock must not start a second guard).
        if self._acts_locally and (self.config.get("lan_mouse") or {}).get("off_when_apart", True):
            threading.Thread(target=self._input_guard, name="lanmouse-guard", daemon=True).start()
        from . import _under_tests
        if self._acts_locally and not _under_tests():
            threading.Thread(target=self._announce_listener,
                             name="address-announce-listener", daemon=True).start()
            threading.Thread(target=self._tunnel_guard, name="tunnel-keepalive", daemon=True).start()
        delay = 1
        while not self.stop_event.is_set() and not self.restarting:
            if self._cooperative_stop_requested():
                break
            if self._rejoined():  # before redeem_invite, which would use the stale client
                print("JARVIS node: paired again (new credential); restarting to use it.", flush=True)
                self.restarting = True
                break
            try:
                redeem_invite(self.config, self.state, self.client)
                worked = self.once()
                delay = 1
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"JARVIS node: {exc}", flush=True)
                worked = False
                delay = min(delay * 2, 15)
            except Exception as exc:  # a bug in one poll must not end the agent (no supervisor on Windows)
                print(f"JARVIS node: unexpected {type(exc).__name__}: {exc}", flush=True)
                worked = False
                delay = min(delay * 2, 15)
            if self._cooperative_stop_requested():
                break
            if not worked:
                self.stop_event.wait(delay)

    def run(self):
        from .locking import exclusive_lock
        from contextlib import ExitStack
        with ExitStack() as stack:
            try:  # only a busy lock means "already running"; the loop's own errors are its own
                stack.enter_context(exclusive_lock(self.state / "cluster-node.lock"))
            except ValueError:
                print("A JARVIS node agent is already running for this state directory.", flush=True)
            else:
                self._register_local_process()
                # A node restarts after each code update: bring the watcher along, and keep it
                # up. Only the agent holding the lock does (audit cluster-05: a console's
                # worker that lost the lock ran a second keep-alive beside the agent's).
                if getattr(self, "_acts_locally", False):
                    _refresh_clipboard(self.state, getattr(self, "config", None),
                                       getattr(self, "stop_event", None))
                else:
                    _refresh_clipboard(self.state)
                # Release restart: don't wait 60s or a new tunnel edge to re-check dependents.
                if getattr(self, "_acts_locally", False):
                    _keepalive_after_update(self.state, self.config, self.stop_event)
                if getattr(self, "_acts_locally", False):  # the last agent died mid-session: let go of its keys
                    from .remote_view import recover
                    try:
                        recover(self.state)
                    except Exception as exc:
                        print(f"JARVIS node: remote-control recovery: {exc}", flush=True)
                from .continuity.renewal import start_bound
                renewer = start_bound(self.state, getattr(self, "config", None),
                                      Path(self.config_path).resolve().parent
                                      if getattr(self, "config_path", None) else None)
                try:
                    self._poll_loop()
                finally:
                    if renewer is not None:
                        renewer.close()
                    from .remote_view import end_all
                    try:
                        end_all()  # a restart or stop never leaves David's keys held down
                    finally:
                        from .camera import end_all as end_cameras
                        try:
                            end_cameras()  # nor the camera on: ffmpeg stops with the agent
                        finally:
                            self._close_managed_process()
                            self._finish_local_process()
        if self.restarting:
            self.respawn()

    def _close_managed_process(self):
        """Close process-local continuity/runtime journals before stop ack."""
        try:
            from .continuity.participant import lookup, unbind
            participant = lookup(self.state)
            if participant is None:
                return
            services = getattr(participant, "services", None)
            if services is not None and callable(getattr(services, "deactivate", None)):
                services.deactivate("cooperative stop")
            else:
                controller = getattr(participant, "production_controller", None)
                if controller is not None:
                    controller.close()
            guard = getattr(participant, "guard", None)
            if guard is not None:
                guard.close()
            unbind(self.state)
        except Exception as exc:
            print(f"JARVIS node: continuity shutdown: {type(exc).__name__}", flush=True)

    def respawn(self):
        """Start the same command again (the lock is released by now), then exit. No execv:
        it doesn't replace the process on Windows. The short sleep lets this process free
        its ports first."""
        import subprocess
        import sys
        from .release import guard
        want = getattr(self, "installed", None)
        if _systemd_service("jarvis-cluster"):
            # Under systemd (Restart=on-failure) a clean exit stops the unit and kills the
            # detached child with its cgroup. Exit non-zero and let systemd start us again;
            # the watchdog runs as its own unit and rolls back if the new code never checks in.
            if want:
                guard(self.state, want, systemd=True)
            os._exit(75)
        # Under `python -m jarvis`, argv[0] is jarvis/__main__.py; run as a plain script its
        # relative imports fail, so a self-update would leave the node dead. Restart as -m.
        if Path(sys.argv[0]).name == "__main__.py" and Path(sys.argv[0]).parent.name == "jarvis":
            argv = [sys.executable, "-m", "jarvis", *sys.argv[1:]]
        else:
            argv = [sys.executable, *sys.argv]
        if want:  # starts the new code itself, and the old again if the new never checks in
            guard(self.state, want, argv=argv)
            os._exit(0)
        wrapper = "import subprocess,sys,time; time.sleep(2); sys.exit(subprocess.call(sys.argv[1:]))"
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | NO_WINDOW)
        subprocess.Popen([sys.executable, "-c", wrapper, *argv], close_fds=True,
                         **({"creationflags": flags} if on_windows() else {"start_new_session": True}))
        os._exit(0)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
