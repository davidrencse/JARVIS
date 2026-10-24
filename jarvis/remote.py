"""Desktop actions on either laptop: open an app, open a link, list apps.

The brain's open_app / open_url / list_apps tools take an optional `device`. Here runs
DesktopTools directly. The other laptop is reached the same way Grokbot prompts are:
- Dell -> Zenbook: SSH with the configured key, running `python -m jarvis desktop ...`
  in the Zenbook's checkout. That command finds the Hyprland session itself, so apps
  open on David's screen, not in the SSH session.
- Zenbook -> Dell: Windows OpenSSH sessions can't reach the desktop, so the Dell's JARVIS
  keeps `grokbot tunnel` running; its loopback relay takes /desktop calls with the token.
Only the three actions below travel; arguments go as base64 or JSON data, never as code.
"""

import base64
import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ACTIONS = ("open_app", "open_url", "list_apps")
REMOTE_DIR = "~/projects/JARVIS"  # where the other laptop's checkout lives, by default
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def session_env():
    """Find the Wayland/Hyprland session from an SSH shell, so launched apps show up."""
    env = dict(os.environ)
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = run
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        hypr = Path(run, "hypr")
        found = sorted(hypr.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) \
            if hypr.is_dir() else []
        if found:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = found[0].name
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(p.name for p in Path(run).glob("wayland-?"))
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0]
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and Path(run, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run}/bus"
    return env


def run_here(action, arg, tools=None):
    """(ok, text) for one action on this laptop."""
    if action not in ACTIONS:
        return False, f"Unknown desktop action {action}."
    if tools is None:
        from .actions import DesktopTools
        tools = DesktopTools()
    if os.name != "nt" and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.update(session_env())  # started over SSH: borrow the desktop session
    key = "query" if action == "list_apps" else "name" if action == "open_app" else "url"
    return tools.call(action, {key: arg})


class Remote:
    def __init__(self, config, state_dir, tools=None):
        self.config, self.state, self.tools = config, Path(state_dir), tools
        self.machines = config.get("devices", {})

    def here(self):
        from .grokbot import Grokbot
        return Grokbot(self.config, self.state).here()

    def names(self):
        return sorted(self.machines)

    def call(self, device, action, arg):
        """(ok, text): `action` on `device` (None or this laptop: run here)."""
        if action not in ACTIONS:
            return False, f"Unknown desktop action {action}."
        here = self.here()
        if not device or device in ("here", "local", here):
            return run_here(action, arg, self.tools)
        if device not in self.machines:
            return False, f"Unknown laptop {device!r}; known: {', '.join(self.names())}."
        try:
            machine = self.machines[device]
            if machine.get("transport") == "ssh" and machine.get("target"):
                return self._ssh(device, machine, action, arg)
            return self._tunnel(device, action, arg)
        except (OSError, subprocess.SubprocessError, ValueError, LookupError) as exc:
            return False, f"Couldn't reach {device}: {exc}"

    def _ssh(self, device, machine, action, arg):
        binary = shutil.which("ssh")
        if not binary:
            raise LookupError("OpenSSH client is not installed")
        folder = machine.get("jarvis_dir") or REMOTE_DIR
        if not all(c.isalnum() or c in "~/._- " for c in folder):
            raise ValueError("odd jarvis_dir in config")
        data = base64.b64encode(arg.encode("utf-8")).decode()
        python = f"$( [ -x {folder}/.venv/bin/python ] && echo {folder}/.venv/bin/python || echo python3 )"
        remote = (f"cd {folder} && {python} -m jarvis desktop {shlex.quote(action)} "
                  f"--b64 {shlex.quote(data)}")
        args = [binary, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=6"]
        if machine.get("identity_file"):
            args += ["-i", str(Path(machine["identity_file"]).expanduser()), "-o", "IdentitiesOnly=yes"]
        last = ""
        for target in [machine["target"], *machine.get("fallback_targets", [])]:
            out = subprocess.run([*args, target, remote], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=45, creationflags=FLAGS)
            if out.returncode == 255:  # ssh itself failed: try the next address
                last = out.stderr.strip()[-200:]
                continue
            try:
                body = json.loads(out.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return False, (out.stderr or out.stdout).strip()[-300:] or f"{device} said nothing."
            return bool(body.get("ok")), str(body.get("text", ""))
        raise ConnectionError(last or "no address answered")

    def _tunnel(self, device, action, arg):
        from .grokbot import TUNNEL_DIR, _json, _loads
        note = _json(TUNNEL_DIR / f"grokbot-{device}.json")
        if not note or not note.get("port") or not note.get("token"):
            raise LookupError(f"no tunnel from {device}. Its JARVIS app starts one; or run "
                              f"python -m jarvis grokbot tunnel there")
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(note['port'])}/desktop",
            data=json.dumps({"action": action, "arg": arg}).encode(),
            headers={"Content-Type": "application/json", "X-JARVIS-Token": note["token"]})
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                body = _loads(res.read())
        except urllib.error.HTTPError as exc:
            body = _loads(exc.read())
            return False, body.get("error") or f"{device} refused ({exc.code})."
        except (urllib.error.URLError, OSError):
            raise ConnectionError(f"the tunnel from {device} is down")
        return bool(body.get("ok")), str(body.get("text", ""))
