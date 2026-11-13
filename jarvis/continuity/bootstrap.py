"""Local, explicit setup for a managed continuity participant.

Commands are intentionally split: ``preflight`` is read-only, ``stage`` only
copies operator metadata and creates storage directories, ``configure`` updates
the local config pointer with a backup, and ``cutover`` fences only at a
validated authority quiescence boundary. Private key files are never copied.
"""
import json
import ntpath
import os
import posixpath
import stat
import subprocess
from pathlib import Path

from jarvis.fsio import atomic_write


class BootstrapError(RuntimeError):
    pass


_OPERATOR_KEYS = {
    "origin", "authority_id", "device_id", "installation_id", "private_key_path",
    "owner_private_key_path", "fleet_id", "enrollment", "successor_routes",
    "owner_proxy_device", "owner_proxy_clients",
}
_MARKER = "continuity-managed.json"
_SERVICE = "jarvis-cluster.service"


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise BootstrapError("config_unreadable") from None
    if not isinstance(value, dict):
        raise BootstrapError("config_invalid")
    return value


def _private_path(operator_path, value, *, required=True):
    if not isinstance(value, str) or not value:
        if required:
            raise BootstrapError("key_path_missing")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(operator_path).resolve().parent / path
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise BootstrapError("key_file_missing") from None
    if not resolved.is_file():
        raise BootstrapError("key_file_missing")
    return resolved


def _operator(operator_path, client_factory):
    path = Path(operator_path).expanduser().resolve()
    data = _json(path)
    if set(data) - _OPERATOR_KEYS:
        raise BootstrapError("operator_unsupported_fields")
    if any(not isinstance(data.get(k), str) or not data[k].strip()
           for k in ("origin", "authority_id", "device_id", "installation_id", "fleet_id")):
        raise BootstrapError("operator_incomplete")
    if not isinstance(data.get("enrollment"), dict) or not data["enrollment"]:
        raise BootstrapError("enrollment_pins_missing")
    for device, pin in data["enrollment"].items():
        if (not isinstance(device, str) or not device or not isinstance(pin, dict)
                or pin.get("kind") != "pc"
                or not isinstance(pin.get("installation_id"), str) or not pin["installation_id"].strip()
                or not isinstance(pin.get("public_key"), str) or not pin["public_key"].strip()):
            raise BootstrapError("enrollment_pins_missing")
    local = data["device_id"]
    own = data["enrollment"].get(local)
    if not own or own.get("installation_id") != data["installation_id"]:
        raise BootstrapError("local_enrollment_mismatch")
    key = _private_path(path, data.get("private_key_path"))
    owner = _private_path(path, data.get("owner_private_key_path"), required=False)
    try:
        derived = subprocess.run(["ssh-keygen", "-y", "-f", str(key)],
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=5, check=True).stdout.decode("ascii").strip().split()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise BootstrapError("local_key_unreadable") from None
    pin_fields = own["public_key"].strip().split()
    if len(derived) < 2 or len(pin_fields) < 2 or derived[:2] != pin_fields[:2]:
        raise BootstrapError("local_pin_mismatch")
    if not isinstance(data.get("successor_routes"), dict):
        raise BootstrapError("successor_route_unavailable")
    proxy = data.get("owner_proxy_device")
    if proxy is not None and (not isinstance(proxy, str) or proxy not in data["enrollment"]):
        raise BootstrapError("owner_proxy_unavailable")
    proxy_clients = data.get("owner_proxy_clients", ())
    if not isinstance(proxy_clients, (list, tuple)) or any(
            not isinstance(item, str) or item not in data["enrollment"] for item in proxy_clients):
        raise BootstrapError("owner_proxy_unavailable")
    try:
        client, loaded = client_factory(path)
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("operator_authentication_failed") from None
    if (not isinstance(loaded, dict) or loaded.get("device_id") != local
            or getattr(client, "device_id", None) != local
            or getattr(client, "installation_id", None) != data["installation_id"]):
        raise BootstrapError("local_identity_mismatch")
    if owner is not None and getattr(client, "_owner_key", None) is None:
        raise BootstrapError("owner_key_path_invalid")
    try:
        status = client.status()
    except Exception:
        raise BootstrapError("gateway_unavailable") from None
    if (not isinstance(status, dict) or status.get("authority_id") != data["authority_id"]
            or status.get("fleet_id") != data["fleet_id"]):
        raise BootstrapError("gateway_identity_mismatch")
    return path, data, client, status, key, owner


