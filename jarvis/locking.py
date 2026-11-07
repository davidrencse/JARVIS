"""Local process locks: the OS releases ownership when a worker exits."""

import errno
import os
from .platforms import on_windows
import time
from contextlib import contextmanager


class LockBusy(ValueError):
    """The lock is held by another run. ValueError so existing handlers keep working."""


@contextmanager
def exclusive_lock(path, timeout=0.0, poll=0.05):
    """Nonblocking lock (or, with `timeout`, retried until then); keep the file so waiters
    always lock the same inode.

    Intended for state on a local filesystem, not distributed network storage.
    The open handle owns the lock; a leftover file does not mean a stale owner.
    """
    with open(path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = None  # the clock is read only when someone else holds the lock
        while True:
            try:
                if on_windows():
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if deadline is None:
                    deadline = time.monotonic() + timeout
                if time.monotonic() >= deadline:
                    name = os.path.basename(str(path))
                    raise LockBusy(f"Lock {name!r} is busy in another run or recovery. Try "
                                   "again after that operation finishes.") from exc
                time.sleep(poll)
        try:
            yield
        finally:
            if on_windows():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
