"""One main JARVIS, authenticated outbound node agents, and a local console proxy.

Nodes poll for allowlisted work. Dispatched actions are never automatically replayed.
Only the main creates model lanes, writes shared memory, or accepts control requests.
"""

import hmac
import json
import os
import platform
import select
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__
from .deploy import code_fingerprint
from .cluster_store import Broker, CAPABILITIES, TERMINAL
from .topology import endpoint, identity, is_node, request_from

_HUBS = {}
MAX_PROTOCOL = 65536
MAX_RESULT = 4_000_000
POLL_WAIT = 25
FRESH = 10  # seconds after a check-in that a node counts as alive between polls


class NodeOffline(ConnectionError):
    """An enrolled node was offline before any job was enqueued; SSH is safe."""


# Queries that change nothing, allowed even when a node's code is out of date, so Settings
# and status can still say what it is.
READ_ONLY_DESKTOP = frozenset({"input_info", "list_apps"})


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


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class HubRefused(RuntimeError):
    """The main answered with an HTTP error; `status` says which (400/404 are final)."""
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # credentials must never follow redirects


class Client:
    def __init__(self, config, state, owner=False):
        self.config, self.state = config, Path(state)
        self.local, self.main = identity(config)
        self.url = endpoint(config)
        if owner:
            if self.local != self.main:
                raise ValueError("Only the configured main PC can use its owner credential.")
            path = self.state / "cluster-owner.token"
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
        while True:
            result = self.request("/v1/jobs/" + job_id, timeout=min(15, max(1, deadline - time.monotonic())))
            if result["state"] in TERMINAL:
                if result["state"] == "done":
                    return result["result"]
                error = (result.get("result") or {}).get("error")
                raise RuntimeError(error or f"Request {job_id} is {result['state']}; do not automatically retry it.")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Request {job_id} has no confirmed result. It may have run; do not automatically retry.")
            time.sleep(0.15)

    def ask(self, text, tier=None, write=False, timeout=600, request_id=None):
        started = self.request("/v1/ask", {"text": text, "tier": tier, "write": write,
                               "timeout": timeout, "request_id": request_id or uuid.uuid4().hex})
        return self.wait(started["id"], timeout + 5)


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
    elif kind != "inventory":
        raise ValueError("Nodes execute only desktop, inventory, or Grokbot operations.")


def execute_local(kind, payload, config, state):
    validate_action(kind, payload)
    if kind == "desktop":
        from .remote import run_here
        ok, text = run_here(payload["action"], payload["arg"])
        return {"ok": ok, "text": text}
    if kind == "inventory":
        from .inventory import collect
        return collect()
    if kind == "storage":
        from .storage import Storage
        return Storage(config, state)._local(identity(config)[0], payload["path"], payload["action"])
    from .grokbot import Grokbot
    return Grokbot(config, state).send(payload["text"], device=identity(config)[0])


