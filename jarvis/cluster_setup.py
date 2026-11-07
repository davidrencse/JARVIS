"""Set up the main and its nodes in two commands, and say what is wrong when it breaks.

    main:  jarvis cluster init                 # this PC is the main (forward tunnel)
           jarvis cluster invite dell          # prints one secret invite code
    node:  jarvis cluster join CODE            # saves the link, connects, starts at login
    both:  jarvis cluster doctor               # every check, with the fix for each failure

The invite carries everything the node needs and nothing it must type: the node's hub
credential, the main's pinned SSH host key, the addresses the main answers on (LAN first,
then Tailscale), and a fresh SSH key made for that node only. On the main that key is
added to authorized_keys with options that allow nothing but a local forward to the hub's
loopback port: no shell, no other ports, no remote forwards. The node keeps the link in
its state folder (`cluster-node.json`, `cluster-node.key`), so `jarvis.config.json` stays
the same file on every machine and never holds a secret.

`jarvis cluster revoke NODE` (main) removes both the credential and the SSH key;
`jarvis cluster leave` (node) stops and forgets the link on the node.
"""

import base64
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path

from .fsio import atomic_write
from .platforms import NO_WINDOW, on_windows
from .cluster import Client, save_json
from .cluster_store import NODE_DEFAULT, Broker
from .deploy import CODE_KEY, code_key
from .topology import DEFAULT_PORT, DEVICE_ID, enabled, identity

PREFIX = "jarvis-invite-1:"
INVITE_TTL = 24 * 3600  # an invite nobody joins with stops working after a day
MARK = "jarvis-node:"  # SSH key comment that marks the key JARVIS made for one node
HOST_KEY = re.compile(r"(?:ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+")
ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
FLAGS = NO_WINDOW


def _os():
    from .platforms import detect
    return detect()["os"]


def _hostname():
    return socket.gethostname()


def _this_device(config):
    host = _hostname().casefold()
    found = [k for k, d in (config.get("devices") or {}).items()
             if str((d or {}).get("hostname", "")).casefold() == host]
    return found[0] if len(found) == 1 else None


def _slug(text):
    return re.sub(r"[^a-z0-9_-]+", "-", text.casefold()).strip("-_")[:64] or "main"


def _sshd_up(host="127.0.0.1", port=22, timeout=2):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


SSHD_FIX = {
    "linux": "Install and start OpenSSH server: sudo systemctl enable --now sshd (Debian/Ubuntu: ssh)",
    "macos": "System Settings > General > Sharing > Remote Login: on",
    "windows": "Settings > System > Optional features > add OpenSSH Server, then in an admin "
               "PowerShell: Start-Service sshd; Set-Service sshd -StartupType Automatic",
}


# --- main side --------------------------------------------------------------------------

def init(config, path, state, device=None, autostart=True):
    """Make this PC the main: register it under its hostname, use the forward tunnel
    (only the main needs an SSH server), and run the hub at login."""
    host = _hostname()
    devices = dict(config.get("devices") or {})
    device = device or _this_device(config) or _slug(host)
    if not DEVICE_ID.fullmatch(device):
        raise ValueError("Device IDs are letters, digits, - and _ (e.g. zenbook).")
    entry = dict(devices.get(device) or {})
    known = str(entry.get("hostname", ""))
    if known and known.casefold() != host.casefold():
        raise ValueError(f"devices.{device}.hostname is {known}, but this PC is {host}. "
                         f"Run init with --id set to this PC's device, or fix that hostname.")
    from .platforms import detect
    entry.setdefault("hostname", host)
    entry.setdefault("label", detect()["label"])
    devices[device] = entry
    settings = dict(config.get("cluster") or {})
    port = int(settings.get("listen_port") or DEFAULT_PORT)
    settings.update(enabled=True, main_device=device, local_device="auto",
                    tunnel=settings.get("tunnel", "forward"), listen_host="127.0.0.1",
                    listen_port=port, main_url=f"http://127.0.0.1:{port}")
    proposed = {**config, "devices": devices, "cluster": settings}
    identity(proposed)  # fails before saving if the hostname is ambiguous
    save_json(path, proposed)
    report = {"main_device": device, "hostname": host, "hub": f"127.0.0.1:{port}",
              "tunnel": settings["tunnel"]}
    if settings["tunnel"] == "forward":
        report["ssh_server"] = _sshd_up()
        if not report["ssh_server"]:
            report["fix"] = SSHD_FIX[_os()]
    if autostart:
        from .cluster_cli import autostart as role_autostart
        report["autostart"] = role_autostart(path, state, True)
    report["next"] = ("Restart the main if it was already running, then on this PC: "
                      "jarvis cluster invite NODE_ID   (one per other computer)")
    return report


