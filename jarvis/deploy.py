"""Deploy JARVIS's code from the main PC to its nodes, without git (`jarvis cluster update`).

Every node reports a fingerprint of its code with each check-in, and the main refuses to
send an action to a node whose code differs from its own (read-only queries still work),
so the two can never silently drift apart. `update` closes the gap:

1. pack the main's `jarvis/` package and `pyproject.toml` (no `__pycache__`)
2. stream it over the node's pinned-host-key SSH link into a staging folder
3. check the staged fingerprint on the node before touching anything
4. swap it in, keeping the previous code as `jarvis.prev` (`--rollback` swaps it back)
5. restart only the node agent and wait until it reports the new fingerprint

The node's own config and state are never part of the package.
"""

import base64
import hashlib
import io
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
SKIP = {"__pycache__"}
REMOTE_DIR = "~/projects/JARVIS"
SERVICE = "jarvis-cluster.service"
WAIT = 40  # seconds for the restarted node to check in with the new code

# Runs on the node: the same fingerprint as code_fingerprint(), for a folder (CRLF-blind).
REMOTE_FINGERPRINT = r"""
import hashlib, os, sys
root = sys.argv[1]; h = hashlib.sha256(); found = []
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    found += [os.path.join(base, n) for n in files if not n.endswith(".pyc")]
for rel, path in sorted((os.path.relpath(p, root).replace(os.sep, "/"), p) for p in found):
    h.update(rel.encode() + b"\0")
    with open(path, "rb") as f:
        h.update(f.read().replace(b"\r\n", b"\n") + b"\0")
print(h.hexdigest()[:16])
"""

_CACHE = {"stamp": None, "value": None}


def _files(root):
    """Package files in one canonical order (relative POSIX path), the node's order too."""
    found = [p for p in root.rglob("*")
             if p.is_file() and not SKIP.intersection(p.parts) and p.suffix != ".pyc"]
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def code_fingerprint(root=PACKAGE):
    """16 hex chars identifying this exact code. Cached until a file changes."""
    root = Path(root)
    files = list(_files(root))
    stamp = (len(files), max((f.stat().st_mtime_ns for f in files), default=0))
    if root == PACKAGE and _CACHE["stamp"] == stamp:
        return _CACHE["value"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        # CRLF -> LF so a Windows checkout with core.autocrlf matches the same commit.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    value = digest.hexdigest()[:16]
    if root == PACKAGE:
        _CACHE.update(stamp=stamp, value=value)
    return value


def package():
    """The tar stream a node unpacks: jarvis/ plus pyproject.toml."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in _files(PACKAGE):
            tar.add(path, arcname="jarvis/" + path.relative_to(PACKAGE).as_posix(), recursive=False)
        pyproject = PACKAGE.parent / "pyproject.toml"
        if pyproject.is_file():
            tar.add(pyproject, arcname="pyproject.toml")
    return buffer.getvalue()


def is_ssh_node(config, node):
    return ((config.get("devices") or {}).get(node) or {}).get("transport") == "ssh"


def code_bundle():
    """What /v1/code serves: jarvis/ + pyproject.toml as base64 zip, with its fingerprint."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in _files(PACKAGE):
            bundle.write(path, "jarvis/" + path.relative_to(PACKAGE).as_posix())
        pyproject = PACKAGE.parent / "pyproject.toml"
        if pyproject.is_file():
            bundle.write(pyproject, "pyproject.toml")
    return {"code": code_fingerprint(), "zip": base64.b64encode(buffer.getvalue()).decode("ascii")}


def _safe_name(name):
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or ":" in name or
            any(p in ("", ".", "..") for p in parts) or (parts[0] != "jarvis" and name != "pyproject.toml")):
        raise ValueError(f"Unsafe path in code package: {name!r}")
    return parts


def apply_bundle(bundle, repo=PACKAGE.parent):
    """Unpack a /v1/code bundle, check its fingerprint, then replace this checkout's files
    (tmp + os.replace each, so a crash never leaves a half-written .py). Returns the count."""
    repo = Path(repo).resolve()
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["zip"], validate=True))) as archive:
        entries = [(i.filename, _safe_name(i.filename)) for i in archive.infolist() if not i.is_dir()]
        with tempfile.TemporaryDirectory(prefix="jarvis-update-") as stage:
            stage = Path(stage)
            for name, parts in entries:
                target = stage.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
            got = code_fingerprint(stage / "jarvis")
            if got != bundle.get("code"):
                raise ValueError(f"Downloaded code fingerprint {got} != {bundle.get('code')}; not installed.")
            for name, parts in entries:
                target = repo.joinpath(*parts).resolve()
                if repo not in target.parents:
                    raise ValueError(f"Unsafe path in code package: {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{os.getpid()}.new")
                shutil.copyfile(stage.joinpath(*parts), temporary)
                os.replace(temporary, target)
            # Files the main no longer has would change the fingerprint: drop them.
            keep = {repo.joinpath(*parts).resolve() for _, parts in entries}
            for path in _files(repo / "jarvis"):
                if path.resolve() not in keep:
                    path.unlink(missing_ok=True)
    return len(entries)


