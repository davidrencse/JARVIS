"""Fixed, read-only remote inventory over an existing trusted SSH connection."""

import json
import shutil
import socket
import subprocess
from pathlib import Path

from .transfer import _ssh_args, ssh_host


def devices_here(devices, hostname=None):
    """The config is written from the Dell's side: Dell local, Zenbook over SSH. Seen from
    the machine whose hostname matches a device, that device is local instead, and a
    "local" device for another host has no route from here, so it is left out."""
    hostname = (hostname or socket.gethostname()).lower()
    if not any(d.get("hostname", "").lower() == hostname for d in devices.values()):
        return devices
    here = {}
    for name, device in devices.items():
        if device.get("hostname", "").lower() == hostname:
            here[name] = {k: v for k, v in device.items() if k not in ("target", "identity_file",
                                                                       "host_fingerprint")}
            here[name]["transport"] = "local"
        elif device.get("transport") != "local":
            here[name] = device
    return here


def ssh_inventory(target, identity_file=None):
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    identity = None
    if identity_file:
        path = Path(identity_file).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Configured SSH identity file does not exist.")
        identity = str(path)
    # Send a fixed, reviewable collector through stdin. No credentials or remote files copied.
    source = Path(__file__).with_name("inventory.py").read_text(encoding="utf-8")
    result = subprocess.run(
        [*_ssh_args(binary, identity), "-T", *ssh_host(target), "python3 -"],
        input=source, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=150, shell=False)
    if result.returncode:
        raise RuntimeError("SSH inventory failed. Verify the host key and key-based SSH login "
                           "in your terminal first. " + result.stderr[-1500:])
    data = json.loads(result.stdout)
    data["source"] = "live_ssh_probe"
    return data