def host_key():
    """This PC's SSH server key, `ssh-ed25519 AAAA...`: what nodes pin for the main."""
    folder = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ssh"
              if _os() == "windows" else Path("/etc/ssh"))
    for name in ("ssh_host_ed25519_key.pub", "ssh_host_ecdsa_key.pub", "ssh_host_rsa_key.pub"):
        try:
            found = HOST_KEY.match((folder / name).read_text(encoding="ascii").strip())
        except OSError:
            continue
        if found:
            return found.group(0)
    scan = shutil.which("ssh-keyscan")
    if scan:
        try:
            out = subprocess.run([scan, "-t", "ed25519", "127.0.0.1"], capture_output=True,
                                 text=True, timeout=8, creationflags=FLAGS).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            found = HOST_KEY.search(line)
            if found and not line.startswith("#"):
                return found.group(0)
    raise ValueError("Couldn't read this PC's SSH server key. " + SSHD_FIX[_os()])


def _local_ips():
    found = []
    try:  # the address of the default route; a UDP connect sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    tailscale = shutil.which("tailscale")
    if tailscale:
        try:
            out = subprocess.run([tailscale, "ip", "-4"], capture_output=True, text=True,
                                 timeout=4, creationflags=FLAGS)
            if out.returncode == 0:
                found += out.stdout.split()
        except (OSError, subprocess.SubprocessError):
            pass
    return found


def addresses(config, main, extra=()):
    """Where nodes should dial the main, best first: given ones, this PC's live addresses
    (LAN, then Tailscale), then the targets already configured for it."""
    machine = (config.get("devices") or {}).get(main) or {}
    configured = [t.split("@", 1)[-1] for t in [machine.get("target"), *machine.get("fallback_targets", [])]
                  if isinstance(t, str)]
    out = []
    for value in [*extra, *_local_ips(), *configured]:
        value = str(value).strip()
        if (value and ADDRESS.fullmatch(value) and not value.startswith("127.")
                and value != "localhost" and value not in out):
            out.append(value)
    return out[:8]


def _windows_admin():
    """Is this account in Administrators, elevated or not? sshd logs in with the full token,
    so a UAC-filtered admin still gets administrators_authorized_keys; IsUserAnAdmin()
    only says whether this terminal is elevated. whoami lists the group either way."""
    try:
        out = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=10, creationflags=FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if "S-1-5-32-544" in out:  # BUILTIN\Administrators
        return True
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _authorized_keys():
    if _os() == "windows" and _windows_admin():
        # Windows OpenSSH reads only this file for members of Administrators.
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ssh/administrators_authorized_keys"
    return Path.home() / ".ssh/authorized_keys"


_KEYS_THREADS = threading.Lock()


