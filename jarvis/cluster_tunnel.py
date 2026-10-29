"""One supervised, host-key-pinned SSH link between the main and a node, in either
direction (cluster.tunnel: reverse = main -> node, forward = node -> main).

Stop uses a cooperative marker, not a stale PID that could kill an unrelated process.
Windows launches through Explorer so the worker outlives the invoking CLI job.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .cluster import save_json
from .locking import exclusive_lock
from .topology import identity
from .transfer import _check_target, _ssh_args, ssh_device

NAME = "cluster-tunnel"
# While on a fallback target (e.g. Tailscale), how often to look for an earlier one (the
# LAN) again. Doubles after each switch back that does not hold, up to LAN_RETRY_MAX.
LAN_RETRY, LAN_RETRY_MAX = 60, 30 * 60


def reachable(target, timeout=2):
    """Cheap check that the target's sshd answers; the SSH attempt itself proves the rest."""
    try:
        socket.create_connection((target.split("@", 1)[1], 22), timeout=timeout).close()
        return True
    except OSError:
        return False


def pinned_host(machine, state):
    alias = machine.get("host_key_alias")
    key = machine.get("host_key")
    if key:
        alias = "jarvis-" + machine.get("hostname", "peer")
    if alias and not re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
        raise ValueError("Invalid SSH host key alias.")
    if not key:
        return ["-o", f"HostKeyAlias={alias}"] if alias else []
    if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+) [A-Za-z0-9+/=]+", key.strip()):
        raise ValueError("Invalid pinned SSH host key.")
    path = Path(state).resolve() / f"known-host-{alias}"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"{alias} {key.strip()}\n"
    if not path.exists() or path.read_text(encoding="ascii") != text:
        path.write_text(text, encoding="ascii")
    return ["-o", f"HostKeyAlias={alias}", "-o", f"UserKnownHostsFile={path}"]


def mode(config):
    """reverse (default): the main runs `ssh -R` into each node, so nodes need an SSH
    server. forward: each node runs `ssh -L` to the main, so only the main needs one
    (e.g. a Linux or macOS main with a Windows node). Either way the node reaches the
    main's hub on its own 127.0.0.1:<listen_port>."""
    value = (config.get("cluster") or {}).get("tunnel", "reverse")
    if value not in ("reverse", "forward"):
        raise ValueError('cluster.tunnel must be "reverse" or "forward".')
    return value


