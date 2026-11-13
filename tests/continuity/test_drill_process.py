"""Multiprocess drill harness and stdin JSON worker.

The worker is a separate process: ``python -m tests.continuity.test_drill_process``.
It speaks one JSON object per line on stdin and stdout. This module has no
``TestCase`` so discovery does not collect it.

Nodes use the production ``AuthorityClient``, ``AuthenticatedPort``,
``MainRuntime``, ``ExecutorGuard``, and ``SignedAuthority``. The relay sink and
the command loop share one thread so the executor journal is not used across
threads. Nothing here starts ``IsolatedFleet``.
"""

import hashlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.gateway import (
    RELAY_PATH,
    AuthenticatedPort,
    AuthorityClient,
    GatewayError,
    canonical,
)
from jarvis.continuity.integration import SignedAuthority
from jarvis.continuity.runtime import LinkError, MainRuntime, RuntimeRefusal

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable
PC = ["status", "lease", "fence", "candidate", "consume", "reconcile"]
CLIENT = ["status", "client"]
METADATA = b"drill-metadata"
META_KEYS = frozenset({
    "operation_id", "target", "attempt_id", "digest", "capability",
    "authority_id", "incarnation", "epoch", "main",
})
_CAP = 256 * 1024
_LIVE = (REPO / ".jarvis", Path.home() / ".local" / "state" / "jarvis")


class SeamGap(AssertionError):
    """A proved hole at a named workstream boundary. Not a hidden regression."""


class _SeamResult:
    """Turn only ``SeamGap`` into an expected failure. Every other failure stays red."""

    def __init__(self, result):
        self._result = result

    def addFailure(self, test, err):
        exc = err[1]
        if isinstance(exc, SeamGap) and hasattr(self._result, "addExpectedFailure"):
            self._result.addExpectedFailure(test, err)
            return
        self._result.addFailure(test, err)

    def __getattr__(self, name):
        return getattr(self._result, name)


class SeamMixin:
    """Wrap the runner result. Cleanups still run; ``tearDown`` is not required."""

    def run(self, result=None):
        own = False
        if result is None:
            result = self.defaultTestResult()
            own = True
            start = getattr(result, "startTestRun", None)
            if start is not None:
                start()
        try:
            return super().run(_SeamResult(result))
        finally:
            if own:
                stop = getattr(result, "stopTestRun", None)
                if stop is not None:
                    stop()


class DrillError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code


