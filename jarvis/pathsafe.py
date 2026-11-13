"""Checks for paths, SSH names, and addresses that arrive from another machine.

Stdlib only. Callers keep their own destination roots; this module refuses to
follow a final symlink, to publish over an existing name, or to treat a
Tailscale address as a Lan Mouse peer.
"""

import errno
import ipaddress
import os
import re
import stat

# Tailscale's CGNAT v4 range and its IPv6 ULA prefix. The patched Lan Mouse
# build refuses both; writers on this side must not put them in a peer config.
TAILSCALE = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

_SSH_DEST = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*")
_COLLISION = re.compile(r" \(\d+\)")
_RELEASE_KEY = re.compile(r"[A-Za-z0-9_]{1,64}")
# Set on symlink, junction, and mount-point tags. Cloud placeholders (0x9000001A)
# have FILE_ATTRIBUTE_REPARSE_POINT too, but this bit stays clear.
_NAME_SURROGATE = 0x20000000


def is_tailscale_ip(value):
    """True for an address inside Tailscale's v4 or v6 range."""
    text = str(value).strip().strip("[]")
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(addr in net for net in TAILSCALE)


def lan_mouse_ip_blocked(value):
    """Configured peer addresses that must not count as "on this network".

    ``100.`` is wider than 100.64.0.0/10 and matches the guard's existing
    rule. ``is_tailscale_ip`` adds the IPv6 prefix that the ``100.`` check misses.
    """
    text = str(value).strip()
    return text.startswith("100.") or is_tailscale_ip(text)


def has_parent_segment(path):
    """True when ``path`` has a ``..`` segment, a NUL, or is not a string."""
    if not isinstance(path, str) or "\x00" in path:
        return True
    return any(part == ".." for part in path.replace("\\", "/").split("/"))


def ssh_destination(target):
    """``user@host`` only. Rejects option-looking targets such as ``-oProxyCommand=...``."""
    if not isinstance(target, str) or not _SSH_DEST.fullmatch(target):
        raise ValueError("Use user@hostname or user@IPv4 for the configured SSH target.")
    return target


def scp_remote_filename(name):
    """A remote scp path component that is safe to pass unquoted.

    OpenSSH 9+ scp speaks SFTP and does not use a shell. Older scp does, and
    parentheses then start a subshell. Collision names are ``name (2).ext``;
    digits inside that one marker are allowed, a command is not. Spaces stay:
    callers and tests pin the unquoted form ``shot (2).png``.
    """
    if not isinstance(name, str) or not name or len(name) > 255:
        raise ValueError("That file name can't be copied over SSH.")
    if "/" in name or "\\" in name or any(c in name for c in "\x00\n\r\t\"'\\$`;&|<>"):
        raise ValueError("That file name can't be copied over SSH.")
    plain = _COLLISION.sub("", name)
    if "(" in plain or ")" in plain:
        raise ValueError("That file name can't be copied over SSH.")
    return name


def release_key(key):
    """One Lan Mouse release-bind token, with no TOML quoting characters."""
    if not isinstance(key, str) or not _RELEASE_KEY.fullmatch(key):
        raise ValueError("Unsupported release key.")
    return key


def _nofollow():
    return getattr(os, "O_NOFOLLOW", 0)


def _is_reparse(path):
    """True for a symlink, junction, or mount point. A cloud placeholder is not one."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", None)
    if tag:
        return bool(tag & _NAME_SURROGATE)
    attrs = getattr(st, "st_file_attributes", 0) or 0
    return bool(attrs & 0x400 and not stat.S_ISREG(st.st_mode))


def _same_file(fd, path):
    """The opened handle is the file now named ``path``, and that name is no link.
    Without O_NOFOLLOW (Windows) a name swapped for a junction between the
    lexists check and the open is only caught here."""
    try:
        if _is_reparse(path):
            return False
        now, opened = os.lstat(path), os.fstat(fd)
    except OSError:
        return False
    return (now.st_dev, now.st_ino) == (opened.st_dev, opened.st_ino)


def _binary():
    return getattr(os, "O_BINARY", 0)  # Windows opens in text mode without it


def _flags(mode):
    if mode == "rb":
        return os.O_RDONLY | _binary()
    if mode == "xb":
        return os.O_CREAT | os.O_EXCL | os.O_WRONLY | _binary()
    if mode == "ab":
        return os.O_WRONLY | os.O_APPEND | _binary()
    raise ValueError("unsupported open mode")


def _stream_mode(mode):
    if mode == "rb":
        return "rb"
    if mode == "ab":
        return "ab"
    return "wb"  # already created exclusively; "xb" is not valid on an open fd


def open_leaf(folder, name, mode="rb"):
    """Open one child of ``folder``. Symlinks and non-regular files fail.

    Uses ``openat`` (``dir_fd`` + ``O_NOFOLLOW``) when the platform allows it,
    so a symlink planted at ``name`` is not followed. ``name`` is one component.
    """
    if (not isinstance(name, str) or not name or name in (".", "..")
            or "/" in name or "\\" in name or "\x00" in name):
        raise ValueError("Invalid file name.")
    folder_s = os.fspath(folder)
    flags = _flags(mode) | _nofollow()
    directory = getattr(os, "O_DIRECTORY", 0)
    dirfd = None
    try:
        if os.name == "nt" or os.open not in os.supports_dir_fd:
            path = os.path.join(folder_s, name)
            if _is_reparse(folder_s) or (os.path.lexists(path) and _is_reparse(path)):
                raise ValueError("Refusing to follow a link.")
            fd = os.open(path, flags, 0o600)
            if not _same_file(fd, path) or _is_reparse(folder_s):  # swapped after the check
                os.close(fd)
                raise ValueError("Refusing to follow a link.")
        else:
            dirfd = os.open(folder_s, os.O_RDONLY | directory | _nofollow())
            fd = os.open(name, flags, 0o600, dir_fd=dirfd)
    except FileNotFoundError:
        raise
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EPERM) or _is_reparse(os.path.join(folder_s, name)):
            raise ValueError("Refusing to follow a link.") from exc
        raise
    finally:
        if dirfd is not None:
            os.close(dirfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Not a regular file.")
        if mode in ("xb", "ab") and hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    return os.fdopen(fd, _stream_mode(mode))


def regular_size(path):
    """Size of a regular file. A symlink raises ``ValueError`` and is not followed."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or _is_reparse(path):
        raise ValueError("Refusing to follow a link.")
    return st.st_size


