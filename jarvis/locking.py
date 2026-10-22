"""Local process locks: the OS releases ownership when a worker exits."""

import errno
import os
from contextlib import contextmanager


@contextmanager
def exclusive_lock(path):
    """Nonblocking lock; keep the file so waiters always lock the same inode.

    Intended for state on a local filesystem, not distributed network storage.
    The open handle owns the lock; a leftover file does not mean a stale owner.
    """
    with open(path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise ValueError("Plan is busy in another run or recovery. Try again after "
                             "that operation finishes.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