def child_env():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("CODEX_"):
            env.pop(key, None)
    env["JARVIS_NO_MODELS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def refuse_live(path):
    resolved = Path(path).resolve()
    for root in _LIVE:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            raise DrillError("refuses_live_state", str(resolved))


def digest_bytes(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return


def make_keys(folder, names):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    made = {}
    for name in names:
        path = folder / name
        refuse_live(path)
        out = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-continuity-test", "-f", str(path)],
            check=False, capture_output=True, timeout=10,
        )
        if out.returncode != 0:
            raise DrillError("bad_config", out.stderr.decode("utf-8", "replace")[:300])
        token = (folder / (name + ".pub")).read_text(encoding="ascii").split()
        made[name] = ("%s %s" % (token[0], token[1]), path)
    return made


def write_config(folder, *, state, fleet, authority, owner_public, bind_port=0, lease_seconds=6,
                 margin=0.5, tls=None, name="gateway.json"):
    folder = Path(folder)
    if name.startswith("jarvis.config.json"):
        raise DrillError("bad_config", "config name")
    refuse_live(folder / name)
    refuse_live(state)
    body = {
        "state_path": str(state),
        "fleet_id": fleet,
        "authority_id": authority,
        "owner_public_keys": [owner_public],
        "bind_host": "127.0.0.1",
        "bind_port": bind_port,
        "lease_seconds": lease_seconds,
        "margin": margin,
    }
    if tls is not None:
        body["tls_cert"] = str(tls[0])
        body["tls_key"] = str(tls[1])
    path = folder / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def port_open(port):
    """True when a connect to ``127.0.0.1:port`` completes.

    A just-closed loopback port with ``TIME-WAIT`` sockets can complete
    this connect with no process listening. Use ``listener_open`` to prove
    a listener is gone.
    """
    port = int(port)
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        sock.bind(("127.0.0.1", 0))
        if sock.getsockname()[1] == port:
            return False
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def listener_open(port):
    """True when ``/proc/net/tcp`` shows a LISTEN socket on ``127.0.0.1:port``."""
    port = int(port)
    needle = "0100007F:%04X" % port
    try:
        with open("/proc/net/tcp", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return port_open(port)
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1].upper() == needle and parts[3] == "0A":
            return True
    return False


def origin_port(origin):
    host, _sep, rest = origin.rpartition(":")
    return host.split("://", 1)[-1].strip("[]"), int(rest)


def post_bytes(origin, path, raw, headers=None, timeout=10):
    request = urllib.request.Request(origin + path, data=raw, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(8192)
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(8192)
            return exc.code, dict(exc.headers or {}), payload
        finally:
            exc.close()


def cli_payload(raw):
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    return json.loads(text.splitlines()[-1])


def exc_code(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return "unavailable"


class _Pump:
    """Drain a pipe so the child cannot block, and keep the first bytes.

    ``BufferedReader.read`` on a pipe waits until its buffer fills or the
    child exits, so a one-line readiness message never arrives. Read the
    raw fd instead.
    """

    def __init__(self, stream, cap=_CAP):
        self.buf = bytearray()
        self.cap = cap
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        try:
            fd = stream.fileno()
        except (AttributeError, OSError):
            return
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                with self.lock:
                    if len(self.buf) < self.cap:
                        self.buf.extend(chunk[:self.cap - len(self.buf)])
        except OSError:
            return

    def bytes(self):
        with self.lock:
            return bytes(self.buf)

    def text(self):
        return self.bytes().decode("utf-8", "replace")


def run_cli(args, timeout=20):
    proc = subprocess.Popen(
        [str(PY), "-u", "-m", "jarvis.continuity.gateway", *args],
        cwd=str(REPO), env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True, bufsize=0,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid, signal.SIGKILL)
        out, err = proc.communicate(timeout=3)
        raise AssertionError("cli timeout %s stderr=%s" % (args, err[:300]))
    return proc.returncode, out, err


class GatewayProc:
    """``python -m jarvis.continuity.gateway serve`` in its own process group."""

    def __init__(self, config_path):
        self.config_path = str(config_path)
        self.proc = None
        self.out = None
        self.err = None
        self.info = None
        self.returncode = None

    def start(self):
        self.proc = subprocess.Popen(
            [str(PY), "-u", "-m", "jarvis.continuity.gateway", "serve", "--config", self.config_path],
            cwd=str(REPO), env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True, bufsize=0,
        )
        self.out = _Pump(self.proc.stdout)
        self.err = _Pump(self.proc.stderr)
        return self

    def ready(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.out.text()
            line, sep, _rest = text.partition("\n")
            if sep and line.strip():
                try:
                    info = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError("readiness is not JSON: %s" % line[:300]) from exc
                self.info = info
                return info
            if self.proc.poll() is not None:
                raise AssertionError(
                    "gateway exited %s before readiness stderr=%s stdout=%s"
                    % (self.proc.returncode, self.err.text()[-500:], text[-300:]))
            time.sleep(0.05)
        raise AssertionError(
            "gateway readiness timed out stderr=%s stdout=%s"
            % (self.err.text()[-500:], self.out.text()[-300:]))

    def serve(self, timeout=15):
        self.start()
        return self.ready(timeout)

    def stop(self):
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None:
            kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
        self.returncode = proc.returncode
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for pump in (self.out, self.err):
            if pump is not None:
                pump.thread.join(timeout=1)
        return self.returncode


class WorkerProc:
    """One node or phone process. stdout is the JSON protocol; stderr is drained."""

    def __init__(self):
        self.proc = None
        self.err = None
        self.seq = 1
        self._buf = b""

    def start(self):
        self.proc = subprocess.Popen(
            [str(PY), "-u", "-m", "tests.continuity.test_drill_process"],
            cwd=str(REPO), env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, start_new_session=True, bufsize=0,
        )
        self.err = _Pump(self.proc.stderr)
        return self

    def call(self, op, args=None, timeout=25):
        if self.proc is None or self.proc.poll() is not None:
            code = None if self.proc is None else self.proc.returncode
            raise AssertionError("worker dead before %s code=%s stderr=%s" % (op, code, self._stderr()))
        request = {"id": self.seq, "op": op, "args": args or {}}
        self.seq += 1
        self.proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("worker timeout %s stderr=%s" % (op, self._stderr()))
            ready, _w, _x = select.select([self.proc.stdout], [], [], min(remaining, 0.5))
            if self.proc.poll() is not None and not ready:
                raise AssertionError(
                    "worker exited %s during %s stderr=%s" % (self.proc.returncode, op, self._stderr()))
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError("worker eof during %s stderr=%s" % (op, self._stderr()))
            self._buf += chunk
            if len(self._buf) > _CAP and b"\n" not in self._buf:
                raise AssertionError("worker line exceeded cap during %s" % op)
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            reply = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AssertionError("worker reply is not JSON: %s" % line[:300]) from exc
        if reply.get("id") != request["id"]:
            raise AssertionError("worker id mismatch: %s" % reply)
        return reply

    def _stderr(self):
        if self.err is None:
            return ""
        return self.err.text()[-500:]

    def stop(self):
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self.err is not None:
            self.err.thread.join(timeout=1)
        return proc.returncode


class LoopProxy:
    """HTTP test forwarder with backend Host; close drops all live sockets.

    Signed request bodies are unchanged. The gateway now validates its bound
    Host/port, so the transport proxy forwards that authority, not its own port.
    """

    def __init__(self, host, port):
        self.upstream = (host, int(port))
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._live = []
        self._pumps = []
        self._closed = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set():
                client.close()
                return
            worker = threading.Thread(target=self._splice, args=(client,), daemon=True)
            with self._lock:
                self._pumps.append(worker)
            worker.start()

    def _splice(self, client):
        if self._stop.is_set():
            client.close()
            return
        try:
            upstream = socket.create_connection(self.upstream, timeout=2)
        except OSError:
            client.close()
            return
        client.settimeout(0.2)
        upstream.settimeout(0.2)
        with self._lock:
            self._live.extend((client, upstream))

        def pump(src, dst, rewrite_host=False):
            head = b""
            try:
                while not self._stop.is_set():
                    try:
                        data = src.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    if rewrite_host:
                        head += data
                        if b"\r\n\r\n" not in head:
                            if len(head) > 65536:
                                return
                            continue
                        headers, body = head.split(b"\r\n\r\n", 1)
                        host, port = self.upstream
                        authority = ("[%s]:%d" if ":" in host else "%s:%d") % (host, port)
                        lines = headers.split(b"\r\n")
                        # Keep duplicate headers duplicate: the gateway must still
                        # reject malformed framing/authentication normally.
                        lines = [b"Host: " + authority.encode("ascii")
                                 if line.lower().startswith(b"host:") else line for line in lines]
                        data = b"\r\n".join(lines) + b"\r\n\r\n" + body
                        rewrite_host = False
                    dst.sendall(data)
            except OSError:
                return
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        left = threading.Thread(target=pump, args=(client, upstream, True), daemon=True)
        right = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        left.start()
        right.start()
        left.join(timeout=2)
        right.join(timeout=2)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        for action in (
            lambda: self.sock.shutdown(socket.SHUT_RDWR),
            self.sock.close,
        ):
            try:
                action()
            except OSError:
                pass
        self._thread.join(timeout=2)
        with self._lock:
            live = list(self._live)
            pumps = list(self._pumps)
            self._live.clear()
        for sock in live:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for worker in pumps:
            worker.join(timeout=2)


def _probe():
    return {
        "control_ready": True, "luna": True, "grok": True, "quota_ok": True,
        "mediated_tools": True, "session_local": True, "checkpoint": "cp-1",
    }


class _Relay:
    def __init__(self, session):
        self.session = session
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.origin = "http://127.0.0.1:%d" % self.port

    def drain(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except BlockingIOError:
                return
            conn.settimeout(2)
            try:
                self._one(conn, addr[0])
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _one(self, conn, peer):
        try:
            request, body = _read_http(conn)
            path = request.split(" ")[1] if " " in request else ""
            if path != RELAY_PATH:
                _http_json(conn, 404, {"error": "not_found"})
                return
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeError, json.JSONDecodeError):
                parsed = None
            status, reply = self.session.handle_relay(parsed, peer)
            _http_json(conn, status, reply)
        except (OSError, DrillError):
            try:
                _http_json(conn, 400, {"error": "bad_request"})
            except OSError:
                return

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _read_http(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise DrillError("bad_request", "short header")
        data += chunk
        if len(data) > 16384:
            raise DrillError("bad_request", "header too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        raise DrillError("bad_request", "length")
    if length < 0 or length > 16384:
        raise DrillError("bad_request", "length")
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    return lines[0], rest[:length]


def _http_json(conn, status, obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    reason = "OK" if status == 200 else "REFUSED"
    head = (
        "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
        "Connection: close\r\n\r\n" % (status, reason, len(raw))
    ).encode("ascii")
    conn.sendall(head + raw)


class Session:
    def __init__(self, args):
        self.role = args["role"]
        self.origin = args["origin"]
        self.authority_id = args["authority_id"]
        self.fleet_id = args["fleet_id"]
        self.device_id = args["device_id"]
        self.installation_id = args["installation_id"]
        self.private_key = args["private_key"]
        self.public_keys = dict(args.get("public_keys") or {})
        self.boot_id = "boot-%s-1" % self.device_id
        self.seen = []
        self.effects = []
        self.holding = False
        self.next_renew = 0.0
        self.last_renew = None
        self.relay = None
        self.client = None
        self.port = None
        self.runtime = None
        self.signed = None
        self.executor = None
        self.journal = args.get("journal")
        refuse_live(self.private_key)
        if self.journal:
            refuse_live(self.journal)
        self.client = self._client(self.origin)
        if self.role == "phone":
            return
        if self.role != "node":
            raise DrillError("bad_request", "role")
        self.port = AuthenticatedPort(self.client)
        self.runtime = MainRuntime(
            self.port, self.device_id, self.installation_id, _probe,
            clock="auto", boot=lambda: self.boot_id)
        self.signed = SignedAuthority(self.client, self.public_keys)
        Path(self.journal).parent.mkdir(parents=True, exist_ok=True)
        self.executor = ExecutorGuard(
            self.journal, device_id=self.device_id, installation_id=self.installation_id,
            fleet_id=self.fleet_id, authority_id=self.authority_id, authority=self.signed,
            verifier=self.signed.verify, boot=lambda: self.boot_id)
        try:
            self.relay = _Relay(self)
        except Exception:
            self.close()
            raise

    def _client(self, origin):
        return AuthorityClient(
            origin, authority_id=self.authority_id, device_id=self.device_id,
            installation_id=self.installation_id, private_key_path=self.private_key)

    def boot_view(self):
        return {
            "pid": os.getpid(),
            "device_id": self.device_id,
            "relay_origin": None if self.relay is None else self.relay.origin,
        }

    def close(self):
        self.holding = False
        if self.relay is not None:
            self.relay.close()
            self.relay = None
        if self.executor is not None:
            self.executor.close()
            self.executor = None

    def dispatch(self, op, args):
        method = {
            "status": self.op_status,
            "set_origin": self.op_set_origin,
            "fence": self.op_fence,
            "start": self.op_start,
            "accept_lease": self.op_accept_lease,
            "hold": self.op_hold,
            "release": self.op_release,
            "require": self.op_require,
            "sign_ticket": self.op_sign_ticket,
            "accept_ticket": self.op_accept_ticket,
            "readiness": self.op_readiness,
            "hits": self.op_hits,
            "relay": self.op_relay,
            "renew": self.op_renew,
            "shutdown": self.op_shutdown,
        }.get(op)
        if method is None:
            raise DrillError("bad_request", op)
        return method(args)

    def op_status(self, _args):
        return self.port.status() if self.port is not None else self.client.status()

    def op_set_origin(self, args):
        self.origin = args["origin"]
        self.client = self._client(self.origin)
        if self.role == "phone":
            return {"origin": self.origin}
        self.port = AuthenticatedPort(self.client)
        self.runtime._port = self.port
        self.signed = SignedAuthority(self.client, self.public_keys)
        self.executor._authority = self.signed
        self.executor._verifier = self.signed.verify
        return {"origin": self.origin}

    def op_fence(self, args):
        return self.executor.fence(args["incarnation"], args["epoch"], args["mode_version"])

    def op_start(self, _args):
        self.runtime.start()
        return {"started": True, "block": self.runtime._block}

    def op_accept_lease(self, args):
        self.runtime.accept_lease(args["lease"])
        return {"holder": args["lease"]["holder"], "lease_id": args["lease"]["lease_id"]}

    def op_hold(self, args):
        self.holding = bool(args.get("on"))
        if self.holding:
            self.next_renew = time.monotonic() + 1.0
            self.last_renew = self.runtime.renew_once()
        return {"holding": self.holding, "renew": self.last_renew}

    def op_release(self, _args):
        self.holding = False
        return self.runtime.release_before_suspend()

    def op_require(self, args):
        return self.runtime.require_authority(args.get("effect_class") or "ordinary")

    def op_sign_ticket(self, args):
        status = self.port.status()
        if status.get("main") != self.device_id:
            raise AuthorityError("not_holder", "this node is not MAIN")
        now = time.time()
        operation_id = args["operation_id"]
        digest = digest_bytes(METADATA)
        ticket = {
            "admission_id": "adm-" + operation_id,
            "target": self.device_id,
            "operation_id": operation_id,
            "attempt_id": "try-1",
            "digest": digest,
            "main": status["main"],
            "incarnation": status["incarnation"],
            "epoch": status["epoch"],
            "issued_at": now - 1,
            "admit_until": now + 60,
            "issuer_incarnation": status["incarnation"],
        }
        signature = self.client.sign_ticket(ticket)
        signed = {
            "authority_id": self.authority_id,
            "fleet_id": self.fleet_id,
            "main": status["main"],
            "incarnation": status["incarnation"],
            "epoch": status["epoch"],
            "mode_version": status["mode_version"],
            "policy_version": status["policy_version"],
            "target": self.device_id,
            "operation_id": operation_id,
            "attempt_id": "try-1",
            "digest": digest,
            "effect_class": "ordinary",
            "issued_at": ticket["issued_at"],
            "admit_until": ticket["admit_until"],
            "issuer_incarnation": status["incarnation"],
            "admission_id": ticket["admission_id"],
            "signature": signature,
        }
        return {"ticket": ticket, "signed": signed}

    def op_accept_ticket(self, args):
        signed = args["signed"]
        ticket = args["ticket"]
        ran = {"value": False}

        def sink():
            ran["value"] = True
            self.effects.append(signed.get("operation_id"))

        try:
            receipt = self.executor.accept(signed, ticket, sink)
        except AuthorityError as exc:
            return {"ran": ran["value"], "error": exc.code}
        return {"ran": ran["value"], "error": None, "effect": receipt.get("effect"), "state": receipt.get("state")}

    def op_readiness(self, args):
        report = self.runtime.readiness("control")
        if report.get("meets") is not True:
            raise RuntimeRefusal("not_ready")
        return self.client.readiness(
            args["transition_id"], report["control_ready"], report["ai_ready"], report.get("checkpoint"))

    def op_hits(self, _args):
        return {"seen": list(self.seen), "effects": list(self.effects), "pid": os.getpid()}

    def op_relay(self, args):
        return self.client.relay(
            args["operation_id"], args["target"], args["attempt_id"], digest_bytes(METADATA), "client")

    def op_renew(self, _args):
        self.last_renew = self.runtime.renew_once()
        return self.last_renew

    def op_shutdown(self, _args):
        self.holding = False
        return {"pid": os.getpid()}

    def handle_relay(self, body, peer):
        operation_id = body.get("operation_id") if isinstance(body, dict) else None
        self.seen.append({"peer": peer, "operation_id": operation_id})
        if not isinstance(body, dict) or set(body) != META_KEYS:
            return 400, {"error": "bad_request"}
        accepted = False
        try:
            accepted = self._effect(body)
        except Exception:
            accepted = False
        self.seen[-1]["accepted"] = accepted
        return 200, {"operation_id": body["operation_id"], "accepted": bool(accepted)}

    def _effect(self, body):
        if body["target"] != self.device_id or body["main"] != self.device_id or body["capability"] != "client":
            return False
        self.runtime.require_authority("ordinary")
        status = self.port.status()
        now = time.time()
        digest = body["digest"]
        ticket = {
            "admission_id": "adm-" + body["operation_id"],
            "target": self.device_id,
            "operation_id": body["operation_id"],
            "attempt_id": body["attempt_id"],
            "digest": digest,
            "main": status["main"],
            "incarnation": status["incarnation"],
            "epoch": status["epoch"],
            "issued_at": now - 1,
            "admit_until": now + 30,
            "issuer_incarnation": status["incarnation"],
        }
        signature = self.client.sign_ticket(ticket)
        signed = {
            "authority_id": status["authority_id"],
            "fleet_id": status["fleet_id"],
            "main": status["main"],
            "incarnation": status["incarnation"],
            "epoch": status["epoch"],
            "mode_version": status["mode_version"],
            "policy_version": status["policy_version"],
            "target": self.device_id,
            "operation_id": body["operation_id"],
            "attempt_id": body["attempt_id"],
            "digest": digest,
            "effect_class": "ordinary",
            "issued_at": ticket["issued_at"],
            "admit_until": ticket["admit_until"],
            "issuer_incarnation": status["incarnation"],
            "admission_id": ticket["admission_id"],
            "signature": signature,
        }
        ran = {"value": False}

        def sink():
            ran["value"] = True
            self.effects.append(body["operation_id"])

        receipt = self.executor.accept(signed, ticket, sink)
        return ran["value"] and receipt.get("effect") == "completed"

    def renew_if_due(self):
        if not self.holding or time.monotonic() < self.next_renew:
            return
        try:
            self.last_renew = self.runtime.renew_once()
        except (AuthorityError, GatewayError, RuntimeRefusal, LinkError) as exc:
            self.last_renew = {"code": exc_code(exc)}
        self.next_renew = time.monotonic() + 1.0


def _reply(req, ok, **fields):
    body = {"id": req.get("id"), "ok": ok}
    body.update(fields)
    sys.stdout.buffer.write(json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def worker_main():
    session = None
    buf = b""
    stdin = sys.stdin.buffer.fileno()
    try:
        while True:
            watch = [stdin]
            relay = None if session is None else session.relay
            if relay is not None:
                watch.append(relay.sock)
            try:
                ready, _w, _x = select.select(watch, [], [], 0.5)
            except (OSError, ValueError):
                break
            if relay is not None and relay.sock in ready:
                relay.drain()
            if session is not None:
                session.renew_if_due()
            if stdin not in ready:
                continue
            chunk = os.read(stdin, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _CAP and b"\n" not in buf:
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    _reply({"id": None}, False, error="bad_request", detail="request")
                    continue
                try:
                    if req.get("op") == "boot":
                        session = Session(req.get("args") or {})
                        result = session.boot_view()
                    elif session is None:
                        raise DrillError("not_booted")
                    else:
                        result = session.dispatch(req.get("op"), req.get("args") or {})
                    _reply(req, True, result=result)
                    if req.get("op") == "shutdown":
                        return
                except Exception as exc:
                    _reply(req, False, error=exc_code(exc), detail=str(exc)[:400])
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    try:
        worker_main()
    except KeyboardInterrupt:
        raise SystemExit(0)
