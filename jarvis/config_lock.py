"""One local lock and one patch step for the managed writers of jarvis.config.json.

Managed writers: the console fleet invite (fleet_management), `jarvis cluster configure`,
`main`, `init`, `invite`, `enroll`, `join` and `leave` (cluster_cli / cluster_setup). They all
take `config_mutation(state)` (the file `<state>/fleet-manage.lock`, the same file the fleet
lock always used) and change the config only through `patch_config`, which:

- re-reads the file as it is on disk now, under the lock;
- runs the caller's `guard(doc)` on that latest document (a role/tunnel check, for example),
  so a decision is never made on a cached copy;
- applies only the caller's change to that document, so another writer's unrelated edit is kept;
- writes a uniquely named backup of the exact bytes it read, then compares the file again
  just before the atomic replace and refuses (`ConfigDrift`) if anything changed it.

What this is not: it is not a lease and it fences no other machine. A person editing the file
in an editor does not take this lock. The compare before the replace narrows that window to
the instant between the compare and the rename; it does not close it. Settings uses this same
lock and patches only the keys it was asked to change, so it does not write an older main or
tunnel back over a managed edit. Same-thread re-entry is deliberate (an invite holds the lock
and calls another managed writer). A settings save on that thread is applied, and the invite
re-reads the file before it issues a code.

The lock is re-entrant per thread (flock is per open file, so a second open in the same thread
would wait on itself), which lets the console invite hold it and call `cluster_setup.invite`,
which takes it again. Different threads and processes are serialised. Receipt read-modify-write
uses this same lock and merges its delta onto the latest receipt file, so a full snapshot
from an earlier read cannot drop a fence another writer stored."""

import contextlib
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

LOCK_NAME = "fleet-manage.lock"
DEFAULT_TIMEOUT = 5.0
_HELD = threading.local()


class ConfigBusy(ValueError):
    """Another managed config change holds the lock. Retryable."""
    code = "busy"


class ConfigUnreadable(ValueError):
    code = "config_unreadable"


class ConfigDrift(ValueError):
    """The file changed between this writer's read and its replace (an editor or an
    unmanaged writer). Nothing was written. Re-run the command."""
    code = "config_changed"


def _key(state):
    return str((Path(state).resolve() / LOCK_NAME))


def held(state):
    """True when this thread holds the managed config lock for `state`."""
    return _key(state) in getattr(_HELD, "keys", ())


@contextlib.contextmanager
def config_mutation(state, timeout=None):
    """Hold the managed config lock for `state`. Re-entrant in the same thread.
    timeout: seconds to wait for another holder (default DEFAULT_TIMEOUT)."""
    key = _key(state)
    keys = getattr(_HELD, "keys", None)
    if keys is None:
        keys = _HELD.keys = set()
    if key in keys:
        yield
        return
    from . import locking  # looked up at call time so tests can shorten the wait
    folder = Path(state)
    folder.mkdir(parents=True, exist_ok=True)
    guard = locking.exclusive_lock(folder / LOCK_NAME,
                                   timeout=DEFAULT_TIMEOUT if timeout is None else timeout)
    try:
        guard.__enter__()
    except locking.LockBusy as exc:
        raise ConfigBusy("Another configuration or fleet change is running. Try again in a few "
                         "seconds.") from exc
    keys.add(key)
    try:
        yield
    finally:
        keys.discard(key)
        guard.__exit__(None, None, None)


def _read(path):
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        raise ConfigUnreadable("The config file can't be read; nothing was changed.") from exc
    if not isinstance(doc, dict):
        raise ConfigUnreadable("The config file isn't a JSON object; nothing was changed.")
    return raw, doc


def read_config(path):
    """The config as it is on disk now. Missing file -> ConfigUnreadable."""
    raw, doc = _read(path)
    if raw is None:
        raise ConfigUnreadable("The config file can't be read; nothing was changed.")
    return doc


def _backup(path, raw, tag, mode):
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{path.name}.pre-{tag}-{stamp}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    target = path.with_name(name)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return target


def patch_config(state, path, change, *, guard=None, backup=None, create=False):
    """Apply `change(doc)` to the config file as it is on disk, under the managed lock.

    change(doc): edit the given (deep-copied) latest document in place, or return a new dict.
    guard(doc): runs on the latest document first; raise to refuse (nothing is written).
    backup: a short tag; when the file changes, its previous bytes are kept as
            `<name>.pre-<tag>-<time>-<ns>-<random>` (never overwrites an earlier backup).
    create: allow a missing file (treated as {}).
    Returns (document as written or as found, changed: bool)."""
    from .fsio import atomic_write
    path = Path(path)
    with config_mutation(state):
        raw, doc = _read(path)
        if raw is None:
            if not create:
                raise ConfigUnreadable("The config file can't be read; nothing was changed.")
            doc = {}
        if guard is not None:
            guard(copy.deepcopy(doc))
        work = copy.deepcopy(doc)
        result = change(work)
        new = work if result is None else result
        if not isinstance(new, dict):
            raise TypeError("A config change must produce a JSON object.")
        if new == doc and raw is not None:
            return new, False
        blob = json.dumps(new, indent=2)
        mode = (path.stat().st_mode & 0o777) if raw is not None else 0o600
        if raw is not None and backup:
            _backup(path, raw, backup, mode)
        # Compare-and-swap against a writer that does not take this lock (an editor).
        now, _ = _read(path)
        if now != raw:
            raise ConfigDrift("The config file changed while this change was being made "
                              "(an editor or another tool). Nothing was written; run it again.")
        atomic_write(path, blob, mode=mode)
        return new, True


_MISSING = object()


def merge_changes(doc, before, after, where=""):
    """Apply to `doc` (the file as it is now) only what differs between `before` (the copy the
    caller decided on) and `after` (what it wants). Keys the caller did not change keep their
    current value on disk, so another writer's unrelated edit survives. A key the caller
    changes that someone else has changed too (present on disk with a third value) is a
    conflict: ConfigDrift, nothing is written."""
    for key in set(before) | set(after):
        old, new = before.get(key, _MISSING), after.get(key, _MISSING)
        if old == new:
            continue
        cur = doc.get(key, _MISSING)
        name = f"{where}.{key}" if where else key
        if isinstance(old, dict) and isinstance(new, dict) and isinstance(cur, dict):
            merge_changes(cur, old, new, name)
            continue
        if cur is not _MISSING and cur != old and cur != new:
            raise ConfigDrift(f"{name} was changed by someone else while this ran. Nothing was "
                              "written; run it again.")
        if new is _MISSING:
            doc.pop(key, None)
        else:
            doc[key] = copy.deepcopy(new)
    return doc


def save_delta(state, path, before, after, *, guard=None, backup=None, create=True):
    """patch_config with merge_changes: write what changed from `before` to `after` onto the
    latest file, under the managed lock."""
    def change(doc):
        if not doc:  # no file yet (or an empty one): the caller's whole document
            doc.update(copy.deepcopy(after))
            return
        merge_changes(doc, before or {}, after)
        if guard is not None:
            guard(copy.deepcopy(doc))
    return patch_config(state, path, change, backup=backup, create=create)