class Hub:
    def __init__(self, config, base, state, console=None, ask_handler=None):
        self.config, self.base, self.state = config, Path(base), Path(state).resolve()
        self.local, self.main = identity(config)
        if self.local != self.main:
            raise ValueError("Only the configured main PC may host JARVIS.")
        self.broker = Broker(state)
        self.owner = self.broker.owner_token()
        self.console, self.ask_handler = console, ask_handler
        self.stop_event = threading.Event()
        self._condition = threading.Condition()
        self._polling = {}  # node -> sockets of polls held open now (guarded by _condition)
        self.server = self.thread = self.worker = self._lock = None
        self.started = False

    def authenticate(self, headers, capability=None):
        if headers.get("X-JARVIS-Main") != self.main:
            raise PermissionError("This endpoint is not the configured main PC; update cluster config.")
        auth = headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token and hmac.compare_digest(token.encode(), self.owner.encode()):
            return self.main, CAPABILITIES | {"owner"}
        found = self.broker.authenticate(token)
        if not found or (capability and capability not in found[1]):
            raise PermissionError("Node credential is missing, revoked, or lacks this capability.")
        return found

    def _auto_update(self, settle=20):
        """Once after start: bring every online node that runs other code up to this main's
        code (jarvis cluster update), so nodes follow the main without git or a manual step.
        Failures are logged; actions to that node stay refused until it matches. Nodes
        without SSH (forward tunnel) pull the code themselves, so they are skipped here."""
        if self.stop_event.wait(settle):  # let nodes reconnect and report their code
            return
        from .deploy import is_ssh_node, update
        for status in self.broker.nodes():
            if (not status["online"] or status.get("code") == code_fingerprint()
                    or not is_ssh_node(self.config, status["id"])):
                continue
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
        if not status.get("online"):
            return False
        with self._condition:
            held = list(self._polling.get(node, ()))
        if any(_connected(sock) for sock in held):
            return True
        if status.get("seen") and time.time() - status["seen"] < FRESH:
            return True
        return self.broker.running(node) > 0

    def dispatch(self, node, kind, payload, timeout=45):
        validate_action(kind, payload)
        if node not in self.config.get("devices", {}):
            raise ValueError("Unknown node.")
        if node == self.main:
            return execute_local(kind, payload, self.config, self.state)
        status = next((n for n in self.broker.nodes() if n["id"] == node), None)
        capability = "files" if kind == "storage" else kind
        if not status or capability not in status["capabilities"]:
            raise ValueError(f"Node {node} is not enrolled for {kind} actions.")
        if not self.alive(node, status):
            raise NodeOffline(f"Node {node} is offline. Start jarvis cluster node on that PC.")
        if not _read_only(kind, payload) and status.get("code") != code_fingerprint():
            # Mismatched code can misread a newer job; refuse rather than guess.
            raise ValueError(f"{node} runs different JARVIS code ({status.get('code') or 'unknown'}, "
                             f"main {code_fingerprint()}). Run: jarvis cluster update {node}")
        with self._condition:
            job_id = self.broker.enqueue(node, self.main, kind, payload, timeout)
            self._condition.notify_all()
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set():
            with self._condition:
                job = self.broker.get(job_id)
                if job["state"] in TERMINAL:
                    if job["state"] == "done":
                        return job["result"]
                    raise RuntimeError((job.get("result") or {}).get("error") or
                                       f"Node action {job_id} is {job['state']}; do not automatically retry.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Node action {job_id} has no confirmed result. It may have run; do not retry.")
                self._condition.wait(remaining)
        raise RuntimeError(f"Main stopped while waiting for {job_id}; action outcome is uncertain.")

    def _asks(self):
        while not self.stop_event.is_set():
            with self._condition:
                job = self.broker.claim(self.main, {"ask"})
                if not job and not self.stop_event.is_set():
                    self._condition.wait()
                    continue
            if not job:
                continue
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
        port = int(settings.get("listen_port", 7778) if port is None else port)
        cert, key = settings.get("tls_cert"), settings.get("tls_key")
        if host not in ("127.0.0.1", "localhost", "::1") and not (cert and key):
            raise ValueError("A network-facing main endpoint requires cluster.tls_cert and tls_key; or use SSH forwarding.")
        self._lock = exclusive_lock(self.state / "cluster-main.lock")
        try:
            self._lock.__enter__()
        except BaseException:
            self._lock = None
            raise RuntimeError("The main runtime is already running for this state directory.") from None
        try:
            self.server = make_server(self, host, port)
            if cert or key:
                if not (cert and key):
                    raise ValueError("Set both cluster.tls_cert and cluster.tls_key.")
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(cert, key)
                self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.broker.recover()
            _HUBS[str(self.state)] = self
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.worker = threading.Thread(target=self._asks, daemon=True)
            self.thread.start()
            self.worker.start()
            self.started = True
            if settings.get("auto_update", True):
                threading.Thread(target=self._auto_update, name="jarvis-auto-update",
                                 daemon=True).start()
        except BaseException:
            if self.server:
                self.server.server_close()
            self._lock.__exit__(None, None, None)
            self._lock = None
            raise
        return self

    def close(self):
        self.stop_event.set()
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


