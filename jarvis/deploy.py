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
import json
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
    with tarfile.open(fileobj=buffer, mode="w", dereference=True) as tar:
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


BUNDLE_DOMAIN = b"jarvis-code-v2\n"  # a versioned bundle's signature can't pass as a job's or a legacy one
VERSION_FILE = "cluster-code-version.json"

# Nodes pull updates themselves. One that has never installed a versioned bundle (no
# cluster-code-version.json — the Dell is in that set and cannot be fixed by hand) still
# has to accept a zip signed only with `signature`, or refusing it strands the node.
# code_bundle() already serves `version` + `vsig` beside that signature, and the release
# those nodes run installs a versioned bundle and records the version, so one pull from
# the current main leaves this path. At and after this instant a signature-only bundle
# is refused. To close the window once the date has passed: delete LEGACY_UNVERSIONED_UNTIL,
# unversioned_allowed, and the accept branch in apply_bundle (keep the refusal).
LEGACY_UNVERSIONED_UNTIL = 1799971200  # 2027-01-15 00:00:00 UTC


def unversioned_allowed(now=None):
    """True while a never-versioned node may still install a signature-only bundle."""
    if now is None:
        now = time.time()
    return now < LEGACY_UNVERSIONED_UNTIL


def _bundle_bytes(raw, version, code):
    return BUNDLE_DOMAIN + f"{int(version)}\n{code}\n".encode("ascii") + raw


def installed_version(state):
    """The newest signed bundle version this node ever installed (0 when none)."""
    try:
        value = json.loads((Path(state) / VERSION_FILE).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    code, version = code_fingerprint(root), int(time.time())
    # `signature` (over the zip alone) stays for nodes that predate versions; `vsig` also covers
    # the version and fingerprint, so a replayed older bundle can't pass as a newer one.
    return {"code": code, "zip": base64.b64encode(raw).decode("ascii"), "signature": sign(raw, state),
            "version": version, "vsig": sign(_bundle_bytes(raw, version, code), state)}


def _zip_symlink(info):
    """Unix symlink entry. The DOS attribute word is not a Unix mode, so create_system 0 is left."""
    return info.create_system == 3 and (info.external_attr >> 16) & 0o170000 == 0o120000


def _safe_name(name):
    if not isinstance(name, str) or any(c in name for c in "\x00\n\r"):
        raise ValueError(f"Unsafe path in code package: {name!r}")
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or ":" in name or
            any(p in ("", ".", "..") for p in parts) or (parts[0] != "jarvis" and name != "pyproject.toml")):
        raise ValueError(f"Unsafe path in code package: {name!r}")
    return parts


