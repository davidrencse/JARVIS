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
"""

import base64
import hashlib
import ipaddress
import json
import math
import re
import secrets
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
    TICKET_FIELDS as _TICKET_FIELDS,
    AuthorityError,
    AuthorityStore,
)

DEVICE_NS = "jarvis-continuity-device-v1"
OWNER_NS = "jarvis-continuity-owner-v1"
TICKET_NS = "jarvis-continuity-ticket-v1"
_NAMESPACES = (DEVICE_NS, OWNER_NS, TICKET_NS)

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
    "forbidden": 403, "unknown_participant": 403,
    "not_found": 404,
    "ticket_expired": 410,
    "stale": 409, "busy": 409, "key_reused": 409, "conflict": 409, "not_quiet": 409,
    "lease_live": 409, "ahead": 409, "bad_edge": 409, "unacked": 409, "expired": 409,
    "draining": 409, "mode": 409, "ineligible": 409, "main_alive": 409, "no_main": 409,
    "noop": 409, "not_ready": 409, "bad_state": 409, "no_admission": 409,
    "target_unfenced": 409,
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
}
_OWNER_PATH = {
    "transition": "/v1/authority/transitions",
    "step": "/v1/authority/transitions/step",
    "enroll": "/v1/authority/participants",
    "mode": "/v1/authority/mode",
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
    """Relay targets are loopback HTTP only. Non-loopback and https forwards are refused."""
    pinned = pin_origin(origin)
    parts = urlsplit(pinned)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"}:
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return "jarvis-continuity"

    def do_POST(self):
        self.server.gateway._http(self)

    def do_GET(self):
        self.server.gateway._reply(self, 405, {"error": "bad_request"}, None, "bad_request")

    do_PUT = do_DELETE = do_PATCH = do_GET

    def log_message(self, fmt, *args):
        return


class ContinuityGateway:
    """One `AuthorityStore`, one device directory, and an optional in-process HTTP server.

    `open` does not listen. Call `serve` explicitly. `close` stops the listener and,
    when this object created the store, closes it.
    """

    def __init__(self, store, owner_public_keys, *, wall=time.time, owns_store=False):
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
        self._closed = False

    @classmethod
    def open(cls, path, fleet_id, authority_id, owner_public_keys, *, monotonic=time.monotonic,
             wall=time.time, lease_seconds=None, margin=None):
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
            return cls(store, owner_public_keys, wall=wall, owns_store=True)
        except Exception:
            store.close()
            raise

    def close(self):
        self.shutdown()
        if self._owns_store and not self._closed:
            self._closed = True
            self.store.close()

    def serve(self, host="127.0.0.1", port=0, *, confirm_bind=False):
        """Bind an in-process HTTP server. Refuses wildcard addresses. Does not enable TLS."""
        self._check_bind(host, confirm_bind)
        if self._httpd is not None:
            raise GatewayError("bad_request")
        try:
            httpd = ThreadingHTTPServer((host, port), _Handler)
        except OSError:
            raise GatewayError("refused_bind") from None
        httpd.daemon_threads = True
        httpd.gateway = self
        httpd.timeout = 2
        self._httpd = httpd
        bound_host, bound_port = httpd.server_address[:2]
        shown = f"[{bound_host}]" if ":" in bound_host else bound_host
        self._listening_origin = f"http://{shown}:{bound_port}"
        self._thread = threading.Thread(target=httpd.serve_forever, name="continuity-gateway", daemon=False)
        self._thread.start()
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
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._listening_origin = None

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
        with self._lock:
            self._fresh(self._nonces, (device_id, nonce), issued_at)
            if not verify_message(DEVICE_NS, message, signature, record.public_key):
                raise GatewayError("unauthorized")
            self._nonces[(device_id, nonce)] = issued_at + SKEW_SECONDS
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
        try:
            return fn()
        except AuthorityError as exc:
            raise GatewayError(exc.code, wait=exc.wait) from None

    # --- facade ------------------------------------------------------------------------

    def status(self, principal, payload):
        """AuthorityStatus for an enrolled device. Empty payload."""
        self._accept_device(principal, "status", "status", _exact(payload, ()))
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
        if main is None or not verify_message(TICKET_NS, _message(TICKET_NS, signed), signature, main.public_key):
            raise GatewayError("unauthorized")
        record = self._store(lambda: self.store.consume(
            admission_id=fields["admission_id"], target=fields["target"], operation_id=fields["operation_id"],
            attempt_id=fields["attempt_id"], digest=fields["digest"], main=fields["main"],
            incarnation=fields["incarnation"], epoch=fields["epoch"], issued_at=fields["issued_at"],
            admit_until=fields["admit_until"], issuer_incarnation=fields["issuer_incarnation"]))
        return _project_admission(record)

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
        except GatewayError:
            with self._lock:
                self._relays[operation_id] = {"identity": identity, "state": "error"}
            raise GatewayError("no_relay", operation_id=operation_id) from None
        if (set(upstream) != {"operation_id", "accepted"} or upstream["operation_id"] != operation_id
                or not isinstance(upstream["accepted"], bool)):
            with self._lock:
                self._relays[operation_id] = {"identity": identity, "state": "error"}
            raise GatewayError("no_relay", operation_id=operation_id)
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
        small result, and a lost or odd answer leaves the operation unresolved
        (`no_relay`). A read is one attempt and is not remembered. `effect_authorized`
        is always false: MAIN still needs its own admission ticket.
        """
        self._accept_device(principal, "client", "relay", _exact(
            payload, ("operation_id", "target", "attempt_id", "digest", "capability", "kind")))
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
        try:
            upstream = _post_json(origin + RELAY_PATH, body, limit)
            result = self._transient_result(effect, operation_id, upstream)
        except GatewayError:
            if effect:
                with self._lock:
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

        `client` devices are directory entries only. They are not authority participants,
        so a phone cannot satisfy or block a fence. The directory is RAM-only.
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
        if kind != "client":
            self._store(lambda: self.store.register_participant(device_id, installation_id, kind, True))
        with self._lock:
            self._devices[device_id] = _Record(
                device_id, installation_id, kind, public_key, capabilities, relay_origin)
        return {"device_id": device_id, "installation_id": installation_id, "kind": kind,
                "capabilities": sorted(capabilities)}

    def set_mode(self, owner_proof, payload):
        """One cutover edge. The store still refuses jumps to V2_ACTIVE."""
        self._accept_owner(owner_proof, "mode", _exact(payload, ("mode", "expected_mode_version")))
        mode = payload["mode"]
        if not isinstance(mode, str) or mode not in {
                "PREPARING_V2", "QUIESCED", "V2_ONLY_NO_MAIN", "LEGACY_ACTIVE", "ROLLBACK_QUIESCED"}:
            raise GatewayError("bad_request")
        version = self._store(lambda: self.store.set_mode(mode, _nnint(payload["expected_mode_version"])))
        return {"mode": mode, "mode_version": version}

    def _capabilities(self, kind, values):
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

    # --- HTTP --------------------------------------------------------------------------

    def _http(self, handler):
        code, status, wait, device_id, operation_id = "unavailable", 503, None, None, None
        try:
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
        return self.relay(principal, payload)

    def _dispatch_owner(self, purpose, proof, payload):
        if purpose == "transition":
            return self.transition_request(proof, payload)
        if purpose == "step":
            return self.transition_step(proof, payload)
        if purpose == "enroll":
            return self.enroll(proof, payload)
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


def _post_json(url, data, limit):
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with _opener().open(request, timeout=2) as response:
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

    def set_mode(self, mode, expected_mode_version):
        return self._owner("mode", {"mode": mode, "expected_mode_version": expected_mode_version})

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
