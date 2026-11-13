"""Operator CLI for the continuity authority. Config contains paths, never key material."""
import json
from pathlib import Path

from .gateway import AuthorityClient
from . import authority


def add_arguments(parser):
    sub = parser.add_subparsers(dest="continuity_action", required=True)
    bootstrap = sub.add_parser("bootstrap", help="stage and explicitly enroll this local PC")
    boot = bootstrap.add_subparsers(dest="bootstrap_action", required=True)
    pre = boot.add_parser("preflight", help="read-only local enrollment and gateway check")
    _bootstrap_args(pre)
    pre.add_argument("--settings-manifest")
    stage = boot.add_parser("prepare", aliases=["stage"],
                            help="stage operator metadata and transfer storage")
    stage.add_argument("--hub-config", required=True)
    stage.add_argument("--operator-config", required=True)
    stage.add_argument("--staging-dir", required=True)
    stage.add_argument("--settings-manifest", required=True)
    conf = boot.add_parser("configure", help="write local operator pointer with backups")
    _bootstrap_args(conf)
    conf.add_argument("--settings-manifest", required=True)
    conf.add_argument("--yes", action="store_true", help="confirm this local configuration change")
    cut = boot.add_parser("cutover", help="fence local admission at an authority quiescence boundary")
    _bootstrap_args(cut)
    cut.add_argument("--expect-mode", choices=("QUIESCED", "V2_ONLY_NO_MAIN"), required=True)
    cut.add_argument("--expect-version", type=int, required=True)
    cut.add_argument("--yes", action="store_true", help="confirm local service stop and fence")
    sub.add_parser("status")
    recover = sub.add_parser("recover", aliases=["lease"])
    recover.add_argument("--hub-config", required=True, help="local JARVIS config (read only)")
    recover.add_argument("--state", dest="recovery_state", required=True, help="local running hub state directory")
    recover.add_argument("--request-id", required=True, help="stable manual recovery request id")
    switch = sub.add_parser("main", help="safe MAIN handoff through the local hub")
    switch.add_argument("device")
    switch.add_argument("--hub-config", required=True, help="local JARVIS config (read only)")
    switch.add_argument("--state", dest="recovery_state", required=True, help="local running hub state directory")
    prepare = sub.add_parser("return-prepare", help="offline, explicit hash-bound archive of a frozen former MAIN")
    prepare.add_argument("--hub-config", required=True)
    prepare.add_argument("--state", dest="recovery_state", required=True)
    prepare.add_argument("--bundle", required=True)
    prepare.add_argument("--incoming-memory-sha256", required=True)
    prepare.add_argument("--current-memory-sha256", required=True)
    prepare.add_argument("--freeze-sha256", required=True)
    retire = sub.add_parser("return-retire", help="retire a reviewed interrupted return approval without moving memory")
    retire.add_argument("--hub-config", required=True)
    retire.add_argument("--state", dest="recovery_state", required=True)
    retire.add_argument("--incoming-memory-sha256", required=True)
    retire.add_argument("--current-memory-sha256", required=True)
    retire.add_argument("--freeze-sha256", required=True)
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
    parser.add_argument("--config", help="continuity operator JSON (paths to keys, never key material)")


def _bootstrap_args(parser):
    parser.add_argument("--hub-config", required=True, help="local JARVIS config")
    parser.add_argument("--state", required=True, help="local JARVIS state directory")
    parser.add_argument("--operator-config", required=True, help="locally provisioned operator JSON")


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
        action = args.continuity_action
        if action == "bootstrap":
            from . import bootstrap
            subaction = args.bootstrap_action
            try:
                if subaction == "preflight":
                    result = bootstrap.preflight(args.hub_config, args.state, args.operator_config,
                                                 settings_manifest=args.settings_manifest,
                                                 client_factory=client_factory)
                elif subaction in ("prepare", "stage"):
                    result = bootstrap.stage(args.operator_config, args.staging_dir,
                                             hub_config=args.hub_config,
                                             settings_manifest=args.settings_manifest,
                                             client_factory=client_factory)
                elif subaction == "configure":
                    if not args.yes and confirm("Configure this local PC for continuity? [y/N] ").strip().lower() != "y":
                        return 1
                    result = bootstrap.configure(args.hub_config, args.state, args.operator_config,
                                                 settings_manifest=args.settings_manifest,
                                                 confirmed=True, client_factory=client_factory)
                elif subaction == "cutover":
                    if not args.yes and confirm("Stop local JARVIS and fence continuity admission? [y/N] ").strip().lower() != "y":
                        return 1
                    result = bootstrap.cutover(args.hub_config, args.state, args.operator_config,
                        expect_mode=args.expect_mode, expect_version=args.expect_version,
                        confirmed=True, client_factory=client_factory)
                else:
                    raise bootstrap.BootstrapError("unknown_bootstrap_action")
            except Exception as exc:
                code = getattr(exc, "code", None)
                safe = str(code) if isinstance(code, str) else (
                    str(exc) if isinstance(exc, bootstrap.BootstrapError) else type(exc).__name__)
                output(json.dumps({"error": safe}))
                return 1
            output(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if not getattr(args, "config", None):
            raise ValueError("--config is required for this continuity action")
        client, cfg = client_factory(args.config)
        if action != "status" and not args.yes and confirm("Proceed? [y/N] ").strip().lower() != "y":
            return 1
        if action == "status": result = client.status()
        elif action == "return-prepare":
            from .return_brain import prepare
            hub_config = json.loads(Path(args.hub_config).read_text(encoding="utf-8"))
            result = prepare(hub_config, args.recovery_state, client, args.bundle,
                incoming_sha256=args.incoming_memory_sha256, current_sha256=args.current_memory_sha256,
                freeze_sha256=args.freeze_sha256, confirmed=True)
        elif action == "return-retire":
            from .return_brain import retire_interrupted
            hub_config = json.loads(Path(args.hub_config).read_text(encoding="utf-8"))
            result = retire_interrupted(hub_config, args.recovery_state, client,
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
        elif action == "main":
            from jarvis.topology import identity

            from .hub_control import switch_main
            from .repoint import effective_config
            hub_config = json.loads(Path(args.hub_config).read_text(encoding="utf-8"))
            hub_config = effective_config(hub_config, args.recovery_state)
            if cfg.get("device_id") != identity(hub_config)[0]:
                raise ValueError("operator config does not belong to the local PC")
            result = switch_main(hub_config, args.recovery_state, args.device)
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
        return 1 if action in ("recover", "lease", "main") and result.get("ok") is not True else 0
    except Exception as exc:
        output(json.dumps({"error": str(exc)})); return 1
