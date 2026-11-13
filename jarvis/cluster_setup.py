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
import copy
import errno
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

from .fsio import atomic_write, read_json
from .platforms import NO_WINDOW
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
    (only the main needs an SSH server), and run the hub at login.

    The file under the fleet lock decides. An enabled fleet that already names
    another main is refused before any write or autostart. That change is
    `jarvis cluster main`, not init. First setup, a disabled cluster, and a
    refresh when this PC is already the main still write."""
    host = _hostname()
    requested = device
    from .platforms import detect
    label = detect()["label"]
    chosen = {}

    def change(doc):
        # A missing file has no disk role yet: the caller's document is the
        # first setup. An existing file, including {}, is the only source.
        if not doc and not Path(path).is_file() and isinstance(config, dict):
            doc.update(copy.deepcopy(config))
        devices = dict(doc.get("devices") or {})
        device_id = requested or _this_device(doc) or _slug(host)
        if not DEVICE_ID.fullmatch(device_id):
            raise ValueError("Device IDs are letters, digits, - and _ (e.g. zenbook).")
        entry = dict(devices.get(device_id) or {})
        known = str(entry.get("hostname", ""))
        if known and known.casefold() != host.casefold():
            raise ValueError(f"devices.{device_id}.hostname is {known}, but this PC is {host}. "
                             f"Run init with --id set to this PC's device, or fix that hostname.")
        settings = dict(doc.get("cluster") or {})
        existing = settings.get("main_device")
        if (isinstance(existing, str) and existing and enabled(doc)
                and existing != device_id):
            raise ValueError(f"This fleet already has a main ({existing}). "
                             f"Change it with: jarvis cluster main {device_id}")
        entry.setdefault("hostname", host)
        entry.setdefault("label", label)
        devices[device_id] = entry
        port = int(settings.get("listen_port") or DEFAULT_PORT)
        settings.update(enabled=True, main_device=device_id, local_device="auto",
                        tunnel=settings.get("tunnel", "forward"), listen_host="127.0.0.1",
                        listen_port=port, main_url=f"http://127.0.0.1:{port}")
        preview = {**doc, "devices": devices, "cluster": settings}
        identity(preview)  # fails before saving if the hostname is ambiguous
        doc["devices"] = devices
        doc["cluster"] = settings
        chosen["device"] = device_id
        chosen["settings"] = settings
        chosen["port"] = port

    from .config_lock import patch_config
    patch_config(state, path, change, backup="cluster-init", create=True)
    device = chosen["device"]
    settings = chosen["settings"]
    port = chosen["port"]
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
    from .platforms import host_keys_dir
    folder = host_keys_dir()
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
    from .cluster_recovery import magic_names, valid_address
    for value in [*extra, *_local_ips(), *magic_names(), *configured]:
        value = str(value).strip()
        if (value and valid_address(value) and not value.startswith("127.")
                and value != "localhost" and value not in out):
            out.append(value)
    return out[:8]


def _windows_admin():
    """Is this account in Administrators, elevated or not?

    ``platforms.ssh_admin_account`` is the authority. The whoami probe stays so a test
    can patch ``cluster_setup.subprocess.run`` and still see the Administrators SID;
    a hit returns before the helper, and a miss asks the helper (including the
    elevated-token check on Windows).
    """
    try:
        out = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=10, creationflags=FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if "S-1-5-32-544" in (out or ""):  # BUILTIN\Administrators
        return True
    from .platforms import ssh_admin_account
    return ssh_admin_account()


_windows_admin._platform_wrapper = True


def _authorized_keys():
    """The authorized_keys file OpenSSH reads for this account.

    Unpatched, this is ``platforms.ssh_authorized_keys_path``. Tests that replace
    ``_os`` or ``_windows_admin`` still get the path those replacements describe.
    """
    from . import platforms
    if _os() == platforms._os() and getattr(_windows_admin, "_platform_wrapper", False) is True:
        return platforms.ssh_authorized_keys_path()
    if _os() == "windows" and _windows_admin():
        root = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(root) / "ssh" / "administrators_authorized_keys"
    return Path.home() / ".ssh" / "authorized_keys"


def _keys_locked(path):
    """Hold authorized_keys for one read-modify-write.

    The lock is ``platforms.locked_ssh_keys``: one thread lock and one OS lock
    shared with every other writer, in this process and across processes.
    """
    from .platforms import locked_ssh_keys
    return locked_ssh_keys(path)


def _checked_from(from_addr):
    """Text safe to place inside ``from="..."``, or None when there is no pin.

    None and "" leave the key line unchanged. Any other value must be one address,
    or a comma-separated list of them, and each element must match ADDRESS. Quotes,
    spaces, and newlines are refused: they would close the option or start another.
    The result is the joined checked parts, never the raw argument."""
    if from_addr is None or from_addr == "":
        return None
    if not isinstance(from_addr, str) or any(c in from_addr for c in "\r\n\x00\"' \t"):
        raise ValueError("Invalid source address pin.")
    parts = from_addr.split(",")
    if not parts or any(not ADDRESS.fullmatch(part) for part in parts):
        raise ValueError("Invalid source address pin.")
    return ",".join(parts)


def _recorded_node_addresses(device):
    """Addresses this config already stores for one node. Loopback is not one of them."""
    if not isinstance(device, dict):
        return []
    raw = []
    for field in ("lan_ip", "tailscale_ip"):
        value = device.get(field)
        if isinstance(value, str):
            raw.append(value)
    fallbacks = device.get("fallback_targets") or []
    if not isinstance(fallbacks, (list, tuple)):
        fallbacks = []
    for target in (device.get("target"), *fallbacks):
        if isinstance(target, str):
            raw.append(target.split("@", 1)[-1])
    out = []
    for value in raw:
        value = value.strip()
        if (value and ADDRESS.fullmatch(value) and not value.startswith("127.")
                and value.casefold() != "localhost" and value not in out):
            out.append(value)
    return out[:8]


def pinned_from(config, node):
    """Source pin for this node's key, or None so the line stays as it is today.

    There is no ``cluster.nodes`` map. The opt-in is the boolean
    ``cluster.pin_node_address`` on the existing cluster object. The address is not a
    free-form string: it is taken from what the device entry already records
    (``lan_ip``, ``tailscale_ip``, then the host in ``target`` and ``fallback_targets``).
    Several recorded addresses become one comma list, and only after each element matches
    ADDRESS. A value with a quote, a space, or a newline is ignored.

    The Dell's address changes between networks (home LAN, another LAN, Tailscale, DHCP).
    A pin sshd does not see locks that node out of the hub, so this is never turned on
    by itself. When the flag is on and no recorded address checks out, this returns None
    and the key is written exactly as before. Nothing about the address is logged.
    """
    if not isinstance(config, dict) or not isinstance(node, str) or not node:
        return None
    cluster = config.get("cluster")
    if not isinstance(cluster, dict) or cluster.get("pin_node_address") is not True:
        return None
    devices = config.get("devices")
    device = devices.get(node) if isinstance(devices, dict) else None
    found = _recorded_node_addresses(device)
    if not found:
        return None
    return ",".join(found)


def restrictions(port, from_addr=None):
    """Everything the node's key may do on the main: forward to the hub. Verified against
    OpenSSH 10: a shell gets the forced echo, -L elsewhere and any -R are refused.

    ``from_addr`` prepends ``from="<addr>",`` ahead of ``restrict``. OpenSSH reads
    authorized_keys options as one comma-separated field with no required order and no
    spaces outside quotes (sshd(8) AUTHORIZED_KEYS FILE FORMAT). ``from=`` only limits
    which client address may use the key; it does not interact with ``restrict``, so
    placing it first is valid. Leave ``from_addr`` unset and the result is the historical
    line, byte for byte.

    The Dell's address changes between networks, so nothing here enables the pin. A
    caller passes an address only when ``pinned_from`` saw ``cluster.pin_node_address``.
    Opted in with no known address, the caller passes nothing and this line is today's.
    The address is not logged.
    """
    line = (f'restrict,port-forwarding,permitopen="127.0.0.1:{int(port)}",'
            f'permitlisten="127.0.0.1:1",command="echo jarvis-node-link"')
    pin = _checked_from(from_addr)
    if pin is None:
        return line
    return f'from="{pin}",{line}'


def _key_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


_NODE_KINDS = {"ssh-ed25519", "sk-ed25519@openssh.com", "ssh-rsa"}
_KEY_BLOB = re.compile(r"[A-Za-z0-9+/=]+\Z")
_KEY_COMMENT = re.compile(r"[A-Za-z0-9_.:@+-]+\Z")


def _rsa_bits(blob):
    """Bit length of an OpenSSH ssh-rsa modulus, or 0 when the blob is not that key."""
    try:
        raw = base64.b64decode(blob, validate=True)

        def take():
            nonlocal raw
            if len(raw) < 4:
                raise ValueError
            size = struct.unpack(">I", raw[:4])[0]
            raw = raw[4:]
            if size > len(raw):
                raise ValueError
            chunk, raw = raw[:size], raw[size:]
            return chunk

        if take() != b"ssh-rsa":
            return 0
        take()  # exponent
        modulus = take()
        if modulus[:1] == b"\x00":
            modulus = modulus[1:]
        return int.from_bytes(modulus or b"\x00", "big").bit_length()
    except (ValueError, struct.error):
        return 0


def _public_key_parts(public_key):
    """Type and blob of one node public key. Options, newlines, and weak types are refused.

    The comment, when present, is checked and then dropped: the line JARVIS writes uses
    its own ``jarvis-node:`` mark, never text from the key."""
    if not isinstance(public_key, str) or any(c in public_key for c in "\r\n\x00"):
        raise ValueError("ssh-keygen made an unexpected public key.")
    parts = public_key.split(" ")
    if len(parts) not in (2, 3):
        raise ValueError("ssh-keygen made an unexpected public key.")
    kind, blob = parts[0], parts[1]
    if (kind not in _NODE_KINDS or kind.startswith("-") or any(c in kind for c in ",=\"")
            or blob.startswith("-") or any(c in blob for c in ",\"") or not _KEY_BLOB.fullmatch(blob)):
        raise ValueError("ssh-keygen made an unexpected public key.")
    if len(parts) == 3 and not _KEY_COMMENT.fullmatch(parts[2]):
        raise ValueError("ssh-keygen made an unexpected public key.")
    if kind == "ssh-rsa" and _rsa_bits(blob) < 3072:
        raise ValueError("ssh-rsa node keys must be at least 3072 bits.")
    return kind, blob


def authorized(node, path=None, public_key=None):
    """This node has a JARVIS key line (holding exactly `public_key`, when given)."""
    if not isinstance(node, str) or not DEVICE_ID.fullmatch(node):
        return False
    path = path or _authorized_keys()
    key = ""
    if public_key:
        try:
            kind, blob = _public_key_parts(public_key)
        except ValueError:
            return False
        key = f"{kind} {blob}"
    return any(line.rstrip().endswith(" " + MARK + node) and (not key or f" {key} " in line)
               for line in _key_lines(path))


def _write_keys(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    if _os() != "windows":
        path.parent.chmod(0o700)  # sshd's StrictModes ignores keys in a group-writable folder
    atomic_write(path, "".join(line + "\n" for line in lines), fsync=True,
                 mode=None if _os() == "windows" else 0o600)


def authorize(node, public_key, port, path=None, from_addr=None):
    """Replace this node's JARVIS key line (only that one) with a fresh restricted one.

    ``from_addr`` is passed through to ``restrictions``. Callers pass it only when the
    node opted in via ``pinned_from``. The default writes the same line as before."""
    if not isinstance(node, str) or not DEVICE_ID.fullmatch(node):
        raise ValueError("Invalid node ID.")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid cluster listen port.")
    kind, blob = _public_key_parts(public_key)
    # Check the pin before opening the key file, so a bad address writes nothing.
    options = restrictions(port, from_addr)
    path = path or _authorized_keys()
    with _keys_locked(path):
        lines = [line for line in _key_lines(path) if not line.rstrip().endswith(" " + MARK + node)]
        lines.append(f"{options} {kind} {blob} {MARK}{node}")
        _write_keys(path, lines)
    return str(path)


def unauthorize(node, path=None):
    if not isinstance(node, str) or not DEVICE_ID.fullmatch(node):
        raise ValueError("Invalid node ID.")
    path = path or _authorized_keys()
    with _keys_locked(path):
        lines = _key_lines(path)
        keep = [line for line in lines if not line.rstrip().endswith(" " + MARK + node)]
        if len(keep) != len(lines):
            _write_keys(path, keep)
    return len(lines) - len(keep)


def unauthorize_key(node, public_key, path=None):
    """Remove this node's line only when it still holds this exact public key.

    A newer line for the same node is left as it is. Callers that also hold the
    fleet lock take that lock first: this takes the SSH-key lock, which is the
    order revoke already uses. Returns how many lines were removed.
    """
    if not isinstance(node, str) or not DEVICE_ID.fullmatch(node):
        raise ValueError("Invalid node ID.")
    try:
        kind, blob = _public_key_parts(public_key)
    except ValueError:
        return 0
    bits = f"{kind} {blob}"
    path = path or _authorized_keys()
    mark = " " + MARK + node
    with _keys_locked(path):
        lines = _key_lines(path)
        keep = []
        removed = 0
        for line in lines:
            stripped = line.rstrip()
            if stripped.endswith(mark) and f" {bits} " in f" {stripped} ":
                removed += 1
                continue
            keep.append(line)
        if removed:
            _write_keys(path, keep)
    return removed


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


class InviteIncomplete(RuntimeError):
    """An invite stopped after a side effect. retryable is true only when this attempt's
    new credential and key were removed or put back. It is false when cleanup could not
    prove it still owned them: the caller must not try the same request again."""

    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = bool(retryable)


def _public_invite_error(exc):
    """A message safe to show. Never the private key, the code, or a token."""
    text = str(exc or "").strip()
    if (not text or "PRIVATE KEY" in text or "BEGIN OPENSSH" in text or "BEGIN " in text
            or PREFIX in text):
        return "The invite could not be created."
    return text[:300]


def _node_snapshot(state, node):
    """The node's credential row before this attempt, or None. Raw column values."""
    with Broker(state).db() as db:
        row = db.execute("SELECT token_hash, capabilities, seen, info, invite_expires "
                         "FROM nodes WHERE id=?", (node,)).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in ("token_hash", "capabilities", "seen", "info", "invite_expires")}


