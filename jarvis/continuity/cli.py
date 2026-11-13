"""Operator CLI for the continuity authority. Config contains paths, never key material."""
import json
from pathlib import Path

from .gateway import AuthorityClient
from . import authority


def add_arguments(parser):
    sub = parser.add_subparsers(dest="continuity_action", required=True)
    sub.add_parser("status")
    recover = sub.add_parser("recover", aliases=["lease"])
    recover.add_argument("--hub-config", required=True, help="local JARVIS config (read only)")
    recover.add_argument("--state", dest="recovery_state", required=True, help="local running hub state directory")
    recover.add_argument("--request-id", required=True, help="stable manual recovery request id")
    prepare = sub.add_parser("return-prepare", help="offline, explicit hash-bound archive of a frozen former MAIN")
    prepare.add_argument("--hub-config", required=True)
    prepare.add_argument("--state", dest="recovery_state", required=True)
    prepare.add_argument("--bundle", required=True)
    prepare.add_argument("--incoming-memory-sha256", required=True)
    prepare.add_argument("--current-memory-sha256", required=True)
    prepare.add_argument("--freeze-sha256", required=True)
    refreeze = sub.add_parser("return-refreeze", help="preserve incoming memory and restore freeze after a failed return")
    refreeze.add_argument("--state", dest="recovery_state", required=True)
    en = sub.add_parser("enroll"); en.add_argument("device")
    ack = sub.add_parser("ack")
    ack.add_argument("--next-version", action="store_true",
                     help="acknowledge fencing for the next mode version while QUIESCED")
    mode = sub.add_parser("mode"); mode.add_argument("mode"); mode.add_argument("--expect-version", type=int, required=True)
    rm = sub.add_parser("remove"); rm.add_argument("device")
    rm.add_argument("--installation", help="installation id (default: from the config's enrollment record)")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--config", required=True, help="continuity operator JSON (paths to keys, never key material)")


def build_client(config_path):
    """Read JSON keys: origin, authority_id, device_id, installation_id,
    private_key_path, owner_private_key_path. Paths may be relative to JSON file.
    """
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    base = Path(config_path).resolve().parent
    def path(key):
        p = Path(data[key]).expanduser()
        return str(p if p.is_absolute() else base / p)
    return AuthorityClient(data["origin"], authority_id=data["authority_id"],
        device_id=data["device_id"], installation_id=data["installation_id"],
        private_key_path=path("private_key_path"),
        owner_private_key_path=path("owner_private_key_path") if data.get("owner_private_key_path") else None), data


def run(args, client_factory=build_client, output=print, confirm=input):
    try:
        client, cfg = client_factory(args.config)
        action = args.continuity_action
        if action != "status" and not args.yes and confirm("Proceed? [y/N] ").strip().lower() != "y":
            return 1
        if action == "status": result = client.status()
        elif action == "return-prepare":
            from .return_brain import prepare
            hub_config = json.loads(Path(args.hub_config).read_text(encoding="utf-8"))
            result = prepare(hub_config, args.recovery_state, client, args.bundle,
                incoming_sha256=args.incoming_memory_sha256, current_sha256=args.current_memory_sha256,
                freeze_sha256=args.freeze_sha256, confirmed=True)
        elif action == "return-refreeze":
            from .return_brain import refreeze
            from types import SimpleNamespace
            result = refreeze(SimpleNamespace(client=client, state=args.recovery_state))
        elif action in ("recover", "lease"):
            from .hub_control import request_recover
            hub_config = json.loads(Path(args.hub_config).read_text(encoding="utf-8"))
            from jarvis.topology import identity
            device = identity(hub_config)[0]
            if cfg.get("device_id") != device:
                raise ValueError("operator config does not belong to the local PC")
            result = request_recover(hub_config, args.recovery_state, device, args.request_id)
        elif action == "ack":
            status = client.status()
            next_version = getattr(args, "next_version", False)
            if next_version and status["mode"] not in ("QUIESCED", "ROLLBACK_QUIESCED"):
                raise ValueError("ack --next-version requires QUIESCED or ROLLBACK_QUIESCED")
            result = client.acknowledge(status["mode_version"] + int(next_version), status["epoch"])
        elif action == "mode":
            target = args.mode.upper()
            current = client.status()["mode"]
            if target == "V2_ACTIVE" or target not in authority.MODE_EDGES.get(current, set()):
                raise ValueError("mode edge is not operator-settable")
            result = client.set_mode(target, args.expect_version)
        elif action == "enroll":
            record = cfg.get("enrollment", {}).get(args.device)
            if not record: raise ValueError("enrollment record missing from config")
            result = client.enroll(record)
        else:
            if hasattr(client, "disable_participant"):
                record = cfg.get("enrollment", {}).get(args.device) or {}
                installation = getattr(args, "installation", None) or record.get("installation_id")
                if not installation: raise ValueError("installation id missing: pass --installation")
                result = client.disable_participant(args.device, installation)
            else:
                output(json.dumps({"error": "not available yet"})); return 2
        output(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if action in ("recover", "lease") and result.get("ok") is not True else 0
    except Exception as exc:
        output(json.dumps({"error": str(exc)})); return 1