def apply_bundle(bundle, key, repo=PACKAGE.parent, state=None, *, health=None):
    """Check a /v1/code bundle's signature against the pinned main `key` and its
    fingerprint, then replace this checkout's files (tmp + os.replace each, so a crash
    never leaves a half-written .py). Returns the count.

    state: this node's state folder. With it, a bundle older than the newest one installed
    here is refused (a replayed old signed bundle would downgrade the node), and once a
    versioned bundle was installed an unversioned one is refused too. Until
    LEGACY_UNVERSIONED_UNTIL a node with no version on record may still install a
    signature-only bundle; that bundle does not write a version file.

    After those files are on disk, a health check imports the agent and runs local
    config and state checks (no network, no systemd). `_starts` still runs first, on
    the staged tree, and still refuses a bundle that cannot be imported. `health`
    replaces only the post-apply check: health(repo, state) returns None when the
    install should stay. Any other result, or a check that raises, restores
    `jarvis.prev` from `_keep_previous`, deletes files the bundle added, and does not
    advance the installed version. With `state`, that failure records the bundle
    fingerprint as a bad release."""
    repo = Path(repo).resolve()
    if not key:
        raise ValueError("No code-signing key is pinned for the main PC; re-join with a fresh "
                         "invite (jarvis cluster invite on the main) to accept its updates.")
    raw = base64.b64decode(bundle["zip"], validate=True)
    version = None
    have = installed_version(state) if state is not None else 0
    if bundle.get("vsig") is not None or bundle.get("version") is not None:
        version = bundle.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or not verify(
                _bundle_bytes(raw, version, bundle.get("code")), bundle.get("vsig"), key):
            raise ValueError("The code bundle isn't signed by the main PC's pinned key; not installed.")
        if version < have:
            raise ValueError("The code bundle is older than the code installed here "
                             f"(version {version} < {have}); refused as a replay.")
    elif have:
        raise ValueError("The code bundle carries no version but this node already installed a "
                         "versioned one; refused as a possible replay.")
    elif state is not None and not unversioned_allowed():  # state=None: tests/legacy callers; every node passes state
        raise ValueError("The code bundle carries no version; unversioned bundles are no longer "
                         "accepted. Update from the main (versioned bundles) or re-join.")
    # Compat window (LEGACY_UNVERSIONED_UNTIL): the zip's own signature is enough.
    # `version` stays None, so the write below never records a version file for it.
    elif not verify(raw, bundle.get("signature"), key):
        raise ValueError("The code bundle isn't signed by the main PC's pinned key; not installed.")
    previous_project = None
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            if _zip_symlink(info):
                raise ValueError(f"Unsafe path in code package: {info.filename!r}")
            entries.append((info.filename, _safe_name(info.filename)))
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
            # pyproject.toml is not inside jarvis.prev. Hold the bytes so a failed
            # health check can put the previous declaration back too.
            project_path = repo / "pyproject.toml"
            previous_project = project_path.read_bytes() if project_path.is_file() else None
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
    # The staged import already passed. This check sees the tree just swapped in,
    # including config and state that are not part of the bundle.
    outcome = _post_apply_health(health, repo, state)
    if outcome is not None:
        rollback_error = _restore_previous(repo, previous_project)
        mark_error = None
        # A failed restore leaves the new files. Don't record the fingerprint as bad
        # then: a retry is the only way back, and the version below is still unwritten.
        if rollback_error is None and state is not None:
            try:
                _mark_bad(state, bundle.get("code"), str(outcome))
            except OSError as exc:
                mark_error = str(exc)
        if rollback_error:
            raise ValueError("The installed code failed its health check "
                             f"({outcome}); rollback failed: {rollback_error}")
        if mark_error:
            raise ValueError("The installed code failed its health check "
                             f"({outcome}); restored the previous code, but could not mark "
                             f"the release bad ({mark_error}).")
        raise ValueError(f"The installed code failed its health check ({outcome}); "
                         "restored the previous code.")
    if state is not None and version is not None:
        from .fsio import atomic_write
        atomic_write(Path(state) / VERSION_FILE, json.dumps({"version": version}))
    return len(entries)


# What a node must import to run its agent and pull the next update. The main serves its
# files as they are, so a bundle taken mid-edit (2026-10-30: deploy.py used NO_WINDOW a minute
# before its import landed) once killed the Dell's agent, which then couldn't pull the fix.
NODE_MODULES = ("__main__", "cluster_cli", "cluster", "deploy", "node", "remote")


