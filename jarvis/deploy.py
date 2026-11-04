"""Deploy JARVIS's code from the main PC to its nodes, without git (`jarvis cluster update`).

Every node reports a fingerprint of its code with each check-in, and the main refuses to
send an action to a node whose code differs from its own (read-only queries still work),
so the two can never silently drift apart. `update` closes the gap:

1. pack the main's `jarvis/` package and `pyproject.toml` (no `__pycache__`)
2. stream it over the node's pinned-host-key SSH link into a staging folder
3. check the staged fingerprint on the node before touching anything
4. swap it in, keeping the previous code as `jarvis.prev` (`--rollback` swaps it back)
5. restart only the node agent and wait until it reports the new fingerprint

The node's own config and state are never part of the package. What gets packed is the
tested release (release.py), not the live working tree: a file saved mid-edit never reaches
a node, and a node that doesn't check in with new code is rolled back.

Nodes without SSH pull the code instead (/v1/code). That reply comes from a loopback port
which, while the tunnel is down, any program on the node could be listening on, so the
main signs each bundle with its code-signing key (`ssh-keygen -Y sign`, no extra Python
packages) and a node installs only what verifies against the key its invite pinned.
"""

import base64
import fnmatch
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

from .platforms import NO_WINDOW

PACKAGE = Path(__file__).resolve().parent
SKIP = {"__pycache__"}
# Editor and hand-made backup files never count as code (the node's fingerprint and the
# release.GUARD walk use the same list).
EXCLUDE = ("*.pre-*", "*~", "*.swp", ".#*", "*.tmp")
REMOTE_DIR = "~/projects/JARVIS"
SERVICE = "jarvis-cluster.service"
WAIT = 40  # seconds for the restarted node to check in with the new code
SIGN_NAMESPACE = "jarvis-code"
SIGNING_KEY = "code-signing.key"  # in the main's state folder; the .pub beside it is pinned
CODE_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}")
FLAGS = NO_WINDOW

# Runs on the node: the same fingerprint as code_fingerprint(), for a folder (CRLF-blind).
REMOTE_FINGERPRINT = r"""
import fnmatch, hashlib, os, sys
EXCLUDE = ("*.pre-*", "*~", "*.swp", ".#*", "*.tmp")
root = sys.argv[1]; h = hashlib.sha256(); found = []
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    found += [os.path.join(base, n) for n in files if not n.endswith(".pyc") and not any(fnmatch.fnmatchcase(n, x) for x in EXCLUDE)]
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
             if p.is_file() and not SKIP.intersection(p.parts) and p.suffix != ".pyc"
             and not any(fnmatch.fnmatchcase(p.name, x) for x in EXCLUDE)]
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def code_fingerprint(root=PACKAGE):
    """16 hex chars identifying this exact code. Cached until a file changes."""
    root = Path(root)
    files, stats = [], []
    for path in _files(root):
        try:
            info = path.stat()
        except OSError:
            continue  # saved-and-gone between the listing and now (editor temp file)
        files.append(path)
        stats.append((info.st_size, info.st_mtime_ns, info.st_ino))
    # Size and inode too: a content change that keeps its old mtime (cp -p, rsync -a) still shows.
    stamp = (len(files), max((s[1] for s in stats), default=0), sum(s[0] for s in stats),
             sum(s[2] for s in stats))
    if root == PACKAGE and _CACHE["stamp"] == stamp:
        return _CACHE["value"]
    digest = hashlib.sha256()
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        # CRLF -> LF so a Windows checkout with core.autocrlf matches the same commit.
        digest.update(data.replace(b"\r\n", b"\n") + b"\0")
    value = digest.hexdigest()[:16]
    if root == PACKAGE:
        _CACHE.update(stamp=stamp, value=value)
    return value


def package(root=PACKAGE):
    """The tar stream a node unpacks: jarvis/ plus pyproject.toml, from `root` (the tested
    release, release.source(); the live package by default)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in _files(root):
            tar.add(path, arcname="jarvis/" + path.relative_to(root).as_posix(), recursive=False)
        pyproject = root.parent / "pyproject.toml"
        if pyproject.is_file():
            tar.add(pyproject, arcname="pyproject.toml")
    return buffer.getvalue()


def is_ssh_node(config, node):
    return ((config.get("devices") or {}).get(node) or {}).get("transport") == "ssh"


def _keygen():
    binary = shutil.which("ssh-keygen")
    if not binary:
        raise ValueError("ssh-keygen (OpenSSH client) is not installed.")
    return binary


