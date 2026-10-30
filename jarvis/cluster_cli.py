"""Explicit setup, pairing, revocation and startup for the main/node topology."""

import json
import re
import sys
from pathlib import Path

from .cluster import Client, NodeWorker, save_json
from .cluster_store import CAPABILITIES, NODE_DEFAULT, Broker
from .deploy import code_key
from .topology import DEFAULT_PORT, DEVICE_ID, enabled, endpoint, identity


def parser(sub):
    command = sub.add_parser("cluster", help="Configure the main PC and its lightweight nodes",
        description="Setup: on the main `jarvis cluster init` then `jarvis cluster invite NODE`; "
                    "on each node `jarvis cluster join CODE`. `jarvis cluster doctor` on either "
                    "says what is wrong and how to fix it.")
    command.add_argument("action", choices=("init", "invite", "join", "leave", "doctor",
                                           "configure", "enroll", "revoke", "caps", "status",
                                           "run", "serve", "node", "request", "autostart", "tunnel", "update", "release", "main", "roles"))
    command.add_argument("value", nargs="?", help="node ID, invite code or file, request ID, or on/off")
    command.add_argument("--id", dest="device_id", help="init: this PC's device ID (default: from its hostname)")
    command.add_argument("--address", action="append", default=[],
                         help="invite: an address nodes can reach this main at (repeatable; LAN and Tailscale are found)")
    command.add_argument("--no-autostart", action="store_true", help="init/join: don't start at login")
    command.add_argument("--no-start", action="store_true", help="join: save the link without connecting")
    command.add_argument("--main", dest="main_device", help="device ID of the main PC")
    command.add_argument("--local", dest="local_device", help="this PC's device ID, or auto")
    command.add_argument("--url", help="main HTTPS origin, or loopback HTTP through SSH")
    command.add_argument("--hostname", help="enroll: hostname of a new node")
    command.add_argument("--capability", action="append", choices=sorted(CAPABILITIES),
                         help="enroll/invite/caps: what the node may do (default: "
                              + ", ".join(sorted(NODE_DEFAULT)) + ")")
    command.add_argument("--output", help="enroll: write the one-time pairing bundle here")
    command.add_argument("--port", type=int, default=7777, help="main: local console port")
    command.add_argument("--rollback", action="store_true", help="update: swap a node back to its previous code")
    command.add_argument("--autostart", action="store_true", help="tunnel start/stop: also enable/disable login startup")


def roles(config):
    """Every configured machine and its role. Exactly one is main: cluster.main_device."""
    main = (config.get("cluster") or {}).get("main_device")
    local = identity(config)[0] if (config.get("cluster") or {}).get("enabled") else None
    return {"main_device": main, "tunnel": (config.get("cluster") or {}).get("tunnel", "reverse"),
            "devices": {key: {"label": (device or {}).get("label") or key,
                              "role": "main" if key == main else "node", "this_pc": key == local}
                        for key, device in (config.get("devices") or {}).items()}}


def set_main(config, path, device):
    """Make one configured machine the main; every other machine becomes a node."""
    devices = config.get("devices") or {}
    if not device:
        raise ValueError("Usage: jarvis cluster main DEVICE_ID  (one of: " + ", ".join(devices) + ")")
    if device not in devices:
        raise ValueError(f"No device '{device}'. Configured: {', '.join(devices)}.")
    proposed = {**config, "cluster": {**(config.get("cluster") or {}), "main_device": device}}
    save_json(path, proposed)
    return {**roles(proposed), "next": "Copy this jarvis.config.json to every machine (or "
            "commit it), restart `jarvis cluster run` on the new main and `jarvis cluster node` "
            "on the others. See docs/architecture-guide.md#main-and-nodes."}