def _child_import(root, modules, timeout, failure):
    """Import `jarvis.<m>` for each module in a child Python with `root` first on the path.
    Returns None on success, else the last stderr line (or `failure: <exc>` if the child
    could not run). Callers pick the modules and own the missing-module policy."""
    import sys
    code = "import " + ", ".join(f"jarvis.{m}" for m in modules)
    try:
        done = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True,
                              text=True, timeout=timeout, creationflags=NO_WINDOW,
                              env={**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{failure}: {exc}"
    if done.returncode:
        last = (done.stderr.strip().splitlines() or ["exit " + str(done.returncode)])[-1]
        return last[-300:]
    return None


def _starts(stage, timeout=60):
    """Why the staged package can't be imported, or None. Runs in a child Python with the
    staged folder first on the path, so it imports the new code, not this process's."""
    present = [m for m in NODE_MODULES if (Path(stage) / "jarvis" / f"{m}.py").is_file()]
    if not present or not (Path(stage) / "jarvis" / "__init__.py").is_file():
        return None  # not a full package (tests' small trees); the fingerprint check stands
    return _child_import(stage, present, timeout, "couldn't test it")


def _keep_previous(repo):
    """Copy the running jarvis/ to jarvis.prev before an update replaces it, so a bad update
    can be undone by hand (swap the folders back) even when the agent won't start."""
    current, previous = Path(repo) / "jarvis", Path(repo) / "jarvis.prev"
    if not current.is_dir():
        return
    # An install must never proceed without a complete recoverable prior tree.
    fresh = Path(repo) / "jarvis.prev.new"
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(current, fresh, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(previous, ignore_errors=True)
    os.replace(fresh, previous)


def _post_apply_health(health, repo, state):
    """The post-apply check's reason to roll back, or None. A raised check is a failure.
    `health is None` means the real check. It is not a way to skip it."""
    checker = release_health if health is None else health
    try:
        return checker(repo, state)
    except Exception as exc:
        return f"health check failed: {exc}"


def _restore_previous(repo, previous_project):
    """Put `jarvis.prev` back over `jarvis/` and drop files the new bundle added.
    `previous_project` is the pyproject.toml bytes from before the swap, or None if
    there wasn't one. Returns None, or why the restore could not finish."""
    repo = Path(repo)
    current, previous = repo / "jarvis", repo / "jarvis.prev"
    if not previous.is_dir():
        return "no jarvis.prev backup to restore"
    try:
        restored = set()
        for path in _files(previous):
            relative = path.relative_to(previous)
            target = (current / relative).resolve()
            if current.resolve() not in target.parents:
                return f"backup path escapes the package: {relative.as_posix()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.rollback")
            try:
                shutil.copyfile(path, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            restored.add(target)
        if current.is_dir():
            for path in list(_files(current)):
                if path.resolve() not in restored:
                    path.unlink(missing_ok=True)
        project = repo / "pyproject.toml"
        if previous_project is None:
            project.unlink(missing_ok=True)
        else:
            temporary = project.with_name(f".{project.name}.{os.getpid()}.rollback")
            try:
                temporary.write_bytes(previous_project)
                os.replace(temporary, project)
            finally:
                temporary.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return None


def _mark_bad(state, code, why):
    """Record this fingerprint the way release.guard does, so the node does not pull it again."""
    from .fsio import atomic_write
    from .release import BAD
    atomic_write(Path(state) / BAD, json.dumps({"code": code, "at": time.time(), "why": why}))


def release_health(repo, state=None):
    """Why this checkout should not keep the release just installed, or None when it should.

    A tree with no `jarvis/__init__.py` is not a package. Signature tests use those
    trees; the fingerprint check is what judges them. A package is never reported
    healthy without both of the following actually running:

    - a child interpreter imports every agent module (`NODE_MODULES`); a missing
      module is a failure, not a skip
    - local config and state checks of the kind `cluster doctor` makes before it
      opens a socket or touches systemd: the config parses, cluster identity
      resolves, and a node's credential matches that identity

    Nothing here calls `doctor()`, opens a connection, runs ssh, or calls systemctl.
    """
    repo = Path(repo)
    package = repo / "jarvis"
    if not (package / "__init__.py").is_file():
        return None
    missing = [name for name in NODE_MODULES if not (package / f"{name}.py").is_file()]
    if missing:
        return "agent modules missing: " + ", ".join(missing)
    problem = _import_agent(repo)
    if problem:
        return problem
    return _local_doctor(repo, state)


def _import_agent(repo, timeout=60):
    """Why the installed agent modules did not import, or None.

    Unlike `_starts`, this does not treat a missing package as success. The caller
    has already required every `NODE_MODULES` file. A child that cannot be started
    is a failure."""
    return _child_import(repo, NODE_MODULES, timeout, "couldn't import the agent")


def _local_doctor(repo, state):
    """Config and state checks that do not leave this machine.

    `cluster doctor` goes on to the hub, SSH, and the service manager. Those stay
    out of an install: a code swap must not depend on the network or on systemd.
    """
    from .topology import enabled, identity
    config_path = Path(repo) / "jarvis.config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "jarvis.config.json can't be read"
    if not isinstance(config, dict):
        return "jarvis.config.json isn't a JSON object"
    try:
        if not enabled(config):
            return "cluster.enabled is false"
        local, main = identity(config)
    except (TypeError, ValueError, AttributeError) as exc:
        return str(exc) or "cluster config is unusable"
    if state is None:
        return None
    state = Path(state)
    if not state.is_dir():
        return "state folder is missing"
    if local == main:
        return None
    try:
        note = json.loads((state / "cluster-node.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "node credential can't be read"
    if not isinstance(note, dict):
        return "node credential isn't a JSON object"
    if note.get("node_id") != local or note.get("main_device") != main:
        return "node credential does not match this PC's configured identity"
    token = note.get("token")
    if not isinstance(token, str) or len(token) < 20:
        return "node credential is missing a token"
    code_key = note.get("code_key")
    if not isinstance(code_key, str) or not CODE_KEY.fullmatch(code_key):
        return "node credential is missing the main's code-signing key"
    return None


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
    for target in targets:  # _ssh already refused a target that is not user@host
        try:
            out = subprocess.run([*argv, "--", target, command], input=stdin, capture_output=True,
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


PULL_WAIT = 240   # seconds a pull node gets to poll (~30 s), install, restart and check in
HEARTBEAT = 30    # seconds between "still testing" lines during a release cut
SEEN_FRESH = 90   # a node that checked in this recently counts as online


def _fresh(status):
    return bool(status and status.get("seen") and time.time() - status["seen"] < SEEN_FRESH)


def _say(text):
    """Progress goes to stderr: stdout stays the JSON report."""
    import sys
    print(f"jarvis update: {text}", file=sys.stderr, flush=True)


def _heartbeat(label, every=None, say=None):
    """Context manager: says `label (Ns)` every `every` seconds while a long step runs, so a
    release cut (the full test suite, ~9 min) never looks like a hang."""
    import contextlib
    import threading
    say, every = say or _say, every or HEARTBEAT

    @contextlib.contextmanager
    def beat():
        stop, began = threading.Event(), time.monotonic()

        def tick():
            while not stop.wait(every):
                say(f"{label} ({round(time.monotonic() - began)} s)")
        thread = threading.Thread(target=tick, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
    return beat()


def _pull(state, node, how, cut=None, say=None, wait=None):
    """A pull node takes the main's *release*, never the live tree. Cut one now (the full
    suite runs first, minutes: progress is printed meanwhile) so the update means what David
    asked for, say plainly when the tests hold it back, then wait (bounded) until the node
    reports the release's fingerprint. An offline node is reported and left alone."""
    from .release import cut as cut_release, served_code
    say = say or _say
    wait = PULL_WAIT if wait is None else wait
    started = time.monotonic()
    _, before = _node_code(state, node)
    say(f"cutting a release for {node}: the full test suite runs on a copy of the code first "
        "(about 9 minutes); a tested release is what nodes get")
    with _heartbeat("release tests still running", say=say):
        report = (cut or cut_release)(state, quiet=0)
    out = {"node": node, "updated": False, "pull": True, "release": report, "before": before}
    if report.get("released") or "already get" in str(report.get("note", "")):
        want = served_code(state)
        out["expected"] = want
        say(f"release {want} ready; waiting for {node} to pull it")
        status, now = _node_code(state, node)
        online = _fresh(status)
        deadline = time.monotonic() + (wait if online else 0)
        while not (now == want and online):
            if time.monotonic() >= deadline:
                break
            time.sleep(2)
            status, now = _node_code(state, node)
            online = _fresh(status)
        out["after"] = now
        if now == want and online:
            out.update(updated=True, note=f"{node} runs {want}.")
        elif not online:
            out["note"] = (f"{how}; {node} is offline right now, so it will pull release {want} "
                           "when it reconnects. Not retrying.")
        else:
            out["note"] = (f"Release {want} is cut but {node} still reports {now} after {wait} s. "
                           f"{how}; check its node agent (jarvis cluster doctor).")
    elif report.get("tests"):
        out["note"] = (f"Not sent: the release tests failed, so {node} keeps its last release. "
                       f"Fix the failing test, then run this again. ({str(report['tests'])[:900]})")
    else:
        out["note"] = f"Not sent yet: {report.get('note') or 'no release could be cut'}"
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def update(config, state, node, rollback=False, cut=None):
    """Bring `node` to this main's code (or back to its previous code). Returns a report.
    `cut` replaces release.cut for pull nodes (one cut shared by a whole fleet update)."""
    from .topology import identity
    local, main = identity(config)
    if local != main:
        raise ValueError(f"This is {local}, a node. Run jarvis cluster update {local} on the main PC ({main}); "
                         "the node then picks up the new code on its next poll.")
    if node == main:
        raise ValueError("The main PC already runs this code.")
    if not is_ssh_node(config, node):
        if rollback:
            raise ValueError(f"{node} has no SSH link, so it can't be rolled back remotely.")
        return _pull(state, node, f"{node} has no SSH link: it pulls the main's code itself", cut=cut)
    _, _, folder = _ssh(config, state, node)
    kernel = _node_os(config, state, node)
    if kernel is None:
        # The scripts below need a POSIX shell, tar and a known service manager.
        if rollback:
            raise ValueError(f"{node} isn't a Linux or macOS node, so it can't be rolled back remotely.")
        return _pull(state, node, f"{node} isn't a Linux or macOS node: it pulls the main's code itself", cut=cut)
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
