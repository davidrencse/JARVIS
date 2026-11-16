"""Authenticated HTTP boundary in front of `AuthorityStore`.

Isolated prototype. Importing this module does not bind a socket, read live
config, or change `cluster.main_device`. `ContinuityGateway.serve` is the only
listener, and it starts only when called. Nothing in the running application
imports this module. This is not operational failover and it does not claim
Oracle or Dell are ready.

The store trusts its caller. This module authenticates before every store call:
an enrolled installation key for device routes, and a fresh signature by a
pinned owner key for `manage_main`. There is no unsigned bypass.

`AuthenticatedPort` is the store-shaped seam for `MainRuntime` and `ExecutorGuard`.
It still signs with the installation key and the pinned owner key. A shared-secret
identity dict is not accepted; the integration fleet uses this port.

Full contract, error map, and limits: docs/design/mobile-continuity/GATEWAY-IMPLEMENTATION.md.

Process entry (not run on import): `python -m jarvis.continuity.gateway`.
`serve` reads one dedicated JSON file and validates it before opening the
authority database. It does not read `jarvis.config.json` and it does not
load an owner private key. `backup` copies that database with the SQLite
backup API while holding the authority writer lock. `restore` checks the
snapshot read-only, writes a new file while holding the destination writer
lock, and requires a new authority id.
The destination drops leases, transitions, admissions, participants, and
the gateway directory, then stays `LEGACY_ACTIVE` until a new cutover.
See `GatewayConfig`, `FailoverPolicy`, `backup_gateway`, and `restore_gateway`.
"""

from .. import jobtiming
from ..home import home as _jarvis_home
import argparse
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.continuity.authority import (
    MAX_SIGNATURE,
    SCHEMA_VERSION,
    TICKET_FIELDS as _TICKET_FIELDS,
    AuthorityError,
    AuthorityStore,
    _writer_lock,
)
from jarvis.remote_view import FRAME_HOLD

DEVICE_NS = "jarvis-continuity-device-v1"
OWNER_NS = "jarvis-continuity-owner-v1"
TICKET_NS = "jarvis-continuity-ticket-v1"
RUNTIME_NS = "jarvis-continuity-runtime-v1"
_NAMESPACES = (DEVICE_NS, OWNER_NS, TICKET_NS, RUNTIME_NS)

MAX_BODY = 8192
MAX_RESPONSE = 8192
MAX_RELAY_RESPONSE = 1024
SKEW_SECONDS = 30
CHALLENGE_TTL = 60.0
MAX_CHALLENGES = 32
MAX_NONCES = 4096
MAX_RELAYS = 1024
RELAY_PATH = "/v1/continuity/relay"
MAX_ENVELOPE = 256 * 1024
MAX_READ_RESPONSE = 4 * 1024 * 1024
MAX_RAW = 2_500_000
EFFECT_KINDS = frozenset({"ask", "session_start", "session_input", "session_stop", "logout_end"})
READ_KINDS = frozenset({"overview", "fleet", "ask_poll", "session_frame", "effect_status"})
# The hub holds an unchanged frame for FRAME_HOLD. A relay read shorter than that
# raises no_relay on every quiet screen and the phone drops the session.
READ_RELAY_TIMEOUT = FRAME_HOLD + 3

CAPABILITIES = frozenset({
    "status", "lease", "fence", "candidate", "consume", "reconcile", "client",
})
_PC_CAPS = frozenset({"status", "lease", "fence", "candidate", "consume", "reconcile", "client"})
_SERVER_CAPS = frozenset({"status", "fence", "reconcile"})
_CLIENT_CAPS = frozenset({"status", "client", "reconcile"})

# Stable store codes from authority.py, plus gateway codes. Unknown store codes
# become HTTP 503 without their message.
HTTP_STATUS = {
    "bad_request": 400, "bad_ticket": 400, "bad_origin": 400, "refused_bind": 400,
    "unauthorized": 401,
    "forbidden": 403, "unknown_participant": 403, "revoked": 403,
    "not_found": 404,
    "ticket_expired": 410,
    "stale": 409, "busy": 409, "key_reused": 409, "conflict": 409, "not_quiet": 409,
    "lease_live": 409, "ahead": 409, "bad_edge": 409, "unacked": 409, "expired": 409,
    "draining": 409, "mode": 409, "ineligible": 409, "main_alive": 409, "no_main": 409,
    "noop": 409, "not_ready": 409, "bad_state": 409, "no_admission": 409,
    "target_unfenced": 409, "installation_mismatch": 409, "active_holder": 409,
    "transition_member": 409,
    "envelope_unsigned": 401, "body_not_relayable": 503,
    "quarantined": 503, "writer_busy": 503, "unavailable": 503, "no_relay": 503,
    "authority_mismatch": 503, "redirect": 503,
}

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}")
_FORBIDDEN_KEYS = frozenset({
    "prompt", "body", "text", "content", "message", "file", "filename", "path",
    "url", "upstream", "host", "authorization", "cookie", "secret", "token",
    "password", "transcript", "frame", "image", "payload_body",
})
_ADMISSION_KEYS = _TICKET_FIELDS + ("consumed_at", "already")
_PURPOSE_PATH = {
    "status": "/v1/authority/status",
    "renew": "/v1/authority/renew",
    "release": "/v1/authority/release",
    "acknowledge": "/v1/authority/acknowledge",
    "readiness": "/v1/authority/transitions/readiness",
    "consume": "/v1/authority/admissions/consume",
    "lookup": "/v1/authority/admissions/lookup",
    "relay": "/v1/authority/relay",
    # Self-recovery (opt-in per gateway config): an enrolled PC asks to take or
    # return MAIN for itself. The store's lease, epoch and fence rules still
    # decide; these purposes only let a device open and step its own transition.
    "self_policy": "/v1/authority/self/policy",
    "self_transition": "/v1/authority/self/transitions",
    "self_step": "/v1/authority/self/transitions/step",
}
_OWNER_PATH = {
    "transition": "/v1/authority/transitions",
    "step": "/v1/authority/transitions/step",
    "enroll": "/v1/authority/participants",
    "mode": "/v1/authority/mode",
    "disable": "/v1/authority/participants/disable",
}
_DEVICE_PURPOSE = {path: name for name, path in _PURPOSE_PATH.items()}
_OWNER_PURPOSE = {path: name for name, path in _OWNER_PATH.items()}


class GatewayError(Exception):
    """Refusal safe to return to a client. `str` is the stable code only."""

    def __init__(self, code, status=None, wait=None, operation_id=None):
        super().__init__(code if code in HTTP_STATUS or _safe_code(code) else "unavailable")
        self.code = str(self)
        self.status = HTTP_STATUS.get(self.code, 503) if status is None else status
        self.wait = wait if _finite_wait(wait) else None
        self.operation_id = operation_id


def _safe_code(code):
    return isinstance(code, str) and re.fullmatch(r"[a-z_]{1,32}", code) is not None


def _finite_wait(wait):
    return isinstance(wait, (int, float)) and not isinstance(wait, bool) and math.isfinite(wait) and wait >= 0