def _hub(hub_config, local, participants):
    config = _json(hub_config)
    from jarvis.topology import identity
    try:
        actual = identity(config)[0]
    except Exception:
        raise BootstrapError("local_topology_invalid") from None
    if actual != local:
        raise BootstrapError("local_identity_mismatch")
    cluster = config.get("cluster")
    if not isinstance(cluster, dict) or cluster.get("enabled") is not True:
        raise BootstrapError("cluster_disabled")
    devices = config.get("devices")
    if not isinstance(devices, dict):
        raise BootstrapError("storage_staging_missing")
    for device in participants:
        if device not in devices or not isinstance(devices[device], dict):
            raise BootstrapError("participant_config_missing")
    return config


def _foreign_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise BootstrapError("storage_path_invalid")
    if ntpath.isabs(value):
        parts = ntpath.normpath(value).replace("/", "\\").split("\\")
        if ".." in parts:
            raise BootstrapError("storage_path_invalid")
        return value
    if posixpath.isabs(value):
        if ".." in value.split("/"):
            raise BootstrapError("storage_path_invalid")
        return value
    raise BootstrapError("storage_path_invalid")


def _local_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise BootstrapError("storage_path_invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise BootstrapError("storage_path_invalid")
    path = path.resolve()
    from .brain_channel import _outside_git
    try:
        _outside_git(path)
    except Exception:
        raise BootstrapError("storage_staging_invalid") from None
    return path


def _settings_manifest(path, participant_ids):
    if path is None:
        return None
    manifest = _json(path)
    if set(manifest) - {"schema", "staging_dirs", "brainsync_enabled"} or manifest.get("schema") != 1:
        raise BootstrapError("settings_manifest_invalid")
    roots = manifest.get("staging_dirs")
    if not isinstance(roots, dict) or not roots or set(roots) - set(participant_ids):
        raise BootstrapError("settings_manifest_invalid")
    if "brainsync_enabled" in manifest and type(manifest["brainsync_enabled"]) is not bool:
        raise BootstrapError("settings_manifest_invalid")
    for device, root in roots.items():
        if not isinstance(device, str) or not device:
            raise BootstrapError("settings_manifest_invalid")
        if not isinstance(root, str) or not root:
            raise BootstrapError("settings_manifest_invalid")
    return manifest


def _apply_settings(hub, participants, local, manifest):
    merged = json.loads(json.dumps(hub))
    if manifest:
        for device, root in manifest["staging_dirs"].items():
            merged["devices"][device]["brain_transfer_dir"] = root
        if "brainsync_enabled" in manifest:
            sync = merged.get("brainsync")
            if sync is None:
                sync = {}
            if not isinstance(sync, dict):
                raise BootstrapError("brainsync_config_invalid")
            sync["enabled"] = manifest["brainsync_enabled"]
            merged["brainsync"] = sync
    for device in participants:
        record = merged["devices"].get(device) or {}
        if not isinstance(record, dict):
            raise BootstrapError("participant_config_missing")
        root = record.get("brain_transfer_dir")
        if device == local:
            record["brain_transfer_dir"] = str(_local_path(root))
        else:
            record["brain_transfer_dir"] = _foreign_path(root)
    return merged


def _storage_path(hub, local):
    record = hub.get("devices", {}).get(local)
    root = record.get("brain_transfer_dir") if isinstance(record, dict) else None
    if not isinstance(root, str) or not root:
        raise BootstrapError("storage_staging_missing")
    return _local_path(root)


def _prepare_storage(path, device_id):
    """Claim a dedicated local staging directory without chmodding user/system roots."""
    path = Path(path).resolve()
    if (path == path.parent or path == Path.home().resolve()
            or path == Path(os.path.abspath(os.sep))):
        raise BootstrapError("storage_path_too_broad")
    marker = path / ".jarvis-continuity-storage.json"
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise BootstrapError("storage_path_invalid")
        prior = _read_json_optional(marker)
        if prior is None:
            try:
                if any(path.iterdir()):
                    raise BootstrapError("storage_directory_unclaimed")
            except OSError:
                raise BootstrapError("storage_path_invalid") from None
        elif prior != {"schema": 1, "device_id": device_id, "path": str(path)}:
            raise BootstrapError("storage_owner_mismatch")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    atomic_write(marker, json.dumps({"schema": 1, "device_id": device_id,
                                     "path": str(path)}, sort_keys=True),
                 mode=0o600, fsync=True)
    return path


def _read_json_optional(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _operator_target():
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "jarvis" / "continuity" / "operator.json"


def _default_lifecycle(hub_config, state, device_id):
    if os.name == "nt":
        from .local_lifecycle import WindowsNodeLifecycle
        return WindowsNodeLifecycle(hub_config, state, device_id)
    import jarvis.rest
    return jarvis.rest


def _cutover_tuple(status, mode, version):
    if (not isinstance(status, dict) or status.get("mode") != mode
            or type(status.get("mode_version")) is not int or status["mode_version"] != version):
        raise BootstrapError("gateway_tuple_mismatch")
    if (type(status.get("incarnation")) is not int or type(status.get("epoch")) is not int
            or status["incarnation"] < 0 or status["epoch"] < 0):
        raise BootstrapError("gateway_tuple_invalid")
    if status.get("transition") not in (None, {}):
        raise BootstrapError("gateway_transition_open")
    if status.get("lease_valid") is not False or status.get("main_ready") is not False:
        raise BootstrapError("gateway_not_quiescent")
    return status["incarnation"], status["epoch"]


def _validate_metadata(operator, hub):
    """Validate production pins/routes/storage without opening writable state."""
    from .production import _pins, _route
    pins = _pins(operator)
    from .gateway import _public_key
    try:
        for pin in pins.values():
            _public_key(pin["public_key"])
    except Exception:
        raise BootstrapError("enrollment_pins_invalid") from None
    local = operator["device_id"]
    if pins.get(local, {}).get("installation_id") != operator["installation_id"]:
        raise BootstrapError("local_enrollment_mismatch")
    for device in pins:
        _route(operator, device)


def _validate_production(operator_path, operator, hub, state):
    """Construct real adapters without writing the managed marker or starting work."""
    from . import startup
    from .participant import load_local
    from .production import build_controller
    participant = load_local(hub, state, probe=startup.local_probe(state))
    guard = startup._open_guard(state, participant, operator, boot=None)
    if guard is None:
        raise BootstrapError("executor_config_incomplete")
    participant.guard = guard
    controller = None
    try:
        controller = build_controller(participant, hub, Path(state).resolve().parent,
                                     state, operator_path, operator)
        if (not callable(controller.issuer) and not callable(controller._production_issuer)
                or not callable(getattr(participant.runtime, "require_authority", None))
                or not callable(getattr(guard, "fence", None))):
            raise BootstrapError("production_binding_incomplete")
    except Exception:
        guard.close()
        raise
    return participant, guard, controller


def _validate_binding(operator_path, operator, hub, state, client):
    """Build the runtime/issuer composition in memory without touching state."""
    from . import startup
    from .participant import Participant
    from .production import build_controller
    from .runtime import GatewayPort, MainRuntime
    participant = Participant(client,
        MainRuntime(GatewayPort(client), client.device_id, client.installation_id,
                    startup.local_probe(state)),
        config=hub, state=state)
    controller = build_controller(participant, hub, Path(state).resolve().parent,
                                  state, operator_path, operator)
    if (not callable(controller._production_issuer)
            or not callable(getattr(participant.runtime, "require_authority", None))):
        raise BootstrapError("production_binding_incomplete")
    return participant, controller


def preflight(hub_config, state, operator_config, *, settings_manifest=None, client_factory=None):
    """Read-only preflight. Output deliberately omits paths, keys, routes, and status."""
    from .cli import build_client
    factory = client_factory or build_client
    path, operator, client, status, key, owner = _operator(operator_config, factory)
    participant_ids = tuple(operator["enrollment"])
    hub = _hub(hub_config, operator["device_id"], participant_ids)
    _validate_metadata(operator, hub)
    try:
        manifest = _settings_manifest(settings_manifest, participant_ids)
        effective = _apply_settings(hub, participant_ids, operator["device_id"], manifest)
        _validate_binding(path, operator, effective, state, client)
        storage = _storage_path(effective, operator["device_id"])
        blockers = []
    except BootstrapError as exc:
        effective, storage = hub, None
        blockers = [str(exc)]
    marker = Path(state) / _MARKER
    return {
        "ok": not blockers,
        "device_id": operator["device_id"],
        "gateway_mode": status.get("mode"),
        "gateway_mode_version": status.get("mode_version"),
        "local_key_available": key.is_file(),
        "owner_signer_available": bool(owner and owner.is_file()),
        "managed_marker_present": marker.is_file(),
        "local_storage_ready": bool(storage and storage.is_dir()),
        "brainsync_enabled": (effective.get("brainsync") or {}).get("enabled") is True,
        "blockers": blockers,
        "stage_required": not marker.is_file(),
        "activation_performed": False,
    }


def stage(operator_config, staging_dir, *, hub_config, settings_manifest, client_factory=None):
    """Prepare local operator metadata and storage without changing the live config."""
    from .cli import build_client
    path, operator, _client, _status, key, owner = _operator(
        operator_config, client_factory or build_client)
    participant_ids = tuple(operator["enrollment"])
    hub = _hub(hub_config, operator["device_id"], participant_ids)
    _validate_metadata(operator, hub)
    manifest = _settings_manifest(settings_manifest, participant_ids)
    effective = _apply_settings(hub, participant_ids, operator["device_id"], manifest)
    configured_storage = _storage_path(effective, operator["device_id"])
    target = Path(staging_dir).expanduser().resolve()
    if (target == configured_storage or target in configured_storage.parents
            or configured_storage in target.parents):
        raise BootstrapError("staging_paths_overlap")
    from .brain_channel import _outside_git
    try:
        _outside_git(target)
    except Exception:
        raise BootstrapError("staging_inside_checkout") from None
    destination = target / "continuity-operator.json"
    for directory in (target, configured_storage):
        if directory == Path.cwd().resolve() or Path.cwd().resolve() in directory.parents:
            raise BootstrapError("staging_inside_checkout")
    if destination.exists():
        raise BootstrapError("staging_exists")
    for directory in (target, configured_storage):
        if directory == target:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
    configured_storage = _prepare_storage(configured_storage, operator["device_id"])
    # Keep the same key references; never copy the key files or their contents.
    staged = dict(operator)
    staged["private_key_path"] = str(key)
    if owner is not None:
        staged["owner_private_key_path"] = str(owner)
    atomic_write(destination, json.dumps(staged, indent=2, sort_keys=True) + "\n", mode=0o600, fsync=True)
    manifest_destination = target / "continuity-settings-manifest.json"
    try:
        atomic_write(manifest_destination,
                     json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                     mode=0o600, fsync=True)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"ok": True, "operator_staged": True, "storage_staged": True,
            "local_key_copied": False, "owner_key_copied": False,
            "operator_config": str(destination), "settings_manifest": str(manifest_destination),
            "storage_directory": str(configured_storage),
            "managed_marker_created": False, "activation_performed": False}


def configure(hub_config, state, operator_config, *, settings_manifest, confirmed=False,
              client_factory=None):
    """Install validated local config references with durable backups, still inactive."""
    if confirmed is not True:
        raise BootstrapError("confirmation_required")
    from .cli import build_client
    path, operator, client, status, _key, _owner = _operator(
        operator_config, client_factory or build_client)
    hub_path = Path(hub_config).expanduser().resolve()
    participant_ids = tuple(operator["enrollment"])
    hub = _hub(hub_path, operator["device_id"], participant_ids)
    _validate_metadata(operator, hub)
    manifest = _settings_manifest(settings_manifest, participant_ids)
    updated = _apply_settings(hub, participant_ids, operator["device_id"], manifest)
    _validate_binding(path, operator, updated, state, client)
    if not _storage_path(updated, operator["device_id"]).is_dir():
        raise BootstrapError("storage_not_staged")
    if status.get("mode") not in ("LEGACY_ACTIVE", "QUIESCED", "V2_ONLY_NO_MAIN"):
        raise BootstrapError("gateway_state_unsupported")
    target = _operator_target()
    from .brain_channel import _outside_git
    try:
        _outside_git(target.resolve())
    except Exception:
        raise BootstrapError("operator_target_in_checkout") from None
    if target.exists():
        raise BootstrapError("operator_target_exists")
    backup = hub_path.with_name(hub_path.name + ".pre-continuity-bootstrap")
    if backup.exists():
        raise BootstrapError("backup_exists")
    updated = dict(updated)
    cont = dict(updated.get("continuity") or {})
    if cont.get("operator_config"):
        raise BootstrapError("operator_config_already_set")
    cont["operator_config"] = str(target)
    updated["continuity"] = cont
    from jarvis.config_lock import config_mutation
    with config_mutation(state):
        # Network/key checks above may take time. Never publish their stale
        # role/configuration over a concurrent owner edit.
        if _json(hub_path) != hub:
            raise BootstrapError("local_config_changed")
        if target.exists():
            raise BootstrapError("operator_target_exists")
        if backup.exists():
            raise BootstrapError("backup_exists")
        original = hub_path.read_bytes()
        config_mode = stat.S_IMODE(hub_path.stat().st_mode)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        atomic_write(backup, original, mode=0o600, fsync=True)
        try:
            installed = dict(operator)
            installed["private_key_path"] = str(_private_path(path, operator["private_key_path"]))
            owner_path = _private_path(path, operator.get("owner_private_key_path"), required=False)
            if owner_path is not None:
                installed["owner_private_key_path"] = str(owner_path)
            atomic_write(target, json.dumps(installed, indent=2, sort_keys=True) + "\n", mode=0o600, fsync=True)
            atomic_write(hub_path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
                         mode=config_mode, fsync=True)
        except Exception:
            target.unlink(missing_ok=True)
            atomic_write(hub_path, original, mode=config_mode, fsync=True)
            raise
    return {"ok": True, "configured": True, "config_backup": str(backup),
            "operator_config": str(target), "managed_marker_created": False,
            "gateway_mode": status.get("mode"), "activation_performed": False}


def cutover(hub_config, state, operator_config, *, expect_mode, expect_version,
            confirmed=False, client_factory=None, lifecycle=None):
    """Fence this local participant only after existing JARVIS service quiesces.

    The lifecycle object is the existing ``jarvis.rest`` module. It is injectable
    solely to test the state machine without controlling a workstation service.
    """
    if confirmed is not True:
        raise BootstrapError("confirmation_required")
    if expect_mode not in ("QUIESCED", "V2_ONLY_NO_MAIN"):
        raise BootstrapError("unsupported_cutover_mode")
    if type(expect_version) is not int or expect_version < 0:
        raise BootstrapError("bad_mode_version")
    if lifecycle is not None:
        if not lifecycle.supported():
            raise BootstrapError("local_lifecycle_unsupported")
        if lifecycle.active(getattr(lifecycle, "service_name", _SERVICE)) is not True:
            raise BootstrapError("local_service_not_active")
    from .cli import build_client
    path, operator, client, status, _key, _owner = _operator(
        operator_config, client_factory or build_client)
    hub_path = Path(hub_config).expanduser().resolve()
    participant_ids = tuple(operator["enrollment"])
    hub = _hub(hub_path, operator["device_id"], participant_ids)
    lifecycle = lifecycle or _default_lifecycle(hub_path, state, operator["device_id"])
    if not lifecycle.supported():
        raise BootstrapError("local_lifecycle_unsupported")
    service_name = getattr(lifecycle, "service_name", _SERVICE)
    if lifecycle.active(service_name) is not True:
        raise BootstrapError("local_service_not_active")
    configured_pointer = (hub.get("continuity") or {}).get("operator_config")
    if not isinstance(configured_pointer, str) or not configured_pointer:
        raise BootstrapError("local_config_not_configured")
    configured_path = Path(configured_pointer).expanduser()
    if not configured_path.is_absolute():
        configured_path = hub_path.parent / configured_path
    if configured_path.resolve() != path:
        raise BootstrapError("local_operator_config_mismatch")
    before_tuple = _cutover_tuple(status, expect_mode, expect_version)
    _validate_metadata(operator, hub)
    hub = _apply_settings(hub, participant_ids, operator["device_id"], None)
    if not _storage_path(hub, operator["device_id"]).is_dir():
        raise BootstrapError("storage_not_staged")
    _validate_binding(path, operator, hub, state, client)
    guard = None
    paused = False
    pause_attempted = False
    try:
        pause_attempted = True
        paused_result = lifecycle.pause(state)
        paused = paused_result.get("paused") is True
        if not paused or paused_result.get("failed"):
            raise BootstrapError("local_service_stop_failed")
        if service_name not in paused_result.get("stopped", []):
            raise BootstrapError("local_service_stop_unverified")
        if lifecycle.active(service_name) is not False:
            raise BootstrapError("local_service_stop_unverified")
        after = client.status()
        after_tuple = _cutover_tuple(after, expect_mode, expect_version)
        if after_tuple != before_tuple:
            raise BootstrapError("gateway_tuple_moved")
        participant, guard, controller = _validate_production(path, operator, hub, state)
        from . import integration
        from .participant import bind
        bind(state, participant)
        integration.configure_production(state, participant.runtime, guard, controller, controller._production_issuer)
        ack_version = expect_version + (1 if expect_mode == "QUIESCED" else 0)
        result = guard.fence(after["incarnation"], after["epoch"], ack_version)
        local_view = guard.local_view()
        if (not isinstance(result, dict) or result.get("authority_recorded") is not True
                or result.get("mode_version") != ack_version
                or local_view.get("fenced_epoch") != after["epoch"]
                or local_view.get("legacy_closed") is not True):
            raise BootstrapError("durable_fence_unverified")
        return {"ok": True, "activated": True, "mode": expect_mode,
                "mode_version_acknowledged": ack_version, "fenced_epoch": after["epoch"],
                "managed_marker_created": True, "durable_ack_recorded": True,
                "local_service_restored": True}
    finally:
        try:
            if guard is not None:
                guard.close()
        finally:
            if pause_attempted:
                restored = lifecycle.wake(state)
                if restored.get("failed"):
                    raise BootstrapError("local_service_restore_failed")