def code_key(state):
    """The main's code-signing public key line ("ssh-ed25519 AAAA..."), made on first use."""
    key = Path(state) / SIGNING_KEY
    public = Path(f"{key}.pub")
    if not key.is_file():
        key.parent.mkdir(parents=True, exist_ok=True)
        public.unlink(missing_ok=True)
        try:
            subprocess.run([_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", SIGN_NAMESPACE,
                            "-f", str(key)], check=True, capture_output=True, timeout=30,
                           creationflags=FLAGS)
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Couldn't make the code-signing key: {exc}") from None
    line = " ".join(public.read_text(encoding="ascii").split()[:2])
    if not CODE_KEY.fullmatch(line):
        raise ValueError(f"{public} is not an ed25519 public key.")
    return line


def sign(data, state):
    """An armored SSH signature of `data` by the main's code-signing key."""
    code_key(state)
    try:
        out = subprocess.run([_keygen(), "-Y", "sign", "-q", "-f", str(Path(state) / SIGNING_KEY),
                              "-n", SIGN_NAMESPACE], input=data, capture_output=True, timeout=30,
                             creationflags=FLAGS)
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f"Couldn't sign the code bundle: {exc}") from None
    if out.returncode or b"BEGIN SSH SIGNATURE" not in out.stdout:
        raise RuntimeError("Couldn't sign the code bundle: "
                           + out.stderr.decode("utf-8", "replace").strip()[-200:])
    return out.stdout.decode("ascii")


def verify(data, signature, key):
    """True only if `signature` is the holder of public `key` signing exactly `data`."""
    if not isinstance(key, str) or not CODE_KEY.fullmatch(key) or not isinstance(signature, str) \
            or "BEGIN SSH SIGNATURE" not in signature:
        return False
    with tempfile.TemporaryDirectory(prefix="jarvis-verify-") as folder:
        signers, sig = Path(folder) / "signers", Path(folder) / "code.sig"
        signers.write_text(f'{SIGN_NAMESPACE} namespaces="{SIGN_NAMESPACE}" {key}\n', encoding="ascii")
        sig.write_text(signature, encoding="ascii")
        try:
            out = subprocess.run([_keygen(), "-Y", "verify", "-f", str(signers), "-I", SIGN_NAMESPACE,
                                  "-n", SIGN_NAMESPACE, "-s", str(sig)], input=data,
                                 capture_output=True, timeout=30, creationflags=FLAGS)
        except subprocess.SubprocessError:
            return False
    return out.returncode == 0


JOB_SKEW = 600  # seconds a signed job's `issued` may differ from the node's clock
JOB_DOMAIN = b"jarvis-job-v1\n"  # never the bytes of a code bundle: a job signature can't pass as one


def _job_bytes(job, node):
    import json
    return JOB_DOMAIN + json.dumps({"id": job.get("id"), "node": node, "kind": job.get("kind"),
                                    "payload": job.get("payload"), "issued": job.get("issued")},
                                   sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign_job(job, node, state):
    """The job as the main hands it to `node`, signed with the code-signing key the node
    pinned at join: a program squatting the node's loopback port while the tunnel is down
    can't hand it commands (audit 2026-11-03)."""
    import time
    job = {**job, "issued": round(time.time())}
    return {**job, "sig": sign(_job_bytes(job, node), state)}


def verify_job(job, node, key, now=None):
    """None when the job is the main's, for this node and fresh; else why it is refused."""
    import time
    if not isinstance(job, dict) or not verify(_job_bytes(job, node), job.get("sig"), key):
        return "not signed by the main"
    issued = job.get("issued")
    if not isinstance(issued, (int, float)) or abs((now or time.time()) - issued) > JOB_SKEW:
        return "too old (or the clocks differ by more than 10 minutes)"
    return None


def code_bundle(state):
    """What /v1/code serves: the tested release's jarvis/ + pyproject.toml (release.py) as
    base64 zip, with its fingerprint and the main's signature over the zip."""
    from .release import source
    root = source(state)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in _files(root):
            bundle.write(path, "jarvis/" + path.relative_to(root).as_posix())
        pyproject = root.parent / "pyproject.toml"
        if pyproject.is_file():
            bundle.write(pyproject, "pyproject.toml")
    raw = buffer.getvalue()
    return {"code": code_fingerprint(root), "zip": base64.b64encode(raw).decode("ascii"),
            "signature": sign(raw, state)}


def _safe_name(name):
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or ":" in name or
            any(p in ("", ".", "..") for p in parts) or (parts[0] != "jarvis" and name != "pyproject.toml")):
        raise ValueError(f"Unsafe path in code package: {name!r}")
    return parts


