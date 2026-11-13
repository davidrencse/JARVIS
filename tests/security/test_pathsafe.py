"""pathsafe: refuse symlink follows, Tailscale peers, and unsafe scp names.

Temp directories only. No clipboard tools, no SSH, no network.
"""
import os
import stat
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from jarvis import clipsync, pathsafe


class TailscaleTests(unittest.TestCase):
    def test_ranges(self):
        inside = ("100.64.0.1", "100.127.255.255", "100.64.0.11",
                  "fd7a:115c:a1e0::1", "[fd7a:115c:a1e0::1]")
        outside = ("100.63.255.255", "100.128.0.1", "172.1.1.1", "10.0.0.2",
                   "fd7a:115c:a1e1::1", "not-an-ip")
        for value in inside:
            with self.subTest(value=value):
                self.assertTrue(pathsafe.is_tailscale_ip(value))
                self.assertTrue(pathsafe.lan_mouse_ip_blocked(value))
        for value in outside:
            with self.subTest(value=value):
                self.assertFalse(pathsafe.is_tailscale_ip(value))
        # The guard's old "100." rule is wider than 100.64.0.0/10.
        self.assertTrue(pathsafe.lan_mouse_ip_blocked("100.63.1.1"))
        self.assertTrue(pathsafe.lan_mouse_ip_blocked("100.128.0.1"))
        self.assertFalse(pathsafe.lan_mouse_ip_blocked("172.1.41.116"))
        self.assertFalse(pathsafe.lan_mouse_ip_blocked("fd7a:115c:a1e1::1"))