def _same_row(left, right):
    if left is None or right is None:
        return left is None and right is None
    return all(left.get(key) == right.get(key)
               for key in ("token_hash", "capabilities", "seen", "info", "invite_expires"))


def _restore_node(state, node, prior, attempt_hash):
    """Undo this attempt's node row only when that row is still the one we wrote.
    Returns False when the row is not ours: the caller must not claim the invite was atomic."""
    current = _node_snapshot(state, node)
    if not attempt_hash:
        return _same_row(current, prior)
    if current is None:
        return prior is None
    if current.get("token_hash") != attempt_hash:
        return False
    with Broker(state).db() as db:
        if prior is None:
            db.execute("DELETE FROM nodes WHERE id=? AND token_hash=?", (node, attempt_hash))
        else:
            db.execute("UPDATE nodes SET token_hash=?, capabilities=?, seen=?, info=?, invite_expires=? "
                       "WHERE id=? AND token_hash=?",
                       (prior["token_hash"], prior["capabilities"], prior["seen"], prior["info"],
                        prior["invite_expires"], node, attempt_hash))
        left = db.execute("SELECT token_hash FROM nodes WHERE id=?", (node,)).fetchone()
    if prior is None:
        return left is None
    return left is not None and left["token_hash"] == prior["token_hash"]


