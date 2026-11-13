"""Phone front: one stable origin that forwards the phone PWA to whichever PC holds MAIN.

Runs on the Oracle server behind `tailscale serve` (https://<oracle>.ts.net:8443 ->
127.0.0.1:8767). Every request asks `status_fn()` who holds the MAIN lease right now and
relays to that PC's phone origin, so a MAIN change shows up on the very next request.
No MAIN, or an unreachable one, is a retryable 503/502 and never a guess.

Standard library only: the Oracle runs `/usr/bin/python3` with no venv. Nothing here
logs a request line, header or body (cookies stay out of every log).
"""

import http.client
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 1 << 20
CHUNK = 16384
RETRY_AFTER = 2

_REQUEST_HEADERS = (
    "cookie", "content-type", "accept", "origin", "referer", "user-agent", "x-csrf-token",
    "if-none-match", "if-modified-since",
)
_RESPONSE_HEADERS = (
    "Content-Type", "Cache-Control", "ETag", "Last-Modified", "Location", "Retry-After",
    "Service-Worker-Allowed", "Content-Security-Policy", "X-Content-Type-Options",
    "Referrer-Policy",
)
_TS_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.ts\.net")
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _check_origin(value):
    """`https://<name>.ts.net[:port]`, or loopback `http://127.0.0.1|localhost:<port>`."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("bad upstream origin")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        host = parts.hostname or ""
    except ValueError:
        raise ValueError("bad upstream origin") from None
    if (parts.path or parts.query or parts.fragment or parts.username is not None
            or parts.password is not None or value.endswith("/")):
        raise ValueError("bad upstream origin")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("bad upstream origin")
    if parts.scheme == "https" and _TS_HOST.fullmatch(host):
        pass
    elif parts.scheme == "http" and host in ("127.0.0.1", "localhost") and port is not None:
        pass
    else:
        raise ValueError("bad upstream origin")
    netloc = host + (f":{port}" if port is not None else "")
    return f"{parts.scheme}://{netloc}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _routable(path):
    """Only /phone and what sits under it. No dot segments."""
    bare = path.split("?", 1)[0].split("#", 1)[0]
    if bare != "/phone" and not bare.startswith("/phone/"):
        return False
    if "\\" in bare:
        return False
    decoded = urllib.parse.unquote(bare)
    return not any(seg in (".", "..") for seg in decoded.split("/")) and "\\" not in decoded


class PhoneFront:
    def __init__(self, status_fn, upstreams, *, bind_host="127.0.0.1", bind_port=8767,
                 timeout=30.0, opener=None):
        if not callable(status_fn) or not isinstance(upstreams, dict) or not upstreams:
            raise ValueError("bad phone front config")
        self.status_fn = status_fn
        self.upstreams = {}
        for device, origin in upstreams.items():
            if not isinstance(device, str) or not _DEVICE.fullmatch(device):
                raise ValueError("bad upstream device")
            self.upstreams[device] = _check_origin(origin)
        if bind_host != "127.0.0.1" and bind_host != "localhost":
            raise ValueError("phone front binds loopback only")
        if isinstance(bind_port, bool) or not isinstance(bind_port, int) or not 0 <= bind_port <= 65535:
            raise ValueError("bad bind port")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("bad timeout")
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.timeout = float(timeout)
        self.opener = opener or _default_opener()
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()

    # --- target ------------------------------------------------------------------------

    def target(self):
        """(device, origin) for the MAIN right now, or None. Asked fresh on every request."""
        try:
            status = self.status_fn()
        except Exception:
            return None
        if not isinstance(status, dict) or status.get("lease_valid") is not True:
            return None
        main = status.get("main")
        if not isinstance(main, str) or main not in self.upstreams:
            return None
        return main, self.upstreams[main]

    # --- lifecycle ---------------------------------------------------------------------

    def start(self):
        with self._lock:
            if self._httpd is not None:
                raise RuntimeError("phone front already started")
            front = self

            class Handler(_Handler):
                owner = front

            httpd = ThreadingHTTPServer((self.bind_host, self.bind_port), Handler)
            httpd.daemon_threads = True
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                      name="phone-front", daemon=True)
            self._httpd, self._thread = httpd, thread
            thread.start()
            host, port = httpd.server_address[:2]
            self.bind_port = port
            return f"http://{host}:{port}"

    @property
    def origin(self):
        httpd = self._httpd
        if httpd is None:
            return None
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        with self._lock:
            httpd, thread = self._httpd, self._thread
            self._httpd = self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=3)


class _Handler(BaseHTTPRequestHandler):
    owner = None
    protocol_version = "HTTP/1.1"
    server_version = "JarvisPhoneFront"
    sys_version = ""

    def log_message(self, *args):  # nothing sensitive is ever logged
        pass

    # --- replies -----------------------------------------------------------------------

    def _json(self, status, body, *, headers=None, close=False):
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _unavailable(self, status, message, code):
        self._json(status, {"error": message, "code": code, "wait": RETRY_AFTER},
                   headers={"Retry-After": str(RETRY_AFTER)})

    # --- methods -----------------------------------------------------------------------

    def do_GET(self):
        self._handle()

    do_HEAD = do_GET
    do_POST = do_GET

    def _other(self):
        self._json(405, {"error": "Method not allowed.", "code": "method"},
                   headers={"Allow": "GET, HEAD, POST"}, close=True)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other

    def _handle(self):
        path = self.path
        if path == "/" or path.startswith("/?"):
            self.send_response(302)
            self.send_header("Location", "/phone/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not _routable(path):
            self._json(404, {"error": "Not found.", "code": "not_found"})
            return
        body = None
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding"):
                self._json(411, {"error": "Length required.", "code": "length"}, close=True)
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length < 0:
                self._json(400, {"error": "Bad request.", "code": "bad_request"}, close=True)
                return
            if length > MAX_BODY:
                self._json(413, {"error": "Request too large.", "code": "too_large"}, close=True)
                return
            body = self.rfile.read(length) if length else b""
        found = self.owner.target()
        if found is None:
            self._unavailable(503, "No ready MAIN. Waiting to reconnect.", "no_main")
            return
        self._forward(*found, body)

    # --- relay -------------------------------------------------------------------------

    def _forward(self, device, origin, body):
        headers = {}
        for name in self.headers.keys():
            low = name.lower()
            if low in ("x-jarvis-front", "x-jarvis-front-main"):
                continue
            if low in _REQUEST_HEADERS or low.startswith("x-jarvis-"):
                headers[name] = self.headers[name]
        host = self.headers.get("Host")
        if host:
            headers["Host"] = host
            headers["X-Forwarded-Host"] = host
        headers["X-Jarvis-Front"] = "1"
        headers["X-Jarvis-Front-Main"] = device
        request = urllib.request.Request(origin + self.path, data=body, headers=headers,
                                         method=self.command)
        try:
            response = self.owner.opener.open(request, timeout=self.owner.timeout)
        except urllib.error.HTTPError as exc:
            response = exc  # 3xx (not followed), 4xx and 5xx pass straight through
        except (http.client.InvalidURL, ValueError):
            self._json(400, {"error": "Bad request.", "code": "bad_request"}, close=True)
            return
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            self._unavailable(502, "The MAIN connection is unavailable. Waiting to reconnect.",
                              "no_relay")
            return
        try:
            self._relay(response, device)
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _relay(self, response, device):
        info = response.headers
        status = getattr(response, "status", None) or response.getcode()
        length = info.get("Content-Length")
        try:
            length = int(length) if length is not None else None
        except ValueError:
            length = None
        if length is not None and length < 0:
            length = None
        self.send_response(status)
        for name in _RESPONSE_HEADERS:
            for value in info.get_all(name) or ():
                self.send_header(name, value)
        for value in info.get_all("Set-Cookie") or ():
            self.send_header("Set-Cookie", value)
        self.send_header("X-Jarvis-Main", device)
        no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        if length is not None:
            self.send_header("Content-Length", str(length))
        elif not no_body:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if no_body:
            return
        sent = 0
        try:
            while length is None or sent < length:
                want = CHUNK if length is None else min(CHUNK, length - sent)
                chunk = response.read1(want) if hasattr(response, "read1") else response.read(want)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except (OSError, http.client.HTTPException, socket.timeout):
            self.close_connection = True
            return
        if length is not None and sent < length:
            self.close_connection = True  # upstream cut short: do not reuse the connection