def run(args, config, base):
    action = args.action
    path, state = Path(args.config).resolve(), Path(args.state).resolve()
    if action in ("run", "serve", "node") and sys.stdout is None:
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(logs / "cluster.log", "a", encoding="utf-8", buffering=1)
    if action == "roles":
        return roles(config)
    if action == "main":
        return set_main(config, path, args.value)
    if action == "configure":
        settings = dict(config.get("cluster", {}))
        settings.update(enabled=True,
                        main_device=args.main_device or settings.get("main_device"),
                        local_device=args.local_device or settings.get("local_device", "auto"),
                        main_url=args.url or settings.get("main_url", f"http://127.0.0.1:{DEFAULT_PORT}"))
        proposed = {**config, "cluster": settings}
        # auto can be used in a shared checkout, but explicit IDs are recommended for nodes.
        local, main = identity(proposed)
        endpoint(proposed)
        save_json(path, proposed)
        return {"configured": True, "local_device": local, "main_device": main,
                "role": "main" if local == main else "node", "main_url": endpoint(proposed)}
    if action == "init":
        from .cluster_setup import init
        return init(config, path, state, args.device_id, autostart=not args.no_autostart)
    if action == "doctor":
        from .cluster_setup import doctor
        report = doctor(config, path, state)
        if not sys.stdout.isatty():
            return report
        for check in report["checks"]:  # a checklist for people; JSON when piped
            print(f"{'ok  ' if check['ok'] else 'FAIL'}  {check['check']}: {check['detail']}")
            if check.get("fix"):
                print(f"      fix: {check['fix']}")
        print("All good." if report["ok"] else "Fix the first FAIL, then run jarvis cluster doctor again.")
        return None
    if action == "join":
        if not args.value:
            raise ValueError("Usage: jarvis cluster join INVITE_CODE  (from jarvis cluster invite on the main)")
        from .cluster_setup import decode, join
        invite = decode(args.value)
        if invite is not None:
            return join(config, path, state, invite, autostart=not args.no_autostart,
                        start=not args.no_start)
        note = json.loads(Path(args.value).expanduser().read_text(encoding="utf-8"))
        for key in ("node_id", "main_device"):
            if not isinstance(note.get(key), str) or not DEVICE_ID.fullmatch(note[key]):
                raise ValueError("Invalid pairing bundle identity.")
        if note["node_id"] == note["main_device"]:
            raise ValueError("A main PC cannot join itself as a node.")
        token = note.get("token")
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise ValueError("Invalid pairing token.")
        devices = dict(config.get("devices", {}))
        for name, device in note.get("devices", {}).items():
            if DEVICE_ID.fullmatch(name) and isinstance(device, dict):
                devices.setdefault(name, {k: device[k] for k in ("hostname", "label", "aliases") if k in device})
        devices.setdefault(note["main_device"], {})
        devices.setdefault(note["node_id"], {})
        proposed = {**config, "devices": devices, "cluster": {**config.get("cluster", {}),
                    "enabled": True, "local_device": note["node_id"],
                    "main_device": note["main_device"], "main_url": args.url or note.get("main_url")}}
        endpoint(proposed)
        identity(proposed)
        from .deploy import CODE_KEY
        key = note.get("code_key")
        if not isinstance(key, str) or not CODE_KEY.fullmatch(key):
            raise ValueError("The pairing bundle has no code-signing key; enroll again on the updated main.")
        save_json(state / "cluster-node.json", {"node_id": note["node_id"],
                  "main_device": note["main_device"], "token": token, "code_key": key})
        save_json(path, proposed)
        return {"joined": note["node_id"], "main_device": note["main_device"],
                "main_url": endpoint(proposed), "next": "jarvis cluster node"}
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
        for node in nodes:  # one failing node must not stop the rest
            try:
                reports.append(update(config, state, node, rollback=args.rollback))
            except (OSError, ValueError, RuntimeError) as exc:
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
        return getattr(cluster_tunnel, args.value)(state)
    if action == "leave":
        if local == main:
            raise ValueError("This PC is the main; nodes leave, the main revokes them.")
        from .cluster_setup import leave
        return leave(config, path, state)
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
        devices = dict(config.get("devices", {}))
        if node not in devices:
            if not args.hostname:
                raise ValueError("A new node needs --hostname HOSTNAME.")
            devices[node] = {"hostname": args.hostname, "label": node}
            config = {**config, "devices": devices}
            save_json(path, config)
        caps = args.capability or sorted(NODE_DEFAULT)
        token = Broker(state).enroll(node, caps)
        safe = {name: {k: device[k] for k in ("hostname", "label", "aliases") if k in device}
                for name, device in devices.items()}
        save_json(output, {"node_id": node, "main_device": main, "main_url": endpoint(config),
                           "token": token, "capabilities": caps, "devices": safe,
                           "code_key": code_key(state)})
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
        return result
    if action == "serve" and local != main:
        raise ValueError("This PC is a node. Run jarvis cluster node.")
    if action == "node" or (action == "run" and local != main):
        worker = NodeWorker(config, state)
        try:
            worker.run()
        except KeyboardInterrupt:
            worker.close()
        return None
    from .ui import serve
    serve(config, base, state, port=args.port)
    return None


def autostart(config_path, state, turn_on):
    """Run this PC's role (main or node) at login: Windows, macOS or Linux."""
    from .autostart import install
    argv = [sys.executable, "-m", "jarvis", "--config", str(config_path), "--state", str(state), "cluster", "run"]
    return install("jarvis-cluster", "JARVIS Cluster", argv, turn_on)


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