@contextmanager
def _keys_locked(path):
    """Hold authorized_keys for one read-modify-write. `cluster invite` (its own process) and
    the hub's redeem (a server thread) both rewrite it; unlocked, one drops the other's line.
    Blocking, unlike locking.exclusive_lock: the other writer finishes in milliseconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _KEYS_THREADS, open(path.with_name(f".{path.name}.jarvis-lock"), "a+b") as handle:
        if _os() == "windows":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10 s, then OSError
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _os() == "windows":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def restrictions(port):
    """Everything the node's key may do on the main: forward to the hub. Verified against
    OpenSSH 10: a shell gets the forced echo, -L elsewhere and any -R are refused."""
    return (f'restrict,port-forwarding,permitopen="127.0.0.1:{int(port)}",'
            f'permitlisten="127.0.0.1:1",command="echo jarvis-node-link"')


def _key_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def authorized(node, path=None, public_key=None):
    """This node has a JARVIS key line (holding exactly `public_key`, when given)."""
    path = path or _authorized_keys()
    key = " ".join(str(public_key).split()[:2]) if public_key else ""
    return any(line.rstrip().endswith(" " + MARK + node) and (not key or f" {key} " in line)
               for line in _key_lines(path))


def _write_keys(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    if _os() != "windows":
        path.parent.chmod(0o700)  # sshd's StrictModes ignores keys in a group-writable folder
    atomic_write(path, "".join(line + "\n" for line in lines), fsync=True,
                 mode=None if _os() == "windows" else 0o600)


def authorize(node, public_key, port, path=None):
    """Replace this node's JARVIS key line (only that one) with a fresh restricted one."""
    path = path or _authorized_keys()
    parts = public_key.split()
    if len(parts) < 2 or not HOST_KEY.fullmatch(" ".join(parts[:2])):
        raise ValueError("ssh-keygen made an unexpected public key.")
    with _keys_locked(path):
        lines = [line for line in _key_lines(path) if not line.rstrip().endswith(" " + MARK + node)]
        lines.append(f"{restrictions(port)} {parts[0]} {parts[1]} {MARK}{node}")
        _write_keys(path, lines)
    return str(path)


def unauthorize(node, path=None):
    path = path or _authorized_keys()
    with _keys_locked(path):
        lines = _key_lines(path)
        keep = [line for line in lines if not line.rstrip().endswith(" " + MARK + node)]
        if len(keep) != len(lines):
            _write_keys(path, keep)
    return len(lines) - len(keep)


def keypair(node):
    """A new ed25519 key for one node: (private OpenSSH text, public line)."""
    binary = shutil.which("ssh-keygen")
    if not binary:
        raise ValueError("ssh-keygen (OpenSSH client) is not installed.")
    with tempfile.TemporaryDirectory(prefix="jarvis-key-") as folder:
        key = Path(folder) / "key"
        subprocess.run([binary, "-q", "-t", "ed25519", "-N", "", "-C", MARK + node, "-f", str(key)],
                       check=True, capture_output=True, timeout=30, creationflags=FLAGS)
        return key.read_text(encoding="ascii"), key.with_suffix(".pub").read_text(encoding="ascii").strip()


def encode(note):
    raw = zlib.compress(json.dumps(note, separators=(",", ":")).encode(), 9)
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(value):
    """An invite from its code, a file holding the code, or None for anything else."""
    text = str(value or "").strip()
    if not text.startswith(PREFIX):
        try:
            candidate = Path(text).expanduser()
            text = candidate.read_text(encoding="utf-8").strip() if candidate.is_file() else text
        except (OSError, ValueError):
            pass
        try:  # an `invite --output` file: {"code": "jarvis-invite-1:..."}
            saved = json.loads(text)
            text = saved["code"] if isinstance(saved, dict) and isinstance(saved.get("code"), str) else text
        except ValueError:
            pass
    if not text.startswith(PREFIX):
        return None
    body = re.sub(r"\s+", "", text[len(PREFIX):])
    try:
        inflate = zlib.decompressobj()
        raw = inflate.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), 65536)
        if not inflate.eof:  # cut short in the paste, or bigger than any real invite
            raise ValueError
        note = json.loads(raw)
    except (ValueError, zlib.error):
        raise ValueError("That invite code is damaged; copy the whole line again.") from None
    if not isinstance(note, dict) or note.get("v") != 1:
        raise ValueError("Unknown invite version; update JARVIS on this PC.")
    return note


