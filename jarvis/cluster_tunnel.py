"""One supervised, host-key-pinned reverse SSH link from the main to a node.

Stop uses a cooperative marker, not a stale PID that could kill an unrelated process.
Windows launches through Explorer so the worker outlives the invoking CLI job.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .cluster import save_json
from .locking import exclusive_lock
from .topology import identity
from .transfer import _check_target, _ssh_args, ssh_device

NAME = "cluster-tunnel"


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


def command(config, state, target):
    if identity(config)[0] != identity(config)[1]:
        raise ValueError("The cluster reverse tunnel runs on the main PC.")
    machine = ssh_device(config, config.get("cluster", {}).get("tunnel_device"))
    _check_target(target)
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    port = int(config.get("cluster", {}).get("listen_port", 7778))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid cluster listen port.")
    return [*_ssh_args(binary, machine.get("identity_file")), *pinned_host(machine, state),
            "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-R", f"127.0.0.1:{port}:127.0.0.1:{port}", target]


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
    machine = ssh_device(config, config.get("cluster", {}).get("tunnel_device"))
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
        delay, proc = 1, None
        logs = state / "logs"
        logs.mkdir(exist_ok=True)
        try:
            while not marker.exists():
                for target in targets:
                    if marker.exists():
                        break
                    started = time.monotonic()
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
                            if marker.exists() and proc.poll() is None:
                                proc.terminate()
                            proc.wait(timeout=5)
                            publish(ssh_alive=False, error=f"SSH exited ({proc.returncode})")
                    except (OSError, subprocess.SubprocessError) as exc:
                        publish(ssh_alive=False, error=str(exc))
                    finally:
                        if proc is not None and proc.poll() is None:
                            proc.kill()
                            proc.wait()
                        proc = None
                    if time.monotonic() - started > 60:
                        delay = 1
                wait(delay)
                delay = min(delay * 2, 60)
        finally:
            publish(ssh_alive=False, pid=0)


def startup_path():
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/JARVIS Cluster Tunnel.vbs"
    return Path.home() / ".config/systemd/user/jarvis-cluster-tunnel.service"


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
    path = startup_path()
    if enable:
        argv = launcher(config_path, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            line = subprocess.list2cmdline(argv).replace('"', '""')
            path.write_text(f'CreateObject("WScript.Shell").Run "{line}", 0, False\n', encoding="utf-8")
        else:
            line = " ".join(json.dumps(a).replace("%", "%%") for a in argv)
            path.write_text("[Unit]\nDescription=JARVIS reverse cluster tunnel\nAfter=network-online.target\n"
                            f"[Service]\nExecStart={line}\nRestart=on-failure\nRestartSec=5\n"
                            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
    elif os.name == "nt":
        path.unlink(missing_ok=True)
    if os.name != "nt":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        subprocess.run(["systemctl", "--user", "enable" if enable else "disable", "--now", path.name], check=True, timeout=15)
        if not enable:
            path.unlink(missing_ok=True)
    return {"autostart": enable, "path": str(path)}


def start(config_path, state):
    if status(state)["running"]:
        return {"started": False, **status(state)}
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    argv = launcher(config_path, state)
    if os.name == "nt":
        script = state / f"{NAME}-launch.vbs"
        line = subprocess.list2cmdline(argv).replace('"', '""')
        script.write_text(f'CreateObject("WScript.Shell").Run "{line}", 0, False\n', encoding="utf-8")
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