def _node_key_lines(node, path):
    mark = " " + MARK + node
    return [line.rstrip() for line in _key_lines(path) if line.rstrip().endswith(mark)]


def _restore_key(node, public, prior_lines, path):
    """Remove only the key line this attempt added. An existing line is put back.
    Returns False when a line for this node is present that this attempt does not own."""
    parts = str(public or "").split()
    if len(parts) < 2:
        return False
    bits = f"{parts[0]} {parts[1]}"
    mark = " " + MARK + node
    prior = [line.rstrip() for line in (prior_lines or [])]
    try:
        with _keys_locked(path):
            lines = [line.rstrip() for line in _key_lines(path) if line.strip()]
            ours, foreign, rest = [], [], []
            for line in lines:
                if not line.endswith(mark):
                    rest.append(line)
                elif f" {bits} " in f" {line} ":
                    ours.append(line)
                else:
                    foreign.append(line)
            if not ours:
                return foreign == prior
            if foreign and foreign != prior:
                return False  # another key for this node: not ours to delete or replace
            _write_keys(path, rest + prior)
            after = [line.rstrip() for line in _key_lines(path) if line.strip() and line.rstrip().endswith(mark)]
            if any(f" {bits} " in f" {line} " for line in after):
                return False
            return after == prior
    except OSError:
        return False


def invite(config, path, state, node, extra=(), capabilities=None, output=None):
    """Pair a new node: one restricted SSH key + one hub credential, as a single code."""
    local, main = identity(config)
    if local != main:
        raise ValueError("Create invites on the main PC.")
    if not node or not DEVICE_ID.fullmatch(node) or node == main:
        raise ValueError("Name the node, e.g. jarvis cluster invite dell.")
    # Same ledger as the console. A different node id is not stopped, and this does not
    # freeze every invite. Checked again under the lock, after the signing key is made.
    from .fleet_management import assert_node_admitted
    assert_node_admitted(state, node)
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
    caps = sorted(capabilities or NODE_DEFAULT)
    # The slow, side-effect-free material is made first, outside the config lock: the
    # signing key, the host key and the node's key pair. A failure here changes nothing that
    # belongs to this node. An existing code-signing key is left as it is.
    signing = code_key(state)
    host = host_key()
    private, public = keypair(node)
    keys_path = _authorized_keys()
    from .config_lock import config_mutation
    with config_mutation(state):  # re-entrant: the console invite already holds it
        return _invite_locked(config, path, state, node, caps, found, target,
                              signing, host, private, public, keys_path)


