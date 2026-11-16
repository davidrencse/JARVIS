"""Optional phone mount on an existing successor Hub listener.

Only a running, locally bound PhoneServer is a backend. Its real HTTP handler keeps
all phone identity, cookie, CSRF, origin and refusal checks. Cluster bearer tokens
never cross this seam. No listener, redirect following or configurable proxy target.
"""
import http.client
import json
import re
from pathlib import Path

from . import phone
from .continuity.phone_front import CHUNK, _RESPONSE_HEADERS, _routable

_REQUEST_HEADERS = frozenset({
    "host", "cookie", "content-type", "accept", "origin", "referer", "user-agent",
    "x-csrf-token", "x-jarvis-phone", "x-jarvis-session", "sec-fetch-site",
    "if-none-match", "if-modified-since", "x-forwarded-for", "tailscale-user-login",
    "x-jarvis-front", "x-jarvis-front-main", "x-jarvis-front-for",
    "x-jarvis-front-login", "x-forwarded-host",
})
_REPLY_HEADERS = (*_RESPONSE_HEADERS, "Set-Cookie", "X-Frame-Options",
                  "Cross-Origin-Resource-Policy")


def _backend_ready(hub, backend):
    if not isinstance(backend, phone.PhoneServer) or backend.hub is not hub:
        return False
    if getattr(hub, "started", False) is not True:
        return False
    listener, thread = backend.httpd, backend.thread
    if (listener is None or thread is None or not thread.is_alive()
            or listener.server_address[:2] != ("127.0.0.1", backend.port)):
        return False
    endpoint = getattr(hub, "continuity_endpoint", None)
    if not endpoint or backend._public_origin() != endpoint:
        return False
    return Path(hub.state).resolve() == backend.state.resolve()


def mount(hub, backend):
    """Opt in only after the existing loopback backend and exact Hub origin are ready."""
    if not _backend_ready(hub, backend) or backend.refusal() is not None:
        return False
    hub.phone = backend
    return True


def _authority_ready(hub, backend):
    """Read-only fence: never drop a candidate grant by trying ordinary admission.

    state() includes the runtime's local clock, pause and admission latches; the
    separate authenticated status read proves the exact grant is still ready MAIN.
    """
    from .continuity.integration import production_binding
    from .continuity.participant import lookup
    participant = lookup(backend.state)
    binding = production_binding(backend.state)
    runtime = getattr(participant, "runtime", None)
    if runtime is None or not binding or binding.get("runtime") is not runtime:
        return False
    device = getattr(participant, "device_id", None)
    if device != getattr(hub, "main", None) or device != runtime.device_id:
        return False
    status = participant.client.status()
    if (not isinstance(status, dict) or status.get("mode") != "V2_ACTIVE"
            or status.get("lease_valid") is not True or status.get("main_ready") is not True
            or status.get("main") != device or status.get("lease_holder") != device
            or status.get("transition") is not None):
        return False
    lease, local = runtime.lease_snapshot(), runtime.state()
    if (not isinstance(lease, dict) or not lease.get("lease_id")
            or lease.get("holder") != device or local.get("admission_open") is not True
            or local.get("stale") is not False or local.get("main") != device
            or not status.get("authority_id")
            or local.get("authority_id") != status.get("authority_id")):
        return False
    for key in ("incarnation", "epoch"):
        value = status.get(key)
        if (type(value) is not int or type(lease.get(key)) is not int
                or type(local.get(key)) is not int
                or lease[key] != value or local[key] != value):
            return False
    if runtime.readiness("control").get("meets") is not True:
        return False
    return (runtime.lease_snapshot() == lease and getattr(hub, "phone", None) is backend
            and backend.refusal() is None)


def _reply(handler, status, message, code, *, retry=False):
    raw = json.dumps({"error": message, "code": code}, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    if retry:
        handler.send_header("Retry-After", "2")
    handler.close_connection = True
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(raw)


def dispatch(handler, hub):
    """Handle an opted-in phone route, returning False for every other Hub request."""
    backend = getattr(hub, "phone", None)
    if backend is None or not _routable(handler.path):
        return False
    handler._phone_reply_started = False
    if not phone.loopback_peer(handler.client_address):
        _reply(handler, 403, "Refused.", "refused")
        return True
    if handler.command not in ("GET", "HEAD", "POST"):
        _reply(handler, 405, "Method not allowed.", "method")
        return True
    lengths = handler.headers.get_all("Content-Length") or []
    if handler.headers.get_all("Transfer-Encoding") or len(lengths) > 1:
        _reply(handler, 400, "Bad request length.", "length")
        return True
    length = 0
    if lengths:
        if not re.fullmatch(r"[0-9]+", lengths[0]):
            _reply(handler, 400, "Bad request length.", "length")
            return True
        if len(lengths[0]) > len(str(phone.MAX_BODY)):
            _reply(handler, 413, "Request too large.", "too_large")
            return True
        length = int(lengths[0])
    if length > phone.MAX_BODY:
        _reply(handler, 413, "Request too large.", "too_large")
        return True
    if handler.command != "POST" and length:
        _reply(handler, 400, "Unexpected request body.", "length")
        return True
    if handler.command == "POST" and not lengths:
        _reply(handler, 411, "Length required.", "length")
        return True
    try:
        ready = _backend_ready(hub, backend) and _authority_ready(hub, backend)
    except Exception:
        ready = False
    if not ready:
        _reply(handler, 503, "No ready MAIN. Waiting to reconnect.", "no_main", retry=True)
        return True
    body = handler.rfile.read(length) if length else None
    if length and len(body) != length:
        _reply(handler, 400, "Incomplete request body.", "length")
        return True
    conn = http.client.HTTPConnection("127.0.0.1", backend.port, timeout=phone.SOCKET_TIMEOUT)
    try:
        conn.putrequest(handler.command, handler.path, skip_host=True, skip_accept_encoding=True)
        for name, value in handler.headers.items():
            if name.lower() in _REQUEST_HEADERS:
                conn.putheader(name, value)
        if handler.command == "POST":
            conn.putheader("Content-Length", str(length))
        conn.endheaders(body)
        response = conn.getresponse()
        _relay(handler, response)
    except (OSError, http.client.HTTPException):
        if not getattr(handler, "_phone_reply_started", False):
            _reply(handler, 503, "The MAIN phone connection is unavailable.", "no_relay", retry=True)
        handler.close_connection = True
    finally:
        conn.close()
    return True


def _relay(handler, response):
    handler._phone_reply_started = True
    handler.send_response(response.status)
    for name in _REPLY_HEADERS:
        for value in response.headers.get_all(name) or ():
            handler.send_header(name, value)
    raw_length = response.getheader("Content-Length")
    length = int(raw_length) if raw_length and re.fullmatch(r"[0-9]+", raw_length) else None
    if length is not None:
        handler.send_header("Content-Length", str(length))
    elif response.status not in (204, 304) and handler.command != "HEAD":
        handler.close_connection = True
        handler.send_header("Connection", "close")
    handler.end_headers()
    if handler.command == "HEAD" or response.status in (204, 304):
        return
    sent = 0
    while length is None or sent < length:
        chunk = response.read1(CHUNK if length is None else min(CHUNK, length - sent))
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()
        sent += len(chunk)
    if length is not None and sent < length:
        handler.close_connection = True