class NameTests(unittest.TestCase):
    def test_scp_allows_collision_markers_and_spaces_only(self):
        for name in ("shot (2).png", "shot (2) (3).png", "a b.png", "note.txt"):
            with self.subTest(name=name):
                self.assertEqual(pathsafe.scp_remote_filename(name), name)
        for name in ("a (id).txt", "a (touch pwned)", "a;id", "../x", "a\\b",
                     "a\nb", "", "a$b", "a`b`", "a|b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    pathsafe.scp_remote_filename(name)

    def test_ssh_destination_rejects_option_injection(self):
        self.assertEqual(pathsafe.ssh_destination("jdoe@host"), "jdoe@host")
        self.assertEqual(pathsafe.ssh_destination("jdoe@172.1.42.215"), "jdoe@172.1.42.215")
        for bad in ("-oProxyCommand=x@y", "user@host;id", "user@host\n", " @host", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pathsafe.ssh_destination(bad)

    def test_parent_segment_and_release_key(self):
        self.assertTrue(pathsafe.has_parent_segment("/tmp/../etc/passwd"))
        self.assertTrue(pathsafe.has_parent_segment("..\\windows\\system32"))
        self.assertTrue(pathsafe.has_parent_segment("a\x00b"))
        self.assertTrue(pathsafe.has_parent_segment(None))
        self.assertFalse(pathsafe.has_parent_segment("/tmp/ok"))
        self.assertEqual(pathsafe.release_key("KeyLeftAlt"), "KeyLeftAlt")
        with self.assertRaises(ValueError):
            pathsafe.release_key('Key"Left')


class OpenPublishTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.secret = self.root / "secret"
        self.secret.write_bytes(b"SECRET")
        self.folder = self.root / "drop"
        self.folder.mkdir()

    def test_open_leaf_reads_a_regular_file_and_refuses_a_symlink(self):
        (self.folder / "note.txt").write_bytes(b"hello")
        with pathsafe.open_leaf(self.folder, "note.txt", "rb") as stream:
            self.assertEqual(stream.read(), b"hello")
        (self.folder / "link.txt").symlink_to(self.secret)
        with self.assertRaises(ValueError):
            pathsafe.open_leaf(self.folder, "link.txt", "rb")
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        for bad in ("..", "../secret", "a/b", "a\\b", "a\x00b", ".", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, bad, "rb")

    def test_windows_style_path_branch_still_reads_writes_and_refuses_links(self):
        # Windows cannot os.open a directory or use dir_fd: the path branch must carry the call.
        from unittest import mock
        (self.folder / "note.txt").write_bytes(b"a\r\nb")
        (self.folder / "link.txt").symlink_to(self.secret)
        with mock.patch.object(pathsafe.os, "name", "nt"):
            with pathsafe.open_leaf(self.folder, "note.txt", "rb") as stream:
                self.assertEqual(stream.read(), b"a\r\nb")
            with pathsafe.open_leaf(self.folder, "new.bin", "xb") as stream:
                stream.write(b"\x1a\r\n")
            with self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, "link.txt", "rb")
        self.assertEqual((self.folder / "new.bin").read_bytes(), b"\x1a\r\n")
        self.assertTrue(pathsafe._flags("rb") | pathsafe._flags("xb") | pathsafe._flags("ab"))

    def test_link_new_without_follow_symlinks_support_falls_back(self):
        from unittest import mock
        part = self.folder / "a.part"
        part.write_bytes(b"x")
        with mock.patch.object(pathsafe.os, "supports_follow_symlinks", set()):
            pathsafe.link_new(part, self.folder / "a")
        self.assertEqual((self.folder / "a").read_bytes(), b"x")

    def test_exclusive_create_does_not_write_through_a_symlink(self):
        link = self.folder / "part"
        link.symlink_to(self.secret)
        with self.assertRaises((OSError, ValueError)):
            pathsafe.open_leaf(self.folder, "part", "xb")
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertTrue(link.is_symlink())

    def test_append_does_not_follow_a_swapped_part(self):
        part = self.folder / "part"
        part.write_bytes(b"abcd")
        part.unlink()
        part.symlink_to(self.secret)
        with self.assertRaises(ValueError):
            pathsafe.regular_size(part)
        with self.assertRaises((OSError, ValueError)):
            pathsafe.open_leaf(self.folder, "part", "ab")
        self.assertEqual(self.secret.read_bytes(), b"SECRET")

    def test_link_and_publish_do_not_replace_a_file_or_a_symlink(self):
        part = self.folder / "incoming"
        part.write_bytes(b"new")
        occupied = self.folder / "kept.txt"
        occupied.write_bytes(b"old")
        link = self.folder / "link.txt"
        link.symlink_to(self.secret)
        for dest in (occupied, link):
            for publish in (pathsafe.link_new, pathsafe.publish_new):
                with self.subTest(publish=publish.__name__, dest=dest.name):
                    with self.assertRaises((OSError, ValueError)):
                        publish(part, dest)
        self.assertEqual(occupied.read_bytes(), b"old")
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertTrue(link.is_symlink())
        self.assertEqual(part.read_bytes(), b"new")

    def test_publish_keeps_bytes_when_windows_denies_the_followup_open(self):
        # os.replace has already moved the only copy. A WinError 5 from opening
        # that file (Defender, indexer, OneDrive) must not delete it.
        part = self.folder / "incoming"
        part.write_bytes(b"new")
        dest = self.folder / "landed.txt"

        def deny(path):
            raise PermissionError(13, "Access is denied")

        with mock.patch.object(pathsafe, "_fchmod_regular", side_effect=deny):
            with self.assertRaises(PermissionError):
                pathsafe.publish_new(part, dest)
        self.assertEqual(dest.read_bytes(), b"new")
        self.assertFalse(part.exists())

    def test_publish_still_removes_a_non_regular_result(self):
        part = self.folder / "incoming"
        part.write_bytes(b"new")
        dest = self.folder / "landed.txt"

        def not_regular(path):
            raise ValueError("Refusing to publish a link.")

        with mock.patch.object(pathsafe, "_fchmod_regular", side_effect=not_regular):
            with self.assertRaises(ValueError):
                pathsafe.publish_new(part, dest)
        self.assertFalse(dest.exists())
        self.assertFalse(part.exists())

    def test_link_refuses_a_symlink_source(self):
        planted = self.folder / "part"
        planted.symlink_to(self.secret)
        dest = self.folder / "landed.txt"
        with self.assertRaises(ValueError):
            pathsafe.link_new(planted, dest)
        with self.assertRaises(ValueError):
            pathsafe.publish_new(planted, dest)
        self.assertFalse(dest.exists())
        self.assertEqual(self.secret.read_bytes(), b"SECRET")

    def test_new_files_and_dirs_are_private(self):
        dest = self.folder / "landed.txt"
        part = self.folder / "incoming"
        part.write_bytes(b"new")
        pathsafe.link_new(part, dest)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        self.assertEqual(dest.read_bytes(), b"new")
        fresh = self.root / "private"
        pathsafe.ensure_private_dir(fresh)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o700)
        real = self.root / "real"
        real.mkdir()
        (real / "marker").write_text("keep")
        alias = self.root / "alias"
        alias.symlink_to(real)
        with self.assertRaises(ValueError):
            pathsafe.ensure_private_dir(alias)
        self.assertEqual((real / "marker").read_text(), "keep")
        self.assertTrue(alias.is_symlink())

    def test_write_text_private_refuses_a_symlink_and_is_mode_0600(self):
        target = self.root / "real.toml"
        target.write_text("ORIGINAL")
        link = self.root / "config.toml"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            pathsafe.write_text_private(link, "pwned")
        self.assertEqual(target.read_text(), "ORIGINAL")
        plain = self.root / "plain.toml"
        pathsafe.write_text_private(plain, "ok")
        self.assertEqual(plain.read_text(), "ok")
        self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o600)