def invite(config, path, state, node, extra=(), capabilities=None, output=None):
    """Pair a new node: one restricted SSH key + one hub credential, as a single code."""
    local, main = identity(config)
    if local != main:
        raise ValueError("Create invites on the main PC.")
    if not node or not DEVICE_ID.fullmatch(node) or node == main:
        raise ValueError("Name the node, e.g. jarvis cluster invite dell.")
    from .cluster_tunnel import mode
    if mode(config) != "forward":
        raise ValueError('Invites make forward links (the node dials the main). Set '
                         '"cluster": {"tunnel": "forward"} or use jarvis cluster enroll.')
    # Before anything is rotated: a leftover file must not unpair the node and print no code.
    target = Path(output).expanduser() if output else None
    if target is not None and target.exists():
        raise ValueError("That invite file already exists; choose a new name.")
    if not _sshd_up():
        raise ValueError("This PC's SSH server isn't answering on port 22. " + SSHD_FIX[_os()])
    found = addresses(config, main, extra)
    if not found:
        raise ValueError("No address found for this PC; pass --address IP_OR_NAME.")
    key = host_key()
    caps = sorted(capabilities or NODE_DEFAULT)
    devices = dict(config.get("devices") or {})
    if node not in devices:
        devices[node] = {"label": node}
        save_json(path, {**config, "devices": devices})
    port = int((config.get("cluster") or {}).get("listen_port") or DEFAULT_PORT)
    private, public = keypair(node)
    try:
        keys_file = authorize(node, public, port)
    except PermissionError:
        raise ValueError(f"Can't write {_authorized_keys()}. On Windows, admin accounts' SSH keys "
                         "live there: run this invite in an administrator terminal.") from None
    # Rotates any earlier credential for this node. The node trades this one for its own
    # on joining (cluster.redeem_invite), so the code is single-use as well as short-lived.
    token = Broker(state).enroll(node, caps, invite_ttl=INVITE_TTL)
    me = devices.get(main) or {}
    code = encode({"v": 1, "node_id": node, "main_device": main, "user": getpass.getuser(),
                   "addresses": found, "host_key": key, "port": port, "token": token,
                   "key": private, "capabilities": caps, "code_key": code_key(state),
                   "main": {k: me[k] for k in ("hostname", "label", "aliases") if k in me}})
    report = {"invited": node, "capabilities": caps, "addresses": found,
              "authorized_keys": keys_file, "expires": "in 24 hours, or once used"}
    if target is not None:
        save_json(target, {"code": code})  # 0600, like any credential
        report["invite_file"] = str(target)
    report.update(code=code, next=f"On {node}, in its JARVIS folder: jarvis cluster join \"<code>\". "
                  f"The code is a password for {node}; a new invite or "
                  f"jarvis cluster revoke {node} cancels it.")
    return report


def revoke(state, node):
    Broker(state).revoke(node)
    try:
        removed = unauthorize(node)
    except OSError as exc:
        return {"revoked": node, "ssh_key_removed": False, "error": str(exc)}
    return {"revoked": node, "ssh_key_removed": bool(removed)}


# --- node side --------------------------------------------------------------------------

def _private_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with open(path, "x", encoding="ascii", newline="\n") as stream:
        if _os() != "windows":
            path.chmod(0o600)
        stream.write(text)
    if _os() == "windows":
        # Windows OpenSSH refuses a key that other accounts can read.
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"{domain}\\{user}:F" if domain else f"{user}:F"],
                       capture_output=True, timeout=15, creationflags=FLAGS)


