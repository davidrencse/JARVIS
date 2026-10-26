"""Loopback console on a node; all API state and model work belong to the main."""

import json
import urllib.error

from .cluster import Client, Hub, NodeWorker, _HUBS
from .topology import identity, is_node


class _Body:
    def __init__(self, stream, length):
        self.stream, self.left = stream, length

    def read(self, size=-1):
        if not self.left:
            return b""
        block = self.stream.read(min(self.left, size if size >= 0 else self.left))
        if not block:
            raise ValueError("The proxy upload ended early.")
        self.left -= len(block)
        return block


class Runtime:
    def __init__(self, console=None, hub=None, worker=None):
        self.console, self.hub, self.worker = console, hub, worker

    def close(self):
        if self.worker:
            self.worker.close()
        if self.hub:
            self.hub.close()
        if self.console:
            self.console.close()


def proxy_handler(client, overlay=None):
    from .ui import Handler

    class ProxyHandler(Handler):
        hosts = ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._allowed():
                return self._send(403, {"error": "Local console requests only."})
            if self.path.startswith("/api/"):
                return self._forward(False)
            return self._static(self.path.split("?", 1)[0])

        def do_POST(self):
            if not self._allowed() or self.headers.get("X-JARVIS") != "1":
                return self._refuse()
            if self.path == "/api/overlay":
                if overlay is None:
                    return self._send(404, {"error": "This window is not an overlay."})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Invalid overlay request length.")
                    body = json.loads(self.rfile.read(length))
                    return self._send(200, overlay.request(body.get("action") or "hide"))
                except (ValueError, AttributeError) as exc:
                    return self._send(400, {"error": str(exc)})
            if not self.path.startswith("/api/"):
                return self._send(404, {"error": "Unknown console endpoint."})
            return self._forward(True)

        def _forward(self, post):
            response = None
            sent = False
            try:
                headers = {"X-JARVIS": "1"}
                for name in ("Content-Type", "Content-Length", "Last-Event-ID"):
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                length = int(self.headers.get("Content-Length", "0")) if post else 0
                if not 0 <= length <= 2 * 1024 ** 3:
                    raise ValueError("Invalid proxy request length.")
                self.connection.settimeout(60)
                try:
                    response = client.open(self.path, _Body(self.rfile, length) if post else None,
                                           timeout=60, headers=headers, method="POST" if post else "GET")
                except urllib.error.HTTPError as exc:
                    response = exc  # forward the main's structured error and status
                self.send_response(response.status)
                for name in ("Content-Type", "Content-Length", "Cache-Control",
                             "Content-Disposition", "X-Content-Type-Options"):
                    if response.headers.get(name):
                        self.send_header(name, response.headers[name])
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                sent = True
                while True:
                    block = response.read1(65536)
                    if not block:
                        break
                    self.wfile.write(block)
                    self.wfile.flush()
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                if not sent:
                    self._send(502, {"error": f"Main PC unavailable: {exc}"})
            finally:
                self.close_connection = True
                if response:
                    response.close()

    return ProxyHandler


def local_server(config, base, state, port, overlay=None):
    """Create a main runtime, or attach a node/extra window to the existing main."""
    from pathlib import Path
    from .ui import Console, ConsoleServer, Handler
    if is_node(config):
        client = Client(config, state)
        worker = NodeWorker(config, state).start()
        runtime = Runtime(worker=worker)
        try:
            return ConsoleServer(("127.0.0.1", port), proxy_handler(client, overlay)), runtime
        except BaseException:
            runtime.close()
            raise
    # Extra local windows and overlays must share the existing main brain too.
    running = _HUBS.get(str(Path(state).resolve()))
    client = None
    if (Path(state) / "cluster-owner.token").is_file():
        candidate = Client(config, state, owner=True)
        try:
            candidate.request("/v1/status", timeout=1)
            client = candidate
        except (OSError, RuntimeError):
            pass
    if running or client:
        client = client or Client(config, state, owner=True)
        return ConsoleServer(("127.0.0.1", port), proxy_handler(client, overlay)), Runtime()
    console = Console(config, base, state).start_live()
    hub = Hub(config, base, state, console=console)
    runtime = Runtime(console=console, hub=hub)
    try:
        hub.start()
        if overlay:
            console.overlay = overlay
        handler = type("MainConsoleHandler", (Handler,), {
            "console": console, "hosts": ("127.0.0.1", "localhost", "::1")})
        return ConsoleServer(("127.0.0.1", port), handler), runtime
    except BaseException:
        runtime.close()
        raise
