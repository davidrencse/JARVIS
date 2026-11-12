"""JARVIS on David's phone: the server behind the phone app (a PWA), over Tailscale.

Why its own listener: the console on :7777 trusts anything that reaches loopback (a fixed
header is all it asks), so a tailnet name must never reach it. This server binds
127.0.0.1:7787 only; David publishes it to his tailnet with `tailscale serve` (never
funnel). Every request must prove, failing closed at each step:
  1. Host is this PC's tailnet name (or the loopback address, in case Serve rewrites it);
  2. Serve forwarded the tailnet address (X-Forwarded-For) and `tailscale whois` knows it;
  3. Tailscale-User-Login matches whois's login;
  4. no other site sent it (Sec-Fetch-Site);
  5. a POST carries X-JARVIS-Phone and a JSON object;
  6. /phone/api/* carries the cookie of a phone paired at the main's own terminal, from the
     same tailnet device and login it paired from.
It serves a fixed list of shell files and a typed allowlist of routes, and never falls
through to console routes.

A paired phone is owner-level: remote screen input is raw keyboard and mouse, and Ask
reaches the brain under David's rules. What this can't stop: unsandboxed code running as
David on the main (Grok's local exec, the desktop `computer` tool typing in a real
terminal) can forge loopback requests or edit .jarvis/phone.json. gates.hard_deny is a
speed bump there, not a wall.
"""

import copy
import hashlib
import hmac
import ipaddress
import json
import queue
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .fsio import atomic_write, read_json
from .locking import exclusive_lock
from .platforms import private_dir, run_quiet

PORT = 7787  # not 7790-7799: the overlay and showcase scan those
STORE = "phone.json"
LOCK = "phone.lock"
COOKIE = "__Host-jarvis_phone"
COOKIE_AGE = 90 * 24 * 3600
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # base32 without 0/O and 1/I
CODE_LEN = 8
CODE_TTL = 300        # s a pairing code lives
APPROVED_GRACE = 60   # s the phone has to collect its key after David says yes
BAD_CODES, BAD_WINDOW = 5, 600  # wrong codes per source per window
MAX_BODY = 256 * 1024
MAX_CONNECTIONS = 24
SOCKET_TIMEOUT = 30
NAME_MAX, UA_MAX = 40, 160
TAILNET_TTL = 300     # s whois answers and this PC's tailnet name are cached
FAILED_TTL = 10       # s a failed `tailscale status` read is remembered
FUNNEL_TTL = 60       # s between reads of the serve config
SEEN_EVERY = 60       # s between last_seen writes per phone
WATCH_EVERY = 5       # s between checks for phones revoked from the CLI
ASK_WAIT = 3          # s POST /phone/api/ask waits for a quick reply before answering "pending"
ASK_POLL_WAIT = 8     # s GET /phone/api/ask/<job> waits (it returns early when the trace moves)
ASK_BATCH = 0.15      # s a poll lets a burst of steps settle before it answers
TRACE_JOBS = 48       # requests whose steps are kept for their phone (oldest forgotten)
TRACE_STEPS = 64      # steps kept per request
ASK_KEYS = 64         # recent (phone, key) pairs remembered so a retried Ask finds its first request
ASK_MAX = 2000
MOBILE_OS = frozenset({"ios", "android"})
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
QUALITIES = ("low", "medium", "high")
KINDS = ("screen", "camera")
SETUP = "sudo tailscale serve --bg --https=443 http://127.0.0.1:{port}"
UNDO = "sudo tailscale serve --https=443 http://127.0.0.1:{port} off"
NO_ADDRESS = "Tailscale serve did not forward the client address."
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")

WEB = Path(__file__).resolve().parent / "web"
# The only files served, by exact path: no folder is ever mapped, so nothing else can leak.
SHELL = {
    "/": "phone/index.html",
    "/phone.css": "phone/phone.css",
    "/phone.js": "phone/phone.js",
    "/sw.js": "phone/sw.js",
    "/manifest.webmanifest": "phone/manifest.webmanifest",
    "/icons/icon-192.png": "phone/icons/icon-192.png",
    "/icons/icon-512.png": "phone/icons/icon-512.png",
    "/icons/apple-touch-icon.png": "phone/icons/apple-touch-icon.png",
    "/icons/icon.svg": "phone/icons/icon.svg",
    "/fonts/geist.woff2": "fonts/geist.woff2",
    "/fonts/geist-mono.woff2": "fonts/geist-mono.woff2",
    "/fonts/doto.woff2": "fonts/doto.woff2",
    "/icons.svg": "icons.svg",
    "/theme.js": "theme.js",
    "/esc.js": "esc.js",
    "/meters.js": "meters.js",
}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".woff2": "font/woff2", ".webmanifest": "application/manifest+json"}
LASTING = (".png", ".svg", ".woff2")  # fonts and icons: cached a day; the rest revalidates
PAIR_PATHS = ("/phone/pair", "/phone/pair/status")
_ASK_KEY = re.compile(r"[A-Za-z0-9_-]{8,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
# Short answers that would approve or refuse a pending confirmation, beyond
# assistant.confirmation_words: those are answered on the PC, never from the phone.
_APPROVALS = re.compile(r"(?:jarvis[\s,]+)?(?:approve[d]?|reject|deny|allow|ok(?:ay)?|sure|yep|yeah|yup|"
                        r"nope|y|n)(?:\s+c?\d+)?", re.I)


def _log(text):
    """One line to the JARVIS log (stderr). Never handed a code, token or cookie."""
    print(f"JARVIS phone: {text}", file=sys.stderr, flush=True)


def _digest(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def clean_text(value, limit):
    """A display string from a device or a phone (names, user agents, hostnames): control
    and direction characters out, so it can't rewrite David's terminal; capped."""
    text = _CONTROL.sub("", str(value or "")).strip()
    return text[:limit]


def normalize_code(value):
    """What David typed on the phone -> the 8 code characters, or "" if it can't be one."""
    text = re.sub(r"[\s-]+", "", str(value or "")).upper()
    return text if len(text) == CODE_LEN and all(c in CODE_CHARS for c in text) else ""


def port_for(config):
    """config `phone.port` when it is a usable port, else 7787. Not a Settings field."""
    try:
        port = int(((config or {}).get("phone") or {}).get("port") or PORT)
    except (TypeError, ValueError):
        return PORT
    return port if 1024 <= port <= 65535 else PORT


# ---- the store: .jarvis/phone.json ------------------------------------------------------

def _shape(data):
    data = data if isinstance(data, dict) else {}
    data["on"] = data.get("on") is True
    data["phones"] = [p for p in data.get("phones") or [] if isinstance(p, dict) and p.get("id")]
    data["codes"] = [c for c in data.get("codes") or [] if isinstance(c, dict) and c.get("id")]
    return data


class Store:
    """.jarvis/phone.json: whether the phone link is on, the paired phones (token hashes
    only) and live pairing codes (salted hashes only). Re-read whenever the file changes,
    so a revoke or `phone off` from the CLI counts on the server's very next request."""

    def __init__(self, state, clock=time.time):
        self.state, self.clock = Path(state), clock
        self.path = self.state / STORE
        self._mu = threading.Lock()
        self._stamp, self._data = None, None

    def load(self):
        try:
            st = self.path.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)  # atomic replace = new inode
        except OSError:
            stamp = None
        with self._mu:
            if self._data is None or stamp != self._stamp:
                self._data = _shape(read_json(self.path, {}) if stamp else {})
                self._stamp = stamp
            return copy.deepcopy(self._data)

    def update(self, change):
        """change(data) edits a fresh copy in place and returns a result; the file is
        rewritten atomically under a file lock, so the CLI and the server never interleave."""
        private_dir(self.state)
        with exclusive_lock(self.state / LOCK, timeout=5):
            data = _shape(read_json(self.path, {}))
            result = change(data)
            atomic_write(self.path, json.dumps(data, indent=2), mode=0o600)
        with self._mu:
            self._data = None
        return result

    def on(self):
        return self.load()["on"]

    def set_on(self, flag):
        def change(data):
            data["on"] = bool(flag)
        self.update(change)
        return bool(flag)

    def remember_host(self, host, port):
        """This PC's tailnet name and the phone port, for actions.py's browser block list
        (it must never look them up itself). Written only when they change."""
        data = self.load()
        if host and (data.get("host") != host or data.get("port") != port):
            def change(data):
                data["host"], data["port"] = host, port
            self.update(change)

    def new_code(self, name=None, allow_desktop=False):
        """A fresh pairing code -> (code, code id). Only the salted hash is stored."""
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        salt = secrets.token_hex(16)
        now = self.clock()
        entry = {"id": secrets.token_hex(6), "salt": salt, "sha256": _digest(salt + code),
                 "created": now, "expires": now + CODE_TTL, "state": "waiting",
                 "name": clean_text(name, NAME_MAX) or None, "allow_desktop": bool(allow_desktop),
                 "request": None}

        def change(data):
            _purge(data, now)
            data["codes"].append(entry)
        self.update(change)
        return code, entry["id"]

    def code(self, code_id):
        return next((c for c in self.load()["codes"] if c["id"] == code_id), None)

    def _set_code(self, code_id, state):
        now = self.clock()

        def change(data):
            entry = next((c for c in data["codes"] if c["id"] == code_id), None)
            if not entry or entry["expires"] <= now or entry["state"] != "pending":
                return False
            entry["state"] = state
            if state == "approved":
                entry["expires"] = max(entry["expires"], now + APPROVED_GRACE)
            return True
        return self.update(change)

    def approve(self, code_id):
        return self._set_code(code_id, "approved")

    def deny(self, code_id):
        return self._set_code(code_id, "denied")

    def burn(self, code_id):
        def change(data):
            data["codes"] = [c for c in data["codes"] if c["id"] != code_id]
        self.update(change)

    def revoke(self, which):
        """Remove one phone (by id) or "all" -> the removed phones."""
        def change(data):
            gone = [p for p in data["phones"] if which == "all" or p["id"] == which]
            data["phones"] = [p for p in data["phones"] if p not in gone]
            return gone
        return self.update(change)


def _purge(data, now):
    data["codes"] = [c for c in data["codes"] if c.get("expires", 0) > now]


def _match_code(data, code, now):
    for entry in data["codes"]:
        if entry.get("expires", 0) > now and hmac.compare_digest(
                str(entry.get("sha256", "")), _digest(str(entry.get("salt", "")) + code)):
            return entry
    return None


def _pair_step(data, code, who, name, ua, mobile, claim, now):
    """One pairing request against the stored codes (run under the store's lock) ->
    (outcome, detail). A desktop is turned away before any code is looked at unless some
    live code allows desktops, so a refused source never spends anyone's attempts."""
    _purge(data, now)
    if not mobile and not any(c.get("allow_desktop") for c in data["codes"]):
        return "desktop", None
    entry = _match_code(data, code, now)
    if entry is None:
        return "bad", None
    if not mobile and not entry.get("allow_desktop"):
        return "desktop", None
    request = entry.get("request") or {}
    if entry["state"] == "waiting":
        if not claim:
            return "unclaimed", None
        entry["state"] = "pending"
        entry["request"] = {"stable_id": who["stable_id"], "node": who["node"], "os": who["os"],
                            "login": who["login"], "name": entry.get("name") or name or who["node"] or "Phone",
                            "ua": ua, "at": now}
        return "pending", None
    if request.get("stable_id") != who["stable_id"]:
        return "taken", None
    if entry["state"] == "pending":
        return "pending", None
    if entry["state"] == "denied":
        return "denied", None
    if entry["state"] != "approved":
        return "bad", None
    token = secrets.token_urlsafe(32)
    ids = {p["id"] for p in data["phones"]}
    phone_id = next(i for i in iter(lambda: secrets.token_hex(4), None) if i not in ids)
    phone = {"id": phone_id, "name": request.get("name") or "Phone", "token_sha256": _digest(token),
             "stable_id": who["stable_id"], "node": request.get("node"), "os": request.get("os"),
             "login": request.get("login"), "ua": request.get("ua"), "created": now, "last_seen": now}
    data["phones"].append(phone)
    data["codes"] = [c for c in data["codes"] if c is not entry]
    return "paired", (phone, token)


def known_origins(state):
    """https://<this PC's tailnet name> (and the loopback origin of a non-default phone
    port), as the phone server last recorded them in .jarvis/phone.json. File read only:
    actions.py calls this while building browser block lists."""
    data = read_json(Path(state) / STORE, {})
    if not isinstance(data, dict):
        return []
    out = []
    host = str(data.get("host") or "")
    if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        out.append(f"https://{host}")
    port = data.get("port")
    if isinstance(port, int) and port != PORT and 1024 <= port <= 65535:
        out += [f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")]
    return out


# ---- Tailscale ---------------------------------------------------------------------------

def _run_tailscale(args, timeout=5):
    try:
        out = run_quiet(["tailscale", *args], timeout)
        return out.returncode, out.stdout or ""
    except Exception:  # not installed, hung: "can't tell", which every caller fails closed on
        return 127, ""


def _json(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _proxies_to(target, port):
    """A serve handler target (http://127.0.0.1:7787, 127.0.0.1:7787, 7787) that is `port`."""
    return bool(re.search(rf"(?:^|:){port}(?:/|$)", str(target or "").strip()))


def _serve_parts(config):
    """Every serve config in a ServeConfig (foreground sessions and services nest more)."""
    yield config
    for key in ("Foreground", "Services"):
        for sub in (config.get(key) or {}).values() if isinstance(config.get(key), dict) else ():
            if isinstance(sub, dict):
                yield from _serve_parts(sub)


def serve_targets(config, port):
    """(serve points at `port`, funnel publishes `port`) from `tailscale serve status --json`."""
    funnel, web, tcp = set(), {}, {}
    for part in _serve_parts(config):
        funnel |= {str(hp) for hp, on in (part.get("AllowFunnel") or {}).items() if on}
        for hp, site in (part.get("Web") or {}).items():
            handlers = (site or {}).get("Handlers") or {}
            web.setdefault(str(hp), []).extend(h.get("Proxy") for h in handlers.values()
                                               if isinstance(h, dict))
        for listen, handler in (part.get("TCP") or {}).items():
            tcp.setdefault(str(listen), []).append((handler or {}).get("TCPForward"))
    here = lambda targets: any(_proxies_to(t, port) for t in targets)  # noqa: E731
    serving = any(here(t) for t in web.values()) or any(here(t) for t in tcp.values())
    published = any(here(web.get(hp, [])) or here(tcp.get(hp.rsplit(":", 1)[-1], []))
                    for hp in funnel)
    return serving, published


class Tailnet:
    """What Tailscale knows: this PC's tailnet name and node, who a tailnet address belongs
    to, and whether Serve/Funnel publish the phone port. `run(args, timeout)` ->
    (returncode, stdout) is injectable: tests never start the real CLI."""

    def __init__(self, run=None, clock=time.monotonic):
        self.run, self.clock = run or _run_tailscale, clock
        self._mu = threading.Lock()
        self._status = (None, None)  # (read at, status)
        self._whois = {}  # ip -> (read at, answer)

    def status(self):
        with self._mu:
            at, data = self._status
            # A failed read is retried after FAILED_TTL, not on every request: a hung CLI
            # must not cost each request five seconds.
            if at is not None and self.clock() - at < (TAILNET_TTL if data is not None else FAILED_TTL):
                return data
        code, out = self.run(["status", "--json"], 5)
        data = _json(out) if code == 0 else None
        with self._mu:
            self._status = (self.clock(), data)
        return data

    def dns_name(self):
        name = str(((self.status() or {}).get("Self") or {}).get("DNSName") or "")
        return name.rstrip(".").lower() or None

    def self_node(self):
        me = (self.status() or {}).get("Self") or {}
        if not me.get("ID"):
            return None
        return {"id": str(me["ID"]), "ips": [str(ip) for ip in me.get("TailscaleIPs") or []]}

    def peer_ip(self, hostname):
        """The tailnet IPv4 of the peer whose HostName is `hostname`, or None."""
        want = str(hostname or "").casefold()
        status = self.status() or {}
        peers = [status.get("Self") or {}] + list((status.get("Peer") or {}).values())
        for peer in peers:
            if want and str(peer.get("HostName") or "").casefold() == want:
                return next((ip for ip in peer.get("TailscaleIPs") or [] if "." in str(ip)), None)
        return None

    def whois(self, ip):
        """{stable_id, os, hostname, name, node, login, addresses} for a tailnet address, or None."""
        now = self.clock()
        with self._mu:
            found = self._whois.get(ip)
            if found and now - found[0] < TAILNET_TTL:
                return found[1]
        code, out = self.run(["whois", "--json", ip], 5)
        data = _json(out) if code == 0 else None
        answer = _who(data) if data else None
        if answer:
            with self._mu:
                if len(self._whois) > 256:
                    self._whois.clear()
                self._whois[ip] = (now, answer)
        return answer

    def serve_config(self):
        """`tailscale serve status --json` as a dict, or None when it can't be read."""
        code, out = self.run(["serve", "status", "--json"], 5)
        if code != 0:
            return None
        if not out.strip():
            return {}
        return _json(out)


def _who(data):
    node, profile = data.get("Node") or {}, data.get("UserProfile") or {}
    info = node.get("Hostinfo") or {}
    name = str(node.get("Name") or "").rstrip(".")
    stable = str(node.get("StableID") or "")
    if not stable:
        return None
    return {"stable_id": stable, "os": clean_text(info.get("OS"), 40),
            "hostname": clean_text(info.get("Hostname"), 80), "name": name.lower(),
            "node": clean_text(name.split(".")[0] or info.get("Hostname"), 80),
            "login": clean_text(profile.get("LoginName"), 120),
            "addresses": [str(a).split("/")[0] for a in node.get("Addresses") or []]}


def tailnet_ip(value):
    """The last X-Forwarded-For hop as a tailnet address string, or None."""
    hop = str(value or "").split(",")[-1].strip()
    try:
        address = ipaddress.ip_address(hop)
    except ValueError:
        return None
    return str(address) if any(address in net for net in TAILNET) else None


def cookie_values(headers):
    out = []
    for header in headers.get_all("Cookie") or []:
        for part in str(header).split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == COOKIE and value:
                out.append(value.strip())
    return out


def set_cookie(token, age=COOKIE_AGE):
    return f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}"


# ---- rate limits -------------------------------------------------------------------------

class Limiter:
    """Wrong pairing codes per source (tailnet address, then tailnet node): BAD_CODES per
    BAD_WINDOW. Counted per source, never globally, so nobody can lock David out."""

    def __init__(self, clock, limit=BAD_CODES, window=BAD_WINDOW):
        self.clock, self.limit, self.window = clock, limit, window
        self._mu, self._hits = threading.Lock(), {}

    def _recent(self, key):
        now = self.clock()
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key):
        with self._mu:
            return len(self._recent(key)) >= self.limit

    def hit(self, key):
        with self._mu:
            if len(self._hits) > 1024:
                self._hits.clear()
            self._hits[key] = self._recent(key) + [self.clock()]


# ---- the server --------------------------------------------------------------------------

class Reply(Exception):
    """An answer decided early (a refusal, usually): status, JSON body, extra headers."""

    def __init__(self, status, body, headers=None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


def _refuse(status, text, **extra):
    return Reply(status, {"error": text, **extra})


def _status_of(exc):
    """HTTP status for an error from the hub or a worker module."""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status < 600:
        return status
    if type(exc).__name__ == "Ended":  # remote_view.Ended: the app stops instead of retrying
        return 410
    if type(exc).__name__ in ("Busy", "SessionBusy", "Conflict", "WrongKind"):
        return 409
    if isinstance(exc, LookupError):
        return 404
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, ValueError):
        return 400
    if isinstance(exc, TimeoutError):
        return 504
    return 502


def _hub_error(exc):
    status = _status_of(exc)
    if status == 404:  # the same answer for "no such session" and "not yours": no oracle
        return _refuse(404, "No such session.")
    if isinstance(exc, (ValueError, RuntimeError, OSError, LookupError)) or status != 502:
        return _refuse(status, clean_text(exc, 300) or "The computer refused that.")
    return _refuse(502, "Something went wrong on the PC.")


class PhoneHTTPServer(ThreadingHTTPServer):
    """At most `slots` connections at once (each pins a thread): any tailnet device could
    otherwise exhaust the main's threads. Over the cap: an immediate 503."""
    daemon_threads = True
    request_queue_size = 32
    _BUSY_BODY = b'{"error": "JARVIS is busy; try again."}'
    BUSY = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\n"
            b"X-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\n"
            b"Connection: close\r\n\r\n" + _BUSY_BODY)

    def __init__(self, address, handler, slots=MAX_CONNECTIONS):
        self.slots = threading.BoundedSemaphore(slots)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(self.BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError,
                                           ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# ---- Ask's live decision tree --------------------------------------------------------------
# The console already announces every step of a request (events.Turn.step): recall, route,
# brain, tools, plan. A Trace keeps those per job so the phone can draw them while the request
# runs. It never invents a step and shows only what is safe on a phone: labels and short
# summaries, never a tool's raw output (which can hold file text or secrets).
_SECRET = re.compile(
    r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza|ya29)[\w\-]{10,}"
    r"|\b(?:bearer|token|secret|password|passwd|api[_-]?key|authorization|cookie)\b\s*[:=]?\s*\S+"
    r"|[A-Za-z0-9+/_\-]{32,}={0,2}")
