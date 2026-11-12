"""Move files and images between the laptops, over the cluster link or plain SSH.

Lan Mouse shares the mouse and keyboard, not drag-and-drop sessions: a drag begun in a
Windows app can't continue into a Wayland app, because each OS keeps its drag data
in-process. This is the equivalent that does work: drop a file on the console (or run
`jarvis send`) and it lands in ~/JARVIS-Drop on the other laptop; `jarvis fetch` pulls
one back to ~/Downloads/JARVIS-Drop. Only the fixed drop folders are touched.

The console's storage browser (storage.py) also copies files and folders to or from a node
the hub is the only way to reach. Those jobs are the "path" operations (`tree`, `mkdir`, `get`
with a path, `put` with dest "path"): the receiving or serving computer checks every path
against its own storage roots (Storage._local), names are single components, and only the
main's console can send them (a node may not use them against the main).

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
import sys
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
RESERVE = 2 * 1024 ** 3  # an upload may never leave less than this free in the drop folder
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. ()+,@-]{0,199}")
MAX_TREE = 3000  # files plus folders in one folder copy
MAX_DEPTH = 32  # folder nesting in one folder copy
MAX_COLLISIONS = 200


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


def desktop_folder():
    """Resolve the receiving user's Desktop, including Windows folder redirection."""
    if sys.platform == "win32":
        import ctypes
        # CSIDL_DESKTOPDIRECTORY: shell resolves OneDrive/redirected Desktops too.
        path = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, path)
        if result != 0 or not path.value:
            raise OSError("Cannot locate the Windows Desktop; no file was delivered.")
        folder = Path(path.value)
    else:
        folder = Path.home() / "Desktop"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def plain_name(name):
    """One path component: no separators, no dot names, no NUL, at most 255 characters."""
    return (isinstance(name, str) and 0 < len(name) <= 255 and name not in (".", "..")
            and not re.search(r"[/\\\x00]", name))


