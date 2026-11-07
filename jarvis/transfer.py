"""Move files and images between the laptops, over the cluster link or plain SSH.

Lan Mouse shares the mouse and keyboard, not drag-and-drop sessions: a drag begun in a
Windows app can't continue into a Wayland app, because each OS keeps its drag data
in-process. This is the equivalent that does work: drop a file on the console (or run
`jarvis send`) and it lands in ~/JARVIS-Drop on the other laptop; `jarvis fetch` pulls
one back to ~/Downloads/JARVIS-Drop. Only the fixed drop folders are touched.

With the cluster on, files ride the hub link in CHUNK-sized `transfer` jobs: the main
dispatches them to a node, and a node hands them to the main (its "files" capability
allows that one kind). The receiving side writes a hidden .part file and gives it a
free name only once the last chunk is in, so a broken transfer never shows up as a
truncated file. Clipboard sync (clipsync) sends its copies the same way. Without the
cluster, or when a node is offline and the peer has an SSH target, the SSH path below
(same key, BatchMode and strict host-key checking as the inventory probes) is used.
"""

import base64
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath

REMOTE_DIR = "JARVIS-Drop"
MAX_BYTES = 2 * 1024 ** 3
CLIP_BYTES = 32 * 1024 * 1024  # clipsync.MAX_BYTES; a clipboard copy never needs more
CHUNK = 2 * 1024 * 1024  # raw bytes per cluster job; base64 keeps it under the bulk cap
UPLOAD_ID = re.compile(r"[a-f0-9]{32}")
PART = ".jarvis-upload-{}.part"
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. ()+,@-]{0,199}")


def ssh_device(config, name=None):
    """The configured laptop reached over SSH (the named one, else the first)."""
    devices = {k: v for k, v in config.get("devices", {}).items() if v.get("transport") == "ssh"}
    from .topology import enabled, identity
    if enabled(config):
        local, _ = identity(config)
        devices.pop(local, None)
    if name:
        if name not in devices:
            raise ValueError(f"{name!r} is not a configured SSH device.")
        return devices[name]
    if not devices:
        raise ValueError("No SSH device is configured.")
    return next(iter(devices.values()))