def _validate(note):
    for key in ("node_id", "main_device"):
        if not isinstance(note.get(key), str) or not DEVICE_ID.fullmatch(note[key]):
            raise ValueError("The invite names an invalid device.")
    if note["node_id"] == note["main_device"]:
        raise ValueError("A main PC cannot join itself as a node.")
    token = note.get("token")
    if not isinstance(token, str) or not 20 <= len(token) <= 256:
        raise ValueError("The invite has an invalid credential.")
    if not isinstance(note.get("host_key"), str) or not HOST_KEY.fullmatch(note["host_key"]):
        raise ValueError("The invite has an invalid host key.")
    if not isinstance(note.get("key"), str) or "BEGIN OPENSSH PRIVATE KEY" not in note["key"]:
        raise ValueError("The invite has no SSH key.")
    if not isinstance(note.get("port"), int) or not 1 <= note["port"] <= 65535:
        raise ValueError("The invite has an invalid hub port.")
    if not isinstance(note.get("code_key"), str) or not CODE_KEY.fullmatch(note["code_key"]):
        raise ValueError("The invite has no code-signing key; make a new one on the updated main.")
    from .transfer import _check_target
    targets = [f"{note.get('user')}@{a}" for a in note.get("addresses") or []]
    if not targets:
        raise ValueError("The invite lists no address for the main.")
    for target in targets:
        _check_target(target)
    hostname = str((note.get("main") or {}).get("hostname") or note["main_device"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", hostname):
        raise ValueError("The invite has an invalid main hostname.")
    return targets, hostname


def _agent_running(state):
    from .locking import exclusive_lock
    try:
        with exclusive_lock(Path(state) / "cluster-node.lock"):
            return False
    except ValueError:
        return True


def _launch_agent(path, state):
    """Start `jarvis cluster run` in the background, the way the tunnel worker starts."""
    import sys
    python = Path(sys.executable)
    if on_windows() and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    argv = [str(python), "-m", "jarvis", "--config", str(Path(path).resolve()),
            "--state", str(Path(state).resolve()), "cluster", "run"]
    if on_windows():  # through Explorer, so it outlives this shell (see cluster_tunnel.start)
        script = Path(state) / "cluster-node-launch.vbs"
        from .autostart import vbs
        line = subprocess.list2cmdline(argv).replace('"', '""')
        script.write_text(vbs(line), encoding="utf-8")
        subprocess.Popen(["explorer.exe", str(script)], creationflags=FLAGS)
    else:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def join(config, path, state, note, autostart=True, start=True, wait=25):
    """Pair this PC as a node from a decoded invite, then connect and check."""
    targets, main_host = _validate(note)
    node, main = note["node_id"], note["main_device"]
    state = Path(state).resolve()
    if enabled(config):
        try:
            local, current_main = identity(config)
        except ValueError:
            local = current_main = None
        if local and local == current_main and local != node:
            raise ValueError(f"This PC is the main ({local}). Run jarvis cluster main {main} "
                             "first if it should become a node.")
    here = _this_device(config)
    if here and here != node:
        raise ValueError(f"This PC is configured as {here} (hostname {_hostname()}), but the "
                         f"invite is for {node}. Invite {here} instead.")
    from . import cluster_tunnel
    if cluster_tunnel.status(state)["running"]:
        cluster_tunnel.stop(state)  # it holds the previous link
    key_file = state / "cluster-node.key"
    _private_file(key_file, note["key"])
    save_json(state / "cluster-node.json", {
        "node_id": node, "main_device": main, "token": note["token"], "redeem": True,
        "code_key": note["code_key"],  # only code signed by this key is installed
        "link": {"targets": targets, "host_key": note["host_key"], "hostname": main_host,
                 "identity_file": str(key_file)}})
    devices = dict(config.get("devices") or {})
    mine = dict(devices.get(node) or {})
    local_device = "auto"
    if not mine.get("hostname"):
        mine["hostname"] = _hostname()
    elif mine["hostname"].casefold() != _hostname().casefold():
        local_device = node  # the shared config names another hostname; don't rewrite it
    devices[node] = mine
    devices.setdefault(main, {k: v for k, v in (note.get("main") or {}).items()
                              if k in ("hostname", "label", "aliases")})
    settings = {**(config.get("cluster") or {}), "enabled": True, "main_device": main,
                "local_device": local_device, "tunnel": "forward", "listen_host": "127.0.0.1",
                "listen_port": note["port"], "main_url": f"http://127.0.0.1:{note['port']}"}
    proposed = {**config, "devices": devices, "cluster": settings}
    identity(proposed)
    save_json(path, proposed)
    report = {"joined": node, "main_device": main, "addresses": targets}
    if autostart:
        from .cluster_cli import autostart as role_autostart
        report["autostart"] = {"tunnel": cluster_tunnel.autostart(path, state, True),
                               "node": role_autostart(path, state, True)}
    if start:
        report["tunnel"] = cluster_tunnel.start(path, state).get("running", False)
        report["redeemed"] = _redeem(proposed, state, wait)
        if not _agent_running(state):
            time.sleep(1)  # a just-enabled systemd unit may be taking the lock
            if not _agent_running(state):
                _launch_agent(path, state)
        report["connected"] = _await_hub(proposed, state, wait)
    report["next"] = ("jarvis cluster doctor" if report.get("connected", True) is False
                      else "Done. Check any time with: jarvis cluster doctor")
    return report


def _redeem(config, state, seconds):
    """Trade the invite's credentials for this node's own as soon as the main answers.
    If it can't be reached yet, the node agent does it on its next start."""
    from .cluster import HubRefused, redeem_invite
    deadline = time.monotonic() + seconds
    while True:
        try:
            return redeem_invite(config, state) or {"token": False, "note": "nothing to redeem"}
        except HubRefused as exc:
            return {"token": False, "error": str(exc),
                    "fix": "Ask the main for a new invite: jarvis cluster invite NODE"}
        except (OSError, ValueError, RuntimeError) as exc:
            if time.monotonic() >= deadline:
                return {"token": False, "pending": str(exc),
                        "note": "The node agent will finish this when it reaches the main."}
            time.sleep(1)


def _await_hub(config, state, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            Client(config, state).request("/v1/status", timeout=3)
            return True
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)


def leave(config, path, state):
    """Forget this node's link: stop the tunnel and login startup, delete the credential."""
    from . import cluster_tunnel
    from .cluster_cli import autostart as role_autostart
    state = Path(state).resolve()
    try:
        node = identity(config)[0]
    except ValueError:
        node = "NODE"
    report = {"left": node}
    if cluster_tunnel.status(state)["running"]:
        report["tunnel"] = cluster_tunnel.stop(state)
    for name, off in (("tunnel", lambda: cluster_tunnel.autostart(path, state, False)),
                      ("node", lambda: role_autostart(path, state, False))):
        try:
            off()
        except (OSError, subprocess.SubprocessError) as exc:
            report[f"{name}_autostart_error"] = str(exc)
    for name in ("cluster-node.json", "cluster-node.key"):
        (state / name).unlink(missing_ok=True)
    save_json(path, {**config, "cluster": {**(config.get("cluster") or {}), "enabled": False}})
    report["next"] = f"Restart or stop this PC's node agent. On the main: jarvis cluster revoke {node}"
    return report


# --- doctor -----------------------------------------------------------------------------

def _check(checks, name, ok, detail="", fix=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail, **({"fix": fix} if fix and not ok else {})})
    return bool(ok)


def doctor(config, path, state):
    """Every link in the chain from config to a working node, in order, with a fix each."""
    from . import cluster_tunnel
    from .deploy import code_fingerprint
    checks = []
    if not enabled(config):
        _check(checks, "cluster enabled", False, "cluster.enabled is false",
               "On the main: jarvis cluster init. On a node: jarvis cluster join CODE")
        return {"ok": False, "checks": checks}
    try:
        local, main = identity(config)
    except ValueError as exc:
        _check(checks, "this PC's identity", False, str(exc),
               f"Add this PC (hostname {_hostname()}) to devices, or set cluster.local_device")
        return {"ok": False, "checks": checks}
    role = "main" if local == main else "node"
    _check(checks, "this PC's identity", True, f"{local} ({role}); main is {main}")
    forward = cluster_tunnel.mode(config) == "forward"
    if role == "main":
        from .release import served_code  # nodes must match the tested release, not the live tree
        _doctor_main(config, state, main, forward, checks, served_code(state))
    else:
        _doctor_node(config, state, local, main, forward, checks, code_fingerprint())
    return {"ok": all(c["ok"] for c in checks), "role": role, "checks": checks}


def _doctor_main(config, state, main, forward, checks, code):
    status = None
    try:
        status = Client(config, state, owner=True).request("/v1/status", timeout=3)
        _check(checks, "hub running", True, (config.get("cluster") or {}).get("main_url", ""))
    except (OSError, ValueError, RuntimeError) as exc:
        _check(checks, "hub running", False, str(exc),
               "jarvis cluster autostart on   (or run: jarvis cluster run)")
    if forward:
        _check(checks, "SSH server on this PC", _sshd_up(), "port 22 on 127.0.0.1", SSHD_FIX[_os()])
    nodes = {n["id"]: n for n in (status or {}).get("nodes") or Broker(state).nodes()}
    for device in sorted(set(config.get("devices") or {}) - {main}):
        node = nodes.get(device)
        if not node:
            _check(checks, f"{device}: paired", False, "no credential on this main",
                   f"jarvis cluster invite {device}")
            continue
        if forward and (config.get("devices") or {}).get(device, {}).get("transport") != "ssh":
            try:
                ok = authorized(device)
            except OSError:
                ok = False
            _check(checks, f"{device}: SSH key authorized", ok, "authorized_keys entry " + MARK + device,
                   f"jarvis cluster invite {device}   (then join on {device} again)")
        _check(checks, f"{device}: online", node.get("online"),
               f"last seen {round(time.time() - node['seen'])} s ago" if node.get("seen") else "never seen",
               f"On {device}: jarvis cluster doctor")
        ended = Broker(state).delivery(device)
        lost = ended.get("uncertain", 0) + ended.get("expired", 0)
        _check(checks, f"{device}: every job delivered (24 h)", not lost,
               ", ".join(f"{count} {name}" for name, count in sorted(ended.items())) or "no jobs",
               f"Jobs went unanswered: on {device}, jarvis cluster doctor (look at link health)")
        if node.get("online"):
            _check(checks, f"{device}: same code", node.get("code") == code,
                   f"{node.get('code') or 'unknown'} vs main {code}",
                   f"jarvis cluster update {device}   (forward nodes update themselves within a minute)")


def _link_hint(error, forward, main):
    """What to do about dropped polls. They travel over the SSH tunnel, so Wi-Fi settings and
    invite addresses only matter if the tunnel itself keeps reconnecting."""
    error = (error or "").lower()
    if "closed connection" in error or "refused" in error or "reset" in error:
        lead = (f"The tunnel was up but nothing answered on {main}: usually JARVIS restarting "
                f"there (on {main}: systemctl --user status jarvis-cluster). ")
    else:
        lead = ""
    if forward:
        return (lead + "Polls go through this PC's SSH tunnel: jarvis cluster tunnel status, and "
                "its log in .jarvis/logs/cluster-tunnel.log. Only if the tunnel keeps reconnecting is "
                "the network between the PCs the problem (Wi-Fi, Tailscale).")
    return (lead + f"Polls go through the SSH tunnel {main} opens to this PC: on {main}, "
            "jarvis cluster tunnel status and its log.")


def _doctor_node(config, state, local, main, forward, checks, code):
    from . import cluster_tunnel
    from .transfer import _ssh_args
    try:
        Client(config, state)
        _check(checks, "paired with the main", True, f"credential for {local}")
    except ValueError as exc:
        _check(checks, "paired with the main", False, str(exc),
               f"On {main}: jarvis cluster invite {local}; here: jarvis cluster join CODE")
        return
    if forward:
        try:
            machine = cluster_tunnel.peer(config, state)
        except ValueError as exc:
            _check(checks, "SSH link", False, str(exc), f"Join again with a new invite from {main}")
            return
        binary = shutil.which("ssh")
        if not _check(checks, "OpenSSH client", binary, binary or "ssh not found",
                      "Install the OpenSSH client (Windows: Settings > Optional features)"):
            return
        key = machine.get("identity_file")
        if key and not _check(checks, "SSH key file", Path(key).expanduser().is_file(), key,
                              f"Join again with a new invite from {main}"):
            return
        targets = [machine["target"], *machine.get("fallback_targets", [])]
        reachable = [t for t in targets if cluster_tunnel.reachable(t)]
        if not _check(checks, f"{main} reachable", reachable,
                      ", ".join(reachable) if reachable else "no answer on port 22 at " + ", ".join(targets),
                      f"Is {main} awake and on this network (or Tailscale)? If its address "
                      f"changed: on {main}, jarvis cluster invite {local}, then join again"):
            return
        argv = [*_ssh_args(binary, key), *cluster_tunnel.pinned_host(machine, state), "-T", reachable[0]]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=20, creationflags=FLAGS)
            ok = out.returncode == 0 and "jarvis-node-link" in out.stdout
            detail = reachable[0] if ok else (out.stderr.strip()[-200:] or f"ssh exited {out.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            ok, detail = False, str(exc)
        if "REMOTE HOST IDENTIFICATION" in detail or "Host key verification" in detail:
            fix = f"{main}'s SSH host key changed; make a new invite there and join again"
        else:
            fix = f"On {main}: jarvis cluster invite {local}; here: jarvis cluster join CODE"
        _check(checks, "SSH login", ok, detail, fix)
        tunnel = cluster_tunnel.status(state)
        _check(checks, "tunnel running", tunnel.get("ssh_alive"),
               tunnel.get("error") or tunnel.get("target") or "",
               "jarvis cluster tunnel start --autostart")
    try:
        status = Client(config, state).request("/v1/status", timeout=3)
        _check(checks, "hub answers", True, (config.get("cluster") or {}).get("main_url", ""))
    except (OSError, ValueError, RuntimeError) as exc:
        _check(checks, "hub answers", False, str(exc),
               f"Is JARVIS running on {main}? There: jarvis cluster doctor")
        return
    _check(checks, "node agent running", _agent_running(state), "jarvis cluster run",
           "jarvis cluster autostart on")
    try:
        stats = json.loads((Path(state) / "cluster-link-stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stats = None
    if stats:
        polls = stats["polls_ok"] + stats["polls_failed"]
        # A few failures while the main reboots are normal; a steady share means a bad link.
        # A main restart fails several polls in a row but is one outage; only repeated
        # outages count against the link (older stats without the count: the share alone).
        outages = stats.get("outages")
        _check(checks, "link health", polls < 10 or stats["polls_failed"] / polls < 0.1
               or (outages is not None and outages <= max(2, polls // 100)),
               f"{stats['polls_ok']}/{polls} polls reached the main"
               f"{f' ({outages} outage' + ('s' if outages != 1 else '') + ')' if outages else ''}, "
               f"{stats['results_sent']} results "
               f"delivered, {stats['results_resent']} resent; last error: {stats['last_error'] or 'none'}",
               _link_hint(stats.get("last_error"), forward, main))
    me = next((n for n in status.get("nodes") or [] if n["id"] == local), {})
    _check(checks, "main sees this node", me.get("online"), "online" if me.get("online") else "offline",
           "Start the node agent: jarvis cluster autostart on")
    if status.get("code"):
        _check(checks, "same code as the main", status["code"] == code,
               f"{code} vs main {status['code']}",
               "Leave the node agent running; it installs the main's code and restarts itself")
