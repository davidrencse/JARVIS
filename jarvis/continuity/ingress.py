"""Stable phone origin for a future gateway. Isolated until an explicit v2 cutover.

This is the mobile ingress in docs/design/mobile-continuity/OPERATIONAL-CONTRACT.md.
It is a client of a gateway object. It does not write the authority database, renew a
lease, consume an admission, or decide who is MAIN. `cluster.main_device` is never read.

Default is legacy: every pair, relay, and MAIN change raises `legacy_retained` and the
store is not used as a phone origin. `activate()` is the cutover. It requires the
authority mode to already be v2 and the caller to claim the legacy phone server was
fenced. That claim is not checked here (`proved` is false). After activation a mode
change, a stale mode version, a quarantine, or an unreachable authority fails closed.
Nothing falls back to the last MAIN.

Personal prompts, replies, frames, input, cookies, CSRF tokens, and passkey material are
not written to sqlite or the log hook.

The gateway this module calls is `ContinuityGateway` through `ContinuityGatewaySession`:
`status()` with an empty payload, `relay_metadata` with exactly the five metadata fields
`ContinuityGateway.relay` accepts, and `transition_request` with a fresh gateway
`OwnerProof`. A phone prompt, event, frame, or cookie is not an argument to those calls.
`ContinuityGateway.relay` sets `effect_authorized` false and posts only to an enrolled
loopback origin. This module does not treat that post as fenced work, and it does not
open a second socket to deliver the body. Phone routes that need a body fail with
`body_not_relayable` and are not stored. Metadata notices are at most once per operation
id. A lost metadata answer stays `uncertain` and is not sent again.

`phone.py` is not modified. Phone claim helpers are imported lazily inside the
signer and the verifier. `PhoneProtocolAdapter` maps the current HTTP paths, requires
`X-CSRF-Token`, and does not bypass that check. Pairing returns `csrf_token` once.
`/phone/api/main/preview` and `/phone/api/main/confirm` are the manage-MAIN routes.
`accept_gateway_phone` is the PC seam and is not implemented here.
The signed phone envelope the PC verifier imports is `PHONE_ENVELOPE`.

Optional transient relay: when the gateway object also has `relay_transient`, phone reads
(overview, fleet, ask poll, frames) and effects (ask, session start, input, stop) go to the
allowlisted ready MAIN through `ContinuityGateway.forward_transient`, in RAM only. The row for
an effect keeps ids and a digest, never the text. A lost answer stays `uncertain` and is
never sent again. `effect_authorized` is still false: MAIN's executor admits or refuses.
An effect post is accepted or refused only. A later `effect_status` read may carry `target`
and a trace; projection copies steps that are already there and does not invent any.
Without `relay_transient`, the metadata-only rules above apply unchanged.

Every transient envelope is the signed claim in `PHONE_ENVELOPE`. A missing or bad
signer raises `envelope_unsigned` and the body is not relayed. `verify_phone_envelope`
accepts only that signed shape. `production_ingress` is the factory that requires the
concrete WebAuthn verifier, owner bridge, ticket verifier, and signer.

Not operational failover. No gateway host is provisioned by this module.
"""

from ..locking import transaction
import base64
import contextvars
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.continuity.authority import ACTIONS as MAIN_ACTIONS
from jarvis.continuity.authority import PROFILES as MAIN_PROFILES
from jarvis.continuity.authority import TICKET_FIELDS
from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.gateway import CAPABILITIES as RELAY_CAPABILITIES
from jarvis.continuity.gateway import DEVICE_NS
from jarvis.continuity.gateway import OWNER_NS
from jarvis.continuity.gateway import TICKET_NS
from jarvis.continuity.gateway import AuthorityClient
from jarvis.continuity.gateway import GatewayError
from jarvis.continuity.gateway import HTTP_STATUS as GATEWAY_HTTP_STATUS
from jarvis.continuity.gateway import OwnerProof as GatewayOwnerProof
from jarvis.continuity.gateway import _message as gateway_message
from jarvis.continuity.gateway import _public_key as gateway_public_key
from jarvis.continuity.gateway import payload_digest
from jarvis.continuity.gateway import sign_message
from jarvis.continuity.gateway import verify_message

SCHEMA_VERSION = 1
PHONE_COOKIE = "__Host-jarvis_phone"
PHONE_HEADER = "X-JARVIS-Phone"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_AGE = 90 * 24 * 3600
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_TTL = 300
APPROVED_GRACE = 60
BAD_CODES, BAD_WINDOW = 5, 600
GLOBAL_BAD = 50  # wrong codes from every source in that same window
MAX_BODY = 256 * 1024
ASK_MAX = 2000
NAME_MAX = 40
FRAME_MAX = 2_500_000  # remote_view.MAX_FRAME; a larger picture is dropped, not stored
PREVIEW_TTL = 60
SEEN_EVERY = 60
MOBILE_OS = frozenset({"ios", "android"})
V2_SERVE = frozenset({"V2_ONLY_NO_MAIN", "V2_ACTIVE", "HANDOVER"})
EFFECT_KINDS = frozenset({"ask", "session_start", "session_input", "session_stop", "logout_end"})
READ_KINDS = frozenset({"overview", "fleet", "ask_poll", "session_frame", "effect_status"})
# Session methods. `status` takes no arguments: the session already authenticated.
# `relay_metadata` is the five-field ContinuityGateway.relay payload, never a body.
GATEWAY_METHODS = ("status", "relay_metadata", "transition_request")
# Methods this client must not call. Runtime and the executor own them.
GATEWAY_NOT_OURS = ("renew", "release", "acknowledge", "consume", "admission",
                    "transition_readiness", "transition_step", "set_mode", "enroll")
# Keys ContinuityGateway.relay rejects. A phone request that carries one is not forwarded.
PERSONAL_KEYS = frozenset({
    "prompt", "body", "text", "content", "message", "file", "filename", "path",
    "url", "upstream", "host", "authorization", "cookie", "secret", "token",
    "password", "transcript", "frame", "image", "payload_body", "events",
    "reply", "raw", "passkey", "csrf", "csrf_token", "spool",
})
BODY_TEXT = "The gateway relay carries metadata only. This request was not sent."
METADATA_KEYS = frozenset({"attempt_id", "effect_class"})
PAIR_PATHS = ("/phone/pair", "/phone/pair/status")
PHONE_SHELL = (
    "/", "/phone.css", "/phone.js", "/sw.js", "/manifest.webmanifest",
    "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png",
    "/icons/icon.svg", "/fonts/geist.woff2", "/fonts/geist-mono.woff2",
    "/fonts/doto.woff2", "/icons.svg", "/theme.js", "/esc.js", "/meters.js",
)
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OP = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_JOB = re.compile(r"^[0-9a-f]{6,32}$")
_SESSION = re.compile(r"^[0-9a-f]{32}$")
_RECEIPT = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
# Same shape as phone.py's phone-approval refusal. Locked by tests.continuity.test_ingress.
APPROVALS = re.compile(
    r"(?:jarvis[\s,]+)?(?:approve[d]?|reject|deny|allow|ok(?:ay)?|sure|yep|yeah|yup|"
    r"nope|y|n)(?:\s+c?\d+)?", re.I)
_ASK_STRIP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Statuses come from the gateway's own map, so a code cannot mean two things. Each boundary keeps
# only its allowlist of codes; `authority_unavailable` is the ingress's own 503 for a gateway it
# cannot trust.
_AUTH_STATUS = {code: GATEWAY_HTTP_STATUS[code] for code in (
    "stale", "busy", "key_reused", "conflict", "not_quiet", "lease_live", "bad_request",
    "bad_ticket", "ticket_expired", "quarantined")}
_GATEWAY_HTTP = {**{code: GATEWAY_HTTP_STATUS[code] for code in (
    "bad_request", "forbidden", "not_found", "ticket_expired", "stale", "busy", "key_reused",
    "conflict", "not_quiet", "lease_live", "quarantined", "no_relay", "unavailable",
    "writer_busy")}, "authority_unavailable": 503}
LEGACY_TEXT = "The phone link is still the PC's own origin. This gateway is not serving it."