def make_server(hub, host, port):
    from .ui import ConsoleServer, Handler

    class MainHandler(Handler):
        console = hub.console

        def _allowed(self):
            try:
                self.node_id, self.capabilities = hub.authenticate(self.headers, "console")
                return True
            except PermissionError:
                return False

        def do_GET(self):
            self._request(False)

        def do_POST(self):
            self._request(True)

        def _request(self, post):
            try:
                if self.path.startswith("/api/"):
                    self.node_id, _ = hub.authenticate(self.headers, "console")
                    with request_from(self.node_id):
                        return Handler.do_POST(self) if post else Handler.do_GET(self)
                node, caps = hub.authenticate(self.headers)
                if self.path == "/v1/status" and not post:
                    return self._send(200, {"main_device": hub.main, "nodes": hub.broker.nodes()})
                if self.path == "/v1/code" and not post and "owner" not in caps:
                    from .deploy import code_bundle  # nodes pull the main's code over their own channel
                    return self._send(200, code_bundle())
                if self.path.startswith("/v1/jobs/") and not post:
                    return self._send(200, hub.broker.get(self.path.rsplit("/", 1)[-1],
                                                         None if "owner" in caps else node))
                if not post:
                    return self._send(404, {"error": "Unknown cluster endpoint."})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= (MAX_RESULT if self.path == "/v1/result" else MAX_PROTOCOL):
                    raise ValueError("Invalid cluster request length.")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Cluster body must be an object.")
                if self.path == "/v1/poll" and "owner" not in caps:
                    hub.broker.heartbeat(node, data)
                    kinds = caps & {"desktop", "inventory", "grokbot"}
                    if "files" in caps:
                        kinds.add("storage")
                    deadline = time.monotonic() + POLL_WAIT
                    with hub._condition:
                        hub._polling.setdefault(node, set()).add(self.connection)
                        try:
                            job = hub.broker.claim(node, kinds)
                            while job is None and not hub.stop_event.is_set():
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    break
                                hub._condition.wait(remaining)
                                job = hub.broker.claim(node, kinds)
                        finally:
                            hub._polling[node].discard(self.connection)
                    return self._send(200, {"job": job, "main_device": hub.main,
                                             "code": code_fingerprint()})
                if self.path == "/v1/result" and "owner" not in caps:
                    with hub._condition:
                        hub.broker.finish(data.get("id"), node, data.get("result"), bool(data.get("failed")))
                        hub._condition.notify_all()
                    return self._send(200, {"accepted": True})
                if ((self.path == "/v1/action" and "owner" in caps) or
                        (self.path == "/v1/control" and "console" in caps)):
                    return self._send(200, hub.dispatch(data.get("node"), data.get("kind"),
                                        data.get("payload"), timeout=45))
                if self.path == "/v1/ask" and "ask" in caps:
                    text, tier = data.get("text"), data.get("tier")
                    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                        raise ValueError("A request needs 1-20000 characters of text.")
                    if tier not in (None, "simple", "medium", "complex"):
                        raise ValueError("Unknown request tier.")
                    timeout = data.get("timeout", 600)
                    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
                        raise ValueError("Request timeout must be 1-1800 seconds.")
                    payload = {"text": text.strip(), "tier": tier, "write": bool(data.get("write"))}
                    with hub._condition:
                        job_id = hub.broker.enqueue(hub.main, node, "ask", payload, timeout,
                                                   data.get("request_id"))
                        hub._condition.notify_all()
                    return self._send(202, {"id": job_id})
                raise PermissionError("This node cannot use that endpoint.")
            except NodeOffline as exc:
                self._send(409, {"error": str(exc), "code": "node_offline"})
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
            except LookupError as exc:
                self._send(404, {"error": str(exc)})
            except (ValueError, TypeError, OSError, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})

    return ConsoleServer((host, port), MainHandler)


def dispatch(config, state, node, kind, payload, timeout=45):
    local, main = identity(config)
    if local != main:
        raise ValueError("Node agents cannot route directly to other nodes; submit the request to the main PC.")
    hub = _HUBS.get(str(Path(state).resolve()))
    if hub:
        return hub.dispatch(node, kind, payload, timeout)
    return Client(config, state, owner=True).request("/v1/action",
               {"node": node, "kind": kind, "payload": payload}, timeout=timeout + 5)


