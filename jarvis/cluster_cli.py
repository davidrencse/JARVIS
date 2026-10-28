"""Explicit setup, pairing, revocation and startup for the main/node topology."""

import json
import re
import sys
from pathlib import Path

from .cluster import Client, NodeWorker, save_json
from .cluster_store import CAPABILITIES, Broker
from .topology import DEVICE_ID, enabled, endpoint, identity


def parser(sub):
    command = sub.add_parser("cluster", help="Configure the main PC and its lightweight nodes")
    command.add_argument("action", choices=("configure", "enroll", "join", "revoke", "status",
                                           "run", "serve", "node", "request", "autostart", "tunnel", "update", "main", "roles"))
    command.add_argument("value", nargs="?", help="node ID, pairing file, request ID, or on/off")
    command.add_argument("--main", dest="main_device", help="device ID of the main PC")
    command.add_argument("--local", dest="local_device", help="this PC's device ID, or auto")
    command.add_argument("--url", help="main HTTPS origin, or loopback HTTP through SSH")
    command.add_argument("--hostname", help="enroll: hostname of a new node")
    command.add_argument("--capability", action="append", choices=sorted(CAPABILITIES))
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
                        main_url=args.url or settings.get("main_url", "http://127.0.0.1:7778"))
        proposed = {**config, "cluster": settings}
        # auto can be used in a shared checkout, but explicit IDs are recommended for nodes.
        local, main = identity(proposed)
        endpoint(proposed)
        save_json(path, proposed)
        return {"configured": True, "local_device": local, "main_device": main,
                "role": "main" if local == main else "node", "main_url": endpoint(proposed)}
    if action == "join":
        if not args.value:
            raise ValueError("Usage: jarvis cluster join PAIRING_FILE")
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
        save_json(state / "cluster-node.json", {"node_id": note["node_id"],
                  "main_device": note["main_device"], "token": token})
        save_json(path, proposed)
        return {"joined": note["node_id"], "main_device": note["main_device"],
                "main_url": endpoint(proposed), "next": "jarvis cluster node"}
    if not enabled(config):
        raise ValueError("Configure the main PC first: jarvis cluster configure --main DEVICE --local DEVICE")
    local, main = identity(config)
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
    if action in ("enroll", "revoke"):
        if local != main:
            raise ValueError("Enroll or revoke nodes on the main PC.")
        node = args.value or ""
        if not DEVICE_ID.fullmatch(node) or node == main:
            raise ValueError("Name a valid node other than the main PC.")
        if action == "revoke":
            Broker(state).revoke(node)
            return {"revoked": node}
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
        caps = args.capability or sorted(CAPABILITIES)
        token = Broker(state).enroll(node, caps)
        safe = {name: {k: device[k] for k in ("hostname", "label", "aliases") if k in device}
                for name, device in devices.items()}
        save_json(output, {"node_id": node, "main_device": main, "main_url": endpoint(config),
                           "token": token, "capabilities": caps, "devices": safe})
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
        result = {"role": "main" if local == main else "node", "local_device": local,
                  "code": code_fingerprint(),
                  "main_device": main, "main_url": endpoint(config)}
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
