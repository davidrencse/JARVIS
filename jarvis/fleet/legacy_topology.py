"""Legacy (config-driven) fleet topology, behind a narrow facade. P1: parity only.

`jarvis.topology` re-exports the existing public API. Shared ContextVars and the socket
module retain their identity; replacing a shim attribute does not replace these globals.

What this is NOT: `cluster.main_device` is a static configuration value. It is not a lease,
not an epoch-fenced authority and not proof that exactly one MAIN is acting. Protocol
enforcement (stable identity, authority, tickets, fencing) belongs to later phases; code
must not treat anything here as a safety guarantee.

Four roles that the legacy code keeps in different places are named apart here:

  coordinator       config `cluster.main_device`: who the hub/brain runs on today
  local             this PC (`cluster.local_device`, or the one device whose hostname matches)
  origin (caller)   `ORIGIN`: the node/interface a request came from, set by `request_from`
  approval station  `APPROVAL_ORIGIN`: where confirmations park, set by `approvals_on`
  execution target  the device an action runs on; legacy rule: explicit device, else origin,
                    else local (the rule `remote.Remote.call` applies inline)
"""

import re
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

ORIGIN = ContextVar("jarvis_origin_device", default=None)
# A mobile request can target a node while approval remains on the main PC.
APPROVAL_ORIGIN = ContextVar("jarvis_approval_origin", default=None)
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# The hub's port. 7778 is taken by the Grok Bot tunnel's forward, so never default to it.
DEFAULT_PORT = 7780


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


def endpoint(config):
    value = str(config.get("cluster", {}).get("main_url", f"http://127.0.0.1:{DEFAULT_PORT}")).rstrip("/")
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


@contextmanager
def approvals_on(device):
    marker = APPROVAL_ORIGIN.set(device)
    try:
        yield
    finally:
        APPROVAL_ORIGIN.reset(marker)


def _station(approval, origin):
    return approval or origin


def confirmation_station(origin):
    """Where a confirmation parks: the `approvals_on` override, else `origin`.

    `origin` is the caller's already-resolved origin (e.g. pipeline's explicit `origin` argument,
    which may differ from the ORIGIN ContextVar, or the local PC when the cluster is off). No
    cluster identity is read, so this is safe on standalone installs."""
    return _station(APPROVAL_ORIGIN.get(), origin)


@dataclass(frozen=True)
class Roles:
    """Read-only snapshot of the legacy roles. Descriptive only; grants nothing."""
    local: str
    coordinator: str                 # legacy cluster.main_device; NOT a lease
    origin: Optional[str]            # caller identity for this request, if any
    approval_station: Optional[str]  # explicit approval override, if any

    @property
    def is_coordinator(self):
        return self.local == self.coordinator

    def execution_target(self, explicit=None):
        """Legacy default target: explicit device, else the request's origin, else this PC."""
        return explicit or self.origin or self.local

    def confirmation_station(self):
        """Where legacy confirmations park: the approval override, else the origin, else this PC."""
        return _station(self.approval_station, self.origin or self.local)


def roles(config):
    """Roles for the current request. Raises like `identity` when the cluster is unconfigured;
    callers that may run with the cluster off should check `enabled` first."""
    local, main = identity(config)
    return Roles(local, main, ORIGIN.get(), APPROVAL_ORIGIN.get())