class ProxyLane:
    """Voice callers keep their local microphone/speaker; cognition stays on the main."""
    def __init__(self, config, state):
        self.config, self.state = config, Path(state)

    def warm(self):
        pass

    def close(self):
        pass


class NodeWorker:
    def __init__(self, config, state, executor=None):
        if not is_node(config):
            raise ValueError("Run the node agent on a PC other than the configured main.")
        self.config, self.state = config, Path(state).resolve()
        self.client = Client(config, state)
        self.executor = executor or (lambda kind, payload: execute_local(kind, payload, config, state))
        self.stop_event = threading.Event()
        self.outbox = self.state / "cluster-outbox.json"
        self.thread = None
        self.restarting = False  # set once new code is installed; run() then re-execs

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

    def _pull_update(self, want):
        """The main runs other code: fetch, verify and install it, then restart. Tried once
        per target fingerprint per 10 minutes so a bad package can't loop the node."""
        from .deploy import apply_bundle
        mark = self.state / "cluster-update.json"
        try:
            last = json.loads(mark.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}
        if last.get("code") == want and time.time() - last.get("at", 0) < 600:
            return False
        save_json(mark, {"code": want, "at": time.time()})
        bundle = self.client.request("/v1/code", timeout=60)
        count = apply_bundle(bundle)
        print(f"JARVIS node: installed main's code {bundle.get('code')} ({count} files); restarting.", flush=True)
        return True

    def once(self):
        # A crash after execution can resend the result; it never reexecutes the action.
        if self.outbox.exists():
            note = json.loads(self.outbox.read_text(encoding="utf-8"))
            if note.get("main_device") != self.client.main:
                raise ValueError("An unsent result belongs to the previous main PC; inspect cluster-outbox.json.")
            self._send_result(note["message"])
        response = self.client.request("/v1/poll", {"hostname": platform.node(),
                            "platform": platform.system(), "version": __version__,
                            "code": code_fingerprint()},
                            timeout=POLL_WAIT + 10)
        received = time.monotonic()
        want = response.get("code")
        if want and want != code_fingerprint():
            try:
                if self._pull_update(want):
                    self.restarting = True
                    return False
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"JARVIS node: code update failed: {exc}", flush=True)
        job = response.get("job")
        if not job:
            return False
        try:
            # Time the job on our own clock: the main's wall clock may differ from ours.
            if "remaining" in job:
                expired = received + job["remaining"] <= time.monotonic()
            else:
                expired = time.time() >= job["deadline"]
            if expired:
                raise TimeoutError("Node action expired before execution.")
            result = self.executor(job["kind"], job["payload"])
            message = {"id": job["id"], "result": result, "failed": False}
        except Exception as exc:
            message = {"id": job["id"], "result": {"error": str(exc)}, "failed": True}
        save_json(self.outbox, {"main_device": self.client.main, "message": message})
        self._send_result(message)
        return True

    def run(self):
        from .locking import exclusive_lock
        try:
            with exclusive_lock(self.state / "cluster-node.lock"):
                delay = 1
                while not self.stop_event.is_set() and not self.restarting:
                    try:
                        worked = self.once()
                        delay = 1
                    except (OSError, ValueError, RuntimeError) as exc:
                        print(f"JARVIS node: {exc}", flush=True)
                        worked = False
                        delay = min(delay * 2, 15)
                    if not worked:
                        self.stop_event.wait(delay)
        except ValueError:
            print("A JARVIS node agent is already running for this state directory.", flush=True)
        if self.restarting:
            self.respawn()

    def respawn(self):
        """Start the same command again (the lock is released by now), then exit. No execv:
        it doesn't replace the process on Windows. The short sleep lets this process free
        its ports first."""
        import subprocess
        import sys
        argv = [sys.executable, *sys.argv]
        wrapper = "import subprocess,sys,time; time.sleep(2); sys.exit(subprocess.call(sys.argv[1:]))"
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen([sys.executable, "-c", wrapper, *argv], close_fds=True,
                         **({"creationflags": flags} if os.name == "nt" else {"start_new_session": True}))
        os._exit(0)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
