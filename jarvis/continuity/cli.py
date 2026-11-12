"""Operator CLI for the continuity authority. Config contains paths, never key material."""
import json
from pathlib import Path

from .gateway import AuthorityClient
from . import authority


def add_arguments(parser):
    sub = parser.add_subparsers(dest="continuity_action", required=True)
    sub.add_parser("status")
    en = sub.add_parser("enroll"); en.add_argument("device")
    sub.add_parser("ack")
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
        elif action == "ack":
            status = client.status()
            result = client.acknowledge(status["mode_version"], status["epoch"])
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
        output(json.dumps(result, indent=2, ensure_ascii=False)); return 0
    except Exception as exc:
        output(json.dumps({"error": str(exc)})); return 1
