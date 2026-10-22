"""Move files and images between the laptops over the SSH link JARVIS already trusts.

Lan Mouse shares the mouse and keyboard, not drag-and-drop sessions: a drag begun in a
Windows app can't continue into a Wayland app, because each OS keeps its drag data
in-process. This is the equivalent that does work: drop a file on the console (or run
`jarvis send`) and it lands in ~/JARVIS-Drop on the other laptop; `jarvis fetch` pulls
one back to ~/Downloads/JARVIS-Drop. Same key, BatchMode and strict host-key checking
as the inventory probes; only the fixed drop folders are touched.
"""

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

REMOTE_DIR = "JARVIS-Drop"
MAX_BYTES = 2 * 1024 ** 3
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. ()+,@-]{0,199}")


def ssh_device(config, name=None):
    """The configured laptop reached over SSH (the named one, else the first)."""
    devices = {k: v for k, v in config.get("devices", {}).items() if v.get("transport") == "ssh"}
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
