"""Loopback console on a node; all API state and model work belong to the main."""

import json
import urllib.error

from .cluster import Client, Hub, NodeWorker, _HUBS
from .topology import is_node


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


VOICE_SETUP = ("/api/voice/setup", "/api/voice/devices")
PERIPHERALS = "/api/peripherals"


def proxy_handler(client, overlay=None, setup=None, local=None):
    """`setup` is this laptop's own VoiceSetup: a voiceprint belongs to the mic it was
    recorded on, so voice setup is answered here, never forwarded to the main.
    `local` is (config, config path, state): Connect & share is answered here too, so a
    node whose link is down can still show its own checks and take an invite code."""
    from .ui import Handler

    class ProxyHandler(Handler):
        hosts = ("127.0.0.1", "localhost", "::1")

        def do_GET(self):
            if not self._allowed():
                return self._send(403, {"error": "Local console requests only."})
            if setup is not None and self.path in VOICE_SETUP:
                try:
                    return self._send(200, setup.status() if self.path == VOICE_SETUP[0]
                                      else {"devices": setup.devices()})
                except Exception as exc:
                    return self._send(500, {"error": str(exc)})
            if local is not None and self.path == PERIPHERALS:
                from .peripherals import overview
                try:
                    return self._send(200, overview(*local))
                except Exception as exc:
                    return self._send(500, {"error": str(exc)})
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
            if setup is not None and self.path == VOICE_SETUP[0]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Invalid voice setup request length.")
                    body = json.loads(self.rfile.read(length))
                    action = body.get("action")
                    if action == "start":
                        return self._send(200, setup.start(body.get("device") or None))
                    if action == "cancel":
                        return self._send(200, setup.cancel())
                    if action == "autostart":
                        return self._send(200, setup.autostart(bool(body.get("on"))))
                    raise ValueError("action must be start, cancel or autostart.")
                except (ValueError, AttributeError, RuntimeError) as exc:
                    return self._send(400, {"error": str(exc)})
            if local is not None and self.path == PERIPHERALS:
                from .peripherals import act
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        raise ValueError("Invalid request length.")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("Request body must be a JSON object.")
                    return self._send(200, act(*local, body))
                except Exception as exc:
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
        from .onboard import VoiceSetup
        try:
            return ConsoleServer(("127.0.0.1", port), proxy_handler(
                client, overlay, VoiceSetup(state, config),
                (config, Path(base) / "jarvis.config.json", state))), runtime
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
