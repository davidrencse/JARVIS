"""Fixed, read-only remote inventory over an existing trusted SSH connection."""

import json
import re
import shutil
import subprocess
from pathlib import Path


def ssh_inventory(target, identity_file=None):
    if not isinstance(target, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", target):
        raise ValueError("Use user@hostname or user@IPv4 for the configured SSH target.")
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    identity_args = []
    if identity_file:
        identity = Path(identity_file).expanduser().resolve()
        if not identity.is_file():
            raise ValueError("Configured SSH identity file does not exist.")
        identity_args = ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    # Send a fixed, reviewable collector through stdin. No credentials or remote files copied.
    source = Path(__file__).with_name("inventory.py").read_text(encoding="utf-8")
    result = subprocess.run(
        [binary, "-T", *identity_args, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=8", target, "python3 -"],
        input=source, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=150, shell=False)
    if result.returncode:
        raise RuntimeError("SSH inventory failed. Verify the host key and key-based SSH login "
                           "in your terminal first. " + result.stderr[-1500:])
    data = json.loads(result.stdout)
    data["source"] = "live_ssh_probe"
    return data
