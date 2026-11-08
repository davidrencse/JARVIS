"""Atomic file writes, shared. Each writer gets its own temporary name next to the target,
so two writers at once (a console sync and the nightly timer) never share one half-written
file, and a crash leaves the old file whole."""

import os
import uuid
from pathlib import Path


def atomic_write(path, data, mode=None, newline=None, fsync=False, prepare=None):
    """Replace `path` with `data` (str or bytes) in one step.
    mode: chmod for the new file (e.g. 0o600, 0o444); prepare(temporary): runs on the
    finished temporary file before it replaces the target (e.g. an owner-only ACL)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    binary = isinstance(data, (bytes, bytearray))
    # Born with its final permission bits (writable until written): a secrets file is never
    # readable by others, not even between creating and chmod-ing it. umask only narrows it.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temporary, flags, 0o666 if mode is None else mode & 0o777 | 0o200)
        with os.fdopen(fd, "wb" if binary else "w", **({} if binary else
                       {"encoding": "utf-8", "newline": newline})) as stream:
            stream.write(data)
            if fsync:
                stream.flush()
                os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if prepare:
            prepare(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_json(path, default=None):
    """Read UTF-8 JSON, returning `default` for missing or malformed files."""
    import json
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