def _role_guard(node):
    """Refuse unless the config on disk still says this PC is the main and links forward."""
    def check(doc):
        from .cluster_tunnel import mode
        local, main = identity(doc)
        if local != main:
            raise ValueError("This PC is no longer the main; nothing was changed.")
        if node == main:
            raise ValueError("The main can't be invited.")
        if mode(doc) != "forward":
            raise ValueError("The tunnel is no longer forward; nothing was changed.")
    return check


def _fence_unverified(state, node, reason):
    """Record one uncertain receipt, then fail closed.

    A failed fence write is not treated as recorded: the error stays non-retryable and
    says the receipt could not be saved. It does not clear any fence already on disk."""
    from .fleet_management import note_uncertain_attempt
    try:
        note_uncertain_attempt(state, node)
    except (OSError, ValueError) as exc:
        raise InviteIncomplete(
            "The invite stopped after a credential change and the uncertain receipt could not be saved. "
            "It was not atomic. Retrying this request would not be safe.",
            retryable=False) from exc
    raise InviteIncomplete(
        "The invite stopped after a credential change and the cleanup could not be verified. "
        "It was not atomic. Retrying this request would not be safe.",
        retryable=False) from reason


def _ensure_invite_holds(state, path, node, public, prior_lines, keys_path, prior_row, token):
    """Re-read role and admission after a step that can itself write the config.

    A settings save on this thread re-enters the config lock and can change the tunnel
    after the first guard. Do not enroll, and do not return a code, if the file no longer
    allows the invite. Compensation restores only this attempt's row and key. Jobs are
    settled only after a code exists, so a finished job is never resurrected here."""
    from .config_lock import read_config
    from .fleet_management import assert_node_admitted
    reason = None
    try:
        _role_guard(node)(read_config(path))
        assert_node_admitted(state, node)
    except ValueError as exc:
        reason = exc
    if reason is None:
        return
    attempt = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and token else None
    node_ok = _restore_node(state, node, prior_row, attempt)
    key_ok = _restore_key(node, public, prior_lines, keys_path)
    if node_ok and key_ok:
        raise InviteIncomplete(str(reason), retryable=True) from reason
    _fence_unverified(state, node, reason)


def _invite_locked(config, path, state, node, caps, found, target, signing, host, private,
                   public, keys_path):
    """The part of an invite that changes things. Runs under the managed config lock, on the
    config as it is on disk now (a role or tunnel flip while the keys were made is seen)."""
    from .config_lock import patch_config, read_config
    from .fleet_management import assert_node_admitted
    disk = read_config(path)
    _role_guard(node)(disk)
    assert_node_admitted(state, node)
    local, main = identity(disk)
    prior_row = _node_snapshot(state, node)
    prior_lines = _node_key_lines(node, keys_path)
    token = None
    try:
        if node not in (disk.get("devices") or {}):
            def add(doc):
                doc.setdefault("devices", {}).setdefault(node, {"label": node})
            disk, _ = patch_config(state, path, add, guard=_role_guard(node))
        devices = disk.get("devices") or {}
        port = int((disk.get("cluster") or {}).get("listen_port") or DEFAULT_PORT)
        try:
            keys_file = authorize(node, public, port, path=keys_path,
                                  from_addr=pinned_from(disk, node))
        except PermissionError:
            raise ValueError(f"Can't write {keys_path}. On Windows, admin accounts' SSH keys "
                             "live there: run this invite in an administrator terminal.") from None
        # A same-thread settings save during authorize can flip the tunnel after the guard.
        _ensure_invite_holds(state, path, node, public, prior_lines, keys_path, prior_row, None)
        # Rotates any earlier credential for this node. The node trades this one for its own
        # on joining (cluster.redeem_invite), so the code is single-use as well as short-lived.
        token = Broker(state).enroll(node, caps, invite_ttl=INVITE_TTL, settle_jobs=False)
        _ensure_invite_holds(state, path, node, public, prior_lines, keys_path, prior_row, token)
        me = devices.get(main) or {}
        code = encode({"v": 1, "node_id": node, "main_device": main, "user": getpass.getuser(),
                       "addresses": found, "host_key": host, "port": port, "token": token,
                       "key": private, "capabilities": caps, "code_key": signing,
                       "main": {k: me[k] for k in ("hostname", "label", "aliases") if k in me}})
        report = {"invited": node, "capabilities": caps, "addresses": found,
                  "authorized_keys": keys_file, "expires": "in 24 hours, or once used"}
        if target is not None:
            save_json(target, {"code": code})  # 0600, like any credential
            report["invite_file"] = str(target)
        # The rotation is final now. Only from here are jobs still running under the old
        # credential marked uncertain, so a rolled-back invite never had to "restore" a job
        # (and a job that finished meanwhile is never resurrected). Best effort: the old
        # credential is already dead, so such a job can't report back either way.
        try:
            Broker(state).settle_rotated(node)
        except Exception:  # noqa: BLE001 - the invite itself succeeded
            pass
        report.update(code=code, next=f"On {node}, in its JARVIS folder: jarvis cluster join \"<code>\". "
                      f"The code is a password for {node}; a new invite or "
                      f"jarvis cluster revoke {node} cancels it.")
        return report
    except InviteIncomplete:
        raise
    except Exception as exc:
        # Roll back only a row or key line this attempt still owns. Never delete a
        # credential or key that was already there, and never delete one we no longer own.
        attempt = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and token else None
        node_ok = _restore_node(state, node, prior_row, attempt)
        key_ok = _restore_key(node, public, prior_lines, keys_path)
        if node_ok and key_ok:
            raise InviteIncomplete(_public_invite_error(exc), retryable=True) from exc
        _fence_unverified(state, node, exc)


def revoke(state, node):
    """Cut the credential, then this node's receipts. CLI and console both use this.

    Broker.revoke runs first. If it raises, the receipt file is not opened and no fence
    is cleared. SSH key removal is reported and does not by itself keep the fence: the
    credential is already gone. A damaged receipt file does not undo the credential cut
    and does not wipe another node's rows. Other node ids stay."""
    from .config_lock import config_mutation
    with config_mutation(state):
        Broker(state).revoke(node)
        try:
            removed = unauthorize(node)
        except OSError as exc:
            result = {"revoked": node, "ssh_key_removed": False, "error": str(exc)}
        else:
            result = {"revoked": node, "ssh_key_removed": bool(removed)}
        from .fleet_management import FleetError, clear_node_receipts
        try:
            clear_node_receipts(state, node)
        except FleetError:
            pass  # a damaged ledger must not block cutting the credential
        return result