_URL_TAIL = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_URL_AUTH = re.compile(r"(\w+://)[^/\s@]+@")
_PATH = re.compile(r"(?:~|/)(?:[\w.\-@+]+/){2,}([\w.\-@+]+)")
_TOOL_NAME = re.compile(r"[^\w .:/\-]")
# Nodes whose short summary is about the request, not about the machine: kept when not failed.
_SAFE_DETAIL = frozenset({"recall", "route", "instant", "judge", "task", "adapter", "grok-check",
                          "grok-send", "plan", "combine", "standin", "brain"})


def redact(value, limit):
    """Display text for the phone: secrets, credentialed or query-carrying URLs and deep local
    paths replaced, control and direction characters out, capped."""
    text = _URL_AUTH.sub(r"\1", str(value or ""))
    text = _URL_TAIL.sub(r"\1", text)
    text = _PATH.sub(lambda m: "…/" + m.group(1), text)
    text = _SECRET.sub("[hidden]", text)
    return clean_text(text, limit)


def project_step(step):
    """One console step -> what the phone may see, or None when it is not a step."""
    sid, state = str(step.get("id") or "")[:48], step.get("state")
    if not sid:
        return None
    state = state if state in ("run", "done", "fail") else "done"
    parent = str(step.get("parent") or "root")[:48]
    tool = sid.startswith("tool:")
    label = _TOOL_NAME.sub("", redact(step.get("label"), 80)) if tool else redact(step.get("label"), 80)
    if not label.strip():
        label = "Tool" if tool else "Step"
    base = sid.split(":", 1)[0]
    keep = state != "fail" and (base in _SAFE_DETAIL or sid.startswith("plan:"))
    detail = redact(step.get("detail"), 120) if keep else ""
    return {"id": sid, "parent": parent, "label": label[:80], "detail": detail, "state": state,
            "kind": "tool" if tool else "step"}