def link(config, state):
    """The SSH link `jarvis cluster join` saved for this node (forward mode), as a device
    entry; None if the node was paired the old way and its link lives in the config."""
    try:
        note = json.loads((Path(state) / "cluster-node.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    saved = note.get("link") if isinstance(note, dict) else None
    if not isinstance(saved, dict) or note.get("main_device") != identity(config)[1]:
        return None
    targets = [t for t in saved.get("targets", []) if isinstance(t, str)]
    if not targets:
        return None
    return {"transport": "ssh", "target": targets[0], "fallback_targets": targets[1:],
            "host_key": saved.get("host_key"), "hostname": saved.get("hostname") or "main",
            "identity_file": saved.get("identity_file")}


def peer(config, state=None):
    """The machine this end opens SSH to: a node (reverse, run on the main) or the main
    (forward, run on a node; the joined link in `state` wins over the config)."""
    local, main = identity(config)
    if mode(config) == "forward":
        if local == main:
            raise ValueError("With cluster.tunnel = forward, each node runs the tunnel to the main.")
        joined = link(config, state) if state is not None else None
        if joined:
            return joined
        machine = (config.get("devices") or {}).get(main) or {}
        if machine.get("transport") != "ssh" or not machine.get("target"):
            raise ValueError(f"Give devices.{main} an SSH target (transport ssh) so this node can reach it.")
        return machine
    if local != main:
        raise ValueError("With cluster.tunnel = reverse, the main runs the tunnel to its nodes.")
    return ssh_device(config, config.get("cluster", {}).get("tunnel_device"))


def command(config, state, target):
    machine = peer(config, state)
    _check_target(target)
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    port = int(config.get("cluster", {}).get("listen_port", 7778))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid cluster listen port.")
    return [*_ssh_args(binary, machine.get("identity_file")), *pinned_host(machine, state),
            "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-L" if mode(config) == "forward" else "-R",
            f"127.0.0.1:{port}:127.0.0.1:{port}", target]


def status(state):
    from .daemon import _alive
    state = Path(state)
    try:
        note = json.loads((state / f"{NAME}.json").read_text(encoding="utf-8"))
        running = bool(_alive(int(note["pid"])) and time.time() - note["updated"] < 15)
    except (OSError, ValueError, KeyError, TypeError):
        note, running = {}, False
    return {**note, "running": running, "ssh_alive": running and note.get("ssh_alive", False),
            "autostart": startup_path().is_file()}


def run(config, state):
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    machine = peer(config, state)
    targets = list(dict.fromkeys(t for t in [machine.get("target"), *machine.get("fallback_targets", [])] if t))
    if not targets:
        raise ValueError("No cluster tunnel targets configured.")
    # Validate before claiming the worker lock.
    for target in targets:
        command(config, state, target)
    marker = state / f"{NAME}.stop"
    note = {"pid": os.getpid(), "ssh_alive": False}

    def publish(**values):
        note.update(values, updated=time.time())
        # Windows refuses to replace a file another process is reading (WinError 5), and
        # `cluster tunnel status` or the console reads this one. A status note must never
        # kill the tunnel: retry briefly, then skip; the next publish (<= 1 s) refreshes it.
        for _ in range(5):
            try:
                save_json(state / f"{NAME}.json", note)
                return
            except PermissionError:
                time.sleep(0.05)

    def wait(seconds):
        until = time.monotonic() + seconds
        while not marker.exists() and time.monotonic() < until:
            publish()
            time.sleep(min(0.25, max(0, until - time.monotonic())))

    with exclusive_lock(state / f"{NAME}.lock"):
        marker.unlink(missing_ok=True)
        delay, proc, retry = 1, None, LAN_RETRY
        logs = state / "logs"
        logs.mkdir(exist_ok=True)
        try:
            while not marker.exists():
                for index, target in enumerate(targets):
                    if marker.exists():
                        break
                    started = probed = time.monotonic()
                    preferred = None
                    publish(target=target, ssh_alive=False, error=None)
                    try:
                        with (logs / f"{NAME}.log").open("a", encoding="utf-8") as log:
                            proc = subprocess.Popen(command(config, state, target), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=log,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                            while proc.poll() is None and not marker.exists():
                                # SSH -N has no application handshake. This reports a
                                # living SSH session, not node health; cluster status owns that.
                                publish(ssh_alive=time.monotonic() - started >= 9)
                                wait(1)
                                if index and time.monotonic() - probed >= retry:
                                    probed = time.monotonic()
                                    preferred = next((t for t in targets[:index] if reachable(t)), None)
                                    if preferred:
                                        proc.terminate()
                                        break
                            if marker.exists() and proc.poll() is None:
                                proc.terminate()
                            proc.wait(timeout=5)
                            publish(ssh_alive=False, error=f"{preferred} is reachable again; switching back"
                                    if preferred else f"SSH exited ({proc.returncode})")
                    except (OSError, subprocess.SubprocessError) as exc:
                        publish(ssh_alive=False, error=str(exc))
                    finally:
                        if proc is not None and proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        proc = None
                    if time.monotonic() - started > 60:
                        delay = 1
                        if not index:
                            retry = LAN_RETRY
                    if preferred:
                        # Restart from the first target. If the switch back does not hold,
                        # we land on the fallback again and wait longer before the next try.
                        retry = min(retry * 2, LAN_RETRY_MAX)
                        break
                wait(delay)
                delay = min(delay * 2, 60)
        finally:
            publish(ssh_alive=False, pid=0)


TITLE = "JARVIS Cluster Tunnel"


def startup_path():
    from .autostart import path
    return path("jarvis-cluster-tunnel", TITLE)


def launcher(config_path, state):
    python = Path(sys.executable)
    if os.name == "nt" and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    argv = [str(python), "-m", "jarvis", "--config", str(Path(config_path).resolve()),
            "--state", str(Path(state).resolve()), "cluster", "tunnel", "run"]
    if any("\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Newlines are not allowed in startup paths.")
    return argv


def autostart(config_path, state, enable):
    from .autostart import install
    return install("jarvis-cluster-tunnel", TITLE, launcher(config_path, state), enable)


def start(config_path, state):
    if status(state)["running"]:
        return {"started": False, **status(state)}
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    argv = launcher(config_path, state)
    if os.name == "nt":
        script = state / f"{NAME}-launch.vbs"
        from .autostart import vbs
        line = subprocess.list2cmdline(argv).replace('"', '""')
        script.write_text(vbs(line), encoding="utf-8")
        subprocess.Popen(["explorer.exe", str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        if status(state)["running"]:
            return {"started": True, **status(state)}
    return {"started": False, "note": "Worker did not start; inspect configuration and the tunnel log.", **status(state)}


def stop(state):
    if not status(state)["running"]:
        return {"stopped": False, "note": "Not running."}
    (Path(state) / f"{NAME}.stop").touch()
    for _ in range(40):
        time.sleep(0.25)
        if not status(state)["running"]:
            return {"stopped": True}
    return {"stopped": False, "note": "Stop requested; SSH is still shutting down."}