def apply_bundle(bundle, key, repo=PACKAGE.parent):
    """Check a /v1/code bundle's signature against the pinned main `key` and its
    fingerprint, then replace this checkout's files (tmp + os.replace each, so a crash
    never leaves a half-written .py). Returns the count."""
    repo = Path(repo).resolve()
    if not key:
        raise ValueError("No code-signing key is pinned for the main PC; re-join with a fresh "
                         "invite (jarvis cluster invite on the main) to accept its updates.")
    raw = base64.b64decode(bundle["zip"], validate=True)
    if not verify(raw, bundle.get("signature"), key):
        raise ValueError("The code bundle isn't signed by the main PC's pinned key; not installed.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
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
            # Requirements first, from the signed bundle's own pyproject.toml: a release that
            # adds an import-time dependency can't pass the start check without them.
            from .release import install_dependencies
            staged = stage / "pyproject.toml"
            current = repo / "pyproject.toml"
            if staged.is_file():
                install_dependencies(current.read_text(encoding="utf-8") if current.is_file() else "",
                                     staged.read_text(encoding="utf-8"))
            problem = _starts(stage)
            if problem:
                raise ValueError(f"The main's code doesn't start ({problem}); kept this node's code.")
            _keep_previous(repo)
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


# What a node must import to run its agent and pull the next update. The main serves its
# files as they are, so a bundle taken mid-edit (2026-10-30: deploy.py used NO_WINDOW a minute
# before its import landed) once killed the Dell's agent, which then couldn't pull the fix.
NODE_MODULES = ("__main__", "cluster_cli", "cluster", "deploy", "node", "remote")


def _starts(stage, timeout=60):
    """Why the staged package can't be imported, or None. Runs in a child Python with the
    staged folder first on the path, so it imports the new code, not this process's."""
    present = [m for m in NODE_MODULES if (Path(stage) / "jarvis" / f"{m}.py").is_file()]
    if not present or not (Path(stage) / "jarvis" / "__init__.py").is_file():
        return None  # not a full package (tests' small trees); the fingerprint check stands
    import sys
    code = "import " + ", ".join(f"jarvis.{m}" for m in present)
    try:
        done = subprocess.run([sys.executable, "-c", code], cwd=stage, capture_output=True,
                              text=True, timeout=timeout, creationflags=NO_WINDOW,
                              env={**os.environ, "PYTHONPATH": str(stage), "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"couldn't test it: {exc}"
    if done.returncode:
        last = (done.stderr.strip().splitlines() or ["exit " + str(done.returncode)])[-1]
        return last[-300:]
    return None


def _keep_previous(repo):
    """Copy the running jarvis/ to jarvis.prev before an update replaces it, so a bad update
    can be undone by hand (swap the folders back) even when the agent won't start."""
    current, previous = Path(repo) / "jarvis", Path(repo) / "jarvis.prev"
    if not current.is_dir():
        return
    try:
        shutil.rmtree(previous, ignore_errors=True)
        shutil.copytree(current, previous, ignore=shutil.ignore_patterns("__pycache__"))
    except OSError:
        pass  # a backup that can't be made never blocks the update itself


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


PIP_SSH_TIMEOUT = 720


def _remote(config, state, node, command, stdin=b"", timeout=120):
    """Run `command` on the node (stdin piped in) over the first target that answers."""
    argv, targets, _ = _ssh(config, state, node)
    last = ""
    for target in targets:
        try:
            out = subprocess.run([*argv, target, command], input=stdin, capture_output=True,
                                 timeout=timeout, creationflags=NO_WINDOW)
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"ssh to {node} failed: {exc}") from None
        if out.returncode == 255:  # ssh itself failed: next address
            last = out.stderr.decode("utf-8", "replace").strip()[-200:]
            continue
        return out.returncode, out.stdout.decode("utf-8", "replace"), out.stderr.decode("utf-8", "replace")
    raise ConnectionError(last or f"No address for {node} answered.")


# How each node OS restarts its agent (autostart.install("jarvis-cluster", ...)). A failed
# restart is reported but not fatal: the files are already swapped, and a node still on the
# old code sees the main's fingerprint differ on its next poll and restarts itself.
RESTART = {"Linux": f"systemctl --user restart {SERVICE}",
           "Darwin": 'launchctl kickstart -k "gui/$(id -u)/com.jarvis.jarvis-cluster"'}


def _node_os(config, state, node):
    """"Linux" or "Darwin" when the node's SSH shell is POSIX with one of those kernels,
    else None (Windows' cmd/PowerShell, Git Bash, or anything unrecognised)."""
    code, out, _ = _remote(config, state, node, "uname -s", timeout=30)
    kernel = out.strip().split()[0] if not code and out.strip() else ""
    return kernel if kernel in RESTART else None


def _restart(kernel):
    return (f"{{ {RESTART[kernel]}; }} || echo 'agent restart failed; it restarts itself "
            "on its next poll' >&2")


def _bash(script):
    """A command line that runs script under bash whatever the node's login shell is (fish
    can't parse `{ ...; }`); base64 keeps the script clear of every shell's quoting."""
    return f"bash -c 'eval \"$(echo {base64.b64encode(script.encode()).decode()} | base64 -d)\"'"


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
    kernel = _node_os(config, state, node)
    if kernel is None:
        # The scripts below need a POSIX shell, tar and a known service manager.
        if rollback:
            raise ValueError(f"{node} isn't a Linux or macOS node, so it can't be rolled back remotely.")
        return {"node": node, "updated": False, "pull": True,
                "note": f"{node} isn't a Linux or macOS node: it pulls the main's code itself on "
                        "its next poll (within ~30 s while its node agent runs) and restarts."}
    started = time.monotonic()
    _, before = _node_code(state, node)
    from .release import served_code, source
    want = None if rollback else served_code(state)
    py = f"$( [ -x {folder}/.venv/bin/python ] && echo {folder}/.venv/bin/python || echo python3 )"
    if rollback:
        script = (f"set -e; cd {folder}; [ -d jarvis.prev ] || {{ echo 'no previous code to roll back to' >&2; exit 3; }}; "
                  f"rm -rf jarvis.bad; mv jarvis jarvis.bad; mv jarvis.prev jarvis; mv jarvis.bad jarvis.prev; "
                  f"{_restart(kernel)}; {py} - jarvis <<'PY'\n{REMOTE_FINGERPRINT}\nPY")
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
                  # New requirements are installed before the restart (see release.install_dependencies).
                  f"deps=; [ -f {stage}/pyproject.toml ] && ! cmp -s {stage}/pyproject.toml pyproject.toml && deps=1; "
                  f"[ -f {stage}/pyproject.toml ] && cp {stage}/pyproject.toml pyproject.toml; rm -rf {stage}; "
                  f"[ -n \"$deps\" ] && {{ timeout 600 {py} -m pip install --quiet --disable-pip-version-check -e . "
                  f"|| echo 'pip install failed or timed out (600 s); the node may need its requirements installed by hand' >&2; }}; "
                  f"{_restart(kernel)}; echo ok")
        # Longer than the script's own `timeout 600` pip install, or ssh is cut mid-install.
        code, out, err = _remote(config, state, node, _bash(script), stdin=package(source(state)),
                                 timeout=PIP_SSH_TIMEOUT)
        if code or "ok" not in out:
            raise RuntimeError(err.strip()[-300:] or f"Update failed ({code}); the node kept its code.")
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        status, now = _node_code(state, node)
        if now == want and status and status.get("seen") and time.time() - status["seen"] < 15:
            return {"node": node, "updated": not rollback, "rolled_back": rollback,
                    "before": before, "after": now, "seconds": round(time.monotonic() - started, 1)}
        time.sleep(1)
    if not rollback:
        # The new code never checked in: put the previous code back (jarvis.prev) instead of
        # leaving the node dead until someone notices.
        try:
            back = update(config, state, node, rollback=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            back = {"error": str(exc)}
        restored = back.get("after") or back.get("expected")
        return {"node": node, "updated": False, "rolled_back": "error" not in back,
                "before": before, "after": restored, "rejected": want,
                "note": f"{node} didn't check in with {want} within {WAIT} s; " +
                        (f"rolled back to {restored}." if "error" not in back
                         else f"rollback failed too: {back['error']}"),
                "seconds": round(time.monotonic() - started, 1)}
    return {"node": node, "updated": not rollback, "rolled_back": rollback, "before": before,
            "after": _node_code(state, node)[1], "expected": want,
            "note": f"Code is in place but {node} hasn't checked in with it yet; check its jarvis-cluster.service.",
            "seconds": round(time.monotonic() - started, 1)}
