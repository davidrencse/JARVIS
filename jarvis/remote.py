"""Desktop actions on either laptop: open an app, open a link, list apps.

The brain's open_app / open_url / list_apps tools take an optional `device`. Here runs
DesktopTools directly. The other laptop is reached the same way Grokbot prompts are:
- Dell -> Zenbook: SSH with the configured key, running `python -m jarvis desktop ...`
  in the Zenbook's checkout. That command finds the Hyprland session itself, so apps
  open on David's screen, not in the SSH session.
- Zenbook -> Dell: Windows OpenSSH sessions can't reach the desktop, so the Dell's JARVIS
  keeps `grokbot tunnel` running; its loopback relay takes /desktop calls with the token.
Only the actions below travel (open_page: a console page, e.g. the knowledge page, as a window;
close_app, media and volume: close an app's windows, play/pause/skip, speaker volume;
screen: a downscaled snapshot of the screen, for the Devices view; lan_mouse: start, stop
or pair Lan Mouse there);
arguments go as base64 or JSON data, never as code.
A laptop can be named by its key, hostname, configured `aliases` ("arch", "asus"...) or
"the other laptop".
"""

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# ssh stderr that means no session ever opened, so the command can't have run there.
PRE_SESSION = re.compile(r"ssh: connect to host|Could not resolve hostname|kex_exchange_identification|"
                         r"banner exchange|Host key verification failed|Permission denied \(", re.I)

ACTIONS = ("open_app", "open_url", "list_apps", "open_page", "focus_window", "close_app", "media",
           "volume", "scroll", "browse", "observe", "input_info", "screen", "lan_mouse")
# The argument each action's tool takes.
ARG = {"open_app": "name", "list_apps": "query", "open_url": "url", "open_page": "page",
       "focus_window": "name", "close_app": "name", "media": "command", "volume": "level",
       "scroll": "spec", "browse": "spec", "observe": "predicate", "input_info": "query",
       "screen": "spec", "lan_mouse": "command"}
# Desktop variables an SSH shell has wrong or lacks; the desktop's own values win.
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "XAUTHORITY",
                "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP",
                "DBUS_SESSION_BUS_ADDRESS")
REMOTE_DIR = "~/projects/JARVIS"  # where the other laptop's checkout lives, by default
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def systemd_env(env=None):
    """The systemd user manager's environment, which the desktop session exports into
    (Hyprland's DISPLAY, WAYLAND_DISPLAY, XDG_CURRENT_DESKTOP...). {} if unavailable."""
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode:
        return {}
    pairs = (line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    return {k: v for k, v in pairs if not v.startswith("$'")}  # skip shell-quoted values


def session_env():
    """Find the Wayland/Hyprland session from an SSH shell, so launched apps show up.
    Without the session's DISPLAY, X11 apps such as Chrome exit at once over SSH."""
    env = dict(os.environ)
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = run
    for key, value in systemd_env({**env, "XDG_RUNTIME_DIR": run}).items():
        if key in SESSION_KEYS or key not in env:
            env[key] = value
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        hypr = Path(run, "hypr")
        found = sorted(hypr.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)             if hypr.is_dir() else []
        if found:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = found[0].name
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(p.name for p in Path(run).glob("wayland-?"))
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0]
    if not env.get("DISPLAY"):  # XWayland's socket is owned by the session's user
        mine = sorted(p.name[1:] for p in Path("/tmp/.X11-unix").glob("X*")
                      if p.name[1:].isdigit() and p.stat().st_uid == os.getuid())
        if mine:
            env["DISPLAY"] = f":{mine[0]}"
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and Path(run, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run}/bus"
    return env