def _plain_path(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and "\x00" not in value


def is_path_op(payload):
    """True for the operations that name paths on the receiving computer (storage copies)."""
    action = payload.get("action")
    return (action in ("tree", "mkdir") or (action == "get" and "path" in payload)
            or (action == "put" and payload.get("dest") == "path"))


def validate(payload):
    """Refuse anything but the fixed transfer operations (cluster.validate_action)."""
    action = payload.get("action")
    if action == "inbox":
        return
    if action == "tree":
        if not _plain_path(payload.get("path")):
            raise ValueError("Invalid folder path.")
        return
    if action == "mkdir":
        if not _plain_path(payload.get("dir")) or not plain_name(payload.get("name")):
            raise ValueError("Invalid folder name.")
        return
    if action == "get":
        if "path" in payload:
            rel = payload.get("rel", [])
            if (not _plain_path(payload["path"]) or "name" in payload or not isinstance(rel, list)
                    or len(rel) > MAX_DEPTH or not all(plain_name(part) for part in rel)):
                raise ValueError("Invalid file path.")
        elif not isinstance(payload.get("name"), str) or not NAME.fullmatch(payload["name"]):
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
    declared = payload.get("size")
    if declared is not None and (not isinstance(declared, int) or isinstance(declared, bool)
                                 or not 0 <= declared <= MAX_BYTES):
        raise ValueError("Invalid declared size.")
    if payload.get("dest", "drop") in ("drop", "desktop"):
        if not isinstance(payload.get("name"), str) or not 0 < len(payload["name"]) <= 255:
            raise ValueError("Invalid file name.")
    elif payload.get("dest") == "path":
        if not _plain_path(payload.get("dir")) or not plain_name(payload.get("name")):
            raise ValueError("Invalid file name or folder.")
    elif payload.get("dest") != "clipboard" or payload.get("kind") not in ("text", "png", "files"):
        raise ValueError("Invalid transfer destination.")


def _sweep(folder, hours=24):
    """Remove upload parts abandoned by a transfer that never finished."""
    for part in folder.glob(PART.format("*")):
        try:
            if time.time() - part.stat().st_mtime > hours * 3600:
                part.unlink()
        except OSError:
            pass


def _check_space(folder, incoming, declared=0):
    """Refuse an upload that would leave under RESERVE free (or under declared size + RESERVE)."""
    free = shutil.disk_usage(folder).free
    if free - max(incoming, declared) < RESERVE:
        raise RuntimeError(
            f"Not enough free space here: {free // 1024 ** 2} MB free, "
            f"{max(incoming, declared) // 1024 ** 2} MB incoming, and {RESERVE // 1024 ** 3} GB "
            "must stay free. Nothing was received.")


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


def _within(config, path):
    """Resolve `path` on this computer, only if it is inside this computer's storage roots."""
    if config is None:
        raise ValueError("This computer can't check its storage folders, so it won't copy there.")
    from .storage import Storage
    from .topology import identity
    return Storage(config)._local(identity(config)[0], path, "resolve")


def make_dir(parent, name):
    """Create a new folder `name` in `parent`, taking `name (2)` and so on if it is taken."""
    base = safe_name(name)
    final, n = base, 1
    while True:
        try:
            os.mkdir(Path(parent) / final)
            return final
        except FileExistsError:
            n += 1
            if n > MAX_COLLISIONS:
                raise ValueError(f"{base} and {MAX_COLLISIONS} numbered copies of it already "
                                 "exist there; tidy up or pick another folder.") from None
            final = collision_name(base, n, directory=True, max_bytes=200)


def walk_tree(root):
    """Folders and files under `root` as lists of names, parents first.

    Links and special files are skipped, never followed, so nothing outside the folder is read.
    """
    dirs, files, state = [], [], {"skipped": 0, "bytes": 0}

    def scan(path, rel):
        if len(rel) >= MAX_DEPTH:
            raise ValueError("That folder is nested too deeply to copy this way.")
        try:
            with os.scandir(path) as found:
                entries = sorted(found, key=lambda e: e.name)
        except PermissionError:
            raise ValueError("Permission denied reading a folder inside it.") from None
        for entry in entries:
            if (not plain_name(entry.name) or entry.is_symlink()
                    or getattr(entry, "is_junction", lambda: False)()):
                state["skipped"] += 1
            elif entry.is_dir(follow_symlinks=False):
                dirs.append([*rel, entry.name])
                if len(dirs) + len(files) > MAX_TREE:
                    break
                scan(entry.path, [*rel, entry.name])
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                if size > MAX_BYTES:
                    raise ValueError(f"{entry.name} is over {MAX_BYTES // 1024 ** 3} GB, which "
                                     "isn't copied this way.")
                files.append({"rel": [*rel, entry.name], "size": size})
                state["bytes"] += size
            else:
                state["skipped"] += 1
            if len(dirs) + len(files) > MAX_TREE:
                break
        if len(dirs) + len(files) > MAX_TREE:
            raise ValueError(f"That folder has more than {MAX_TREE} items; copy it in smaller "
                             "pieces or zip it first.")
        if state["bytes"] > MAX_BYTES:
            raise ValueError(f"That folder is over {MAX_BYTES // 1024 ** 3} GB in total; copy it "
                             "in smaller pieces.")

    scan(str(root), [])
    return {"dirs": dirs, "files": files, "skipped": state["skipped"], "bytes": state["bytes"]}


def check_tree(tree):
    """Refuse a folder listing from the other computer that is not the plain shape walk_tree makes."""
    try:
        dirs, files = tree["dirs"], tree["files"]
        ok = (isinstance(dirs, list) and isinstance(files, list)
              and len(dirs) + len(files) <= MAX_TREE
              and all(isinstance(rel, list) and 0 < len(rel) <= MAX_DEPTH
                      and all(plain_name(part) for part in rel) for rel in dirs)
              and all(isinstance(f["rel"], list) and 0 < len(f["rel"]) <= MAX_DEPTH
                      and all(plain_name(part) for part in f["rel"])
                      and isinstance(f["size"], int) and not isinstance(f["size"], bool)
                      and 0 <= f["size"] <= MAX_BYTES for f in files))
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise ValueError("The other computer sent an unreadable folder listing.")
    return tree


def _path_op(payload, config):
    """The path operations that don't write an upload: `tree`, `mkdir`, `get` by path."""
    action = payload["action"]
    if action == "tree":
        base = _within(config, payload["path"])
        if not base["dir"]:
            raise ValueError("Not a folder.")
        return walk_tree(base["path"])
    if action == "mkdir":
        parent = _within(config, payload["dir"])
        if not parent["dir"]:
            raise ValueError("The destination must be a folder.")
        final = make_dir(parent["path"], payload["name"])
        return {"path": str(Path(parent["path"]) / final), "as": final}
    base = _within(config, payload["path"])
    rel = payload.get("rel") or []
    target = base
    if rel:
        if not base["dir"]:
            raise ValueError("Not a folder.")
        target = _within(config, str(Path(base["path"]).joinpath(*rel)))
        if Path(base["path"]) not in Path(target["path"]).parents:
            raise ValueError("That file is outside the folder being copied.")
    if target["dir"] or not Path(target["path"]).is_file():
        raise ValueError("That is not a file.")
    size = target["size"]
    if size > MAX_BYTES:
        raise ValueError(f"Files over {MAX_BYTES // 1024 ** 3} GB aren't copied this way.")
    with open(target["path"], "rb") as stream:
        stream.seek(payload["offset"])
        data = stream.read(CHUNK)
    return {"data": base64.b64encode(data).decode(), "size": size,
            "eof": payload["offset"] + len(data) >= size}


def run_here(payload, state=None, config=None):
    """One transfer operation on this machine (a cluster job, or the main serving a node)."""
    validate(payload)
    if is_path_op(payload) and payload["action"] != "put":
        return _path_op(payload, config)
    if payload["action"] == "put" and payload.get("dest") == "path":
        where = _within(config, payload["dir"])
        if not where["dir"]:
            raise ValueError("The destination must be a folder.")
        folder = Path(where["path"])
    else:
        folder = (desktop_folder() if payload["action"] == "put" and payload.get("dest") == "desktop"
                  else drop_folder())
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
        _check_space(folder, len(data), payload.get("size") or 0)
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
        try:
            _check_space(folder, len(data))
        except RuntimeError:
            part.unlink(missing_ok=True)  # the disk filled up mid-upload: give the space back
            raise
    if payload["offset"] + len(data) > (MAX_BYTES if payload.get("dest", "drop") in ("drop", "desktop", "path")
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


def push(config, state, device, source, size, name=None, dest="drop", kind=None, folder=None):
    """Stream `source` (a binary file object of `size` bytes) to `device` in chunks.

    dest "path" lands the file in `folder`, a folder on that device inside its storage roots.
    """
    upload, offset, result = uuid.uuid4().hex, 0, None
    if dest not in ("drop", "desktop", "clipboard", "path"):
        raise ValueError("Invalid transfer destination.")
    extra = ({"dest": dest, "name": name, "size": size} if dest in ("drop", "desktop")
             else {"dest": "path", "name": name, "size": size, "dir": folder} if dest == "path"
             else {"dest": "clipboard", "kind": kind})
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


def pull_path(config, state, device, path, folder, name, rel=None):
    """Stream one file on `device` (`path`, or `rel` inside the folder `path`) into local `folder`.

    Returns the name it landed under; an existing file is never replaced.
    """
    folder = Path(folder)
    extra = {"rel": rel} if rel else {}
    part = folder / PART.format(uuid.uuid4().hex)
    try:
        with open(part, "xb") as out:
            while True:
                chunk = call(config, state, device, {"action": "get", "path": path,
                                                     "offset": out.tell(), **extra})
                if out.tell() == 0:
                    _check_space(folder, chunk["size"])
                data = base64.b64decode(chunk["data"], validate=True)
                out.write(data)
                if chunk["eof"]:
                    break
                if not data:
                    raise RuntimeError("The file changed while it was being copied; try again.")
        return _land(part, folder, safe_name(name))
    finally:
        part.unlink(missing_ok=True)