# --- node side --------------------------------------------------------------------------

def _unlink_quiet(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _private_file(path, text):
    """Write an SSH private key and restrict it to this account.

    Permissions are ``platforms.private_key_permissions``. If that fails, including
    ``ValueError`` when ``USERNAME`` is missing, the new file is removed and any
    previous key at ``path`` is left as it was.
    """
    from .platforms import private_key_permissions
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".writing")
    try:
        temporary.unlink(missing_ok=True)
        # Born 0600 (not 0666 & ~umask, then chmod): no window where another user can read it.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                     0o600)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(text)
        private_key_permissions(temporary)
        os.replace(temporary, path)
    except BaseException:
        _unlink_quiet(temporary)
        raise
    return path


def _snapshot_regular(path):
    """Bytes of a regular file, or None when it is absent.

    The last path component is not followed. A symlink is refused so a join
    cannot read, or later replace, whatever it points at.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path.name} is not a regular file in the state folder, so this "
                         "join did not read or replace it.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            raise ValueError(f"{path.name} is not a regular file in the state folder, so this "
                             "join did not read or replace it.") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular file in the state folder, so this "
                             "join did not read or replace it.")
        chunks = []
        total = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            total += len(block)
            if total > 1_048_576:
                raise ValueError(f"{path.name} is too large to replace safely, so this join "
                                 "did not change it.")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _stage_secret(path, data, *, private):
    """Write ``data`` to a new temp file beside ``path`` and restrict it. Do not publish."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        os.write(fd, data)
    except BaseException:
        os.close(fd)
        _unlink_quiet(temporary)
        raise
    os.close(fd)
    try:
        if private:
            from .platforms import private_key_permissions
            private_key_permissions(temporary)
        else:
            os.chmod(temporary, 0o600)
    except BaseException:
        _unlink_quiet(temporary)
        raise
    return temporary


def _publish_owned(key_file, key_text, note_file, payload, owned):
    """Snapshot the live key and note, stage the new bytes, then publish them.

    ``owned`` records the snapshot and the bytes this attempt published so a later
    config failure can put back only those bytes.
    """
    owned["prior"][key_file] = _snapshot_regular(key_file)
    owned["prior"][note_file] = _snapshot_regular(note_file)
    staged_key = _stage_secret(key_file, key_text.encode("ascii"), private=True)
    staged_note = None
    try:
        staged_note = _stage_secret(note_file, json.dumps(payload, indent=2).encode("utf-8"),
                                    private=False)
        attempt_key = _snapshot_regular(staged_key)
        attempt_note = _snapshot_regular(staged_note)
        os.replace(staged_key, key_file)
        owned["attempt"][key_file] = attempt_key
        staged_key = None
        os.replace(staged_note, note_file)
        owned["attempt"][note_file] = attempt_note
        staged_note = None
    finally:
        if staged_key is not None:
            _unlink_quiet(staged_key)
        if staged_note is not None:
            _unlink_quiet(staged_note)


def _drop_if_attempt(path, attempt):
    path = Path(path)
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return
    try:
        if _snapshot_regular(path) != attempt:
            return
        path.unlink()
    except (OSError, ValueError):
        return


def _restore_if_attempt(path, attempt, previous):
    """Put ``previous`` back only while ``path`` still holds this attempt's bytes."""
    temporary = None
    try:
        if _snapshot_regular(path) != attempt:
            return
        temporary = _stage_secret(path, previous, private=str(path).endswith(".key"))
        if _snapshot_regular(path) != attempt:
            return
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    finally:
        if temporary is not None:
            _unlink_quiet(temporary)


def _compensate_owned(owned):
    """Undo key and note bytes this attempt still owns. Anything else stays."""
    prior = owned.get("prior") or {}
    for path, attempt in list((owned.get("attempt") or {}).items()):
        if path not in prior or not isinstance(attempt, (bytes, bytearray)):
            continue
        try:
            current = _snapshot_regular(path)
        except (OSError, ValueError):
            continue
        if current != attempt:
            continue
        previous = prior[path]
        if previous is None:
            _drop_if_attempt(path, attempt)
        elif isinstance(previous, (bytes, bytearray)):
            _restore_if_attempt(path, attempt, previous)


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
    """Start `jarvis cluster run` detached. The platform helper owns the OS handoff."""
    import sys
    from .autostart import interpreter
    from .platforms import spawn_detached
    argv = interpreter([sys.executable, "-m", "jarvis", "--config", str(Path(path).resolve()),
                        "--state", str(Path(state).resolve()), "cluster", "run"])
    spawn_detached(argv, state=state)


def _main_id(config):
    """Device id when this enabled fleet says this PC is its main.

    An enabled config whose identity cannot be read is refused. Returning None
    would let join treat that fleet as unset and overwrite it. A disabled
    config, or a readable config where this PC is not the main, returns None.
    """
    if not isinstance(config, dict):
        return None
    try:
        turned_on = enabled(config)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("This PC's cluster is enabled but its identity cannot be read, so the "
                         "config was not changed. Set cluster.local_device to a configured device "
                         "before joining or leaving.") from exc
    if not turned_on:
        return None
    try:
        local, current = identity(config)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("This PC's cluster is enabled but its identity cannot be read, so the "
                         "config was not changed. Set cluster.local_device to a configured device "
                         "before joining or leaving.") from exc
    if local and local == current:
        return local
    return None


def _under_state(state, path):
    """True when this path's own location is inside `state`.

    The last component is not resolved. A symlink cannot make another file look
    inside state, and it cannot hide a path that is already inside state.
    """
    try:
        base = Path(state).resolve()
        candidate = Path(path)
        candidate.parent.resolve().relative_to(base)
    except (OSError, ValueError):
        return False
    name = candidate.name
    return bool(name) and name not in (".", "..")


_TUNNEL_LOCK = "cluster-tunnel.lock"
_LINK_NAMES = (
    "cluster-node.json", "cluster-node.key", "cluster-node-pending.key",
    "cluster-node-launch.vbs", "cluster-tunnel.json", "cluster-tunnel.stop",
    "cluster-tunnel-launch.vbs", "cluster-addresses.json", "cluster-discovered.json", "cluster-scan.json",
)