def _fchmod_regular(path):
    fd = os.open(os.fspath(path), os.O_RDONLY | _nofollow() | _binary())
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Refusing to publish a link.")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def link_new(part, dest):
    """Hard-link ``part`` to ``dest`` only if both names are regular files.

    ``follow_symlinks=False`` so a part file swapped for a symlink is not
    turned into a hard link to the symlink's target. An existing ``dest``,
    including a symlink, raises ``FileExistsError``.
    """
    part_s, dest_s = os.fspath(part), os.fspath(dest)
    if not stat.S_ISREG(os.lstat(part_s).st_mode):
        raise ValueError("Refusing to publish a link.")
    if os.link in os.supports_follow_symlinks:
        os.link(part_s, dest_s, follow_symlinks=False)
    else:  # Windows: no linkat; part was just lstat-checked as a regular file
        os.link(part_s, dest_s)
    try:
        _fchmod_regular(dest_s)
    except (OSError, ValueError):
        try:
            os.unlink(dest_s)
        except OSError:
            pass
        raise
    if not stat.S_ISREG(os.lstat(dest_s).st_mode):
        os.unlink(dest_s)
        raise ValueError("Refusing to publish a link.")


def publish_new(part, dest):
    """Move ``part`` onto ``dest`` only when ``dest`` does not exist.

    Creates an exclusive placeholder first, so ``os.replace`` overwrites that
    placeholder and not a file or symlink a peer already planted.
    """
    part_s, dest_s = os.fspath(part), os.fspath(dest)
    if not stat.S_ISREG(os.lstat(part_s).st_mode):
        raise ValueError("Refusing to publish a link.")
    fd = os.open(dest_s, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _nofollow() | _binary(), 0o600)
    os.close(fd)
    replaced = False
    try:
        if not stat.S_ISREG(os.lstat(part_s).st_mode):
            raise ValueError("Refusing to publish a link.")
        os.replace(part_s, dest_s)
        replaced = True
    except Exception:
        # The placeholder is empty. The part, if replace did not move it, is the copy.
        try:
            os.unlink(dest_s)
        except OSError:
            pass
        raise
    try:
        _fchmod_regular(dest_s)
    except ValueError:
        try:
            os.unlink(dest_s)
        except OSError:
            pass
        raise
    except OSError:
        # WinError 5 after a successful replace: Defender, the indexer, or
        # OneDrive still has the new file open. The part name is already
        # gone, so deleting dest would drop the only copy.
        if not replaced or os.path.lexists(part_s):
            try:
                os.unlink(dest_s)
            except OSError:
                pass
        raise


def ensure_private_dir(path, mode=0o700):
    """Create ``path`` as a real directory. A symlink is refused, not followed."""
    path = os.fspath(path)
    if not os.path.lexists(path):
        os.makedirs(path, mode=mode, exist_ok=True)
    if _is_reparse(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
        raise ValueError("The destination folder is a link or not a directory.")
    if os.name != "nt":
        os.chmod(path, mode)
    return path


def write_text_private(path, text, mode=0o600):
    """Write ``text`` to a regular file. An existing symlink is not followed."""
    path = os.fspath(path)
    if os.path.lexists(path) and _is_reparse(path):
        raise ValueError("Refusing to write through a link.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _nofollow(), mode)
    owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Refusing to write through a link.")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            owned = False
            out.write(text)
    finally:
        if owned:
            os.close(fd)
