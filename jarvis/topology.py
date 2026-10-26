"""Explicit main/node roles and request origin, independent of any transport."""

import re
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

ORIGIN = ContextVar("jarvis_origin_device", default=None)
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def enabled(config):
    return bool(config.get("cluster", {}).get("enabled", False))


def identity(config):
    """(local, main). Unknown identity fails closed rather than creating a new brain."""
    settings = config.get("cluster", {})
    devices = config.get("devices", {})
    main = settings.get("main_device")
    local = settings.get("local_device", "auto")
    if local == "auto":
        host = socket.gethostname().casefold()
        matches = [name for name, d in devices.items()
                   if str(d.get("hostname", "")).casefold() == host]
        if len(matches) != 1:
            raise ValueError("Set cluster.local_device to this PC's device ID; hostname is not unique or known.")
        local = matches[0]
    for label, value in (("main_device", main), ("local_device", local)):
        if not isinstance(value, str) or not DEVICE_ID.fullmatch(value) or value not in devices:
            raise ValueError(f"cluster.{label} must name a configured device.")
    return local, main


def is_node(config):
    return enabled(config) and identity(config)[0] != identity(config)[1]


def endpoint(config):
    value = str(config.get("cluster", {}).get("main_url", "http://127.0.0.1:7778")).rstrip("/")
    parts = urlsplit(value)
    if (parts.username or parts.password or parts.query or parts.fragment
            or parts.path or not parts.hostname or parts.port == 0
            or (parts.scheme != "https" and not (parts.scheme == "http" and
                    parts.hostname in ("127.0.0.1", "localhost", "::1")))):
        raise ValueError("cluster.main_url must be an HTTPS origin or loopback HTTP origin (for an SSH tunnel).")
    return value


@contextmanager
def request_from(device):
    marker = ORIGIN.set(device)
    try:
        yield
    finally:
        ORIGIN.reset(marker)
