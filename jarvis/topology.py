"""Explicit main/node roles and request origin, independent of any transport.

Legacy-only compatibility module: the implementation lives in `jarvis.fleet.legacy_topology`
(a behavior-preserving facade, mobile-continuity P1). Public names remain available to existing callers. Re-exported functions retain the
implementation module's globals; replacing a shim attribute does not change those globals.
`is_node` deliberately composes the shim's `enabled`/`identity` so patches still apply to it.
"""

import socket  # noqa: F401  (tests patch `jarvis.topology.socket.gethostname`)

from .fleet.legacy_topology import (  # noqa: F401
    APPROVAL_ORIGIN, DEFAULT_PORT, DEVICE_ID, ORIGIN, Roles,
    approvals_on, confirmation_station, enabled, endpoint, identity, request_from, roles,
)


def is_node(config):
    return enabled(config) and identity(config)[0] != identity(config)[1]