def safe_name(name):
    """A plain file name: no folders, no leading dash or dot, no shell-significant characters."""
    # Parse separators without Windows drive-prefix semantics: a Linux filename
    # such as a:b.txt must become a_b.txt, not silently lose the a: prefix.
    name = PurePosixPath(str(name or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[^A-Za-z0-9_. ()+,@-]", "_", name).lstrip(".- ") or "file"
    name = name[:200].rstrip(". ") or "file"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "_" + name
    if not re.match(r"[A-Za-z0-9_]", name):  # NAME (inbox/get) needs one of these first
        name = "_" + name
    return name[:200]


def collision_name(name, number, directory=False, max_bytes=255):
    """Add a suffix within the filesystem or drop-folder filename budget."""
    suffix = "" if directory else Path(name).suffix
    stem = name if directory else Path(name).stem
    marker = f" ({number})"
    # Keep some stem even for an unusually long extension. Never truncate the
    # numeric marker: distinct collision candidates must remain distinct.
    suffix = suffix.encode("utf-8")[:max_bytes - 1 - len(marker)].decode("utf-8", errors="ignore")
    budget = max_bytes - len(marker) - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore") or "_"
    return stem + marker + suffix


def _occupied(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _ssh_args(binary, identity_file):
    args = [binary, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=8"]
    if identity_file:
        args += ["-i", str(Path(identity_file).expanduser()), "-o", "IdentitiesOnly=yes"]
    return args


def _tools():
    ssh, scp = shutil.which("ssh"), shutil.which("scp")
    if not ssh or not scp:
        raise ValueError("The OpenSSH client (ssh and scp) is not installed.")
    return ssh, scp


def _check_target(target):
    if not isinstance(target, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", target):
        raise ValueError("Use user@hostname or user@IPv4 for the configured SSH target.")


def _run(args, timeout, **kw):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, shell=False, **kw)
    except subprocess.TimeoutExpired:
        raise RuntimeError("The other laptop didn't respond in time.") from None


def _fail(action, result):
    tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no detail"]
    raise RuntimeError(f"{action} failed: {tail[0][:200]}")


def send(target, identity_file, local_path, name=None):
    """Copy one local file into the other laptop's drop folder; returns what landed."""
    _check_target(target)
    path = Path(local_path)
    if not path.is_file():
        raise ValueError("That isn't a file.")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"Files over {MAX_BYTES // 1024 ** 3} GB aren't sent this way.")
    name = safe_name(name or path.name)
    ssh, scp = _tools()
    made = _run([*_ssh_args(ssh, identity_file), target, f"mkdir -p -- {REMOTE_DIR}"], 20)
    if made.returncode:
        _fail("Preparing the drop folder", made)
    # scp to a name that exists would overwrite it; pick a free one instead.
    taken = set(inbox(target, identity_file, include_directories=True))
    final, n = name, 1
    while final in taken:
        n += 1
        final = collision_name(name, n, max_bytes=200)
    done = _run([scp, "-q", *_ssh_args(scp, identity_file)[1:], "--", str(path),
                 f"{target}:{REMOTE_DIR}/{final}"], max(60, size // (1024 * 1024)))
    if done.returncode:
        _fail("Sending the file", done)
    return {"sent": final, "bytes": size, "to": f"{target}:~/{REMOTE_DIR}/{final}"}


def inbox(target, identity_file, include_directories=False):
    """File names waiting in the other laptop's drop folder."""
    _check_target(target)
    ssh, _ = _tools()
    out = _run([*_ssh_args(ssh, identity_file), target,
                f"mkdir -p -- {REMOTE_DIR} && ls -1p -- {REMOTE_DIR}"], 20)
    if out.returncode:
        _fail("Listing the drop folder", out)
    return [line.rstrip("/") for line in out.stdout.splitlines()
            if NAME.fullmatch(line.rstrip("/"))
            and (include_directories or not line.endswith("/"))]


def fetch(target, identity_file, name, dest=None):
    """Pull one file from the other laptop's drop folder into ~/Downloads/JARVIS-Drop."""
    _check_target(target)
    if name not in inbox(target, identity_file):
        raise ValueError(f"No file called {name!r} in the drop folder.")
    _, scp = _tools()
    folder = Path(dest) if dest else Path.home() / "Downloads" / REMOTE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    local_name = safe_name(name)
    target_path = folder / local_name
    n = 1
    while _occupied(target_path):
        n += 1
        target_path = folder / collision_name(local_name, n)
    done = _run([scp, "-q", *_ssh_args(scp, identity_file)[1:], "--",
                 f"{target}:{REMOTE_DIR}/{name}", str(target_path)], 600)
    if done.returncode:
        _fail("Fetching the file", done)
    return {"fetched": target_path.name, "path": str(target_path)}


# ---- over the cluster link -------------------------------------------------------------
def drop_folder():
    folder = Path.home() / REMOTE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def validate(payload):
    """Refuse anything but the fixed transfer operations (cluster.validate_action)."""
    action = payload.get("action")
    if action == "inbox":
        return
    if action == "get":
        if not isinstance(payload.get("name"), str) or not NAME.fullmatch(payload["name"]):
            raise ValueError("Invalid drop-folder name.")
        offset = payload.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MAX_BYTES:
            raise ValueError("Invalid transfer offset.")
        return
    if action != "put":
        raise ValueError("Unknown transfer action.")
    if not isinstance(payload.get("upload"), str) or not UPLOAD_ID.fullmatch(payload["upload"]):
        raise ValueError("Invalid upload id.")
    offset, data = payload.get("offset"), payload.get("data")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MAX_BYTES:
        raise ValueError("Invalid transfer offset.")
    if not isinstance(data, str) or len(data) > (CHUNK * 4) // 3 + 4:
        raise ValueError("Invalid transfer chunk.")
    if payload.get("dest", "drop") == "drop":
        if not isinstance(payload.get("name"), str) or not 0 < len(payload["name"]) <= 255:
            raise ValueError("Invalid file name.")
    elif payload.get("dest") != "clipboard" or payload.get("kind") not in ("text", "png"):
        raise ValueError("Invalid transfer destination.")


def _sweep(folder, hours=24):
    """Remove upload parts abandoned by a transfer that never finished."""
    for part in folder.glob(PART.format("*")):
        try:
            if time.time() - part.stat().st_mtime > hours * 3600:
                part.unlink()
        except OSError:
            pass


def _land(part, folder, name):
    """Give a finished part a free name in the drop folder, never replacing a file."""
    final, n = name, 1
    while True:
        try:
            os.link(part, folder / final)  # fails on an existing name, unlike rename on POSIX
        except FileExistsError:
            n += 1
            final = collision_name(name, n, max_bytes=200)
            continue
        except OSError:  # no hard links (FAT, some shares): fall back to a checked rename
            if _occupied(folder / final):
                n += 1
                final = collision_name(name, n, max_bytes=200)
                continue
            os.rename(part, folder / final)
            return final
        part.unlink()
        return final


def run_here(payload, state=None):
    """One transfer operation on this machine (a cluster job, or the main serving a node)."""
    validate(payload)
    folder = drop_folder()
    action = payload["action"]
    if action == "inbox":
        entries = []
        with os.scandir(folder) as found:
            for entry in found:
                if NAME.fullmatch(entry.name):
                    entries.append({"name": entry.name, "dir": entry.is_dir()})
        return {"entries": sorted(entries, key=lambda e: e["name"].lower())}
    if action == "get":
        path = folder / payload["name"]
        if path.parent != folder or not path.is_file():
            raise ValueError(f"No file called {payload['name']!r} in the drop folder.")
        size = path.stat().st_size
        with open(path, "rb") as stream:
            stream.seek(payload["offset"])
            data = stream.read(CHUNK)
        return {"data": base64.b64encode(data).decode(), "size": size,
                "eof": payload["offset"] + len(data) >= size}
    data = base64.b64decode(payload["data"], validate=True)
    part = folder / PART.format(payload["upload"])
    if payload["offset"] == 0:
        _sweep(folder)
        mode = "xb"
    else:
        mode = "ab"
        try:
            have = part.stat().st_size
        except FileNotFoundError:
            raise ValueError("That upload isn't in progress here; send the file again.") from None
        if have != payload["offset"]:
            part.unlink(missing_ok=True)
            raise ValueError("A chunk of that upload went missing; send the file again.")
    if payload["offset"] + len(data) > (MAX_BYTES if payload.get("dest", "drop") == "drop"
                                         else CLIP_BYTES):
        part.unlink(missing_ok=True)
        raise ValueError("That is too large to send this way.")
    with open(part, mode) as stream:
        stream.write(data)
    size = payload["offset"] + len(data)
    if not payload.get("last"):
        return {"received": size}
    if payload.get("dest") == "clipboard":
        try:
            content = part.read_bytes()
        finally:
            part.unlink(missing_ok=True)
        from .clipsync import apply
        if apply(state, payload["kind"], content) is False:  # apart / remote control on here
            return {"pasted": None, "paused": True, "bytes": size}
        return {"pasted": payload["kind"], "bytes": size}
    final = _land(part, folder, safe_name(payload["name"]))
    return {"sent": final, "bytes": size}



def clustered(config):
    from .topology import enabled
    return enabled(config)


def peers(config):
    """The devices this machine can send to: the main sends to any node, a node to the main."""
    from .topology import identity
    local, main = identity(config)
    if local != main:
        return [main]
    return [name for name in config.get("devices", {}) if name != local]


def peer(config, name=None):
    """The device a send/fetch/inbox means (the named one, else the only other one)."""
    if not clustered(config):
        return None
    choices = peers(config)
    if name:
        if name not in config.get("devices", {}):
            raise ValueError(f"{name!r} is not a configured device.")
        if name not in choices:
            from .topology import identity
            local, main = identity(config)
            raise ValueError("That is this computer." if name == local else
                             f"A node sends files only to the main PC ({main}).")
        return name
    if not choices:
        raise ValueError("No other computer is configured.")
    return choices[0]


def call(config, state, device, payload, timeout=120):
    """Run one transfer operation on `device` over the cluster link."""
    from .topology import identity
    local, main = identity(config)
    if local == main:
        from .cluster import dispatch
        return dispatch(config, state, device, "transfer", payload, timeout=timeout)
    from .cluster import Client
    return Client(config, state).request("/v1/control", {
        "node": main, "kind": "transfer", "payload": payload,
        "timeout": max(1, timeout - 5)}, timeout=timeout)


def push(config, state, device, source, size, name=None, dest="drop", kind=None):
    """Stream `source` (a binary file object of `size` bytes) to `device` in chunks."""
    upload, offset, result = uuid.uuid4().hex, 0, None
    extra = {"name": name} if dest == "drop" else {"dest": "clipboard", "kind": kind}
    while True:
        data = source.read(CHUNK)
        last = offset + len(data) >= size or len(data) < CHUNK
        result = call(config, state, device, {
            "action": "put", "upload": upload, "offset": offset, "last": last,
            "data": base64.b64encode(data).decode(), **extra})
        offset += len(data)
        if last:
            return result


def _ssh_fallback(config, device):
    """The peer's SSH entry when the cluster can't reach it, or None."""
    entry = config.get("devices", {}).get(device) or {}
    return entry if entry.get("transport") == "ssh" and entry.get("target") else None


def send_file(config, state, path, device=None, name=None):
    """`jarvis send`: one local file into the other laptop's drop folder."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("That isn't a file.")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"Files over {MAX_BYTES // 1024 ** 3} GB aren't sent this way.")
    target = peer(config, device)
    if target is None:
        entry = ssh_device(config, device)
        return send(entry.get("target"), entry.get("identity_file"), path, name)
    try:
        with open(path, "rb") as source:
            result = push(config, state, target, source, size, safe_name(name or path.name))
    except ConnectionError:
        entry = _ssh_fallback(config, target)
        if not entry:
            raise
        return send(entry.get("target"), entry.get("identity_file"), path, name)
    return {**result, "to": f"{target}:~/{REMOTE_DIR}/{result['sent']}"}


def list_inbox(config, state, device=None):
    """`jarvis inbox`: file names waiting in the other laptop's drop folder."""
    target = peer(config, device)
    if target is None:
        entry = ssh_device(config, device)
        return inbox(entry.get("target"), entry.get("identity_file"))
    try:
        entries = call(config, state, target, {"action": "inbox"}, timeout=45)["entries"]
    except ConnectionError:
        entry = _ssh_fallback(config, target)
        if not entry:
            raise
        return inbox(entry.get("target"), entry.get("identity_file"))
    return [e["name"] for e in entries if not e.get("dir")]


def fetch_file(config, state, name, device=None, dest=None):
    """`jarvis fetch`: pull one file from the other laptop's drop folder."""
    target = peer(config, device)
    if target is None:
        entry = ssh_device(config, device)
        return fetch(entry.get("target"), entry.get("identity_file"), name, dest)
    if not NAME.fullmatch(str(name)):
        raise ValueError(f"No file called {name!r} in the drop folder.")
    try:
        first = call(config, state, target, {"action": "get", "name": name, "offset": 0})
    except ConnectionError:
        entry = _ssh_fallback(config, target)
        if not entry:
            raise
        return fetch(entry.get("target"), entry.get("identity_file"), name, dest)
    folder = Path(dest) if dest else Path.home() / "Downloads" / REMOTE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / PART.format(uuid.uuid4().hex)
    try:
        with open(part, "xb") as out:
            chunk = first
            while True:
                data = base64.b64decode(chunk["data"], validate=True)
                out.write(data)
                if chunk["eof"] or not data:
                    break
                chunk = call(config, state, target, {"action": "get", "name": name,
                                                     "offset": out.tell()})
        final = _land(part, folder, safe_name(name))
    finally:
        part.unlink(missing_ok=True)
    return {"fetched": final, "path": str(folder / final), "bytes": (folder / final).stat().st_size}
