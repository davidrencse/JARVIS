"""Browse and copy between the storage of both laptops from the JARVIS console.

Each configured device has a set of *roots* (default: its home folder; widen them with
`"storage": {"<device>": {"roots": ["~", "D:\\\\"]}}` in jarvis.config.json). Nothing
outside the roots can be listed or written, after symlinks are resolved. The Dell is read
directly; the Zenbook is reached with the same key-only, strict-host-key SSH the other
probes use, through one fixed read-only listing script and `scp`. Copies go in either
direction and choose unused names (a taken name becomes `name (2)`). Selection and
scp are separate operations: concurrent destination writers still need coordination.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import transfer
from .transfer import _check_target, _fail, _ssh_args, collision_name, safe_name

MAX_ENTRIES = 3000
MAX_COLLISIONS = 200  # each probe is an SSH round trip, so don't hunt for a free name forever

# Runs on the remote host. REQUEST is spliced in as a JSON string literal, so no remote
# path or root is ever interpreted by a shell.
REMOTE_SCRIPT = r'''
import json, os
req = json.loads(REQUEST)
roots = [os.path.realpath(os.path.expanduser(r)) for r in req["roots"]]
def inside(p):
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)
def out(value):
    print(json.dumps(value))
if req["action"] == "exists":
    raw = os.path.abspath(os.path.expanduser(req["path"]))
    parent = os.path.realpath(os.path.dirname(raw))
    if not inside(parent):
        out({"error": "That folder is outside the allowed locations."}); raise SystemExit
    candidate = os.path.join(parent, os.path.basename(raw))
    try:
        os.lstat(candidate)  # even a broken symlink occupies the destination name
        exists = True
    except FileNotFoundError:
        exists = False
    out({"exists": exists}); raise SystemExit
path = os.path.realpath(os.path.expanduser(req["path"] or roots[0]))
if not inside(path):
    out({"error": "That folder is outside the allowed locations."}); raise SystemExit
if not os.path.exists(path):
    out({"error": "No such file or folder."}); raise SystemExit
info = os.stat(path)
res = {"path": path, "dir": os.path.isdir(path), "size": info.st_size, "roots": roots,
       "sep": os.sep}
if req["action"] == "thumbnail":
    try:
        out(thumbnail_payload(path))
    except Exception:
        out({"error": "Image preview unavailable."})
    raise SystemExit
if req["action"] == "list":
    if not res["dir"]:
        out({"error": "Not a folder."}); raise SystemExit
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                    entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size,
                                    "mtime": st.st_mtime})
                except OSError:
                    entries.append({"name": e.name, "dir": False, "size": 0, "mtime": 0})
                if len(entries) >= %d:
                    break
    except PermissionError:
        out({"error": "Permission denied."}); raise SystemExit
    res["entries"] = entries
out(res)
''' % MAX_ENTRIES


def _pure(sep):
    """Path flavour of a remote machine, from the separator it reported for itself."""
    return PureWindowsPath if sep == "\\" else PurePosixPath


def _name_ok(name):
    return isinstance(name, str) and name and name not in (".", "..") and "/" not in name \
        and "\\" not in name and "\x00" not in name


class Storage:
    def __init__(self, config, state=None):
        self.config = config
        self.state = state

    # ---- devices and roots -------------------------------------------------------------
    def device(self, device_id):
        device = self.config.get("devices", {}).get(device_id)
        if not device:
            raise ValueError(f"Unknown device {device_id!r}.")
        return device

    def is_remote(self, device_id):
        from .topology import enabled, identity
        if enabled(self.config):
            self.device(device_id)
            return device_id != identity(self.config)[0]
        return self.device(device_id).get("transport") == "ssh"

    def roots(self, device_id):
        configured = self.config.get("storage", {}).get(device_id, {}).get("roots")
        if configured:
            return [str(r) for r in configured]
        return ["~"] if self.is_remote(device_id) else [str(Path.home())]

    def devices(self):
        return [{"id": name, "hostname": d.get("hostname", name), "remote": d.get("transport") == "ssh"}
                for name, d in self.config.get("devices", {}).items()]

    # ---- listing / resolving -----------------------------------------------------------
    def _local(self, device_id, path, action):
        roots = [Path(os.path.expanduser(r)).resolve() for r in self.roots(device_id)]
        if action == "exists":
            raw = Path(os.path.abspath(os.path.expanduser(path)))
            parent = raw.parent.resolve()
            if not any(parent == r or r in parent.parents for r in roots):
                raise ValueError("That folder is outside the allowed locations.")
            try:
                (parent / raw.name).lstat()
                exists = True
            except FileNotFoundError:
                exists = False
            return {"exists": exists}
        target = Path(os.path.expanduser(path)).resolve() if path else roots[0]
        if not any(target == r or r in target.parents for r in roots):
            raise ValueError("That folder is outside the allowed locations.")
        if not target.exists():
            raise ValueError("No such file or folder.")
        res = {"path": str(target), "dir": target.is_dir(), "size": target.stat().st_size,
               "roots": [str(r) for r in roots], "sep": os.sep}
        if action == "thumbnail":
            if not target.is_file():
                raise ValueError("Preview requires an image file.")
            from .thumbnails import thumbnail_payload
            return thumbnail_payload(target)
        if action == "list":
            if not target.is_dir():
                raise ValueError("Not a folder.")
            entries = []
            try:
                with os.scandir(target) as it:
                    for entry in it:
                        try:
                            st = entry.stat()
                            entries.append({"name": entry.name, "dir": entry.is_dir(),
                                            "size": st.st_size, "mtime": st.st_mtime})
                        except OSError:
                            entries.append({"name": entry.name, "dir": False, "size": 0,
                                            "mtime": 0})
                        if len(entries) >= MAX_ENTRIES:
                            break
            except PermissionError:
                raise ValueError("Permission denied.") from None
            res["entries"] = entries
        return res

    def _remote(self, device_id, path, action):
        from .topology import enabled
        if enabled(self.config):
            if self.state is None:
                raise ValueError("Node storage queries require the main runtime's state directory.")
            from .cluster import dispatch
            return dispatch(self.config, self.state, device_id, "storage", {
                "path": path or "", "action": action})
        device = self.device(device_id)
        _check_target(device.get("target"))
        ssh = shutil.which("ssh")
        if not ssh:
            raise ValueError("The OpenSSH client is not installed.")
        request = json.dumps({"action": action, "path": path or "", "roots": self.roots(device_id)})
        script = f"REQUEST = {json.dumps(request)}\n" + REMOTE_SCRIPT
        if action == "thumbnail":
            import inspect
            from .thumbnails import thumbnail_payload
            script = inspect.getsource(thumbnail_payload) + "\n" + script
        try:
            done = subprocess.run(
                [*_ssh_args(ssh, device.get("identity_file")), "-T", device["target"], "python3 -"],
                input=script, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, shell=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("The other laptop didn't respond in time.") from None
        if done.returncode:
            _fail("Reading the other laptop's storage", done)
        try:
            res = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("The other laptop returned an unreadable listing.") from None
        if "error" in res:
            raise ValueError(res["error"])
        return res

    def resolve(self, device_id, path, action="resolve"):
        return (self._remote if self.is_remote(device_id) else self._local)(device_id, path, action)

    def list(self, device_id, path=""):
        res = self.resolve(device_id, path, "list")
        remote = self.is_remote(device_id)
        # A remote node reports its own separator (the Dell is Windows); fall back to POSIX.
        sep = res.get("sep") or "/" if remote else os.sep
        parent = None
        if not any(res["path"].rstrip("/\\") == r.rstrip("/\\") for r in res["roots"]):
            parent = str(_pure(sep)(res["path"]).parent) if remote \
                else str(Path(res["path"]).parent)
            if not any(parent == r or parent.startswith(r.rstrip("/\\") + sep)
                       for r in res["roots"]):
                parent = None
        entries = sorted(res["entries"], key=lambda e: (not e["dir"], e["name"].lower()))
        return {"device": device_id, "path": res["path"], "parent": parent, "roots": res["roots"],
                "sep": sep, "entries": entries,
                "truncated": len(entries) >= MAX_ENTRIES}

    # ---- copying -----------------------------------------------------------------------
    def copy(self, src_device, src_path, dst_device, dst_dir):
        """Copy a file or folder from one laptop to the other; returns where it landed."""
        if src_device == dst_device:
            raise ValueError("Pick the other laptop: copies go between the two machines.")
        if self.is_remote(src_device) == self.is_remote(dst_device):
            raise ValueError("Copies need one local and one SSH device.")
        remote_id = src_device if self.is_remote(src_device) else dst_device
        device = self.device(remote_id)
        via_ssh = device.get("transport") == "ssh" and device.get("target")
        if not via_ssh and not transfer.clustered(self.config):
            label = device.get("label") or remote_id
            raise ValueError(f"Copying to or from {label} needs SSH or the cluster link, and "
                             "neither is set up. Browsing works; to copy, set it up with an SSH "
                             "target or move the file another way (shared folder, USB).")
        src = self.resolve(src_device, src_path)
        dst = self.resolve(dst_device, dst_dir)
        if not dst["dir"]:
            raise ValueError("The destination must be a folder.")
        name = Path(src["path"]).name if not self.is_remote(src_device) \
            else _pure(src.get("sep"))(src["path"]).name
        if not _name_ok(name):
            raise ValueError("Can't copy a drive or filesystem root; pick something inside it.")
        if not via_ssh:
            return self._copy_cluster(remote_id, src_device, src, name, dst_device, dst)
        # Normalize before checking collisions: two Linux names can map to one
        # Windows name. Inspect candidates directly, never infer absence from a
        # truncated directory listing. lstat also counts broken symlinks as taken.
        local_dst = not self.is_remote(dst_device)
        final = safe_name(name) if local_dst else name
        normalized, n = final, 1
        while self.resolve(dst_device, str(Path(dst["path"]) / final) if local_dst
                           else str(PurePosixPath(dst["path"]) / final), "exists")["exists"]:
            n += 1
            if n > MAX_COLLISIONS:
                raise ValueError(f"{normalized} and {MAX_COLLISIONS} numbered copies of it "
                                 "already exist there; tidy up or pick another folder.")
            final = collision_name(normalized, n, directory=src["dir"])
        _check_target(device.get("target"))
        scp = shutil.which("scp")
        if not scp:
            raise ValueError("The OpenSSH client is not installed.")
        base = [scp, "-q", *_ssh_args(scp, device.get("identity_file"))[1:]]
        if src["dir"]:
            base.append("-r")
        target = device["target"]
        # Local arguments are bare names run from their own folder: a Windows path such as
        # C:\x would otherwise be read by scp as host "C".
        if self.is_remote(src_device):
            cwd = dst["path"]
            args = [*base, "--", f"{target}:{src['path']}", final]
        else:
            cwd = str(Path(src["path"]).parent)
            args = [*base, "--", name, f"{target}:{dst['path'].rstrip('/')}/{final}"]
        try:
            done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=3600, shell=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("The copy took over an hour and was stopped.") from None
        if done.returncode:
            _fail("Copying", done)
        return {"copied": name, "as": final, "from": f"{src_device}:{src['path']}",
                "to": f"{dst_device}:{dst['path']}", "dir": src["dir"],
                "bytes": None if src["dir"] else src["size"]}

    def _copy_cluster(self, node, src_device, src, name, dst_device, dst):
        """Copy over the cluster link (chunked transfer jobs) to or from a hub-only node."""
        from .topology import identity
        local, main = identity(self.config)
        if local != main or self.state is None:
            raise ValueError("Copies to or from another laptop run from the main PC's console.")
        config, state = self.config, self.state
        up = not self.is_remote(src_device)  # main -> node, else node -> main

        def job(payload):
            return transfer.call(config, state, node, payload)

        if not src["dir"]:
            if src["size"] > transfer.MAX_BYTES:
                raise ValueError(f"Files over {transfer.MAX_BYTES // 1024 ** 3} GB aren't "
                                 "copied this way.")
            if up:
                with open(src["path"], "rb") as source:
                    size = os.fstat(source.fileno()).st_size
                    final = transfer.push(config, state, node, source, size, name=name,
                                          dest="path", folder=dst["path"])["sent"]
            else:
                final = transfer.pull_path(config, state, node, src["path"], dst["path"], name)
        else:
            tree = transfer.check_tree(transfer.walk_tree(src["path"]) if up
                                       else job({"action": "tree", "path": src["path"]}))
            if up:
                made = job({"action": "mkdir", "dir": dst["path"], "name": name})
                final, folders = made["as"], {(): made["path"]}
            else:
                final = transfer.make_dir(dst["path"], name)
                folders = {(): str(Path(dst["path"]) / final)}
            try:
                for rel in tree["dirs"]:
                    parent = folders[tuple(rel[:-1])]
                    if up:
                        folders[tuple(rel)] = job({"action": "mkdir", "dir": parent,
                                                   "name": rel[-1]})["path"]
                    else:
                        folders[tuple(rel)] = str(Path(parent) / transfer.make_dir(parent, rel[-1]))
                for item in tree["files"]:
                    rel, parent = item["rel"], folders[tuple(item["rel"][:-1])]
                    if up:
                        with open(Path(src["path"]).joinpath(*rel), "rb") as source:
                            transfer.push(config, state, node, source,
                                          os.fstat(source.fileno()).st_size, name=rel[-1],
                                          dest="path", folder=parent)
                    else:
                        transfer.pull_path(config, state, node, src["path"], parent, rel[-1],
                                           rel=rel)
            except Exception as exc:
                where = folders[()]
                raise RuntimeError(f"The folder copy stopped part-way ({exc}). What arrived so "
                                   f"far is in {where}.") from exc
        return {"copied": name, "as": final, "from": f"{src_device}:{src['path']}",
                "to": f"{dst_device}:{dst['path']}", "dir": src["dir"],
                "bytes": None if src["dir"] else src["size"]}