def _retry_after_seconds(value):
    """Delay-seconds only. An HTTP-date is not a store wait."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.fullmatch(r"[0-9]{1,6}", text):
        return None
    return int(text)


def _agreed_wait(body_wait, header):
    """Exact store wait, only when Retry-After is its ceiling.

    A header alone, a body field alone, a boolean, zero, or a mismatch is not
    a wait. Callers must not treat a missing wait as the barrier having elapsed.
    """
    if not _finite_wait(body_wait) or body_wait <= 0 or body_wait > 86400:
        return None
    seconds = _retry_after_seconds(header)
    if seconds is None or seconds != max(0, math.ceil(body_wait)):
        return None
    return body_wait


def canonical(value):
    """One ASCII JSON encoding. Signatures and digests are over these bytes."""
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise GatewayError("bad_request") from None
    if len(raw) > MAX_BODY:
        raise GatewayError("bad_request")
    return raw.encode("ascii")


def payload_digest(payload):
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


def pin_origin(origin):
    """Pinned authority origin. HTTP is loopback-only. No userinfo, path, query, or fragment."""
    if not isinstance(origin, str) or not origin or len(origin) > 200:
        raise GatewayError("bad_origin")
    parts = urlsplit(origin)
    host, port = parts.hostname, parts.port
    if parts.username or parts.password or parts.query or parts.fragment:
        raise GatewayError("bad_origin")
    if not host or port is None or parts.path not in ("", "/"):
        raise GatewayError("bad_origin")
    if parts.scheme == "http":
        if host not in {"127.0.0.1", "::1"}:
            raise GatewayError("bad_origin")
    elif parts.scheme != "https" or host in {"localhost", "0.0.0.0", "::"}:
        raise GatewayError("bad_origin")
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown}:{port}"


def pin_relay(origin):
    """Pin a loopback HTTP or tailnet HTTPS relay origin; never accept redirects."""
    pinned = pin_origin(origin)
    parts = urlsplit(pinned)
    host = (parts.hostname or "").lower()
    if parts.scheme == "http":
        if host not in {"127.0.0.1", "::1"}:
            raise GatewayError("bad_origin")
    elif parts.scheme == "https":
        # A DNS name is explicitly pinned by the enrollment record. Never accept
        # an address literal or a merely similar public hostname.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise GatewayError("bad_origin")
        if not host.endswith(".ts.net") or host == "ts.net":
            raise GatewayError("bad_origin")
    else:
        raise GatewayError("bad_origin")
    return pinned


def _message(namespace, fields):
    if namespace not in _NAMESPACES:
        raise GatewayError("bad_request")
    return namespace.encode("ascii") + b"\n" + canonical(fields)


def _ident(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GatewayError("bad_request")
    return value


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GatewayError("bad_request")
    return value


def _nnint(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GatewayError("bad_request")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayError("bad_request")
    if not math.isfinite(value) or abs(value) > 1e12:
        raise GatewayError("bad_request")
    return float(value)


def _flag(value):
    if not isinstance(value, bool):
        raise GatewayError("bad_request")
    return value


def _signature(value):
    if (not isinstance(value, str) or len(value) > MAX_SIGNATURE or not value.isascii()
            or "BEGIN SSH SIGNATURE" not in value or "END SSH SIGNATURE" not in value):
        raise GatewayError("bad_request")
    return value


def _public_key(value):
    if not isinstance(value, str) or not _PUBLIC_KEY.fullmatch(value):
        raise GatewayError("bad_request")
    return value


def _exact(payload, required, optional=()):
    if not isinstance(payload, dict):
        raise GatewayError("bad_request")
    keys = set(payload)
    if keys & _FORBIDDEN_KEYS or not set(required) <= keys or keys - set(required) - set(optional):
        raise GatewayError("bad_request")
    return payload


def _windows_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def sign_message(namespace, private_key, data):
    """SSH signature of `data` under `namespace`. The private key never leaves this call."""
    if namespace not in _NAMESPACES or not isinstance(data, bytes) or len(data) > MAX_BODY:
        raise GatewayError("bad_request")
    jobtiming.mark("sign_begin")
    try:
        out = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-q", "-f", str(private_key), "-n", namespace],
            input=data, capture_output=True, timeout=5, creationflags=_windows_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        raise GatewayError("unauthorized") from None
    if out.returncode or b"BEGIN SSH SIGNATURE" not in out.stdout:
        raise GatewayError("unauthorized")
    text = out.stdout.decode("ascii", "strict")
    jobtiming.mark("signed")
    return _signature(text)


def verify_message(namespace, data, signature, public_key):
    """True only when `public_key` signed exactly `data` in `namespace`. Never raises."""
    try:
        if namespace not in _NAMESPACES or not isinstance(data, bytes) or len(data) > MAX_BODY:
            return False
        signature = _signature(signature)
        public_key = _public_key(public_key)
    except GatewayError:
        return False
    # The signed bytes stay on stdin. The temp files are the public key and the signature.
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-continuity-verify-") as folder:
            signers = Path(folder) / "signers"
            sig = Path(folder) / "msg.sig"
            signers.write_text(f'{namespace} namespaces="{namespace}" {public_key}\n', encoding="ascii")
            sig.write_text(signature, encoding="ascii")
            out = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", namespace,
                 "-n", namespace, "-s", str(sig)],
                input=data, capture_output=True, timeout=5, creationflags=_windows_flags(),
            )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
    jobtiming.mark("verified")
    return out.returncode == 0


def _owner_id(public_key):
    return "owner-" + hashlib.sha256(public_key.encode("ascii")).hexdigest()[:32]


class DevicePrincipal:
    """Minted only by `ContinuityGateway.authenticate_device`. Single use."""

    def __init__(self, token, device_id, installation_id, kind, capabilities, purpose, digest):
        self.token = token
        self.device_id = device_id
        self.installation_id = installation_id
        self.kind = kind
        self.capabilities = capabilities
        self.purpose = purpose
        self.payload_digest = digest
        self.used = False


class OwnerProof:
    """Minted only by `ContinuityGateway.authenticate_owner`. Single use."""

    def __init__(self, token, owner_id, purpose, digest):
        self.token = token
        self.owner_id = owner_id
        self.purpose = purpose
        self.payload_digest = digest
        self.used = False


class _Record:
    def __init__(self, device_id, installation_id, kind, public_key, capabilities, relay_origin):
        self.device_id = device_id
        self.installation_id = installation_id
        self.kind = kind
        self.public_key = public_key
        self.capabilities = capabilities
        self.relay_origin = relay_origin


def _no_redirect(handler):
    class _Stop(handler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise GatewayError("redirect")
    return _Stop


def _opener():
    # No environment proxy. A 3xx is a failure, not a second request.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _no_redirect(urllib.request.HTTPRedirectHandler),
    )


def _loopback_ip(value):
    """True for 127.0.0.1, ::1, and an IPv4-mapped loopback. Not a name."""
    text = str(value or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    text = text.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_loopback)


def _split_host(value):
    """(hostname, port or None) from one Host header. Bare IPv6 without brackets is refused."""
    text = str(value or "").strip().lower()
    if not text:
        return None, None
    if text.startswith("["):
        end = text.find("]")
        if end < 1:
            return None, None
        name = text[1:end]
        rest = text[end + 1:]
        if rest == "":
            return name, None
        if rest.startswith(":") and rest[1:].isdigit():
            return name, int(rest[1:])
        return None, None
    if text.count(":") > 1:
        return None, None
    if ":" in text:
        name, _, port = text.partition(":")
        if not port.isdigit():
            return None, None
        return name.rstrip("."), int(port)
    return text.rstrip("."), None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return "jarvis-continuity"

    def setup(self):
        timeout = getattr(self.server, "request_timeout", None)
        if timeout:
            try:
                self.request.settimeout(timeout)
            except OSError:
                pass
        super().setup()

    def handle(self):
        # A client that never finishes the body, or a shutdown that closes the
        # socket, must not sit in the server forever or dump a traceback.
        try:
            super().handle()
        except (TimeoutError, ConnectionError, socket.timeout):
            return

    def do_POST(self):
        gateway = self.server.gateway
        if gateway._closing or gateway._closed:
            gateway._reply(self, 503, {"error": "unavailable"}, None, "unavailable")
            return
        gateway._http(self)

    def do_GET(self):
        self.server.gateway._reply(self, 405, {"error": "bad_request"}, None, "bad_request")

    do_PUT = do_DELETE = do_PATCH = do_GET

    def log_message(self, fmt, *args):
        return


class _GatewayHTTPServer(ThreadingHTTPServer):
    """Listener whose client reads and handler joins are bounded.

    Handler threads are non-daemon so a request still inside the authority
    database keeps the process up until it finishes or the socket times out.
    `block_on_close` stays false: `server_close` must not join without a
    deadline. `shutdown` on the gateway counts handlers itself and waits
    with a timeout.
    """

    daemon_threads = False
    block_on_close = False
    request_timeout = 5.0

    def __init__(self, server_address, RequestHandlerClass):
        self._socks = set()
        self._inflight_lock = threading.Lock()
        self._active = 0
        self._idle = threading.Event()
        self._idle.set()
        super().__init__(server_address, RequestHandlerClass)

    def get_request(self):
        conn, addr = super().get_request()
        try:
            conn.settimeout(self.request_timeout)
        except OSError:
            pass
        with self._inflight_lock:
            self._socks.add(conn)
        return conn, addr

    def process_request(self, request, client_address):
        # Count before the thread runs. When the accept loop has returned,
        # every accepted request is already in `_active`.
        with self._inflight_lock:
            self._active += 1
            self._idle.clear()
        started = False
        try:
            super().process_request(request, client_address)
            started = True
        finally:
            if not started:
                with self._inflight_lock:
                    self._socks.discard(request)
                    self._active -= 1
                    if self._active == 0:
                        self._idle.set()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._inflight_lock:
                self._socks.discard(request)
                self._active -= 1
                if self._active == 0:
                    self._idle.set()

    def abort_inflight(self):
        """Unblock handlers stuck in a socket read. Does not touch the database."""
        with self._inflight_lock:
            socks = list(self._socks)
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait_idle(self, timeout):
        if self._idle.wait(timeout):
            return True
        with self._inflight_lock:
            return self._active == 0


def _participant_disabled(gateway, device_id, installation_id):
    """True when this installation's participant row is ineligible.

    A missing row is not disabled: clients are directory entries only.
    A gateway double with no store (transport tests) is not disabled.
    The store lock is taken after the gateway lock when the caller holds one.
    """
    store = getattr(gateway, "store", None)
    db = getattr(store, "_db", None)
    lock = getattr(store, "_lock", None)
    if store is None or db is None or lock is None:
        return False
    with lock:
        try:
            row = db.execute(
                "SELECT eligible FROM participants WHERE device_id=? AND installation_id=?",
                (device_id, installation_id)).fetchone()
        except sqlite3.Error:
            raise GatewayError("unavailable") from None
    if row is None:
        return False
    return not row["eligible"]


def _relay_authority_changed(gateway, status, origin):
    """True when the posted answer is no longer the live MAIN's pinned origin.

    A read must not be returned after takeover. An effect that already left
    the process is left unresolved and is not posted again.
    """
    try:
        current = gateway._relay_status()
    except GatewayError:
        return True
    if not isinstance(current, dict) or not isinstance(status, dict):
        return True
    for key in ("authority_id", "incarnation", "epoch", "mode_version", "main"):
        if current.get(key) != status.get(key):
            return True
    record = getattr(gateway, "_devices", {}).get(current.get("main"))
    return record is None or record.relay_origin != origin


class ContinuityGateway:
    """One `AuthorityStore`, one device directory, and an optional in-process HTTP server.

    `open` does not listen. Call `serve` explicitly. `close` stops the listener and,
    when this object created the store, closes it.

    Enrollment metadata and device-nonce replay digests live in gateway-owned
    tables in the store database (`gateway_directory`, `gateway_nonces`).
    `open` loads them through the same validators `enroll` uses. A row that
    fails validation refuses startup (`bad_state`). Owner challenges and owner
    nonces stay in RAM: a restart voids outstanding owner challenges. Relay
    and phone bodies stay in RAM and are never written.
    """

    def __init__(self, store, owner_public_keys, *, wall=time.time, owns_store=False,
                 self_recovery=None):
        keys = tuple(owner_public_keys)
        if not keys:
            raise GatewayError("bad_request")
        self.store = store
        self._owner_keys = tuple(_public_key(key) for key in keys)
        self._owner_by_id = {_owner_id(key): key for key in self._owner_keys}
        self._wall = wall
        self._owns_store = owns_store
        self._token = object()
        self._lock = threading.RLock()
        self._devices = {}
        self._nonces = {}
        self._owner_nonces = {}
        self._challenges = {}
        self._relays = {}
        self._effects = {}
        self._events = []
        self._httpd = None
        self._thread = None
        self._listening_origin = None
        self._tls = False
        self._closed = False
        self._closing = False
        # Manual promotion only. Constructed with the default so no gateway
        # object can grow an automatic promoter later in this process.
        self.policy = FailoverPolicy(auto_failover=False)
        self.self_recovery = _self_recovery(self_recovery)
        self._load_durable()

    @classmethod
    def open(cls, path, fleet_id, authority_id, owner_public_keys, *, monotonic=time.monotonic,
             wall=time.time, lease_seconds=None, margin=None, self_recovery=None):
        kwargs = {"monotonic": monotonic, "wall": wall}
        if lease_seconds is not None:
            kwargs["lease_seconds"] = lease_seconds
        if margin is not None:
            kwargs["margin"] = margin
        try:
            store = AuthorityStore(path, fleet_id, authority_id, **kwargs)
        except AuthorityError as exc:
            raise GatewayError(exc.code) from None
        try:
            return cls(store, owner_public_keys, wall=wall, owns_store=True,
                       self_recovery=self_recovery)
        except Exception:
            store.close()
            raise

    def close(self):
        front = getattr(self, "phone_front", None)
        if front:
            front.close()
        self.shutdown()
        if self._owns_store and not self._closed:
            self._closed = True
            self.store.close()

    def serve(self, host="127.0.0.1", port=0, *, confirm_bind=False, tls_cert=None, tls_key=None):
        """Bind an in-process HTTP server. Refuses wildcard addresses.

        TLS stays off unless both `tls_cert` and `tls_key` are set. Then the
        accepted socket is wrapped with the stdlib `ssl.SSLContext` (TLS 1.2
        or newer) and the origin uses `https`. Passing only one path is
        `bad_config`. This loads the TLS key into the SSL stack for the
        handshake. It does not load an owner private key, and it does not
        start failover.
        """
        self._check_bind(host, confirm_bind)
        if (tls_cert is None) != (tls_key is None):
            raise GatewayError("bad_config")
        if self._httpd is not None:
            raise GatewayError("bad_request")
        try:
            httpd = _GatewayHTTPServer((host, port), _Handler)
        except OSError:
            raise GatewayError("refused_bind") from None
        httpd.gateway = self
        httpd.timeout = 2
        if tls_cert is not None:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(certfile=str(tls_cert), keyfile=str(tls_key))
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            except (OSError, ssl.SSLError):
                httpd.server_close()
                raise GatewayError("bad_config") from None
        self._httpd = httpd
        self._tls = tls_cert is not None
        bound_host, bound_port = httpd.server_address[:2]
        shown = f"[{bound_host}]" if ":" in bound_host else bound_host
        scheme = "https" if self._tls else "http"
        self._listening_origin = f"{scheme}://{shown}:{bound_port}"
        self._closing = False
        self._thread = threading.Thread(target=httpd.serve_forever, name="continuity-gateway", daemon=False)
        try:
            self._thread.start()
        except Exception:
            self._httpd = None
            self._thread = None
            self._tls = False
            self._listening_origin = None
            httpd.server_close()
            raise GatewayError("refused_bind") from None
        return self._listening_origin

    @staticmethod
    def _check_bind(host, confirm_bind):
        """Loopback is the default. A confirmed bind is one IP, never a name or a wildcard."""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            raise GatewayError("refused_bind") from None
        if addr.is_unspecified or addr.is_multicast or addr.is_link_local:
            raise GatewayError("refused_bind")
        if not addr.is_loopback and not confirm_bind:
            raise GatewayError("refused_bind")

    def shutdown(self):
        """Stop the accept loop and joined handlers. Never waits without a deadline.

        In-flight reads are aborted, then the server thread is joined. Handlers
        already past the closed check are given `request_timeout` plus a second
        to leave the database. After that this returns anyway so `close` can
        drop the writer lock. A handler that starts later sees `_closing` and
        does not touch the database.
        """
        self._closing = True
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.abort_inflight()
            httpd.shutdown()
        if thread is not None:
            thread.join(timeout=3)
            if thread.is_alive():
                thread.join(timeout=1)
        if httpd is not None:
            httpd.wait_idle(httpd.request_timeout + 1)
            try:
                httpd.server_close()
            except OSError:
                pass
        self._listening_origin = None
        self._tls = False

    @property
    def listening(self):
        return self._listening_origin

    def audit_events(self):
        with self._lock:
            return list(self._events)

    # --- authentication ----------------------------------------------------------------

    def issue_challenge(self, request_digest):
        digest = _digest(request_digest)
        with self._lock:
            self._prune(self._challenges, "expires")
            if len(self._challenges) >= MAX_CHALLENGES:
                raise GatewayError("unavailable")
            challenge_id = "ch-" + secrets.token_hex(16)
            expires = self._wall() + CHALLENGE_TTL
            self._challenges[challenge_id] = {"digest": digest, "expires": expires, "attempts": 0}
        return {"challenge_id": challenge_id, "expires_at": expires}

    def authenticate_device(self, *, device_id, installation_id, nonce, issued_at, purpose, payload, signature):
        device_id, installation_id, nonce = _ident(device_id), _ident(installation_id), _ident(nonce)
        issued_at = _nnint(issued_at)
        signature = _signature(signature)
        if purpose not in _PURPOSE_PATH:
            raise GatewayError("bad_request")
        digest = payload_digest(payload)
        record = self._devices.get(device_id)
        if record is None or record.installation_id != installation_id:
            raise GatewayError("unauthorized")
        message = _message(DEVICE_NS, {
            "v": 1, "device_id": device_id, "installation_id": installation_id, "nonce": nonce,
            "issued_at": issued_at, "purpose": purpose, "payload_digest": digest,
        })
        # Replay key is a digest. The nonce text is not stored.
        nonce_digest = _nonce_digest(nonce)
        with self._lock:
            current = self._devices.get(device_id)
            if (current is None or current.installation_id != installation_id
                    or current.public_key != record.public_key):
                raise GatewayError("unauthorized")
            record = current
            self._fresh(self._nonces, (device_id, nonce_digest), issued_at)
            if not verify_message(DEVICE_NS, message, signature, record.public_key):
                raise GatewayError("unauthorized")
            # Status and relay must not answer for a disabled installation.
            # Acknowledge and consume still reach the store, which returns revoked.
            if purpose in {"status", "relay"} and _participant_disabled(
                    self, device_id, installation_id):
                raise GatewayError("unauthorized")
            current = self._devices.get(device_id)
            if (current is None or current.installation_id != installation_id
                    or current.public_key != record.public_key):
                raise GatewayError("unauthorized")
            expires = issued_at + SKEW_SECONDS
            self._persist_device_nonce(device_id, nonce_digest, expires)
            self._nonces[(device_id, nonce_digest)] = expires
        return DevicePrincipal(self._token, device_id, installation_id, record.kind,
                               record.capabilities, purpose, digest)

    def authenticate_owner(self, *, challenge_id, nonce, issued_at, purpose, payload, signature):
        challenge_id, nonce = _ident(challenge_id), _ident(nonce)
        issued_at = _nnint(issued_at)
        signature = _signature(signature)
        if purpose not in _OWNER_PATH:
            raise GatewayError("bad_request")
        digest = payload_digest(payload)
        message = _message(OWNER_NS, {
            "v": 1, "challenge_id": challenge_id, "nonce": nonce, "issued_at": issued_at,
            "payload_digest": digest,
        })
        with self._lock:
            self._fresh(self._owner_nonces, nonce, issued_at)
            record = self._challenges.get(challenge_id)
            now = self._wall()
            if record is None or record["expires"] < now or record["digest"] != digest:
                raise GatewayError("unauthorized")
            matched = None
            for key in self._owner_keys:
                if verify_message(OWNER_NS, message, signature, key):
                    matched = key
                    break
            if matched is None:
                record["attempts"] += 1
                if record["attempts"] >= 5:
                    self._challenges.pop(challenge_id, None)
                raise GatewayError("unauthorized")
            self._challenges.pop(challenge_id, None)
            self._owner_nonces[nonce] = issued_at + SKEW_SECONDS
        return OwnerProof(self._token, _owner_id(matched), purpose, digest)

    def _fresh(self, bucket, key, issued_at):
        now = self._wall()
        if abs(now - issued_at) > SKEW_SECONDS:
            raise GatewayError("unauthorized")
        self._prune(bucket, None)
        if key in bucket:
            raise GatewayError("unauthorized")
        if len(bucket) >= MAX_NONCES:
            raise GatewayError("unavailable")

    def _prune(self, bucket, field):
        now = self._wall()
        dead = [key for key, value in bucket.items()
                if (value["expires"] if field else value) < now]
        for key in dead:
            bucket.pop(key, None)

    def _accept_device(self, principal, capability, purpose, payload):
        with self._lock:
            if (not isinstance(principal, DevicePrincipal) or principal.token is not self._token
                    or principal.used):
                raise GatewayError("unauthorized")
            principal.used = True
            if principal.purpose != purpose or principal.payload_digest != payload_digest(payload):
                raise GatewayError("unauthorized")
            if capability not in principal.capabilities:
                raise GatewayError("forbidden")
        return payload

    def _accept_owner(self, proof, purpose, payload):
        with self._lock:
            if not isinstance(proof, OwnerProof) or proof.token is not self._token or proof.used:
                raise GatewayError("unauthorized")
            proof.used = True
            if proof.purpose != purpose or proof.payload_digest != payload_digest(payload):
                raise GatewayError("unauthorized")
        return payload

    def _store(self, fn):
        # `_closed` means `close` has dropped or is dropping the connection.
        # `_closing` alone only stops new HTTP work; in-process calls after
        # `shutdown` and before `close` still reach the open store.
        if self._closed:
            raise GatewayError("unavailable")
        try:
            return fn()
        except AuthorityError as exc:
            raise GatewayError(exc.code, wait=exc.wait) from None

    # --- facade ------------------------------------------------------------------------

    def status(self, principal, payload):
        """AuthorityStatus for an enrolled device. Empty payload."""
        self._accept_device(principal, "status", "status", _exact(payload, ()))
        if _participant_disabled(self, principal.device_id, principal.installation_id):
            raise GatewayError("unauthorized")
        return self._store(self.store.status)

    def renew(self, principal, payload):
        """LeaseView. `valid_for` is the store's value, not a new clock started here."""
        self._accept_device(principal, "lease", "renew", _exact(
            payload, ("holder", "lease_id", "incarnation", "epoch")))
        holder, lease_id = _ident(payload["holder"]), _ident(payload["lease_id"])
        incarnation, epoch = _nnint(payload["incarnation"]), _nnint(payload["epoch"])
        if holder != principal.device_id:
            raise GatewayError("forbidden")
        view = self._store(lambda: self.store.renew(holder, lease_id, incarnation, epoch))
        return {"holder": holder, "lease_id": lease_id, "incarnation": incarnation, "epoch": epoch,
                "valid_for": view["valid_for"], "renew_after": view["renew_after"], "ready": view["ready"]}

    def release(self, principal, payload):
        self._accept_device(principal, "lease", "release", _exact(
            payload, ("holder", "lease_id", "incarnation", "epoch")))
        holder, lease_id = _ident(payload["holder"]), _ident(payload["lease_id"])
        incarnation, epoch = _nnint(payload["incarnation"]), _nnint(payload["epoch"])
        if holder != principal.device_id:
            raise GatewayError("forbidden")
        self._store(lambda: self.store.release(holder, lease_id, incarnation, epoch))
        return self._store(self.store.status)

    def acknowledge(self, principal, payload):
        self._accept_device(principal, "fence", "acknowledge", _exact(
            payload, ("mode_version", "fenced_epoch"), ("boot_id",)))
        mode_version, fenced_epoch = _nnint(payload["mode_version"]), _nnint(payload["fenced_epoch"])
        boot_id = _ident(payload["boot_id"]) if "boot_id" in payload else None
        self._store(lambda: self.store.acknowledge(
            principal.device_id, principal.installation_id, mode_version, fenced_epoch, boot_id))
        # The store keeps the max ack it has seen. This echoes the accepted request.
        view = {"device_id": principal.device_id, "installation_id": principal.installation_id,
                "mode_version": mode_version, "fenced_epoch": fenced_epoch, "accepted": True}
        return view

    def transition_request(self, owner_proof, payload):
        """Open one transition. `caller` is the owner-key id, never a field the client picks."""
        self._accept_owner(owner_proof, "transition", _exact(payload, (
            "client_key", "action", "candidate", "expected_incarnation", "expected_epoch",
            "expected_policy", "profile")))
        view = self._store(lambda: self.store.request(
            owner_proof.owner_id, _ident(payload["client_key"]), payload["action"],
            _ident(payload["candidate"]), _nnint(payload["expected_incarnation"]),
            _nnint(payload["expected_epoch"]), _nnint(payload["expected_policy"]), payload["profile"]))
        return view

    def transition_readiness(self, principal, payload):
        self._accept_device(principal, "candidate", "readiness", _exact(
            payload, ("transition_id", "control_ready", "ai_ready"), ("checkpoint",)))
        transition_id = _ident(payload["transition_id"])
        control_ready, ai_ready = _flag(payload["control_ready"]), _flag(payload["ai_ready"])
        checkpoint = _ident(payload["checkpoint"]) if "checkpoint" in payload else None
        current = self._store(lambda: self.store.transition(transition_id))
        if current["candidate"] != principal.device_id:
            raise GatewayError("forbidden")
        return self._store(lambda: self.store.readiness(transition_id, control_ready, ai_ready, checkpoint))

    # --- self-recovery -------------------------------------------------------------
    #
    # Off unless the gateway config names `self_recovery`. A PC listed there may
    # open `recover` for itself once the old lease has lapsed (the store refuses
    # `main_alive` before that), and the preferred PC may open
    # `set_preferred_main` for itself to take MAIN back. The device then steps only
    # the transitions it opened. Fencing is unchanged: `fence` still waits for the
    # old validity plus margin or a full acknowledgement quorum, and the epoch
    # moves before any grant, so a returning or partitioned old MAIN is refused.

    def _self_allowed(self, principal):
        config = self.self_recovery
        if config is None:
            raise GatewayError("forbidden")
        if principal.kind != "pc" or principal.device_id not in config["candidates"]:
            raise GatewayError("forbidden")
        return config

    def self_policy(self, principal, payload):
        self._accept_device(principal, "candidate", "self_policy", _exact(payload, ()))
        config = self.self_recovery
        if config is None or principal.kind != "pc" or principal.device_id not in config["candidates"]:
            return {"enabled": False}
        return {"enabled": True, "candidates": list(config["candidates"]),
                "preferred": config["preferred"], "stability_s": config["stability_s"]}

    def self_transition(self, principal, payload):
        self._accept_device(principal, "candidate", "self_transition", _exact(payload, (
            "client_key", "action", "expected_incarnation", "expected_epoch",
            "expected_policy", "profile")))
        config = self._self_allowed(principal)
        action = payload["action"]
        if action == "set_preferred_main":
            current = self._store(self.store.status)
            if principal.device_id != (current.get("preferred") or config["preferred"]):
                raise GatewayError("forbidden")
        elif action != "recover":
            raise GatewayError("forbidden")
        return self._store(lambda: self.store.request(
            "self:" + principal.device_id, _ident(payload["client_key"]), action,
            principal.device_id, _nnint(payload["expected_incarnation"]),
            _nnint(payload["expected_epoch"]), _nnint(payload["expected_policy"]), payload["profile"]))

    def self_step(self, principal, payload):
        self._accept_device(principal, "candidate", "self_step", _exact(
            payload, ("transition_id", "step"), ("canary_ok",)))
        self._self_allowed(principal)
        transition_id, step = _ident(payload["transition_id"]), payload["step"]
        if step not in {"drain", "fence", "grant", "complete", "cancel"}:
            raise GatewayError("bad_request")
        if (step == "complete") != ("canary_ok" in payload):
            raise GatewayError("bad_request")
        current = self._store(lambda: self.store.transition(transition_id))
        caller = self._store(lambda: self.store.transition_caller(transition_id))
        own = caller == "self:" + principal.device_id and current["candidate"] == principal.device_id
        if not own:
            # A surviving PC may clear a dead candidate's not-ready grant and
            # nothing else. After a fence that leaves no MAIN, never two.
            status = self._store(self.store.status)
            if not (step == "cancel" and current["state"] == "GRANTED_NOT_READY"
                    and not status.get("lease_valid")):
                raise GatewayError("forbidden")
        if step == "drain":
            return self._store(lambda: self.store.drain(transition_id))
        if step == "fence":
            return self._store(lambda: self.store.fence(transition_id))
        if step == "grant":
            return self._store(lambda: self.store.grant(transition_id))
        if step == "cancel":
            return self._store(lambda: self.store.cancel(transition_id))
        return self._store(lambda: self.store.complete(transition_id, _flag(payload["canary_ok"])))

    def transition_step(self, owner_proof, payload):
        """drain, fence, grant, complete, or cancel. Owner proof only; no policy-token bypass."""
        self._accept_owner(owner_proof, "step", _exact(
            payload, ("transition_id", "step"), ("canary_ok",)))
        transition_id, step = _ident(payload["transition_id"]), payload["step"]
        if step not in {"drain", "fence", "grant", "complete", "cancel"}:
            raise GatewayError("bad_request")
        if step == "complete":
            if "canary_ok" not in payload:
                raise GatewayError("bad_request")
            canary_ok = _flag(payload["canary_ok"])
        elif "canary_ok" in payload:
            raise GatewayError("bad_request")
        if step == "drain":
            return self._store(lambda: self.store.drain(transition_id))
        if step == "fence":
            return self._store(lambda: self.store.fence(transition_id))
        if step == "grant":
            return self._store(lambda: self.store.grant(transition_id))
        if step == "cancel":
            return self._store(lambda: self.store.cancel(transition_id))
        return self._store(lambda: self.store.complete(transition_id, canary_ok))

    def consume(self, principal, payload):
        """Target device consumes a ticket the current MAIN's installation key signed.

        The HTTP caller is the target, not `authenticated_main`. See GATEWAY-IMPLEMENTATION.md.
        """
        self._accept_device(principal, "consume", "consume", _exact(payload, _TICKET_FIELDS + ("ticket_signature",)))
        signature = _signature(payload["ticket_signature"])
        # Verify the MAIN signature over the caller's original numbers. Coercing 1 to 1.0
        # would change the canonical bytes and reject a valid ticket, or the reverse.
        signed = {key: payload[key] for key in _TICKET_FIELDS}
        fields = self._ticket_fields(payload)
        if principal.device_id != fields["target"]:
            raise GatewayError("forbidden")
        main = self._devices.get(fields["main"])
        if main is None or main.kind != "pc":
            raise GatewayError("unauthorized")
        if not verify_message(TICKET_NS, _message(TICKET_NS, signed), signature, main.public_key):
            raise GatewayError("unauthorized")
        verified = (main.installation_id, main.kind, main.public_key)
        with self._lock:
            current = self._devices.get(fields["main"])
            if current is None or (current.installation_id, current.kind, current.public_key) != verified:
                raise GatewayError("unauthorized")
            record = self._consume_bound(current, fields)
        return _project_admission(record)

    def _consume_bound(self, main, fields):
        """Consume only when the signed directory row is that PC's installation.

        The store lock covers the directory read and `consume`. `consume`
        takes that same re-entrant lock for its transaction. Enrollment takes
        the gateway lock first, then the store lock, so it cannot change the
        row between the check and the admission insert.
        """
        store = self.store
        with store._lock:
            directory = store._db.execute(
                "SELECT installation_id, kind, public_key FROM gateway_directory WHERE device_id=?",
                (main.device_id,)).fetchone()
            participant = store._db.execute(
                "SELECT installation_id, kind FROM participants WHERE device_id=?",
                (main.device_id,)).fetchone()
            if (directory is None or participant is None or directory["kind"] != "pc"
                    or participant["kind"] != "pc"
                    or directory["installation_id"] != participant["installation_id"]
                    or directory["installation_id"] != main.installation_id
                    or directory["public_key"] != main.public_key):
                raise GatewayError("unauthorized")
            return self._store(lambda: store.consume(
                admission_id=fields["admission_id"], target=fields["target"],
                operation_id=fields["operation_id"], attempt_id=fields["attempt_id"],
                digest=fields["digest"], main=fields["main"], incarnation=fields["incarnation"],
                epoch=fields["epoch"], issued_at=fields["issued_at"], admit_until=fields["admit_until"],
                issuer_incarnation=fields["issuer_incarnation"]))

    def admission(self, principal, payload):
        """Read a consumed admission. A device that is neither target nor MAIN gets the same not_found."""
        self._accept_device(principal, "reconcile", "lookup", _exact(payload, ("admission_id",)))
        admission_id = _ident(payload["admission_id"])
        record = self._store(lambda: self.store.admission(admission_id))
        if record is None or principal.device_id not in (record["target"], record["main"]):
            raise GatewayError("not_found")
        return _project_admission(record)

    def relay(self, principal, payload):
        """Metadata forward to the current MAIN's pinned loopback origin.

        Never an effect authorization. `effect_authorized` is always false. A repeated
        operation id is not sent twice. No body, prompt, or caller-chosen URL is accepted.
        """
        self._accept_device(principal, "client", "relay", _exact(
            payload, ("operation_id", "target", "attempt_id", "digest", "capability")))
        if _participant_disabled(self, principal.device_id, principal.installation_id):
            raise GatewayError("unauthorized")
        operation_id = _ident(payload["operation_id"])
        target, attempt_id = _ident(payload["target"]), _ident(payload["attempt_id"])
        digest, capability = _digest(payload["digest"]), _ident(payload["capability"])
        if capability not in CAPABILITIES:
            raise GatewayError("bad_request")
        identity = (digest, target, attempt_id, capability)
        with self._lock:
            prior = self._relays.get(operation_id)
            if prior is not None:
                if prior["identity"] != identity:
                    raise GatewayError("conflict", operation_id=operation_id)
                if prior["state"] == "done":
                    return dict(prior["result"])
                raise GatewayError("no_relay", operation_id=operation_id)
            try:
                status = self._relay_status()
            except GatewayError as exc:
                raise GatewayError(exc.code, status=exc.status, wait=exc.wait,
                                   operation_id=operation_id) from None
            record = self._devices.get(status["main"])
            if record is None or not record.relay_origin:
                raise GatewayError("no_relay", operation_id=operation_id)
            if record.relay_origin == self._listening_origin:
                raise GatewayError("no_relay", operation_id=operation_id)
            if len(self._relays) >= MAX_RELAYS:
                raise GatewayError("unavailable", operation_id=operation_id)
            self._relays[operation_id] = {"identity": identity, "state": "inflight"}
            origin = record.relay_origin
        body = canonical({
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability, "authority_id": status["authority_id"],
            "incarnation": status["incarnation"], "epoch": status["epoch"], "main": status["main"],
        })
        try:
            upstream = _post_json(origin + RELAY_PATH, body, MAX_RELAY_RESPONSE)
            if (set(upstream) != {"operation_id", "accepted"} or upstream["operation_id"] != operation_id
                    or not isinstance(upstream["accepted"], bool)):
                raise GatewayError("no_relay", operation_id=operation_id)
            if _relay_authority_changed(self, status, origin):
                raise GatewayError("no_relay", operation_id=operation_id)
        except GatewayError:
            with self._lock:
                self._relays[operation_id] = {"identity": identity, "state": "error"}
            raise GatewayError("no_relay", operation_id=operation_id) from None
        result = {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                  "upstream_accepted": upstream["accepted"]}
        with self._lock:
            self._relays[operation_id] = {"identity": identity, "state": "done", "result": result}
        return result

    def forward_transient(self, principal, payload, envelope):
        """Forward one phone request to the ready MAIN through RAM only.

        `envelope` is the request body the phone sent. It is checked against the signed
        `digest`, sent once, and never stored, logged, or put in the audit trail. An
        effect is keyed by `operation_id` and never re-posted: a repeat returns the stored
        small result, and a lost, odd, or stale-authority answer leaves the operation
        unresolved (`no_relay`). A read is one attempt and is not remembered. A read or
        an effect that finishes after the MAIN tuple or its pinned origin changes is
        `no_relay` and is not posted again. `effect_authorized` is always false: MAIN
        still needs its own admission ticket.
        """
        self._accept_device(principal, "client", "relay", _exact(
            payload, ("operation_id", "target", "attempt_id", "digest", "capability", "kind")))
        if _participant_disabled(self, principal.device_id, principal.installation_id):
            raise GatewayError("unauthorized")
        operation_id = _ident(payload["operation_id"])
        target, attempt_id = _ident(payload["target"]), _ident(payload["attempt_id"])
        digest, capability = _digest(payload["digest"]), payload["capability"]
        kind = payload["kind"]
        if capability != "client" or not isinstance(kind, str) or kind not in EFFECT_KINDS | READ_KINDS:
            raise GatewayError("bad_request")
        if not isinstance(envelope, (bytes, bytearray)) or not 1 <= len(envelope) <= MAX_ENVELOPE:
            raise GatewayError("bad_request")
        envelope = bytes(envelope)
        if digest != "sha256:" + hashlib.sha256(envelope).hexdigest():
            raise GatewayError("bad_request")
        effect = kind in EFFECT_KINDS
        identity = (digest, target, attempt_id, capability, kind)
        with self._lock:
            if effect:
                prior = self._effects.get(operation_id)
                if prior is not None:
                    if prior["identity"] != identity:
                        raise GatewayError("conflict", operation_id=operation_id)
                    if prior["state"] == "done":
                        return dict(prior["result"])
                    raise GatewayError("no_relay", operation_id=operation_id)
            try:
                status = self._relay_status()
            except GatewayError as exc:
                raise GatewayError(exc.code, status=exc.status, wait=exc.wait,
                                   operation_id=operation_id) from None
            if target != status["main"]:
                raise GatewayError("no_relay", operation_id=operation_id)
            record = self._devices.get(status["main"])
            if record is None or not record.relay_origin or record.relay_origin == self._listening_origin:
                raise GatewayError("no_relay", operation_id=operation_id)
            origin = record.relay_origin
            if effect:
                if len(self._effects) >= MAX_RELAYS:
                    raise GatewayError("unavailable", operation_id=operation_id)
                self._effects[operation_id] = {"identity": identity, "state": "inflight"}
        body = json.dumps({
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability, "kind": kind,
            "authority_id": status["authority_id"], "incarnation": status["incarnation"],
            "epoch": status["epoch"], "main": status["main"],
            "envelope_b64": base64.b64encode(envelope).decode("ascii"),
        }, sort_keys=True, separators=(",", ":")).encode("ascii")
        limit = MAX_RELAY_RESPONSE if effect else MAX_READ_RESPONSE
        # Effects and short reads keep the 2s relay deadline. session_frame must
        # outlast the hub's unchanged-frame hold.
        timeout = READ_RELAY_TIMEOUT if kind == "session_frame" else 2
        try:
            upstream = _post_json(origin + RELAY_PATH, body, limit, timeout=timeout)
            result = self._transient_result(effect, operation_id, upstream)
            # A held call can finish after a takeover. Never return the old
            # MAIN's answer as the new authority's. An effect stays unresolved
            # and is not posted a second time.
            if _relay_authority_changed(self, status, origin):
                raise GatewayError("no_relay", operation_id=operation_id)
        except GatewayError:
            if effect:
                with self._lock:
                    slot = self._effects.get(operation_id)
                    if slot is None or slot.get("state") != "done":
                        self._effects[operation_id] = {"identity": identity, "state": "error"}
            raise GatewayError("no_relay", operation_id=operation_id) from None
        if effect:
            with self._lock:
                self._effects[operation_id] = {"identity": identity, "state": "done", "result": result}
        return result

    @staticmethod
    def _transient_result(effect, operation_id, upstream):
        if upstream.get("operation_id") != operation_id:
            raise GatewayError("no_relay")
        if effect:
            if set(upstream) != {"operation_id", "accepted"} or not isinstance(upstream["accepted"], bool):
                raise GatewayError("no_relay")
            return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                    "upstream_accepted": upstream["accepted"]}
        if not set(upstream) <= {"operation_id", "result", "raw_b64"} or not isinstance(
                upstream.get("result"), dict):
            raise GatewayError("no_relay")
        raw = None
        if "raw_b64" in upstream:
            text = upstream["raw_b64"]
            if not isinstance(text, str) or len(text) > MAX_RAW * 4 // 3 + 8:
                raise GatewayError("no_relay")
            try:
                raw = base64.b64decode(text.encode("ascii"), validate=True)
            except (ValueError, UnicodeError):
                raise GatewayError("no_relay") from None
            if len(raw) > MAX_RAW:
                raise GatewayError("no_relay")
        return {"operation_id": operation_id, "relayed": True, "effect_authorized": False,
                "result": upstream["result"], "raw": raw}

    def enroll(self, owner_proof, payload):
        """Bind an installation public key and capabilities. Owner proof required.

        `client` devices are directory entries only. They are not authority
        participants, so a phone cannot satisfy or block a fence. Nothing in
        the config file enrolls a device, and this method does not run without
        a fresh owner proof.

        The participant row and the directory row commit in one transaction,
        or neither does. RAM is updated only after that commit. A client id
        and a participant id cannot replace each other. A restart loads the
        directory back. Stored columns are the device id, installation id,
        kind, public key, capability list, and optional loopback relay origin.
        The owner signature and the request body are not stored.
        """
        self._accept_owner(owner_proof, "enroll", _exact(payload, (
            "device_id", "installation_id", "kind", "public_key", "capabilities"), ("relay_origin",)))
        device_id, installation_id = _ident(payload["device_id"]), _ident(payload["installation_id"])
        kind = payload["kind"]
        if kind not in {"pc", "server", "client"}:
            raise GatewayError("bad_request")
        public_key = _public_key(payload["public_key"])
        capabilities = self._capabilities(kind, payload["capabilities"])
        relay_origin = pin_relay(payload["relay_origin"]) if "relay_origin" in payload else None
        record = _Record(device_id, installation_id, kind, public_key, capabilities, relay_origin)
        # Gateway lock first, then the store lock inside the helper, so an
        # authenticate on another thread cannot invert the order. Membership
        # and the directory row commit together before RAM sees the device.
        with self._lock:
            self._save_enrollment(record)
            self._devices[device_id] = record
        return {"device_id": device_id, "installation_id": installation_id, "kind": kind,
                "capabilities": sorted(capabilities)}

    def disable_participant(self, owner_proof, payload):
        """Durably revoke a participant after fresh owner proof.

        A participant row stays, with `eligible` false, so acknowledge still
        reaches the store and returns `revoked`. Status and relay refuse that
        installation. A client has no participant row: the matching directory
        entry is deleted, and a second call is `unknown_participant`.
        """
        self._accept_owner(owner_proof, "disable", _exact(payload, ("device_id", "installation_id")))
        device_id = _ident(payload["device_id"])
        installation_id = _ident(payload["installation_id"])
        with self._lock:
            try:
                result = self._store(lambda: self.store.disable_participant(device_id, installation_id))
            except GatewayError as exc:
                if exc.code != "unknown_participant":
                    raise
                result = self._remove_client_directory(device_id, installation_id)
                if result is None:
                    raise
        return result if isinstance(result, dict) else {"device_id": device_id, "disabled": True}

    def _remove_client_directory(self, device_id, installation_id):
        """Delete one client directory row. None when this is not that client.

        Caller holds the gateway lock. The store lock is taken after it.
        A participant row is left for `disable_participant` on the store.
        """
        store = self.store
        with store._lock:
            try:
                participant = store._db.execute(
                    "SELECT 1 FROM participants WHERE device_id=?", (device_id,)).fetchone()
                if participant is not None:
                    return None
                row = store._db.execute(
                    "SELECT installation_id, kind FROM gateway_directory WHERE device_id=?",
                    (device_id,)).fetchone()
                if (row is None or row["kind"] != "client"
                        or row["installation_id"] != installation_id):
                    return None
                store._db.execute(
                    "DELETE FROM gateway_directory WHERE device_id=? AND installation_id=? AND kind='client'",
                    (device_id, installation_id))
            except sqlite3.Error:
                raise GatewayError("unavailable") from None
        current = self._devices.get(device_id)
        if (current is not None and current.installation_id == installation_id
                and current.kind == "client"):
            self._devices.pop(device_id, None)
        return {"device_id": device_id, "installation_id": installation_id, "kind": "client",
                "eligible": False, "disabled": True, "already": False}

    def set_mode(self, owner_proof, payload):
        """One cutover edge. The store still refuses jumps to V2_ACTIVE."""
        self._accept_owner(owner_proof, "mode", _exact(payload, ("mode", "expected_mode_version")))
        mode = payload["mode"]
        if mode == "CANCEL_INITIAL_BOOTSTRAP":
            version = self._store(lambda: self.store.cancel_initial_bootstrap(
                _nnint(payload["expected_mode_version"])))
            return {"mode": "LEGACY_ACTIVE", "mode_version": version,
                    "initial_bootstrap_cancelled": True}
        if not isinstance(mode, str) or mode not in {
                "PREPARING_V2", "QUIESCED", "V2_ONLY_NO_MAIN", "LEGACY_ACTIVE", "ROLLBACK_QUIESCED"}:
            raise GatewayError("bad_request")
        version = self._store(lambda: self.store.set_mode(mode, _nnint(payload["expected_mode_version"])))
        return {"mode": mode, "mode_version": version}

    @staticmethod
    def _capabilities(kind, values):
        if not isinstance(values, list) or not values or len(values) > len(CAPABILITIES):
            raise GatewayError("bad_request")
        caps = []
        for value in values:
            caps.append(_ident(value))
        unique = frozenset(caps)
        if len(unique) != len(caps) or "manage_main" in unique or not unique <= CAPABILITIES:
            raise GatewayError("bad_request")
        allowed = {"pc": _PC_CAPS, "server": _SERVER_CAPS, "client": _CLIENT_CAPS}[kind]
        if not unique <= allowed:
            raise GatewayError("bad_request")
        return unique

    def _ticket_fields(self, payload):
        fields = {key: payload[key] for key in _TICKET_FIELDS}
        for key in ("admission_id", "target", "operation_id", "attempt_id", "main"):
            _ident(fields[key])
        _digest(fields["digest"])
        for key in ("incarnation", "epoch", "issuer_incarnation"):
            _nnint(fields[key])
        fields["issued_at"] = _number(fields["issued_at"])
        fields["admit_until"] = _number(fields["admit_until"])
        return {key: fields[key] for key in _TICKET_FIELDS}

    def _relay_status(self):
        status = self._store(self.store.status)
        if (status.get("mode") != "V2_ACTIVE" or not status.get("main_ready")
                or not status.get("lease_valid") or not status.get("main")):
            raise GatewayError("no_relay")
        return status

    # --- durable directory -------------------------------------------------------------

    def _store_tx(self):
        """One immediate transaction on the authority connection.

        Caller may already hold the gateway lock. This acquires `store._lock`
        after that, never before. It does not call `AuthorityStore._tx`,
        because that helper releases the store lock itself. A closed store
        is not opened again here.
        """
        if self._closed:
            raise GatewayError("unavailable")
        store = self.store
        store._lock.acquire()
        try:
            store._db.execute("BEGIN IMMEDIATE")
        except BaseException:
            store._lock.release()
            raise
        return _GatewayTx(store)

    def _load_durable(self):
        """Read the directory and unexpired nonce digests. Invalid rows refuse startup."""
        if not hasattr(self.store, "_db") or not hasattr(self.store, "_lock"):
            raise GatewayError("bad_state")
        try:
            with self._store_tx() as db:
                _ensure_gateway_schema(db)
                now = self._wall()
                db.execute("DELETE FROM gateway_nonces WHERE expires_at < ?", (now,))
                rows = list(db.execute(
                    "SELECT device_id, installation_id, kind, public_key, capabilities, relay_origin"
                    " FROM gateway_directory ORDER BY device_id"))
                nonces = list(db.execute(
                    "SELECT device_id, nonce_digest, expires_at FROM gateway_nonces"))
        except GatewayError:
            raise
        except sqlite3.Error:
            raise GatewayError("bad_state") from None
        devices = {}
        for row in rows:
            record = _record_from_stored(row)
            if record.device_id in devices:
                raise GatewayError("bad_state")
            devices[record.device_id] = record
        loaded = {}
        for row in nonces:
            key, expires = _nonce_from_stored(row)
            loaded[key] = expires
        if len(loaded) > MAX_NONCES:
            raise GatewayError("unavailable")
        self._devices = devices
        self._nonces = loaded

    def _save_enrollment(self, record):
        """Commit participant membership and the directory row together.

        `AuthorityStore.apply_participant_registration` writes on this open
        transaction. `register_participant` would commit the participant
        before the directory insert. The same installation updates kind only,
        so acknowledgements stay and a revoked installation is not made
        eligible again. A new installation resets ack columns through the
        store helper. The current holder and an open transition endpoint are
        refused with no write.

        A client is a directory row only and does not call `_guard`, so a
        fresh client id can still enroll while quarantined. A client write
        is refused when a participant row exists, or when the directory row
        is not already that client installation. A PC or server write is
        refused when the directory id is client-only. RAM is updated by the
        caller only after this returns.
        """
        caps = _caps_json(record.capabilities)
        try:
            with self._store_tx() as db:
                _ensure_gateway_schema(db)
                if record.kind == "client":
                    _refuse_client_namespace(db, record)
                else:
                    self.store._guard()
                    _refuse_client_only_id(db, record)
                    _apply_participant_registration(
                        self.store, db, record.device_id, record.installation_id, record.kind)
                    _require_participant_binding(db, record)
                db.execute(
                    "INSERT INTO gateway_directory(device_id, installation_id, kind, public_key,"
                    " capabilities, relay_origin) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(device_id) DO UPDATE SET"
                    " installation_id=excluded.installation_id, kind=excluded.kind,"
                    " public_key=excluded.public_key, capabilities=excluded.capabilities,"
                    " relay_origin=excluded.relay_origin",
                    (record.device_id, record.installation_id, record.kind, record.public_key,
                     caps, record.relay_origin))
        except GatewayError:
            raise
        except AuthorityError as exc:
            raise GatewayError(exc.code, wait=exc.wait) from None
        except sqlite3.Error:
            raise GatewayError("unavailable") from None

    def _persist_device_nonce(self, device_id, nonce_digest, expires):
        """Commit one replay digest. The nonce text is not a parameter we store."""
        now = self._wall()
        try:
            with self._store_tx() as db:
                _ensure_gateway_schema(db)
                db.execute("DELETE FROM gateway_nonces WHERE expires_at < ?", (now,))
                found = db.execute(
                    "SELECT 1 FROM gateway_nonces WHERE device_id=? AND nonce_digest=?",
                    (device_id, nonce_digest)).fetchone()
                if found is not None:
                    raise GatewayError("unauthorized")
                count = db.execute("SELECT COUNT(*) FROM gateway_nonces").fetchone()[0]
                if count >= MAX_NONCES:
                    raise GatewayError("unavailable")
                db.execute(
                    "INSERT INTO gateway_nonces(device_id, nonce_digest, expires_at) VALUES(?,?,?)",
                    (device_id, nonce_digest, float(expires)))
        except GatewayError:
            raise
        except sqlite3.Error:
            raise GatewayError("unavailable") from None

    # --- HTTP --------------------------------------------------------------------------

    def _bound_endpoint(self):
        httpd = self._httpd
        if httpd is None:
            return None
        host, port = httpd.server_address[:2]
        return str(host), int(port)

    def _one_header(self, handler, name):
        """One header value, or None when it is missing or repeated."""
        headers = handler.headers
        get_all = getattr(headers, "get_all", None)
        if get_all is None:
            found = headers.get(name) if hasattr(headers, "get") else None
            return None if found is None else str(found).strip()
        values = [str(value).strip() for value in (get_all(name) or [])]
        if len(values) != 1:
            return None
        return values[0]

    def _peer_ok(self, handler):
        """A loopback bind accepts only a loopback TCP peer. Tailscale headers are not read."""
        bound = self._bound_endpoint()
        if bound is None or not _loopback_ip(bound[0]):
            return bound is not None
        addr = getattr(handler, "client_address", None)
        peer = addr[0] if isinstance(addr, tuple) and addr else addr
        return _loopback_ip(peer)

    def _host_ok(self, handler):
        """Host is the bound address, with or without :port. A loopback bind also allows
        127.0.0.1, localhost, and ::1, so a raw `Host: 127.0.0.1` (no port) still reaches
        the body-size check. A second Host header is refused."""
        bound = self._bound_endpoint()
        raw = self._one_header(handler, "Host")
        if bound is None or not raw:
            return False
        host, port = bound
        name, got = _split_host(raw)
        if name is None:
            return False
        bound_name = host.lower().strip("[]")
        if _loopback_ip(host):
            if name in {bound_name, "127.0.0.1", "localhost", "::1"}:
                return got in (None, port)
            # tailscale serve keeps the caller's Host; the TCP peer is still loopback (tailscaled)
            return (got in (None, 443) and name.endswith(".ts.net")
                    and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", name))
        return name == bound_name and got in (None, port)

    def _origin_ok(self, handler):
        """A missing Origin is allowed. A present Origin must be this listener, once."""
        headers = handler.headers
        get_all = getattr(headers, "get_all", None)
        if get_all is None:
            found = headers.get("Origin") if hasattr(headers, "get") else None
            values = [] if not found else [str(found).strip()]
        else:
            values = [str(value).strip() for value in (get_all("Origin") or []) if str(value or "").strip()]
        if not values:
            return True
        if len(values) != 1:
            return False
        bound = self._bound_endpoint()
        if bound is None:
            return False
        host, port = bound
        origin = values[0]
        if origin.casefold() == "null" or origin.endswith("/"):
            return False
        scheme = "https" if self._tls else "http"
        names = ["127.0.0.1", "localhost", "::1"] if _loopback_ip(host) else [host]
        allowed = set()
        for name in names:
            shown = f"[{name}]" if ":" in name else name
            allowed.add(f"{scheme}://{shown}:{port}")
        return origin in allowed

    def _http(self, handler):
        if self._closing or self._closed:
            self._reply(handler, 503, {"error": "unavailable"}, None, "unavailable")
            return
        code, status, wait, device_id, operation_id = "unavailable", 503, None, None, None
        try:
            if not self._peer_ok(handler) or not self._host_ok(handler) or not self._origin_ok(handler):
                raise GatewayError("forbidden")
            if "?" in handler.path or handler.path != handler.path.split("?")[0]:
                raise GatewayError("bad_request")
            parsed = self._read_json(handler)
            path = handler.path
            if path == "/v1/authority/challenge":
                result = self.issue_challenge(_exact(parsed, ("request_digest",))["request_digest"])
            elif path in _OWNER_PURPOSE:
                proof = self._owner_from_body(path, parsed)
                result = self._dispatch_owner(_OWNER_PURPOSE[path], proof, parsed["payload"])
            elif path in _DEVICE_PURPOSE:
                principal = self._device_from_body(path, parsed)
                device_id = principal.device_id
                result = self._dispatch_device(_DEVICE_PURPOSE[path], principal, parsed["payload"])
            else:
                raise GatewayError("not_found")
            self._reply(handler, 200, result, None, None, device_id)
            return
        except GatewayError as exc:
            code, status, wait = exc.code, exc.status, exc.wait
            operation_id = exc.operation_id
        except Exception:
            code, status = "unavailable", 503
        body = {"error": code}
        if operation_id:
            body["operation_id"] = operation_id
        if _finite_wait(wait) and wait > 0:
            body["wait"] = wait
        self._reply(handler, status, body, wait, code, device_id)

    def _dispatch_device(self, purpose, principal, payload):
        if purpose == "status":
            return self.status(principal, payload)
        if purpose == "renew":
            return self.renew(principal, payload)
        if purpose == "release":
            return self.release(principal, payload)
        if purpose == "acknowledge":
            return self.acknowledge(principal, payload)
        if purpose == "readiness":
            return self.transition_readiness(principal, payload)
        if purpose == "consume":
            return self.consume(principal, payload)
        if purpose == "lookup":
            return self.admission(principal, payload)
        if purpose == "self_policy":
            return self.self_policy(principal, payload)
        if purpose == "self_transition":
            return self.self_transition(principal, payload)
        if purpose == "self_step":
            return self.self_step(principal, payload)
        return self.relay(principal, payload)

    def _dispatch_owner(self, purpose, proof, payload):
        if purpose == "transition":
            return self.transition_request(proof, payload)
        if purpose == "step":
            return self.transition_step(proof, payload)
        if purpose == "enroll":
            return self.enroll(proof, payload)
        if purpose == "disable":
            return self.disable_participant(proof, payload)
        return self.set_mode(proof, payload)

    def _device_from_body(self, path, parsed):
        body = _exact(parsed, ("payload", "device"))
        payload, auth = body["payload"], body["device"]
        if not isinstance(payload, dict) or not isinstance(auth, dict):
            raise GatewayError("bad_request")
        auth = _exact(auth, ("device_id", "installation_id", "nonce", "issued_at", "signature"))
        return self.authenticate_device(
            device_id=auth["device_id"], installation_id=auth["installation_id"], nonce=auth["nonce"],
            issued_at=auth["issued_at"], purpose=_DEVICE_PURPOSE[path], payload=payload,
            signature=auth["signature"])

    def _owner_from_body(self, path, parsed):
        body = _exact(parsed, ("payload", "owner"))
        payload, auth = body["payload"], body["owner"]
        if not isinstance(payload, dict) or not isinstance(auth, dict):
            raise GatewayError("bad_request")
        auth = _exact(auth, ("challenge_id", "nonce", "issued_at", "signature"))
        return self.authenticate_owner(
            challenge_id=auth["challenge_id"], nonce=auth["nonce"], issued_at=auth["issued_at"],
            purpose=_OWNER_PURPOSE[path], payload=payload, signature=auth["signature"])

    def _read_json(self, handler):
        if handler.headers.get("Content-Type") != "application/json":
            raise GatewayError("bad_request")
        if handler.headers.get("Transfer-Encoding"):
            raise GatewayError("bad_request")
        length = handler.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise GatewayError("bad_request")
        size = int(length)
        if size < 2 or size > MAX_BODY:
            raise GatewayError("bad_request")
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise GatewayError("bad_request")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise GatewayError("bad_request") from None
        if not isinstance(parsed, dict):
            raise GatewayError("bad_request")
        return parsed

    def _reply(self, handler, status, obj, wait, code, device_id=None):
        try:
            raw = canonical(obj)
        except GatewayError:
            raw = b'{"error":"unavailable"}'
            status, code = 503, "unavailable"
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("X-Frame-Options", "DENY")
            handler.send_header("Content-Security-Policy",
                                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
            handler.send_header("Connection", "close")
            if wait is not None:
                handler.send_header("Retry-After", str(max(0, math.ceil(wait))))
            handler.end_headers()
            handler.wfile.write(raw)
        except Exception:
            pass
        self._remember(handler.command, handler.path, status, code, device_id)

    def _remember(self, method, route, status, code, device_id):
        if device_id is not None:
            try:
                device_id = _ident(device_id)
            except GatewayError:
                device_id = None
        event = {"method": method, "route": route.split("?")[0], "status": status, "code": code,
                 "device_id": device_id}
        with self._lock:
            self._events.append(event)
            if len(self._events) > 200:
                del self._events[:100]


def _project_admission(record):
    view = {}
    for key in _ADMISSION_KEYS:
        if key in record and record[key] is not None:
            view[key] = record[key]
    return view


def _post_json(url, data, limit, timeout=2):
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with _opener().open(request, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            if status != 200:
                raise GatewayError("no_relay")
            raw = response.read(limit + 1)
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("no_relay") from None
    if len(raw) > limit:
        raise GatewayError("no_relay")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise GatewayError("no_relay") from None
    if not isinstance(parsed, dict):
        raise GatewayError("no_relay")
    return parsed


class AuthorityClient:
    """HTTP client for one pinned gateway origin and one pinned `authority_id`.

    Methods do not take a URL. HTTP is allowed only for a loopback origin. Redirects
    and environment proxies are not followed. A response that names another authority
    is refused.
    """

    def __init__(self, origin, *, authority_id, wall=time.time, device_id=None, installation_id=None,
                 private_key_path=None, owner_private_key_path=None):
        self.origin = pin_origin(origin)
        self.authority_id = _ident(authority_id)
        self._wall = wall
        self.device_id = _ident(device_id) if device_id is not None else None
        self.installation_id = _ident(installation_id) if installation_id is not None else None
        self._key = None if private_key_path is None else Path(private_key_path)
        self._owner_key = None if owner_private_key_path is None else Path(owner_private_key_path)

    @property
    def can_sign_owner(self):
        """True when this process has the owner private key. Possession is not a grant."""
        return self._owner_key is not None

    def status(self):
        return self._device("status", {})

    def renew(self, holder, lease_id, incarnation, epoch):
        return self._device("renew", {"holder": holder, "lease_id": lease_id,
                                       "incarnation": incarnation, "epoch": epoch})

    def release(self, holder, lease_id, incarnation, epoch):
        return self._device("release", {"holder": holder, "lease_id": lease_id,
                                         "incarnation": incarnation, "epoch": epoch})

    def acknowledge(self, mode_version, fenced_epoch, boot_id=None):
        payload = {"mode_version": mode_version, "fenced_epoch": fenced_epoch}
        if boot_id is not None:
            payload["boot_id"] = boot_id
        return self._device("acknowledge", payload)

    def readiness(self, transition_id, control_ready, ai_ready, checkpoint=None):
        payload = {"transition_id": transition_id, "control_ready": control_ready, "ai_ready": ai_ready}
        if checkpoint is not None:
            payload["checkpoint"] = checkpoint
        return self._device("readiness", payload)

    def consume(self, ticket, ticket_signature):
        return self._device("consume", {**ticket, "ticket_signature": ticket_signature})

    def admission(self, admission_id):
        return self._device("lookup", {"admission_id": admission_id})

    def relay(self, operation_id, target, attempt_id, digest, capability):
        return self._device("relay", {"operation_id": operation_id, "target": target,
                                       "attempt_id": attempt_id, "digest": digest, "capability": capability})

    def sign_ticket(self, ticket):
        fields = {key: ticket[key] for key in _TICKET_FIELDS}
        if self._key is None:
            raise GatewayError("unauthorized")
        return sign_message(TICKET_NS, self._key, _message(TICKET_NS, fields))

    def transition_request(self, client_key, action, candidate, expected_incarnation, expected_epoch,
                           expected_policy, profile="control"):
        return self._owner("transition", {
            "client_key": client_key, "action": action, "candidate": candidate,
            "expected_incarnation": expected_incarnation, "expected_epoch": expected_epoch,
            "expected_policy": expected_policy, "profile": profile,
        })

    def step(self, transition_id, step, canary_ok=None):
        payload = {"transition_id": transition_id, "step": step}
        if canary_ok is not None:
            payload["canary_ok"] = canary_ok
        return self._owner("step", payload)

    def enroll(self, record):
        return self._owner("enroll", record)

    def self_policy(self):
        return self._device("self_policy", {})

    def self_request(self, client_key, action, expected_incarnation, expected_epoch,
                     expected_policy, profile="control"):
        return self._device("self_transition", {
            "client_key": client_key, "action": action,
            "expected_incarnation": expected_incarnation, "expected_epoch": expected_epoch,
            "expected_policy": expected_policy, "profile": profile,
        })

    def self_step(self, transition_id, step, canary_ok=None):
        payload = {"transition_id": transition_id, "step": step}
        if canary_ok is not None:
            payload["canary_ok"] = canary_ok
        return self._device("self_step", payload)

    def set_mode(self, mode, expected_mode_version):
        return self._owner("mode", {"mode": mode, "expected_mode_version": expected_mode_version})

    def disable_participant(self, device_id, installation_id):
        return self._owner("disable", {"device_id": device_id, "installation_id": installation_id})

    def _device(self, purpose, payload):
        if self._key is None or self.device_id is None or self.installation_id is None:
            raise GatewayError("unauthorized")
        issued_at = int(self._wall())
        nonce = "n-" + secrets.token_hex(8)
        digest = payload_digest(payload)
        signature = sign_message(DEVICE_NS, self._key, _message(DEVICE_NS, {
            "v": 1, "device_id": self.device_id, "installation_id": self.installation_id,
            "nonce": nonce, "issued_at": issued_at, "purpose": purpose, "payload_digest": digest,
        }))
        return self._send(_PURPOSE_PATH[purpose], {"payload": payload, "device": {
            "device_id": self.device_id, "installation_id": self.installation_id, "nonce": nonce,
            "issued_at": issued_at, "signature": signature,
        }})

    def _owner(self, purpose, payload):
        if self._owner_key is None:
            raise GatewayError("unauthorized")
        digest = payload_digest(payload)
        challenge = self._send("/v1/authority/challenge", {"request_digest": digest})
        issued_at = int(self._wall())
        nonce = "n-" + secrets.token_hex(8)
        signature = sign_message(OWNER_NS, self._owner_key, _message(OWNER_NS, {
            "v": 1, "challenge_id": challenge["challenge_id"], "nonce": nonce,
            "issued_at": issued_at, "payload_digest": digest,
        }))
        return self._send(_OWNER_PATH[purpose], {"payload": payload, "owner": {
            "challenge_id": challenge["challenge_id"], "nonce": nonce, "issued_at": issued_at,
            "signature": signature,
        }})

    def _send(self, path, obj):
        try:
            return self._send_once(path, obj)
        finally:
            jobtiming.mark("authority" + path[len("/v1/authority"):])

    def _send_once(self, path, obj):
        if not path.startswith("/v1/authority/"):
            raise GatewayError("bad_request")
        request = urllib.request.Request(self.origin + path, data=canonical(obj), method="POST")
        target = urlsplit(request.full_url)
        pinned = urlsplit(self.origin)
        if target.hostname != pinned.hostname or target.port != pinned.port or target.scheme != pinned.scheme:
            raise GatewayError("bad_origin")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        retry_after = None
        try:
            with _opener().open(request, timeout=10) as response:
                status = getattr(response, "status", response.getcode())
                retry_after = response.headers.get("Retry-After")
                raw = response.read(MAX_RESPONSE + 1)
        except GatewayError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                raw = exc.read(MAX_RESPONSE + 1)
            finally:
                exc.close()
        except Exception:
            raise GatewayError("unavailable") from None
        if len(raw) > MAX_RESPONSE:
            raise GatewayError("unavailable")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise GatewayError("unavailable") from None
        if not isinstance(body, dict):
            raise GatewayError("unavailable")
        if "authority_id" in body and body["authority_id"] != self.authority_id:
            raise GatewayError("authority_mismatch")
        if status != 200:
            code = body.get("error") if _safe_code(body.get("error")) else "unavailable"
            operation = body.get("operation_id")
            if not isinstance(operation, str) or not _ID.fullmatch(operation):
                operation = None
            agreed = _agreed_wait(body.get("wait"), retry_after) if "wait" in body else None
            raise GatewayError(code, status=status, wait=agreed, operation_id=operation)
        return body


_LEASE_KEYS = frozenset({"holder", "lease_id", "incarnation", "epoch"})
_REQUEST_KEYS = frozenset({
    "client_key", "action", "candidate", "expected_incarnation", "expected_epoch",
    "expected_policy", "profile",
})


class AuthenticatedPort:
    """Store-shaped calls for `MainRuntime` and `ExecutorGuard`, over `AuthorityClient`.

    The installation is fixed when the client is built. A `device_id` or
    `installation_id` argument that names someone else is refused before any
    request is signed. Owner steps need `can_sign_owner`. A `caller` string in a
    transition request is not forwarded and is not proof.

    `consume` requires `ticket_signature`. `ExecutorGuard.accept` does not pass
    that field itself, so `integration.SignedAuthority` captures the MAIN signature at
    verify time and passes it here. This port never accepts a shared-secret identity.

    Refusals are `AuthorityError` with the stable code as the message, which is
    what those callers already catch. Nothing here is a live failover.
    """

    def __init__(self, client):
        if not isinstance(client, AuthorityClient) or client.device_id is None or client._key is None:
            raise GatewayError("unauthorized")
        self._client = client

    def status(self):
        return self._call(self._client.status)

    def renew(self, lease_tuple):
        self._lease(lease_tuple)
        return self._call(lambda: self._client.renew(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"]))

    def release(self, lease_tuple):
        self._lease(lease_tuple)
        return self._call(lambda: self._client.release(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"]))

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        if device_id != self._client.device_id or installation_id != self._client.installation_id:
            raise AuthorityError("forbidden", "forbidden")
        return self._call(lambda: self._client.acknowledge(mode_version, fenced_epoch, boot_id))

    def transition_request(self, request):
        self._require_owner()
        fields = self._request(request)
        return self._call(lambda: self._client.transition_request(
            fields["client_key"], fields["action"], fields["candidate"],
            fields["expected_incarnation"], fields["expected_epoch"],
            fields["expected_policy"], fields["profile"]))

    def transition_readiness(self, transition_id, readiness):
        if not isinstance(readiness, dict):
            raise AuthorityError("bad_request", "bad_request")
        if set(readiness) & _FORBIDDEN_KEYS or set(readiness) - {"control_ready", "ai_ready", "checkpoint"}:
            raise AuthorityError("bad_request", "bad_request")
        if "control_ready" not in readiness or "ai_ready" not in readiness:
            raise AuthorityError("bad_request", "bad_request")
        # Absent and JSON null are the same: the client omits the field. 1 is not true.
        checkpoint = readiness.get("checkpoint")
        return self._call(lambda: self._client.readiness(
            transition_id, readiness["control_ready"], readiness["ai_ready"], checkpoint))

    def transition_step(self, transition_id, step, canary_ok=None):
        self._require_owner()
        return self._call(lambda: self._client.step(transition_id, step, canary_ok))

    def consume(self, ticket=None, **fields):
        """Target consume. Missing `ticket_signature` does not call the store."""
        payload = {}
        if ticket is not None:
            if not isinstance(ticket, dict):
                raise AuthorityError("bad_request", "bad_request")
            payload.update(ticket)
        if fields:
            payload.update(fields)
        if set(payload) & _FORBIDDEN_KEYS:
            raise AuthorityError("bad_request", "bad_request")
        signature = payload.get("ticket_signature")
        if not isinstance(signature, str):
            raise AuthorityError("unauthorized", "unauthorized")
        if set(payload) - set(_TICKET_FIELDS) - {"ticket_signature"}:
            raise AuthorityError("bad_request", "bad_request")
        if payload.get("target") != self._client.device_id:
            raise AuthorityError("forbidden", "forbidden")
        body = {key: payload[key] for key in _TICKET_FIELDS}
        return self._call(lambda: self._client.consume(body, signature))

    def admission(self, admission_id):
        return self._call(lambda: self._client.admission(admission_id))

    def _require_owner(self):
        if not self._client.can_sign_owner:
            raise AuthorityError("unauthorized", "unauthorized")

    def _lease(self, lease_tuple):
        if not isinstance(lease_tuple, dict) or set(lease_tuple) != _LEASE_KEYS:
            raise AuthorityError("bad_request", "bad_request")
        if lease_tuple["holder"] != self._client.device_id:
            raise AuthorityError("forbidden", "forbidden")

    def _request(self, request):
        if not isinstance(request, dict) or set(request) & _FORBIDDEN_KEYS:
            raise AuthorityError("bad_request", "bad_request")
        if set(request) - _REQUEST_KEYS - {"caller"} or not _REQUEST_KEYS <= set(request):
            raise AuthorityError("bad_request", "bad_request")
        return request

    def _call(self, fn):
        try:
            return fn()
        except GatewayError as exc:
            raise AuthorityError(exc.code, exc.code, wait=exc.wait) from None


class _GatewayTx:
    """Finishes a transaction `_store_tx` already began. Releases the store lock."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self.store._db

    def __exit__(self, kind, exc, tb):
        try:
            self.store._db.execute("ROLLBACK" if kind is not None else "COMMIT")
        finally:
            self.store._lock.release()
        return False


_GATEWAY_SCHEMA_VERSION = "1"
_ALLOWED_SNAPSHOT_TABLES = frozenset({
    "meta", "authority", "participants", "transitions", "admissions",
    "gateway_directory", "gateway_nonces",
})
_CONFIG_KEYS = frozenset({
    "state_path", "fleet_id", "authority_id", "owner_public_keys",
    "bind_host", "bind_port", "tls_cert", "tls_key",
    "lease_seconds", "margin", "auto_failover", "self_recovery", "phone_front",
})
_CONFIG_REQUIRED = frozenset({"state_path", "fleet_id", "authority_id", "owner_public_keys"})
_MAX_CONFIG_BYTES = 65536
_DIRECTORY_DDL = (
    "CREATE TABLE IF NOT EXISTS gateway_directory ("
    " device_id TEXT PRIMARY KEY,"
    " installation_id TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " public_key TEXT NOT NULL,"
    " capabilities TEXT NOT NULL,"
    " relay_origin TEXT)"
)
_NONCE_DDL = (
    "CREATE TABLE IF NOT EXISTS gateway_nonces ("
    " device_id TEXT NOT NULL,"
    " nonce_digest TEXT NOT NULL,"
    " expires_at REAL NOT NULL,"
    " PRIMARY KEY (device_id, nonce_digest))"
)


def _apply_participant_registration(store, db, device_id, installation_id, kind):
    """Participant half of an enrollment transaction. Does not commit.

    `store.apply_participant_registration` is the store's no-reenable write.
    Calling `register_participant` here would commit before the directory row.
    """
    if kind not in ("pc", "server"):
        raise GatewayError("bad_request")
    store.apply_participant_registration(db, device_id, installation_id, kind, True)


def _directory_identity(db, device_id):
    return db.execute(
        "SELECT installation_id, kind FROM gateway_directory WHERE device_id=?",
        (device_id,)).fetchone()


def _refuse_client_namespace(db, record):
    """A client cannot take a participant id or retarget an existing directory row.

    A missing directory row is a new client id. The same client installation
    may replace its key. Any other client write is `conflict` and writes nothing.
    """
    participant = db.execute(
        "SELECT 1 FROM participants WHERE device_id=?", (record.device_id,)).fetchone()
    if participant is not None:
        raise GatewayError("conflict")
    directory = _directory_identity(db, record.device_id)
    if directory is None:
        return
    if directory["kind"] != "client" or directory["installation_id"] != record.installation_id:
        raise GatewayError("conflict")


def _refuse_client_only_id(db, record):
    """A PC or server cannot take an id that is only a client directory row."""
    directory = _directory_identity(db, record.device_id)
    if directory is None or directory["kind"] != "client":
        return
    participant = db.execute(
        "SELECT 1 FROM participants WHERE device_id=?", (record.device_id,)).fetchone()
    if participant is None:
        raise GatewayError("conflict")


def _require_participant_binding(db, record):
    """The directory upsert may record only the participant row this transaction wrote."""
    bound = db.execute(
        "SELECT installation_id, kind FROM participants WHERE device_id=?",
        (record.device_id,)).fetchone()
    if (bound is None or bound["installation_id"] != record.installation_id
            or bound["kind"] != record.kind):
        raise GatewayError("conflict")


def _ensure_gateway_schema(db):
    """Create the gateway tables. They hold no prompt, body, or private key column."""
    db.execute(_DIRECTORY_DDL)
    db.execute(_NONCE_DDL)
    row = db.execute("SELECT value FROM meta WHERE key='gateway_schema'").fetchone()
    if row is None:
        db.execute(
            "INSERT INTO meta(key, value) VALUES('gateway_schema', ?)", (_GATEWAY_SCHEMA_VERSION,))
    elif row[0] != _GATEWAY_SCHEMA_VERSION:
        raise GatewayError("bad_state")


def _caps_json(capabilities):
    return json.dumps(sorted(capabilities), separators=(",", ":"), ensure_ascii=True)


def _nonce_digest(nonce):
    """Replay identity for one device nonce. The nonce text is not returned."""
    return "sha256:" + hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _record_from_stored(row):
    """One directory row, or `bad_state`. A loose row is not skipped."""
    try:
        device_id = _ident(row["device_id"])
        installation_id = _ident(row["installation_id"])
        kind = row["kind"]
        if kind not in {"pc", "server", "client"}:
            raise GatewayError("bad_state")
        public_key = _public_key(row["public_key"])
        text = row["capabilities"]
        if not isinstance(text, str):
            raise GatewayError("bad_state")
        parsed = json.loads(text)
        capabilities = ContinuityGateway._capabilities(kind, parsed)
        if _caps_json(capabilities) != text:
            raise GatewayError("bad_state")
        relay = row["relay_origin"]
        if relay is not None:
            if not isinstance(relay, str):
                raise GatewayError("bad_state")
            pinned = pin_relay(relay)
            if pinned != relay:
                raise GatewayError("bad_state")
            relay = pinned
    except GatewayError as exc:
        if exc.code != "bad_state":
            raise GatewayError("bad_state") from None
        raise
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise GatewayError("bad_state") from None
    return _Record(device_id, installation_id, kind, public_key, capabilities, relay)


def _nonce_from_stored(row):
    try:
        device_id = _ident(row["device_id"])
        digest = _digest(row["nonce_digest"])
        expires = row["expires_at"]
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            raise GatewayError("bad_state")
        expires = float(expires)
        if not math.isfinite(expires) or expires < 0:
            raise GatewayError("bad_state")
    except GatewayError as exc:
        if exc.code != "bad_state":
            raise GatewayError("bad_state") from None
        raise
    except (TypeError, ValueError, KeyError):
        raise GatewayError("bad_state") from None
    return (device_id, digest), expires


SELF_RECOVERY_STABILITY_S = 30.0


def _self_recovery(value):
    """`{"candidates": [pc ids], "preferred": pc id}` or None (off).

    `preferred` must be a candidate; it is the PC that takes MAIN back after
    `stability_s` (at least 30 s) of steady readiness. No other keys.
    """
    if value is None:
        return None
    if isinstance(value, dict) and value.get("enabled") is False and set(value) <= {"enabled"}:
        return None
    if not isinstance(value, dict) or set(value) - {"candidates", "preferred", "stability_s"}:
        raise GatewayError("bad_request")
    candidates = value.get("candidates")
    if not isinstance(candidates, (list, tuple)) or not 0 < len(candidates) <= 8:
        raise GatewayError("bad_request")
    names = tuple(_ident(item) for item in candidates)
    if len(set(names)) != len(names):
        raise GatewayError("bad_request")
    preferred = value.get("preferred")
    if preferred is not None and preferred not in names:
        raise GatewayError("bad_request")
    stability = value.get("stability_s", SELF_RECOVERY_STABILITY_S)
    if (isinstance(stability, bool) or not isinstance(stability, (int, float))
            or not math.isfinite(stability) or stability < SELF_RECOVERY_STABILITY_S or stability > 3600):
        raise GatewayError("bad_request")
    return {"candidates": names, "preferred": preferred, "stability_s": float(stability)}


def _phone_front(value):
    """`{"bind_port": 1024-65535, "upstreams": {device: origin}}` or None (off).

    The front binds 127.0.0.1 only. Each origin is an https `*.ts.net` name or
    a loopback http origin; `PhoneFront` validates them.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"bind_port", "upstreams"}:
        raise GatewayError("bad_config")
    port = value["bind_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise GatewayError("bad_config")
    upstreams = value["upstreams"]
    if not isinstance(upstreams, dict) or not 0 < len(upstreams) <= 8:
        raise GatewayError("bad_config")
    from jarvis.continuity import phone_front
    clean = {}
    for device, origin in upstreams.items():
        try:
            clean[_ident(device)] = phone_front._check_origin(origin)
        except (GatewayError, ValueError):
            raise GatewayError("bad_config") from None
    return {"bind_port": port, "upstreams": clean}


class FailoverPolicy:
    """Manual MAIN promotion only.

    WS1 owns lease grant and fencing. This process has no safe hook that can
    promote a candidate, so it never does. `auto_failover` defaults to false.
    `FailoverPolicy(auto_failover=True)` raises `auto_failover_disabled`
    before a store is opened and before any promotion attempt. Any other
    value is `bad_config`.

    `on_unreachable` and `on_lease` return `action=none`. An unreachable peer
    is not a promotion. A live lease is not a promotion. Nothing here calls
    those methods on a timer.
    """

    def __init__(self, auto_failover=False):
        if auto_failover is True:
            raise GatewayError("auto_failover_disabled")
        if auto_failover is not False:
            raise GatewayError("bad_config")
        self.auto_failover = False

    def on_unreachable(self, device_id):
        """A silent peer does not elect a MAIN."""
        _ident(device_id)
        return {"action": "none", "auto_failover": False, "reason": "manual_only"}

    def on_lease(self, lease_valid):
        """A live or lapsed lease does not elect a MAIN."""
        if not isinstance(lease_valid, bool):
            raise GatewayError("bad_config")
        return {"action": "none", "auto_failover": False, "reason": "manual_only"}


class GatewayConfig:
    """Dedicated gateway JSON. This is not `jarvis.config.json`.

    Required: `state_path`, `fleet_id`, `authority_id`, `owner_public_keys`
    (a non-empty list of `ssh-ed25519` public key strings, no comment).
    Optional: `bind_host` (`127.0.0.1`), `bind_port` (`0`), `tls_cert` and
    `tls_key` (both paths or neither), `lease_seconds` and `margin` (test
    clocks only; omitted uses the store defaults), `auto_failover` (false).

    Unknown keys are `bad_config`, including any owner private-key field.
    `owner_public_keys` only verify owner signatures. They do not enroll a
    device. `serve` with `auto_failover` true is `auto_failover_disabled`
    and does not open `state_path`.

    On restore, `authority_id` is the snapshot pin, not the id written into
    the destination. `state_path` is the destination and must not exist yet.
    David then replaces this file with one whose `authority_id` is the
    `--new-authority-id` value before serving that destination.
    """

    def __init__(self, *, state_path, fleet_id, authority_id, owner_public_keys, bind_host,
                 bind_port, tls_cert, tls_key, lease_seconds, margin, auto_failover,
                 self_recovery=None, phone_front=None):
        self.state_path = state_path
        self.fleet_id = fleet_id
        self.authority_id = authority_id
        self.owner_public_keys = tuple(owner_public_keys)
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.lease_seconds = lease_seconds
        self.margin = margin
        self.auto_failover = auto_failover
        self.self_recovery = self_recovery
        self.phone_front = phone_front


def _live_roots():
    repo = _jarvis_home()
    return (repo / ".jarvis", Path.home() / ".local" / "state" / "jarvis")


def _reject_live(path):
    """Refuse the repo `.jarvis` tree and the user JARVIS state directory."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        raise GatewayError("bad_config") from None
    for root in _live_roots():
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            raise GatewayError("refuses_live_state")


def _config_pairs(pairs):
    out = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in out:
            raise GatewayError("bad_config")
        out[key] = value
    return out


def _as_ident(value):
    try:
        return _ident(value)
    except GatewayError:
        raise GatewayError("bad_config") from None


def _config_number(value, low, high, *, low_inclusive):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayError("bad_config")
    number = float(value)
    if not math.isfinite(number):
        raise GatewayError("bad_config")
    if low_inclusive:
        ok = low <= number <= high
    else:
        ok = low < number <= high
    if not ok:
        raise GatewayError("bad_config")
    return number


def _tls_file(value):
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise GatewayError("bad_config")
    file = Path(value)
    if file.is_symlink() or not file.is_file():
        raise GatewayError("bad_config")
    _reject_live(file)
    return str(file)


def load_gateway_config(path):
    """Parse one gateway JSON file. Does not open `state_path`.

    A file named `jarvis.config.json`, a symlink, or a file under the live
    state roots is refused before its bytes are used as config. The state
    path is checked the same way, still without opening it.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise GatewayError("bad_config")
    file = Path(path)
    if file.name.startswith("jarvis.config.json") or file.is_symlink():
        raise GatewayError("bad_config")
    if not file.is_file():
        raise GatewayError("not_found")
    resolved = file.resolve()
    if resolved.name.startswith("jarvis.config.json"):
        raise GatewayError("bad_config")
    _reject_live(resolved)
    try:
        if resolved.stat().st_size > _MAX_CONFIG_BYTES:
            raise GatewayError("bad_config")
        raw = resolved.read_text(encoding="utf-8")
    except GatewayError:
        raise
    except (OSError, UnicodeError):
        raise GatewayError("bad_config") from None
    try:
        data = json.loads(raw, object_pairs_hook=_config_pairs)
    except GatewayError:
        raise
    except (json.JSONDecodeError, UnicodeError):
        raise GatewayError("bad_config") from None
    if not isinstance(data, dict) or set(data) - _CONFIG_KEYS or not _CONFIG_REQUIRED <= set(data):
        raise GatewayError("bad_config")
    state_path = data["state_path"]
    if not isinstance(state_path, str) or not state_path or "\x00" in state_path or len(state_path) > 4096:
        raise GatewayError("bad_config")
    _reject_live(state_path)
    keys = data["owner_public_keys"]
    if not isinstance(keys, list) or not keys or len(keys) > 16:
        raise GatewayError("bad_config")
    public_keys = []
    for key in keys:
        try:
            public_keys.append(_public_key(key))
        except GatewayError:
            raise GatewayError("bad_config") from None
    if len(set(public_keys)) != len(public_keys):
        raise GatewayError("bad_config")
    bind_host = data.get("bind_host", "127.0.0.1")
    if not isinstance(bind_host, str):
        raise GatewayError("bad_config")
    ContinuityGateway._check_bind(bind_host, False)
    bind_port = data.get("bind_port", 0)
    if isinstance(bind_port, bool) or not isinstance(bind_port, int) or not 0 <= bind_port <= 65535:
        raise GatewayError("bad_config")
    has_cert = "tls_cert" in data
    has_key = "tls_key" in data
    if has_cert != has_key:
        raise GatewayError("bad_config")
    tls_cert = _tls_file(data["tls_cert"]) if has_cert else None
    tls_key = _tls_file(data["tls_key"]) if has_key else None
    lease_seconds = None
    if "lease_seconds" in data:
        lease_seconds = _config_number(data["lease_seconds"], 0, 300, low_inclusive=False)
    margin = None
    if "margin" in data:
        margin = _config_number(data["margin"], 0, 86400, low_inclusive=True)
    auto_failover = data.get("auto_failover", False)
    if not isinstance(auto_failover, bool):
        raise GatewayError("bad_config")
    self_recovery = None
    if "self_recovery" in data:
        try:
            self_recovery = _self_recovery(data["self_recovery"])
        except GatewayError:
            raise GatewayError("bad_config") from None
    phone_front = None
    if "phone_front" in data:
        phone_front = _phone_front(data["phone_front"])
    return GatewayConfig(
        state_path=state_path, fleet_id=_as_ident(data["fleet_id"]),
        authority_id=_as_ident(data["authority_id"]), owner_public_keys=tuple(public_keys),
        bind_host=bind_host, bind_port=bind_port, tls_cert=tls_cert, tls_key=tls_key,
        lease_seconds=lease_seconds, margin=margin, auto_failover=auto_failover,
        self_recovery=self_recovery, phone_front=phone_front)


def _require_config(config):
    if not isinstance(config, GatewayConfig):
        raise GatewayError("bad_config")


def _present_file(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise GatewayError("bad_config")
    file = Path(path)
    _reject_live(path)
    if file.is_symlink():
        raise GatewayError("conflict")
    if not file.is_file():
        raise GatewayError("not_found")
    return file


def _absent_file(path):
    """A path that must not exist. Symlinks are refused and not followed."""
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4096:
        raise GatewayError("bad_config")
    file = Path(path)
    _reject_live(path)
    parent = file.parent
    if str(parent) not in ("", ".") and not parent.is_dir():
        raise GatewayError("not_found")
    if os.path.lexists(file):
        raise GatewayError("conflict")
    return file


def _open_ro(path):
    """Read-only SQLite connection. query_only is on, so this cannot write."""
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, isolation_level=None)
    except (OSError, sqlite3.Error, ValueError):
        raise GatewayError("bad_state") from None
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        _integrity(db)
    except GatewayError:
        db.close()
        raise
    except sqlite3.Error:
        db.close()
        raise GatewayError("bad_state") from None
    return db


def _integrity(db):
    row = db.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise GatewayError("bad_state")


def _snapshot_shape(db):
    """Refuse a snapshot that is not this authority schema or that grew a table."""
    try:
        schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if schema is None or schema[0] != str(SCHEMA_VERSION):
            raise GatewayError("bad_state")
        objects = db.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        rows = db.execute("SELECT fleet_id, authority_id FROM authority").fetchall()
    except GatewayError:
        raise
    except sqlite3.Error:
        raise GatewayError("bad_state") from None
    for obj in objects:
        if obj["type"] == "table" and obj["name"] in _ALLOWED_SNAPSHOT_TABLES:
            continue
        if obj["type"] == "index" and obj["tbl_name"] in _ALLOWED_SNAPSHOT_TABLES:
            continue
        raise GatewayError("bad_state")
    if len(rows) != 1:
        raise GatewayError("bad_state")
    return rows[0]["fleet_id"], rows[0]["authority_id"]


def _pin_snapshot(db, config):
    fleet_id, authority_id = _snapshot_shape(db)
    if fleet_id != config.fleet_id or authority_id != config.authority_id:
        raise GatewayError("authority_mismatch")
    return fleet_id, authority_id


def _assert_wiped(db, fleet_id, new_authority_id):
    row = db.execute(
        "SELECT authority_id, mode, holder, lease_id, lease_ready, transition_id, max_validity"
        " FROM authority WHERE fleet_id=?", (fleet_id,)).fetchone()
    if (row is None or row["authority_id"] != new_authority_id or row["mode"] != "LEGACY_ACTIVE"
            or row["holder"] is not None or row["lease_id"] is not None or row["lease_ready"]
            or row["transition_id"] is not None or row["max_validity"] != 0):
        raise GatewayError("bad_state")
    for table in ("admissions", "transitions", "participants", "gateway_directory", "gateway_nonces"):
        if db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] != 0:
            raise GatewayError("bad_state")


def _wipe_destination(db, fleet_id, new_authority_id):
    """Drop grants and membership. The new id is the only authority identity left."""
    _ensure_gateway_schema(db)
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        for table in ("admissions", "transitions", "participants", "gateway_directory", "gateway_nonces"):
            db.execute("DELETE FROM " + table)
        changed = db.execute(
            "UPDATE authority SET authority_id=?, mode='LEGACY_ACTIVE', mode_version=1,"
            " incarnation=1, epoch=0, policy_version=1, holder=NULL, lease_id=NULL,"
            " lease_ready=0, last_holder=NULL, preferred=NULL, transition_id=NULL,"
            " max_validity=0, updated_at=? WHERE fleet_id=?",
            (new_authority_id, now, fleet_id)).rowcount
        if changed == 0:
            raise GatewayError("bad_state")
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    _assert_wiped(db, fleet_id, new_authority_id)


def _backup_image(src):
    """Copy `src` in memory and return a plain SQLite image. Membership stays.

    SQLite is not given the destination path. A build without
    `serialize`/`deserialize`, or an image that stays WAL, is `unavailable`.
    This does not wipe grants or directory rows.
    """
    if not _sqlite_image_io():
        raise GatewayError("unavailable")
    try:
        mem = sqlite3.connect(":memory:", isolation_level=None)
    except sqlite3.Error:
        raise GatewayError("unavailable") from None
    mem.row_factory = sqlite3.Row
    try:
        src.backup(mem)
        _non_wal(mem)
        _integrity(mem)
        return _rollback_header(mem.serialize())
    except GatewayError:
        raise
    except sqlite3.Error:
        raise GatewayError("bad_state") from None
    finally:
        mem.close()


def _verify_backup_image(blob, fleet_id, authority_id):
    """Re-read a backup image in memory. The source authority id must still match."""
    if not _plain_sqlite_image(blob) or not _sqlite_image_io():
        raise GatewayError("bad_state")
    try:
        check = sqlite3.connect(":memory:", isolation_level=None)
    except sqlite3.Error:
        raise GatewayError("unavailable") from None
    check.row_factory = sqlite3.Row
    try:
        check.deserialize(bytes(blob))
        check.execute("PRAGMA query_only=ON")
        _integrity(check)
        seen_fleet, seen_auth = _snapshot_shape(check)
    except GatewayError:
        raise
    except sqlite3.Error:
        raise GatewayError("bad_state") from None
    finally:
        check.close()
    if seen_fleet != fleet_id or seen_auth != authority_id:
        raise GatewayError("authority_mismatch")
    return seen_fleet, seen_auth


def backup_gateway(config, output):
    """Copy one stopped authority database to a new file.

    Does not construct `AuthorityStore`, so the source incarnation does not
    move. Acquires the same exclusive lock as the store (`_writer_lock` on
    the database path, which locks `<realpath>.writer`). A held lock is
    `writer_busy` and creates no output.

    `output` must be absent, including a symlink. A platform without directory
    fds or SQLite serialize/deserialize is `unavailable` and creates nothing.

    Pages are copied in a private memory database. SQLite never opens
    `output`. The image is a plain rollback-journal file and is written
    through an `O_EXCL|O_NOFOLLOW` fd on the parent directory. A name that is
    no longer that inode, including a symlink planted in the write window, is
    `conflict`. Failure unlinks only the inode this call still holds. A
    replacement symlink is left in place and is not followed.

    The source is opened read-only after the lock is held. Its fleet id and
    authority id must match the config. An extra table, view, or trigger is
    `bad_state` and is not copied. No personal body is read or written.
    """
    _require_config(config)
    if not _posix_anchored_io() or not _sqlite_image_io():
        raise GatewayError("unavailable")
    source = _present_file(config.state_path)
    dest = _absent_file(output)
    if source.resolve() == Path(output).resolve():
        raise GatewayError("conflict")
    if dest.name in ("", ".", "..") or "/" in dest.name or "\\" in dest.name:
        raise GatewayError("bad_config")
    try:
        # `_writer_lock` appends `.writer` to the database path. Passing the
        # lock file here would lock a different inode from `AuthorityStore`.
        lock = _writer_lock(os.fspath(source))
    except AuthorityError as exc:
        raise GatewayError(exc.code if _safe_code(exc.code) else "writer_busy") from None
    parent_fd = dest_fd = None
    created = False
    fleet_id = authority_id = None
    try:
        src = _open_ro(source)
        try:
            fleet_id, authority_id = _pin_snapshot(src, config)
            image = _backup_image(src)
        finally:
            src.close()
        if os.path.lexists(dest):
            raise GatewayError("conflict")
        parent_fd = _open_parent_dir(dest.parent)
        if not _parent_anchored(parent_fd, dest.parent) or not _entry_absent(parent_fd, dest.name):
            raise GatewayError("conflict")
        dest_fd = _create_at(parent_fd, dest.name)
        created = True
        _write_fd(dest_fd, image)
        readback = _read_fd(dest_fd)
        if readback != image:
            raise GatewayError("bad_state")
        if not _parent_anchored(parent_fd, dest.parent) or not _entry_is_fd(
                parent_fd, dest.name, dest_fd):
            raise GatewayError("conflict")
        _verify_backup_image(readback, fleet_id, authority_id)
        if not _entry_is_fd(parent_fd, dest.name, dest_fd):
            raise GatewayError("conflict")
    except Exception:
        if created:
            _unlink_if_ours(parent_fd, dest.name, dest_fd)
        raise
    finally:
        if dest_fd is not None:
            os.close(dest_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        lock.close()
    return {"ok": True, "output": os.fspath(dest), "state_path": os.fspath(source),
            "fleet_id": fleet_id, "authority_id": authority_id}


def _refuse_destination(path):
    """The new database and the siblings SQLite or the writer lock would reuse."""
    dest = _absent_file(path)
    for suffix in ("-wal", "-shm", "-journal", ".writer"):
        sibling = Path(os.fspath(dest) + suffix)
        if os.path.lexists(sibling):
            raise GatewayError("conflict")
    return dest


def _dirfd_names(collection):
    """Function names in an `os.supports_*` set.

    Those sets store the interpreter's original function objects. Comparing
    identity would treat a wrapper around `os.link` as a platform that cannot
    publish with `linkat`, and restore would fail closed before the publish
    check. The name is the capability. A real platform that lacks `link`
    still has no such name.
    """
    return {getattr(fn, "__name__", "") for fn in collection}


def _posix_anchored_io():
    """True when restore can pin the parent directory and publish with `linkat`.

    A platform without directory fds fails closed. Restore does not fall
    back to path-string `os.link`: swapping the parent for a symlink in that
    window would receive the new authority file. `follow_symlinks=False` is
    required for the same reason; without it, restore refuses to publish.
    """
    if sys.platform == "win32":
        return False
    if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_EXCL")):
        return False
    names = _dirfd_names(getattr(os, "supports_dir_fd", ()))
    if not {"open", "stat", "unlink", "link"} <= names:
        return False
    follow = _dirfd_names(getattr(os, "supports_follow_symlinks", ()))
    return "link" in follow


def _dir_flags():
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return flags


def _open_parent_dir(parent):
    """Open the real directory. A symlink parent is `conflict` and is not followed."""
    try:
        return os.open(os.fspath(parent), _dir_flags())
    except OSError:
        raise GatewayError("conflict") from None


def _parent_anchored(dir_fd, parent):
    """The path still names the directory fd, not a symlink planted later."""
    try:
        fd = os.open(os.fspath(parent), _dir_flags())
    except OSError:
        return False
    try:
        here, opened = os.fstat(fd), os.fstat(dir_fd)
    finally:
        os.close(fd)
    return (here.st_dev, here.st_ino) == (opened.st_dev, opened.st_ino)


def _entry_absent(dir_fd, name):
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        raise GatewayError("conflict") from None
    return False


def _require_unpublished(dir_fd, name):
    """Destination and SQLite sidecars are absent. The writer lock is checked apart."""
    for entry in (name, name + "-wal", name + "-shm", name + "-journal"):
        if not _entry_absent(dir_fd, entry):
            raise GatewayError("conflict")


def _create_at(dir_fd, name):
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError:
        raise GatewayError("conflict") from None


def _flock_exclusive(fd):
    """Same nonblocking exclusive flock as `authority._writer_lock` on POSIX.

    The caller created `fd` with `O_EXCL` and holds it through publication.
    There is no close-and-reopen gap. A preexisting or replaced lock file is
    not adopted, because this is never asked to open a path.
    """
    try:
        import fcntl
    except ImportError:
        raise GatewayError("unavailable") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise GatewayError("writer_busy") from None


def _entry_is_fd(dir_fd, name, fd):
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise GatewayError("conflict") from None
    cur = os.fstat(fd)
    return stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == (cur.st_dev, cur.st_ino)


def _unlink_if_ours(dir_fd, name, fd):
    """Remove `name` only while it is still the inode `fd` has open.

    A replacement directory entry is left alone. Callers never pass the
    published destination: a failed link means that name is not ours.
    """
    if dir_fd is None or fd is None or not name:
        return
    try:
        if not _entry_is_fd(dir_fd, name, fd):
            return
        os.unlink(name, dir_fd=dir_fd)
    except (GatewayError, OSError):
        return


def _unlink_regular(dir_fd, name):
    """Unlink a regular file at `name`. A symlink is left in place."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return
    if not stat.S_ISREG(st.st_mode):
        return
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        return


def _sqlite_image_io():
    """Serialize and deserialize exist, so SQLite need not open the destination."""
    return hasattr(sqlite3.Connection, "serialize") and hasattr(sqlite3.Connection, "deserialize")


def _plain_sqlite_image(blob):
    """A rollback-journal SQLite file. Write version 2 is WAL and needs a sidecar."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 100:
        return False
    if bytes(blob[:16]) != b"SQLite format 3\x00":
        return False
    return blob[18] == 1 and blob[19] == 1


def _rollback_header(blob):
    """Return `blob` with a rollback-journal header.

    The backup API copies a consistent page image. When the source is WAL,
    bytes 18 and 19 of that image stay at write/read version 2 even though
    the pages already contain the checkpointed rows. A version-2 file is not
    published: opening it looks for a `-wal` sidecar of the destination name.
    Those two bytes are the WAL marker. The caller deserializes the result
    and runs `integrity_check` before the publishing link.
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 100:
        raise GatewayError("unavailable")
    if bytes(blob[:16]) != b"SQLite format 3\x00":
        raise GatewayError("unavailable")
    raw = bytearray(blob)
    if raw[18] == 2 and raw[19] == 2:
        raw[18] = 1
        raw[19] = 1
    if not _plain_sqlite_image(raw):
        raise GatewayError("unavailable")
    return bytes(raw)


def _non_wal(db):
    """Leave a journal mode whose serialized image does not point at `-wal`."""
    for mode in ("DELETE", "OFF", "TRUNCATE", "MEMORY"):
        try:
            row = db.execute("PRAGMA journal_mode=%s" % mode).fetchone()
        except sqlite3.Error:
            continue
        if row is not None and str(row[0]).lower() != "wal":
            return
    raise GatewayError("unavailable")


def _wiped_image(src, fleet_id, new_id):
    """Copy and wipe `src` in memory. Return a plain SQLite image.

    SQLite is not given the destination path. Replacing that parent with a
    symlink cannot redirect this copy, and a WAL sidecar of the destination
    name is never created. A build without `serialize`/`deserialize`, or an
    image that stays WAL, is `unavailable` rather than a path-string fallback.
    """
    if not _sqlite_image_io():
        raise GatewayError("unavailable")
    try:
        mem = sqlite3.connect(":memory:", isolation_level=None)
    except sqlite3.Error:
        raise GatewayError("unavailable") from None
    mem.row_factory = sqlite3.Row
    try:
        src.backup(mem)
        _non_wal(mem)
        _wipe_destination(mem, fleet_id, new_id)
        _non_wal(mem)
        _integrity(mem)
        blob = _rollback_header(mem.serialize())
    except GatewayError:
        raise
    except sqlite3.Error:
        raise GatewayError("bad_state") from None
    finally:
        mem.close()
    return blob


def _verify_image(blob, fleet_id, new_id):
    """Re-read a plain image in a new memory database. No destination path."""
    if not _plain_sqlite_image(blob) or not _sqlite_image_io():
        raise GatewayError("bad_state")
    try:
        check = sqlite3.connect(":memory:", isolation_level=None)
    except sqlite3.Error:
        raise GatewayError("unavailable") from None
    check.row_factory = sqlite3.Row
    try:
        check.deserialize(bytes(blob))
        check.execute("PRAGMA query_only=ON")
        _integrity(check)
        _assert_wiped(check, fleet_id, new_id)
        return _snapshot_shape(check)
    except GatewayError:
        raise
    except sqlite3.Error:
        raise GatewayError("bad_state") from None
    finally:
        check.close()


def _write_fd(fd, blob):
    """Replace the inode's bytes. The directory entry is not named here."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(blob)
        offset = 0
        while offset < len(view):
            wrote = os.write(fd, view[offset:])
            if wrote <= 0:
                raise GatewayError("unavailable")
            offset += wrote
        os.ftruncate(fd, len(blob))
        os.fsync(fd)
    except GatewayError:
        raise
    except OSError:
        raise GatewayError("unavailable") from None


def _read_fd(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
    except OSError:
        raise GatewayError("unavailable") from None
    return b"".join(chunks)


def _publish_ready(parent_fd, dest, lock_name, lock_fd, partial_name, partial_fd):
    """The opened directory, lock inode, temp inode, and destination are unchanged.

    A parent path that no longer names the directory fd, a destination or
    sidecar that appeared, or a lock/temp name whose inode is no longer the
    one this restore holds, is `conflict`. Nothing is unlinked here.
    """
    if not _parent_anchored(parent_fd, dest.parent):
        raise GatewayError("conflict")
    _require_unpublished(parent_fd, dest.name)
    if not _entry_is_fd(parent_fd, lock_name, lock_fd):
        raise GatewayError("conflict")
    if not _entry_is_fd(parent_fd, partial_name, partial_fd):
        raise GatewayError("conflict")


def restore_gateway(config, snapshot, new_authority_id):
    """Write a new authority file that cannot resume the snapshot's grants.

    `config.authority_id` is the source pin. It must equal the snapshot's
    authority id, and `config.fleet_id` must equal the snapshot's fleet id.
    The check opens the snapshot read-only. This does not take the source
    writer lock and does not construct `AuthorityStore` on the snapshot, so
    the snapshot's incarnation does not move.

    `new_authority_id` must be a different identifier. The destination is
    `config.state_path`. That path, and its `-wal`, `-shm`, `-journal`, and
    `.writer` siblings, must not exist. A sibling is enough to revive pages
    or to collide with a live writer, so it is `conflict`. Symlinks count as
    existing and are not followed.

    The parent directory is opened `O_DIRECTORY|O_NOFOLLOW`. Creates, stats,
    unlinks, and the publishing link use that directory fd. A platform
    without directory fds raises `unavailable` and does not publish by path.
    If the parent path later becomes a symlink, the fd still names the
    original directory and publication is refused.

    This restore creates `<state_path>.writer` with `O_EXCL` and flocks it
    the way `AuthorityStore` does (`LOCK_EX|LOCK_NB` on that fd). The same
    fd is held, without closing it, until the hard link returns. The lock
    file this call just created is the reservation, not a foreign writer.
    A `.writer` that already existed, or a different inode placed at that
    name, is `conflict` and is not removed. The flock is not dropped and
    taken again before publication.

    Pages are copied in a private memory database. SQLite never opens the
    destination path, so replacing the parent directory with a symlink cannot
    redirect the copy. The wiped image is a plain rollback-journal file, not
    WAL, and those bytes are written to a temporary inode created through the
    directory fd. Admissions, transitions, participants, the gateway directory,
    and device-nonce digests are deleted in that memory copy. Lease columns
    are cleared (`holder`, `lease_id`, `lease_ready`, `transition_id`,
    `max_validity`). `mode` is set to `LEGACY_ACTIVE`, `policy_version` to 1,
    and incarnation/epoch to the fresh-file pair (1, 0). `authority_id`
    becomes `new_authority_id`. Immediately before that write, and again
    immediately before the link, the parent path must still name the opened
    directory and the destination and its sidecars must be absent. The link
    fails if the name exists. This function never unlinks `state_path` or a
    sibling it does not still have open. A failure removes the temp inode
    and, when this restore still owns the lock inode, that lock name. A build
    that cannot serialize a plain image raises `unavailable` and does not
    fall back to opening the destination path.

    DESIGN.md section 16A.6 accepted that a stale backup might restore a
    device revoked after the backup. This command does not. Membership is
    empty, so a rolled-back admission is not returned as `already`, and a
    revoked installation is not enrolled. David re-enrolls the devices he
    still wants and runs cutover again.

    The config file is not modified. Serving `state_path` with this same
    config quarantines, because the file's authority id is no longer the
    pin. David writes a new config whose `authority_id` is the new id and
    whose `state_path` is this destination. The first `AuthorityStore` open
    then increments incarnation and epoch. `max_validity` is 0, so that open
    does not treat an old lease as still running. Mode stays `LEGACY_ACTIVE`.

    `auto_failover` is not a promotion switch here. This function never
    promotes a device.
    """
    _require_config(config)
    new_id = _as_ident(new_authority_id)
    if new_id == config.authority_id:
        raise GatewayError("conflict")
    if not _posix_anchored_io() or not _sqlite_image_io():
        raise GatewayError("unavailable")
    source = _present_file(snapshot)
    dest = _refuse_destination(config.state_path)
    if dest.name in ("", ".", "..") or "/" in dest.name:
        raise GatewayError("bad_config")
    if source.resolve() == dest.resolve():
        raise GatewayError("conflict")
    parent_fd = _open_parent_dir(dest.parent)
    lock_fd = partial_fd = None
    partial_name = None
    lock_name = dest.name + ".writer"
    release_lock = False
    published = False
    try:
        _require_unpublished(parent_fd, dest.name)
        if not _entry_absent(parent_fd, lock_name):
            raise GatewayError("conflict")
        if not _parent_anchored(parent_fd, dest.parent):
            raise GatewayError("conflict")
        src = _open_ro(source)
        try:
            fleet_id, authority_id = _pin_snapshot(src, config)
        finally:
            src.close()
        lock_fd = _create_at(parent_fd, lock_name)
        try:
            _flock_exclusive(lock_fd)
        except GatewayError:
            # Do not unlink. A failed flock means this name is not our reservation.
            os.close(lock_fd)
            lock_fd = None
            raise
        release_lock = True
        if not _entry_is_fd(parent_fd, lock_name, lock_fd):
            raise GatewayError("conflict")
        partial_name = ".%s.partial-%s" % (dest.name, secrets.token_hex(8))
        _reject_live(dest.parent / partial_name)
        if not _entry_absent(parent_fd, partial_name):
            raise GatewayError("conflict")
        partial_fd = _create_at(parent_fd, partial_name)
        src = _open_ro(source)
        try:
            image = _wiped_image(src, fleet_id, new_id)
        finally:
            src.close()
        _publish_ready(parent_fd, dest, lock_name, lock_fd, partial_name, partial_fd)
        _write_fd(partial_fd, image)
        readback = _read_fd(partial_fd)
        if readback != image:
            raise GatewayError("bad_state")
        seen_fleet, seen_auth = _verify_image(readback, fleet_id, new_id)
        if seen_fleet != fleet_id or seen_auth != new_id:
            raise GatewayError("bad_state")
        # The parent can move while the bytes are written. Recheck on the
        # directory fd immediately before the publishing link.
        _publish_ready(parent_fd, dest, lock_name, lock_fd, partial_name, partial_fd)
        try:
            os.link(
                partial_name, dest.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise GatewayError("conflict") from None
        published = True
        _unlink_if_ours(parent_fd, partial_name, partial_fd)
        _unlink_if_ours(parent_fd, lock_name, lock_fd)
        release_lock = False
    except Exception:
        # `dest` is published only by linkat. Never unlink that name: a failed
        # link means the directory entry belongs to someone else. The same
        # rule applies to a writer inode this restore does not still hold.
        if not published:
            if partial_fd is not None and partial_name:
                for suffix in ("-wal", "-shm", "-journal"):
                    _unlink_regular(parent_fd, partial_name + suffix)
                _unlink_if_ours(parent_fd, partial_name, partial_fd)
            if release_lock and lock_fd is not None:
                _unlink_if_ours(parent_fd, lock_name, lock_fd)
        raise
    finally:
        if partial_fd is not None:
            os.close(partial_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(parent_fd)
    return {
        "ok": True, "state_path": os.fspath(dest), "fleet_id": fleet_id,
        "source_authority_id": authority_id, "authority_id": new_id,
        "mode": "LEGACY_ACTIVE", "participants": 0, "admissions": 0,
        "transitions": 0, "directory": 0, "serve_requires_new_config": True,
    }


def serve_gateway(config):
    """Open the store and listen. The caller prints readiness and must `close`.

    `FailoverPolicy` is built first. `auto_failover=True` raises
    `auto_failover_disabled` and does not create or open `state_path`.
    No thread calls `on_unreachable` or `on_lease`.
    """
    _require_config(config)
    policy = FailoverPolicy(auto_failover=config.auto_failover)
    kwargs = {}
    if config.lease_seconds is not None:
        kwargs["lease_seconds"] = config.lease_seconds
    if config.margin is not None:
        kwargs["margin"] = config.margin
    gateway = ContinuityGateway.open(
        config.state_path, config.fleet_id, config.authority_id, config.owner_public_keys,
        self_recovery=config.self_recovery, **kwargs)
    gateway.policy = policy
    try:
        origin = gateway.serve(
            config.bind_host, config.bind_port, tls_cert=config.tls_cert, tls_key=config.tls_key)
    except Exception:
        gateway.close()
        raise
    if config.phone_front:
        from jarvis.continuity.phone_front import PhoneFront
        try:
            front = PhoneFront(gateway.store.status, config.phone_front["upstreams"],
                               bind_port=config.phone_front["bind_port"])
            front.start()
        except Exception:
            gateway.close()
            raise
        gateway.phone_front = front
    return gateway, origin


def _emit(obj, stream):
    print(json.dumps(obj, sort_keys=True, separators=(",", ":")), file=stream, flush=True)


class _Parser(argparse.ArgumentParser):
    """Usage mistakes are `bad_config`, not an argparse traceback."""

    def error(self, message):
        raise GatewayError("bad_config")


def _parser():
    parser = _Parser(prog="python -m jarvis.continuity.gateway")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--config", required=True)
    backup.add_argument("--output", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--config", required=True)
    restore.add_argument("--input", required=True)
    restore.add_argument("--new-authority-id", required=True)
    return parser


def _serve_until_signal(config):
    gateway, origin = serve_gateway(config)
    stop = threading.Event()

    def _handle(signum, _frame):
        stop.set()

    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _handle)
        _emit({"authority_id": config.authority_id, "origin": origin, "pid": os.getpid()}, sys.stdout)
        while not stop.is_set():
            thread = gateway._thread
            if thread is None or not thread.is_alive():
                break
            thread.join(timeout=0.5)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        gateway.close()
    return 0


def _dispatch(argv):
    args = _parser().parse_args(argv)
    if args.command is None:
        raise GatewayError("bad_config")
    config = load_gateway_config(args.config)
    if args.command == "serve":
        return _serve_until_signal(config)
    if args.command == "backup":
        _emit(backup_gateway(config, args.output), sys.stdout)
        return 0
    if args.command == "restore":
        _emit(restore_gateway(config, args.input, args.new_authority_id), sys.stdout)
        return 0
    raise GatewayError("bad_config")


def main(argv=None):
    """Run `serve`, `backup`, or `restore`. Return 0 on success and 2 on refusal.

    stdout is one JSON object. `serve` prints `authority_id`, `origin`, and
    `pid` after the socket is listening, then waits for SIGINT or SIGTERM
    and closes the listener and the store. stderr on failure is
    `{"error":"<code>"}` with no path and no traceback.

    Codes: `bad_config` (shape, unknown key, tls pair, `jarvis.config.json`),
    `auto_failover_disabled` (serve with the flag true), `refuses_live_state`,
    `refused_bind`, `writer_busy`, `not_found`, `conflict` (existing output,
    symlink, or new authority id equal to the source pin),
    `authority_mismatch`, `bad_state`, `unavailable`.
    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _dispatch(list(argv))
    except (KeyboardInterrupt, SystemExit):
        raise
    except GatewayError as exc:
        _emit({"error": exc.code}, sys.stderr)
        return 2
    except AuthorityError as exc:
        code = exc.code if _safe_code(getattr(exc, "code", None)) else "unavailable"
        _emit({"error": code}, sys.stderr)
        return 2
    except Exception:
        _emit({"error": "unavailable"}, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