def run_here(action, arg, tools=None):
    """(ok, text) for one action on this laptop."""
    if action not in ACTIONS:
        return False, f"Unknown desktop action {action}."
    if os.name != "nt" and not (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY")):
        os.environ.update(session_env())  # started over SSH: borrow the desktop session
    if action == "open_page":
        from .showcase import open_page
        return open_page(arg)
    if action == "observe":  # read-only: did the last action's window appear?
        from .observe import run
        return run(arg)
    if action == "screen":  # read-only: a snapshot for the Devices view's screen viewer
        from .screen import run
        return run(arg)
    if action == "input_info":  # read-only: screens and Lan Mouse, for Settings
        from .sharedinput import run
        return run(arg)
    if action == "lan_mouse":  # start / stop / status / pair this computer's Lan Mouse
        from .sharedinput import control
        return control(arg)
    if tools is None:
        from .actions import DesktopTools
        tools = DesktopTools()
    return tools.call(action, {ARG[action]: arg})


class Remote:
    def __init__(self, config, state_dir, tools=None):
        self.config, self.state, self.tools = config, Path(state_dir), tools
        self.machines = config.get("devices", {})

    def here(self):
        from .topology import enabled, identity
        if enabled(self.config):
            return identity(self.config)[0]
        from .grokbot import Grokbot
        return Grokbot(self.config, self.state).here()

    def names(self):
        return sorted(self.machines)

    def describe(self):
        """The laptops for a tool description: "dell (Windows 11...), zenbook (...)"."""
        parts = []
        for key in self.names():
            machine = self.machines[key]
            extra = [machine["label"]] if machine.get("label") else []
            if machine.get("aliases"):
                extra.append("he may call it " + ", ".join(machine["aliases"]))
            parts.append(f"{key} ({'; '.join(extra)})" if extra else key)
        return ", ".join(parts)

    def resolve(self, name):
        """The configured laptop a spoken name means: its key, hostname or an alias
        ("my arch linux laptop" -> zenbook), or "other" for the one this isn't.
        None means this laptop; an unknown name comes back as it was."""
        word = re.sub(r"^(?:the|my)\s+|\s+(?:laptop|pc|computer|machine)$", "",
                      re.sub(r"\s+", " ", str(name or "").strip().lower()))
        from .topology import enabled, identity
        if enabled(self.config) and word in ("main", "main pc", "controller"):
            return identity(self.config)[1]
        if word in ("", "here", "local", "this", "this one"):
            return None
        for key, machine in self.machines.items():
            names = {key.lower(), str(machine.get("hostname", "")).lower(),
                     *(str(a).lower() for a in machine.get("aliases", []))}
            if word in names:
                return key
        if word in ("other", "other one") and len(self.machines) == 2:
            return next((k for k in self.machines if k != self.here()), name)
        return name

    def call(self, device, action, arg):
        """(ok, text): `action` on `device` (None or this laptop: run here)."""
        if action not in ACTIONS:
            return False, f"Unknown desktop action {action}."
        device = self.resolve(device)
        from .topology import ORIGIN, enabled, identity
        if enabled(self.config):
            from .cluster import Client, NodeOffline, dispatch
            local, main = identity(self.config)
            target = device or ORIGIN.get() or local
            if target not in self.machines:
                return False, f"Unknown node {target!r}; known: {', '.join(self.names())}."
            if target == local:
                # This machine acts itself. On a node this matters: the main's SSH fallback
                # runs `jarvis desktop ...` here, and routing that back through the main
                # would send it to this (stopped) node agent again.
                return run_here(action, arg, self.tools)
            try:
                payload = {"action": action, "arg": arg}
                if local != main:
                    result = Client(self.config, self.state).request("/v1/control", {
                        "node": target, "kind": "desktop", "payload": payload}, timeout=50)
                elif target == main:
                    return run_here(action, arg, self.tools)
                else:
                    result = dispatch(self.config, self.state, target, "desktop", payload)
                return bool(result.get("ok")), str(result.get("text", ""))
            except NodeOffline as exc:
                # Only a pre-dispatch offline verdict permits another transport.
                # Timeouts and lost acknowledgements may already have had effects.
                machine = self.machines[target]
                if local == main and machine.get("transport") == "ssh" and machine.get("target"):
                    try:
                        return self._ssh(target, machine, action, arg)
                    except (OSError, subprocess.SubprocessError, ValueError, LookupError) as fallback:
                        return False, f"Couldn't reach {target}: {fallback}"
                return False, str(exc)
            except (OSError, ValueError, RuntimeError) as exc:
                return False, str(exc)
        if not device or device == self.here():
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
        from .cluster_tunnel import pinned_host
        from .transfer import _check_target
        args += pinned_host(machine, self.state)
        last = ""
        for target in [machine["target"], *machine.get("fallback_targets", [])]:
            _check_target(target)
            out = subprocess.run([*args, target, remote], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=45, creationflags=FLAGS)
            if out.returncode == 255:
                last = out.stderr.strip()[-200:]
                if PRE_SESSION.search(out.stderr):  # never got a session: try the next address
                    continue
                # ssh also exits 255 when the link drops mid-command; resending on another
                # address could repeat it (close_app, media next).
                return False, f"Lost the SSH link to {device} mid-command ({last}); it may have run."
            try:
                body = json.loads(out.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                said = (out.stderr or out.stdout).strip()[-300:] or f"{device} said nothing"
                return False, f"{said} (no result from {device}; it may have run)"
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
            # urlopen wraps connect/send errors in URLError; reading the reply raises plain
            # OSError, and by then the node has the command.
            with urllib.request.urlopen(req, timeout=45) as res:
                body = _loads(res.read())
        except urllib.error.HTTPError as exc:
            try:
                body = _loads(exc.read())
                return False, body.get("error") or f"{device} refused ({exc.code})."
            finally:
                exc.close()
        except urllib.error.URLError:
            raise ConnectionError(f"the tunnel from {device} is down")
        except OSError as exc:
            return False, f"{device} got the command but no reply came back ({exc}); it may have run."
        return bool(body.get("ok")), str(body.get("text", ""))