def _ssh(config, state, node):
    """argv prefix and targets to reach `node` over its pinned SSH link."""
    from .cluster_tunnel import pinned_host
    from .transfer import _check_target, _ssh_args
    machine = (config.get("devices") or {}).get(node)
    if not machine or machine.get("transport") != "ssh":  # forward-mode nodes pull instead
        raise ValueError(f"{node!r} is not a configured SSH node.")
    binary = shutil.which("ssh")
    if not binary:
        raise ValueError("OpenSSH client is not installed.")
    targets = [t for t in [machine.get("target"), *machine.get("fallback_targets", [])] if t]
    for target in targets:
        _check_target(target)
    folder = machine.get("jarvis_dir") or REMOTE_DIR
    if not all(c.isalnum() or c in "~/._- " for c in folder):
        raise ValueError("Odd jarvis_dir in config.")
    return [*_ssh_args(binary, machine.get("identity_file")), *pinned_host(machine, state), "-T"], targets, folder


def _remote(config, state, node, command, stdin=b"", timeout=120):
    """Run `command` on the node (stdin piped in) over the first target that answers."""
    argv, targets, _ = _ssh(config, state, node)
    last = ""
    for target in targets:
        out = subprocess.run([*argv, target, command], input=stdin, capture_output=True,
                             timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode == 255:  # ssh itself failed: next address
            last = out.stderr.decode("utf-8", "replace").strip()[-200:]
            continue
        return out.returncode, out.stdout.decode("utf-8", "replace"), out.stderr.decode("utf-8", "replace")
    raise ConnectionError(last or f"No address for {node} answered.")


def _node_code(state, node):
    from .cluster_store import Broker
    status = next((n for n in Broker(state).nodes() if n["id"] == node), None)
    return status, (status or {}).get("code")


def update(config, state, node, rollback=False):
    """Bring `node` to this main's code (or back to its previous code). Returns a report."""
    from .topology import identity
    local, main = identity(config)
    if local != main:
        raise ValueError("Run jarvis cluster update on the main PC.")
    if node == main:
        raise ValueError("The main PC already runs this code.")
    if not is_ssh_node(config, node):
        if rollback:
            raise ValueError(f"{node} has no SSH link, so it can't be rolled back remotely.")
        return {"node": node, "updated": False, "pull": True,
                "note": f"{node} has no SSH link: it pulls the main's code itself on its next poll "
                        "(within ~30 s while its node agent runs) and restarts."}
    _, _, folder = _ssh(config, state, node)
    started = time.monotonic()
    _, before = _node_code(state, node)
    want = None if rollback else code_fingerprint()
    py = f"$( [ -x {folder}/.venv/bin/python ] && echo {folder}/.venv/bin/python || echo python3 )"
    if rollback:
        script = (f"set -e; cd {folder}; [ -d jarvis.prev ] || {{ echo 'no previous code to roll back to' >&2; exit 3; }}; "
                  f"rm -rf jarvis.bad; mv jarvis jarvis.bad; mv jarvis.prev jarvis; mv jarvis.bad jarvis.prev; "
                  f"systemctl --user restart {SERVICE}; {py} - jarvis <<'PY'\n{REMOTE_FINGERPRINT}\nPY")
        code, out, err = _remote(config, state, node, "bash -s", stdin=script.encode() + b"\n")
        if code:
            raise RuntimeError(err.strip()[-300:] or f"Rollback failed ({code}).")
        want = out.strip().splitlines()[-1]
    else:
        stage = ".jarvis-update"
        script = (f"set -e; cd {folder}; rm -rf {stage}; mkdir {stage}; tar xf - -C {stage}; "
                  f"got=$({py} -c {shlex.quote(REMOTE_FINGERPRINT)} {stage}/jarvis); "
                  f"[ \"$got\" = {shlex.quote(want)} ] || {{ echo \"staged code fingerprint $got != {want}\" >&2; rm -rf {stage}; exit 4; }}; "
                  f"rm -rf jarvis.prev; [ -d jarvis ] && mv jarvis jarvis.prev; mv {stage}/jarvis jarvis; "
                  f"[ -f {stage}/pyproject.toml ] && cp {stage}/pyproject.toml pyproject.toml; rm -rf {stage}; "
                  f"systemctl --user restart {SERVICE}; echo ok")
        code, out, err = _remote(config, state, node, script, stdin=package())
        if code or "ok" not in out:
            raise RuntimeError(err.strip()[-300:] or f"Update failed ({code}); the node kept its code.")
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        status, now = _node_code(state, node)
        if now == want and status and status.get("seen") and time.time() - status["seen"] < 15:
            return {"node": node, "updated": not rollback, "rolled_back": rollback,
                    "before": before, "after": now, "seconds": round(time.monotonic() - started, 1)}
        time.sleep(1)
    return {"node": node, "updated": not rollback, "rolled_back": rollback, "before": before,
            "after": _node_code(state, node)[1], "expected": want,
            "note": f"Code is in place but {node} hasn't checked in with it yet; check its jarvis-cluster.service.",
            "seconds": round(time.monotonic() - started, 1)}