def request_usage(raw):
    """Only the numeric per-request meter summary, never provider payloads or credentials."""
    import math
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in ("tokens", "usd", "calls", "input", "cached", "output"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1e15:
            out[key] = value
    if not out:
        return None
    out["engine"] = redact(raw.get("engine"), 60)
    return out


class Trace:
    """Steps of the console's recent requests, fed from its event bus. Ownership is not decided
    here: PhoneServer only reads a job's trace after it has checked the job is that phone's."""

    def __init__(self):
        self._mu = threading.Lock()
        self._jobs = {}  # job id -> record, oldest first

    def _record(self, job):
        rec = self._jobs.get(job)
        if rec is None:
            rec = self._jobs[job] = {"steps": {}, "rev": 0, "tier": None, "reasons": [],
                                     "streaming": False, "end": None}
            while len(self._jobs) > TRACE_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
        return rec

    def feed(self, kind, data):
        job = data.get("job") if isinstance(data, dict) else None
        if not isinstance(job, str) or not kind.startswith("turn.") or data.get("channel") != "console":
            return
        with self._mu:
            rec = self._record(job)
            if kind == "turn.start":
                rec["tier"] = redact(data.get("tier"), 12) or None
                reasons = data.get("reasons")
                rec["reasons"] = [redact(r, 80) for r in reasons[:4]] if isinstance(reasons, list) else []
            elif kind == "turn.step":
                step = project_step(data)
                if step is None or (step["id"] not in rec["steps"] and len(rec["steps"]) >= TRACE_STEPS):
                    return
                rec["steps"][step["id"]] = step
            elif kind == "turn.sentence":
                if rec["streaming"]:
                    return
                rec["streaming"] = True
            elif kind in ("turn.done", "turn.error"):
                rec["end"] = "done" if kind == "turn.done" else "error"
            else:
                return
            rec["rev"] += 1

    def rev(self, job):
        with self._mu:
            rec = self._jobs.get(job)
            return rec["rev"] if rec else 0

    def snapshot(self, job):
        """{"rev", "tier", "reasons", "streaming", "steps"} or None when nothing was seen."""
        with self._mu:
            rec = self._jobs.get(job)
            if rec is None:
                return None
            steps = [dict(s) for s in rec["steps"].values()]
            return {"rev": rec["rev"], "tier": rec["tier"], "reasons": list(rec["reasons"]),
                    "streaming": rec["streaming"], "steps": steps}


class PhoneServer:
    """The phone app's server, inside the main's runtime (cluster_proxy.local_server).
    console: ui.Console (Ask, events, remote_status); hub: cluster.Hub (sessions)."""

    def __init__(self, config, state, console=None, hub=None, port=None, tailnet=None, web=None,
                 clock=time.time, slots=MAX_CONNECTIONS):
        self.config, self.state = config or {}, Path(state)
        self.console, self.hub = console, hub
        self.port = port_for(self.config) if port is None else port
        self.tailnet = tailnet or Tailnet()
        self.web = Path(web or WEB)
        self.clock, self.slots = clock, slots
        self.store = Store(self.state, clock)
        self.limiter = Limiter(clock)
        self.httpd = self.thread = None
        self.poll = 0.5  # s serve_forever sleeps between shutdown checks
        self._stop = threading.Event()
        self._mu = threading.Lock()
        self._frames = {}  # phone id -> Lock: one frame long-poll in flight per phone
        self._mine = {}  # session id -> {"phone", "device", "kind"}: for the end-of-session events
        self._known = {p["id"]: p.get("name") for p in self.store.load()["phones"]}
        self._funnel = (None, None)  # (checked at, refusal text or None)
        self._funnel_mu = threading.Lock()
        self._version = (None, "")
        self.trace = Trace()
        self._keys = {}  # (phone id, Ask key) -> (job id, text), oldest first
        # Gateway phones are not rows in phone.json and not Tailscale identities.
        self._gateway_phones = {}  # phone id -> {"id", "name", "via"}
        self._gateway_ops = {}  # operation id -> outcome record; the request body is not kept

    # ---- lifecycle
    def start(self):
        handler = type("PhoneHandler", (PhoneHandler,), {"phone": self})
        self.httpd = PhoneHTTPServer(("127.0.0.1", self.port), handler, self.slots)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(self.poll,),
                                       name="jarvis-phone", daemon=True)
        self.thread.start()
        threading.Thread(target=self._watch, name="jarvis-phone-watch", daemon=True).start()
        self._follow_console()
        try:
            self.store.remember_host(self.tailnet.dns_name(), self.port)
        except (OSError, ValueError):
            pass
        why = self.refusal()
        _log(f"listening on 127.0.0.1:{self.port}" + (f" but refusing: {why}" if why else ""))
        return self

    def close(self):
        self._stop.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def _follow_console(self):
        """Feed the trace from the console's bus. A console with no bus leaves Ask without a tree."""
        bus = getattr(self.console, "bus", None)
        subscribe = getattr(bus, "subscribe", None)
        if not callable(subscribe):
            return
        threading.Thread(target=self._trace_loop, args=(bus,), name="jarvis-phone-trace", daemon=True).start()

    def _trace_loop(self, bus):
        last = None
        while not self._stop.is_set():
            try:
                sub, backlog = bus.subscribe(last)
            except Exception:
                return
            try:
                pending = list(backlog)
                while not self._stop.is_set():
                    if pending:
                        event = pending.pop(0)
                    else:
                        try:
                            event = sub.get(timeout=0.25)
                        except queue.Empty:
                            continue
                    if event is None:  # the bus dropped us (queue full): resubscribe from where we were
                        break
                    last = event.get("seq", last)
                    try:
                        self.trace.feed(event.get("kind", ""), event.get("data"))
                    except Exception:  # one odd event must not end the tree for every request
                        pass
            finally:
                try:
                    bus.unsubscribe(sub)
                except Exception:
                    pass

    def _watch(self):
        while not self._stop.wait(WATCH_EVERY):
            try:
                self.sync_phones()
            except Exception:  # a bad read now is retried on the next tick
                pass

    def sync_phones(self):
        """A phone gone from phone.json (CLI revoke, logout): end its sessions here."""
        now = {p["id"]: p.get("name") for p in self.store.load()["phones"]}
        with self._mu:
            gone = {pid: name for pid, name in self._known.items() if pid not in now}
            self._known = now
            for sid in [s for s, m in self._mine.items() if m["phone"] in gone]:
                self._mine.pop(sid, None)
        for pid, name in gone.items():
            end = getattr(self.hub, "end_opener", None)
            if end:
                try:
                    end(f"phone:{pid}")
                except Exception as exc:
                    _log(f"couldn't end a removed phone's sessions: {clean_text(exc, 200)}")
            self.publish("phone.revoked", {"name": name})
        return list(gone)

    def publish(self, kind, data):
        bus = getattr(self.console, "bus", None)
        if bus is not None:
            try:
                bus.publish(kind, data)
            except Exception:
                pass

    # ---- the checks every request passes
    def refusal(self):
        """Why the whole server says no right now (503), or None: the phone link is off, or
        Funnel (the public internet) reaches this port, or the serve config can't be read."""
        if not self.store.on():
            return "JARVIS's phone link is off. Turn it on at the PC: jarvis phone on"
        with self._funnel_mu:
            at, why = self._funnel
            if at is None or self.clock() - at >= FUNNEL_TTL or at > self.clock():
                config = self.tailnet.serve_config()
                if config is None:
                    why = "JARVIS can't read Tailscale's serve settings, so the phone link stays shut."
                else:
                    try:
                        published = serve_targets(config, self.port)[1]
                    except (AttributeError, TypeError, ValueError):
                        published = True  # a shape this check can't read: assume the worst
                    why = ("Tailscale Funnel publishes JARVIS to the internet; the phone link "
                           "stays shut until funnel is off." if published else None)
                    if published:
                        _log("refusing: Tailscale Funnel reaches the phone port")
                self._funnel = (self.clock(), why)
        return why

    def host_ok(self, header):
        host = str(header or "").strip().lower()
        if host.endswith(".") and ":" not in host:
            host = host[:-1]
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        name = self.tailnet.dns_name()
        if name:
            allowed |= {name, f"{name}:443"}
        return host in allowed

    def identify(self, headers, ip=None):
        """The tailnet device and login behind a request (checks 2 and 3), or a Reply."""
        ip = ip or tailnet_ip(headers.get("X-Forwarded-For"))
        if ip is None:
            raise _refuse(401, NO_ADDRESS, pair=False)
        who = self.tailnet.whois(ip)
        if not who:
            raise _refuse(401, "Tailscale couldn't say which device sent this.", pair=False)
        login = str(headers.get("Tailscale-User-Login") or "").strip()
        if not login or not who["login"] or login.casefold() != who["login"].casefold():
            raise _refuse(403, "That Tailscale login doesn't match this device.")
        return who

    def phone_for(self, headers, who):
        """The paired phone this request's cookie belongs to (check 6), or a 401 {pair:true}."""
        tokens = cookie_values(headers)
        if not tokens:
            raise _refuse(401, "This phone isn't paired with JARVIS.", pair=True)
        phones = self.store.load()["phones"]
        for token in tokens[:4]:
            digest = _digest(token)
            for phone in phones:
                if hmac.compare_digest(str(phone.get("token_sha256", "")), digest) and \
                        phone.get("stable_id") == who["stable_id"] and \
                        str(phone.get("login", "")).casefold() == who["login"].casefold():
                    self._seen(phone)
                    return phone
        raise _refuse(401, "This phone isn't paired with JARVIS (or was removed).", pair=True)

    def _seen(self, phone):
        now = self.clock()
        if now - float(phone.get("last_seen") or 0) < SEEN_EVERY:
            return

        def change(data):
            for p in data["phones"]:
                if p["id"] == phone["id"]:
                    p["last_seen"] = now
        try:
            self.store.update(change)
        except (OSError, ValueError):  # busy lock: next minute's request writes it
            pass

    # ---- pairing
    def device_refusal(self, who):
        """Why this tailnet device may never pair (it is the main or a cluster computer), or None."""
        me = self.tailnet.self_node()
        if me is None:
            return "JARVIS couldn't read this PC's Tailscale status; try again."
        if who["stable_id"] == me["id"] or set(who["addresses"]) & set(me["ips"]):
            return "That's the main PC itself. Pair from your phone."
        names, ips = set(), set()
        for word in self._cluster_words():
            try:
                ips.add(str(ipaddress.ip_address(word)))
            except ValueError:
                names |= {word, word.split(".")[0]}
        mine = {who["hostname"].casefold(), who["name"], who["node"].casefold()} - {""}
        if mine & names or set(who["addresses"]) & ips:
            return "That's one of your JARVIS computers. Pair from your phone."
        return None

    def _cluster_words(self):
        """Hostnames and addresses the config and the hub know for every cluster device."""
        words = []
        for device in (self.config.get("devices") or {}).values():
            if not isinstance(device, dict):
                continue
            for key in ("hostname", "target", "lan_ip", "tailscale_ip", "host", "ip"):
                words.append(device.get(key))
            words += list(device.get("fallback_targets") or [])
        broker = getattr(self.hub, "broker", None)
        if broker is not None:
            try:
                words += [n.get("hostname") for n in broker.nodes()]
            except Exception:
                pass
        out = set()
        for word in words:
            text = str(word or "").strip().lower().rsplit("@", 1)[-1]  # user@host:22
            if text.startswith("["):
                text = text[1:].split("]")[0]
            elif text.count(":") == 1:
                text = text.split(":")[0]
            text = text.rstrip(".")
            if text:
                out.add(text)
        return out

    def pair(self, body, who, ip, ua, claim):
        code = normalize_code(body.get("code"))
        if not code:
            raise _refuse(400, "Type the 8-character code shown on your PC.")
        keys = ("ip:" + ip, "node:" + who["stable_id"])
        if any(self.limiter.blocked(k) for k in keys):
            raise _refuse(429, "Too many wrong codes. Wait ten minutes, then make a new one on your PC.")
        why = self.device_refusal(who)
        if why:
            raise _refuse(403, why)
        mobile = who["os"].casefold() in MOBILE_OS
        name = clean_text(body.get("name"), NAME_MAX)
        now = self.clock()
        outcome, detail = self.store.update(
            lambda data: _pair_step(data, code, who, name, ua, mobile, claim, now))
        if outcome == "desktop":
            raise _refuse(403, "Only a phone can pair with JARVIS.")
        if outcome == "bad":
            for key in keys:
                self.limiter.hit(key)
            raise _refuse(403, "That code is wrong or has expired. Make a new one on your PC.")
        if outcome == "unclaimed":
            raise _refuse(400, "Send the code first.")
        if outcome == "taken":
            raise _refuse(409, "Another device is already using that code.")
        if outcome == "denied":
            raise _refuse(403, "Pairing was refused on the PC.")
        if outcome == "pending":
            raise Reply(202, {"pending": True})
        phone, token = detail
        with self._mu:
            self._known[phone["id"]] = phone["name"]
        self.publish("phone.paired", {"name": phone["name"]})
        return Reply(200, {"ok": True, "phone": {"id": phone["id"], "name": phone["name"]}},
                     {"Set-Cookie": set_cookie(token)})

    # ---- what a paired phone may do
    def _hub(self):
        if self.hub is None:
            raise _refuse(503, "Remote control needs JARVIS's main runtime.")
        return self.hub

    def main_id(self):
        return (self.config.get("cluster") or {}).get("main_device")

    def _label(self, device):
        found = (self.config.get("devices") or {}).get(device) or {}
        return str(found.get("label") or device)

    def _device(self, value):
        if not isinstance(value, str) or value not in (self.config.get("devices") or {}):
            raise _refuse(400, "Pick one of your computers.")
        return value

    def me(self, phone):
        main = self.main_id()
        return {"phone": {"id": phone["id"], "name": phone.get("name"), "created": phone.get("created")},
                "host": self.tailnet.dns_name(), "main": {"id": main, "label": self._label(main) if main else None}}

    def overview(self, phone):
        return {"devices": self.devices(), "sessions": self.sessions(phone),
                "ask": {"enabled": self.console is not None}}

    def fleet(self, phone):
        """Fleet status and onboarding guidance, read-only: no addresses, hostnames, invite
        state or secrets, and every write action is reported `blocked` (a paired phone is not
        fleet admin; that needs the future fresh-owner-proof step)."""
        from . import fleet_management
        return fleet_management.inventory(self.config, self.state, audience="phone", now=self.clock())

    def devices(self):
        main = self.main_id()
        try:
            listed = {d["id"]: d for d in (self.console.remote_status() or {}).get("devices", [])} \
                if self.console is not None else {}
        except Exception:
            listed = {}
        nodes = {}
        broker = getattr(self.hub, "broker", None)
        if broker is not None:
            try:
                nodes = {n["id"]: n for n in broker.nodes()}
            except Exception:
                pass
        from . import hostmeter
        from .fsio import read_json
        out = []
        for name, device in (self.config.get("devices") or {}).items():
            if not isinstance(device, dict):
                continue
            node, shown = nodes.get(name) or {}, listed.get(name) or {}
            role = "main" if name == main else "node"
            caps, why = self._caps(name, role, shown)
            if role == "main":
                from .platforms import label
                platform, online, seen = label(), True, self.clock()
            else:
                platform = node.get("platform") or shown.get("platform") or device.get("platform")
                # Proof only: the broker's own check-in window. The console's `shown.online` also counts a
                # node that is mid-job (hub.alive), which is "busy", not proved online; used only when the
                # broker has no row for it.
                online = bool(node.get("online")) if node else bool(shown.get("online"))
                seen = node.get("seen")
            network = {k: v for k, v in {
                "lan_ip": device.get("lan_ip"),
                "tailscale_ip": self.tailnet.peer_ip(device.get("hostname"))}.items() if v}
            # One liveness word for every surface (connections.presence): a node mid-job that last
            # checked in over 75 s ago is "busy", awaiting contact: not proved online, not offline.
            live = None
            if role == "main":
                presence = live = "online"
            elif node or shown:
                from .connections import presence as _presence
                busy = False
                if broker is not None and node and not online:
                    try:
                        busy = bool(broker.busy_job(name))
                    except Exception:
                        busy = False
                live = "online" if online else _presence(seen, busy, self.clock())
                presence = None if live == "busy" else live  # meters: no "Offline" claim while it works
            else:
                presence = None
            inv = read_json(self.state / "inventory" / f"{name}.json", {}) or {}
            out.append({"id": name, "label": str(device.get("label") or name), "platform": platform,
                        "role": role, "online": online, "presence": live, "seen": seen, "caps": caps, "why": why,
                        "network": network,
                        "meters": hostmeter.summarize(device, inv, node, here=(role == "main"),
                                                      presence=presence, now=self.clock())})
        return out

    def _caps(self, device, role, shown):
        """({remote, camera}: bool, {the same: why not}) from hub.target_caps."""
        found = getattr(self.hub, "target_caps", None)
        raw = {}
        if found is not None:
            try:
                raw = found(device) or {}
            except Exception as exc:
                raw = {k: (False, clean_text(exc, 200)) for k in ("remote", "camera")}
        elif role == "node" and shown:
            raw = {"remote": (bool(shown.get("ready")), shown.get("why"))}
        caps, why = {}, {}
        for key in ("remote", "camera"):
            value = raw.get(key, (False, "This JARVIS version can't do that yet."))
            ok, text = (value[0], value[1] if len(value) > 1 else None) \
                if isinstance(value, (tuple, list)) else (value, None)
            caps[key] = bool(ok)
            if not ok:
                why[key] = clean_text(text, 300) or "Not available."
        return caps, why

    def _opener(self, phone):
        return f"phone:{phone['id']}"

    def sessions(self, phone):
        """This phone's sessions as the hub knows them (never the console's or another
        phone's). One that ended or was forgotten since says so in the events once."""
        if self.hub is None:
            return []
        views = [dict(v) for v in self.hub.sessions(self._opener(phone)) or []]
        live = {v.get("id") for v in views if v.get("state") != "ended"}
        with self._mu:
            gone = [sid for sid, m in self._mine.items() if m["phone"] == phone["id"] and sid not in live]
        for sid in gone:
            self._ended(sid)
        return views

    def _ended(self, sid, state="ended"):
        with self._mu:
            meta = self._mine.pop(sid, None)
            name = self._known.get(meta["phone"]) if meta else None
        if meta:
            self.publish("phone.session", {"name": name, "device": meta["device"],
                                           "kind": meta["kind"], "state": state})
            if meta["kind"] == "camera":
                self.publish("camera.off", {"device": meta["device"]})

    def start_session(self, phone, body):
        device = self._device(body.get("device"))
        kind = body.get("kind") or "screen"
        quality = body.get("quality") or "low"
        if kind not in KINDS:
            raise _refuse(400, "kind must be screen or camera.")
        if quality not in QUALITIES:
            raise _refuse(400, "quality must be low, medium or high.")
        hub = self._hub()
        try:
            view = hub.remote_open(device, quality, kind=kind, opener=self._opener(phone),
                                   allow_main=device == self.main_id())
        except Reply:
            raise
        except Exception as exc:
            raise _hub_error(exc) from None
        view = dict(view or {})
        sid = view.get("id")
        if sid:
            with self._mu:
                self._mine[sid] = {"phone": phone["id"], "device": device, "kind": kind}
        self.publish("phone.session", {"name": phone.get("name"), "device": device, "kind": kind,
                                       "state": view.get("state")})
        if kind == "camera":
            self.publish("camera.on", {"device": device, "by": phone.get("name")})
        return view

    def frame(self, phone, query):
        """(seq, jpeg, width, height), None (unchanged: 204), or a Reply. One per phone."""
        sid = (query.get("session") or [""])[0]
        try:
            after = int((query.get("after") or ["0"])[0] or 0)
        except ValueError:
            raise _refuse(400, "after must be a frame number.") from None
        hub = self._hub()
        lock = self._frames.setdefault(phone["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            raise _refuse(429, "One picture request at a time.")
        try:
            return hub.session_frame(sid, after, self._opener(phone))
        except Exception as exc:
            if type(exc).__name__ == "Ended":
                self._ended(sid)
                raise _refuse(410, clean_text(exc, 300) or "The session ended.") from None
            raise _hub_error(exc) from None
        finally:
            lock.release()

    def node_ago(self, phone, sid):
        """Seconds since the target last checked in, for a 204's X-Node-Ago; None if unknown."""
        try:
            return float(self.hub.session_node_ago(sid, self._opener(phone)))
        except Exception:
            return None

    def session_input(self, phone, body):
        events = body.get("events")
        if not isinstance(events, list) or len(events) > 200:
            raise _refuse(400, "events must be a list of at most 200.")
        try:
            queued = self._hub().session_input(body.get("session"), events, self._opener(phone))
        except Reply:
            raise
        except Exception as exc:
            raise _hub_error(exc) from None
        if isinstance(queued, dict):
            return {"ok": True, **queued}
        return {"ok": True, "queued": queued}

    def stop_session(self, phone, body):
        sid = body.get("session")
        try:
            view = self._hub().remote_close(sid, opener=self._opener(phone))
        except Reply:
            raise
        except Exception as exc:
            raise _hub_error(exc) from None
        self._ended(sid)
        return {"ok": True, "session": view}

    def ask(self, phone, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _refuse(400, "Type something for JARVIS to do.")
        if len(text) > ASK_MAX:
            raise _refuse(400, f"Keep it under {ASK_MAX} characters.")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()
        from .assistant import confirmation_words
        words = text.lower().replace("’", "'").rstrip(".!?").strip()
        if confirmation_words(text) or _APPROVALS.fullmatch(words):
            raise _refuse(400, "Approve on your PC.")
        if self.console is None:
            raise _refuse(503, "Ask needs JARVIS's main runtime.")
        target = body.get("device")
        if target is not None:
            if not isinstance(target, str) or target not in self.config.get("devices", {}):
                raise _refuse(400, "Choose a known computer.")
            if self.hub is None:
                raise _refuse(503, "The computer link is unavailable.")
            main = self.hub.main
            if target != main:
                caps = self.hub.target_caps(target)
                ok, why = caps.get("remote", (False, "Desktop actions unavailable."))
                if not ok:
                    raise _refuse(409, why or "That computer is unavailable.")
        else:
            main = None
        key = body.get("key")
        key = (phone["id"], target, key) if isinstance(key, str) and _ASK_KEY.fullmatch(key) else None
        if key:  # a retry after a lost reply: the same request, not a second one
            with self._mu:
                seen = self._keys.get(key)
            if seen and seen[1] == text:
                try:
                    return self._answer(self.console.job(seen[0]), ASK_WAIT)
                except LookupError:
                    pass
        from .topology import request_from
        try:
            with request_from(None):  # the main's own request: confirmations park on the PC
                job = (self.console.submit(text, origin_device=target, approval_device=main)
                       if target is not None else self.console.submit(text, origin_device=None))
        except ValueError as exc:
            if "still on the last request" in str(exc):
                raise _refuse(409, "JARVIS is still on the last request.") from None
            raise _refuse(400, clean_text(exc, 300)) from None
        job["via"] = self._opener(phone)
        if key:
            with self._mu:
                self._keys[key] = (job["id"], text)
                while len(self._keys) > ASK_KEYS:
                    self._keys.pop(next(iter(self._keys)))
        return self._answer(job, ASK_WAIT)

    def ask_job(self, phone, job_id, query=None):
        if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or "") or self.console is None:
            raise _refuse(404, "No such request.")
        try:
            job = self.console.job(job_id)
        except LookupError:
            raise _refuse(404, "No such request.") from None
        if job.get("via") != self._opener(phone):
            raise _refuse(404, "No such request.")
        after = (query or {}).get("after", [""])[0]
        return self._answer(job, ASK_POLL_WAIT, int(after) if re.fullmatch(r"\d{1,9}", after) else None)

    def _trace_of(self, job):
        """The decision tree for a phone-owned job, or an honest "none": a console that sends no
        step events (an old one, a failed feed) just leaves the phone with the plain Working line."""
        snap = self.trace.snapshot(job.get("id"))
        started = job.get("started")
        ended = job.get("finished") or time.time()
        out = {"available": snap is not None, "rev": snap["rev"] if snap else 0,
               "steps": snap["steps"] if snap else [], "tier": snap["tier"] if snap else None,
               "reasons": snap["reasons"] if snap else [], "streaming": bool(snap and snap["streaming"])}
        if isinstance(started, (int, float)):
            out["elapsed"] = max(0, round(ended - started, 1))
        return out

    def _answer(self, job, wait, after=None):
        deadline = time.monotonic() + wait
        settled = False
        while job.get("state") == "running" and time.monotonic() < deadline and not self._stop.is_set():
            if after is not None and self.trace.rev(job.get("id")) > after:
                if settled:
                    break
                settled = True  # let a burst of steps land together, then answer once
                time.sleep(ASK_BATCH)
                continue
            time.sleep(0.1)
        trace = self._trace_of(job)
        if job.get("state") == "running":
            return {"pending": True, "job": job["id"], "trace": trace}
        if job.get("state") == "error":
            raise _refuse(502, "JARVIS couldn't finish that: " + clean_text(job.get("error"), 300),
                          job=job["id"], trace=trace)
        result = job.get("result") or {}
        usage = request_usage(result.get("usage"))
        if result.get("confirmations"):
            return {"reply": "Waiting for your OK on the PC.", "waiting": True, "job": job["id"], "trace": trace, "usage": usage}
        return {"reply": str(result.get("reply") or ""), "job": job["id"], "trace": trace, "usage": usage}

    def logout(self, phone):
        self.store.revoke(phone["id"])
        self.sync_phones()
        return Reply(200, {"ok": True}, {"Set-Cookie": set_cookie("", 0)})

    def gateway_identity(self, claim):
        """The server-owned phone for a verified gateway claim.

        The same object is returned for the same phone id, so session and Ask
        ownership stay on `phone:{phone_id}` across calls. A client dict is not
        an identity. This does not read cookies or Tailscale headers.
        """
        if type(claim) is not GatewayPhoneClaim or not claim._intact():
            raise PhoneGatewayError("forbidden", 403, "The phone identity was not verified.")
        with self._mu:
            found = self._gateway_phones.get(claim.phone_id)
            if found is None:
                found = {"id": claim.phone_id, "name": claim.phone_id, "via": "gateway"}
                self._gateway_phones[claim.phone_id] = found
            return found

    def gateway_logout(self, phone):
        """End this gateway phone's sessions. Does not rewrite phone.json."""
        opener = self._opener(phone)
        pid = phone["id"]
        with self._mu:
            self._gateway_phones.pop(pid, None)
            mine = [sid for sid, meta in self._mine.items() if meta.get("phone") == pid]
            for sid in mine:
                self._mine.pop(sid, None)
            self._keys = {key: value for key, value in self._keys.items() if key[0] != pid}
        end = getattr(self.hub, "end_opener", None)
        if callable(end):
            try:
                end(opener)
            except Exception:
                raise _refuse(503, "The phone's sessions could not be ended.") from None
        self.publish("phone.revoked", {"name": phone.get("name")})
        return {"ok": True, "ending": "gateway"}

    def _gateway_reserve(self, claim):
        """One in-flight record per operation id. The request body is not stored."""
        with self._mu:
            row = self._gateway_ops.get(claim.operation_id)
            if row is not None:
                if (not _eq(row["digest"], claim.digest) or row["phone_id"] != claim.phone_id
                        or row["kind"] != claim.kind):
                    raise PhoneGatewayError("conflict", 409, "That request already exists with different contents.")
                if row["state"] == "inflight":
                    raise PhoneGatewayError("uncertain", 409, "The outcome is unknown. It was not started again.")
                if row["state"] == "error":
                    err = row["error"]
                    raise PhoneGatewayError(err["code"], err["status"], err["message"])
                result = copy.deepcopy(row["result"])
                return {"operation_id": claim.operation_id, "kind": claim.kind, "duplicate": True,
                        "target": claim.target, "result": result}
            if len(self._gateway_ops) >= 1024:
                raise PhoneGatewayError("unavailable", 503, "The computer cannot take another request.")
            self._gateway_ops[claim.operation_id] = {
                "digest": claim.digest, "phone_id": claim.phone_id, "kind": claim.kind,
                "state": "inflight", "result": None, "error": None,
            }
        return None

    def _gateway_settle(self, claim, outcome):
        with self._mu:
            row = self._gateway_ops.get(claim.operation_id)
            if row is None or not _eq(row["digest"], claim.digest):
                return
            if isinstance(outcome, PhoneGatewayError):
                row["state"] = "error"
                row["error"] = {"code": outcome.code, "status": outcome.status, "message": outcome.message}
                row["result"] = None
                return
            row["state"] = "done"
            row["result"] = copy.deepcopy(outcome)
            row["error"] = None

    def _gateway_status(self, phone, body):
        """The stored projection. Does not run the handler or the guard again."""
        operation_id = body.get("operation_id")
        if not isinstance(operation_id, str) or not _OP_ID.fullmatch(operation_id):
            raise PhoneGatewayError("not_found", 404, "No such request.")
        with self._mu:
            row = self._gateway_ops.get(operation_id)
            if row is None or row["phone_id"] != phone["id"]:
                raise PhoneGatewayError("not_found", 404, "No such request.")
            if body.get("kind") not in (None, row["kind"]):
                raise PhoneGatewayError("not_found", 404, "No such request.")
            if row["state"] == "error":
                err = row["error"]
                raise PhoneGatewayError(err["code"], err["status"], err["message"])
            if row["state"] != "done" or not isinstance(row["result"], dict):
                raise PhoneGatewayError("uncertain", 409, "The outcome is unknown. It was not started again.")
            return copy.deepcopy(row["result"])

    def accept_gateway_phone(self, claim, kind, operation_id, body, *, guard,
                             signed_operation=None, admission_ticket=None):
        """PhoneServer entry for the gateway seam. No listener and no cookie check."""
        return _accept_gateway_phone(
            claim, kind, operation_id, body, guard=guard, signed_operation=signed_operation,
            admission_ticket=admission_ticket, receiver=self)

    # ---- static shell
    def shell_version(self):
        """A short hash of the shell files: a new one makes phones fetch the new shell."""
        files = sorted({rel for rel in SHELL.values()})
        stamp = []
        for rel in files:
            try:
                st = (self.web / rel).stat()
                stamp.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((rel, None, None))
        stamp = tuple(stamp)
        if self._version[0] != stamp:
            digest = hashlib.sha256()
            for rel in files:
                try:
                    digest.update(rel.encode() + b"\0" + (self.web / rel).read_bytes())
                except OSError:
                    digest.update(rel.encode() + b"\0-")
            self._version = (stamp, digest.hexdigest()[:12])
        return self._version[1]

    def shell_file(self, path):
        """(bytes, content type, cache) for an allowlisted shell path, or None."""
        rel = SHELL.get(path)
        if rel is None:
            return None
        root = self.web.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            return None
        ctype = TYPES.get(target.suffix.lower())
        if ctype is None:
            return None
        data = target.read_bytes()
        if path == "/sw.js":
            data = data.replace(b"__SHELL_VERSION__", self.shell_version().encode())
        return data, ctype, "max-age=86400" if target.suffix.lower() in LASTING else "no-cache"


class PhoneHandler(BaseHTTPRequestHandler):
    """Routes for the phone app only. Never a subclass of ui.Handler: its _send carries the
    console's CSP, and nothing here may fall through to a console route."""
    phone = None  # the PhoneServer, set by PhoneServer.start
    timeout = SOCKET_TIMEOUT
    server_version = "JARVIS"
    sys_version = ""

    def log_message(self, *_):
        pass  # request lines can hold session ids; nothing is logged per request

    def send_error(self, code, message=None, explain=None):
        """Malformed requests (bad request line, huge headers) get our headers too."""
        self.close_connection = True
        try:
            self._json(code, {"error": message or "Bad request."})
        except OSError:
            pass

    def _send(self, status, data, ctype, cache="no-store", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", cache)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, body, headers=None):
        self._send(status, json.dumps(body, default=str).encode(), "application/json; charset=utf-8",
                   headers=headers)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _not_allowed(self):
        self.close_connection = True
        self._json(405, {"error": "Method not allowed."})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = _not_allowed

    def _handle(self, method):
        try:
            try:
                from .continuity.integration import phone_listener_open
            except ImportError:
                phone_listener_open = None
            if phone_listener_open is not None and not phone_listener_open():
                raise Reply(503, {"error": "The phone listener is closed for continuity.", "code": "legacy_retained"})
            self._route(method)
        except Reply as reply:
            if reply.status >= 400 and method == "POST":
                self.close_connection = True
            self._json(reply.status, reply.body, reply.headers)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:  # never a traceback to the phone
            _log(f"error on {self.command} {urlsplit(self.path).path[:80]}: {type(exc).__name__}")
            self.close_connection = True
            self._json(500, {"error": "Something went wrong on the PC."})

    def _body(self):
        """Check 5: the POST header, a JSON content type, and a JSON object of at most 256 KiB."""
        if self.headers.get("X-JARVIS-Phone") != "1":
            raise _refuse(403, "Refused.")
        ctype = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _refuse(415, "Send JSON.")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise _refuse(411, "Content-Length is required.") from None
        if not 0 <= length <= MAX_BODY:
            raise _refuse(413, "That request is too large.")
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            raise _refuse(400, "That isn't JSON.") from None
        if not isinstance(body, dict):
            raise _refuse(400, "Send a JSON object.")
        return body

    def _route(self, method):
        srv = self.phone
        url = urlsplit(self.path)
        path = url.path
        why = srv.refusal()
        if why:
            raise _refuse(503, why)
        if not srv.host_ok(self.headers.get("Host")):
            raise _refuse(403, "Unexpected Host header.")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise _refuse(403, "Requests from other sites are refused.")
        if method == "GET" and path in SHELL:
            found = srv.shell_file(path)
            if found is None:
                raise _refuse(404, "Not found.")
            data, ctype, cache = found
            return self._send(200, data, ctype, cache)
        api = path.startswith("/phone/api/")
        if not (api or path in PAIR_PATHS):
            raise _refuse(404, "Not found.")
        if path in PAIR_PATHS and method != "POST":
            raise _refuse(405, "Method not allowed.")
        body = self._body() if method == "POST" else None
        ip = tailnet_ip(self.headers.get("X-Forwarded-For"))
        if path in PAIR_PATHS:
            if ip is None:
                raise _refuse(401, NO_ADDRESS, pair=False)
            if srv.limiter.blocked("ip:" + ip):  # before whois: wrong codes don't buy lookups
                raise _refuse(429, "Too many wrong codes. Wait ten minutes, then make a new one on your PC.")
            who = srv.identify(self.headers, ip)
            ua = clean_text(self.headers.get("User-Agent"), UA_MAX)
            raise srv.pair(body, who, ip, ua, claim=path == "/phone/pair")
        who = srv.identify(self.headers, ip)
        phone = srv.phone_for(self.headers, who)
        return self._api(method, path, url.query, body, phone)

    def _api(self, method, path, query, body, phone):
        srv = self.phone
        if method == "GET":
            if path == "/phone/api/me":
                return self._json(200, srv.me(phone))
            if path == "/phone/api/overview":
                return self._json(200, srv.overview(phone))
            if path == "/phone/api/fleet":
                return self._json(200, srv.fleet(phone))
            if path == "/phone/api/session/frame":
                frame = srv.frame(phone, parse_qs(query))
                if frame is None:
                    ago = srv.node_ago(phone, (parse_qs(query).get("session") or [""])[0])
                    return self._send(204, b"", "text/plain",
                                      headers={"X-Node-Ago": f"{ago:.1f}"} if ago is not None else None)
                seq, jpeg, width, height = frame
                return self._send(200, jpeg, "image/jpeg", headers={
                    "X-Frame-Seq": seq, "X-Frame-Width": width, "X-Frame-Height": height})
            if path.startswith("/phone/api/ask/"):
                return self._json(200, srv.ask_job(phone, path.rsplit("/", 1)[-1], parse_qs(query)))
            raise _refuse(404, "Not found.")
        routes = {"/phone/api/session/start": srv.start_session,
                  "/phone/api/session/input": srv.session_input,
                  "/phone/api/session/stop": srv.stop_session, "/phone/api/ask": srv.ask}
        if path == "/phone/api/logout":
            raise srv.logout(phone)
        if path not in routes:
            raise _refuse(404, "Not found.")
        return self._json(200, routes[path](phone, body))


def start(config, state, console=None, hub=None):
    """The phone server for the main runtime, or None when the phone link is off or can't
    bind. Never raises: the console must come up even when the phone can't."""
    try:
        server = PhoneServer(config, state, console=console, hub=hub)
        if not server.store.on():
            return None
        return server.start()
    except Exception as exc:
        _log(f"not started ({type(exc).__name__}: {clean_text(exc, 200)})")
        return None


# ---- the CLI: jarvis phone on|off|status|pair|list|revoke --------------------------------

def _main_only(config):
    from .topology import enabled, is_node
    if not enabled(config):
        raise ValueError("The phone app needs the cluster on (JARVIS's main runtime serves it).")
    if is_node(config):
        raise ValueError("Phones pair with the main PC. Run this there.")


def _listening(port):
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def status(config, state, tailnet=None):
    """What `jarvis phone status` prints: on or off, the address, whether Serve points at the
    port and whether Funnel would publish it (then the server refuses everything)."""
    store, port = Store(state), port_for(config)
    tailnet = tailnet or Tailnet()
    data = store.load()
    name = tailnet.dns_name()
    serve = tailnet.serve_config()
    serving = published = None
    if serve is not None:
        serving, published = serve_targets(serve, port)
    if name:
        try:
            store.remember_host(name, port)
        except (OSError, ValueError):
            pass
    if not data["on"]:
        refusing = "The phone link is off (jarvis phone on, then restart JARVIS)."
    elif serve is None:
        refusing = "Tailscale's serve settings can't be read, so the server refuses everything."
    elif published:
        refusing = ("Tailscale Funnel publishes this port to the internet, so the server refuses "
                    f"everything. Turn it off: {UNDO.format(port=port)}, then {SETUP.format(port=port)}")
    else:
        refusing = None
    return {"on": data["on"], "port": port, "listening": _listening(port),
            "url": f"https://{name}/" if name else None, "dns_name": name,
            "serve_points_here": serving, "funnel": published, "refusing": refusing,
            "phones": len(data["phones"]),
            "setup": SETUP.format(port=port) if not serving else None,
            "note": "Tailnet only, never funnel. `jarvis phone on` binds at the next JARVIS start; "
                    "`jarvis phone off` refuses at once."}


def listing(state):
    return [{k: p.get(k) for k in ("id", "name", "node", "os", "login", "created", "last_seen")}
            for p in Store(state).load()["phones"]]


def pair_cli(state, name=None, allow_desktop=False, ask=input, out=print, sleep=time.sleep,
             clock=time.time):
    """`jarvis phone pair`: make a code, print it to this terminal only, wait for a phone to
    send it, then ask David y/N. Deny, Ctrl+C or timeout burns the code."""
    store = Store(state, clock)
    if not store.on():
        raise ValueError("The phone link is off. Run: jarvis phone on (then restart JARVIS).")
    code, code_id = store.new_code(name, allow_desktop)
    out(f"Pairing code: {code[:4]}-{code[4:]}  (valid {CODE_TTL // 60} minutes; type it on your phone)")
    out("Waiting for your phone...")
    try:
        while True:
            entry = store.code(code_id)
            if entry is None or entry["expires"] <= clock():
                store.burn(code_id)
                return {"ok": False, "text": "The code expired. Run jarvis phone pair again."}
            if entry["state"] == "pending":
                break
            sleep(1)
        request = entry.get("request") or {}
        question = (f"{clean_text(request.get('node'), 80)} ({clean_text(request.get('os'), 40)}, "
                    f"{clean_text(request.get('login'), 120)}) wants to pair as "
                    f"'{clean_text(request.get('name'), NAME_MAX)}'. Allow? [y/N] ")
        answer = ask(question)
        if str(answer or "").strip().lower() != "y":
            store.deny(code_id)
            return {"ok": False, "text": "Refused. That code can't be used again."}
        if not store.approve(code_id):
            store.burn(code_id)
            return {"ok": False, "text": "The code expired before you answered. Run jarvis phone pair again."}
        while True:
            entry = store.code(code_id)
            if entry is None:
                phone = next((p for p in store.load()["phones"]
                              if p.get("stable_id") == request.get("stable_id")
                              and float(p.get("created") or 0) >= float(request.get("at") or 0)), None)
                if phone:
                    return {"ok": True, "text": f"Paired '{phone['name']}' (id {phone['id']})."}
                return {"ok": False, "text": "The phone didn't collect its key. Pair again."}
            if entry["expires"] <= clock():
                store.burn(code_id)
                return {"ok": False, "text": "The phone didn't come back for its key. Pair again."}
            sleep(1)
    except BaseException:
        store.burn(code_id)
        raise


def cli(args, config, state):
    """The `jarvis phone` subcommands -> what to print (pair prints its code itself)."""
    _main_only(config)
    store = Store(state)
    if args.action in ("on", "off"):
        store.set_on(args.action == "on")
        if args.action == "on":
            return {"on": True, "text": "Phone link on. It starts listening when JARVIS restarts "
                                        "(systemctl --user restart jarvis-cluster)."}
        return {"on": False, "text": "Phone link off. JARVIS refuses phone requests from now on."}
    if args.action == "status":
        return status(config, state)
    if args.action == "list":
        return listing(state)
    if args.action == "revoke":
        if not args.value:
            raise ValueError("Usage: jarvis phone revoke <id>|all  (ids: jarvis phone list)")
        gone = store.revoke(args.value)
        if not gone:
            raise ValueError("No paired phone has that id (see jarvis phone list).")
        return {"revoked": [p.get("name") for p in gone],
                "text": "Revoked. Its sessions end within seconds."}
    if args.action == "pair":
        return pair_cli(state, args.name, args.allow_desktop)
    raise ValueError("Usage: jarvis phone on|off|status|pair|list|revoke")


# ---- gateway phone receiver -------------------------------------------------------------
# The PC seam for a signed transient envelope. WS5 calls GatewayPhoneReceiver.
# This does not listen, does not read Tailscale whois, and does not import a
# cookie. A phone mutation runs only inside ExecutorGuard.accept.

GATEWAY_PHONE_NS = "jarvis-continuity-gateway-phone-v1"
GATEWAY_PHONE_PURPOSE = "gateway_phone"
# verify_message only accepts the namespaces gateway.py lists. The ingress
# installation key signs in DEVICE_NS. GATEWAY_PHONE_NS is a signed prefix so
# those bytes are not a device-authentication message.
_GATEWAY_PHONE_FIELDS = (
    "attempt_id", "authority_id", "digest", "domain", "epoch", "fleet_id",
    "incarnation", "issuer_incarnation", "kind", "main", "manage_main",
    "mode_version", "opener", "operation_id", "phone_id", "policy_version",
    "purpose", "role", "target", "v",
)
# Ingress imports this tuple lazily and signs exactly these fields. There is no second schema.
GATEWAY_PHONE_FIELDS = _GATEWAY_PHONE_FIELDS
_READ_KINDS = frozenset({"overview", "fleet", "ask_poll", "session_frame", "effect_status"})
_EFFECT_KINDS = frozenset({"ask", "session_start", "session_input", "session_stop", "logout_end"})
_EFFECT_CLASS = {
    "ask": "ordinary", "logout_end": "ordinary", "session_stop": "ordinary",
    "session_start": "interactive", "session_input": "interactive",
}
_RELAY_KEYS = frozenset({
    "operation_id", "target", "attempt_id", "digest", "capability", "kind",
    "authority_id", "incarnation", "epoch", "main", "envelope_b64",
})
_ENVELOPE_KEYS = frozenset({"v", "claim", "signature", "body"})
_BANNED_KEYS = frozenset({
    "cookie", "cookies", "set-cookie", "x-forwarded-for", "forwarded",
    "authorization", "proxy-authorization", "tailscale-user-login",
    "stable_id", "whois", "csrf", "csrf_token", "password", "secret", "token",
    "url", "host", "upstream", "connect_ip",
})
_BODY_REQUIRED = {
    "ask": frozenset({"text"}),
    "session_start": frozenset({"device", "session_kind"}),
    "session_input": frozenset({"session", "events"}),
    "session_stop": frozenset({"session"}),
    "logout_end": frozenset({"phone_id"}),
    "overview": frozenset(),
    "fleet": frozenset(),
    "ask_poll": frozenset({"job"}),
    "session_frame": frozenset({"session"}),
    "effect_status": frozenset(),
}
_BODY_OPTIONAL = {
    "ask": frozenset({"device"}),
    "session_start": frozenset({"quality"}),
    "session_stop": frozenset({"release_only"}),
    "ask_poll": frozenset({"after"}),
    "session_frame": frozenset({"after"}),
    "effect_status": frozenset({"operation_id", "kind"}),
}
_PHONE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_OP_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_JOB_RE = re.compile(r"[0-9a-f]{6,32}")
_SESSION_RE = re.compile(r"[0-9a-f]{32}")
_PUBLIC_KEY_RE = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}")
_MARK = object()


class PhoneGatewayError(Exception):
    """A refusal safe to show. It never includes the request body."""

    def __init__(self, code, status, message):
        super().__init__(code)
        self.code, self.status, self.message = code, int(status), message

    def body(self):
        return {"error": self.message, "code": self.code}


class GatewayPhoneClaim:
    """Opaque phone claim. Only GatewayPhoneVerifier can construct one."""

    __slots__ = (
        "_phone_id", "_opener", "_operation_id", "_attempt_id", "_kind", "_digest",
        "_main", "_authority_id", "_fleet_id", "_incarnation", "_epoch",
        "_mode_version", "_policy_version", "_issuer_incarnation", "_target", "_mark", "_stamp",
    )

    def __new__(cls, mark, **fields):
        if cls is not GatewayPhoneClaim or mark is not _MARK:
            raise PhoneGatewayError("forbidden", 403, "A phone claim is only issued by the verifier.")
        return object.__new__(cls)

    def __init__(self, mark, **fields):
        if mark is not _MARK:
            raise PhoneGatewayError("forbidden", 403, "A phone claim is only issued by the verifier.")
        for name, value in fields.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_mark", _MARK)
        object.__setattr__(self, "_stamp", self._fingerprint())

    def _fingerprint(self):
        raw = json.dumps([
            self._phone_id, self._opener, self._operation_id, self._attempt_id, self._kind,
            self._digest, self._main, self._authority_id, self._fleet_id, self._incarnation,
            self._epoch, self._mode_version, self._policy_version, self._issuer_incarnation,
            self._target,
        ], separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _intact(self):
        if self._mark is not _MARK:
            return False
        try:
            return hmac.compare_digest(self._stamp, self._fingerprint())
        except Exception:
            return False

    def __setattr__(self, name, value):
        raise PhoneGatewayError("forbidden", 403, "A phone claim cannot be changed.")

    def __repr__(self):
        return f"GatewayPhoneClaim(phone_id={self._phone_id!r}, kind={self._kind!r})"

    @property
    def phone_id(self):
        return self._phone_id

    @property
    def opener(self):
        return self._opener

    @property
    def operation_id(self):
        return self._operation_id

    @property
    def attempt_id(self):
        return self._attempt_id

    @property
    def kind(self):
        return self._kind

    @property
    def digest(self):
        return self._digest

    @property
    def main(self):
        return self._main

    @property
    def authority_id(self):
        return self._authority_id

    @property
    def fleet_id(self):
        return self._fleet_id

    @property
    def incarnation(self):
        return self._incarnation

    @property
    def epoch(self):
        return self._epoch

    @property
    def mode_version(self):
        return self._mode_version

    @property
    def policy_version(self):
        return self._policy_version

    @property
    def issuer_incarnation(self):
        return self._issuer_incarnation

    @property
    def target(self):
        return self._target

    @property
    def role(self):
        return "phone"

    @property
    def manage_main(self):
        return False


def _gateway():
    from jarvis.continuity import gateway
    return gateway


def _authority_error():
    from jarvis.continuity.authority import AuthorityError
    return AuthorityError


def _pinned_key(value):
    if not isinstance(value, str) or not _PUBLIC_KEY_RE.fullmatch(value):
        raise PhoneGatewayError("forbidden", 403, "The pinned key is not an ed25519 public key.")
    return value


def _ident(value, label):
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise PhoneGatewayError("bad_request", 400, f"{label} must be a short identifier.")
    return value


def _nnint(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhoneGatewayError("bad_request", 400, f"{label} must be a non-negative integer.")
    return value


def _eq(left, right):
    if not isinstance(left, str) or not isinstance(right, str) or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _banned(value, depth=0):
    if depth > 8:
        raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.")
            folded = key.lower().replace("_", "-")
            if folded in _BANNED_KEYS or key.lower() in _BANNED_KEYS:
                raise PhoneGatewayError("forbidden", 403, "That request carried an identity the gateway hop cannot present.")
            _banned(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 200:
            raise PhoneGatewayError("bad_request", 400, "That request is too large.")
        for item in value:
            _banned(item, depth + 1)


def _canonical_body(body):
    if not isinstance(body, dict):
        raise PhoneGatewayError("bad_request", 400, "Send a JSON object.")
    _banned(body)
    try:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.") from None
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_BODY:
        raise PhoneGatewayError("bad_request", 400, "That request is too large.")
    return encoded


def gateway_phone_digest(body):
    """sha256 of the canonical phone body. This is the admission digest, not the relay digest."""
    return "sha256:" + hashlib.sha256(_canonical_body(body)).hexdigest()


def gateway_phone_message(fields):
    """Bytes the ingress key signs. The SSH namespace is DEVICE_NS."""
    if not isinstance(fields, dict) or set(fields) != set(_GATEWAY_PHONE_FIELDS):
        raise PhoneGatewayError("bad_request", 400, "The phone claim fields are not the signed set.")
    gateway = _gateway()
    try:
        signed = gateway._message(gateway.DEVICE_NS, fields)
    except Exception:
        raise PhoneGatewayError("bad_request", 400, "The phone claim cannot be signed.") from None
    raw = GATEWAY_PHONE_NS.encode("ascii") + b"\n" + signed
    if len(raw) > gateway.MAX_BODY:
        raise PhoneGatewayError("bad_request", 400, "The phone claim cannot be signed.")
    return raw


def _body_shape(kind, body):
    if kind not in _BODY_REQUIRED:
        raise PhoneGatewayError("bad_request", 400, "Unknown request.")
    if not isinstance(body, dict):
        raise PhoneGatewayError("bad_request", 400, "Send a JSON object.")
    allowed = _BODY_REQUIRED[kind] | _BODY_OPTIONAL.get(kind, frozenset())
    if set(body) - allowed or any(key not in body for key in _BODY_REQUIRED[kind]):
        raise PhoneGatewayError("bad_request", 400, "That request has the wrong fields.")
    _banned(body)
    return body


def _check_semantics(fields, body):
    if fields.get("domain") != GATEWAY_PHONE_NS or fields.get("purpose") != GATEWAY_PHONE_PURPOSE:
        raise PhoneGatewayError("forbidden", 403, "The signature is not a phone claim.")
    if fields.get("v") != 1 or fields.get("role") != "phone" or fields.get("manage_main") is not False:
        raise PhoneGatewayError("forbidden", 403, "This phone cannot do that.")
    phone_id = fields.get("phone_id")
    if not isinstance(phone_id, str) or not _PHONE_ID.fullmatch(phone_id):
        raise PhoneGatewayError("forbidden", 403, "The phone identity was not verified.")
    opener = "phone:" + phone_id
    if not _eq(fields.get("opener"), opener):
        raise PhoneGatewayError("forbidden", 403, "The phone opener does not match the signed phone.")
    if not isinstance(fields.get("operation_id"), str) or not _OP_ID.fullmatch(fields["operation_id"]):
        raise PhoneGatewayError("bad_request", 400, "This request needs a stable operation id.")
    _ident(fields.get("attempt_id"), "attempt_id")
    _ident(fields.get("main"), "main")
    _ident(fields.get("target"), "target")
    _ident(fields.get("authority_id"), "authority_id")
    _ident(fields.get("fleet_id"), "fleet_id")
    for label in ("incarnation", "epoch", "mode_version", "policy_version", "issuer_incarnation"):
        _nnint(fields.get(label), label)
    kind = fields.get("kind")
    if kind not in _READ_KINDS and kind not in _EFFECT_KINDS:
        raise PhoneGatewayError("bad_request", 400, "Unknown request.")
    digest = fields.get("digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise PhoneGatewayError("bad_request", 400, "The request digest is not valid.")
    if not _eq(digest, gateway_phone_digest(body)):
        raise PhoneGatewayError("bad_request", 400, "The request does not match the signed claim.")
    _body_shape(kind, body)
    return opener


class GatewayPhoneVerifier:
    """Verify one ingress-signed transient envelope and mint a phone claim.

    The public key is pinned here. The envelope cannot choose a key.
    """

    def __init__(self, public_key):
        self._public_key = _pinned_key(public_key)

    def verify(self, envelope):
        data = self._object(envelope)
        claim, body, signature = data.get("claim"), data.get("body"), data.get("signature")
        if not isinstance(claim, dict) or set(claim) != set(_GATEWAY_PHONE_FIELDS):
            raise PhoneGatewayError("bad_request", 400, "The phone claim fields are not the signed set.")
        if not isinstance(signature, str) or not isinstance(body, dict) or data.get("v") != 1:
            raise PhoneGatewayError("unauthorized", 401, "The phone signature was refused.")
        gateway = _gateway()
        try:
            ok = gateway.verify_message(
                gateway.DEVICE_NS, gateway_phone_message(claim), signature, self._public_key)
        except PhoneGatewayError:
            raise
        except Exception:
            ok = False
        if ok is not True:
            raise PhoneGatewayError("unauthorized", 401, "The phone signature was refused.")
        _check_semantics(claim, body)
        return self._mint(claim)

    def _object(self, envelope):
        if isinstance(envelope, (bytes, bytearray)):
            if not envelope or len(envelope) > MAX_BODY:
                raise PhoneGatewayError("bad_request", 400, "That request is too large.")
            try:
                data = json.loads(bytes(envelope).decode("utf-8"))
            except (UnicodeError, ValueError):
                raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.") from None
        elif isinstance(envelope, dict):
            data = envelope
        else:
            raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.")
        if not isinstance(data, dict) or set(data) != _ENVELOPE_KEYS:
            raise PhoneGatewayError("bad_request", 400, "Unknown phone envelope.")
        _banned(data.get("claim"))
        _banned(data.get("body"))
        return data

    def _mint(self, fields):
        opener = "phone:" + fields["phone_id"]
        return GatewayPhoneClaim(
            _MARK, _phone_id=fields["phone_id"], _opener=opener,
            _operation_id=fields["operation_id"], _attempt_id=fields["attempt_id"],
            _kind=fields["kind"], _digest=fields["digest"], _main=fields["main"],
            _authority_id=fields["authority_id"], _fleet_id=fields["fleet_id"],
            _incarnation=fields["incarnation"], _epoch=fields["epoch"],
            _mode_version=fields["mode_version"], _policy_version=fields["policy_version"],
            _issuer_incarnation=fields["issuer_incarnation"], _target=fields["target"])


class GatewayTicketVerifier:
    """MAIN admission verifier. `verify` is true only after `verify_message`.

    Pass this object as `ExecutorGuard`'s verifier. When the authority client
    needs `ticket_signature` (the gateway port), pass this same object as the
    guard's authority too: `verify` keeps the signature and `consume` forwards it.
    A callback that returns true is not a verifier.
    """

    def __init__(self, public_keys, authority=None):
        if not isinstance(public_keys, dict) or not public_keys:
            raise PhoneGatewayError("forbidden", 403, "No MAIN key is pinned.")
        self._keys = {str(key): _pinned_key(value) for key, value in public_keys.items()}
        self._authority = authority
        self._signed = {}
        self._lock = threading.Lock()
        self._peer = _ingress_ticket_verifier(self._keys)

    def __call__(self, signed):
        return self.verify(signed)

    def verify(self, signed):
        try:
            fields, signature, key = self._parts(signed)
        except Exception:
            return False
        gateway = _gateway()
        try:
            ok = gateway.verify_message(gateway.TICKET_NS, gateway._message(gateway.TICKET_NS, fields), signature, key)
        except Exception:
            return False
        if ok is not True:
            return False
        if self._peer is not None and _peer_accepts(self._peer, signed) is not True:
            return False
        with self._lock:
            self._signed[fields["admission_id"]] = (dict(fields), signature)
        return True

    def _parts(self, signed):
        from jarvis.continuity.authority import TICKET_FIELDS
        if not isinstance(signed, dict):
            return None
        signature = signed.get("signature")
        main = signed.get("main")
        key = self._keys.get(main)
        if not isinstance(signature, str) or key is None:
            raise PhoneGatewayError("unauthorized", 401, "The ticket signature was refused.")
        fields = {}
        for name in TICKET_FIELDS:
            if name not in signed:
                raise PhoneGatewayError("bad_request", 400, "The ticket fields are incomplete.")
            fields[name] = signed[name]
        return fields, signature, key

    def status(self):
        return self._forward("status")

    def acknowledge(self, *args, **kwargs):
        if self._authority is None:
            raise _authority_error()("no_authority", "no authority client")
        return self._authority.acknowledge(*args, **kwargs)

    def admission(self, admission_id):
        if self._authority is None:
            raise _authority_error()("no_authority", "no authority client")
        return self._authority.admission(admission_id)

    def consume(self, **fields):
        """Forward a ticket this object has already verified."""
        if self._authority is None:
            raise _authority_error()("no_authority", "no authority client")
        with self._lock:
            stored = self._signed.get(fields.get("admission_id"))
        if stored is None:
            raise _authority_error()("signature", "ticket was not verified against a MAIN signature")
        original, signature = stored
        for key, value in original.items():
            if fields.get(key) != value and not _close_number(value, fields.get(key)):
                raise _authority_error()("signature", "ticket fields differ from the signed ticket")
        import inspect
        params = inspect.signature(self._authority.consume).parameters
        if "ticket_signature" in params:
            return self._authority.consume(**fields, ticket_signature=signature)
        return self._authority.consume(**fields)

    def _forward(self, name):
        if self._authority is None:
            raise _authority_error()("no_authority", "no authority client")
        return getattr(self._authority, name)()


def _close_number(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return abs(float(left) - float(right)) <= 1e-6


def _peer_accepts(peer, signed):
    """True only when the ingress ticket verifier accepts `signed`.

    `TicketSignatureVerifier` is called. It has no `verify` method.
    """
    try:
        if callable(peer):
            return peer(signed) is True
        method = getattr(peer, "verify", None)
        if callable(method):
            return method(signed) is True
    except Exception:
        return False
    return False


def _ingress_ticket_verifier(public_keys):
    """Use ingress.TicketSignatureVerifier when that class exists. Read only."""
    try:
        from jarvis.continuity import ingress as ingress_mod
    except Exception:
        return None
    cls = getattr(ingress_mod, "TicketSignatureVerifier", None)
    if not isinstance(cls, type):
        return None
    try:
        peer = cls(dict(public_keys))
    except Exception:
        return None
    if not (callable(peer) or callable(getattr(peer, "verify", None))):
        return None
    return peer


def _real_guard(guard):
    verifier = getattr(guard, "_verifier", None)
    if isinstance(verifier, GatewayTicketVerifier):
        return True
    owner = getattr(verifier, "__self__", None)
    if isinstance(owner, GatewayTicketVerifier):
        return True
    candidate = owner if owner is not None else verifier
    cls = type(candidate)
    if cls.__name__ == "TicketSignatureVerifier" and cls.__module__ == "jarvis.continuity.ingress":
        return callable(candidate) or callable(getattr(candidate, "verify", None))
    return False


def _ticket_matches(claim, signed, ticket, guard):
    from jarvis.continuity.authority import TICKET_FIELDS
    if not isinstance(signed, dict) or not isinstance(ticket, dict):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
    _banned(signed)
    _banned(ticket)
    allowed = set(TICKET_FIELDS) | {
        "authority_id", "fleet_id", "mode_version", "policy_version", "effect_class",
        "signature", "session_id",
    }
    if set(signed) - allowed or any(name not in signed for name in TICKET_FIELDS):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
    if not isinstance(signed.get("signature"), str) or "effect_class" not in signed:
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
    if set(ticket) != set(TICKET_FIELDS):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
    for name in TICKET_FIELDS:
        if name in ("issued_at", "admit_until"):
            if not _close_number(signed.get(name), ticket.get(name)):
                raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
            continue
        if signed.get(name) != ticket.get(name):
            raise PhoneGatewayError("bad_request", 400, "The admission does not match the phone claim.")
    if not _eq(signed.get("operation_id"), claim.operation_id) or not _eq(ticket.get("operation_id"), claim.operation_id):
        raise PhoneGatewayError("conflict", 409, "The admission is for a different request.")
    if not _eq(signed.get("attempt_id"), claim.attempt_id):
        raise PhoneGatewayError("conflict", 409, "The admission is for a different request.")
    if not _eq(signed.get("digest"), claim.digest) or not _eq(ticket.get("digest"), claim.digest):
        raise PhoneGatewayError("conflict", 409, "The admission does not match the request.")
    if (signed.get("main") != claim.main or ticket.get("main") != claim.main
            or signed.get("target") != claim.target or ticket.get("target") != claim.target
            or signed.get("authority_id") != claim.authority_id
            or signed.get("fleet_id") != claim.fleet_id):
        raise PhoneGatewayError("stale", 409, "The admission is for a different computer.")
    for label in ("incarnation", "epoch", "issuer_incarnation"):
        if signed.get(label) != getattr(claim, label) or ticket.get(label) != getattr(claim, label):
            raise PhoneGatewayError("stale", 409, "Authority moved on. Nothing was changed.")
    if signed.get("mode_version") != claim.mode_version or signed.get("policy_version") != claim.policy_version:
        raise PhoneGatewayError("stale", 409, "Authority moved on. Nothing was changed.")
    if getattr(guard, "device_id", None) != claim.target or getattr(guard, "fleet_id", None) != claim.fleet_id:
        raise PhoneGatewayError("forbidden", 403, "The admission is for a different computer.")
    if getattr(guard, "authority_id", None) != claim.authority_id:
        raise PhoneGatewayError("forbidden", 403, "The admission is for a different computer.")
    effect = _EFFECT_CLASS[claim.kind]
    if signed.get("effect_class") != effect:
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the request.")
    session = signed.get("session_id", None)
    if effect != "interactive" and "session_id" in signed and session is not None:
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the request.")
    if claim.kind == "session_start" and session not in (None, claim.operation_id):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the request.")
    if claim.kind == "session_input" and (not isinstance(session, str) or not _SESSION_RE.fullmatch(session)):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the session.")
    return effect


def _precheck(server, kind, body, phone_id):
    if kind == "ask":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > ASK_MAX:
            raise PhoneGatewayError("bad_request", 400, "Type something for JARVIS to do.")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()
        if not text:
            raise PhoneGatewayError("bad_request", 400, "Type something for JARVIS to do.")
        try:
            from .assistant import confirmation_words
            words = text.lower().replace("\u2019", "'").rstrip(".!?").strip()
            blocked = bool(confirmation_words(text)) or _APPROVALS.fullmatch(words) is not None
        except PhoneGatewayError:
            raise
        except Exception:
            blocked = True
        if blocked:
            raise PhoneGatewayError("bad_request", 400, "Approve on your PC.")
        if server.console is None:
            raise PhoneGatewayError("unavailable", 503, "Ask needs JARVIS's main runtime.")
        target = body.get("device")
        if target is not None:
            if not isinstance(target, str) or target not in (server.config.get("devices") or {}):
                raise PhoneGatewayError("bad_request", 400, "Choose a known computer.")
            if server.hub is None:
                raise PhoneGatewayError("unavailable", 503, "The computer link is unavailable.")
            if target != getattr(server.hub, "main", None):
                try:
                    caps = server.hub.target_caps(target)
                except Exception:
                    raise PhoneGatewayError("conflict", 409, "That computer is unavailable.") from None
                ok, why = caps.get("remote", (False, "Desktop actions unavailable."))
                if not ok:
                    raise PhoneGatewayError("conflict", 409, clean_text(why, 300) or "That computer is unavailable.")
        return
    if kind == "session_start":
        if body.get("session_kind") not in KINDS:
            raise PhoneGatewayError("bad_request", 400, "kind must be screen or camera.")
        if body.get("quality", "low") not in QUALITIES:
            raise PhoneGatewayError("bad_request", 400, "quality must be low, medium or high.")
        if body.get("device") not in (server.config.get("devices") or {}):
            raise PhoneGatewayError("bad_request", 400, "Pick one of your computers.")
        if server.hub is None:
            raise PhoneGatewayError("unavailable", 503, "Remote control needs JARVIS's main runtime.")
        return
    if kind == "session_input":
        if not isinstance(body.get("session"), str) or not _SESSION_RE.fullmatch(body["session"]):
            raise PhoneGatewayError("not_found", 404, "No such session.")
        events = body.get("events")
        if not isinstance(events, list) or len(events) > 200:
            raise PhoneGatewayError("bad_request", 400, "events must be a list of at most 200.")
        try:
            from jarvis.remote_view import clean_event
            for event in events:
                clean_event(event)
        except PhoneGatewayError:
            raise
        except Exception:
            raise PhoneGatewayError("bad_request", 400, "Unknown input event.") from None
        return
    if kind == "session_stop":
        if not isinstance(body.get("session"), str) or not _SESSION_RE.fullmatch(body["session"]):
            raise PhoneGatewayError("not_found", 404, "No such session.")
        if "release_only" in body and not isinstance(body.get("release_only"), bool):
            raise PhoneGatewayError("bad_request", 400, "That request has the wrong fields.")
        return
    if kind == "logout_end":
        if body.get("phone_id") != phone_id:
            raise PhoneGatewayError("forbidden", 403, "The phone identity was not verified.")
        return
    if kind == "ask_poll":
        if not isinstance(body.get("job"), str) or not _JOB_RE.fullmatch(body["job"]):
            raise PhoneGatewayError("not_found", 404, "No such request.")
        _after(body)
        return
    if kind == "session_frame":
        if not isinstance(body.get("session"), str) or not _SESSION_RE.fullmatch(body["session"]):
            raise PhoneGatewayError("not_found", 404, "No such session.")
        _after(body)
        return
    if kind in ("overview", "fleet", "effect_status"):
        return
    raise PhoneGatewayError("bad_request", 400, "Unknown request.")


def _after(body):
    if "after" not in body:
        return 0
    value = body.get("after")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhoneGatewayError("bad_request", 400, "after must be a frame or trace number.")
    return value


def _status_int(status, name):
    value = status.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhoneGatewayError("unavailable", 503, "The computer could not read its authority.")
    return value


def _tuple_live(claim, guard):
    """Refuse unless the signed tuple is the authority's current online MAIN.

    This calls ``authority.status`` only. It does not consume and it does not
    call ``guard.accept``.
    """
    authority = getattr(guard, "_authority", None)
    status_fn = getattr(authority, "status", None)
    if not callable(status_fn):
        raise PhoneGatewayError("unavailable", 503, "The computer could not read its authority.")
    try:
        status = status_fn()
    except PhoneGatewayError:
        raise
    except Exception:
        raise PhoneGatewayError("unavailable", 503, "The computer could not read its authority.") from None
    if not isinstance(status, dict):
        raise PhoneGatewayError("unavailable", 503, "The computer could not read its authority.")
    if (status.get("main") != claim.main or status.get("authority_id") != claim.authority_id
            or status.get("fleet_id") != claim.fleet_id
            or _status_int(status, "incarnation") != claim.incarnation
            or _status_int(status, "epoch") != claim.epoch
            or _status_int(status, "mode_version") != claim.mode_version
            or _status_int(status, "policy_version") != claim.policy_version
            or status.get("lease_valid") is not True or status.get("main_ready") is not True
            or status.get("mode") != "V2_ACTIVE"):
        raise PhoneGatewayError("stale", 409, "Authority moved on. Nothing was changed.")
    return status


def _gateway_wire(kind, internal):
    """Transient JSON the gateway already accepts.

    Effects are exactly ``{operation_id, accepted}``. Reads are
    ``{operation_id, result:{status, body}}`` plus ``raw_b64`` for a frame.
    """
    operation_id = internal.get("operation_id")
    if kind in _EFFECT_KINDS:
        return {"operation_id": operation_id, "accepted": True}
    result = internal.get("result")
    status, raw, body = 200, None, result
    if kind == "session_frame" and isinstance(result, dict):
        found = result.get("http_status", 200)
        if isinstance(found, int) and not isinstance(found, bool):
            status = found
        if isinstance(result.get("raw"), (bytes, bytearray)):
            raw = bytes(result["raw"])
        body = {key: value for key, value in result.items() if key not in ("raw", "http_status")}
    wire = {"operation_id": operation_id, "result": {"status": status, "body": body}}
    if raw:
        import base64
        wire["raw_b64"] = base64.b64encode(raw).decode("ascii")
    return wire


def _perform(server, phone, kind, body, operation_id=None):
    try:
        if kind == "overview":
            return server.overview(phone)
        if kind == "fleet":
            return server.fleet(phone)
        if kind == "ask_poll":
            after = _after(body)
            return server.ask_job(phone, body["job"], {"after": [str(after)]})
        if kind == "session_frame":
            after = _after(body)
            frame = server.frame(phone, {"session": [body["session"]], "after": [str(after)]})
            if frame is None:
                return {"http_status": 204, "node_ago": server.node_ago(phone, body["session"])}
            seq, jpeg, width, height = frame
            if not isinstance(jpeg, (bytes, bytearray)):
                raise PhoneGatewayError("bad_gateway", 502, "The picture was refused.")
            return {"http_status": 200, "raw": bytes(jpeg), "frame_seq": seq,
                    "frame_width": width, "frame_height": height}
        if kind == "effect_status":
            return server._gateway_status(phone, body)
        if kind == "ask":
            # Retry key for this operation. It is not part of the signed body.
            payload = dict(body)
            if isinstance(operation_id, str):
                payload["key"] = operation_id
            return server.ask(phone, payload)
        if kind == "session_start":
            payload = {"device": body["device"], "kind": body["session_kind"],
                       "quality": body.get("quality") or "low"}
            return server.start_session(phone, payload)
        if kind == "session_input":
            return server.session_input(phone, {"session": body["session"], "events": body["events"]})
        if kind == "session_stop":
            # release_only still closes the session and drops held input.
            return server.stop_session(phone, {"session": body["session"]})
        if kind == "logout_end":
            return server.gateway_logout(phone)
    except Reply as exc:
        message = exc.body.get("error") if isinstance(exc.body, dict) else "Refused."
        raise PhoneGatewayError(_reply_code(exc.status), exc.status, clean_text(message, 300) or "Refused.") from None
    raise PhoneGatewayError("bad_request", 400, "Unknown request.")


def _reply_code(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
            409: "conflict", 410: "gone", 429: "busy", 502: "bad_gateway", 503: "unavailable"}.get(status, "unavailable")


def _project(kind, value):
    """Handler result with identity-bearing keys removed. Frames stay bytes in RAM."""
    if kind == "session_frame" and isinstance(value, dict):
        return value
    if not isinstance(value, dict):
        return {"kind": kind}
    out = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        folded = key.lower().replace("_", "-")
        if folded in _BANNED_KEYS or key.lower() in _BANNED_KEYS:
            continue
        out[key] = item
    out["kind"] = kind
    if isinstance(out.get("reply"), str):
        out["reply"] = out["reply"][:20000]
    return out


def _accept_gateway_phone(claim, kind, operation_id, body, *, guard, signed_operation=None,
                          admission_ticket=None, receiver=None):
    if isinstance(claim, dict) or type(claim) is not GatewayPhoneClaim or not claim._intact():
        raise PhoneGatewayError("forbidden", 403, "The phone identity was not verified.")
    if not isinstance(operation_id, str) or not _eq(operation_id, claim.operation_id):
        raise PhoneGatewayError("conflict", 409, "The operation id does not match the signed claim.")
    if kind != claim.kind:
        raise PhoneGatewayError("conflict", 409, "The request does not match the signed claim.")
    if not isinstance(receiver, PhoneServer):
        raise PhoneGatewayError("unavailable", 503, "The phone receiver is not configured.")
    _body_shape(kind, body)
    if not _eq(claim.digest, gateway_phone_digest(body)):
        raise PhoneGatewayError("bad_request", 400, "The request does not match the signed claim.")
    if kind in _READ_KINDS:
        _tuple_live(claim, guard)
        phone = receiver.gateway_identity(claim)
        _precheck(receiver, kind, body, claim.phone_id)
        if kind == "effect_status":
            result = receiver._gateway_status(phone, body)
        else:
            result = _perform(receiver, phone, kind, body)
            if kind != "session_frame":
                result = _project(kind, result)
        _tuple_live(claim, guard)
        return {"operation_id": claim.operation_id, "kind": kind, "duplicate": False,
                "target": claim.target, "result": result}
    if kind not in _EFFECT_KINDS:
        raise PhoneGatewayError("bad_request", 400, "Unknown request.")
    if not _real_guard(guard):
        raise PhoneGatewayError("unavailable", 503, "The computer has no checked admission verifier.")
    _ticket_matches(claim, signed_operation, admission_ticket, guard)
    if claim.kind == "session_input" and signed_operation.get("session_id") != body.get("session"):
        raise PhoneGatewayError("bad_request", 400, "The admission does not match the session.")
    _tuple_live(claim, guard)
    phone = receiver.gateway_identity(claim)
    _precheck(receiver, kind, body, claim.phone_id)
    replay = receiver._gateway_reserve(claim)
    if replay is not None:
        return replay
    box = []

    def sink():
        try:
            box.append(_perform(receiver, phone, kind, body, claim.operation_id))
        except PhoneGatewayError as exc:
            box.append(exc)
        except Reply as exc:
            message = exc.body.get("error") if isinstance(exc.body, dict) else "Refused."
            box.append(PhoneGatewayError(_reply_code(exc.status), exc.status, clean_text(message, 300) or "Refused."))

    AuthorityError = _authority_error()
    try:
        guard.accept(signed_operation, admission_ticket, sink)
    except AuthorityError as exc:
        code = exc.code if exc.code in ("signature", "unauthorized", "forbidden", "bad_request", "stale",
                                        "conflict", "uncertain", "ticket_expired", "no_admission") else "unavailable"
        status = {"signature": 401, "unauthorized": 401, "forbidden": 403, "bad_request": 400,
                  "stale": 409, "conflict": 409, "uncertain": 409, "ticket_expired": 410,
                  "no_admission": 409}.get(code, 503)
        error = PhoneGatewayError(code if status != 503 else "unavailable", status,
                                  "The computer did not admit that request. Nothing was changed.")
        receiver._gateway_settle(claim, error)
        raise error from None
    except PhoneGatewayError as exc:
        receiver._gateway_settle(claim, exc)
        raise
    except Exception:
        error = PhoneGatewayError("uncertain", 409, "The outcome is unknown. It was not started again.")
        receiver._gateway_settle(claim, error)
        raise error from None
    if not box:
        error = PhoneGatewayError("uncertain", 409, "The outcome is unknown. It was not started again.")
        receiver._gateway_settle(claim, error)
        raise error
    outcome = box[0]
    if isinstance(outcome, PhoneGatewayError):
        receiver._gateway_settle(claim, outcome)
        raise outcome
    if kind == "session_stop":
        try:
            guard.stop_interactive(body["session"], "phone_release")
        except AuthorityError:
            error = PhoneGatewayError("uncertain", 409, "The session release outcome is unknown.")
            receiver._gateway_settle(claim, error)
            raise error from None
    projected = _project(kind, outcome)
    receiver._gateway_settle(claim, projected)
    return {"operation_id": claim.operation_id, "kind": kind, "duplicate": False,
            "target": claim.target, "result": projected}


def accept_gateway_phone(claim, kind, operation_id, body, *, guard, signed_operation=None,
                         admission_ticket=None, receiver=None):
    """Admit one signed phone request on this PC.

    The return stays in-process. ``GatewayPhoneReceiver.receive`` is the
    gateway JSON adapter. Reads call ``authority.status`` before and after the
    handler and never call ``guard.accept``. Mutations call ``guard.accept``
    before the handler. ``claim`` must be a ``GatewayPhoneClaim`` from
    ``GatewayPhoneVerifier``.
    """
    return _accept_gateway_phone(
        claim, kind, operation_id, body, guard=guard, signed_operation=signed_operation,
        admission_ticket=admission_ticket, receiver=receiver)


class GatewayPhoneReceiver:
    """Strict adapter WS5 calls with the gateway's transient relay object.

    No socket, no global registration, no cookie, no X-Forwarded-For.
    """

    def __init__(self, server, ingress_public_key, *, device_id, guard=None):
        if not isinstance(server, PhoneServer):
            raise PhoneGatewayError("unavailable", 503, "The phone receiver is not configured.")
        self.server = server
        self.verifier = GatewayPhoneVerifier(ingress_public_key)
        self.device_id = _ident(device_id, "device_id")
        self.guard = guard

    def receive(self, relay, *, signed_operation=None, admission_ticket=None):
        if not isinstance(relay, dict) or set(relay) != _RELAY_KEYS:
            raise PhoneGatewayError("bad_request", 400, "The relay was refused.")
        _banned(relay)
        if relay.get("capability") != "client":
            raise PhoneGatewayError("forbidden", 403, "The relay was refused.")
        kind = relay.get("kind")
        if kind not in _READ_KINDS and kind not in _EFFECT_KINDS:
            raise PhoneGatewayError("bad_request", 400, "Unknown request.")
        raw = _envelope_bytes(relay.get("envelope_b64"))
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not _eq(digest, relay.get("digest")):
            raise PhoneGatewayError("bad_request", 400, "The relay does not match the envelope.")
        claim = self.verifier.verify(raw)
        body = self._body(raw)
        self._match_relay(relay, claim)
        if self.device_id != claim.target or relay.get("target") != claim.target:
            raise PhoneGatewayError("forbidden", 403, "The relay is for a different computer.")
        internal = accept_gateway_phone(
            claim, kind, claim.operation_id, body, guard=self.guard,
            signed_operation=signed_operation, admission_ticket=admission_ticket,
            receiver=self.server)
        return _gateway_wire(kind, internal)

    def _body(self, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.") from None
        body = data.get("body")
        if not isinstance(body, dict):
            raise PhoneGatewayError("bad_request", 400, "Send a JSON object.")
        return body

    def _match_relay(self, relay, claim):
        if not _eq(relay.get("operation_id"), claim.operation_id):
            raise PhoneGatewayError("conflict", 409, "The relay does not match the signed claim.")
        if not _eq(relay.get("attempt_id"), claim.attempt_id) or relay.get("kind") != claim.kind:
            raise PhoneGatewayError("conflict", 409, "The relay does not match the signed claim.")
        if relay.get("main") != claim.main or relay.get("authority_id") != claim.authority_id:
            raise PhoneGatewayError("stale", 409, "Authority moved on. Nothing was changed.")
        if relay.get("incarnation") != claim.incarnation or relay.get("epoch") != claim.epoch:
            raise PhoneGatewayError("stale", 409, "Authority moved on. Nothing was changed.")


def _envelope_bytes(value):
    if not isinstance(value, str) or len(value) > (MAX_BODY * 4 // 3) + 8:
        raise PhoneGatewayError("bad_request", 400, "That request is too large.")
    try:
        import base64
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        raise PhoneGatewayError("bad_request", 400, "That request cannot be checked.") from None
    if not raw or len(raw) > MAX_BODY:
        raise PhoneGatewayError("bad_request", 400, "That request is too large.")
    return raw
