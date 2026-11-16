"""Explicit setup, pairing, revocation and startup for the main/node topology."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .fsio import read_json
from .cluster import Client, NodeWorker, save_json
from .cluster_store import CAPABILITIES, NODE_DEFAULT, Broker
from .deploy import code_key
from .continuity.hub_control import _fsync_directory, _int_field
from .topology import DEFAULT_PORT, DEVICE_ID, enabled, endpoint, identity


def _path_config(path):
    """The config file leave trusts. A missing file is empty.

    The caller's snapshot is not copied in. ``cluster_setup.leave`` reads this
    file again under the lock and refuses an enabled main without changing the role.
    """
    from .config_lock import ConfigUnreadable, read_config
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        fresh = read_config(path)
    except ConfigUnreadable as exc:
        raise ValueError("The config file can't be read; nothing was changed.") from exc
    if not isinstance(fresh, dict):
        raise ValueError("This PC's fleet identity is invalid. Nothing was changed.")
    return fresh


def parser(sub):
    command = sub.add_parser("cluster", help="Configure the main and its nodes",
        description="On the main, run `jarvis cluster init`, then `jarvis cluster invite NODE`; "
                    "on each node, run `jarvis cluster join CODE`; "
                    "run `jarvis cluster doctor` on the main or a node to see what is wrong and how to fix it")
    command.add_argument("action", choices=("init", "invite", "join", "leave", "doctor",
                                           "configure", "enroll", "revoke", "caps", "status",
                                           "run", "serve", "node", "request", "autostart", "tunnel", "update", "release", "main", "roles"))
    command.add_argument("value", nargs="?", help="Node ID, invite code or file, request ID, or on/off")
    command.add_argument("--id", dest="device_id", help="Init: this PC's device ID (default: from its hostname)")
    command.add_argument("--address", action="append", default=[],
                         help="Invite: an address where nodes can reach this main (repeatable; LAN and Tailscale are found)")
    command.add_argument("--no-autostart", action="store_true", help="Init/join: don't start at login")
    command.add_argument("--no-start", action="store_true", help="Join: save the link without connecting")
    command.add_argument("--main", dest="main_device", help="Device ID of the main")
    command.add_argument("--local", dest="local_device", help="This PC's device ID, or auto")
    command.add_argument("--url", help="HTTPS origin of the main, or loopback HTTP through an SSH tunnel")
    command.add_argument("--hostname", help="Enroll: hostname of a new node")
    command.add_argument("--capability", action="append", choices=sorted(CAPABILITIES),
                         help="Enroll/invite/caps: what the node may do (default: "
                              + ", ".join(sorted(NODE_DEFAULT)) + ")")
    command.add_argument("--output", help="Enroll: write the one-time pairing bundle here")
    command.add_argument("--port", type=int, default=7777, help="Main: local console port")
    command.add_argument("--fix", action="store_true", help="Doctor: restart the tunnel supervisor and the clipboard/Lan Mouse keep-alive now")
    command.add_argument("--rollback", action="store_true", help="Update: swap a node back to its previous code")
    command.add_argument("--autostart", action="store_true", help="Tunnel start/stop: also enable or disable login startup")


def roles(config, state=None):
    """Show authority roles when managed, preserving configured topology separately."""
    main = (config.get("cluster") or {}).get("main_device")
    authority = {}
    if state is not None and enabled(config):
        from .continuity.role_status import report
        authority = report(config, state)
        main = authority["main_device"]
    local = identity(config)[0] if (config.get("cluster") or {}).get("enabled") else None
    return {**authority, "main_device": main, "tunnel": (config.get("cluster") or {}).get("tunnel", "reverse"),
            "devices": {key: {"label": (device or {}).get("label") or key,
                              "role": ("main" if key == main else "node") if main else "unknown",
                              "this_pc": key == local}
                        for key, device in (config.get("devices") or {}).items()}}


def _known_device(devices, device):
    if not device:
        raise ValueError("Usage: jarvis cluster main DEVICE_ID  (one of: " + ", ".join(devices) + ")")
    if device not in devices:
        raise ValueError(f"No device '{device}'. Configured: {', '.join(devices)}.")


def _binding_parts(state):
    """`(handoff, runtime, integration module)` from the in-process production binding.

    `production_binding` returns None when this process has no enrollment, including when
    the durable marker is absent. It never invents a binding from the marker file. A bound
    value is a dict with `handoff` and `runtime` (an object with the same attributes is
    accepted too)."""
    try:
        from .continuity import integration
    except ImportError:
        return None, None, None
    lookup = getattr(integration, "production_binding", None)
    if not callable(lookup):
        return None, None, integration
    binding = lookup(state)
    if isinstance(binding, dict):
        return binding.get("handoff"), binding.get("runtime"), integration
    if binding is None:
        return None, None, integration
    return getattr(binding, "handoff", None), getattr(binding, "runtime", None), integration


def configured_handoff(state):
    """The bound handoff callable, or None when this process cannot run one.

    None covers a missing marker, a marker with no in-process binding, and a binding
    whose runtime or handoff is missing. It does not raise and it does not write."""
    handoff, runtime, _integration = _binding_parts(state)
    if callable(handoff) and callable(getattr(runtime, "require_authority", None)):
        return handoff
    return None


def _refuse_unbound(state, integration):
    """Marker absent: handoff is not configured. Marker present and unbound: refuse."""
    require = getattr(integration, "require_production_authority", None) if integration is not None else None
    if not callable(require):
        raise ValueError("Moving MAIN is unavailable until safe handoff is configured. "
                         "cluster.main_device was not changed.")
    try:
        require(state, {}, "set_main")
    except integration.IntegrationError as exc:
        if getattr(exc, "code", None) == "managed_unbound":
            raise ValueError("Safe handoff has no bound managed runtime. "
                             "cluster.main_device was not changed.") from exc
        raise
    raise ValueError("Moving MAIN is unavailable until safe handoff is configured. "
                     "cluster.main_device was not changed.")


def _bound_handoff(state):
    """The handoff callable, or a ValueError that leaves the role file alone."""
    handoff, runtime, integration = _binding_parts(state)
    runtime_ok = callable(getattr(runtime, "require_authority", None))
    if handoff is None and runtime is None:
        _refuse_unbound(state, integration)
    if not runtime_ok:
        raise ValueError("Safe handoff has no bound managed runtime. "
                         "cluster.main_device was not changed.")
    if not callable(handoff):
        raise ValueError("Moving MAIN is unavailable until safe handoff is configured. "
                         "cluster.main_device was not changed.")
    return handoff


def _handoff_result(result, *, mutated):
    return {"ok": bool(mutated and result.get("ok") is True), "status": result.get("status"),
            "transition_id": result.get("transition_id"), "main": result.get("main"),
            "epoch": result.get("epoch"), "mutated": mutated}


def _refuse_cloud(device, row):
    if isinstance(row, dict) and row.get("profile") == "cloud":
        raise ValueError(f"{device} is a cloud server and cannot become MAIN. "
                         "cluster.main_device was not changed.")


# Metadata only. A lease id, token, or key must never be written here.
# `schema` is optional on disk so a journal written before it existed still loads.
_INTENT_FIELDS = ("schema", "target", "old", "transition_id", "epoch", "publication",
                  "action", "incarnation", "mode", "outcome", "reconcile_required",
                  "expected_epoch", "expected_incarnation")
_INTENT_SCHEMA = 1
_JOURNAL_REFUSAL = (
    "The MAIN handoff journal is unusable. It was not replaced and no handoff was started. "
    "cluster.main_device was not changed.")
_PUBLICATIONS = {"intent", "pending", "authority_granted", "publication_failed", "published",
                 "conflict", "unconfirmed", "not_ready", "failed", "outcome_unknown",
                 "reconcile_required"}
_OPEN_INTENT = {"intent", "pending", "authority_granted", "publication_failed",
                "conflict", "unconfirmed", "not_ready", "outcome_unknown", "reconcile_required"}
_GRANT_RECEIPTS = {"authority_granted", "publication_failed", "published", "conflict"}
_AMBIGUOUS = {"outcome_unknown", "reconcile_required", "intent", "unconfirmed"}
_OPEN_TRANSITION = {"REQUESTED", "VALIDATING", "DRAINING", "FENCING", "GRANTED_NOT_READY"}
_HANDOFF_ACTIONS = {"set_preferred_main", "recover"}
_ID_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODE_TEXT = re.compile(r"^[A-Z0-9_]{1,64}$")
_JOURNAL_MAX_BYTES = 4096


class _PublicationConflict(Exception):
    def __init__(self, current):
        self.current = current
        super().__init__(current)


def _intent_path(state):
    return Path(state) / "main-handoff.json"


def _clean_id(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or _ID_TEXT.fullmatch(value) is None:
        raise ValueError(_JOURNAL_REFUSAL)
    return value


def _clean_mode(value):
    if value is None:
        return None
    if not isinstance(value, str) or _MODE_TEXT.fullmatch(value) is None:
        raise ValueError(_JOURNAL_REFUSAL)
    return value


def _load_intent(state):
    """Return {} only when the journal file is confirmed absent.

    An existing file that is unreadable, corrupt, the wrong type, an unknown schema,
    or carrying a field this coordinator does not own is refused. The file is not
    replaced here and the caller must not start a callback."""
    path = _intent_path(state)
    if not os.path.lexists(path):
        return {}
    if path.is_symlink():
        raise ValueError(_JOURNAL_REFUSAL)
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(_JOURNAL_REFUSAL) from exc
    if not info.st_size or info.st_size > _JOURNAL_MAX_BYTES:
        raise ValueError(_JOURNAL_REFUSAL)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(_JOURNAL_REFUSAL) from exc
    if not isinstance(raw, dict) or any(key not in _INTENT_FIELDS for key in raw):
        raise ValueError(_JOURNAL_REFUSAL)
    if "schema" in raw and raw.get("schema") != _INTENT_SCHEMA:
        raise ValueError(_JOURNAL_REFUSAL)
    try:
        record = {
            "schema": raw.get("schema"),
            "target": _clean_id(raw.get("target")),
            "old": _clean_id(raw.get("old")),
            "transition_id": _clean_id(raw.get("transition_id")),
            "action": _clean_id(raw.get("action")),
            "mode": _clean_mode(raw.get("mode")),
            "publication": raw.get("publication"),
            "outcome": raw.get("outcome"),
            "epoch": raw.get("epoch"),
            "incarnation": raw.get("incarnation"),
            "expected_epoch": raw.get("expected_epoch"),
            "expected_incarnation": raw.get("expected_incarnation"),
            "reconcile_required": raw.get("reconcile_required", False),
        }
    except ValueError:
        raise
    publication = record["publication"]
    if publication is not None and publication not in _PUBLICATIONS:
        raise ValueError(_JOURNAL_REFUSAL)
    outcome = record["outcome"]
    if outcome is not None and outcome not in {"outcome_unknown", "known"}:
        raise ValueError(_JOURNAL_REFUSAL)
    for key in ("epoch", "incarnation", "expected_epoch", "expected_incarnation"):
        if not _int_field(record[key]) and record[key] is not None:
            raise ValueError(_JOURNAL_REFUSAL)
    if not isinstance(record["reconcile_required"], bool):
        raise ValueError(_JOURNAL_REFUSAL)
    if record["schema"] not in (None, _INTENT_SCHEMA):
        raise ValueError(_JOURNAL_REFUSAL)
    if record["action"] is not None and record["action"] not in _HANDOFF_ACTIONS:
        raise ValueError(_JOURNAL_REFUSAL)
    return record


def _store_intent(state, **fields):
    """Replace state/main-handoff.json under the fleet lock. Atomic, fsynced, no secrets."""
    record = {key: fields.get(key) for key in _INTENT_FIELDS}
    record["schema"] = _INTENT_SCHEMA
    if record.get("reconcile_required") is not True:
        record["reconcile_required"] = False
    path = _intent_path(state)
    blob = json.dumps(record, indent=2, sort_keys=True)
    if "BEGIN " in blob or "lease_id" in blob or "PRIVATE" in blob:
        raise ValueError("Refusing to store a handoff journal that contains a secret. "
                         "cluster.main_device was not changed.")
    from .fsio import atomic_write
    atomic_write(path, blob, mode=0o600, fsync=True)
    _fsync_directory(path.parent)
    return record


def _unchanged(status, **extra):
    body = {"ok": False, "mutated": False, "authority": extra.pop("authority", None),
            "publication": extra.pop("publication", None)}
    body.update(status)
    body.update(extra)
    return body


def _call_authority_status(handoff):
    """Zero-argument authenticated port.status(). Callback fields are not a substitute."""
    status_fn = getattr(handoff, "authority_status", None)
    if not callable(status_fn):
        raise ValueError("Safe handoff has no authenticated authority status. "
                         "cluster.main_device was not changed.")
    try:
        status = status_fn()
    except TypeError as exc:
        raise ValueError("Safe handoff authority status must take no arguments. "
                         "cluster.main_device was not changed.") from exc
    if not isinstance(status, dict):
        raise ValueError("Authenticated authority status was not a status record. "
                         "cluster.main_device was not changed.")
    return status


def _barrier_clear(status):
    return isinstance(status, dict) and status.get("grant_barrier") in (None, 0, 0.0)


def _status_tuple_ok(status):
    """V2_ACTIVE, lease valid, main ready, and a real incarnation/epoch. Barrier must be quiet."""
    if not isinstance(status, dict) or status.get("mode") != "V2_ACTIVE":
        return False
    if status.get("lease_valid") is not True or status.get("main_ready") is not True:
        return False
    if not _int_field(status.get("epoch")) or not _int_field(status.get("incarnation")):
        return False
    if not isinstance(status.get("main"), str) or not status.get("main"):
        return False
    return _barrier_clear(status)


def _transition_bound(status, target, transition_id, *, allow_cleared=False):
    transition = status.get("transition")
    if transition is None:
        if allow_cleared:
            return True
        return isinstance(transition_id, str) and bool(transition_id)
    if not isinstance(transition, dict):
        return False
    if transition.get("state") != "SUCCEEDED" or transition.get("candidate") != target:
        return False
    bound = transition.get("transition_id")
    if transition_id and bound != transition_id:
        return False
    return isinstance(bound, str) and bool(bound)


def _confirms_target(status, target, transition_id, epoch, incarnation, *, allow_cleared=False):
    """True only when the live status is the same incarnation and the same epoch.

    A later incarnation that happens to reuse the epoch number is not this grant.
    The stored incarnation is never replaced with that newer one.
    """
    if not _status_tuple_ok(status) or status.get("main") != target:
        return False
    if not _int_field(incarnation) or status.get("incarnation") != incarnation:
        return False
    if not _int_field(epoch) or status.get("epoch") != epoch:
        return False
    return _transition_bound(status, target, transition_id, allow_cleared=allow_cleared)


def _incarnation_changed(status, incarnation, epoch):
    """The epoch number matches and the incarnation does not. Do not adopt either."""
    if not isinstance(status, dict) or not _int_field(incarnation) or not _int_field(epoch):
        return False
    got = status.get("incarnation")
    return _int_field(got) and got != incarnation and status.get("epoch") == epoch


def _expiry_proven(status):
    """Central authority says the old lease is gone and the safety barrier is quiet.

    A missing barrier is not proof. `None` is how an infinite barrier is reported.
    """
    if not isinstance(status, dict) or status.get("lease_valid") is not False:
        return False
    barrier = status.get("grant_barrier")
    return barrier == 0 or barrier == 0.0


def _intended_action(status, old_main):
    if not isinstance(status, dict):
        return None
    if status.get("lease_valid") is True and status.get("main") == old_main:
        return "set_preferred_main"
    if status.get("lease_valid") is False:
        return "recover"
    return None


def _open_transition(status):
    transition = status.get("transition") if isinstance(status, dict) else None
    if not isinstance(transition, dict) or transition.get("state") not in _OPEN_TRANSITION:
        return None
    return transition


def _active_conflict(status, device, old_main, intent):
    """An open transition for a different candidate, source, or id. Journal may be absent."""
    transition = _open_transition(status)
    if transition is None:
        return None
    if transition.get("candidate") != device:
        return "A different MAIN transition is already open. cluster.main_device was not changed."
    source = transition.get("source")
    action = transition.get("action")
    if action == "recover":
        if source not in (None, old_main):
            return "The open recovery names a different source. cluster.main_device was not changed."
    elif source != old_main:
        return "The open MAIN transition names a different source. cluster.main_device was not changed."
    tid = intent.get("transition_id") if isinstance(intent, dict) else None
    bound = transition.get("transition_id")
    if isinstance(tid, str) and tid and bound != tid:
        return "The open MAIN transition is not the recorded one. cluster.main_device was not changed."
    return None


def _resume_ok(status, device, old_main, intent):
    """Same-target DRAINING whose id, source, incarnation, and expected epoch all match."""
    transition = _open_transition(status)
    if transition is None or transition.get("state") != "DRAINING" or status.get("mode") != "HANDOVER":
        return False
    if transition.get("candidate") != device:
        return False
    action = transition.get("action")
    if action not in _HANDOFF_ACTIONS:
        return False
    source = transition.get("source")
    if action == "recover":
        if source not in (None, old_main):
            return False
    elif source != old_main:
        return False
    bound = transition.get("transition_id")
    if not isinstance(bound, str) or not bound:
        return False
    tid = intent.get("transition_id") if isinstance(intent, dict) else None
    if isinstance(tid, str) and tid and tid != bound:
        return False
    inc = transition.get("expected_incarnation")
    epoch = transition.get("expected_epoch")
    if not _int_field(inc) or not _int_field(epoch):
        return False
    if status.get("incarnation") != inc or status.get("epoch") != epoch:
        return False
    if isinstance(intent, dict):
        stored_inc = intent.get("incarnation")
        if not _int_field(stored_inc):
            stored_inc = intent.get("expected_incarnation")
        stored_epoch = intent.get("expected_epoch")
        if not _int_field(stored_epoch):
            stored_epoch = intent.get("epoch")
        stored_action = intent.get("action")
        if _int_field(stored_inc) and stored_inc != inc:
            return False
        if _int_field(stored_epoch) and stored_epoch != epoch:
            return False
        if isinstance(stored_action, str) and stored_action and stored_action != action:
            return False
    return True


def _pretuple_match(transition, device, old_main, action, incarnation, expected_epoch):
    """Bind a lost callback only when the live transition is the one we intended."""
    if not isinstance(transition, dict):
        return False
    if transition.get("candidate") != device or transition.get("action") not in _HANDOFF_ACTIONS:
        return False
    if action and transition.get("action") != action:
        return False
    source = transition.get("source")
    if transition.get("action") == "recover":
        if source not in (None, old_main):
            return False
    elif source != old_main:
        return False
    if not isinstance(transition.get("transition_id"), str) or not transition.get("transition_id"):
        return False
    inc = transition.get("expected_incarnation")
    epoch = transition.get("expected_epoch")
    if _int_field(incarnation) and _int_field(inc) and inc != incarnation:
        return False
    if _int_field(expected_epoch) and _int_field(epoch) and epoch != expected_epoch:
        return False
    if not _int_field(inc) or not _int_field(epoch):
        return False
    return True


def _candidate_ready(handoff, device):
    """Coordinator proof. A ping or an online flag is not readiness."""
    ready_fn = getattr(handoff, "candidate_readiness", None)
    if not callable(ready_fn):
        raise ValueError("Safe handoff does not expose candidate readiness to the coordinator. "
                         "cluster.main_device was not changed.")
    try:
        facts = ready_fn(device)
    except TypeError as exc:
        raise ValueError("Candidate readiness must accept the candidate id. "
                         "cluster.main_device was not changed.") from exc
    return isinstance(facts, dict) and facts.get("control_ready") is True


def _same_receipt(current_main, device, intent, status):
    if current_main != device or intent.get("target") != device:
        return False
    if intent.get("publication") not in _GRANT_RECEIPTS:
        return False
    tid = intent.get("transition_id")
    epoch = intent.get("epoch")
    incarnation = intent.get("incarnation")
    return _confirms_target(status, device, tid if isinstance(tid, str) else None,
                            epoch if _int_field(epoch) else None,
                            incarnation if _int_field(incarnation) else None)


def _publish_role(state, path, device, old_main, status, intent):
    """Write cluster.main_device only when the file still has the old main, or this receipt."""
    from .config_lock import ConfigDrift, ConfigUnreadable, patch_config
    epoch = status.get("epoch")
    tid = intent.get("transition_id")
    incarnation = intent.get("incarnation") if _int_field(intent.get("incarnation")) else status.get("incarnation")

    def change(latest):
        current = latest.get("cluster") if isinstance(latest.get("cluster"), dict) else {}
        current_main = current.get("main_device")
        if current_main != old_main and not _same_receipt(current_main, device, intent, status):
            raise _PublicationConflict(current_main)
        latest_devices = latest.get("devices") if isinstance(latest.get("devices"), dict) else {}
        _known_device(latest_devices, device)
        _refuse_cloud(device, latest_devices.get(device))
        if not isinstance(latest.get("cluster"), dict):
            latest["cluster"] = {}
        latest["cluster"]["main_device"] = device

    try:
        proposed, changed = patch_config(state, path, change, backup="cluster-main", create=False)
    except _PublicationConflict as exc:
        _store_intent(state, target=device, old=old_main, transition_id=tid, epoch=epoch,
                      publication="conflict", action=intent.get("action"),
                      incarnation=incarnation, mode=status.get("mode"))
        current = exc.current
        return _unchanged(
            {"status": "publication_conflict", "main": device, "main_device": current,
             "transition_id": tid, "epoch": epoch},
            publication="conflict", authority="granted",
            error=(f"cluster.main_device changed from {old_main} to {current} before publication. "
                   "Authority may have moved. The other role was left untouched."))
    except (ConfigDrift, ConfigUnreadable, OSError, ValueError):
        _store_intent(state, target=device, old=old_main, transition_id=tid, epoch=epoch,
                      publication="publication_failed", action=intent.get("action"),
                      incarnation=incarnation, mode=status.get("mode"))
        return _unchanged(
            {"status": "publication_failed", "main": device, "main_device": old_main,
             "transition_id": tid, "epoch": epoch},
            publication="publication_failed", authority="granted",
            error=("Authority granted the new main, but cluster.main_device was not published. "
                   "Retry the same handoff to finish publication without acquiring another epoch "
                   "or rerunning the brain sync."))
    stored = _store_intent(state, target=device, old=old_main, transition_id=tid, epoch=epoch,
                           publication="published", action=intent.get("action"),
                           incarnation=incarnation, mode=status.get("mode"))
    out = {"ok": True, "status": "ready", "publication": "published", "authority": "granted",
           "mutated": bool(changed), "transition_id": stored.get("transition_id"),
           "main": status.get("main"), "epoch": epoch}
    out.update(roles(proposed))
    return out


def _mode_of(status):
    mode = status.get("mode") if isinstance(status, dict) else None
    if isinstance(mode, str) and _MODE_TEXT.fullmatch(mode):
        return mode
    return None


def _action_name(value):
    return value if value in _HANDOFF_ACTIONS else None


def _tid_name(value):
    if isinstance(value, str) and _ID_TEXT.fullmatch(value):
        return value
    return None


def _callback_view(result):
    """Callback fields safe to return. Lease ids and other secrets are dropped."""
    if not isinstance(result, dict):
        return {"lost": True}
    view = {}
    if isinstance(result.get("ok"), bool):
        view["ok"] = result["ok"]
    for key in ("status", "code", "main"):
        value = result.get(key)
        if isinstance(value, str) and _ID_TEXT.fullmatch(value):
            view[key] = value
    action = _action_name(result.get("action"))
    if action:
        view["action"] = action
    tid = _tid_name(result.get("transition_id"))
    if tid:
        view["transition_id"] = tid
    if _int_field(result.get("epoch")):
        view["epoch"] = result["epoch"]
    return view


def _read_status(handoff):
    try:
        return _call_authority_status(handoff), None
    except Exception:
        return None, "unavailable"


def _kept_incarnation(intent, pre):
    if isinstance(intent, dict):
        for key in ("incarnation", "expected_incarnation"):
            if _int_field(intent.get(key)):
                return intent[key]
    if isinstance(pre, dict) and _int_field(pre.get("incarnation")):
        return pre["incarnation"]
    return None


def _kept_epoch(intent, pre):
    if isinstance(intent, dict):
        for key in ("expected_epoch", "epoch"):
            if _int_field(intent.get(key)):
                return intent[key]
    if isinstance(pre, dict) and _int_field(pre.get("epoch")):
        return pre["epoch"]
    return None


_STATUS_LOST = (
    "Authenticated authority status failed after the callback. The outcome is unknown and must be "
    "reconciled. A new handoff was not started. cluster.main_device was not changed.")
_RECONCILE_ERROR = (
    "The handoff outcome is unknown. Authority does not match the stored intent. "
    "A new handoff was not started. cluster.main_device was not changed.")
_INCARNATION_ERROR = (
    "Authority incarnation changed. The stored epoch was not adopted from the new incarnation. "
    "cluster.main_device was not changed. A new handoff was not started.")
_READY_NOT_PROOF = (
    "The target looks ready, but that is not proof of this handoff. "
    "cluster.main_device was not changed. A new handoff was not started.")


def _refuse_incarnation(old_main, intent, status):
    return _unchanged(
        {"status": "reconcile_required", "code": "incarnation_changed", "main_device": old_main,
         "main": status.get("main") if isinstance(status, dict) else None,
         "transition_id": intent.get("transition_id"), "epoch": intent.get("epoch"),
         "incarnation": intent.get("incarnation")},
        publication="reconcile_required", authority="mismatch", error=_INCARNATION_ERROR)


def set_main(config, path, device, state=None):
    """Move MAIN through the bound safe handoff. Never rewrite the role as a fallback.

    The caller's `config` is not the new role. Under the fleet lock the coordinator writes
    state/main-handoff.json before the callback, then checks the authenticated authority
    status before it patches cluster.main_device. A callback that says ok is not enough.
    Recovery is published only when that status shows the old lease is gone and the safety
    barrier is clear. A later retry that already matches the stored receipt finishes the
    config write and does not call the handoff again.
    """
    from .continuity.hub_control import operator_configured, request_main
    if (configured_handoff(state or Path(path).resolve().parent / ".jarvis") is None
            and operator_configured(config, Path(path).resolve().parent)):
        return request_main(config, state or Path(path).resolve().parent / ".jarvis", device)
    del config
    try:
        from .continuity.integration import refuse_unfenced
    except ImportError:  # continuity is optional; without it there is still no raw rewrite
        refuse_unfenced = None
    if refuse_unfenced is not None:
        refuse_unfenced("set_main")
    path = Path(path)
    state = Path(state) if state is not None else path.resolve().parent / ".jarvis"
    from .config_lock import ConfigUnreadable, config_mutation, read_config
    import copy
    with config_mutation(state):
        handoff = _bound_handoff(state)
        try:
            doc = read_config(path)
        except ConfigUnreadable as exc:
            raise ValueError("The config file can't be read; cluster.main_device was not changed.") from exc
        devices = doc.get("devices") if isinstance(doc.get("devices"), dict) else {}
        _known_device(devices, device)
        _refuse_cloud(device, devices.get(device))
        cluster = doc.get("cluster") if isinstance(doc.get("cluster"), dict) else {}
        raw_main = cluster.get("main_device")
        old_main = raw_main
        # A former MAIN's signed endpoint overlay proves the current route, but
        # while it is serving as a node its raw config can still name the old
        # recipient. Prefer the authenticated live MAIN only when it is this
        # local installation and is fully ready; never infer from a ping.
        from jarvis.topology import identity
        live = _call_authority_status(handoff)
        local = identity(doc)[0]
        if (live.get("mode") == "V2_ACTIVE" and live.get("lease_valid") is True
                and live.get("main_ready") is True and live.get("main") == local):
            old_main = local
        intent = _load_intent(state)
        if (intent.get("target") not in (None, device) and intent.get("publication") in _OPEN_INTENT
                and intent.get("publication") != "published"):
            raise ValueError(
                f"A MAIN handoff to {intent.get('target')} is already recorded. "
                "cluster.main_device was not changed.")

        def remember(publication, *, reconcile=False, outcome=None, transition_id=None, epoch=None,
                     incarnation=None, expected_epoch=None, expected_incarnation=None, action=None,
                     mode=None):
            return _store_intent(
                state, target=device, old=old_main, publication=publication,
                reconcile_required=reconcile is True, outcome=outcome,
                transition_id=_tid_name(transition_id),
                epoch=epoch if _int_field(epoch) else None,
                incarnation=incarnation if _int_field(incarnation) else None,
                expected_epoch=expected_epoch if _int_field(expected_epoch) else None,
                expected_incarnation=expected_incarnation if _int_field(expected_incarnation) else None,
                action=_action_name(action), mode=_mode_of({"mode": mode}))

        def incarnation_stop(status, stored):
            """Keep the stored pair. A matching epoch from a new incarnation is not a grant."""
            remember("reconcile_required", reconcile=True, outcome="outcome_unknown",
                     transition_id=stored.get("transition_id"), epoch=_kept_epoch(stored, None),
                     incarnation=_kept_incarnation(stored, None),
                     expected_epoch=stored.get("expected_epoch"),
                     expected_incarnation=stored.get("expected_incarnation") or _kept_incarnation(stored, None),
                     action=stored.get("action"), mode=stored.get("mode"))
            return _refuse_incarnation(old_main, stored, status)

        def perform(pre, stored):
            """Persist outcome_unknown, then call. Publish only after a confirming status."""
            incarnation = _kept_incarnation(stored, pre)
            expected_epoch = _kept_epoch(stored, pre)
            action = _action_name(stored.get("action")) or _intended_action(pre, old_main)
            live = _open_transition(pre)
            tid = _tid_name(stored.get("transition_id"))
            if tid is None and isinstance(live, dict):
                tid = _tid_name(live.get("transition_id"))
            remember("outcome_unknown", reconcile=True, outcome="outcome_unknown",
                     transition_id=tid, epoch=expected_epoch, incarnation=incarnation,
                     expected_epoch=pre.get("epoch") if _int_field(pre.get("epoch")) else expected_epoch,
                     expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
            durable = _load_intent(state)
            try:
                result = handoff(old_main, device, copy.deepcopy(doc), path.resolve().parent)
            except Exception:
                return _unchanged(
                    {"status": "outcome_unknown", "code": "reconcile_required", "main_device": old_main,
                     "transition_id": tid, "epoch": expected_epoch, "callback": {"lost": True}},
                    publication="outcome_unknown", authority="unknown", error=_STATUS_LOST)
            if not isinstance(result, dict):
                raise ValueError("Safe handoff returned no result. cluster.main_device was not changed.")
            action = _action_name(result.get("action")) or action
            tid = _tid_name(result.get("transition_id")) or tid
            epoch = result.get("epoch") if _int_field(result.get("epoch")) else expected_epoch
            pending = result.get("status") == "pending"
            if pending:
                if not _tid_name(result.get("transition_id")):
                    raise ValueError("Safe handoff is pending without a transition id. "
                                     "cluster.main_device was not changed.")
                remember("pending", transition_id=tid, epoch=epoch, incarnation=incarnation,
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                out = _handoff_result(result, mutated=False)
                out.update(main_device=old_main, publication="pending", authority="pending")
                return out
            if result.get("ok") is not True:
                remember("failed", transition_id=tid, epoch=epoch, incarnation=incarnation,
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                out = _handoff_result(result, mutated=False)
                out.update(main_device=old_main, publication="failed", authority="not_granted")
                return out
            if result.get("main") != device:
                raise ValueError("Safe handoff named a different main. cluster.main_device was not changed.")
            if action == "recover" and not _expiry_proven(pre):
                remember("unconfirmed", reconcile=True, outcome="outcome_unknown",
                         transition_id=tid, epoch=epoch, incarnation=incarnation,
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                return _unchanged(
                    {"status": "refused", "code": "unproven_recovery", "main_device": old_main,
                     "main": result.get("main"), "transition_id": tid, "epoch": epoch},
                    publication="unconfirmed", authority="unproven",
                    error=("Recovery was not proven before the callback. "
                           "cluster.main_device was not changed."))
            if result.get("code") != "noop" and not _tid_name(result.get("transition_id")):
                return lost_result(pre, durable, result, incarnation, action)
            post, err = _read_status(handoff)
            if err is not None:
                remember("outcome_unknown", reconcile=True, outcome="outcome_unknown",
                         transition_id=tid, epoch=epoch, incarnation=incarnation,
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                _load_intent(state)
                return _unchanged(
                    {"status": "outcome_unknown", "code": "reconcile_required", "main_device": old_main,
                     "transition_id": tid, "epoch": epoch, "incarnation": incarnation,
                     "callback": _callback_view(result)},
                    publication="outcome_unknown", authority="unknown", error=_STATUS_LOST)
            if _incarnation_changed(post, incarnation, epoch):
                return incarnation_stop(post, _load_intent(state))
            allow_cleared = result.get("code") == "noop"
            if not _confirms_target(post, device, tid, epoch, incarnation, allow_cleared=allow_cleared):
                remember("unconfirmed", reconcile=True, outcome="outcome_unknown",
                         transition_id=tid, epoch=_kept_epoch(durable, pre), incarnation=incarnation,
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                return _unchanged(
                    {"status": "refused", "code": "authority_unconfirmed", "main_device": old_main,
                     "main": post.get("main"), "transition_id": tid, "epoch": epoch},
                    publication="unconfirmed", authority="mismatch",
                    error=("The callback reported success but the authenticated authority does not "
                           "show that main. cluster.main_device was not changed."))
            granted = {"target": device, "old": old_main, "transition_id": tid, "epoch": post.get("epoch"),
                       "action": action, "incarnation": incarnation, "mode": _mode_of(post),
                       "publication": "authority_granted", "expected_epoch": durable.get("expected_epoch"),
                       "expected_incarnation": incarnation}
            _store_intent(state, **granted)
            return _publish_role(state, path, device, old_main, post, granted)

        def lost_result(pre, durable, result, incarnation, action):
            """No tuple came back. Bind the live transition only when it is the pretuple."""
            post, err = _read_status(handoff)
            if err is not None:
                remember("outcome_unknown", reconcile=True, outcome="outcome_unknown",
                         incarnation=incarnation, epoch=_kept_epoch(durable, pre),
                         expected_epoch=durable.get("expected_epoch"),
                         expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
                return _unchanged(
                    {"status": "outcome_unknown", "code": "reconcile_required", "main_device": old_main,
                     "incarnation": incarnation, "callback": {"lost": True}},
                    publication="outcome_unknown", authority="unknown", error=_STATUS_LOST)
            transition = post.get("transition") if isinstance(post.get("transition"), dict) else None
            expected = durable.get("expected_epoch") if _int_field(durable.get("expected_epoch")) else _kept_epoch(durable, pre)
            if _pretuple_match(transition, device, old_main, action, incarnation, expected):
                bound = transition.get("transition_id")
                remember("outcome_unknown", reconcile=True, outcome="outcome_unknown",
                         transition_id=bound, epoch=expected, incarnation=incarnation,
                         expected_epoch=expected, expected_incarnation=incarnation,
                         action=action or transition.get("action"), mode=_mode_of(post))
                return _unchanged(
                    {"status": "reconcile_required", "code": "bound", "main_device": old_main,
                     "main": post.get("main"), "transition_id": bound, "epoch": expected,
                     "incarnation": incarnation},
                    publication="outcome_unknown", authority="unknown",
                    error=("The callback result had no tuple. The open transition matches the "
                           "intended candidate, source, and action, so it was recorded. "
                           "Success was not inferred. cluster.main_device was not changed."))
            if _status_tuple_ok(post) and post.get("main") == device:
                remember("reconcile_required", reconcile=True, outcome="outcome_unknown",
                         incarnation=incarnation, epoch=expected,
                         expected_epoch=expected, expected_incarnation=incarnation,
                         action=action, mode=_mode_of(post))
                return _unchanged(
                    {"status": "reconcile_required", "code": "not_proof", "main_device": old_main,
                     "main": device, "incarnation": incarnation, "epoch": expected},
                    publication="reconcile_required", authority="mismatch", error=_READY_NOT_PROOF)
            remember("unconfirmed", reconcile=True, outcome="outcome_unknown",
                     incarnation=incarnation, epoch=expected, expected_epoch=expected,
                     expected_incarnation=incarnation, action=action, mode=_mode_of(pre))
            return _unchanged(
                {"status": "refused", "code": "unvalidated", "main_device": old_main, "main": device,
                 "epoch": expected},
                publication="unconfirmed", authority="unchecked",
                error=("The callback reported success without a transition id. "
                       "cluster.main_device was not changed."))

        if intent.get("target") == device and intent.get("publication") in _GRANT_RECEIPTS:
            status, err = _read_status(handoff)
            if err is not None:
                return _unchanged(
                    {"status": "outcome_unknown", "code": "reconcile_required", "main_device": old_main,
                     "transition_id": intent.get("transition_id"), "epoch": intent.get("epoch"),
                     "incarnation": intent.get("incarnation")},
                    publication=intent.get("publication"), authority="unknown", error=_STATUS_LOST)
            if _incarnation_changed(status, intent.get("incarnation"), intent.get("epoch")):
                return incarnation_stop(status, intent)
            tid = intent.get("transition_id")
            if _confirms_target(status, device, tid if isinstance(tid, str) else None,
                                intent.get("epoch"), intent.get("incarnation")):
                return _publish_role(state, path, device, intent.get("old"), status, intent)
            return _unchanged(
                {"status": "publication_failed", "main": status.get("main"),
                 "main_device": old_main, "transition_id": tid, "epoch": intent.get("epoch"),
                 "incarnation": intent.get("incarnation")},
                publication=intent.get("publication"), authority="mismatch",
                error=("Stored handoff intent does not match the authenticated authority. "
                       "cluster.main_device was not changed. A new handoff was not started."))

        if intent.get("target") == device and intent.get("publication") in _AMBIGUOUS:
            status, err = _read_status(handoff)
            if err is not None:
                return _unchanged(
                    {"status": "outcome_unknown", "code": "reconcile_required", "main_device": old_main,
                     "transition_id": intent.get("transition_id"), "epoch": _kept_epoch(intent, None),
                     "incarnation": _kept_incarnation(intent, None), "callback": {"lost": True}},
                    publication="outcome_unknown", authority="unknown", error=_STATUS_LOST)
            incarnation = _kept_incarnation(intent, status)
            epoch = intent.get("epoch") if _int_field(intent.get("epoch")) else _kept_epoch(intent, status)
            if _incarnation_changed(status, incarnation, epoch):
                return incarnation_stop(status, intent)
            tid = intent.get("transition_id") if isinstance(intent.get("transition_id"), str) else None
            if _confirms_target(status, device, tid, epoch, incarnation):
                return _publish_role(state, path, device, intent.get("old") or old_main, status, intent)
            if _resume_ok(status, device, old_main, intent):
                return perform(status, intent)
            transition = status.get("transition") if isinstance(status.get("transition"), dict) else None
            action = _action_name(intent.get("action")) or _intended_action(status, old_main)
            expected = intent.get("expected_epoch") if _int_field(intent.get("expected_epoch")) else epoch
            if not tid and _pretuple_match(transition, device, old_main, action, incarnation, expected):
                remember("outcome_unknown", reconcile=True, outcome="outcome_unknown",
                         transition_id=transition.get("transition_id"), epoch=expected,
                         incarnation=incarnation, expected_epoch=expected,
                         expected_incarnation=incarnation, action=action, mode=_mode_of(status))
                return _unchanged(
                    {"status": "reconcile_required", "code": "bound", "main_device": old_main,
                     "transition_id": transition.get("transition_id"), "epoch": expected,
                     "incarnation": incarnation},
                    publication="outcome_unknown", authority="matched",
                    error=("The lost callback had no tuple. The open transition matches the intended "
                           "candidate, source, and action. Success was not inferred. "
                           "cluster.main_device was not changed."))
            if _status_tuple_ok(status) and status.get("main") == device:
                return _unchanged(
                    {"status": "reconcile_required", "code": "not_proof", "main_device": old_main,
                     "main": device, "incarnation": incarnation, "epoch": epoch},
                    publication="reconcile_required", authority="mismatch", error=_READY_NOT_PROOF)
            return _unchanged(
                {"status": "reconcile_required", "code": "reconcile_required", "main_device": old_main,
                 "transition_id": tid, "epoch": epoch, "incarnation": incarnation},
                publication="outcome_unknown", authority="unknown", error=_RECONCILE_ERROR)

        pre = _call_authority_status(handoff)
        if pre.get("mode") in ("LEGACY_ACTIVE", "PREPARING_V2", "QUIESCED"):
            next_modes = {"LEGACY_ACTIVE": "enter PREPARING_V2, then QUIESCED; ",
                          "PREPARING_V2": "enter QUIESCED; ", "QUIESCED": ""}
            return _unchanged(
                {"status": "refused", "code": "cutover_required", "main_device": old_main,
                 "main": pre.get("main")},
                publication="not_started", authority="not_granted",
                error=(f"The authority is in {pre['mode']}. The operator must finish cutover: "
                       f"{next_modes[pre['mode']]}run continuity ack --next-version on every eligible "
                       "PC while QUIESCED to acknowledge the upcoming mode version; "
                       "enter V2_ONLY_NO_MAIN using the current "
                       "--expect-version. Then obtain a ready MAIN lease through a manual recover "
                       "transition with verified readiness and canary before moving MAIN. "
                       "continuity mode V2_ACTIVE cannot grant a lease. "
                       "cluster.main_device was not changed."))
        conflict = _active_conflict(pre, device, old_main, intent)
        if conflict:
            return _unchanged(
                {"status": "refused", "code": "transition_conflict", "main_device": old_main,
                 "main": pre.get("main")},
                publication="unconfirmed", authority="mismatch", error=conflict)
        draining = _open_transition(pre)
        if isinstance(draining, dict) and draining.get("state") == "DRAINING" and draining.get("candidate") == device:
            if not _resume_ok(pre, device, old_main, intent):
                return _unchanged(
                    {"status": "refused", "code": "transition_not_validated", "main_device": old_main,
                     "main": pre.get("main"), "transition_id": draining.get("transition_id")},
                    publication="unconfirmed", authority="mismatch",
                    error=("The open drain does not match this candidate, source, incarnation, "
                           "and expected epoch. cluster.main_device was not changed."))
            return perform(pre, intent)
        if intent.get("target") == device and intent.get("publication") == "pending":
            return perform(pre, intent)
        from .continuity.runtime import RuntimeRefusal
        try:
            candidate_ready = _candidate_ready(handoff, device)
        except RuntimeRefusal as exc:
            return _unchanged(
                {"status": "refused", "code": exc.code, "main_device": old_main,
                 "main": pre.get("main")},
                publication="not_ready", authority="unchecked",
                error=("Candidate readiness was refused. cluster.main_device was not changed."))
        if not candidate_ready:
            return _unchanged(
                {"status": "not_ready", "main_device": old_main, "main": pre.get("main")},
                publication="not_ready", authority="unchecked",
                error=("Candidate readiness is not proven. A ping is not a substitute. "
                       "cluster.main_device was not changed."))
        already = _status_tuple_ok(pre) and pre.get("main") == device
        preferred = (pre.get("lease_valid") is True and pre.get("main") == old_main
                     and pre.get("main_ready") is True and pre.get("mode") == "V2_ACTIVE"
                     and _barrier_clear(pre))
        if not (_expiry_proven(pre) or already or preferred):
            return _unchanged(
                {"status": "refused", "code": "unproven_recovery", "main_device": old_main,
                 "main": pre.get("main")},
                publication="unconfirmed", authority="unproven",
                error=("The authenticated authority does not prove a live handoff or a quiet "
                       "expired lease. cluster.main_device was not changed."))
        return perform(pre, intent if intent.get("target") == device else {})


def run(args, config, base):
    action = args.action
    path, state = Path(args.config).resolve(), Path(args.state).resolve()
    if action in ("run", "serve", "node") and sys.stdout is None:
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(logs / "cluster.log", "a", encoding="utf-8", buffering=1)
    if action == "roles":
        return roles(config, state)
    if action == "main":
        return set_main(config, path, args.value, state=state)
    if action == "configure":
        # The file under the managed lock decides. A stale copy must not replace an
        # enabled fleet main; that handoff is `jarvis cluster main`. First setup, and a
        # cluster that is not enabled, may still name the main here.
        from .config_lock import patch_config

        def change(doc):
            if not doc:
                doc.update(json.loads(json.dumps(config)))
            settings = dict(doc.get("cluster") or {})
            requested = args.main_device or settings.get("main_device")
            existing = settings.get("main_device")
            if enabled(doc) and existing and requested and requested != existing:
                raise ValueError(f"This fleet already has a main ({existing}). "
                                 f"Change it with: jarvis cluster main {requested}")
            settings.update(enabled=True,
                            main_device=requested,
                            local_device=args.local_device or settings.get("local_device", "auto"),
                            main_url=args.url or settings.get("main_url", f"http://127.0.0.1:{DEFAULT_PORT}"))
            preview = {**doc, "cluster": settings}
            identity(preview)
            endpoint(preview)
            doc["cluster"] = settings

        proposed, _ = patch_config(state, path, change, backup="cluster-configure", create=True)
        local, main = identity(proposed)
        return {"configured": True, "local_device": local, "main_device": main,
                "role": "main" if local == main else "node", "main_url": endpoint(proposed)}
    if action == "init":
        from .cluster_setup import init
        return init(config, path, state, args.device_id, autostart=not args.no_autostart)
    if action == "doctor":
        from .cluster_setup import doctor
        report = (doctor(config, path, state, fix=True) if getattr(args, "fix", False)
                  else doctor(config, path, state))
        if not sys.stdout.isatty():
            return report
        for check in report["checks"]:  # a checklist for people; JSON when piped
            print(f"{'ok  ' if check['ok'] else 'FAIL'}  {check['check']}: {check['detail']}")
            if check.get("fix"):
                print(f"      fix: {check['fix']}")
        print("All good." if report["ok"] else "Fix the first FAIL, then run jarvis cluster doctor again.")
        # Nested candidate diagnostics only. Never a promotion authorization or verified-AI claim.
        readiness = report.get("main_readiness")
        if isinstance(readiness, dict):
            print()
            print("candidate readiness")
            nested = readiness.get("checks")
            if isinstance(nested, list):
                for check in nested:
                    if not isinstance(check, dict) or not isinstance(check.get("check"), str):
                        continue
                    detail = check.get("detail") if isinstance(check.get("detail"), str) else ""
                    mark = "ok  " if check.get("ok") is True else "FAIL"
                    print(f"{mark}  {check['check']}: {detail}")
                    fix = check.get("fix")
                    if isinstance(fix, str) and fix and check.get("ok") is not True:
                        print(f"      fix: {fix}")
            control = "true" if readiness.get("control_ready") is True else "false"
            ai = "true" if readiness.get("ai_ready") is True else "false"
            print(f"control_ready: {control}")
            print(f"ai_ready: {ai}")
            limits = readiness.get("limitations")
            if isinstance(limits, list):
                for item in limits:
                    if isinstance(item, str) and item:
                        print(f"limit: {item}")
        return None
    if action == "join":
        if not args.value:
            raise ValueError("Usage: jarvis cluster join INVITE_CODE  (from jarvis cluster invite on the main)")
        from .cluster_setup import decode, join
        invite = decode(args.value)
        if invite is not None:
            return join(config, path, state, invite, autostart=not args.no_autostart,
                        start=not args.no_start)
        # Validate the bundle before any credential or config write. The file on disk,
        # read under the fleet lock, decides the role: a stale copy must not demote an
        # enabled main or replace a node credential that a config conflict then discards.
        note = json.loads(Path(args.value).expanduser().read_text(encoding="utf-8"))
        if not isinstance(note, dict):
            raise ValueError("Invalid pairing bundle.")
        for field in ("node_id", "main_device"):
            if not isinstance(note.get(field), str) or not DEVICE_ID.fullmatch(note[field]):
                raise ValueError("Invalid pairing bundle identity.")
        if note["node_id"] == note["main_device"]:
            raise ValueError("A main PC cannot join itself as a node.")
        token = note.get("token")
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise ValueError("Invalid pairing token.")
        raw_devices = note.get("devices", {})
        if raw_devices is None:
            raw_devices = {}
        if not isinstance(raw_devices, dict):
            raise ValueError("Invalid pairing bundle.")
        added = {}
        for name, device in raw_devices.items():
            if isinstance(name, str) and DEVICE_ID.fullmatch(name) and isinstance(device, dict):
                added[name] = {k: device[k] for k in ("hostname", "label", "aliases") if k in device}
        from .deploy import CODE_KEY
        key = note.get("code_key")
        if not isinstance(key, str) or not CODE_KEY.fullmatch(key):
            raise ValueError("The pairing bundle has no code-signing key; enroll again on the updated main.")
        chosen_url = args.url or note.get("main_url")
        if chosen_url is not None and not isinstance(chosen_url, str):
            raise ValueError("Invalid pairing bundle.")
        if isinstance(chosen_url, str):
            endpoint({"cluster": {"main_url": chosen_url}})
        from .config_lock import ConfigUnreadable, config_mutation, read_config, save_delta
        from .fsio import atomic_write
        note_path = state / "cluster-node.json"
        with config_mutation(state):
            if path.is_file():
                try:
                    fresh = read_config(path)
                except ConfigUnreadable as exc:
                    raise ValueError("The config file can't be read; nothing was changed.") from exc
            else:
                fresh = {}
            if not isinstance(fresh, dict):
                raise ValueError("This PC's fleet identity is invalid. Nothing was changed.")
            if "cluster" in fresh and not isinstance(fresh.get("cluster"), dict):
                raise ValueError("This PC's fleet identity is invalid. Nothing was changed.")
            device_map = fresh.get("devices")
            if device_map is None:
                device_map = {}
            if not isinstance(device_map, dict):
                raise ValueError("This PC's fleet identity is invalid. Nothing was changed.")
            if enabled(fresh):
                try:
                    local_id, main_id = identity(fresh)
                except (TypeError, ValueError) as exc:
                    raise ValueError("This PC's fleet identity is invalid. Nothing was changed.") from exc
                if local_id == main_id:
                    raise ValueError(
                        f"This PC is the main ({local_id}). Run jarvis cluster main {note['main_device']} "
                        "first if it should become a node.")
                active = None
                if note_path.is_file():
                    active = read_json(note_path, None)
                active_id = active.get("node_id") if isinstance(active, dict) else None
                active_token = active.get("token") if isinstance(active, dict) else None
                if (isinstance(active_id, str) and active_id != note["node_id"] and active_id == local_id
                        and isinstance(active_token, str) and 20 <= len(active_token) <= 256):
                    raise ValueError(
                        f"This PC is already paired as {local_id}. Nothing was changed.")
            devices = dict(device_map)
            for name, device in added.items():
                devices.setdefault(name, device)
            devices.setdefault(note["main_device"], {})
            devices.setdefault(note["node_id"], {})
            cluster = {**(fresh.get("cluster") or {}), "enabled": True, "local_device": note["node_id"],
                       "main_device": note["main_device"], "main_url": chosen_url}
            proposed = {**fresh, "devices": devices, "cluster": cluster}
            endpoint(proposed)
            identity(proposed)
            if not isinstance(key, str) or not CODE_KEY.fullmatch(key):
                raise ValueError("The pairing bundle has no code-signing key; enroll again on the updated main.")
            prior_raw = note_path.read_bytes() if note_path.is_file() else None
            prior_mode = (note_path.stat().st_mode & 0o777) if prior_raw is not None else 0o600
            wrote = False
            try:
                save_json(note_path, {"node_id": note["node_id"], "main_device": note["main_device"],
                          "token": token, "code_key": key})
                wrote = True
                written, _changed = save_delta(state, path, fresh, proposed, backup="cluster-join")
            except Exception:
                if wrote:
                    if prior_raw is None:
                        note_path.unlink(missing_ok=True)
                    else:
                        atomic_write(note_path, prior_raw, mode=prior_mode or 0o600)
                raise
        return {"joined": note["node_id"], "main_device": note["main_device"],
                "main_url": endpoint(written), "next": "jarvis cluster node"}
    if action == "leave":
        # Before the enabled guard. A stop timeout disables the cluster and asks
        # for leave again; that retry still has to reach cluster_setup.leave.
        from .cluster_setup import leave
        return leave(_path_config(path), path, state)
    if not enabled(config):
        raise ValueError("Configure the main PC first: jarvis cluster configure --main DEVICE --local DEVICE")
    local, main = identity(config)
    if action == "release":
        # Nodes get only code the test suite passed on (release.py); this cuts one now
        # instead of waiting for the hub to notice the tree has settled.
        if local != main:
            raise ValueError("Run jarvis cluster release on the main PC.")
        from .release import cut
        return cut(state, quiet=0)
    if action == "update":
        from .deploy import update
        nodes = [args.value] if args.value else [n["id"] for n in Broker(state).nodes()]
        if not nodes:
            raise ValueError("No enrolled nodes to update.")
        reports = []
        from .release import cut as cut_release
        shared = {}

        def cut_once(*a, **k):
            # One release cut per run: a failed or blocked cut (the suite takes ~9 min) is
            # not repeated for every remaining node.
            if "report" not in shared or shared["report"].get("released"):
                shared["report"] = cut_release(*a, **k)
            return shared["report"]
        for node in nodes:  # one failing node must not stop the rest
            try:
                reports.append(update(config, state, node, rollback=args.rollback, cut=cut_once))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                if args.value:
                    raise
                reports.append({"node": node, "updated": False, "error": str(exc)})
        return reports
    if action == "tunnel":
        from . import cluster_tunnel
        if (local == main) != (cluster_tunnel.mode(config) == "reverse"):
            raise ValueError("The tunnel runs on the main (cluster.tunnel = reverse) or on each "
                             "node (cluster.tunnel = forward); this PC is the other end.")
        if args.value not in ("start", "stop", "status", "run"):
            raise ValueError("Usage: jarvis cluster tunnel start|stop|status [--autostart]")
        if getattr(args, "autostart", False):
            if args.value not in ("start", "stop"):
                raise ValueError("--autostart requires tunnel start or stop.")
            cluster_tunnel.autostart(path, state, args.value == "start")
        if args.value == "run":
            return cluster_tunnel.run(config, state)
        if args.value == "start":
            return cluster_tunnel.start(path, state)
        if args.value == "stop":
            # CLI stop is the operator stop. Lifecycle stop() does not set this.
            return cluster_tunnel.stop(state, operator=True, config=config)
        return getattr(cluster_tunnel, args.value)(state)
    if action == "invite":
        from .cluster_setup import invite
        return invite(config, path, state, args.value, args.address, args.capability, args.output)
    if action in ("enroll", "revoke", "caps"):
        if local != main:
            raise ValueError("Enroll or revoke nodes on the main PC.")
        node = args.value or ""
        if not DEVICE_ID.fullmatch(node) or node == main:
            raise ValueError("Name a valid node other than the main PC.")
        if action == "revoke":
            from .cluster_setup import revoke
            return revoke(state, node)
        if action == "caps":  # keeps the node's credential; it takes effect on its next request
            if not args.capability:
                raise ValueError("Usage: jarvis cluster caps NODE --capability CAP [--capability CAP ...]")
            return {"node": node, "capabilities": Broker(state).set_capabilities(node, args.capability)}
        if not args.output:
            raise ValueError("enroll needs --output PAIRING_FILE; it contains the node's credential.")
        output = Path(args.output).expanduser()
        if output.exists():
            raise ValueError("Pairing output already exists; choose a new filename.")
        # Same receipt file as the console. This id only; a different id is not blocked.
        # The check before code_key is a fast refuse. It is not the one that fences the
        # rotation: code_key and the optional device delta both run before that.
        from .fleet_management import assert_node_admitted, note_uncertain_attempt
        assert_node_admitted(state, node)
        # Signing material before any token or job change. A failure here leaves both alone.
        signing = code_key(state)
        devices = dict(config.get("devices", {}))
        if node not in devices:
            if not args.hostname:
                raise ValueError("A new node needs --hostname HOSTNAME.")
            devices[node] = {"hostname": args.hostname, "label": node}
            from .config_lock import save_delta
            config, _ = save_delta(state, path, config, {**config, "devices": devices},
                                   backup="cluster-enroll")
        caps = args.capability or sorted(NODE_DEFAULT)
        from .cluster_setup import _node_snapshot, _restore_node
        from .config_lock import ConfigUnreadable, config_mutation, read_config
        from .deploy import CODE_KEY
        # After the expensive key prep and the device delta, under the fleet lock, before
        # any credential or pairing-file effect. A fence or a role flip written during
        # code_key is seen here. The bundle's ids, devices and url come from that file.
        with config_mutation(state):
            try:
                fresh = read_config(path)
            except ConfigUnreadable as exc:
                raise ValueError("The config file can't be read; nothing was enrolled.") from exc
            try:
                local, main = identity(fresh)
            except (TypeError, ValueError) as exc:
                raise ValueError("This PC's fleet identity is invalid. Nothing was enrolled.") from exc
            if local != main:
                raise ValueError("This PC is no longer the main; nothing was changed.")
            if node == main:
                raise ValueError("Name a valid node other than the main PC.")
            url = endpoint(fresh)
            if not isinstance(signing, str) or not CODE_KEY.fullmatch(signing):
                raise ValueError("The code-signing key is missing; nothing was enrolled.")
            fresh_devices = fresh.get("devices") if isinstance(fresh.get("devices"), dict) else None
            if not isinstance(fresh_devices, dict) or node not in fresh_devices:
                raise ValueError("A new node needs --hostname HOSTNAME.")
            safe = {name: {k: device[k] for k in ("hostname", "label", "aliases") if k in device}
                    for name, device in fresh_devices.items()}
            bundle = {"node_id": node, "main_device": main, "main_url": url,
                      "capabilities": caps, "devices": safe, "code_key": signing}
            assert_node_admitted(state, node)
            prior = _node_snapshot(state, node)
            token = Broker(state).enroll(node, caps, settle_jobs=False)
            bundle["token"] = token
            try:
                save_json(output, bundle)
            except Exception as exc:
                attempt = hashlib.sha256(token.encode()).hexdigest()
                if _restore_node(state, node, prior, attempt):
                    output.unlink(missing_ok=True)
                    raise
                try:
                    note_uncertain_attempt(state, node)
                except (OSError, ValueError) as fence_exc:
                    raise ValueError(
                        "Enrollment stopped after a credential change and the uncertain receipt "
                        "could not be saved. It was not atomic. Revoke the node to reconcile it, "
                        "then enroll again.") from fence_exc
                raise ValueError(
                    "Enrollment stopped after a credential change and the cleanup could not be verified. "
                    "It was not atomic. Revoke the node to reconcile it, then enroll again.") from exc
        # The file holds the new token. Only now are running jobs uncertain. A job that
        # finished during the write stays done. Best effort: the old token is already dead.
        try:
            Broker(state).settle_rotated(node)
        except Exception:  # noqa: BLE001 - the pairing file is the credential
            pass
        return {"enrolled": node, "capabilities": caps, "pairing_file": str(output),
                "next": "Copy this private bundle to the node and run jarvis cluster join FILE."}
    if action == "autostart":
        if args.value not in ("on", "off"):
            raise ValueError("Usage: jarvis cluster autostart on|off")
        return autostart(path, state, args.value == "on")
    if action == "request":
        if not args.value or not re.fullmatch(r"[a-f0-9]{32}", args.value):
            raise ValueError("request needs a request ID.")
        return Client(config, state, owner=local == main).request("/v1/jobs/" + args.value)
    if action == "status":
        from .deploy import code_fingerprint
        from .release import current
        result = {"role": "main" if local == main else "node", "local_device": local,
                  "code": code_fingerprint(),
                  "main_device": main, "main_url": endpoint(config)}
        if local == main:  # what nodes get: the last tested snapshot
            result["release"] = current(state) or None
        try:
            result.update(Client(config, state, owner=local == main).request("/v1/status", timeout=3))
            result["connected"] = True
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(connected=False, error=str(exc))
        from .continuity.role_status import report
        result.update(report(config, state, base=base))
        return result
    if action == "serve" and local != main:
        raise ValueError("This PC is a node. Run jarvis cluster node.")
    if action == "node" or (action == "run" and local != main):
        worker = NodeWorker(config, state, config_path=path)
        try:
            worker.run()
        except KeyboardInterrupt:
            worker.close()
        return None
    from .ui import serve
    serve(config, base, state, port=args.port)
    return None


def autostart(config_path, state, turn_on, *, restart=False):
    """Run this PC's role (main or node) at login: Windows, macOS or Linux."""
    from .autostart import install
    argv = [sys.executable, "-m", "jarvis", "--config", str(config_path), "--state", str(state), "cluster", "run"]
    return install("jarvis-cluster", "JARVIS Cluster", argv, turn_on,
                   **({"restart": True} if restart else {}))


def proxy_cognition(args, config):
    """No ledger, memory, provider discovery or model process is created on a node."""
    client = Client(config, args.state)
    if args.command == "ask":
        result = client.ask(args.text, args.tier, args.write, args.timeout)
        if args.speak:
            from .speech import Voice
            result["voice"] = Voice(args.state, config).say(result["reply"])
        print(json.dumps({**result, "via": "main", "main_device": client.main}, indent=2))
        return 0
    print(f"JARVIS proxy on {client.local}; requests run on {client.main}. Ctrl+C or exit to quit.")
    voice = None
    if not args.quiet:
        from .speech import Voice
        voice = Voice(args.state, config)
    while True:
        try:
            text = input("you > ").strip()
        except EOFError:
            break
        if text.casefold() in ("exit", "quit"):
            break
        if not text:
            continue
        try:
            result = client.ask(text)
            print(f"jarvis > {result['reply']}")
            if voice:
                voice.say(result["reply"])
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"jarvis > {exc}", file=sys.stderr)
    return 0
