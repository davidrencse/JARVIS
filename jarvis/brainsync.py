"""Brain transport: a private git snapshot of the vault, and a direct memory handoff.

Off unless ``config["brainsync"]["enabled"] is True``. The git repository is a
dedicated directory (default ``<base>/.jarvis/brain-git``). It is not the JARVIS
checkout and not the Obsidian vault. Its worktree stages only ``vault/*`` and,
locally, ``state/memory.sqlite3``.

Each device commits an independent ``refs/heads/brainsync/<device>`` snapshot.
Sync fetches the ``brainsync`` namespace and materializes the union of safe vault
files into the real vault with exclusive create. Same path, different bytes, is a
side-by-side ``conflict-<sha256>`` copy. Vault files are never overwritten or deleted.
A file that disappears from the vault stays in the snapshot, so a deletion does not
discard prior content.

``remote_allowlist`` is an exact list of transport paths. It is empty by default.
Secret-shaped names and content, and ``state/memory.sqlite3``, are not pushed unless
that exact path is listed. ``.git``, ``.obsidian``, and ``.jarvis`` never qualify.
History that already contains a disallowed blob is not pushed; a new sanitized
snapshot is pushed beside it. Conflicting branch histories are left in place
(no force-push, no autostash, no reset).

Memory moves only through ``handoff``, as a SQLite backup snapshot inside a bundle.
The production transport is ``SshBundleTransport``: a git bundle over
``deploy._remote``. Callers still pass one of the fixed logical commands
(``python -m jarvis brain receive|export|freeze``). ``_call`` rewrites that to
the peer checkout's interpreter before ssh runs it:
``<jarvis_dir>/.venv/bin/python -m jarvis --config <jarvis_dir>/jarvis.config.json
--state <jarvis_dir>/.jarvis brain <action>``. ``jarvis_dir`` comes from
``devices[node]`` (the same folder ``deploy._ssh`` reads). Each path is
``shlex.quote``'d. The directory must be a POSIX absolute path with no ``..``
component, NUL, or newline, and the peer ``os`` / ``platform`` / ``system``
must not say Windows. A present but unsafe checkout, or a Windows hint, fails
closed (``peer_path_unsafe``) and does not fall back to login ``python``. A
device entry that does not set ``jarvis_dir`` still sends the fixed logical
command, which is what the in-process stand-ins dispatch on. Tests can pass
``transport=``; the default is the SSH adapter.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .platforms import NO_WINDOW
from .second_brain import looks_secret

SCHEMA = "jarvis-brainsync-bundle/1"
EXPORT_SCHEMA = "jarvis-brainsync-export/1"
MARKER_SCHEMA = "jarvis-brainsync-handoff/1"
MEMORY_GIT_PATH = "state/memory.sqlite3"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000
EXCLUSION_SAMPLE = 50
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PUBLISH_REF = re.compile(r"^refs/(heads|remotes)/brainsync/([A-Za-z0-9][A-Za-z0-9_-]{0,80})\Z")
SKIP_COMPONENTS = frozenset({".git", ".obsidian", ".jarvis"})
SECRET_BASENAMES = frozenset({
    "id_rsa", "id_dsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json",
    "secrets.json", "tokens.json",
})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore")
SECRET_WORDS = ("credential", "secret", "token", "password")
RECEIVE_COMMAND = "python -m jarvis brain receive"
EXPORT_COMMAND = "python -m jarvis brain export"
FREEZE_COMMAND = "python -m jarvis brain freeze"
_FIXED_COMMANDS = frozenset({RECEIVE_COMMAND, EXPORT_COMMAND, FREEZE_COMMAND})
_MAGIC = b"JARVIS_BRAIN1\n"
_ZERO = "0" * 40


class BrainSyncError(Exception):
    def __init__(self, code, detail="", **extra):
        self.code = str(code)
        self.detail = str(detail or "")
        self.extra = dict(extra)
        text = self.code if not self.detail else f"{self.code}: {self.detail[:400]}"
        super().__init__(text)


def _result(ok, status, error=None, **extra):
    out = {"ok": bool(ok), "status": status, "error": error}
    out.update(extra)
    return out


def _settings(config):
    """Parsed brainsync config, or None when the shape is unusable."""
    if not isinstance(config, dict):
        return None
    raw = config.get("brainsync")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None
    allow_raw = raw.get("remote_allowlist", [])
    if allow_raw is None:
        allow_raw = []
    if not isinstance(allow_raw, (list, tuple)) or any(not isinstance(item, str) for item in allow_raw):
        return None
    try:
        allow = tuple(_norm_rel(item) for item in allow_raw)
    except BrainSyncError:
        return None
    remote = raw.get("remote", None)
    if remote is not None and not isinstance(remote, str):
        return None
    repo = raw.get("repo", None)
    if repo is not None and not isinstance(repo, str):
        return None
    return {"enabled": raw.get("enabled") is True, "allow": allow, "remote": remote, "repo": repo}


def _norm_rel(value):
    text = str(value).replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"[A-Za-z]:", text):
        raise BrainSyncError("bad_allowlist")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BrainSyncError("bad_allowlist")
    return "/".join(parts)


def _state_dir(base):
    return Path(base) / ".jarvis"


def _marker_path(base):
    return _state_dir(base) / "brainsync-handoff.json"


def _pending_json(base):
    return _state_dir(base) / "brainsync-pending.json"


def _pending_bin(base):
    return _state_dir(base) / "brainsync-pending.bin"


def _read_json(path):
    if not path.is_file() or path.is_symlink() or _reparse(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_marker(base):
    path = _marker_path(base)
    if not path.exists():
        return None
    data = _read_json(path)
    if data is None or data.get("schema") != MARKER_SCHEMA or data.get("readonly") is not True:
        return {"schema": MARKER_SCHEMA, "readonly": True, "old_main": None, "corrupt": True}
    return data


def _frozen(base, device):
    marker = _read_marker(base)
    if not marker:
        return False
    if marker.get("corrupt"):
        return True
    return marker.get("old_main") == device


def _role(config):
    from .topology import enabled, identity
    if not isinstance(config, dict) or not enabled(config):
        return None, "cluster_disabled"
    try:
        local, main = identity(config)
    except (OSError, ValueError):
        return None, "identity"
    if not isinstance(local, str) or not isinstance(main, str):
        return None, "identity"
    if not DEVICE_ID.fullmatch(local) or not DEVICE_ID.fullmatch(main):
        return None, "identity"
    return (local, main), None


def _vault_root(config, base):
    from .vault import vault_path
    settings = config.get("vault") if isinstance(config, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    if settings.get("enabled", True) is False:
        raise BrainSyncError("vault_disabled")
    raw = vault_path(settings)
    if not raw:
        raise BrainSyncError("vault_missing")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    if not path.is_dir():
        raise BrainSyncError("vault_missing")
    return path.resolve()


def _repo_path(cfg, base):
    raw = cfg["repo"]
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(base) / path
    else:
        path = _state_dir(base) / "brain-git"
    return path


def _is_within(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _reject_repo(repo, base, vault):
    """The brain repo must be its own git directory, never the checkout or the vault."""
    try:
        repo_r = repo.resolve()
        base_r = Path(base).resolve()
    except OSError as exc:
        raise BrainSyncError("repo_rejected") from exc
    if repo_r == base_r or repo_r == base_r / ".git" or _is_within(repo_r, base_r / ".git"):
        raise BrainSyncError("repo_is_checkout")
    if (repo_r / "jarvis" / "__main__.py").is_file() and (repo_r / ".git").exists():
        raise BrainSyncError("repo_is_checkout")
    if vault is not None:
        vault_r = vault.resolve()
        if repo_r == vault_r or _is_within(repo_r, vault_r) or _is_within(vault_r, repo_r):
            raise BrainSyncError("repo_is_vault")
    if (base_r / ".git").exists() and (repo_r / ".git").exists():
        base_git = _git_common(base_r)
        repo_git = _git_common(repo_r)
        if base_git and repo_git and base_git == repo_git:
            raise BrainSyncError("repo_is_checkout")
    if repo_r.is_file() or (repo / ".git").is_file():
        raise BrainSyncError("repo_rejected")


def _git_bin():
    binary = shutil.which("git")
    if not binary:
        raise BrainSyncError("git_missing")
    return binary


def _git_env():
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "NUL" if os.name == "nt" else os.devnull,
        "GIT_AUTHOR_NAME": "jarvis-brainsync",
        "GIT_AUTHOR_EMAIL": "brainsync@localhost",
        "GIT_COMMITTER_NAME": "jarvis-brainsync",
        "GIT_COMMITTER_EMAIL": "brainsync@localhost",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "USERPROFILE"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


_HOOKS = None


def _hooks_dir():
    """Empty hooks directory outside the brain repo, so a read does not write into it."""
    global _HOOKS
    if _HOOKS is None or not Path(_HOOKS).is_dir():
        _HOOKS = tempfile.mkdtemp(prefix="jarvis-brainsync-hooks-")
    return _HOOKS


def _git(repo, *args, input=None, check=True, timeout=120):
    """Run git with an argument vector. ``repo`` may be None for commands that create it."""
    binary = _git_bin()
    cmd = [binary, "-c", "core.autocrlf=false", "-c", "safe.directory=*",
           "-c", "protocol.file.allow=always", "-c", "advice.detachedHead=false",
           "-c", "core.hooksPath=" + _hooks_dir()]
    if repo is not None:
        cmd += ["-C", str(repo)]
    for arg in args:
        if not isinstance(arg, str) or arg.startswith("+") or arg in ("--force", "-f", "--autostash"):
            raise BrainSyncError("git_refused")
    cmd.extend(args)
    try:
        proc = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout,
                              shell=False, creationflags=NO_WINDOW, env=_git_env())
    except subprocess.TimeoutExpired as exc:
        raise BrainSyncError("git_timeout") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrainSyncError("git_failed") from exc
    if check and proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BrainSyncError("git_failed", detail[-1] if detail else "git failed")
    return proc


def _git_common(path):
    proc = _git(path, "rev-parse", "--git-common-dir", check=False)
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    common = Path(text)
    if not common.is_absolute():
        common = Path(path) / common
    try:
        return common.resolve()
    except OSError:
        return None


def _remote_arg(cfg, base, vault):
    raw = cfg["remote"]
    if raw is None or raw == "":
        return None
    if raw.startswith("-") or any(ch in raw for ch in "\n\r\t ;|&$`<>"):
        raise BrainSyncError("bad_remote")
    localish = "://" not in raw and not raw.startswith("git@")
    if localish:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(base) / path
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise BrainSyncError("bad_remote") from exc
        _reject_repo(resolved, base, vault)
        return str(resolved)
    ssh_ok = re.fullmatch(r"(ssh://|https://)[A-Za-z0-9._~:/@%+-]{1,500}", raw)
    scp_ok = re.fullmatch(r"git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+\.git", raw)
    if raw.startswith("ext::") or not (ssh_ok or scp_ok):
        raise BrainSyncError("bad_remote")
    if "://" in raw:
        userinfo = raw.split("://", 1)[1].split("/", 1)[0]
        if "@" in userinfo and ":" in userinfo.split("@", 1)[0]:
            raise BrainSyncError("bad_remote")
    return raw


def _reparse(path):
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return False


def _name_secret(path):
    for part in str(path).replace("\\", "/").split("/"):
        low = part.casefold()
        if low.startswith(".env") or low in SECRET_BASENAMES or low.endswith(SECRET_SUFFIXES):
            return True
        if any(word in low for word in SECRET_WORDS):
            return True
    return False


def _content_secret(data):
    if not isinstance(data, (bytes, bytearray)):
        return True
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeError:
        text = bytes(data).decode("utf-8", "replace")
    return looks_secret(text)


def _structural(path):
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if any(part in SKIP_COMPONENTS for part in parts):
        return False
    return True


def _remote_keep(path, data, allow):
    """True when this transport path may be pushed to the configured remote."""
    if not _structural(path) or len(data) > MAX_FILE_BYTES:
        return False
    allow = set(allow or ())
    if path == MEMORY_GIT_PATH or path.startswith("state/"):
        return path in allow
    secret = _name_secret(path) or _content_secret(data)
    if secret:
        return path in allow
    return path.startswith("vault/")


def _local_keep(path, data):
    """Files that may sit in the private snapshot. Memory is local even when it is not remote."""
    if not _structural(path) or len(data) > MAX_FILE_BYTES:
        return False
    if path == MEMORY_GIT_PATH:
        return True
    if not path.startswith("vault/"):
        return False
    return not _name_secret(path) and not _content_secret(data)


def _ensure_state(base):
    state = _state_dir(base)
    state.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(state, 0o700)
        except OSError as exc:
            raise BrainSyncError("state_unwritable") from exc
    return state


class _Lock:
    def __init__(self, base):
        self.path = _ensure_state(base) / "brainsync.lock"
        self.fd = None

    def __enter__(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if os.name == "nt":
            flags |= os.O_BINARY
        try:
            self.fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise BrainSyncError("busy") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def snapshot_memory(path):
    """Consistent SQLite snapshot via the backup API. The source is opened read-only.

    ``path`` is the live database. The returned bytes do not depend on a raw copy of
    that file or its WAL sidecar.
    """
    source_path = Path(path)
    if not source_path.is_file() or source_path.is_symlink() or _reparse(source_path):
        raise BrainSyncError("memory_unavailable")
    uri = source_path.resolve().as_uri() + "?mode=ro"
    try:
        source = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise BrainSyncError("memory_unavailable") from exc
    handle = tempfile.NamedTemporaryFile(prefix="brainsync-snap-", suffix=".sqlite3", delete=False)
    dest_name = handle.name
    handle.close()
    try:
        dest = sqlite3.connect(dest_name)
        try:
            source.backup(dest)
        except sqlite3.Error as exc:
            raise BrainSyncError("memory_unavailable") from exc
        finally:
            dest.close()
        data = Path(dest_name).read_bytes()
    finally:
        source.close()
        try:
            os.unlink(dest_name)
        except OSError:
            pass
    if not data.startswith(b"SQLite format 3\x00"):
        raise BrainSyncError("memory_unavailable")
    return data


def _stable_memory(path, between=None):
    """Two identical backups, or ``(None, code)`` when the source is moving or absent.

    A missing database is ``memory_unavailable``. It is not an empty snapshot.
    ``between`` runs only between the two reads of a file that exists. Two matching
    backups are not a fence; WS1 has to fence writers before handoff.
    """
    path = Path(path)
    if not path.exists():
        return None, "memory_unavailable"
    try:
        first = snapshot_memory(path)
        if between:
            between()
        second = snapshot_memory(path)
    except BrainSyncError as exc:
        return None, exc.code
    if first != second:
        return None, "source_not_quiesced"
    return first, None


def _db_nonempty(blob):
    if not blob:
        return False
    handle = tempfile.NamedTemporaryFile(prefix="brainsync-read-", suffix=".sqlite3", delete=False)
    name = handle.name
    try:
        handle.write(blob)
        handle.close()
        con = sqlite3.connect(f"{Path(name).resolve().as_uri()}?mode=ro", uri=True)
        try:
            names = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in names:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    return True
                count = con.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
                if count:
                    return True
            return False
        finally:
            con.close()
    except sqlite3.Error:
        return True
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _exclusive_bytes(path, data):
    """Create ``path`` only if it does not exist. Returns created, same, or exists."""
    path = Path(path)
    if path.is_symlink() or _reparse(path):
        return "symlink"
    if path.exists():
        if path.is_file() and not path.is_symlink():
            try:
                current = path.read_bytes()
            except OSError:
                return "exists"
            if current == data:
                return "same"
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "nt":
        flags |= os.O_BINARY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags | nofollow, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return "created"


def _replace_empty_db(path, data):
    """Replace an empty destination database after its bytes were copied aside."""
    path = Path(path)
    tmp = path.with_name(path.name + ".incoming")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "nt":
        flags |= os.O_BINARY
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _install_memory(base, snapshot):
    """Install a quiesced snapshot. Never discard a nonempty destination."""
    state = _ensure_state(base)
    dest = state / "memory.sqlite3"
    if dest.is_symlink() or _reparse(dest):
        return _result(False, "memory_conflict", "memory_conflict")
    digest = hashlib.sha256(snapshot).hexdigest()
    if not dest.exists():
        _exclusive_bytes(dest, snapshot)
        return _result(True, "installed", None, memory_sha256=digest)
    try:
        existing = snapshot_memory(dest)
    except BrainSyncError:
        return _result(False, "memory_conflict", "dest_not_readable")
    if existing == snapshot:
        return _result(True, "same", None, memory_sha256=digest)
    side = state / f"memory.sqlite3.conflict-{digest}"
    if _db_nonempty(existing):
        placed = _exclusive_bytes(side, snapshot)
        if placed == "exists":
            return _result(False, "memory_conflict", "memory_conflict")
        return _result(False, "memory_conflict", "memory_conflict", side=side.name)
    prior_name = "memory.sqlite3.prior-" + hashlib.sha256(existing).hexdigest()
    placed = _exclusive_bytes(state / prior_name, existing)
    if placed == "exists":
        return _result(False, "memory_conflict", "memory_conflict")
    _replace_empty_db(dest, snapshot)
    for suffix in ("-wal", "-shm"):
        sidecar = state / f"memory.sqlite3{suffix}"
        if sidecar.is_symlink():
            return _result(False, "memory_conflict", "memory_conflict")
        if sidecar.exists() and not sidecar.is_symlink():
            # The installed file is a standalone backup. Leaving the previous WAL
            # beside it would hide that snapshot on the next open.
            parked = state / f"memory.sqlite3{suffix}.prior-{hashlib.sha256(existing).hexdigest()[:20]}"
            if not parked.exists():
                try:
                    os.replace(sidecar, parked)
                except OSError:
                    return _result(False, "memory_conflict", "memory_conflict")
    return _result(True, "installed", None, memory_sha256=digest, prior=prior_name)


def _contained(root, rel):
    if not _structural(rel):
        raise BrainSyncError("bad_path")
    path = Path(root)
    for part in rel.split("/"):
        path = path / part
        if path.is_symlink() or _reparse(path):
            raise BrainSyncError("symlink")
    try:
        if path.exists() or path.parent.exists():
            path.resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise BrainSyncError("bad_path") from exc
    return path


def _init_repo(repo):
    if (repo / ".git").is_dir():
        return
    if repo.exists() and any(repo.iterdir()):
        raise BrainSyncError("repo_not_empty")
    repo.mkdir(parents=True, exist_ok=True)
    proc = _git(None, "init", "-b", "brainsync-empty", str(repo), check=False)
    if proc.returncode != 0:
        proc = _git(None, "init", str(repo), check=False)
    if proc.returncode != 0:
        raise BrainSyncError("git_failed")


def _rev(repo, ref):
    proc = _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", text):
        return None
    return text


def _tree_of(repo, commit):
    proc = _git(repo, "rev-parse", "--verify", "--quiet", commit + "^{tree}")
    return proc.stdout.decode("ascii", "replace").strip()


def _ls_tree(repo, commit):
    proc = _git(repo, "ls-tree", "-r", "-z", commit)
    raw = proc.stdout
    items = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            meta, path = entry.split(b"\t", 1)
            mode, kind, sha = meta.decode("ascii").split(" ")
            rel = path.decode("utf-8")
        except (ValueError, UnicodeError):
            continue
        if kind == "blob":
            items.append((rel, sha))
    return items


def _cat(repo, sha):
    proc = _git(repo, "cat-file", "blob", sha)
    return proc.stdout


def _hash_object(repo, data):
    proc = _git(repo, "hash-object", "-w", "--stdin", input=data)
    return proc.stdout.decode("ascii", "replace").strip()


def _desired_map(repo, parent, overlay, keep):
    desired = {}
    if parent:
        for rel, sha in _ls_tree(repo, parent):
            data = _cat(repo, sha)
            if keep(rel, data):
                desired[rel] = data
    for rel, data in overlay.items():
        if keep(rel, data):
            desired[rel] = data
        elif rel in desired and not keep(rel, data):
            del desired[rel]
    if len(desired) > MAX_FILES:
        raise BrainSyncError("too_many_files")
    return desired


def _write_tree(repo, desired):
    vault = repo / "vault"
    state = repo / "state"
    for folder in (vault, state):
        if folder.is_symlink() or _reparse(folder):
            raise BrainSyncError("symlink")
        if folder.exists():
            shutil.rmtree(folder)
    for rel in sorted(desired):
        dest = _contained(repo, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(desired[rel])
    _git(repo, "read-tree", "--empty")
    for rel in sorted(desired):
        _git(repo, "add", "--", rel)
    proc = _git(repo, "write-tree")
    return proc.stdout.decode("ascii", "replace").strip()


def _ancestor(repo, older, newer):
    if older == newer:
        return True
    proc = _git(repo, "merge-base", "--is-ancestor", older, newer, check=False)
    return proc.returncode == 0


def _point(repo, ref, sha, parent):
    current = _rev(repo, ref)
    if current == sha:
        return ref
    if current is None:
        _git(repo, "update-ref", ref, sha, _ZERO)
        return ref
    if parent is not None and current == parent and _ancestor(repo, current, sha):
        _git(repo, "update-ref", ref, sha, current)
        return ref
    if _ancestor(repo, current, sha):
        _git(repo, "update-ref", ref, sha, current)
        return ref
    preserved = ref + "-preserved-" + sha[:12]
    if _rev(repo, preserved) != sha:
        existing = _rev(repo, preserved)
        if existing is None:
            _git(repo, "update-ref", preserved, sha, _ZERO)
    return preserved


def _commit(repo, ref, tree, parent):
    if parent and _tree_of(repo, parent) == tree:
        return parent, ref
    cmd = ["commit-tree", tree]
    if parent:
        cmd += ["-p", parent]
    proc = _git(repo, *cmd, input=b"brainsync snapshot\n")
    sha = proc.stdout.decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BrainSyncError("git_failed")
    pointed = _point(repo, ref, sha, parent)
    return sha, pointed


def _history_unsafe(repo, ref, allow):
    """True when any reachable commit or blob must not be pushed."""
    if _rev(repo, ref) is None:
        return False
    commits = _git(repo, "rev-list", ref).stdout.splitlines()
    for line in commits:
        sha = line.decode("ascii", "replace").strip()
        body = _git(repo, "cat-file", "commit", sha).stdout
        if _content_secret(body):
            return True
    for rel, sha in _objects(repo, ref):
        if not _remote_keep(rel, _cat(repo, sha), allow):
            return True
    return False


def _objects(repo, ref):
    proc = _git(repo, "rev-list", "--objects", ref)
    found = []
    for line in proc.stdout.splitlines():
        parts = line.decode("utf-8", "replace").split(" ", 1)
        if len(parts) != 2:
            continue
        sha, rel = parts
        if re.fullmatch(r"[0-9a-f]{40}", sha) and rel:
            kind = _git(repo, "cat-file", "-t", sha).stdout.decode("ascii", "replace").strip()
            if kind == "blob":
                found.append((rel, sha))
    return found


def _exclusion_info(items):
    """Counts are exact. ``sample`` is capped; it is not the count."""
    blocking = [item for item in items if item.get("selected")]
    reasons = {}
    for item in items:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {
        "count": len(items),
        "blocking": len(blocking),
        "policy": len(items) - len(blocking),
        "reasons": reasons,
        "scope": {"reserved_root": sum(1 for item in items if item["reason"] == "reserved_root")},
        "sample": [{"path": item["path"], "reason": item["reason"]} for item in items[:EXCLUSION_SAMPLE]],
    }


def _vault_fingerprint(overlay):
    digest = hashlib.sha256()
    for rel in sorted(overlay):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(overlay[rel]).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _collect_vault(vault):
    """Safe vault files keyed by ``vault/<rel>``. Exclusions are names, not contents.

    Reserved root ``.obsidian``, ``.git``, and ``.jarvis`` directories stay out by
    policy and are counted in ``scope``. A selected note skipped as too large,
    unreadable, a bad path, a secret, or a symlink is blocking: handoff must not
    report success while that file is missing.
    """
    root = vault.resolve()
    overlay = {}
    excluded = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if name in SKIP_COMPONENTS:
                reason = "reserved_root" if rel == name else "reserved"
                excluded.append({"path": rel, "reason": reason, "selected": False})
                continue
            if path.is_symlink() or _reparse(path):
                excluded.append({"path": rel, "reason": "symlink", "selected": True})
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            transport = "vault/" + rel
            if path.is_symlink() or _reparse(path):
                excluded.append({"path": rel, "reason": "symlink", "selected": True})
                continue
            if not _structural(transport):
                excluded.append({"path": rel, "reason": "bad_path", "selected": True})
                continue
            try:
                size = path.stat().st_size
            except OSError:
                excluded.append({"path": rel, "reason": "unreadable", "selected": True})
                continue
            if size > MAX_FILE_BYTES:
                excluded.append({"path": rel, "reason": "too_large", "selected": True})
                continue
            try:
                data = path.read_bytes()
            except OSError:
                excluded.append({"path": rel, "reason": "unreadable", "selected": True})
                continue
            if not _local_keep(transport, data):
                excluded.append({"path": rel, "reason": "secret", "selected": True})
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError:
                excluded.append({"path": rel, "reason": "bad_path", "selected": True})
                continue
            overlay[transport] = data
    return overlay, _exclusion_info(excluded)


def _snapshot_refs(repo, device, overlay, memory, allow):
    """Commit the private local ref and the publishable ref. Return the ref to push."""
    if not DEVICE_ID.fullmatch(device):
        raise BrainSyncError("bad_device")
    publish = f"refs/heads/brainsync/{device}"
    local = f"refs/heads/brainsync-local/{device}"
    parent = _rev(repo, publish)
    dirty = bool(parent and _history_unsafe(repo, publish, allow))
    publish_overlay = dict(overlay)
    if memory and MEMORY_GIT_PATH in set(allow):
        publish_overlay[MEMORY_GIT_PATH] = memory
    publish_map = _desired_map(repo, None if dirty else parent, publish_overlay,
                               lambda path, data: _remote_keep(path, data, allow))
    # Newest safe historical blob wins. A sanitised tip keeps prior vault bytes
    # even when the live file was removed or a later commit introduced a secret.
    if dirty and parent:
        listed = _git(repo, "rev-list", parent).stdout.splitlines()
        for line in listed:
            commit = line.decode("ascii", "replace").strip()
            for rel, sha in _ls_tree(repo, commit):
                if rel in publish_map:
                    continue
                data = _cat(repo, sha)
                if _remote_keep(rel, data, allow):
                    publish_map[rel] = data
    tree = _write_tree(repo, publish_map)
    if dirty:
        safe = f"refs/heads/brainsync/{device}-sanitized"
        existing = _rev(repo, safe)
        if existing and _tree_of(repo, existing) == tree and not _history_unsafe(repo, safe, allow):
            push_ref = safe
        else:
            sha, push_ref = _commit(repo, safe, tree, None)
            if _history_unsafe(repo, push_ref, allow):
                push_ref = None
    else:
        _commit(repo, publish, tree, parent)
        push_ref = None if _history_unsafe(repo, publish, allow) else publish
    local_overlay = dict(overlay)
    if memory:
        local_overlay[MEMORY_GIT_PATH] = memory
    local_parent = _rev(repo, local)
    local_map = _desired_map(repo, local_parent, local_overlay, lambda path, data: _local_keep(path, data))
    local_tree = _write_tree(repo, local_map)
    _commit(repo, local, local_tree, local_parent)
    return push_ref


def _validate_remote_history(repo, ref, allow):
    if ref is None or _history_unsafe(repo, ref, allow):
        raise BrainSyncError("history_unsafe")


def _push(repo, remote, ref, allow):
    _validate_remote_history(repo, ref, allow)
    proc = _git(repo, "push", "--no-tags", remote, ref + ":" + ref, check=False)
    if proc.returncode != 0:
        return {"pushed": False, "preserved": True}
    return {"pushed": True, "preserved": False}


def _fetch(repo, remote):
    proc = _git(repo, "ls-remote", "--refs", remote, "refs/heads/brainsync/*", check=False)
    if proc.returncode != 0:
        raise BrainSyncError("fetch_failed")
    preserved = False
    for line in proc.stdout.splitlines():
        try:
            sha, ref = line.decode("ascii").split()
        except (UnicodeError, ValueError):
            continue
        match = PUBLISH_REF.match(ref)
        if not match or not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        name = match.group(2)
        dest = f"refs/remotes/brainsync/{name}"
        got = _git(repo, "fetch", "--no-tags", remote, ref + ":" + dest, check=False)
        if got.returncode != 0:
            preserved = True
            alt = f"refs/remotes/brainsync/{name}-preserved-{sha[:12]}"
            _git(repo, "fetch", "--no-tags", remote, ref + ":" + alt, check=False)
    return preserved


def _iter_publish_refs(repo):
    proc = _git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/brainsync",
                "refs/remotes/brainsync", check=False)
    if proc.returncode != 0:
        return []
    refs = []
    for line in proc.stdout.splitlines():
        try:
            sha, ref = line.decode("utf-8").split(" ", 1)
        except (UnicodeError, ValueError):
            continue
        if PUBLISH_REF.match(ref) and re.fullmatch(r"[0-9a-f]{40}", sha):
            refs.append(ref)
    return refs


def _conflict_path(vault, rel, data):
    pure = PurePosixPath(rel)
    suffix = pure.suffix if len(pure.suffix) <= 16 else ""
    digest = hashlib.sha256(data).hexdigest()
    name = f"conflict-{digest}{suffix}"
    return vault / PurePosixPath(pure.parent.as_posix()) / name if pure.parent.as_posix() != "." \
        else vault / name


def _materialize(vault, repo, allow):
    created, conflicts, skipped = [], [], []
    seen = set()
    planned = []
    for ref in _iter_publish_refs(repo):
        for rel, sha in _ls_tree(repo, ref):
            if not rel.startswith("vault/"):
                continue
            vault_rel = rel[len("vault/"):]
            data = _cat(repo, sha)
            remote_ok = _remote_keep(rel, data, allow)
            local_ok = _local_keep(rel, data)
            if not _structural(rel) or (not remote_ok and not local_ok):
                skipped.append(vault_rel)
                continue
            if _name_secret(rel) or _content_secret(data):
                if rel not in set(allow):
                    skipped.append(vault_rel)
                    continue
            key = (vault_rel, hashlib.sha256(data).hexdigest())
            if key in seen:
                continue
            seen.add(key)
            planned.append((vault_rel, data))
    if len(planned) > MAX_FILES:
        raise BrainSyncError("too_many_files")
    for vault_rel, data in planned:
        if not _structural("vault/" + vault_rel):
            skipped.append(vault_rel)
            continue
        try:
            target = _contained(vault, vault_rel)
        except BrainSyncError:
            skipped.append(vault_rel)
            continue
        if target.exists() or target.is_symlink():
            if target.is_symlink() or _reparse(target) or not target.is_file():
                skipped.append(vault_rel)
                continue
            try:
                current = target.read_bytes()
            except OSError:
                skipped.append(vault_rel)
                continue
            if current == data:
                continue
            side = _conflict_path(vault, vault_rel, data)
            placed = _exclusive_bytes(side, data)
            if placed in ("created", "same"):
                conflicts.append(str(side.relative_to(vault).as_posix()))
            else:
                skipped.append(vault_rel)
            continue
        # Refuse to create a file through a symlinked parent.
        try:
            target.parent.resolve().relative_to(vault.resolve())
        except ValueError:
            skipped.append(vault_rel)
            continue
        placed = _exclusive_bytes(target, data)
        if placed == "created":
            created.append(vault_rel)
        elif placed != "same":
            skipped.append(vault_rel)
    return {"created": created, "conflicts": conflicts, "skipped": skipped[:50]}


def _prepare(config, base):
    cfg = _settings(config)
    if cfg is None:
        raise BrainSyncError("bad_config")
    if not cfg["enabled"]:
        raise BrainSyncError("disabled")
    role, err = _role(config)
    if err:
        raise BrainSyncError(err)
    vault = _vault_root(config, base)
    repo = _repo_path(cfg, base)
    _reject_repo(repo, base, vault)
    remote = _remote_arg(cfg, base, vault)
    return cfg, role, vault, repo, remote


def _sync_unlocked(config, base):
    cfg, (local, main), vault, repo, remote = _prepare(config, base)
    if local != main:
        raise BrainSyncError("not_main")
    if _frozen(base, local):
        raise BrainSyncError("handoff_readonly")
    _init_repo(repo)
    # Fetch and land the remote union before this device's commit, so the
    # published snapshot contains the files just received. A second materialize
    # after the commit restores vault bytes the sanitized tip still retains.
    preserved = False
    prior = {"created": [], "conflicts": [], "skipped": []}
    if remote:
        preserved = _fetch(repo, remote)
        prior = _materialize(vault, repo, cfg["allow"])
    overlay, excluded = _collect_vault(vault)
    memory_path = _state_dir(base) / "memory.sqlite3"
    memory = b""
    if memory_path.is_file() and not memory_path.is_symlink() and not _reparse(memory_path):
        memory = snapshot_memory(memory_path)
    push_ref = _snapshot_refs(repo, local, overlay, memory or None, cfg["allow"])
    pushed = False
    if remote:
        if push_ref:
            outcome = _push(repo, remote, push_ref, cfg["allow"])
            pushed = outcome["pushed"]
            preserved = preserved or outcome["preserved"]
        else:
            preserved = True
    applied = _materialize(vault, repo, cfg["allow"])
    applied = {
        "created": list(dict.fromkeys(prior["created"] + applied["created"])),
        "conflicts": list(dict.fromkeys(prior["conflicts"] + applied["conflicts"])),
        "skipped": list(dict.fromkeys(prior["skipped"] + applied["skipped"]))[:50],
    }
    status = "history_preserved" if preserved and not pushed else ("synced" if remote else "local_only")
    if remote and pushed and not preserved:
        status = "synced"
    return _result(True, status, None, enabled=True, device=local, pushed=pushed,
                   preserved=preserved, excluded=excluded, **applied)


def _status_repo(repo):
    if not repo.exists():
        return {"repo_present": False, "publish_refs": []}
    if not (repo / ".git").is_dir():
        return {"repo_present": False, "publish_refs": []}
    try:
        refs = _iter_publish_refs(repo)
    except BrainSyncError:
        refs = []
    return {"repo_present": True, "publish_refs": refs}


def status(config, base):
    """Read-only report. Creates nothing and does not fetch, commit, or push."""
    base = Path(base)
    cfg = _settings(config)
    if cfg is None:
        return _result(False, "bad_config", "bad_config", enabled=False, readonly=True)
    role, role_error = (None, None)
    try:
        role, role_error = _role(config)
    except BrainSyncError as exc:
        role_error = exc.code
    marker = None
    try:
        marker = _read_marker(base)
    except OSError:
        marker = {"corrupt": True, "readonly": True}
    repo_info = {"repo_present": False, "publish_refs": []}
    repo_error = None
    if cfg["enabled"]:
        try:
            vault = None
            try:
                vault = _vault_root(config, base)
            except BrainSyncError:
                vault = None
            repo = _repo_path(cfg, base)
            _reject_repo(repo, base, vault)
            repo_info = _status_repo(repo)
        except BrainSyncError as exc:
            repo_error = exc.code
    remote_configured = bool(cfg["remote"])
    return _result(True, "disabled" if not cfg["enabled"] else (repo_error or "ready"),
                   repo_error, enabled=cfg["enabled"], readonly=True,
                   device=None if role is None else role[0],
                   main=None if role is None else role[1],
                   is_main=bool(role and role[0] == role[1]),
                   role_error=role_error, handoff_readonly=bool(marker and marker.get("readonly")),
                   handoff_old_main=None if not marker else marker.get("old_main"),
                   remote_configured=remote_configured,
                   allowlist=list(cfg["allow"]), **repo_info)


def _fail(exc, enabled=True):
    extra = dict(getattr(exc, "extra", None) or {})
    detail = getattr(exc, "detail", "")
    if detail:
        extra["detail"] = detail
    return _result(False, exc.code, exc.code, enabled=enabled, **extra)


def sync(config, base):
    """Snapshot this MAIN and, when a remote is configured, exchange ``brainsync/*``."""
    try:
        cfg = _settings(config)
        if cfg is None:
            return _result(False, "bad_config", "bad_config")
        if not cfg["enabled"]:
            return _result(False, "disabled", "disabled", enabled=False)
        role, err = _role(config)
        if err:
            return _result(False, err, err, enabled=True)
        if role[0] != role[1]:
            return _result(False, "not_main", "not_main", enabled=True)
        if _frozen(base, role[0]):
            return _result(False, "handoff_readonly", "handoff_readonly", enabled=True)
        with _Lock(base):
            return _sync_unlocked(config, base)
    except BrainSyncError as exc:
        return _fail(exc)


def _bundle_bytes(header, git_bundle, memory):
    def part(blob):
        if len(blob) > MAX_BUNDLE_BYTES:
            raise BrainSyncError("too_large")
        return len(blob).to_bytes(8, "big") + blob
    body = _MAGIC + part(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    body += part(git_bundle) + part(memory)
    if len(body) > MAX_BUNDLE_BYTES:
        raise BrainSyncError("too_large")
    return body


def _unbundle(blob):
    if not isinstance(blob, (bytes, bytearray)) or not blob.startswith(_MAGIC):
        raise BrainSyncError("bad_bundle")
    index = len(_MAGIC)

    def take():
        nonlocal index
        if index + 8 > len(blob):
            raise BrainSyncError("bad_bundle")
        size = int.from_bytes(blob[index:index + 8], "big")
        index += 8
        if size > MAX_BUNDLE_BYTES or index + size > len(blob):
            raise BrainSyncError("bad_bundle")
        chunk = bytes(blob[index:index + size])
        index += size
        return chunk

    try:
        header = json.loads(take().decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BrainSyncError("bad_bundle") from exc
    git_bundle = take()
    memory = take()
    if index != len(blob) or not isinstance(header, dict):
        raise BrainSyncError("bad_bundle")
    return header, git_bundle, memory


def _git_bundle(repo, ref):
    state_parent = Path(repo).parent
    handle = tempfile.NamedTemporaryFile(prefix="brainsync-", suffix=".bundle", dir=state_parent, delete=False)
    name = handle.name
    handle.close()
    try:
        proc = _git(repo, "bundle", "create", name, ref, check=False)
        if proc.returncode != 0:
            raise BrainSyncError("bundle_failed")
        data = Path(name).read_bytes()
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    if len(data) > MAX_BUNDLE_BYTES:
        raise BrainSyncError("too_large")
    return data


def _import_git_bundle(repo, blob):
    _init_repo(repo)
    handle = tempfile.NamedTemporaryFile(prefix="brainsync-in-", suffix=".bundle",
                                        dir=Path(repo).parent, delete=False)
    name = handle.name
    try:
        handle.write(blob)
        handle.close()
        listed = _git(None, "bundle", "list-heads", name, check=False)
        if listed.returncode != 0:
            raise BrainSyncError("bad_bundle")
        heads = []
        for line in listed.stdout.splitlines():
            try:
                sha, ref = line.decode("ascii").split()
            except (UnicodeError, ValueError):
                raise BrainSyncError("bad_bundle")
            if not PUBLISH_REF.match(ref):
                raise BrainSyncError("bad_bundle")
            heads.append((sha, ref))
        if not heads:
            raise BrainSyncError("bad_bundle")
        for _sha, ref in heads:
            name_part = ref.rsplit("/", 1)[-1]
            dest = f"refs/remotes/brainsync/{name_part}"
            got = _git(repo, "fetch", "--no-tags", name, ref + ":" + dest, check=False)
            if got.returncode != 0:
                alt = f"refs/remotes/brainsync/{name_part}-preserved-{_sha[:12]}"
                again = _git(repo, "fetch", "--no-tags", name, ref + ":" + alt, check=False)
                if again.returncode != 0:
                    raise BrainSyncError("history_preserved_unfetched")
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _known_devices(config, *device_ids):
    devices = config.get("devices") if isinstance(config, dict) else None
    if not isinstance(devices, dict):
        raise BrainSyncError("bad_device")
    for device_id in device_ids:
        if device_id not in devices:
            raise BrainSyncError("bad_device")


def _header_ok(header, local, expect_new):
    if header.get("schema") != SCHEMA or header.get("quiesced") is not True:
        return False
    old_main, new_main = header.get("old_main"), header.get("new_main")
    if not isinstance(old_main, str) or not isinstance(new_main, str):
        return False
    if not DEVICE_ID.fullmatch(old_main) or not DEVICE_ID.fullmatch(new_main) or old_main == new_main:
        return False
    if local != new_main or new_main != expect_new:
        return False
    return True


def _receive_unlocked(config, base, blob):
    cfg, (local, _main), vault, repo, _remote = _prepare(config, base)
    header, git_bundle, memory = _unbundle(blob)
    if not _header_ok(header, local, local):
        raise BrainSyncError("handoff_identity")
    _known_devices(config, header["old_main"], header["new_main"])
    digest = hashlib.sha256(memory).hexdigest()
    if header.get("memory_sha256") != digest:
        raise BrainSyncError("bad_bundle")
    if memory and not memory.startswith(b"SQLite format 3\x00"):
        raise BrainSyncError("bad_bundle")
    if memory and _db_nonempty(memory):
        dest = _state_dir(base) / "memory.sqlite3"
        if dest.exists() and not dest.is_symlink():
            try:
                existing = snapshot_memory(dest)
            except BrainSyncError:
                existing = b""
            if existing and existing != memory and _db_nonempty(existing):
                placed = _exclusive_bytes(
                    _state_dir(base) / f"memory.sqlite3.conflict-{digest}", memory)
                if placed == "exists":
                    raise BrainSyncError("memory_conflict")
                raise BrainSyncError("memory_conflict")
    _import_git_bundle(repo, git_bundle)
    applied = _materialize(vault, repo, cfg["allow"])
    # A skipped path was selected and not written (symlink, bad path, unreadable).
    # Leave any exclusive creates in place, but do not install memory or look fresh.
    if applied["skipped"]:
        return _result(False, "materialize_incomplete", "materialize_incomplete",
                       enabled=True, device=local, **applied)
    memory_status = "missing"
    if memory:
        installed = _install_memory(base, memory)
        if not installed["ok"]:
            installed["materialized"] = applied
            return installed
        memory_status = installed["status"]
    return _result(True, "imported", None, enabled=True, device=local,
                   memory=memory_status, **applied)


def receive(config, base, blob):
    """Import a handoff bundle. Requires brainsync.enabled and local == bundle new_main."""
    try:
        cfg = _settings(config)
        if cfg is None:
            return _result(False, "bad_config", "bad_config")
        if not cfg["enabled"]:
            return _result(False, "disabled", "disabled", enabled=False)
        if not isinstance(blob, (bytes, bytearray)):
            return _result(False, "bad_bundle", "bad_bundle")
        with _Lock(base):
            return _receive_unlocked(config, base, bytes(blob))
    except BrainSyncError as exc:
        return _result(False, exc.code, exc.code, enabled=True)


def _write_private(path, data, mode):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "nt":
        flags |= os.O_BINARY
    fd = os.open(tmp, flags, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, mode)


def _remember_pending(base, old_main, new_main, bundle, memory_sha256, vault_sha256):
    digest = hashlib.sha256(bundle).hexdigest()
    _write_private(_pending_bin(base), bundle, 0o600)
    meta = {"schema": EXPORT_SCHEMA, "old_main": old_main, "new_main": new_main,
            "bundle_sha256": digest, "memory_sha256": memory_sha256,
            "vault_sha256": vault_sha256}
    _write_private(_pending_json(base), json.dumps(meta, sort_keys=True).encode("utf-8"), 0o600)
    return digest


def _source_unchanged(config, base, pending):
    """True when live memory and the vault overlay still match the pending bundle."""
    remembered_memory = pending.get("memory_sha256")
    remembered_vault = pending.get("vault_sha256")
    if not isinstance(remembered_memory, str) or not isinstance(remembered_vault, str):
        return False
    memory, _reason = _stable_memory(_state_dir(base) / "memory.sqlite3", None)
    if not isinstance(memory, (bytes, bytearray)):
        return False
    if hashlib.sha256(memory).hexdigest() != remembered_memory:
        return False
    overlay, _excluded = _collect_vault(_vault_root(config, base))
    return _vault_fingerprint(overlay) == remembered_vault


def _make_bundle(config, base, old_main, new_main, between):
    cfg, (local, main), vault, repo, _remote = _prepare(config, base)
    _known_devices(config, old_main, new_main)
    if local != old_main or main not in (old_main, new_main) or old_main == new_main:
        raise BrainSyncError("handoff_identity")
    if not DEVICE_ID.fullmatch(new_main):
        raise BrainSyncError("bad_device")
    marker = _read_marker(base)
    if marker and marker.get("old_main") not in (None, old_main):
        raise BrainSyncError("handoff_conflict")
    if marker and marker.get("old_main") == old_main:
        if marker.get("new_main") != new_main:
            raise BrainSyncError("handoff_conflict")
        pending = _read_json(_pending_json(base))
        blob_path = _pending_bin(base)
        if not pending or pending.get("new_main") != new_main or not blob_path.is_file():
            raise BrainSyncError("frozen_bundle_missing")
        blob = blob_path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != pending.get("bundle_sha256"):
            raise BrainSyncError("frozen_bundle_missing")
        return blob, "frozen_resend"
    if _frozen(base, local):
        raise BrainSyncError("handoff_readonly")
    memory, reason = _stable_memory(_state_dir(base) / "memory.sqlite3", between)
    if reason == "source_not_quiesced" or memory is None:
        raise BrainSyncError(reason or "source_not_quiesced")
    _init_repo(repo)
    overlay, excluded = _collect_vault(vault)
    # Selected notes dropped as too large, symlink, unreadable, bad path, or secret
    # must not become a bundle or a pending file. Handoff then returns ok false.
    if excluded.get("blocking", 0) > 0:
        raise BrainSyncError("omitted_notes")
    memory_sha256 = hashlib.sha256(memory).hexdigest()
    vault_sha256 = _vault_fingerprint(overlay)
    push_ref = _snapshot_refs(repo, local, overlay, memory or None, cfg["allow"])
    bundle_ref = push_ref or f"refs/heads/brainsync/{local}-sanitized"
    if _rev(repo, bundle_ref) is None or _history_unsafe(repo, bundle_ref, cfg["allow"]):
        # The publish ref itself is safe when history was clean.
        if push_ref is None and not _history_unsafe(repo, f"refs/heads/brainsync/{local}", cfg["allow"]):
            bundle_ref = f"refs/heads/brainsync/{local}"
        else:
            raise BrainSyncError("history_unsafe")
    git_blob = _git_bundle(repo, bundle_ref)
    header = {"schema": SCHEMA, "old_main": old_main, "new_main": new_main, "device": local,
              "quiesced": True, "memory_sha256": memory_sha256}
    bundle = _bundle_bytes(header, git_blob, memory)
    _remember_pending(base, old_main, new_main, bundle, memory_sha256, vault_sha256)
    return bundle, "exported"


def export_result(config, base, request, *, between_reads=None):
    """Build a base64 bundle for ``new_main``. Stdout of the hidden CLI is this payload."""
    try:
        cfg = _settings(config)
        if cfg is None:
            return _result(False, "bad_config", "bad_config")
        if not cfg["enabled"]:
            return _result(False, "disabled", "disabled", enabled=False)
        if not isinstance(request, (bytes, bytearray)) or len(request) > 4096:
            return _result(False, "bad_request", "bad_request")
        try:
            spec = json.loads(request.decode("utf-8"))
        except (UnicodeError, ValueError):
            return _result(False, "bad_request", "bad_request")
        if not isinstance(spec, dict) or spec.get("schema") != EXPORT_SCHEMA:
            return _result(False, "bad_request", "bad_request")
        old_main, new_main = spec.get("old_main"), spec.get("new_main")
        if not isinstance(old_main, str) or not isinstance(new_main, str):
            return _result(False, "bad_request", "bad_request")
        with _Lock(base):
            bundle, how = _make_bundle(config, base, old_main, new_main, between_reads)
        payload = _b64(bundle)
        return _result(True, how, None, payload=payload, enabled=True)
    except BrainSyncError as exc:
        return _result(False, exc.code, exc.code, enabled=True)


def _b64(blob):
    import base64
    return base64.b64encode(blob).decode("ascii") + "\n"


def _write_marker(base, old_main, new_main, bundle_sha):
    path = _marker_path(base)
    existing = _read_marker(base)
    payload = {"schema": MARKER_SCHEMA, "readonly": True, "old_main": old_main,
               "new_main": new_main, "bundle_sha256": bundle_sha}
    if existing:
        if existing.get("old_main") == old_main and existing.get("new_main") == new_main:
            return _result(True, "frozen", None)
        return _result(False, "handoff_conflict", "handoff_conflict")
    _write_private(path, json.dumps(payload, sort_keys=True).encode("utf-8"), 0o644)
    if os.name != "nt":
        os.chmod(path, 0o444)
    return _result(True, "frozen", None)


def freeze(config, base, request):
    """Record the read-only handoff marker on the old MAIN after a bundle was accepted."""
    try:
        cfg = _settings(config)
        if cfg is None:
            return _result(False, "bad_config", "bad_config")
        if not cfg["enabled"]:
            return _result(False, "disabled", "disabled", enabled=False)
        spec = json.loads(request.decode("utf-8"))
        if not isinstance(spec, dict) or spec.get("schema") != EXPORT_SCHEMA:
            return _result(False, "bad_request", "bad_request")
        old_main, new_main = spec.get("old_main"), spec.get("new_main")
        digest = spec.get("bundle_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return _result(False, "bad_request", "bad_request")
        role, err = _role(config)
        if err:
            return _result(False, err, err)
        if role[0] != old_main:
            return _result(False, "handoff_identity", "handoff_identity")
        with _Lock(base):
            pending = _read_json(_pending_json(base))
            if not pending or pending.get("bundle_sha256") != digest or pending.get("new_main") != new_main:
                return _result(False, "frozen_bundle_missing", "frozen_bundle_missing")
            existing = _read_marker(base)
            # An identical marker is a resend. Do not re-check the live source, and
            # do not rewrite the marker. _write_marker returns frozen for that pair.
            if existing:
                return _write_marker(base, old_main, new_main, digest)
            if not _source_unchanged(config, base, pending):
                return _result(False, "source_changed", "source_changed")
            return _write_marker(base, old_main, new_main, digest)
    except BrainSyncError as exc:
        return _result(False, exc.code, exc.code)
    except (UnicodeError, ValueError, OSError):
        return _result(False, "bad_request", "bad_request")


class SshBundleTransport:
    """Pinned-SSH bundle transport. Uses ``deploy._remote`` and the peer checkout.

    Callers pass a fixed logical command. ``_call`` checks that allow-list, then
    sends ``<jarvis_dir>/.venv/bin/python`` with that checkout's ``--config`` and
    ``--state``. ``deploy._remote`` decodes stdout as text, so export replies are
    base64. The receive command gets the raw bundle on stdin. Device ids are not
    interpolated. A present but unsafe ``jarvis_dir``, or a Windows peer, is
    ``peer_path_unsafe`` and never falls back to login ``python``. No
    ``jarvis_dir`` keeps the fixed logical command.
    """

    def __init__(self, config, base):
        self.config = config
        self.base = base

    def _call(self, node, command, stdin):
        if command not in _FIXED_COMMANDS or not isinstance(stdin, (bytes, bytearray)):
            return _result(False, "transport_failed", "transport_failed")
        if not isinstance(node, str) or not DEVICE_ID.fullmatch(node):
            return _result(False, "bad_device", "bad_device")
        remote_command = _peer_brain_command(self.config, node, command)
        if remote_command is None:
            return _result(False, "peer_path_unsafe", "peer_path_unsafe")
        try:
            from .deploy import _remote
            code, out, _err = _remote(self.config, str(_state_dir(self.base)), node, remote_command,
                                      stdin=bytes(stdin), timeout=180)
        except (ConnectionError, OSError, subprocess.SubprocessError, TimeoutError):
            return _result(False, "source_offline", "source_offline")
        except (RuntimeError, ValueError):
            # _remote raises ValueError when the peer is not an SSH device, and
            # ConnectionError when no pinned address answers. RuntimeError covers a
            # broken ssh client. None of these is a completed handoff.
            return _result(False, "source_offline", "source_offline")
        if code != 0:
            return _result(False, "transport_failed", "transport_failed")
        return _result(True, "transported", None, stdout=out)

    def push(self, node, bundle):
        result = self._call(node, RECEIVE_COMMAND, bundle)
        if not result["ok"]:
            return result
        try:
            body = json.loads(result["stdout"] or "")
        except ValueError:
            return _result(False, "transport_failed", "transport_failed")
        if not isinstance(body, dict) or body.get("ok") is not True:
            error = body.get("error") if isinstance(body, dict) else "transport_failed"
            return _result(False, error or "transport_failed", error or "transport_failed")
        return body

    def pull(self, node, request):
        result = self._call(node, EXPORT_COMMAND, request)
        if not result["ok"]:
            return result
        import base64
        try:
            text = "".join((result.get("stdout") or "").split())
            blob = base64.b64decode(text.encode("ascii"), validate=True)
        except (UnicodeError, ValueError, AttributeError):
            return _result(False, "transport_failed", "transport_failed")
        return _result(True, "exported", None, bundle=blob)

    def freeze(self, node, request):
        result = self._call(node, FREEZE_COMMAND, request)
        if not result["ok"]:
            return result
        try:
            body = json.loads(result["stdout"] or "")
        except ValueError:
            return _result(False, "source_not_frozen", "source_not_frozen")
        if not isinstance(body, dict) or body.get("ok") is not True:
            return _result(False, "source_not_frozen", "source_not_frozen")
        return body


_OS_HINTS = frozenset({"os", "platform", "system"})
_BRAIN_ACTIONS = frozenset({"receive", "export", "freeze"})


def _peer_windows(device):
    """True when ``os``, ``platform``, or ``system`` says Windows."""
    if not isinstance(device, dict):
        return False
    for key, value in device.items():
        if isinstance(key, str) and key.lower() in _OS_HINTS and isinstance(value, str):
            if "win" in value.lower():
                return True
    return False


def _safe_jarvis_dir(value):
    """POSIX absolute checkout path that is safe to quote into a remote command."""
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    if "\x00" in value or "\n" in value or "\r" in value:
        return False
    return all(part != ".." for part in value.split("/"))


def _peer_brain_command(config, node, command):
    """Quoted peer-venv command, the fixed command, or None to fail closed.

    No ``jarvis_dir`` keeps the logical command so an in-process stand-in can
    dispatch it. A directory that is present but not a safe POSIX path, or a
    Windows hint, does not fall back to login ``python``.
    """
    devices = config.get("devices") if isinstance(config, dict) else None
    device = devices.get(node) if isinstance(devices, dict) else None
    if not isinstance(device, dict) or _peer_windows(device):
        return None
    if "jarvis_dir" not in device or device.get("jarvis_dir") is None:
        return command
    root = device.get("jarvis_dir")
    if not _safe_jarvis_dir(root):
        return None
    action = command.rsplit(" ", 1)[-1]
    if action not in _BRAIN_ACTIONS:
        return None
    python = shlex.quote(root + "/.venv/bin/python")
    peer_config = shlex.quote(root + "/jarvis.config.json")
    peer_state = shlex.quote(root + "/.jarvis")
    return f"{python} -m jarvis --config {peer_config} --state {peer_state} brain {action}"


def _default_transport(config, base):
    return SshBundleTransport(config, base)


def handoff(old_main, new_main, config, base, *, transport=None, between_reads=None):
    """Move the quiesced brain from ``old_main`` to ``new_main``.

    Returns ``{ok, status, error}``. ``ok`` is true only after the destination has the
    snapshot and the source carries the read-only marker. WS1 must already have fenced
    writers; a snapshot that changes between two backups is refused and is not called latest.
    """
    try:
        if not isinstance(old_main, str) or not isinstance(new_main, str) \
                or not DEVICE_ID.fullmatch(old_main) or not DEVICE_ID.fullmatch(new_main) \
                or old_main == new_main:
            return _result(False, "bad_device", "bad_device")
        cfg = _settings(config)
        if cfg is None:
            return _result(False, "bad_config", "bad_config")
        if not cfg["enabled"]:
            return _result(False, "disabled", "disabled", enabled=False)
        try:
            _known_devices(config, old_main, new_main)
        except BrainSyncError as exc:
            return _result(False, exc.code, exc.code)
        role, err = _role(config)
        if err:
            return _result(False, err, err)
        local = role[0]
        adapter = transport if transport is not None else _default_transport(config, base)
        for method in ("push", "pull", "freeze"):
            if not callable(getattr(adapter, method, None)):
                return _result(False, "bad_transport", "bad_transport")
        if local == old_main:
            return _handoff_push(old_main, new_main, config, base, adapter, between_reads)
        if local == new_main:
            return _handoff_pull(old_main, new_main, config, base, adapter, between_reads)
        return _result(False, "not_participant", "not_participant")
    except BrainSyncError as exc:
        return _result(False, exc.code, exc.code)


def _handoff_push(old_main, new_main, config, base, adapter, between):
    with _Lock(base):
        bundle, _how = _make_bundle(config, base, old_main, new_main, between)
    delivered = adapter.push(new_main, bundle)
    if not isinstance(delivered, dict) or delivered.get("ok") is not True:
        error = delivered.get("error") if isinstance(delivered, dict) else "transport_failed"
        status = delivered.get("status") if isinstance(delivered, dict) else "transport_failed"
        if error in (None, ""):
            error = "transport_failed"
        return _result(False, status or error, error)
    frozen = _write_marker(base, old_main, new_main, hashlib.sha256(bundle).hexdigest())
    if not frozen["ok"]:
        return frozen
    return _result(True, "handed_off", None, device=old_main, peer=new_main)


def _handoff_pull(old_main, new_main, config, base, adapter, between):
    if between is not None:
        # The quiesce window is on the source. A pull cannot sample the source's
        # database between backups; the source export does that. Refuse a local hook
        # here so a caller cannot treat it as evidence the source was quiet.
        return _result(False, "source_not_quiesced", "source_not_quiesced")
    request = json.dumps({"schema": EXPORT_SCHEMA, "old_main": old_main, "new_main": new_main},
                         sort_keys=True).encode("utf-8")
    pulled = adapter.pull(old_main, request)
    if not isinstance(pulled, dict) or pulled.get("ok") is not True or not isinstance(pulled.get("bundle"),
                                                                                     (bytes, bytearray)):
        error = pulled.get("error") if isinstance(pulled, dict) else "source_offline"
        status = pulled.get("status") if isinstance(pulled, dict) else "source_offline"
        return _result(False, status or "source_offline", error or "source_offline")
    with _Lock(base):
        imported = _receive_unlocked(config, base, bytes(pulled["bundle"]))
    if not imported.get("ok"):
        return imported
    digest = hashlib.sha256(bytes(pulled["bundle"])).hexdigest()
    freeze_req = json.dumps({"schema": EXPORT_SCHEMA, "old_main": old_main, "new_main": new_main,
                             "bundle_sha256": digest}, sort_keys=True).encode("utf-8")
    frozen = adapter.freeze(old_main, freeze_req)
    if not isinstance(frozen, dict) or frozen.get("ok") is not True:
        return _result(False, "source_not_frozen", "source_not_frozen")
    return _result(True, "handed_off", None, device=new_main, peer=old_main)