_PEER = contextvars.ContextVar("jarvis_ingress_peer", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phones(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_sha256 TEXT NOT NULL, csrf_sha256 TEXT NOT NULL,
  stable_id TEXT NOT NULL, login TEXT NOT NULL, os TEXT NOT NULL, node TEXT NOT NULL,
  created REAL NOT NULL, last_seen REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
  manage_main INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS codes(
  id TEXT PRIMARY KEY, salt TEXT NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL,
  expires REAL NOT NULL, state TEXT NOT NULL, name TEXT, allow_desktop INTEGER NOT NULL,
  stable_id TEXT, login TEXT, os TEXT, node TEXT, operation_id TEXT);
CREATE TABLE IF NOT EXISTS pair_binds(
  operation_id TEXT PRIMARY KEY, code_id TEXT NOT NULL, state TEXT NOT NULL,
  phone_id TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
  operation_id TEXT PRIMARY KEY, phone_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
  state TEXT NOT NULL, http_status INTEGER, job_id TEXT, session_id TEXT, cursor INTEGER,
  target TEXT, incarnation INTEGER, epoch INTEGER, mode_version INTEGER, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, phone_id TEXT NOT NULL, device TEXT NOT NULL, kind TEXT NOT NULL,
  state TEXT NOT NULL, incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL, main TEXT NOT NULL,
  updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS previews(
  preview_id TEXT PRIMARY KEY, phone_id TEXT NOT NULL, action TEXT NOT NULL, candidate TEXT NOT NULL,
  profile TEXT NOT NULL, incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL,
  policy_version INTEGER NOT NULL, mode_version INTEGER NOT NULL, challenge_sha256 TEXT NOT NULL,
  expires REAL NOT NULL, transition_id TEXT, state TEXT NOT NULL, updated_at REAL NOT NULL,
  authorized INTEGER NOT NULL DEFAULT 0, operation_id TEXT);
"""


class IngressError(Exception):
    """A refusal. `message` is safe to show; it never echoes a prompt, cookie, or path."""

    def __init__(self, code, status, message, wait=None, pair=None):
        super().__init__(message)
        self.code, self.status, self.message, self.pair = code, int(status), message, pair
        self.wait = wait

    def body(self):
        out = {"error": self.message, "code": self.code}
        if self.pair is not None:
            out["pair"] = bool(self.pair)
        return out


class RelayUncertain(Exception):
    """The gateway does not know whether the MAIN accepted the effect.

    `sent` False means the gateway guarantees the request never left. Ingress still
    does not retry an effect by itself. A later reconcile is a read.
    """

    def __init__(self, sent=True):
        super().__init__("uncertain")
        self.sent = bool(sent)


class PinnedTarget:
    """Where a ready MAIN may be dialed. The IP is pinned; the name is not resolved here."""

    def __init__(self, device_id, connect_ip, port, scheme, sni):
        self.device_id, self.connect_ip = device_id, connect_ip
        self.port, self.scheme, self.sni = int(port), scheme, sni

    def as_dict(self):
        return {"connect_ip": self.connect_ip, "port": self.port, "scheme": self.scheme, "sni": self.sni}


class PeerProof:
    """Identity produced by `identify`. A dict from the client is not a proof."""

    def __init__(self, ingress, who):
        self.ingress, self.who = ingress, dict(who)

    def __repr__(self):
        return "PeerProof(redacted)"


class OwnerProof:
    def __init__(self, ingress, installation_id, device_id):
        self.ingress, self.installation_id, self.device_id = ingress, installation_id, device_id

    def __repr__(self):
        return "OwnerProof(redacted)"


class Principal:
    def __init__(self, ingress, token, phone_id, name, stable_id, login):
        self._ingress, self._token = ingress, token
        self.phone_id, self.name = phone_id, name
        self.role = "phone"
        self.stable_id, self.login = stable_id, login

    def __repr__(self):
        return f"Principal(phone_id={self.phone_id!r}, role='phone')"


class Session:
    """Returned once at pair time. The cookie and CSRF token are not stored in the clear."""

    def __init__(self, phone_id, name, cookie, csrf_token, cookie_header):
        self.phone_id, self.name = phone_id, name
        self.role = "phone"
        self.manage_main = False
        self.cookie, self.csrf_token, self.cookie_header = cookie, csrf_token, cookie_header

    def public(self):
        return {"ok": True, "phone": {"id": self.phone_id, "name": self.name},
                "role": self.role, "manage_main": False, "csrf_token": self.csrf_token,
                "code": "paired"}

    def __repr__(self):
        return f"Session(phone_id={self.phone_id!r}, role='phone')"


class HttpResult:
    def __init__(self, status, body, headers=None, content_type="application/json; charset=utf-8"):
        self.status, self.body = status, body
        self.headers = headers or {}
        self.content_type = content_type


def _envelope(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _envelope_digest(envelope):
    return "sha256:" + hashlib.sha256(envelope).hexdigest()


# Signed claim inside a phone envelope. Field names match phone.py
# `_GATEWAY_PHONE_FIELDS`. `verify_message` only accepts DEVICE_NS; the phone
# domain is a prefix inside `gateway_phone_message`. `purpose` is gateway_phone.
# Authority status does not carry issuer_incarnation; the claim uses that field
# when status has it, otherwise the authority incarnation.
# A lost pair response cannot be replayed: the cookie is not stored.
# A lost preview challenge is repeated only from process RAM, never from disk.
# Pair, preview, and session-stop calls that omit operation_id stay valid so the
# older phone tests still run. phone.py effect_status is read as
# {status, body}; this module does not invent a job or a reply.
ENVELOPE_FIELDS = (
    "attempt_id", "authority_id", "digest", "domain", "epoch", "fleet_id",
    "incarnation", "issuer_incarnation", "kind", "main", "manage_main",
    "mode_version", "opener", "operation_id", "phone_id", "policy_version",
    "purpose", "role", "target", "v",
)
PHONE_DOMAIN = "jarvis-continuity-gateway-phone-v1"
PHONE_PURPOSE = "gateway_phone"
PHONE_ENVELOPE = {
    "version": 1,
    "namespace": DEVICE_NS,
    "domain": PHONE_DOMAIN,
    "purpose": PHONE_PURPOSE,
    "signed_fields": ENVELOPE_FIELDS,
    "wire": "UTF-8 JSON {v:1, claim, signature, body}",
    "unsigned": "refused; verify_phone_envelope returns None and the body is not relayed",
    "binding": "role phone, manage_main false, opener phone:{phone_id}, digest sha256 of the canonical body, target is the active MAIN",
    "verifier": "verify_phone_envelope(envelope_bytes, ssh-ed25519 public key) after GatewayPhoneVerifier",
}
NETWORK_TRANSIENT_BLOCKER = "transient_not_networked"
# What a process on another machine can call today. Transient phone bodies and owner
# proofs are in-process. This module does not add a gateway route for them.
GATEWAY_SESSION_DEPENDENCIES = {
    "in_process": "ContinuityGatewaySession",
    "network_class": "NetworkGatewaySession",
    "network_status": "POST /v1/authority/status via AuthorityClient",
    "network_metadata_relay": "POST /v1/authority/relay, five metadata fields, no phone body",
    "network_transient": "unsupported; WS5 owns the gateway HTTP route; forward_transient is not an HTTP route and MAX_BODY is 8192; blocker transient_not_networked",
    "network_transient_blocker": NETWORK_TRANSIENT_BLOCKER,
    "network_transition": "unsupported; OwnerProof is minted by authenticate_owner in the gateway process",
    "ws5": "Phone bodies across a network need the in-process session, or a gateway route this module will not invent",
}
_TRACE_STATE = frozenset({"run", "done", "fail", "pending", "error"})
_TRACE_KIND = frozenset({"step", "tool"})
_SECRET_TRACE = re.compile(
    r"\b(?:bearer|token|secret|password|passwd|api[_-]?key|authorization|cookie)\b\s*[:=]?\s*\S+",
    re.I)
_URL_AUTH_TRACE = re.compile(r"(https?://)[^/\s@]+@")
_URL_TAIL_TRACE = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_OPENSSL = "/usr/bin/openssl"
_WEBAUTHN_UP = 0x01
_WEBAUTHN_UV = 0x04
_WEBAUTHN_AT = 0x40


def pin_target(device_id, url, connect_ip, *, allow_loopback=False):
    """Check a relay target. Raises unless `connect_ip` is tailnet (or loopback, if allowed).

    The URL's host is not resolved. A DNS name is only the TLS name the transport may
    present while connecting to `connect_ip`. Userinfo, paths, queries, fragments,
    redirects, and non-tailnet addresses are rejected so a request cannot choose a host.
    """
    device_id = _ident(device_id, "device_id")
    parts = urlsplit(str(url or ""))
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise IngressError("bad_target", 400, "Relay target must be an origin, not a URL with a path or secret.")
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(str(connect_ip))
    except ValueError:
        raise IngressError("bad_target", 400, "Relay target needs a pinned IP address.") from None
    loopback = ip.is_loopback
    tailnet = any(ip in net for net in TAILNET)
    if loopback:
        if not allow_loopback:
            raise IngressError("bad_target", 400, "Loopback is not a relay target.")
    elif not tailnet or ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved:
        raise IngressError("bad_target", 400, "Relay target is not a tailnet address.")
    ip_host = _is_ip(host)
    if ip_host:
        if ipaddress.ip_address(host) != ip:
            raise IngressError("bad_target", 400, "Relay host and pinned address disagree.")
        sni = host
    else:
        if not _HOST.fullmatch(host):
            raise IngressError("bad_target", 400, "Relay name is not a DNS name this gateway can pin.")
        sni = host
    scheme = parts.scheme.lower()
    if scheme == "https":
        pass
    elif scheme == "http" and loopback and allow_loopback:
        pass
    else:
        raise IngressError("bad_target", 400, "Relay target must be https.")
    port = parts.port or (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise IngressError("bad_target", 400, "Relay port is not usable.")
    return PinnedTarget(device_id, str(ip), port, scheme, sni)


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _ident(value, name):
    if not isinstance(value, str) or not _ID.match(value):
        raise IngressError("bad_request", 400, f"{name} must be a short identifier")
    return value


def _sha256(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _safe_code(code):
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code):
        return code
    return "uncertain"


def _metadata_digest(operation_id, attempt_id, effect_class, main, incarnation, epoch):
    """Digest of routing metadata. The phone body is not an input."""
    raw = json.dumps(
        ["metadata", operation_id, attempt_id, effect_class, main, incarnation, epoch],
        separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


def _effect_replay_matches(existing, phone_id, operation_id, kind, body_digest):
    """True when this phone is repeating the body already pinned to the row.

    The pin is the MAIN tuple stored with the operation, not the tuple authority
    has now. A moved MAIN does not turn the same body into a conflict, and it does
    not authorize another send. A different phone, kind, or body does not match.
    """
    if existing["phone_id"] != phone_id or existing["kind"] != kind:
        return False
    target, incarnation, epoch = existing["target"], existing["incarnation"], existing["epoch"]
    if not isinstance(target, str) or not _ID.match(target):
        return False
    if isinstance(incarnation, bool) or not isinstance(incarnation, int) or incarnation < 0:
        return False
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return False
    pinned = _metadata_digest(operation_id, kind, body_digest, target, incarnation, epoch)
    return existing["digest"] == pinned


def _clean(value, limit):
    return _CONTROL.sub("", str(value or "")).strip()[:limit]


def _origin_of(value):
    parts = urlsplit(str(value or ""))
    if (parts.scheme != "https" or parts.username or parts.password or parts.query
            or parts.fragment or parts.path not in ("", "/")):
        raise IngressError("bad_request", 400, "Gateway origin must be one https name.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not _HOST.fullmatch(host):
        raise IngressError("bad_request", 400, "Gateway origin must be one https name.")
    if parts.port and parts.port != 443:
        return f"https://{host}:{parts.port}", frozenset({f"{host}:{parts.port}"})
    return f"https://{host}", frozenset({host, f"{host}:443"})


def _header(headers, name):
    if headers is None:
        return ""
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        raw = list(get_all(name) or [])
        if len(raw) > 1:
            return ""  # a second value must not hide behind Message.get's first
        if len(raw) == 1:
            return str(raw[0] or "").strip()
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    found = getter(name)
    if found:
        return str(found).strip()
    items = headers.items() if hasattr(headers, "items") else ()
    want = name.lower()
    for key, value in items:
        if str(key).lower() == want and value:
            return str(value).strip()
    return ""


def _writer_lock(path):
    handle = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise IngressError("writer_busy", 503, "Another ingress process owns this store.") from None
    return handle


def _normalize_code(value):
    text = re.sub(r"[\s-]+", "", str(value or "")).upper()
    return text if len(text) == CODE_LEN and all(c in CODE_CHARS for c in text) else ""


def cookie_values(headers):
    """Cookie values for `__Host-jarvis_phone`, same split as phone.py."""
    out = []
    raw = []
    get_all = getattr(headers, "get_all", None)
    if get_all:
        raw = list(get_all("Cookie") or [])
    elif _header(headers, "Cookie"):
        raw = [_header(headers, "Cookie")]
    for header in raw:
        for part in str(header).split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == PHONE_COOKIE and value:
                out.append(value.strip())
    return out


def _answers_approval(text):
    """True when the phone must not send this line. Import failure refuses the send."""
    words = text.lower().replace("\u2019", "'").rstrip(".!?").strip()
    if APPROVALS.fullmatch(words):
        return True
    try:
        from jarvis.assistant import confirmation_words
    except Exception:
        return True
    try:
        return bool(confirmation_words(text))
    except Exception:
        return True


def _clean_events(events):
    if not isinstance(events, list) or len(events) > 200:
        raise IngressError("bad_request", 400, "events must be a list of at most 200.")
    from jarvis.remote_view import clean_event
    out = []
    for event in events:
        try:
            out.append(clean_event(event))
        except ValueError as exc:
            message = str(exc)
            if len(message) > 200 or "\n" in message:
                message = "Unknown input event."
            raise IngressError("bad_request", 400, message) from None
    return out


def _b64url_decode(value, limit):
    """Strict unpadded base64url. None when the text is not the canonical encoding."""
    if not isinstance(value, str) or not value or len(value) > limit or not _B64URL.fullmatch(value):
        return None
    pad = "=" * ((4 - len(value) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
    except Exception:
        return None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        return None
    return raw


def _same_text(left, right):
    if not isinstance(left, str) or not isinstance(right, str) or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _sha_matches(presented, digest):
    if not isinstance(presented, str) or not isinstance(digest, str):
        return False
    return _same_text(_sha256(presented), digest)


def _phone_fields():
    """The signed field set from phone.py. Imported lazily so this module stays loadable."""
    from jarvis.phone import _GATEWAY_PHONE_FIELDS
    return _GATEWAY_PHONE_FIELDS


def _claim_fields(claim):
    """The signed phone claim, or None when the binding does not hold."""
    if not isinstance(claim, dict) or set(claim) != set(_phone_fields()):
        return None
    if claim.get("domain") != PHONE_DOMAIN or claim.get("purpose") != PHONE_PURPOSE:
        return None
    if claim.get("v") != 1 or claim.get("role") != "phone" or claim.get("manage_main") is not False:
        return None
    phone_id = claim.get("phone_id")
    if not isinstance(phone_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", phone_id):
        return None
    if claim.get("opener") != "phone:" + phone_id:
        return None
    if not isinstance(claim.get("operation_id"), str) or not _OP.match(claim["operation_id"]):
        return None
    if not isinstance(claim.get("attempt_id"), str) or not _ID.match(claim["attempt_id"]):
        return None
    if claim.get("kind") not in EFFECT_KINDS | READ_KINDS:
        return None
    for key in ("main", "target", "authority_id", "fleet_id"):
        if not isinstance(claim.get(key), str) or not _ID.match(claim[key]):
            return None
    for key in ("incarnation", "epoch", "mode_version", "policy_version", "issuer_incarnation"):
        value = claim.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
    digest = claim.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    return {key: claim[key] for key in ENVELOPE_FIELDS}


def _redact_trace(value, limit):
    """Light display text. Secrets and credentialed URLs are not copied through."""
    text = _URL_AUTH_TRACE.sub(r"\1", str(value or ""))
    text = _URL_TAIL_TRACE.sub(r"\1", text)
    text = _SECRET_TRACE.sub("[hidden]", text)
    return _CONTROL.sub("", text).strip()[:limit]


def _project_step(step):
    """Copy one real step. A non-step is dropped and nothing is invented in its place."""
    if not isinstance(step, dict):
        return None
    ident = step.get("id")
    if not isinstance(ident, str) or not _ID.match(ident):
        return None
    out = {"id": ident}
    parent = step.get("parent")
    if isinstance(parent, str) and _ID.match(parent):
        out["parent"] = parent
    if isinstance(step.get("label"), str):
        out["label"] = _redact_trace(step["label"], 80)
    if isinstance(step.get("detail"), str):
        out["detail"] = _redact_trace(step["detail"], 120)
    if step.get("state") in _TRACE_STATE:
        out["state"] = step["state"]
    if step.get("kind") in _TRACE_KIND:
        out["kind"] = step["kind"]
    return out


def _project_trace(trace):
    """Copy a trace object that was actually returned. Missing pieces stay missing."""
    if not isinstance(trace, dict):
        return None
    out = {}
    if isinstance(trace.get("available"), bool):
        out["available"] = trace["available"]
    rev = trace.get("rev")
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0:
        out["rev"] = rev
    elapsed = trace.get("elapsed")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
        out["elapsed"] = elapsed
    if isinstance(trace.get("tier"), str):
        out["tier"] = _redact_trace(trace["tier"], 40)
    if isinstance(trace.get("streaming"), bool):
        out["streaming"] = trace["streaming"]
    reasons = trace.get("reasons")
    if isinstance(reasons, list):
        clean = [_redact_trace(item, 80) for item in reasons[:8] if isinstance(item, str)]
        if clean:
            out["reasons"] = clean
    steps = trace.get("steps")
    if isinstance(steps, list):
        projected = [item for item in (_project_step(step) for step in steps[:40]) if item]
        if projected:
            out["steps"] = projected
    return out or None


def _client_challenge(proof):
    """Challenge inside clientDataJSON, None when that field is absent, False when it is broken."""
    if not isinstance(proof, dict):
        return None
    assertion = proof if isinstance(proof.get("response"), dict) else proof.get("assertion")
    if not isinstance(assertion, dict):
        return None
    response = assertion.get("response")
    if not isinstance(response, dict) or "clientDataJSON" not in response:
        return None
    raw = _b64url_decode(response.get("clientDataJSON"), 4096)
    if raw is None:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("challenge"), str):
        return False
    return data["challenge"]


def _bound_challenge(proof, digest):
    """The challenge the client presented, when it matches the preview hash and itself."""
    if not isinstance(proof, dict):
        return None
    presented = proof.get("challenge")
    if presented is not None and not isinstance(presented, str):
        return None
    inner = _client_challenge(proof)
    if inner is False:
        return None
    if presented is not None and inner is not None and not _same_text(presented, inner):
        return None
    chosen = presented if isinstance(presented, str) else inner
    if not _sha_matches(chosen, digest):
        return None
    return chosen


def _assertion_from(proof):
    if not isinstance(proof, dict):
        return None
    if isinstance(proof.get("response"), dict):
        return proof
    inner = proof.get("assertion")
    if isinstance(inner, dict):
        return inner
    return None


class EnvelopeSigner:
    """SSH-signs the phone claim with the ingress device key.

    The signed bytes are `gateway_phone_message`: the phone domain, then the
    DEVICE_NS canonical claim. The body is not signed on its own; its digest is.
    """

    def __init__(self, private_key_path):
        self._key = Path(private_key_path)
        if not self._key.is_file():
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")

    def sign(self, claim):
        fields = _claim_fields(claim)
        if fields is None:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        from jarvis.phone import gateway_phone_message
        try:
            message = gateway_phone_message(fields)
        except Exception:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.") from None
        try:
            return sign_message(DEVICE_NS, self._key, message)
        except Exception:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.") from None

    def __repr__(self):
        return "EnvelopeSigner(redacted)"


def verify_phone_envelope(envelope_bytes, public_key):
    """Return the claim when `GatewayPhoneVerifier` accepts the envelope.

    An unsigned envelope, a tampered field, a true `manage_main`, or the wrong key is None.
    This does not accept the body as work.
    """
    try:
        if not isinstance(envelope_bytes, (bytes, bytearray)) or not envelope_bytes:
            return None
        if len(envelope_bytes) > MAX_BODY:
            return None
        parsed = json.loads(bytes(envelope_bytes).decode("utf-8"))
        if not isinstance(parsed, dict) or set(parsed) != {"v", "claim", "signature", "body"}:
            return None
        if parsed.get("v") != 1:
            return None
        from jarvis.phone import GatewayPhoneVerifier
        GatewayPhoneVerifier(public_key).verify(bytes(envelope_bytes))
        return _claim_fields(parsed.get("claim"))
    except Exception:
        return None


class TicketSignatureVerifier:
    """True only when `signature` is an SSH signature of `TICKET_FIELDS` by that MAIN's key."""

    def __init__(self, public_keys):
        if not isinstance(public_keys, dict) or not public_keys:
            raise IngressError("bad_request", 400, "Ticket verification needs a MAIN public key.")
        cleaned = {}
        try:
            for device, key in public_keys.items():
                cleaned[_ident(device, "device_id")] = gateway_public_key(key)
        except (IngressError, GatewayError):
            raise IngressError("bad_request", 400, "Ticket verification needs a MAIN public key.") from None
        if not cleaned:
            raise IngressError("bad_request", 400, "Ticket verification needs a MAIN public key.")
        self._keys = cleaned

    def verify(self, signed):
        """Same check as calling the verifier. Phone's peer accepts either shape."""
        return self(signed) is True

    def __call__(self, signed):
        try:
            if not isinstance(signed, dict):
                return False
            if any(key not in signed for key in TICKET_FIELDS):
                return False
            signature = signed.get("signature")
            if not isinstance(signature, str) or "BEGIN SSH SIGNATURE" not in signature:
                return False
            main = signed.get("main")
            public_key = self._keys.get(main)
            if public_key is None:
                return False
            fields = {key: signed[key] for key in TICKET_FIELDS}
            return verify_message(TICKET_NS, gateway_message(TICKET_NS, fields), signature, public_key) is True
        except Exception:
            return False

    def __repr__(self):
        return "TicketSignatureVerifier(redacted)"


class WebAuthnAssertionVerifier:
    """One pinned credential. `verify` is True only after OpenSSL accepts the ES256 assertion."""

    def __init__(self, *, credential_id, public_pem, rp_id, origin):
        if not isinstance(credential_id, str) or not _B64URL.fullmatch(credential_id) or not 1 <= len(credential_id) <= 256:
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        if not isinstance(public_pem, str) or not public_pem.isascii() or len(public_pem) > 8000:
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        if "PRIVATE" in public_pem or "BEGIN PUBLIC KEY" not in public_pem or "END PUBLIC KEY" not in public_pem:
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        if not isinstance(rp_id, str) or not rp_id or len(rp_id) > 253 or rp_id != rp_id.lower():
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        if "/" in rp_id or ":" in rp_id or " " in rp_id:
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        self.credential_ids = (credential_id,)
        self.rp_id = rp_id
        self.origin = _webauthn_origin(origin)
        self._pem = public_pem
        self._counter = None
        self._zero = set()
        self._lock = threading.Lock()
        if not Path(_OPENSSL).is_file():
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        self._require_public_pem()

    def verify(self, assertion, challenge):
        """True only for this credential, this challenge, and a fresh counter. Never raises."""
        try:
            parsed = self._parse(assertion, challenge)
            if parsed is None:
                return False
            auth_data, client_raw, signature, counter = parsed
            if not self._openssl_verify(auth_data + hashlib.sha256(client_raw).digest(), signature):
                return False
            marker = hashlib.sha256(auth_data + client_raw + signature).digest()
            with self._lock:
                return self._accept_counter(counter, marker)
        except Exception:
            return False

    def _parse(self, assertion, challenge):
        if not isinstance(assertion, dict) or not isinstance(challenge, str) or not challenge:
            return None
        if assertion.get("type") != "public-key" or not _same_text(assertion.get("id"), self.credential_ids[0]):
            return None
        raw_id = assertion.get("rawId", assertion.get("raw_id"))
        if raw_id is not None and not _same_text(raw_id, self.credential_ids[0]):
            return None
        response = assertion.get("response")
        if not isinstance(response, dict):
            return None
        client_raw = _b64url_decode(response.get("clientDataJSON"), 4096)
        auth_data = _b64url_decode(response.get("authenticatorData"), 4096)
        signature = _b64url_decode(response.get("signature"), 512)
        if client_raw is None or auth_data is None or signature is None or not signature:
            return None
        if not 37 <= len(auth_data) <= 1024:
            return None
        try:
            client = json.loads(client_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(client, dict):
            return None
        if client.get("type") != "webauthn.get" or client.get("challenge") != challenge:
            return None
        if client.get("origin") != self.origin or client.get("crossOrigin") is not False:
            return None
        if hashlib.sha256(self.rp_id.encode("utf-8")).digest() != auth_data[:32]:
            return None
        flags = auth_data[32]
        if not flags & _WEBAUTHN_UP or not flags & _WEBAUTHN_UV or flags & _WEBAUTHN_AT:
            return None
        return auth_data, client_raw, signature, int.from_bytes(auth_data[33:37], "big")

    def _accept_counter(self, counter, marker):
        if counter > 0:
            if self._counter is not None and counter <= self._counter:
                return False
            self._counter = counter
            return True
        if self._counter not in (None, 0) or marker in self._zero:
            return False
        self._zero.add(marker)
        return True

    def _openssl_verify(self, message, signature):
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-webauthn-") as folder:
                pub = Path(folder) / "pub.pem"
                sig = Path(folder) / "sig.der"
                pub.write_text(self._pem, encoding="ascii")
                sig.write_bytes(signature)
                out = subprocess.run(
                    [_OPENSSL, "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
                    input=message, capture_output=True, timeout=5,
                )
            return out.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _require_public_pem(self):
        """P-256 (prime256v1) only. RSA and every other curve are refused."""
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-webauthn-") as folder:
                pub = Path(folder) / "pub.pem"
                pub.write_text(self._pem, encoding="ascii")
                out = subprocess.run(
                    [_OPENSSL, "pkey", "-pubin", "-in", str(pub), "-text", "-noout"],
                    capture_output=True, timeout=5,
                )
        except (OSError, subprocess.SubprocessError):
            raise IngressError("passkey_unavailable", 503,
                               "Passkey checks are not configured. Nothing was changed.") from None
        text = out.stdout.decode("ascii", "replace") if out.returncode == 0 else ""
        if "prime256v1" not in text or "RSA Public-Key" in text or "Private-Key" in text:
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")

    def __repr__(self):
        return "WebAuthnAssertionVerifier(redacted)"


def _webauthn_origin(origin):
    parts = urlsplit(str(origin or ""))
    if (parts.scheme != "https" or parts.username or parts.password or parts.query
            or parts.fragment or parts.path not in ("", "/")):
        raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or _is_ip(host):
        raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
    if parts.port and parts.port != 443:
        return f"https://{host}:{parts.port}"
    return f"https://{host}"


class OwnerAssertionBridge:
    """Mint a gateway `OwnerProof` after the passkey has already been checked.

    The owner key signs the transition payload. It is not a substitute for the assertion.
    """

    def __init__(self, gateway, owner_private_key, wall=None):
        if not callable(getattr(gateway, "issue_challenge", None)) or not callable(
                getattr(gateway, "authenticate_owner", None)):
            raise IngressError("owner_proof_required", 503,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        self._gateway = gateway
        self._key = Path(owner_private_key)
        if not self._key.is_file():
            raise IngressError("owner_proof_required", 503,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        self._wall = wall or time.time

    def prove(self, payload):
        digest = payload_digest(payload)
        challenge = self._gateway.issue_challenge(digest)
        issued = int(self._wall())
        nonce = "n-" + secrets.token_hex(8)
        signature = sign_message(OWNER_NS, self._key, gateway_message(OWNER_NS, {
            "v": 1, "challenge_id": challenge["challenge_id"], "nonce": nonce,
            "issued_at": issued, "payload_digest": digest,
        }))
        proof = self._gateway.authenticate_owner(
            challenge_id=challenge["challenge_id"], nonce=nonce, issued_at=issued,
            purpose="transition", payload=payload, signature=signature)
        if not isinstance(proof, GatewayOwnerProof):
            raise IngressError("owner_proof_required", 403,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        return proof

    def __repr__(self):
        return "OwnerAssertionBridge(redacted)"


class NetworkGatewaySession:
    """HTTP client for gateway routes that exist today.

    `status` and metadata `relay` use `AuthorityClient`. Phone bodies and owner proofs do
    not: `forward_transient` is not an HTTP route, and an in-process `OwnerProof` cannot
    be replayed on the wire. `transient_supported` is false. See `GATEWAY_SESSION_DEPENDENCIES`.
    """

    transient_supported = False
    transition_supported = False

    def __init__(self, origin, *, authority_id, device_id, installation_id, private_key_path,
                 owner_private_key_path=None, wall=None):
        self._client = AuthorityClient(
            origin, authority_id=authority_id, wall=wall or time.time, device_id=device_id,
            installation_id=installation_id, private_key_path=private_key_path,
            owner_private_key_path=owner_private_key_path)
        self.origin = self._client.origin

    def status(self):
        return self._client.status()

    def relay_metadata(self, operation_id, target, attempt_id, digest, capability):
        payload = {
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability,
        }
        if set(payload) != {"operation_id", "target", "attempt_id", "digest", "capability"}:
            raise IngressError("bad_request", 400, "That request cannot be relayed.")
        if PERSONAL_KEYS.intersection(payload):
            raise IngressError("personal_field", 400, "That request cannot be relayed.")
        return self._client.relay(operation_id, target, attempt_id, digest, capability)

    def relay_transient(self, operation_id, target, attempt_id, digest, capability, kind, envelope):
        del operation_id, target, attempt_id, digest, capability, kind, envelope
        raise IngressError(
            NETWORK_TRANSIENT_BLOCKER, 503,
            "WS5 owns the gateway HTTP route. NetworkGatewaySession cannot carry a phone body. Nothing was sent.")

    def transition_request(self, owner_proof, payload):
        del payload
        if not isinstance(owner_proof, GatewayOwnerProof):
            raise IngressError("owner_proof_required", 403,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        raise IngressError(
            "transition_not_networked", 503,
            "An in-process owner proof cannot be presented to the HTTP gateway. Nothing was changed.")


class ContinuityGatewaySession:
    """Signed in-process client of `ContinuityGateway`.

    Each call mints a one-use device principal. `relay_metadata` has no body, URL, or
    prompt argument. `transition_request` accepts only a gateway `OwnerProof`. This
    object does not listen and does not dial a phone target.
    """

    def __init__(self, gateway, device_id, installation_id, private_key_path, *, wall=None):
        self._gateway = gateway
        self.device_id = _ident(device_id, "device_id")
        self.installation_id = _ident(installation_id, "installation_id")
        self._key = Path(private_key_path)
        self._wall = wall or time.time

    def status(self):
        return self._device("status", {})

    def relay_metadata(self, operation_id, target, attempt_id, digest, capability):
        payload = {
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability,
        }
        if set(payload) != {"operation_id", "target", "attempt_id", "digest", "capability"}:
            raise IngressError("bad_request", 400, "That request cannot be relayed.")
        if PERSONAL_KEYS.intersection(payload):
            raise IngressError("personal_field", 400, "That request cannot be relayed.")
        return self._device("relay", payload)

    def relay_transient(self, operation_id, target, attempt_id, digest, capability, kind, envelope):
        """Send one phone request to the ready MAIN through the gateway's RAM-only path.

        `envelope` is the request body. It is bound to `digest` and signed over by hash only.
        """
        payload = {
            "operation_id": operation_id, "target": target, "attempt_id": attempt_id,
            "digest": digest, "capability": capability, "kind": kind,
        }
        if not isinstance(envelope, (bytes, bytearray)):
            raise IngressError("bad_request", 400, "That request cannot be relayed.")
        return self._device("relay", payload, envelope=bytes(envelope))

    def transition_request(self, owner_proof, payload):
        if not isinstance(owner_proof, GatewayOwnerProof):
            raise IngressError("owner_proof_required", 403,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        if not isinstance(payload, dict) or PERSONAL_KEYS.intersection(payload):
            raise IngressError("personal_field", 400, "That request cannot be relayed.")
        return self._gateway.transition_request(owner_proof, payload)

    def _device(self, purpose, payload, envelope=None):
        issued_at = int(self._wall())
        nonce = "n-" + secrets.token_hex(8)
        signed = payload_digest(payload)
        signature = sign_message(DEVICE_NS, self._key, gateway_message(DEVICE_NS, {
            "v": 1, "device_id": self.device_id, "installation_id": self.installation_id,
            "nonce": nonce, "issued_at": issued_at, "purpose": purpose, "payload_digest": signed,
        }))
        principal = self._gateway.authenticate_device(
            device_id=self.device_id, installation_id=self.installation_id, nonce=nonce,
            issued_at=issued_at, purpose=purpose, payload=payload, signature=signature)
        if purpose == "status":
            return self._gateway.status(principal, payload)
        if envelope is not None:
            return self._gateway.forward_transient(principal, payload, envelope)
        return self._gateway.relay(principal, payload)


def production_ingress(path, gateway, *, origin, installation_id, allowed_mains, peer_verifier,
                       blocked_stable_ids, webauthn, owner_bridge, envelope_signer, ticket_verifier,
                       owner_verifier=None, clock=time.time, log=None):
    """Ingress that refuses stand-in verifiers.

    `webauthn`, `owner_bridge`, `envelope_signer`, and `ticket_verifier` must be the
    concrete classes. A callback that returns true is not accepted here.
    """
    if not isinstance(envelope_signer, EnvelopeSigner):
        raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
    if not isinstance(webauthn, WebAuthnAssertionVerifier):
        raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
    if not isinstance(owner_bridge, OwnerAssertionBridge):
        raise IngressError("owner_proof_required", 503, "MAIN changes need a fresh owner proof. Nothing was changed.")
    if not isinstance(ticket_verifier, TicketSignatureVerifier):
        raise IngressError("bad_request", 400, "Ticket verification needs a MAIN public key.")
    made = MobileIngress(
        path, gateway, origin=origin, installation_id=installation_id, allowed_mains=allowed_mains,
        peer_verifier=peer_verifier, blocked_stable_ids=blocked_stable_ids, owner_verifier=owner_verifier,
        webauthn=webauthn, owner_bridge=owner_bridge, envelope_signer=envelope_signer,
        ticket_verifier=ticket_verifier, clock=clock, log=log)
    made._production = True
    made._passkeys = None
    made._owner_proof = None
    return made


class MobileIngress:
    """Pairing, session ownership, and metadata relay to the current ready MAIN.

    `gateway` must provide `status()`, `relay_metadata(...)`, and `transition_request(proof, payload)`.
    Pass a `ContinuityGatewaySession` for the real gateway. `allowed_mains` maps a device id to a
    `PinnedTarget` from `pin_target`. That pin is an allowlist, not a URL this process dials.
    `blocked_stable_ids` is the enrolled PCs that must not pair; `None` or empty refuses activation.
    """

    def __init__(self, path, gateway, *, origin, installation_id, allowed_mains,
                 peer_verifier, blocked_stable_ids, passkey_verifier=None, owner_verifier=None,
                 owner_proof=None, clock=time.time, log=None, webauthn=None, owner_bridge=None,
                 envelope_signer=None, ticket_verifier=None):
        self.path = Path(path)
        self.origin, self.hosts = _origin_of(origin)
        self.installation_id = _ident(installation_id, "installation_id")
        self._gateway = gateway
        for name in GATEWAY_METHODS:
            if not callable(getattr(gateway, name, None)):
                raise IngressError("bad_request", 400, "Gateway client is missing a required method.")
        self.allowed = {}
        for device, target in (allowed_mains or {}).items():
            if not isinstance(target, PinnedTarget) or target.device_id != device:
                raise IngressError("bad_target", 400, "Allowed MAINs must be pinned targets.")
            self.allowed[_ident(device, "device_id")] = target
        if blocked_stable_ids is None:
            self._blocked = None
        else:
            self._blocked = frozenset(_ident(s, "stable_id") for s in blocked_stable_ids)
        self._peers, self._owners = peer_verifier, owner_verifier
        self._passkeys = passkey_verifier
        self._owner_proof = owner_proof
        self._webauthn = webauthn
        self._owner_bridge = owner_bridge
        self._signer = envelope_signer
        self._tickets = ticket_verifier
        self._passkey_verified = set()
        self._preview_challenges = {}
        self._production = False
        self._clock = clock
        self._log = log if callable(log) else None
        self._activated = None
        self._principals = set()
        self._lock = threading.RLock()
        self._hits = {}
        self._global_hits = []
        self._writer = _writer_lock(str(self.path) + ".writer")
        try:
            self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._open_schema()
        except Exception:
            db = getattr(self, "_db", None)
            if db is not None:
                db.close()
            self._writer.close()
            raise

    def close(self):
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            try:
                self._db.close()
            finally:
                self._writer.close()

    def _note(self, code):
        if self._log is not None and isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code):
            self._log(code)

    def _tx(self):
        return transaction(self._db, self._lock)

    def _open_schema(self):
        self._db.executescript(_SCHEMA)
        self._ensure_column("codes", "operation_id", "TEXT")
        self._ensure_column("previews", "authorized", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("previews", "operation_id", "TEXT")
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS previews_phone_op ON previews(phone_id, operation_id)"
            " WHERE operation_id IS NOT NULL")
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
            elif row[0] != str(SCHEMA_VERSION):
                raise IngressError("quarantined", 503, "Ingress store schema is unrecognized.")
            db.execute("UPDATE operations SET state='uncertain' WHERE state='inflight'")
            db.execute("UPDATE previews SET state='uncertain' WHERE state='sending'")

    def _ensure_column(self, table, column, declaration):
        have = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    # --- cutover -----------------------------------------------------------------------

    def activate(self, expected_mode_version, *, legacy_phone_fenced):
        """Start serving this origin. Refuses while the authority is legacy or the caller
        does not claim the old phone server was fenced. The claim is recorded as unproved."""
        if legacy_phone_fenced is not True:
            raise IngressError("legacy_unfenced", 409, "Legacy phone admission is still a separate origin.")
        if not self._blocked:
            raise IngressError("peer_policy_unset", 409, "Enrolled computers that must not pair were not set.")
        if not callable(self._peers):
            raise IngressError("peer_unavailable", 503, "No device verifier is configured.")
        if isinstance(expected_mode_version, bool) or not isinstance(expected_mode_version, int):
            raise IngressError("bad_request", 400, "expected_mode_version must be an integer")
        status = self._fetch_status()
        if status.get("mode") == "QUARANTINED":
            raise IngressError("quarantined", 503, "Authority state is unrecognized.")
        if status.get("mode") not in V2_SERVE:
            raise IngressError("legacy_retained", 409, LEGACY_TEXT)
        if status.get("mode_version") != expected_mode_version:
            raise IngressError("stale", 409, "Mode version does not match this cutover.")
        self._activated = expected_mode_version
        self._note("activated")
        return {"activated": True, "mode": status["mode"], "mode_version": expected_mode_version,
                "legacy_phone_fenced": True, "proved": False}

    def _fetch_status(self):
        try:
            status = self._gateway.status()
        except AuthorityError as exc:
            raise self._from_authority(exc) from None
        except GatewayError as exc:
            raise self._from_gateway(exc) from None
        except IngressError:
            raise
        except Exception:
            self._note("authority_unavailable")
            raise IngressError("authority_unavailable", 503,
                               "The authority is unavailable. Nothing was sent.") from None
        if not isinstance(status, dict) or status.get("mode") not in V2_SERVE | {"QUARANTINED",
                "LEGACY_ACTIVE", "PREPARING_V2", "QUIESCED", "ROLLBACK_QUIESCED"}:
            raise IngressError("authority_unavailable", 503, "The authority answer was not usable.")
        for key in ("incarnation", "epoch", "policy_version", "mode_version"):
            value = status.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IngressError("authority_unavailable", 503, "The authority answer was not usable.")
        return status

    def _live(self):
        if self._activated is None:
            raise IngressError("legacy_retained", 503, LEGACY_TEXT)
        status = self._fetch_status()
        if status.get("mode") == "QUARANTINED":
            raise IngressError("quarantined", 503, "Authority state is unrecognized. Nothing was sent.")
        if status.get("mode") not in V2_SERVE:
            raise IngressError("legacy_retained", 503, LEGACY_TEXT)
        if status.get("mode_version") != self._activated:
            raise IngressError("cutover_stale", 503, "Cutover no longer matches the authority. Nothing was sent.")
        return status

    def _from_authority(self, exc):
        code = exc.code if exc.code in _AUTH_STATUS else "authority_unavailable"
        return self._refusal(code, _AUTH_STATUS.get(code, 503), exc.wait)

    def _from_gateway(self, exc):
        code = exc.code if isinstance(getattr(exc, "code", None), str) else "authority_unavailable"
        if code == "unauthorized":
            code = "authority_unavailable"
        status = _GATEWAY_HTTP.get(code, 503)
        if code not in _GATEWAY_HTTP:
            code = "authority_unavailable"
        return self._refusal(code, status, getattr(exc, "wait", None))

    def _refusal(self, code, status, wait):
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait != wait or not 0 <= wait <= 3600:
            wait = None
        if re.fullmatch(r"[a-z0-9_]{1,40}", code):
            self._note(code)
        return IngressError(code, status, "The authority refused that.", wait=wait)

    def _route(self, status):
        """The only device an ordinary relay may name: the ready MAIN, and only if pinned."""
        ready = (status.get("mode") == "V2_ACTIVE" and status.get("main_ready") is True
                 and status.get("lease_valid") is True and isinstance(status.get("main"), str))
        if not ready:
            raise IngressError("no_main", 503, "No ready MAIN. Nothing was sent.")
        main = status["main"]
        holder = status.get("lease_holder")
        if isinstance(holder, str) and holder != main:
            raise IngressError("no_main", 503, "Authority disagreed about which MAIN is ready. Nothing was sent.")
        if main not in self.allowed:
            raise IngressError("target_denied", 503, "The ready MAIN is not an allowed relay target. Nothing was sent.")
        return main

    # --- identity ----------------------------------------------------------------------

    def identify(self, headers):
        """Verify the tailnet peer. Sets the context `authenticate` requires."""
        self._live()
        if not callable(self._peers):
            raise IngressError("peer_unavailable", 401, "No device verifier is configured.", pair=False)
        try:
            who = self._peers(headers)
        except IngressError:
            raise
        except Exception:
            who = None
        if not isinstance(who, dict):
            raise IngressError("unauthorized", 401, "Tailscale couldn't say which device sent this.", pair=False)
        stable, login = who.get("stable_id"), who.get("login")
        os_name, node = who.get("os"), who.get("node")
        if not all(isinstance(v, str) and v for v in (stable, login, os_name, node)):
            raise IngressError("unauthorized", 401, "Tailscale couldn't say which device sent this.", pair=False)
        if not _ID.match(stable):
            raise IngressError("unauthorized", 401, "Tailscale couldn't say which device sent this.", pair=False)
        header = _header(headers, "Tailscale-User-Login")
        if not header or header.casefold() != login.casefold():
            raise IngressError("forbidden", 403, "That Tailscale login doesn't match this device.")
        proof = PeerProof(self, {"stable_id": stable, "login": _clean(login, 120),
                                  "os": _clean(os_name, 40), "node": _clean(node, 80),
                                  "address": str(who.get("address") or "")})
        _PEER.set(proof)
        return proof

    def _peer(self, proof):
        current = _PEER.get()
        if (not isinstance(proof, PeerProof) or proof.ingress is not self
                or not isinstance(current, PeerProof) or current.ingress is not self
                or current.who["stable_id"] != proof.who["stable_id"]):
            raise IngressError("peer_required", 401, "This phone isn't paired with JARVIS.", pair=True)
        return proof.who

    def authenticate(self, cookie, csrf_token, request_origin):
        self._live()
        current = _PEER.get()
        if not isinstance(current, PeerProof) or current.ingress is not self:
            raise IngressError("peer_required", 401, "This phone isn't paired with JARVIS.", pair=True)
        if request_origin != self.origin:
            raise IngressError("forbidden", 403, "Unexpected origin.")
        if not isinstance(cookie, str) or not cookie:
            raise IngressError("unauthorized", 401, "This phone isn't paired with JARVIS.", pair=True)
        row = self._phone_by_hash(_sha256(cookie))
        if row is None or row["revoked"]:
            raise IngressError("unauthorized", 401, "This phone isn't paired with JARVIS (or was removed).", pair=True)
        if not isinstance(csrf_token, str) or not csrf_token or not hmac.compare_digest(
                row["csrf_sha256"], _sha256(csrf_token)):
            raise IngressError("csrf", 403, "The session token did not match this origin.")
        who = current.who
        if row["stable_id"] != who["stable_id"] or row["login"].casefold() != who["login"].casefold():
            raise IngressError("unauthorized", 401, "This phone isn't paired with JARVIS (or was removed).", pair=True)
        self._seen(row)
        return self._mint(row)

    def _mint(self, row):
        token = secrets.token_hex(8)
        self._principals.add(token)
        return Principal(self, token, row["id"], row["name"], row["stable_id"], row["login"])

    def _check(self, principal):
        if (not isinstance(principal, Principal) or principal._ingress is not self
                or principal._token not in self._principals):
            raise IngressError("bad_request", 400, "Unauthenticated caller.")
        row = self._phone_row(principal.phone_id)
        if row is None or row["revoked"]:
            raise IngressError("unauthorized", 401, "This phone isn't paired with JARVIS (or was removed).", pair=True)
        return row

    def authenticate_owner(self, material):
        """An enrolled PC owner. A phone cookie is not owner material."""
        self._live()
        if not callable(self._owners):
            raise IngressError("owner_unavailable", 503, "Owner checks are not configured.")
        try:
            who = self._owners(material)
        except IngressError:
            raise
        except Exception:
            who = None
        if not isinstance(who, dict) or who.get("role") != "owner":
            raise IngressError("forbidden", 403, "That is not an owner of this fleet.")
        return OwnerProof(self, _ident(who.get("installation_id"), "installation_id"),
                          _ident(who.get("device_id"), "device_id"))

    def _owner(self, proof):
        if not isinstance(proof, OwnerProof) or proof.ingress is not self:
            raise IngressError("forbidden", 403, "That is not an owner of this fleet.")
        return proof

    # --- pairing -----------------------------------------------------------------------

    def issue_code(self, owner_proof, name=None, allow_desktop=False):
        """Mint a pairing code. The plaintext is returned once; the store keeps a salted hash."""
        self._live()
        self._owner(owner_proof)
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        salt = secrets.token_hex(16)
        now = self._clock()
        code_id = secrets.token_hex(6)
        shown = _clean(name, NAME_MAX) or None
        with self._tx() as db:
            self._purge(db, now)
            db.execute("INSERT INTO codes(id, salt, sha256, created, expires, state, name, allow_desktop)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       (code_id, salt, _sha256(salt + code), now, now + CODE_TTL, "waiting",
                        shown, 1 if allow_desktop else 0))
        self._note("code_issued")
        return {"code_id": code_id, "code": code, "expires": now + CODE_TTL}

    def decide_pairing(self, owner_proof, code_id, allow):
        self._live()
        self._owner(owner_proof)
        _ident(code_id, "code_id")
        now = self._clock()
        with self._tx() as db:
            entry = db.execute("SELECT * FROM codes WHERE id=?", (code_id,)).fetchone()
            if entry is None or entry["expires"] <= now or entry["state"] != "pending":
                raise IngressError("not_found", 404, "That pairing code is not waiting for an answer.")
            state = "approved" if allow else "denied"
            expires = max(entry["expires"], now + APPROVED_GRACE) if allow else entry["expires"]
            db.execute("UPDATE codes SET state=?, expires=? WHERE id=?", (state, expires, code_id))
        self._note("pair_allowed" if allow else "pair_denied")
        return {"ok": True, "state": "approved" if allow else "denied"}

    def grant_manage_main(self, owner_proof, phone_id):
        """A separate grant. Pairing does not call this."""
        self._live()
        self._owner(owner_proof)
        with self._tx() as db:
            cur = db.execute("UPDATE phones SET manage_main=1 WHERE id=? AND revoked=0",
                             (_ident(phone_id, "phone_id"),))
            if cur.rowcount != 1:
                raise IngressError("not_found", 404, "No such phone.")
        self._note("manage_granted")
        return {"ok": True, "phone_id": phone_id, "manage_main": True}

    def revoke_phone(self, owner_proof, phone_id):
        self._live()
        self._owner(owner_proof)
        self._revoke(phone_id)
        return {"ok": True}

    def pair(self, pairing_proof, client_metadata):
        """Claim or finish a code. A pending code is not a session and sets no cookie.

        A supplied operation id is bound to this code. The cookie is returned once.
        A retry after that returns `pair_lost` and does not mint another phone.
        """
        self._live()
        if not isinstance(pairing_proof, dict):
            raise IngressError("bad_request", 400, "Send the code first.")
        operation_id = pairing_proof.get("operation_id", None)
        if operation_id is not None and (not isinstance(operation_id, str) or not _OP.match(operation_id)):
            raise IngressError("operation_id_required", 400, "This request needs a stable operation id.")
        who = self._peer(pairing_proof.get("peer"))
        code = _normalize_code(pairing_proof.get("code"))
        if not code:
            raise IngressError("bad_request", 400, "Type the 8-character code shown on your PC.")
        if who["stable_id"] in self._blocked:
            raise IngressError("forbidden", 403, "That's one of your JARVIS computers. Pair from your phone.")
        keys = ("node:" + who["stable_id"],)
        address = who.get("address") or ""
        if address:
            keys = ("ip:" + address,) + keys
        if any(self._blocked_key(k) for k in keys):
            raise IngressError("rate", 429, "Too many wrong codes. Wait ten minutes, then make a new one on your PC.")
        mobile = who["os"].casefold() in MOBILE_OS
        name = _clean((client_metadata or {}).get("name") if isinstance(client_metadata, dict) else "", NAME_MAX)
        now = self._clock()
        with self._tx() as db:
            outcome, detail = self._pair_step(
                db, code, who, name, mobile, bool(pairing_proof.get("claim")), now, operation_id)
        if outcome == "desktop":
            raise IngressError("forbidden", 403, "Only a phone can pair with JARVIS.")
        if outcome == "bad":
            for key in keys:
                self._hit(key)
            self._hit_global()  # once per attempt, not once per source key
            raise IngressError("forbidden", 403, "That code is wrong or has expired. Make a new one on your PC.")
        if outcome == "unclaimed":
            raise IngressError("bad_request", 400, "Send the code first.")
        if outcome == "taken":
            raise IngressError("conflict", 409, "Another device is already using that code.")
        if outcome == "denied":
            raise IngressError("forbidden", 403, "Pairing was refused on the PC.")
        if outcome == "pending":
            raise IngressError("pending", 202, "Waiting for approval on your PC.")
        if outcome == "mismatch":
            raise IngressError("conflict", 409, "That pairing attempt does not match this code.")
        if outcome == "lost":
            raise IngressError("pair_lost", 409, "That pairing session was already issued. Pair again from the PC.")
        self._note("paired")
        return detail

    def _pair_operation(self, db, entry, operation_id, now):
        """Bind one operation id to this code. The cookie is never written here."""
        if operation_id is None:
            if entry["operation_id"]:
                return "mismatch"
            return None
        bound = entry["operation_id"]
        if bound and bound != operation_id:
            return "mismatch"
        prior = db.execute("SELECT * FROM pair_binds WHERE operation_id=?", (operation_id,)).fetchone()
        if prior is not None and prior["code_id"] != entry["id"]:
            return "mismatch"
        if prior is not None and prior["state"] == "issued":
            return "lost"
        if not bound:
            db.execute("UPDATE codes SET operation_id=? WHERE id=?", (operation_id, entry["id"]))
        if prior is None:
            db.execute("INSERT INTO pair_binds(operation_id, code_id, state, phone_id, updated_at)"
                       " VALUES(?,?,?,?,?)", (operation_id, entry["id"], "claimed", None, now))
        return None

    def _pair_step(self, db, code, who, name, mobile, claim, now, operation_id=None):
        self._purge(db, now)
        if operation_id is not None:
            prior = db.execute("SELECT * FROM pair_binds WHERE operation_id=?", (operation_id,)).fetchone()
            if prior is not None and prior["state"] == "issued":
                return "lost", None
        rows = db.execute("SELECT * FROM codes WHERE expires>?", (now,)).fetchall()
        if not mobile and not any(r["allow_desktop"] for r in rows):
            return "desktop", None
        entry = None
        for row in rows:
            if hmac.compare_digest(row["sha256"], _sha256(row["salt"] + code)):
                entry = row
                break
        if entry is None:
            return "bad", None
        if not mobile and not entry["allow_desktop"]:
            return "desktop", None
        bound = self._pair_operation(db, entry, operation_id, now)
        if bound is not None:
            return bound, None
        if entry["state"] == "waiting":
            if not claim:
                return "unclaimed", None
            shown = entry["name"] or name or who["node"] or "Phone"
            db.execute("UPDATE codes SET state='pending', stable_id=?, login=?, os=?, node=?, name=? WHERE id=?",
                       (who["stable_id"], who["login"], who["os"], who["node"], _clean(shown, NAME_MAX), entry["id"]))
            return "pending", None
        if entry["stable_id"] != who["stable_id"]:
            return "taken", None
        if entry["state"] == "pending":
            return "pending", None
        if entry["state"] == "denied":
            return "denied", None
        if entry["state"] != "approved":
            return "bad", None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        phone_id = secrets.token_hex(4)
        while db.execute("SELECT 1 FROM phones WHERE id=?", (phone_id,)).fetchone():
            phone_id = secrets.token_hex(4)
        shown = _clean(entry["name"] or name or "Phone", NAME_MAX) or "Phone"
        db.execute("INSERT INTO phones(id, name, token_sha256, csrf_sha256, stable_id, login, os, node,"
                   " created, last_seen, revoked, manage_main) VALUES(?,?,?,?,?,?,?,?,?,?,0,0)",
                   (phone_id, shown, _sha256(token), _sha256(csrf), who["stable_id"], who["login"],
                    who["os"], who["node"], now, now))
        db.execute("DELETE FROM codes WHERE id=?", (entry["id"],))
        if operation_id is not None:
            db.execute("UPDATE pair_binds SET state='issued', phone_id=?, updated_at=? WHERE operation_id=?",
                       (phone_id, now, operation_id))
        session = Session(phone_id, shown, token, csrf,
                          f"{PHONE_COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={COOKIE_AGE}")
        return "paired", session

    def _purge(self, db, now):
        db.execute("DELETE FROM codes WHERE expires<=?", (now,))

    def _blocked_key(self, key):
        now = self._clock()
        with self._lock:
            self._global_hits = [t for t in self._global_hits if now - t < BAD_WINDOW]
            if len(self._global_hits) >= GLOBAL_BAD:
                return True
            hits = [t for t in self._hits.get(key, ()) if now - t < BAD_WINDOW]
            if hits:
                self._hits[key] = hits
            else:
                self._hits.pop(key, None)
            return len(hits) >= BAD_CODES

    def _hit(self, key):
        with self._lock:
            now = self._clock()
            if len(self._hits) > 1024:
                kept = {}
                for name, stamps in self._hits.items():
                    fresh = [t for t in stamps if now - t < BAD_WINDOW]
                    if len(fresh) >= BAD_CODES:
                        kept[name] = fresh
                self._hits = kept  # locked sources stay; the global count is not cleared
            fresh = [t for t in self._hits.get(key, ()) if now - t < BAD_WINDOW]
            fresh.append(now)
            self._hits[key] = fresh

    def _hit_global(self):
        with self._lock:
            now = self._clock()
            self._global_hits = [t for t in self._global_hits if now - t < BAD_WINDOW]
            self._global_hits.append(now)

    # --- reads and effects -------------------------------------------------------------

    def status(self, principal):
        row = self._check(principal)
        view = self._authority_view(self._live())
        # `relay` is true only when the gateway has the transient path and MAIN is ready.
        relay = self._transient() and view["main_ready"] is True
        return {"phone": {"id": row["id"], "name": row["name"], "created": row["created"]},
                "role": "phone", "manage_main": bool(row["manage_main"]), "origin": self.origin,
                "relay": relay, "metadata_only": not relay, "authority": view}

    def _transient(self):
        return callable(getattr(self._gateway, "relay_transient", None))

    def _authority_view(self, status):
        try:
            main = self._route(status)
            ready = True
        except IngressError as exc:
            if exc.code not in ("no_main", "target_denied"):
                raise
            main, ready = None, False
        authority_id = status.get("authority_id")
        if not isinstance(authority_id, str) or not _ID.match(authority_id):
            authority_id = None
        preferred = status.get("preferred") if isinstance(status.get("preferred"), str) and _ID.match(
            status.get("preferred") or "") else None
        return {"mode": status["mode"], "mode_version": status["mode_version"],
                "incarnation": status["incarnation"], "epoch": status["epoch"],
                "policy_version": status["policy_version"], "main": main, "main_ready": ready,
                "authority_id": authority_id, "preferred": preferred}

    def read(self, principal, kind, query=None):
        """Validate a read, then fetch it once through the transient path, or refuse.

        Nothing returned is stored. Without `relay_transient` the read is refused.
        """
        self._check(principal)
        if kind not in READ_KINDS:
            raise IngressError("bad_request", 400, "Unknown read.")
        status = self._live()
        main = self._route(status)
        query_body = self._read_body(principal, kind, query or {})
        if not self._transient():
            raise IngressError("body_not_relayable", 503, BODY_TEXT)
        operation_id = "r-" + secrets.token_hex(8)
        attempt_id = "a-" + secrets.token_hex(8)
        envelope = self._phone_envelope(principal, operation_id, attempt_id, kind, query_body, status, main)
        stamp = self._authority_stamp(status, main)
        try:
            result = self._gateway.relay_transient(
                operation_id, main, attempt_id, _envelope_digest(envelope),
                "client", kind, envelope)
        except GatewayError as exc:
            raise self._from_gateway(exc) from None
        except IngressError:
            raise
        except Exception:
            self._note("uncertain")
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.") from None
        if not self._read_result_ok(operation_id, result):
            self._note("relay_overclaim")
            raise IngressError("relay_overclaim", 503,
                               "The relay did not authorize an effect. Nothing more was sent.")
        if not self._stamp_still(stamp):
            self._note("stale_main")
            raise IngressError("stale", 409, "The MAIN changed. That answer was not shown.")
        shaped = dict(result["result"])
        if result.get("raw") is not None:
            shaped["raw"] = result["raw"]
        return self._project(kind, shaped)

    def _phone_envelope(self, principal, operation_id, attempt_id, kind, body, status, main):
        """Signed `{v, claim, signature, body}`. A missing signer does not transmit the body.

        `attempt_id` is chosen by the caller and is the same value passed to relay.
        `target` is the active MAIN. An execution node stays in `body["device"]`.
        `body` is the request object itself, not a `{kind, query}` wrapper.
        """
        if not isinstance(self._signer, EnvelopeSigner):
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        claim = self._phone_claim(principal, operation_id, attempt_id, kind, body, status, main)
        if _claim_fields(claim) is None:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        try:
            signature = self._signer.sign(claim)
        except IngressError:
            raise
        except Exception:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.") from None
        if not isinstance(signature, str) or "BEGIN SSH SIGNATURE" not in signature:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        return _envelope({"v": 1, "claim": claim, "signature": signature, "body": body})

    def _phone_claim(self, principal, operation_id, attempt_id, kind, body, status, main):
        authority_id = status.get("authority_id")
        fleet_id = status.get("fleet_id")
        if not isinstance(authority_id, str) or not _ID.match(authority_id):
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        if not isinstance(fleet_id, str) or not _ID.match(fleet_id):
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        issuer = status.get("issuer_incarnation", status.get("incarnation"))
        if isinstance(issuer, bool) or not isinstance(issuer, int) or issuer < 0:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.")
        from jarvis.phone import gateway_phone_digest
        try:
            digest = gateway_phone_digest(body)
        except Exception:
            raise IngressError("envelope_unsigned", 503, "This phone request was not sent.") from None
        return {
            "attempt_id": attempt_id, "authority_id": authority_id, "digest": digest,
            "domain": PHONE_DOMAIN, "epoch": status["epoch"], "fleet_id": fleet_id,
            "incarnation": status["incarnation"], "issuer_incarnation": issuer, "kind": kind,
            "main": main, "manage_main": False, "mode_version": status["mode_version"],
            "opener": "phone:" + principal.phone_id, "operation_id": operation_id,
            "phone_id": principal.phone_id, "policy_version": status["policy_version"],
            "purpose": PHONE_PURPOSE, "role": "phone", "target": main, "v": 1,
        }

    def _authority_stamp(self, status, main):
        return (main, status["incarnation"], status["epoch"], status["mode_version"])

    def _stamp_still(self, stamp):
        """False when a fresh status disagrees with the tuple just used, or cannot be read."""
        try:
            status = self._live()
            main = self._route(status)
        except IngressError:
            return False
        return self._authority_stamp(status, main) == stamp

    def verify_ticket(self, signed):
        """False unless the configured verifier is a `TicketSignatureVerifier` and the signature holds."""
        verifier = self._tickets
        if not isinstance(verifier, TicketSignatureVerifier):
            return False
        return verifier(signed) is True

    def _read_result_ok(self, operation_id, result):
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            return False
        raw = result.get("raw")
        return (result.get("operation_id") == operation_id and result.get("relayed") is True
                and result.get("effect_authorized") is False
                and (raw is None or isinstance(raw, (bytes, bytearray))))

    def _read_body(self, principal, kind, query):
        if kind in ("overview", "fleet"):
            return {}
        if kind == "ask_poll":
            job = query.get("job")
            if not isinstance(job, str) or not _JOB.match(job):
                raise IngressError("not_found", 404, "No such request.")
            row = self._op_by_job(principal.phone_id, job)
            if row is None:
                raise IngressError("not_found", 404, "No such request.")
            after = query.get("after", 0)
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise IngressError("bad_request", 400, "after must be a frame or trace number.")
            return {"job": job, "after": after}
        if kind == "session_frame":
            sid = query.get("session")
            row = self._own_session(principal, sid)
            after = query.get("after", 0)
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise IngressError("bad_request", 400, "after must be a frame number.")
            return {"session": row["session_id"], "after": after}
        if kind == "effect_status":
            operation_id = query.get("operation_id")
            if not isinstance(operation_id, str) or not _OP.match(operation_id):
                raise IngressError("bad_request", 400, "Unknown operation.")
            row = self._op(operation_id)
            if row is None or row["phone_id"] != principal.phone_id:
                raise IngressError("not_found", 404, "No such request.")
            return {"operation_id": operation_id}
        raise IngressError("bad_request", 400, "Unknown read.")

    def submit(self, principal, operation_id, request):
        """Metadata notice, or a refusal when the request needs a phone body.

        A phone kind is validated and then refused. It is not stored and not relayed.
        A metadata request is forwarded once. The same id is not sent again.
        """
        self._check(principal)
        if not isinstance(request, dict):
            raise IngressError("bad_request", 400, "Send a JSON object.")
        if not isinstance(operation_id, str) or not _OP.match(operation_id):
            raise IngressError("operation_id_required", 400, "This request needs a stable operation id.")
        kind = request.get("kind")
        if kind in EFFECT_KINDS:
            if self._transient():
                return self._submit_effect(principal, operation_id, request)
            return self._refuse_body(principal, operation_id, request)
        if PERSONAL_KEYS.intersection(request) or set(request) - METADATA_KEYS:
            raise IngressError("personal_field", 400, "That request cannot be relayed.")
        return self._submit_metadata(principal, operation_id, request)

    def _refuse_body(self, principal, operation_id, request):
        """Run the phone checks, then stop. Nothing is inserted and nothing is relayed."""
        del operation_id
        status = self._live()
        self._effect_body(principal, request.get("kind"), request, status)
        self._route(status)
        self._note("body_not_relayable")
        raise IngressError("body_not_relayable", 503, BODY_TEXT)

    def _submit_effect(self, principal, operation_id, request):
        """Forward one phone effect once. The body goes to MAIN and is never stored here.

        The row keeps ids and a digest only. The same id and body returns the stored
        outcome. A lost or odd answer stays `uncertain` and is not sent again.
        """
        kind = request.get("kind")
        status = self._live()
        body = self._effect_body(principal, kind, request, status)
        main = self._route(status)
        # The row identity is the inner body pinned to the MAIN tuple at first send.
        # A later tuple is a new operation, not a new body for this id. A signature
        # would make every retry look new, so the row does not store the outer bytes.
        body_digest = _envelope_digest(_envelope(body))
        digest = _metadata_digest(operation_id, kind, body_digest, main,
                                  status["incarnation"], status["epoch"])
        existing = self._op(operation_id)
        if existing is not None:
            if not _effect_replay_matches(existing, principal.phone_id, operation_id, kind, body_digest):
                raise IngressError("conflict", 409, "That request already exists with different contents.")
            return self._stored(existing)
        attempt_id = "a-" + secrets.token_hex(8)
        envelope = self._phone_envelope(principal, operation_id, attempt_id, kind, body, status, main)
        env_digest = _envelope_digest(envelope)
        now = self._clock()
        try:
            with self._tx() as db:
                db.execute("INSERT INTO operations(operation_id, phone_id, kind, digest, state, target,"
                           " incarnation, epoch, mode_version, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (operation_id, principal.phone_id, kind, digest, "inflight", main,
                            status["incarnation"], status["epoch"], status["mode_version"], now))
        except sqlite3.IntegrityError:
            existing = self._op(operation_id)
            if existing is None or not _effect_replay_matches(
                    existing, principal.phone_id, operation_id, kind, body_digest):
                raise IngressError("conflict", 409, "That request already exists with different contents.") from None
            return self._stored(existing)
        stamp = self._authority_stamp(status, main)
        try:
            result = self._gateway.relay_transient(
                operation_id, main, attempt_id, env_digest, "client", kind, envelope)
        except RelayUncertain:
            self._update_op(operation_id, state="uncertain")
            self._note("uncertain")
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.") from None
        except GatewayError as exc:
            self._update_op(operation_id, state="uncertain", http_status=_GATEWAY_HTTP.get(exc.code, 503))
            raise self._from_gateway(exc) from None
        except IngressError as exc:
            self._update_op(operation_id, state="uncertain", http_status=exc.status)
            raise
        except Exception:
            self._update_op(operation_id, state="uncertain")
            self._note("uncertain")
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.") from None
        if not self._metadata_result_ok(operation_id, result):
            self._update_op(operation_id, state="uncertain")
            self._note("relay_overclaim")
            raise IngressError("relay_overclaim", 503,
                               "The relay did not authorize an effect. Nothing more was sent.")
        if result["upstream_accepted"] and not self._stamp_still(stamp):
            self._update_op(operation_id, state="uncertain")
            self._note("stale_main")
            raise IngressError("stale", 409, "The MAIN changed. That answer was not shown.")
        if not result["upstream_accepted"]:
            self._update_op(operation_id, state="refused", http_status=409)
            self._note("refused")
            raise IngressError("refused", 409, "The computer refused that.")
        self._update_op(operation_id, state="forwarded")
        self._note("forwarded")
        view = self._stored(self._op(operation_id))
        view["duplicate"] = False
        view["upstream_accepted"] = True
        if kind in ("ask", "session_start"):
            self._attach_effect_outcome(principal, operation_id, kind, body, status, main, view)
        return view

    def _attach_effect_outcome(self, principal, operation_id, kind, body, status, main, view):
        """Read the accepted effect back. The reply and the prompt are not stored.

        The effect post is accepted-only. The PC outcome travels on a later
        `effect_status` read whose wire result is `{status, body}`. A failure
        here does not send the effect again. A session id is the PC's id when
        that read carries one. If the read has no id, the local hash is only a
        handle for this phone until the PC reports one.
        """
        found = None
        try:
            found = self.read(principal, "effect_status", {"operation_id": operation_id})
        except IngressError:
            found = None
        session_id = None
        if isinstance(found, dict):
            for key in ("job", "reply", "trace", "pending", "waiting", "device", "target", "remote_state"):
                if key in found:
                    view[key] = found[key]
            if isinstance(found.get("session"), str) and _SESSION.match(found["session"]):
                session_id = found["session"]
                view["session"] = session_id
            if isinstance(found.get("job"), str) and _JOB.match(found["job"]):
                self._update_op(operation_id, job_id=found["job"])
        if kind != "session_start":
            return
        if not isinstance(session_id, str):
            session_id = hashlib.sha256(("session\n" + operation_id).encode("utf-8")).hexdigest()[:32]
            view["session"] = session_id
        if isinstance(body.get("device"), str):
            view["device"] = body["device"]
        self._record_session(principal, operation_id, session_id, body, status, main)

    def _record_session(self, principal, operation_id, session_id, body, status, main):
        """Remember the session id only. The prompt and the reply are not columns."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO sessions(session_id, phone_id, device, kind, state,"
                       " incarnation, epoch, main, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (session_id, principal.phone_id, body["device"], body["session_kind"], "live",
                        status["incarnation"], status["epoch"], main, self._clock()))
        self._update_op(operation_id, session_id=session_id)

    def _submit_metadata(self, principal, operation_id, request):
        attempt_id = _ident(request.get("attempt_id"), "attempt_id")
        effect_class = request.get("effect_class", "unspecified")
        if effect_class != "unspecified":
            effect_class = _ident(effect_class, "effect_class")
        if effect_class in PERSONAL_KEYS or effect_class in RELAY_CAPABILITIES:
            # A capability word would look like an authority grant. It is not an effect class.
            raise IngressError("bad_request", 400, "Unknown operation.")
        status = self._live()
        main = self._route(status)
        digest = _metadata_digest(operation_id, attempt_id, effect_class, main,
                                  status["incarnation"], status["epoch"])
        existing = self._op(operation_id)
        if existing is not None:
            if existing["phone_id"] != principal.phone_id or existing["digest"] != digest:
                raise IngressError("conflict", 409, "That request already exists with different contents.")
            return self._stored(existing)
        now = self._clock()
        try:
            with self._tx() as db:
                db.execute("INSERT INTO operations(operation_id, phone_id, kind, digest, state, target,"
                           " incarnation, epoch, mode_version, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (operation_id, principal.phone_id, "metadata", digest, "inflight", main,
                            status["incarnation"], status["epoch"], status["mode_version"], now))
        except sqlite3.IntegrityError:
            existing = self._op(operation_id)
            if existing is None or existing["digest"] != digest:
                raise IngressError("conflict", 409, "That request already exists with different contents.") from None
            return self._stored(existing)
        return self._finish_metadata(operation_id, main, attempt_id, digest)

    def _finish_metadata(self, operation_id, main, attempt_id, digest):
        if "client" not in RELAY_CAPABILITIES:
            self._update_op(operation_id, state="uncertain")
            raise IngressError("body_not_relayable", 503, BODY_TEXT)
        try:
            result = self._gateway.relay_metadata(operation_id, main, attempt_id, digest, "client")
        except RelayUncertain:
            self._update_op(operation_id, state="uncertain")
            self._note("uncertain")
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.") from None
        except GatewayError as exc:
            self._update_op(operation_id, state="uncertain", http_status=_GATEWAY_HTTP.get(exc.code, 503))
            raise self._from_gateway(exc) from None
        except IngressError as exc:
            self._update_op(operation_id, state="uncertain", http_status=exc.status)
            raise
        except Exception:
            self._update_op(operation_id, state="uncertain")
            self._note("uncertain")
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.") from None
        if not self._metadata_result_ok(operation_id, result):
            self._update_op(operation_id, state="uncertain")
            self._note("relay_overclaim")
            raise IngressError("relay_overclaim", 503,
                               "The relay did not authorize an effect. Nothing more was sent.")
        self._update_op(operation_id, state="forwarded")
        self._note("forwarded")
        view = self._stored(self._op(operation_id))
        view["duplicate"] = False
        view["upstream_accepted"] = bool(result.get("upstream_accepted"))
        return view

    def _metadata_result_ok(self, operation_id, result):
        if not isinstance(result, dict):
            return False
        if PERSONAL_KEYS.intersection(result) or any(
                key in result for key in ("path", "spool", "file", "redirect", "location", "url")):
            return False
        return (result.get("operation_id") == operation_id and result.get("relayed") is True
                and result.get("effect_authorized") is False
                and isinstance(result.get("upstream_accepted"), bool))

    def operation(self, principal, operation_id, cursor=None):
        """Read the stored metadata outcome. Does not send the notice again."""
        del cursor
        self._check(principal)
        if not isinstance(operation_id, str) or not _OP.match(operation_id):
            raise IngressError("not_found", 404, "No such request.")
        row = self._op(operation_id)
        if row is None or row["phone_id"] != principal.phone_id:
            raise IngressError("not_found", 404, "No such request.")
        return self._stored(row)

    def _stored(self, row):
        """The row as the client may see it. No relay, no body, no job text."""
        view = {"operation_id": row["operation_id"], "kind": row["kind"], "state": row["state"],
                "duplicate": True, "effect_authorized": False, "target": row["target"]}
        if isinstance(row["session_id"], str) and _SESSION.match(row["session_id"]):
            view["session"] = row["session_id"]
            session = self._session(row["session_id"])
            if session is not None and isinstance(session["device"], str) and _ID.match(session["device"]):
                view["device"] = session["device"]
        if row["state"] in ("inflight", "uncertain"):
            view["uncertain"] = True
            view["state"] = "uncertain"
        elif row["state"] == "forwarded":
            view["state"] = "forwarded"
        elif row["state"] == "refused":
            raise IngressError("refused", row["http_status"] or 400, "The computer refused that.")
        else:
            view["uncertain"] = True
            view["state"] = "uncertain"
        return view

    def _effect_body(self, principal, kind, request, status):
        if kind == "ask":
            text = request.get("text")
            if not isinstance(text, str) or not text.strip():
                raise IngressError("bad_request", 400, "Type something for JARVIS to do.")
            if len(text) > ASK_MAX:
                raise IngressError("bad_request", 400, f"Keep it under {ASK_MAX} characters.")
            text = _ASK_STRIP.sub("", text).strip()
            if not text or _answers_approval(text):
                raise IngressError("approval", 400, "Approve on your PC.")
            body = {"text": text}
            if request.get("device") is not None:
                body["device"] = _ident(request.get("device"), "device")
            return body
        if kind == "session_start":
            device = _ident(request.get("device"), "device")
            session_kind = request.get("session_kind")
            quality = request.get("quality") or "low"
            if session_kind not in ("screen", "camera"):
                raise IngressError("bad_request", 400, "kind must be screen or camera.")
            if quality not in ("low", "medium", "high"):
                raise IngressError("bad_request", 400, "quality must be low, medium or high.")
            return {"device": device, "session_kind": session_kind, "quality": quality}
        if kind == "session_input":
            row = self._own_session(principal, request.get("session"))
            self._session_current(row, status)
            return {"session": row["session_id"], "events": _clean_events(request.get("events"))}
        if kind == "session_stop":
            row = self._own_session(principal, request.get("session"))
            return {"session": row["session_id"], "release_only": True}
        if kind == "logout_end":
            return {"phone_id": principal.phone_id}
        raise IngressError("bad_request", 400, "Unknown operation.")

    def _session_current(self, row, status):
        main = status.get("main")
        if (row["incarnation"] != status["incarnation"] or row["epoch"] != status["epoch"]
                or row["main"] != main or status.get("main_ready") is not True):
            raise IngressError("stale_epoch", 409, "The session is paused. Input was not sent.")

    def _own_session(self, principal, session_id):
        if not isinstance(session_id, str) or not _SESSION.match(session_id):
            raise IngressError("not_found", 404, "No such session.")
        row = self._session(session_id)
        if row is None or row["phone_id"] != principal.phone_id or row["state"] == "ended":
            raise IngressError("not_found", 404, "No such session.")
        return row

    def logout(self, principal, operation_id=None):
        """End the phone. When a transient relay exists, ask MAIN to release before the local revoke."""
        row = self._check(principal)
        ending, release_error = "local_only", None
        if self._transient():
            if not isinstance(operation_id, str) or not _OP.match(operation_id):
                operation_id = "lo-" + secrets.token_hex(8)
            try:
                self.submit(principal, operation_id, {"kind": "logout_end"})
                ending = "released"
            except IngressError as exc:
                ending, release_error = "release_unconfirmed", _safe_code(exc.code)
            except Exception:
                ending, release_error = "release_unconfirmed", "uncertain"
        self._revoke(row["id"])
        self._principals.discard(principal._token)
        self._note("logout_local" if ending == "local_only" else "logout_release")
        out = {"ok": True, "ending": ending, "cookie_header":
               f"{PHONE_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"}
        if release_error:
            out["release_error"] = release_error
        return out

    def _revoke(self, phone_id):
        now = self._clock()
        with self._tx() as db:
            db.execute("UPDATE phones SET revoked=1, csrf_sha256=? WHERE id=?", (_sha256("revoked-" + phone_id), phone_id))
            db.execute("UPDATE sessions SET state='ended', updated_at=? WHERE phone_id=?", (now, phone_id))

    # --- MAIN management ---------------------------------------------------------------

    def manage_main_preview(self, principal, request):
        """Bind a preview to the current tuple. Does not open a transition.

        The same operation id returns the same preview. A missing operation id
        still mints one, which is what the older tests send. The challenge is
        kept in this process only.
        """
        row = self._check(principal)
        if not row["manage_main"]:
            raise IngressError("forbidden", 403, "This phone cannot change MAIN.")
        if not isinstance(request, dict):
            raise IngressError("bad_request", 400, "Send a JSON object.")
        operation_id = request.get("operation_id", None)
        if operation_id is not None and (not isinstance(operation_id, str) or not _OP.match(operation_id)):
            raise IngressError("operation_id_required", 400, "This request needs a stable operation id.")
        action, profile = request.get("action"), request.get("profile") or "control"
        if action not in MAIN_ACTIONS:
            raise IngressError("bad_request", 400, "Unknown action.")
        if profile not in MAIN_PROFILES:
            raise IngressError("bad_request", 400, "Unknown readiness profile.")
        candidate = _ident(request.get("candidate"), "candidate")
        status = self._live()
        if status["mode"] not in V2_SERVE:
            raise IngressError("legacy_retained", 503, LEGACY_TEXT)
        now = self._clock()
        challenge = secrets.token_urlsafe(32)
        preview_id = "pv-" + secrets.token_hex(8)
        with self._tx() as db:
            existing = None
            if operation_id is not None:
                existing = db.execute("SELECT * FROM previews WHERE phone_id=? AND operation_id=?",
                                      (principal.phone_id, operation_id)).fetchone()
            if existing is not None:
                if (existing["action"] != action or existing["candidate"] != candidate
                        or existing["profile"] != profile):
                    raise IngressError("conflict", 409, "That request already exists with different contents.")
                preview = existing
            else:
                try:
                    db.execute("INSERT INTO previews(preview_id, phone_id, action, candidate, profile, incarnation,"
                               " epoch, policy_version, mode_version, challenge_sha256, expires, state, updated_at,"
                               " authorized, operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (preview_id, principal.phone_id, action, candidate, profile, status["incarnation"],
                                status["epoch"], status["policy_version"], status["mode_version"], _sha256(challenge),
                                now + PREVIEW_TTL, "open", now, 0, operation_id))
                except sqlite3.IntegrityError:
                    existing = db.execute("SELECT * FROM previews WHERE phone_id=? AND operation_id=?",
                                          (principal.phone_id, operation_id)).fetchone()
                    if existing is None or existing["action"] != action or existing["candidate"] != candidate or existing["profile"] != profile:
                        raise IngressError("conflict", 409, "That request already exists with different contents.") from None
                    preview = existing
                else:
                    preview = db.execute("SELECT * FROM previews WHERE preview_id=?", (preview_id,)).fetchone()
                    self._preview_challenges[preview_id] = challenge
        self._note("preview")
        shown = self._preview_challenges.get(preview["preview_id"])
        return self._preview_view(preview, shown)

    def _preview_view(self, preview, challenge):
        options = self._webauthn_options()
        out = {"preview_id": preview["preview_id"], "action": preview["action"], "candidate": preview["candidate"],
               "profile": preview["profile"], "incarnation": preview["incarnation"], "epoch": preview["epoch"],
               "policy_version": preview["policy_version"], "mode_version": preview["mode_version"],
               "expires": preview["expires"], "manage_main": True,
               "rp_id": options["rp_id"], "credential_ids": options["credential_ids"]}
        if isinstance(challenge, str) and challenge:
            out["challenge"] = challenge
        return out

    def manage_main_confirm(self, principal, preview_id, passkey_proof):
        """One transition for this preview. A second call reads that id; it does not start another."""
        row = self._check(principal)
        if not row["manage_main"]:
            raise IngressError("forbidden", 403, "This phone cannot change MAIN.")
        if not isinstance(preview_id, str) or not _ID.match(preview_id):
            raise IngressError("not_found", 404, "No such preview.")
        preview = self._preview(preview_id)
        if preview is None or preview["phone_id"] != principal.phone_id:
            raise IngressError("not_found", 404, "No such preview.")
        if preview["transition_id"]:
            return {"transition_id": preview["transition_id"], "duplicate": True, "state": preview["state"]}
        status = self._live()
        self._tuple_match(preview, status)
        if preview["state"] == "uncertain":
            if not self._preview_authorized(preview["preview_id"]):
                raise IngressError("forbidden", 403, "Passkey proof was refused.")
            return self._send_transition(preview, reconcile=True)
        if preview["state"] == "sending":
            raise IngressError("uncertain", 409, "The transition outcome is unknown. It was not started again.")
        if preview["state"] != "open":
            raise IngressError("uncertain", 409, "The transition outcome is unknown. It was not started again.")
        if self._clock() > preview["expires"]:
            raise IngressError("expired", 410, "That preview expired. Ask again.")
        self._accept_passkey(principal, preview, passkey_proof)
        self._passkey_verified.add(preview["preview_id"])
        return self._send_transition(preview, reconcile=False)

    def _webauthn_options(self):
        verifier = self._webauthn
        if isinstance(verifier, WebAuthnAssertionVerifier):
            return {"rp_id": verifier.rp_id, "credential_ids": list(verifier.credential_ids)}
        return {"rp_id": "", "credential_ids": []}

    def _accept_passkey(self, principal, preview, proof):
        """Check the assertion. A configured verifier that is not the concrete class is not called."""
        if getattr(self, "_production", False) or self._webauthn is not None:
            if not isinstance(self._webauthn, WebAuthnAssertionVerifier):
                raise IngressError("passkey_unavailable", 503,
                                   "Passkey checks are not configured. Nothing was changed.")
            challenge = _bound_challenge(proof, preview["challenge_sha256"])
            assertion = _assertion_from(proof)
            try:
                ok = self._webauthn.verify(assertion, challenge) if challenge is not None else False
            except Exception:
                ok = False
            if ok is not True:
                raise IngressError("forbidden", 403, "Passkey proof was refused.")
            return
        if not callable(self._passkeys):
            raise IngressError("passkey_unavailable", 503, "Passkey checks are not configured. Nothing was changed.")
        if not isinstance(proof, dict):
            raise IngressError("forbidden", 403, "Passkey proof was refused.")
        presented = proof.get("challenge")
        if not isinstance(presented, str) or not _sha_matches(presented, preview["challenge_sha256"]):
            raise IngressError("forbidden", 403, "Passkey proof was refused.")
        try:
            receipt = self._passkeys(principal, self._preview_public(preview), proof)
        except IngressError:
            raise
        except Exception:
            receipt = None
        if not isinstance(receipt, str) or not _RECEIPT.match(receipt):
            raise IngressError("forbidden", 403, "Passkey proof was refused.")
        del receipt

    def _preview_authorized(self, preview_id):
        """True when this process or the durable row has already checked the passkey."""
        if preview_id in self._passkey_verified:
            return True
        row = self._preview(preview_id)
        return bool(row and row["authorized"])

    def _claim_preview(self, preview_id, *, reconcile):
        """One sender for this preview. `authorized` is written before the request leaves."""
        with self._tx() as db:
            row = db.execute("SELECT state, authorized, transition_id FROM previews WHERE preview_id=?",
                             (preview_id,)).fetchone()
            if row is None:
                return "forbidden"
            if row["transition_id"]:
                return "done"
            now = self._clock()
            if reconcile:
                if row["state"] == "sending":
                    return "busy"
                if row["state"] != "uncertain" or not row["authorized"]:
                    return "forbidden"
                cur = db.execute(
                    "UPDATE previews SET state='sending', updated_at=? WHERE preview_id=?"
                    " AND state='uncertain' AND authorized=1 AND transition_id IS NULL",
                    (now, preview_id))
            else:
                if row["state"] == "sending":
                    return "busy"
                if row["state"] != "open":
                    return "busy"
                cur = db.execute(
                    "UPDATE previews SET state='sending', authorized=1, updated_at=? WHERE preview_id=?"
                    " AND state='open' AND transition_id IS NULL",
                    (now, preview_id))
            if cur.rowcount == 1:
                return "ok"
            again = db.execute("SELECT transition_id FROM previews WHERE preview_id=?", (preview_id,)).fetchone()
            if again is not None and again["transition_id"]:
                return "done"
            return "busy"

    def _send_transition(self, preview, *, reconcile=False):
        """Same client_key. A second caller sees sending or the stored transition id."""
        claimed = self._claim_preview(preview["preview_id"], reconcile=reconcile)
        if claimed == "done":
            current = self._preview(preview["preview_id"])
            return {"transition_id": current["transition_id"], "duplicate": True, "state": current["state"]}
        if claimed == "forbidden":
            raise IngressError("forbidden", 403, "Passkey proof was refused.")
        if claimed != "ok":
            raise IngressError("uncertain", 409, "The transition outcome is unknown. It was not started again.")
        payload = self._transition_payload(preview)
        try:
            proof = self._mint_owner_proof(payload)
            view = self._gateway.transition_request(proof, payload)
        except AuthorityError as exc:
            self._mark_preview(preview["preview_id"], "uncertain")
            raise self._from_authority(exc) from None
        except GatewayError as exc:
            self._mark_preview(preview["preview_id"], "uncertain")
            raise self._from_gateway(exc) from None
        except IngressError:
            self._mark_preview(preview["preview_id"], "uncertain")
            raise
        except Exception:
            self._mark_preview(preview["preview_id"], "uncertain")
            self._note("transition_uncertain")
            raise IngressError("uncertain", 409, "The transition outcome is unknown. It was not started again.") from None
        return self._accept_transition(preview["preview_id"], view)

    def _mint_owner_proof(self, payload):
        if getattr(self, "_production", False) or self._owner_bridge is not None:
            if not isinstance(self._owner_bridge, OwnerAssertionBridge):
                raise IngressError("owner_proof_required", 403,
                                   "MAIN changes need a fresh owner proof. Nothing was changed.")
            if not isinstance(payload, dict) or not self._preview_authorized(payload.get("client_key")):
                raise IngressError("owner_proof_required", 403,
                                   "MAIN changes need a fresh owner proof. Nothing was changed.")
            try:
                proof = self._owner_bridge.prove(payload)
            except IngressError:
                raise
            except Exception:
                proof = None
            if not isinstance(proof, GatewayOwnerProof):
                raise IngressError("owner_proof_required", 403,
                                   "MAIN changes need a fresh owner proof. Nothing was changed.")
            return proof
        if not callable(self._owner_proof):
            raise IngressError("owner_proof_required", 503,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        try:
            proof = self._owner_proof(payload)
        except IngressError:
            raise
        except Exception:
            proof = None
        if not isinstance(proof, GatewayOwnerProof):
            raise IngressError("owner_proof_required", 403,
                               "MAIN changes need a fresh owner proof. Nothing was changed.")
        return proof

    def _transition_payload(self, preview):
        return {"client_key": preview["preview_id"], "action": preview["action"],
                "candidate": preview["candidate"], "expected_incarnation": preview["incarnation"],
                "expected_epoch": preview["epoch"], "expected_policy": preview["policy_version"],
                "profile": preview["profile"]}

    def _tuple_match(self, preview, status):
        if (preview["incarnation"], preview["epoch"], preview["policy_version"], preview["mode_version"]) != (
                status["incarnation"], status["epoch"], status["policy_version"], status["mode_version"]):
            raise IngressError("stale", 409, "Authority moved on. Ask for a new preview. Nothing was changed.")

    def _accept_transition(self, preview_id, view):
        if not isinstance(view, dict) or not isinstance(view.get("transition_id"), str) or not _ID.match(view["transition_id"]):
            self._mark_preview(preview_id, "uncertain")
            raise IngressError("uncertain", 409, "The transition outcome is unknown. It was not started again.")
        tid = view["transition_id"]
        with self._tx() as db:
            db.execute("UPDATE previews SET state='accepted', transition_id=?, updated_at=? WHERE preview_id=?",
                       (tid, self._clock(), preview_id))
        safe = {k: view.get(k) for k in ("transition_id", "action", "profile", "candidate", "state", "outcome")
                if k in view or k == "transition_id"}
        safe["transition_id"] = tid
        safe["duplicate"] = False
        self._note("transition")
        return safe

    def _preview_public(self, preview):
        return {"preview_id": preview["preview_id"], "action": preview["action"], "candidate": preview["candidate"],
                "profile": preview["profile"], "incarnation": preview["incarnation"], "epoch": preview["epoch"],
                "policy_version": preview["policy_version"], "mode_version": preview["mode_version"]}

    # --- relay -------------------------------------------------------------------------

    def _relay(self, *args, **kwargs):
        """Body relay is not a gateway method. Kept so a caller cannot post a prompt."""
        del args, kwargs
        raise IngressError("body_not_relayable", 503, BODY_TEXT)

    def _project(self, kind, result):
        """Keep the phone's answer in the return value only. Callers must not store `reply` or `raw`."""
        http_status = result.get("status") if isinstance(result.get("status"), int) else 200
        if result.get("uncertain") is True or http_status >= 500:
            return {"uncertain": True, "http_status": 409, "kind": kind}
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        if 400 <= http_status < 500:
            return {"http_status": http_status, "kind": kind, "refused": True,
                    "error": "The computer refused that."}
        if kind == "session_frame":
            if http_status == 204:
                ago = result.get("node_ago")
                return {"http_status": 204, "kind": kind,
                        "node_ago": ago if isinstance(ago, (int, float)) and not isinstance(ago, bool) else None}
            raw = result.get("raw")
            if not isinstance(raw, (bytes, bytearray)):
                raise IngressError("bad_gateway", 502, "The picture was refused.")
            if len(raw) > FRAME_MAX:
                raise IngressError("too_large", 413, "That picture is too large.")
            headers = result.get("headers") if isinstance(result.get("headers"), dict) else {}
            return {"http_status": 200, "kind": kind, "raw": bytes(raw),
                    "frame_seq": _num_header(headers.get("X-Frame-Seq")),
                    "frame_width": _num_header(headers.get("X-Frame-Width")),
                    "frame_height": _num_header(headers.get("X-Frame-Height"))}
        out = {"http_status": http_status, "kind": kind}
        if kind in ("overview", "fleet"):
            encoded = json.dumps(body, default=str)
            if len(encoded) > MAX_BODY:
                raise IngressError("too_large", 502, "The computer's answer was too large.")
            out["body"] = body
            return out
        if isinstance(body.get("job"), str) and _JOB.match(body["job"]):
            out["job"] = body["job"]
        if isinstance(body.get("id"), str) and _SESSION.match(body["id"]):
            out["session"] = body["id"]
        if isinstance(body.get("device"), str) and _ID.match(body["device"]):
            out["device"] = body["device"]
        for key in ("pending", "waiting", "found", "accepted"):
            if isinstance(body.get(key), bool):
                out[key] = body[key]
        if isinstance(body.get("reply"), str):
            out["reply"] = body["reply"][:20000]
        if isinstance(body.get("state"), str) and len(body["state"]) <= 40:
            out["remote_state"] = _clean(body["state"], 40)
        cursor = body.get("cursor", body.get("rev"))
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            out["cursor"] = cursor
        if isinstance(body.get("target"), str) and _ID.match(body["target"]):
            out["target"] = body["target"]
        traced = _project_trace(body.get("trace"))
        if traced is not None:
            out["trace"] = traced
        return out

    # --- rows --------------------------------------------------------------------------

    def _phone_by_hash(self, digest):
        with self._lock:
            return self._db.execute("SELECT * FROM phones WHERE token_sha256=?", (digest,)).fetchone()

    def _phone_row(self, phone_id):
        with self._lock:
            return self._db.execute("SELECT * FROM phones WHERE id=?", (phone_id,)).fetchone()

    def _op(self, operation_id):
        with self._lock:
            return self._db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()

    def _op_by_job(self, phone_id, job):
        with self._lock:
            return self._db.execute("SELECT * FROM operations WHERE phone_id=? AND job_id=?",
                                    (phone_id, job)).fetchone()

    def _session(self, session_id):
        with self._lock:
            return self._db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()

    def _preview(self, preview_id):
        with self._lock:
            return self._db.execute("SELECT * FROM previews WHERE preview_id=?", (preview_id,)).fetchone()

    def _update_op(self, operation_id, **fields):
        allowed = {"state", "http_status", "job_id", "session_id", "cursor"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=?")
                values.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        values.append(self._clock())
        values.append(operation_id)
        with self._tx() as db:
            db.execute(f"UPDATE operations SET {', '.join(sets)} WHERE operation_id=?", values)

    def _mark_preview(self, preview_id, state):
        with self._tx() as db:
            db.execute("UPDATE previews SET state=?, updated_at=? WHERE preview_id=?",
                       (state, self._clock(), preview_id))

    def _seen(self, phone):
        now = self._clock()
        if now - float(phone["last_seen"] or 0) < SEEN_EVERY:
            return
        with self._tx() as db:
            db.execute("UPDATE phones SET last_seen=? WHERE id=?", (now, phone["id"]))


def _num_header(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value):
        return int(value)
    if isinstance(value, (int, float)) and value == value:
        number = int(value)
        return number if number >= 0 else None
    return None


# The PC-side function a future adapter must implement. Not phone.py, and not this module.
PC_SEAM = {
    "function": "accept_gateway_phone",
    "where": "a new PC module the parent wires; do not add it to phone.py until that wiring is reviewed",
    "arguments": ("claim", "kind", "operation_id", "body"),
    "effects": tuple(sorted(EFFECT_KINDS)),
    "reads": tuple(sorted(READ_KINDS)),
    "opener": "phone:{phone_id}",
    "must_refuse": (
        "a raw __Host-jarvis_phone cookie presented by the gateway hop",
        "X-Forwarded-For or tailscale whois of the gateway as if it were the phone",
        "any URL, host, or address in the body (dial claim.main only if it matches the pinned target)",
        "a second execution of the same operation_id",
        "creating work when kind is a read, including effect_status and ask_poll",
        "logging or storing the body",
    ),
    "must_check": (
        "claim.role == phone and claim.manage_main is false",
        "claim.opener == phone:{claim.phone_id}",
        "the PC executor guard accepts the current authority tuple before any effect",
        "session_stop releases held input even when release_only is set",
        "identical operation_id + digest returns the first outcome; a different digest conflicts",
    ),
    "not_called": "ContinuityGateway.relay does not deliver this body. Ingress does not call this function.",
}


class PhoneProtocolAdapter:
    """Map the current phone HTTP paths onto `MobileIngress` without serving the shell.

    The live PWA does not send `X-CSRF-Token` or an operation id on session start/input.
    Those requests fail closed. Ask uses the existing `key` as the operation id.
    """

    def __init__(self, ingress):
        self.ingress = ingress

    def _authenticate(self, headers):
        """Try the phone cookies. A CSRF miss on a real cookie is not a reason to try the next one."""
        self.ingress.identify(headers)
        cookies = cookie_values(headers)[:4]
        csrf = _header(headers, CSRF_HEADER)
        if not cookies:
            return self.ingress.authenticate("", csrf, self.ingress.origin)
        last = None
        for cookie in cookies:
            try:
                return self.ingress.authenticate(cookie, csrf, self.ingress.origin)
            except IngressError as exc:
                last = exc
                if exc.code == "csrf":
                    raise
        raise last

    def handle(self, method, target, headers, body=b""):
        try:
            return self._handle(method, target, headers, body)
        except IngressError as exc:
            self.ingress._note("http_" + exc.code)
            headers_out = {}
            if exc.wait is not None:
                headers_out["Retry-After"] = str(int(exc.wait))
            return HttpResult(exc.status, exc.body() if exc.code != "pending" else {"pending": True, "code": "pending"},
                              headers_out)

    def _handle(self, method, target, headers, body):
        if method not in ("GET", "POST"):
            raise IngressError("method", 405, "Method not allowed.")
        parts = urlsplit(target or "")
        path = parts.path
        if "%" in path or "\\" in path or not path.startswith("/"):
            raise IngressError("not_found", 404, "Not found.")
        if "locate" in path or path.startswith("/phone/api/location"):
            raise IngressError("not_found", 404, "Not found.")
        if method == "GET" and path in PHONE_SHELL:
            raise IngressError("shell_not_ingress", 404, "Not found.")
        self._origin_headers(method, headers)
        if path in PAIR_PATHS:
            if method != "POST":
                raise IngressError("method", 405, "Method not allowed.")
            payload = self._json(body)
            proof = self.ingress.identify(headers)
            try:
                session = self.ingress.pair({"code": payload.get("code"), "claim": path == "/phone/pair",
                                             "peer": proof, "operation_id": payload.get("operation_id", None)},
                                            {"name": payload.get("name")})
            except IngressError as exc:
                if exc.code == "pending":
                    return HttpResult(202, {"pending": True, "code": "pending"})
                raise
            return HttpResult(200, session.public(), {"Set-Cookie": session.cookie_header})
        if not path.startswith("/phone/api/"):
            raise IngressError("not_found", 404, "Not found.")
        if _header(headers, PHONE_HEADER) != "1":
            raise IngressError("forbidden", 403, "Refused.")
        principal = self._authenticate(headers)
        if method == "GET" and path == "/phone/api/me":
            view = self.ingress.status(principal)
            main = view["authority"]["main"]
            return HttpResult(200, {"phone": view["phone"], "host": urlsplit(self.ingress.origin).hostname,
                                    "main": {"id": main, "label": main}, "role": "phone",
                                    "manage_main": view["manage_main"], "relay": view["relay"],
                                    "metadata_only": view["metadata_only"], "authority": view["authority"]})
        if method == "GET" and path == "/phone/api/overview":
            found = self.ingress.read(principal, "overview")
            return HttpResult(200, found.get("body") or {})
        if method == "GET" and path == "/phone/api/fleet":
            found = self.ingress.read(principal, "fleet")
            return HttpResult(200, found.get("body") or {})
        if method == "GET" and path == "/phone/api/session/frame":
            return self._frame(principal, parts.query)
        job = _JOB_PATH.match(path)
        if method == "GET" and job:
            return self._ask_poll(principal, job.group(1), parts.query)
        effect = _EFFECT_PATH.match(path)
        if method == "GET" and effect:
            found = self.ingress.read(principal, "effect_status", {"operation_id": effect.group(1)})
            return HttpResult(found.get("http_status") or 200, _public_effect(found))
        if method != "POST":
            raise IngressError("not_found", 404, "Not found.")
        payload = self._json(body)
        if path == "/phone/api/logout":
            done = self.ingress.logout(principal, payload.get("operation_id"))
            body = {"ok": True, "ending": done["ending"]}
            if done.get("release_error"):
                body["release_error"] = done["release_error"]
            return HttpResult(200, body, {"Set-Cookie": done["cookie_header"]})
        if path == "/phone/api/main/preview":
            return HttpResult(200, self.ingress.manage_main_preview(principal, payload))
        if path == "/phone/api/main/confirm":
            proof, alias = payload.get("proof", None), payload.get("passkey_proof", None)
            if proof is not None and alias is not None and proof != alias:
                raise IngressError("forbidden", 403, "Passkey proof was refused.")
            chosen = proof if proof is not None else alias
            return HttpResult(200, self.ingress.manage_main_confirm(principal, payload.get("preview_id"), chosen))
        if path == "/phone/api/ask":
            operation_id = payload.get("key")
            result = self.ingress.submit(principal, operation_id, {
                "kind": "ask", "text": payload.get("text"), "device": payload.get("device")})
            return self._effect_http(result)
        if path == "/phone/api/session/start":
            result = self.ingress.submit(principal, payload.get("operation_id"), {
                "kind": "session_start", "device": payload.get("device"),
                "session_kind": payload.get("kind"), "quality": payload.get("quality")})
            return self._effect_http(result, session=True)
        if path == "/phone/api/session/input":
            result = self.ingress.submit(principal, payload.get("operation_id"), {
                "kind": "session_input", "session": payload.get("session"), "events": payload.get("events")})
            return self._effect_http(result)
        if path == "/phone/api/session/stop":
            sid = payload.get("session")
            expected = "stop-" + sid if isinstance(sid, str) and _SESSION.match(sid) else ""
            supplied = payload.get("operation_id", None)
            if supplied is not None and supplied != expected:
                raise IngressError("conflict", 409, "The operation id does not match this session.")
            result = self.ingress.submit(principal, expected, {"kind": "session_stop", "session": sid})
            return self._effect_http(result)
        raise IngressError("not_found", 404, "Not found.")

    def _origin_headers(self, method, headers):
        host = _header(headers, "Host").lower()
        if host.endswith(".") and ":" not in host:
            host = host[:-1]
        if host not in self.ingress.hosts:
            raise IngressError("forbidden", 403, "Unexpected Host header.")
        site = _header(headers, "Sec-Fetch-Site")
        if site not in ("", "same-origin", "none"):
            raise IngressError("forbidden", 403, "Requests from other sites are refused.")
        origin = _header(headers, "Origin")
        if method == "POST" and origin != self.ingress.origin:
            raise IngressError("forbidden", 403, "Unexpected origin.")
        if origin and origin != self.ingress.origin:
            raise IngressError("forbidden", 403, "Unexpected origin.")

    def _json(self, body):
        raw = body if isinstance(body, (bytes, bytearray)) else bytes(str(body or ""), "utf-8")
        if len(raw) > MAX_BODY:
            raise IngressError("too_large", 413, "That request is too large.")
        try:
            payload = json.loads(raw or b"null")
        except (ValueError, UnicodeDecodeError):
            raise IngressError("bad_request", 400, "That isn't JSON.") from None
        if not isinstance(payload, dict):
            raise IngressError("bad_request", 400, "Send a JSON object.")
        return payload

    def _frame(self, principal, query):
        from urllib.parse import parse_qs
        parsed = parse_qs(query, keep_blank_values=False)
        session = (parsed.get("session") or [""])[0]
        try:
            after = int((parsed.get("after") or ["0"])[0] or 0)
        except ValueError:
            raise IngressError("bad_request", 400, "after must be a frame number.") from None
        found = self.ingress.read(principal, "session_frame", {"session": session, "after": after})
        if found.get("http_status") == 204:
            headers = {}
            if isinstance(found.get("node_ago"), (int, float)):
                headers["X-Node-Ago"] = f"{found['node_ago']:.1f}"
            return HttpResult(204, b"", headers, "text/plain")
        headers = {}
        for name, key in (("X-Frame-Seq", "frame_seq"), ("X-Frame-Width", "frame_width"),
                          ("X-Frame-Height", "frame_height")):
            if isinstance(found.get(key), int):
                headers[name] = str(found[key])
        return HttpResult(200, found.get("raw") or b"", headers, "image/jpeg")

    def _ask_poll(self, principal, job, query):
        from urllib.parse import parse_qs
        parsed = parse_qs(query)
        after = (parsed.get("after") or ["0"])[0]
        try:
            cursor = int(after) if re.fullmatch(r"\d{1,9}", after or "") else 0
        except ValueError:
            cursor = 0
        found = self.ingress.read(principal, "ask_poll", {"job": job, "after": cursor})
        return HttpResult(found.get("http_status") or 200, _public_effect(found))

    def _effect_http(self, result, session=False):
        if result.get("uncertain"):
            raise IngressError("uncertain", 409, "The outcome is unknown. It was not sent again.")
        if result.get("refused"):
            raise IngressError("refused", result.get("http_status") or 400,
                               result.get("error") or "The computer refused that.")
        return HttpResult(200, _public_effect(result, session=session))


_JOB_PATH = re.compile(r"^/phone/api/ask/([0-9a-f]{6,32})$")
_EFFECT_PATH = re.compile(r"^/phone/api/effect/([A-Za-z0-9_-]{8,64})$")


def _public_effect(result, session=False):
    """Phone JSON. A real trace is copied through. A cursor alone is only `{rev}`."""
    if session and result.get("session"):
        out = {"id": result["session"], "state": result.get("remote_state") or "starting"}
        if isinstance(result.get("device"), str):
            out["device"] = result["device"]
        if isinstance(result.get("target"), str):
            out["target"] = result["target"]
        return out
    out = {}
    if result.get("job"):
        out["job"] = result["job"]
    if result.get("pending") is True:
        out["pending"] = True
    if result.get("waiting") is True:
        out["waiting"] = True
    if isinstance(result.get("reply"), str):
        out["reply"] = result["reply"]
    trace = result.get("trace")
    if isinstance(trace, dict):
        out["trace"] = trace
    elif isinstance(result.get("cursor"), int) and not isinstance(result.get("cursor"), bool):
        out["trace"] = {"rev": result["cursor"]}
    if isinstance(result.get("target"), str):
        out["target"] = result["target"]
    if "ok" not in out and not session:
        out.setdefault("ok", True)
    return out
