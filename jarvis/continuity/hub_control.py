"""Owner-only process bridge on the existing hub socket; never an authority fallback."""
import ipaddress
import json
import os
import re
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


_RECEIPT_NAME = "continuity-main-receipt.json"
_RECEIPT_SCHEMA = 1
_RECEIPT_MAX = 4096
_RECEIPT_FIELDS = ("schema", "target", "publication", "ok", "status", "code",
                   "transition_id", "main", "epoch", "incarnation", "mutated")
_PUBLICATIONS = {"intent", "pending", "authority_granted", "publication_failed", "published",
                 "conflict", "unconfirmed", "not_ready", "failed", "outcome_unknown",
                 "reconcile_required"}
_OPEN_RECEIPT = {"intent", "pending", "authority_granted", "publication_failed",
                 "conflict", "unconfirmed", "not_ready", "outcome_unknown",
                 "reconcile_required"}
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RECEIPT_CODE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_RECEIPT_REFUSAL = (
    "The MAIN switch receipt is unusable. It was not replaced and no switch was started.")


def _receipt_path(state):
    return Path(state) / _RECEIPT_NAME


def _int_field(value):
    return not isinstance(value, bool) and isinstance(value, int)


def _clean_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or _RECEIPT_ID.fullmatch(value) is None:
        raise ValueError(_RECEIPT_REFUSAL)
    return value


def _fsync_directory(folder):
    """POSIX directory entry. Windows keeps the file fsync inside the replacement."""
    if os.name == "nt":
        return
    descriptor = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_receipt(state):
    """Return {} only when the receipt file is confirmed absent.

    A symlink, a torn file, an unknown schema, or a field this coordinator does
    not own is refused. The file is left in place and the caller must not post.
    """
    path = _receipt_path(state)
    if not os.path.lexists(path):
        return {}
    if path.is_symlink():
        raise ValueError(_RECEIPT_REFUSAL)
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(_RECEIPT_REFUSAL) from exc
    if not info.st_size or info.st_size > _RECEIPT_MAX:
        raise ValueError(_RECEIPT_REFUSAL)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(_RECEIPT_REFUSAL) from exc
    if not isinstance(raw, dict) or any(key not in _RECEIPT_FIELDS for key in raw):
        raise ValueError(_RECEIPT_REFUSAL)
    if raw.get("schema") != _RECEIPT_SCHEMA:
        raise ValueError(_RECEIPT_REFUSAL)
    record = {
        "schema": _RECEIPT_SCHEMA,
        "target": _clean_id(raw.get("target")),
        "publication": raw.get("publication"),
        "ok": raw.get("ok"),
        "status": raw.get("status"),
        "code": raw.get("code"),
        "transition_id": _clean_id(raw.get("transition_id")),
        "main": _clean_id(raw.get("main")),
        "epoch": raw.get("epoch"),
        "incarnation": raw.get("incarnation"),
        "mutated": raw.get("mutated", False),
    }
    if record["target"] is None or record["publication"] not in _PUBLICATIONS:
        raise ValueError(_RECEIPT_REFUSAL)
    if not isinstance(record["ok"], bool) or not isinstance(record["mutated"], bool):
        raise ValueError(_RECEIPT_REFUSAL)
    for key in ("status", "code"):
        if record[key] is not None and (not isinstance(record[key], str)
                                        or _RECEIPT_CODE.fullmatch(record[key]) is None):
            raise ValueError(_RECEIPT_REFUSAL)
    for key in ("epoch", "incarnation"):
        if record[key] is not None and not _int_field(record[key]):
            raise ValueError(_RECEIPT_REFUSAL)
    return record


def _store_receipt(state, record):
    path = _receipt_path(state)
    if path.is_symlink():
        raise ValueError(_RECEIPT_REFUSAL)
    blob = json.dumps(record, indent=2, sort_keys=True)
    if "BEGIN " in blob or "lease_id" in blob or "PRIVATE" in blob:
        raise ValueError("Refusing to store a MAIN switch receipt that contains a secret. "
                         "No switch was started.")
    from jarvis.fsio import atomic_write
    atomic_write(path, blob, mode=0o600, fsync=True)
    _fsync_directory(path.parent)
    return record