def _node_link_paths(state):
    """Credential and tunnel files for one joined node, after the worker has stopped.

    The tunnel lock is never listed: its inode stays so a live worker and the next
    worker lock the same file. ``link.identity_file`` is not ownership and is never
    listed. Only the canonical ``cluster-node.key`` and ``cluster-node-pending.key``
    are key files. Stop marker and status are listed for removal only once the
    caller has confirmed the worker is stopped.
    """
    state = Path(state).resolve()
    paths = [state / name for name in _LINK_NAMES]
    paths.append(state / "logs" / "cluster-tunnel.log")
    note = read_json(state / 'cluster-node.json', None)
    link = note.get("link") if isinstance(note, dict) else None
    if isinstance(link, dict):
        alias = "jarvis-" + str(link.get("hostname") or "peer")
        if re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
            paths.append(state / f"known-host-{alias}")
    if state.is_dir():
        paths.extend(state.glob("known-host-jarvis-*"))
        paths.extend(state.glob("cluster-node-launch-*.vbs"))
    unique, seen = [], set()
    for path in paths:
        if path.name == _TUNNEL_LOCK:
            continue
        if not _under_state(state, path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(Path(path))
    return unique


def _forget_node_link(state):
    """Delete this node's link files after the tunnel is confirmed stopped.

    One failure does not keep the rest. No process is killed. The lock file is
    never removed. A symlink is removed as a link; the file it names is not.
    """
    errors = []
    for path in _node_link_paths(state):
        if path.name == _TUNNEL_LOCK:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            continue
        try:
            path.unlink()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return "; ".join(errors)


def _tunnel_lock_free(state):
    """True when no worker holds ``cluster-tunnel.lock``.

    The probe is a nonblocking ``exclusive_lock``. The file is not removed: a
    later worker has to lock the same inode. ``LockBusy`` means the worker is
    still in the lock, even when its status heartbeat has gone stale.
    """
    from .locking import LockBusy, exclusive_lock
    try:
        with exclusive_lock(Path(state) / _TUNNEL_LOCK, timeout=0):
            return True
    except LockBusy:
        return False


def _stop_tunnel(state):
    """Stop the tunnel worker if it is live. Return ``(confirmed_stopped, detail)``.

    ``confirmed_stopped`` is true only when the status heartbeat is idle and this
    process can take ``cluster-tunnel.lock``. A stale heartbeat alone is not
    exit: the worker can still hold the lock. The lock file is not unlinked.
    When the lock is busy the stop marker and status note are left in place.
    ``detail`` is None when the worker was already gone, otherwise the stop result.
    """
    from . import cluster_tunnel
    try:
        running = bool(cluster_tunnel.status(state).get("running"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return False, {"stopped": False, "error": str(exc)}
    try:
        free = _tunnel_lock_free(state)
    except OSError as exc:
        return False, {"stopped": False, "error": str(exc)}
    if not running and free:
        return True, None
    if not running:
        return False, {"stopped": False, "note": "still running"}
    try:
        detail = cluster_tunnel.stop(state)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, {"stopped": False, "error": str(exc)}
    if not isinstance(detail, dict):
        detail = {"stopped": False}
    try:
        running = bool(cluster_tunnel.status(state).get("running"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return False, {**detail, "error": str(exc)}
    try:
        free = _tunnel_lock_free(state)
    except OSError as exc:
        return False, {**detail, "error": str(exc)}
    if running or not free:
        # The worker still owns the lock or the heartbeat. Do not report it
        # stopped, and do not clear the marker ``stop`` just wrote.
        return False, {**detail, "stopped": False, "note": detail.get("note") or "still running"}
    return True, {**detail, "stopped": True}


def join(config, path, state, note, autostart=True, start=True, wait=25):
    """Pair this PC as a node from a decoded invite, then connect and check.

    The role check, the credential files, and the config write are one local
    transaction. The key and the note are staged, then published, and put back
    if the config write does not commit. Only bytes this attempt still owns are
    restored, so a concurrent redemption is not overwritten. The file on disk
    decides the role, so a stale copy cannot turn a main into a node.
    """
    targets, main_host = _validate(note)
    node, main = note["node_id"], note["main_device"]
    state = Path(state).resolve()
    from . import cluster_tunnel
    from .config_lock import patch_config
    key_file = state / "cluster-node.key"
    note_file = state / "cluster-node.json"
    owned = {"prior": {}, "attempt": {}}

    def change(doc):
        if not doc and isinstance(config, dict):
            doc.update(json.loads(json.dumps(config)))
        # An enabled main stays the main. Handoff is `jarvis cluster main`, not join.
        disk_main = _main_id(doc)
        if disk_main:
            raise ValueError(f"This PC is the main ({disk_main}). Run jarvis cluster main {main} "
                             "first if it should become a node.")
        here = _this_device(doc)
        if here and here != node:
            raise ValueError(f"This PC is configured as {here} (hostname {_hostname()}), but the "
                             f"invite is for {node}. Invite {here} instead.")
        devices = dict(doc.get("devices") or {})
        mine = dict(devices.get(node) or {})
        local_device = "auto"
        if not mine.get("hostname"):
            mine["hostname"] = _hostname()
        elif str(mine.get("hostname")).casefold() != _hostname().casefold():
            local_device = node  # the shared config names another hostname; don't rewrite it
        devices[node] = mine
        devices.setdefault(main, {k: v for k, v in (note.get("main") or {}).items()
                                  if k in ("hostname", "label", "aliases")})
        settings = {**(doc.get("cluster") or {}), "enabled": True, "main_device": main,
                    "local_device": local_device, "tunnel": "forward", "listen_host": "127.0.0.1",
                    "listen_port": note["port"], "main_url": f"http://127.0.0.1:{note['port']}"}
        identity({**doc, "devices": devices, "cluster": settings})
        # The previous worker holds the old key. Replace nothing until the lock
        # and the heartbeat both say it has stopped. A timeout must not swap
        # the key, the link, or the config.
        confirmed, detail = _stop_tunnel(state)
        if not confirmed:
            text = ""
            if isinstance(detail, dict):
                text = str(detail.get("error") or detail.get("note") or "")
            if "still running" not in text.lower():
                text = (text + "; " if text else "") + "still running"
            raise ValueError("The cluster tunnel is still running, so this join did not replace "
                             "the SSH key, the saved link, or this PC's config. " + text)
        _publish_owned(key_file, note["key"], note_file, {
            "node_id": node, "main_device": main, "token": note["token"], "redeem": True,
            "code_key": note["code_key"],  # only code signed by this key is installed
            "link": {"targets": targets, "host_key": note["host_key"], "hostname": main_host,
                     "identity_file": str(key_file)}}, owned)
        doc["devices"] = devices
        doc["cluster"] = settings

    try:
        proposed, _ = patch_config(state, path, change, backup="cluster-join", create=True)
    except BaseException:
        _compensate_owned(owned)
        raise
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
    """Forget this node's link: stop the tunnel and login startup, delete the credential.

    One managed mutation reads the file before it decides. This PC being the main
    on that fresh read is always refused, including when the caller already passed
    a main config. The tunnel is stopped with its marker. No pid is signalled.
    """
    from . import cluster_tunnel
    from .cluster_cli import autostart as role_autostart
    from .config_lock import patch_config
    state = Path(state).resolve()
    box = {}

    def change(doc):
        if not doc and isinstance(config, dict):
            doc.update(json.loads(json.dumps(config)))
        disk_main = _main_id(doc)
        if disk_main:
            raise ValueError(f"This PC is the main ({disk_main}). Nodes leave; the main revokes "
                             "them. To hand the fleet to another PC: jarvis cluster main <device>")
        try:
            node = identity(doc)[0]
        except (TypeError, ValueError):
            try:
                node = identity(config)[0]
            except (TypeError, ValueError):
                node = "NODE"
        report = {"left": node}
        try:
            confirmed, detail = _stop_tunnel(state)
        except (OSError, subprocess.SubprocessError) as exc:
            confirmed, detail = False, {"stopped": False, "error": str(exc)}
            report["tunnel_error"] = str(exc)
        if detail is not None:
            report["tunnel"] = detail
        if not confirmed:
            text = ""
            if isinstance(detail, dict):
                text = str(detail.get("error") or detail.get("note") or "")
            if "still running" not in text.lower():
                text = (text + "; " if text else "") + "still running"
            # The worker is live. Keep the marker it watches, the lock inode, the
            # status note, and the credentials. Autostart and the config still go
            # off so a new worker is not started.
            report["tunnel_stop_error"] = text
            report["credentials_retained"] = "retained until the tunnel stops"
        else:
            cleanup = _forget_node_link(state)
            if cleanup:
                report["cleanup_error"] = cleanup
        for name, off in (("tunnel", lambda: cluster_tunnel.autostart(path, state, False)),
                          ("node", lambda: role_autostart(path, state, False))):
            try:
                off()
            except (OSError, subprocess.SubprocessError) as exc:
                report[f"{name}_autostart_error"] = str(exc)
        doc.setdefault("cluster", {})["enabled"] = False
        box["report"] = report

    patch_config(state, path, change, backup="cluster-leave", create=True)
    report = box.get("report") or {"left": "NODE"}
    if report.get("tunnel_stop_error"):
        report["next"] = ("The tunnel is still running. Credentials, the stop marker, the lock, "
                          "and the status note were kept. Autostart is off and the cluster is "
                          "disabled so it will not start again. Stop the tunnel, then leave again.")
    else:
        report["next"] = (f"Restart or stop this PC's node agent. On the main: "
                          f"jarvis cluster revoke {report['left']}")
    return report


# --- doctor -----------------------------------------------------------------------------

def _check(checks, name, ok, detail="", fix=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail, **({"fix": fix} if fix and not ok else {})})
    return bool(ok)


def _known_role(config):
    """``main`` or ``node`` when identity can be read, else None. No network."""
    if not isinstance(config, dict):
        return None
    try:
        local, main = identity(config)
    except (TypeError, ValueError):
        return None
    if not local or not main:
        return None
    return "main" if local == main else "node"


def _tested_release_code(state):
    """Fingerprint of the tested release, or None. Never the live-tree fallback."""
    from .release import current
    try:
        note = current(state)
    except (OSError, ValueError, TypeError):
        return None
    code = note.get("code") if isinstance(note, dict) else None
    return code if isinstance(code, str) and code else None


def _readiness_report(config, state, role, status):
    """Local readiness. The expected fingerprint is a tested release or the hub's status code."""
    from .fleet.readiness import main_readiness
    if role == "main":
        expected = _tested_release_code(state)
    elif role == "node" and isinstance(status, dict):
        code = status.get("code")
        expected = code if isinstance(code, str) and code else None
    else:
        expected = None
    return main_readiness(config if isinstance(config, dict) else {}, state, expected_code=expected)


def doctor(config, path, state, fix=False):
    """Every link in the chain from config to a working node, in order, with a fix each.

    ``main_readiness`` is the local main_readiness report on every result, including a
    disabled cluster or an identity error. It does not change ``ok``. On a main the
    expected fingerprint is ``release.current`` only. On a node it is the code from
    the authenticated ``/v1/status`` already fetched for the link checks. ``served_code``
    live-tree fallback is not a tested release.
    """
    from . import cluster_tunnel
    from .deploy import code_fingerprint
    checks = []
    role = None
    status = None
    if not enabled(config):
        _check(checks, "cluster enabled", False, "cluster.enabled is false",
               "On the main: jarvis cluster init. On a node: jarvis cluster join CODE")
        report = {"ok": False, "checks": checks}
    else:
        try:
            local, main = identity(config)
        except ValueError as exc:
            _check(checks, "this PC's identity", False, str(exc),
                   f"Add this PC (hostname {_hostname()}) to devices, or set cluster.local_device")
            report = {"ok": False, "checks": checks}
        else:
            role = "main" if local == main else "node"
            _check(checks, "this PC's identity", True, f"{local} ({role}); main is {main}")
            forward = cluster_tunnel.mode(config) == "forward"
            if role == "main":
                _doctor_main(config, state, main, forward, checks, _tested_release_code(state))
            else:
                status = _doctor_node(config, state, local, main, forward, checks, code_fingerprint(),
                                      fix=fix, config_path=path)
            report = {"ok": all(c["ok"] for c in checks), "role": role, "checks": checks}
    source = role if role is not None else _known_role(config)
    report["main_readiness"] = _readiness_report(config, state, source, status)
    return report


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
            if not code:
                _check(checks, f"{device}: same code", False, "no tested release on this main",
                       "On this main: jarvis cluster release   (a live tree is not a tested release)")
            else:
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


def _doctor_node(config, state, local, main, forward, checks, code, fix=False, config_path=None):
    """Append node checks. Return the /v1/status object when that call succeeded."""
    from . import cluster_tunnel
    from .transfer import _ssh_args, ssh_host
    apply_fix = fix  # `fix` is reused below for a hint string
    status = None
    try:
        Client(config, state)
        _check(checks, "paired with the main", True, f"credential for {local}")
    except ValueError as exc:
        _check(checks, "paired with the main", False, str(exc),
               f"On {main}: jarvis cluster invite {local}; here: jarvis cluster join CODE")
        return None
    if forward:
        try:
            machine = cluster_tunnel.peer(config, state)
        except ValueError as exc:
            _check(checks, "SSH link", False, str(exc), f"Join again with a new invite from {main}")
            return None
        binary = shutil.which("ssh")
        if not _check(checks, "OpenSSH client", binary, binary or "ssh not found",
                      "Install the OpenSSH client (Windows: Settings > Optional features)"):
            return None
        key = machine.get("identity_file")
        if key and not _check(checks, "SSH key file", Path(key).expanduser().is_file(), key,
                              f"Join again with a new invite from {main}"):
            return None
        targets = cluster_tunnel.candidates(config, state)
        from .cluster_recovery import tailscale_status
        ts = tailscale_status()
        if ts is not None:
            _check(checks, "Tailscale connected", ts.get("BackendState") == "Running",
                   "connected" if ts.get("BackendState") == "Running" else "tailscale installed but not connected",
                   "Open Tailscale, connect, and enable Run unattended on Windows")
        reachable = [t for t in targets if cluster_tunnel.reachable(t)]
        if not _check(checks, f"{main} reachable", reachable,
                      ", ".join(reachable) if reachable else "no answer on port 22 at " + ", ".join(targets),
                      f"Is {main} awake and on this network (or Tailscale)? If its address "
                      f"changed: on {main}, jarvis cluster invite {local}, then join again"):
            return None
        try:
            argv = [*_ssh_args(binary, key), *cluster_tunnel.pinned_host(machine, state), "-T",
                    *ssh_host(reachable[0])]
            out = subprocess.run(argv, capture_output=True, text=True, timeout=20, creationflags=FLAGS)
            ok = out.returncode == 0 and "jarvis-node-link" in out.stdout
            detail = reachable[0] if ok else (out.stderr.strip()[-200:] or f"ssh exited {out.returncode}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            ok, detail = False, str(exc)
        if "REMOTE HOST IDENTIFICATION" in detail or "Host key verification" in detail:
            fix = f"{main}'s SSH host key changed; make a new invite there and join again"
        else:
            fix = f"On {main}: jarvis cluster invite {local}; here: jarvis cluster join CODE"
        _check(checks, "SSH login", ok, detail, fix)
        tunnel = cluster_tunnel.status(state)
        from .cluster_recovery import SUPERVISOR_WITHIN, ensure_tunnel, ensure_dependents
        # Same predicate as ensure_tunnel. While it holds, the node supervisor and
        # `doctor --fix` leave the worker down; `jarvis cluster tunnel start` clears it.
        operator_stop = cluster_tunnel.manual_stop_holds(config, state)
        stopped_detail = "tunnel stopped by the operator; the supervisor will not restart it"
        start_fix = "jarvis cluster tunnel start"
        if ok and not tunnel.get("hub_healthy"):
            # The main answers but the forwarded hub does not. Promise a restart only
            # when no operator stop holds for this boot and role.
            acted = False
            if apply_fix and config_path is not None:
                try:
                    acted = ensure_tunnel(config, state, config_path)
                    ensure_dependents(config, state)
                except (OSError, ValueError, RuntimeError) as exc:
                    detail_fix = f"--fix failed: {exc}"
                else:
                    if operator_stop:
                        detail_fix = f"--fix left the operator stop in place; {start_fix}"
                    else:
                        detail_fix = ("--fix started the tunnel worker" if acted
                                      else "--fix: worker already supervised")
            elif operator_stop:
                detail_fix = start_fix
            else:
                detail_fix = "jarvis cluster doctor --fix starts it now"
            supervisor_detail = (stopped_detail if operator_stop else
                                 f"reachable but tunnel down: the supervisor fixes it within {SUPERVISOR_WITHIN} s")
            _check(checks, "tunnel supervisor", False, supervisor_detail, detail_fix)
        if operator_stop and not tunnel.get("ssh_alive"):
            running_detail = stopped_detail
            running_fix = start_fix
        else:
            running_detail = tunnel.get("error") or tunnel.get("target") or ""
            running_fix = (
                "The node agent supervisor checks within 5 s and restarts a dead worker within 15 s; unhealthy SSH sessions rotate within 20 s per candidate, with backoff capped at 30 s. "
                "If the agent is stopped: jarvis cluster run")
        _check(checks, "tunnel running", tunnel.get("ssh_alive"), running_detail, running_fix)
    try:
        status = Client(config, state).request("/v1/status", timeout=3)
        _check(checks, "hub answers", True, (config.get("cluster") or {}).get("main_url", ""))
    except (OSError, ValueError, RuntimeError) as exc:
        _check(checks, "hub answers", False, str(exc),
               f"Is JARVIS running on {main}? There: jarvis cluster doctor")
        return None
    _check(checks, "node agent running", _agent_running(state), "jarvis cluster run",
           "jarvis cluster autostart on")
    stats = read_json(Path(state) / 'cluster-link-stats.json', None)
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
    return status


def role_autostart(config_path, state, enable, *, restart=False):
    """Existing cluster unit/Startup helper, with explicit role-switch activation."""
    from .cluster_cli import autostart
    return autostart(config_path, state, enable, **({'restart': True} if restart else {}))
