"""Owner-only process bridge on the existing hub socket; never an authority fallback."""
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_main(config, state, device):
    from jarvis.cluster import Client
    from jarvis.topology import endpoint
    # Do not send the owner token to a configured remote origin or a DNS alias.
    if not _loopback(urlsplit(endpoint(config)).hostname):
        raise ValueError("MAIN control requires a loopback cluster.main_url.")
    return Client(config, state, owner=True).request(
        "/v1/continuity/main", {"device": device}, timeout=600)


def run_main(hub, data, peer):
    from jarvis import cluster_cli
    if not _loopback(peer):
        raise PermissionError("MAIN control is loopback only.")
    if set(data) != {"device"} or not isinstance(data["device"], str):
        raise ValueError("MAIN control requires exactly a device id.")
    # Explicitly refuse before set_main: otherwise its process bridge would recurse
    # into this same server when production has no runtime adapters.
    if cluster_cli.configured_handoff(hub.state) is None:
        raise ValueError("Safe handoff has no bound managed runtime.")
    return cluster_cli.set_main(hub.config, hub.base / "jarvis.config.json",
                                data["device"], state=hub.state)


def operator_configured(config, base):
    """Validate optional bridge opt-in locally; never contact the authority."""
    from .cli import build_client
    from .gateway import GatewayError
    from jarvis.topology import identity
    try:
        raw = config.get("continuity", {}).get("operator_config")
        if not isinstance(raw, str) or not raw:
            return False
        path = Path(raw).expanduser()
        client, _ = build_client(path if path.is_absolute() else Path(base) / path)
        return client.device_id == identity(config)[0]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, GatewayError):
        return False


def request_recover(config, state, device, request_id):
    from jarvis.cluster import Client
    from jarvis.topology import endpoint
    from .runtime import _ident_ok
    if not _ident_ok(request_id) or not _ident_ok(device):
        raise ValueError("Invalid manual recovery request.")
    if not _loopback(urlsplit(endpoint(config)).hostname):
        raise ValueError("Recovery control requires a loopback cluster.main_url.")
    return Client(config, state, owner=True).request('/v1/continuity/recover',
        {'device': device, 'request_id': request_id}, timeout=600)


def run_recover(hub, data, peer):
    from .runtime import _ident_ok
    if not _loopback(peer):
        raise PermissionError("Recovery control is loopback only.")
    if (set(data) != {'device', 'request_id'} or data['device'] != hub.local or
            not _ident_ok(data['request_id'])):
        raise ValueError("Recovery control requires the local device and request id.")
    recovery = getattr(hub, 'continuity_recovery', None)
    if not callable(recovery):
        raise ValueError("Production recovery adapters are not bound; no lease was requested.")
    from .runtime import RuntimeRefusal, LinkError
    from .authority import AuthorityError
    try:
        return recovery(data['device'], data['request_id'])
    except (RuntimeRefusal, LinkError, AuthorityError) as exc:
        # A partition may follow a committed grant/complete. Never invent no_main
        # or automatically retry; let the operator inspect authenticated status.
        return {'ok': False, 'status': 'unconfirmed', 'code': exc.code}