class InflateTests(unittest.TestCase):
    def test_over_limit_raises_before_flush_returns_the_rest(self):
        blob = zlib.compress(b"\0" * 10000)
        with self.assertRaises(ValueError):
            clipsync._inflate(blob, 100)
        self.assertEqual(clipsync._inflate(zlib.compress(b"hello"), 5), b"hello")

    def test_small_png_still_decodes(self):
        png = clipsync.png_encode(1, 1, [b"\x01\x02\x03"], False)
        width, height, rows = clipsync.png_decode(png)
        self.assertEqual((width, height), (1, 1))
        self.assertEqual(rows, [b"\x01\x02\x03\xff"])


class _StatView:
    """Real lstat plus Windows reparse fields. Other attributes stay the file's."""

    def __init__(self, st, **extra):
        self._st = st
        self._extra = extra

    def __getattr__(self, name):
        if name in self._extra:
            return self._extra[name]
        return getattr(self._st, name)


class WindowsReparseTests(unittest.TestCase):
    """FILE_ATTRIBUTE_REPARSE_POINT is not enough to call a file a link.

    OneDrive placeholders are regular files tagged 0x9000001A. Junctions and
    mount points set the name-surrogate bit (0xA0000003). os.name and
    supports_dir_fd are patched only inside each test so open_leaf takes the
    Windows path branch.
    """

    _CLOUD = 0x9000001A
    _JUNCTION = 0xA0000003
    _REPARSE = 0x400

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "Desktop"
        self.folder.mkdir()
        self.leaf = "notes.pdf"
        self.file = self.folder / self.leaf
        self.file.write_bytes(b"hello")

    def _windows(self, stamps):
        from contextlib import contextmanager
        from unittest import mock

        real = pathsafe.os.lstat

        def lstat(path, *args, **kwargs):
            st = real(path, *args, **kwargs)
            extra = stamps.get(os.path.abspath(os.fspath(path)))
            if extra is None:
                return st
            return _StatView(st, **extra)

        @contextmanager
        def patched():
            with mock.patch.object(pathsafe.os, "name", "nt"), \
                    mock.patch.object(pathsafe.os, "supports_dir_fd", frozenset()), \
                    mock.patch.object(pathsafe.os, "lstat", side_effect=lstat):
                yield

        return patched()

    def test_cloud_placeholder_opens_and_reports_its_size(self):
        self.assertTrue(stat.S_ISREG(self.file.lstat().st_mode))
        self.assertFalse(self._CLOUD & pathsafe._NAME_SURROGATE)
        stamps = {os.path.abspath(self.file): {
            "st_mode": stat.S_IFREG | 0o644,
            "st_file_attributes": self._REPARSE,
            "st_reparse_tag": self._CLOUD,
        }}
        with self._windows(stamps):
            with pathsafe.open_leaf(self.folder, self.leaf, "rb") as stream:
                self.assertEqual(stream.read(), b"hello")
            self.assertEqual(pathsafe.regular_size(self.file), 5)

    def test_cloud_directory_still_opens_and_creates_a_child(self):
        stamps = {os.path.abspath(self.folder): {
            "st_file_attributes": self._REPARSE,
            "st_reparse_tag": self._CLOUD,
        }}
        with self._windows(stamps):
            self.assertEqual(pathsafe.ensure_private_dir(self.folder), os.fspath(self.folder))
            with pathsafe.open_leaf(self.folder, self.leaf, "rb") as stream:
                self.assertEqual(stream.read(), b"hello")
            with pathsafe.open_leaf(self.folder, "part.bin", "xb") as stream:
                stream.write(b"part")
        self.assertEqual((self.folder / "part.bin").read_bytes(), b"part")

    def test_junction_tag_is_still_refused(self):
        self.assertTrue(self._JUNCTION & pathsafe._NAME_SURROGATE)
        file_stamp = {
            "st_mode": stat.S_IFREG | 0o644,
            "st_file_attributes": self._REPARSE,
            "st_reparse_tag": self._JUNCTION,
        }
        dir_stamp = {
            "st_file_attributes": self._REPARSE,
            "st_reparse_tag": self._JUNCTION,
        }
        with self._windows({os.path.abspath(self.file): file_stamp}):
            with self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, self.leaf, "rb")
            with self.assertRaises(ValueError):
                pathsafe.regular_size(self.file)
        with self._windows({os.path.abspath(self.folder): dir_stamp}):
            with self.assertRaises(ValueError):
                pathsafe.ensure_private_dir(self.folder)
            with self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, self.leaf, "rb")
        self.assertEqual(self.file.read_bytes(), b"hello")

    def test_symlink_is_still_refused(self):
        secret = self.root / "secret"
        secret.write_bytes(b"SECRET")
        link = self.folder / "link.pdf"
        link.symlink_to(secret)
        with self._windows({}):
            with self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, "link.pdf", "rb")
            with self.assertRaises(ValueError):
                pathsafe.regular_size(link)
        self.assertEqual(secret.read_bytes(), b"SECRET")
        self.assertTrue(link.is_symlink())

    def test_directory_reparse_without_a_tag_is_still_refused(self):
        stamps = {os.path.abspath(self.folder): {
            "st_file_attributes": self._REPARSE,
            "st_reparse_tag": None,
        }}
        with self._windows(stamps):
            with self.assertRaises(ValueError):
                pathsafe.ensure_private_dir(self.folder)
            with self.assertRaises(ValueError):
                pathsafe.open_leaf(self.folder, self.leaf, "rb")
        self.assertEqual(self.file.read_bytes(), b"hello")

    def test_plain_file_is_unaffected(self):
        with self._windows({}):
            with pathsafe.open_leaf(self.folder, self.leaf, "rb") as stream:
                self.assertEqual(stream.read(), b"hello")
            self.assertEqual(pathsafe.regular_size(self.file), 5)
            with pathsafe.open_leaf(self.folder, "fresh.bin", "xb") as stream:
                stream.write(b"ok")
        self.assertEqual((self.folder / "fresh.bin").read_bytes(), b"ok")


if __name__ == "__main__":
    unittest.main()
