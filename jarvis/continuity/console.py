"""Read-only gateway line for the console's Computers section.

Returns origin host, mode, main, lease_valid and epoch only. Never key paths, key material or
the raw error text. One real call per 30 s however often the console polls."""
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PATH = "~/.config/jarvis-continuity/operator.json"
TTL = 30.0
TIMEOUT = 5.0
_CACHE = {}
# One fetch per operator path. A waiter that times out does not publish, so its
# Timeout cannot replace a result the owner is still writing.
_INFLIGHT = {}
_LOCK = threading.Lock()


def _read(path, factory):
    client, cfg = factory(path)
    host = urlparse(str(cfg.get("origin", ""))).hostname or ""
    out = {"state": "ok", "host": host}
    try:
        s = client.status()
        out.update(mode=s.get("mode"), main=s.get("main"), lease_valid=bool(s.get("lease_valid")),
                   epoch=s.get("epoch"))
    except Exception as exc:
        out.update(state="unreachable", error=type(exc).__name__)
    return out


def _fetch(path, factory, timeout):
    if not path.is_file():
        return {"state": "not_set_up"}
    if factory is None:
        from .cli import build_client as factory
    box = {}

    def work():
        try:
            box["v"] = _read(path, factory)
        except Exception as exc:
            box["v"] = {"state": "unreachable", "error": type(exc).__name__}

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    return box.get("v") or {"state": "unreachable", "error": "Timeout"}


def gateway_status(config, factory=None, now=None, timeout=TIMEOUT):
    path = Path(str(((config or {}).get("continuity") or {}).get("operator_config") or DEFAULT_PATH)).expanduser()
    now = time.monotonic() if now is None else now
    key = str(path)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
        slot = _INFLIGHT.get(key)
        if slot is None:
            token = object()
            event = threading.Event()
            _INFLIGHT[key] = (token, event)
            owner = True
        else:
            token, event = slot
            owner = False
    if not owner:
        # This caller does not own the slot, so a timeout stays local.
        if not event.wait(timeout):
            return {"state": "unreachable", "error": "Timeout"}
        with _LOCK:
            hit = _CACHE.get(key)
            if hit and now - hit[0] < TTL:
                return hit[1]
        return {"state": "unreachable", "error": "Timeout"}
    try:
        out = _fetch(path, factory, timeout)
    except Exception as exc:
        out = {"state": "unreachable", "error": type(exc).__name__}
    with _LOCK:
        current = _INFLIGHT.get(key)
        if current is not None and current[0] is token:
            _CACHE[key] = (now, out)
            _INFLIGHT.pop(key, None)
        event.set()
    return out
