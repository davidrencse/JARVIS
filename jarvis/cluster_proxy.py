"""Loopback console on a node; all API state and model work belong to the main."""

import json
import urllib.error

from .cluster import Client, Hub, NodeWorker, _HUBS
from .cluster_store import OWNER_TOKEN
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


STOP_GRACE = 15.0  # s a SIGTERM'd main gets to close its runtime before it is cut off


def close_on_sigterm():
    """systemd stops jarvis-cluster (so does `jarvis sleep`) with SIGTERM, whose default action
    skips every `finally`: Runtime.close never ran, and keys or buttons a phone session on the
    main held stayed down until the next start. Raise SystemExit instead, so serve()'s finally
    closes the runtime. A close that hangs is cut off after STOP_GRACE, as SIGTERM used to cut
    it at once. Returns what puts the default back, or None when nothing was set (not the main
    thread, or a handler someone else set)."""
    import os
    import signal
    import sys
    import threading

    def stop(*_):
        timer = threading.Timer(STOP_GRACE, os._exit, args=(0,))
        timer.daemon = True
        timer.start()
        sys.exit(0)

    try:
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
            return None
        signal.signal(signal.SIGTERM, stop)
    except (ValueError, OSError, AttributeError):
        return None

    def restore():
        try:
            if signal.getsignal(signal.SIGTERM) is stop:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    return restore


class Runtime:
    def __init__(self, console=None, hub=None, worker=None, phone=None):
        self.console, self.hub, self.worker, self.phone = console, hub, worker, phone
        self.untrap = None  # close_on_sigterm's undo, on the main

    def close(self):
        # Closing already: a second SIGTERM ends the process at once, as it always did.
        if self.untrap:
            self.untrap()
            self.untrap = None
        # The phone server first: no phone request may arrive while the hub is going away.
        if self.phone:
            try:
                self.phone.close()
            except Exception:
                pass
            self.phone = None
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
            if not self._allowed() or not self._post_ok():
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
                    # Another process here redeemed the invite: reload the token and retry once
                    # (only without a body; a streamed upload can't be sent twice).
                    if exc.code == 403 and not length and getattr(client, "invite", False) \
                            and client._reload():
                        try:
                            response = client.open(self.path, None, timeout=60, headers=headers,
                                                   method="POST" if post else "GET")
                        except urllib.error.HTTPError as again:
                            response = again
                self.send_response(response.status)
                for name in ("Content-Type", "Content-Length", "Cache-Control",
                             "Content-Disposition",
                             "X-Frame-Seq", "X-Frame-Width", "X-Frame-Height", "X-Node-Ago"):
                    if response.headers.get(name):
                        self.send_header(name, response.headers[name])
                # Stamped here, not copied: the main's reply must not choose framing or CORS.
                self._stamp_security(response.headers.get("Content-Type") or "")
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
    owner_config = config
    if running and getattr(running, 'server', None) is not None:
        import copy
        owner_config = copy.deepcopy(config)
        host, listen_port = running.server.server_address[:2]
        loopback = '[::1]' if ':' in host else '127.0.0.1'
        owner_config.setdefault('cluster', {}).update(
            main_device=running.main, main_url=f'http://{loopback}:{listen_port}')
    client = None
    if (Path(state) / OWNER_TOKEN).is_file():
        try:
            candidate = Client(owner_config, state, owner=True)
            candidate.request("/v1/status", timeout=1)
            client = candidate
        except (OSError, RuntimeError, ValueError):
            pass
    if running or client:
        client = client or Client(owner_config, state, owner=True)
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
        server = ConsoleServer(("127.0.0.1", port), handler)
        # Only here, next to the in-process hub: the phone app needs it. Off or unable to
        # bind, phone.start says so in the log and returns None; the console comes up anyway.
        from . import phone
        runtime.phone = phone.start(config, state, console=console, hub=hub)
        # A systemd stop then runs Runtime.close too, not only Ctrl+C: the hub lets go of the
        # main's own sessions first.
        runtime.untrap = close_on_sigterm()
        return server, runtime
    except BaseException:
        runtime.close()
        raise