def _unknown_receipt(device, prior):
    record = {"schema": _RECEIPT_SCHEMA, "target": device, "publication": "outcome_unknown",
              "ok": False, "status": "outcome_unknown", "code": "reconcile_required",
              "mutated": False}
    for key in ("transition_id", "main"):
        if isinstance(prior.get(key), str):
            record[key] = prior[key]
    for key in ("epoch", "incarnation"):
        if _int_field(prior.get(key)):
            record[key] = prior[key]
    return record


def _receipt_from_result(device, result, prior):
    if not isinstance(result, dict):
        return _unknown_receipt(device, prior)
    publication = result.get("publication")
    if publication not in _PUBLICATIONS:
        publication = "published" if result.get("ok") is True else "outcome_unknown"
    record = {"schema": _RECEIPT_SCHEMA, "target": device, "publication": publication,
              "ok": result.get("ok") is True, "mutated": result.get("mutated") is True}
    for key in ("status", "code"):
        value = result.get(key)
        if isinstance(value, str) and _RECEIPT_CODE.fullmatch(value):
            record[key] = value
    for key in ("transition_id", "main"):
        value = result.get(key)
        if isinstance(value, str) and _RECEIPT_ID.fullmatch(value):
            record[key] = value
        elif isinstance(prior.get(key), str):
            record[key] = prior[key]
    for key in ("epoch", "incarnation"):
        value = result.get(key)
        if _int_field(value):
            record[key] = value
        elif _int_field(prior.get(key)):
            record[key] = prior[key]
    return record


def switch_main(config, state, device):
    """Post one safe MAIN switch and keep a retry-safe local receipt.

    The receipt is metadata only. It is written before the post, so a lost
    response stays outcome_unknown and the next call posts again; the hub
    resumes from its own journal. An open receipt for a different device
    refuses without a post. This process does not take the fleet lock and
    does not call Hub.dispatch, so ordinary dispatch stays reachable.
    """
    from jarvis.topology import DEVICE_ID, endpoint
    folder = Path(state)
    if not isinstance(device, str) or DEVICE_ID.fullmatch(device) is None:
        raise ValueError("MAIN switch requires a device id.")
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("MAIN switch needs the local hub state directory.")
    if not _loopback(urlsplit(endpoint(config)).hostname):
        raise ValueError("MAIN control requires a loopback cluster.main_url.")
    current = _load_receipt(folder)
    if (current.get("target") not in (None, device)
            and current.get("publication") in _OPEN_RECEIPT):
        raise ValueError(
            f"A MAIN handoff to {current.get('target')} is already recorded. "
            "No switch was started.")
    prior = current if current.get("target") == device else {}
    _store_receipt(folder, _unknown_receipt(device, prior))
    result = request_main(config, folder, device)
    try:
        _store_receipt(folder, _receipt_from_result(device, result, prior))
    except ValueError:
        # The hub answer is the result. A receipt that cannot be stored stays
        # outcome_unknown, and the next call posts again.
        pass
    return result


def _require_transfer_parity(hub, device):
    """Gate a new transfer on a tested local tree and that candidate's code.

    An existing journal must still be reconciled after a lost response, even if
    code changed since the transition started. It is not a fresh admission.
    """
    from jarvis import cluster_cli
    from jarvis.release import code_parity
    intent = cluster_cli._load_intent(hub.state)
    if (intent.get("target") == device
            and intent.get("publication") in cluster_cli._OPEN_INTENT):
        return
    if device == hub.main:
        return
    node = next((row for row in hub.broker.nodes() if row.get("id") == device), None)
    parity = code_parity(hub.state, reported=node.get("code") if node else None)
    if parity.get("matched") is not True:
        raise ValueError("MAIN transfer requires matching tested code on both PCs. "
                         + (parity.get("instruction") or "Check the candidate release before retrying."))


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
    _require_transfer_parity(hub, data["device"])
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

