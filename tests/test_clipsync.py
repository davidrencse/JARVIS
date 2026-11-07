"""Unified clipboard: image conversions and the sync protocol, with fake clipboards."""

import io
import os
import queue
import struct
import json
import tempfile
import threading
import time
from unittest import mock
import unittest

from jarvis import clipsync
from jarvis.clipsync import Sync, dib_to_png, png_decode, png_encode, png_to_dib


class Board:
    def __init__(self, item=None):
        self.item, self.got, self.notify, self.closed = item, queue.Queue(), None, False

    def read(self):
        return self.item

    def write(self, kind, data):
        self.item = (kind, data)
        self.got.put((kind, data))

    def watch(self, changed):
        self.notify = changed
        threading.Event().wait(5)

    def close(self):
        self.closed = True

    def copy(self, kind, data):  # a user copying on this laptop
        self.item = (kind, data)
        self.notify()


def linked(a, b):
    """Two Syncs joined by pipes, as the SSH session joins the laptops."""
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    left = Sync(a, os.fdopen(r2, "rb"), os.fdopen(w1, "wb"))
    right = Sync(b, os.fdopen(r1, "rb"), os.fdopen(w2, "wb"))
    for sync in (left, right):
        threading.Thread(target=sync.run, daemon=True).start()
    for board in (a, b):
        for _ in range(200):
            if board.notify:
                break
            threading.Event().wait(0.01)
    return left, right


class ImageTests(unittest.TestCase):
    def test_png_roundtrip_through_dib(self):
        rows = [bytes([255, 0, 0, 255, 0, 255, 0, 128]), bytes([0, 0, 255, 255, 9, 9, 9, 0])]
        png = png_encode(2, 2, rows, alpha=True)
        self.assertEqual(png_decode(png), (2, 2, rows))
        dib = png_to_dib(png)
        self.assertEqual(struct.unpack("<IiiHH", dib[:16]), (40, 2, 2, 1, 32))
        self.assertEqual(png_decode(dib_to_png(dib)), (2, 2, rows))

    def test_24bit_bottom_up_dib_with_row_padding(self):
        head = struct.pack("<IiiHHIIiiII", 40, 1, 2, 1, 24, 0, 8, 0, 0, 0, 0)
        pixels = bytes([0, 0, 255, 0]) + bytes([255, 0, 0, 0])  # BGR + pad; bottom row first
        width, height, rows = png_decode(dib_to_png(head + pixels))
        self.assertEqual(rows, [bytes([0, 0, 255, 255]), bytes([255, 0, 0, 255])])

    def test_rgb_png_with_filters_decodes(self):
        import zlib
        raw = b"\x01" + bytes([10, 20, 30, 5, 5, 5]) + b"\x02" + bytes([1, 1, 1, 1, 1, 1])
        head = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
        png = (b"\x89PNG\r\n\x1a\n" + clipsync._png_chunk(b"IHDR", head)
               + clipsync._png_chunk(b"IDAT", zlib.compress(raw)) + clipsync._png_chunk(b"IEND", b""))
        _, _, rows = png_decode(png)
        self.assertEqual(rows[0], bytes([10, 20, 30, 255, 15, 25, 35, 255]))
        self.assertEqual(rows[1], bytes([11, 21, 31, 255, 16, 26, 36, 255]))


class LineEndingAndTransientTests(unittest.TestCase):
    def test_crlf_and_lf_text_digest_alike(self):
        self.assertEqual(clipsync.digest("text", b"a\r\nb"), clipsync.digest("text", b"a\nb"))
        self.assertNotEqual(clipsync.digest("png", b"a\r\nb"), clipsync.digest("png", b"a\nb"))

    def test_applied_crlf_text_does_not_echo(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(clipsync, "local_clipboard") as lc:
            clipsync.apply(tmp, "text", b"one\r\ntwo")
            self.assertEqual(clipsync._applied(tmp), clipsync.digest("text", b"one\ntwo"))

    def test_wayland_skips_jarvis_transient_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = clipsync.Wayland.__new__(clipsync.Wayland)
            board.env = {"XDG_RUNTIME_DIR": tmp}
            with open(os.path.join(tmp, clipsync.TRANSIENT), "w") as f:
                f.write(clipsync.digest("text", b"grok prompt"))
            def paste(*args):
                if "--list-types" in args:
                    return b"text/plain\n"
                return b"grok prompt"
            board._paste = paste
            self.assertIsNone(board.read())
            os.utime(os.path.join(tmp, clipsync.TRANSIENT), (1, 1))  # stale marker
            self.assertEqual(board.read(), ("text", b"grok prompt"))


class SyncTests(unittest.TestCase):
    def test_copies_flow_both_ways_without_echo(self):
        dell, zen = Board(("text", b"old dell")), Board(("text", b"old zen"))
        left, right = linked(dell, zen)
        self.assertTrue(dell.got.empty() and zen.got.empty())  # nothing pushed on connect
        dell.copy("text", "hi from Windows ✓".encode())
        self.assertEqual(zen.got.get(timeout=2), ("text", "hi from Windows ✓".encode()))
        zen.notify()  # wl-paste fires for the paste it just received
        png = png_encode(1, 1, [b"\xff\x00\x00"], alpha=False)
        zen.copy("png", png)
        self.assertEqual(dell.got.get(timeout=2), ("png", png))
        dell.notify()
        threading.Event().wait(0.2)
        self.assertTrue(dell.got.empty() and zen.got.empty())
        self.assertEqual((left.sent, right.sent), (1, 1))

    def test_bad_frames_and_empty_copies_are_ignored(self):
        board = Board()
        stream = io.BytesIO(b"not json\n" + b'{"kind": "exe", "data": "AAAA"}\n'
                            + b'{"kind": "text", "data": "!!"}\n'
                            + b'{"kind": "text", "data": "aGk="}\n')
        Sync(board, stream, io.BytesIO()).receive()
        self.assertEqual(board.got.get_nowait(), ("text", b"hi"))
        self.assertTrue(board.got.empty())
        out = io.BytesIO()
        sync = Sync(Board(None), io.BytesIO(), out)
        sync.changed()
        self.assertEqual(out.getvalue(), b"")

    def test_closed_link_closes_the_watcher(self):
        board = Board()
        Sync(board, io.BytesIO(b""), io.BytesIO()).run()
        self.assertTrue(board.closed)

    def test_remote_half_is_standalone(self):
        """The remote side runs the file as a bare script with no package around it."""
        with open(clipsync.__file__, encoding="utf-8") as stream:
            source = stream.read()
        scope = {"__name__": "jarvis_clip"}
        exec(compile(source, "remote", "exec"), scope)
        self.assertTrue(callable(scope["serve"]))
        self.assertIn("python3 -c", clipsync.BOOTSTRAP)


if __name__ == "__main__":
    unittest.main()


class RefreshTests(unittest.TestCase):
    """Audit 2026-11-01: the watcher is its own long-lived process; code updates never reached it."""

    def test_a_watcher_on_old_code_is_restarted_and_current_one_left_alone(self):
        import tempfile
        from unittest import mock
        from jarvis import clipsync
        # The detached-watcher path, whatever this computer's own systemd unit says.
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(clipsync, "_unit_installed", return_value=False):
            with open(f"{state}/{clipsync.CODE_FILE}", "w") as f:
                f.write("old")
            alive = iter([4242, None, None])
            with mock.patch.object(clipsync, "_pid", side_effect=lambda s: next(alive, None)), \
                    mock.patch("jarvis.deploy.code_fingerprint", return_value="new"), \
                    mock.patch.object(clipsync, "stop") as stop, \
                    mock.patch.object(clipsync, "start", return_value={"started": True}) as start:
                self.assertTrue(clipsync.refresh(state, log=lambda *_: None))
            stop.assert_called_once()
            start.assert_called_once()
            with open(f"{state}/{clipsync.CODE_FILE}", "w") as f:
                f.write("new")
            with mock.patch.object(clipsync, "_pid", return_value=4242), \
                    mock.patch("jarvis.deploy.code_fingerprint", return_value="new"), \
                    mock.patch.object(clipsync, "stop") as stop:
                self.assertFalse(clipsync.refresh(state, log=lambda *_: None))
            stop.assert_not_called()


class PidIdentityTests(unittest.TestCase):
    def test_reused_pid_is_not_the_watcher_and_pid_file_is_removed(self):
        with tempfile.TemporaryDirectory() as state:
            pidfile = os.path.join(state, clipsync.PID)
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))  # alive, but this test runner is not `clipboard run`
            with mock.patch.object(clipsync, "_is_watcher", return_value=False):
                self.assertIsNone(clipsync._pid(state))
            self.assertFalse(os.path.exists(pidfile))

    def test_stop_never_signals_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, clipsync.PID), "w") as f:
                f.write(str(os.getpid()))
            with mock.patch.object(clipsync, "_is_watcher", return_value=False), \
                    mock.patch("os.kill") as kill, \
                    mock.patch("subprocess.run") as run:
                self.assertFalse(clipsync.stop(state)["stopped"])
            self.assertNotIn(15, [c.args[1] for c in kill.call_args_list])  # only the 0 probe
            run.assert_not_called()

    def test_real_watcher_cmdline_is_accepted(self):
        argv = b"python\0-m\0jarvis\0--state\0s\0clipboard\0run\0"
        reads = {"/proc/9/stat": "9 (python) S 1", "/proc/9/cmdline": argv}

        def fake_open(path, mode="r"):
            return io.BytesIO(reads[path]) if "b" in mode else io.StringIO(reads[path])

        with mock.patch("builtins.open", fake_open):
            self.assertTrue(clipsync._is_watcher(9))
            reads["/proc/9/cmdline"] = b"sshd\0-D\0"
            self.assertFalse(clipsync._is_watcher(9))

    def test_pid_file_removed_when_watcher_exits(self):
        import atexit
        with tempfile.TemporaryDirectory() as state:
            pidfile = os.path.join(state, clipsync.PID)
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))
            registered = []
            with mock.patch.object(atexit, "register", registered.append), \
                    mock.patch("signal.signal") as sig:
                clipsync._own_pid_file(state)
            handler = sig.call_args[0][1]
            with self.assertRaises(SystemExit):  # SIGTERM unwinds so `finally` reaps wl-paste
                handler(15, None)
            registered[0]()
            self.assertFalse(os.path.exists(pidfile))


class AppliedExpiryTests(unittest.TestCase):
    def test_applied_digest_expires(self):
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.object(clipsync, "local_clipboard", return_value=Board()):
            clipsync.apply(state, "text", b"hello")
            self.assertEqual(clipsync._applied(state), clipsync.digest("text", b"hello"))
            later = clipsync.time.time() + clipsync.APPLIED_TTL + 1
            with mock.patch.object(clipsync.time, "time", return_value=later):
                self.assertIsNone(clipsync._applied(state))


class ImageFallbackTests(unittest.TestCase):
    def test_truncated_png_falls_back_to_png_only(self):
        board = clipsync.Windows.__new__(clipsync.Windows)
        board.png, board.CF_DIB = 7, 8
        board.u = mock.Mock()
        board._open = mock.Mock()
        puts = []
        board._put = lambda fmt, data: puts.append(fmt)
        png = png_encode(2, 2, [bytes(8), bytes(8)], alpha=True)
        for boom in (IndexError, TypeError):
            puts.clear()
            with mock.patch.object(clipsync, "png_to_dib", side_effect=boom):
                board.write("png", png)
            self.assertEqual(puts, [7])

    def test_pure_python_skipped_above_size_limit(self):
        png = png_encode(2, 2, [bytes(8), bytes(8)], alpha=True)
        with mock.patch.object(clipsync, "_pillow", return_value=None), \
                mock.patch.object(clipsync, "MAX_PURE_PIXELS", 3):
            with self.assertRaises(ValueError):
                png_to_dib(png)

    def test_pillow_and_pure_python_agree(self):
        rows = [bytes([255, 0, 0, 255, 0, 255, 0, 128]), bytes([0, 0, 255, 255, 9, 9, 9, 0])]
        png = png_encode(2, 2, rows, alpha=True)
        with mock.patch.object(clipsync, "_pillow", return_value=None):
            pure = png_to_dib(png)
        if clipsync._pillow() is not None:
            self.assertEqual(png_to_dib(png), pure)
            self.assertEqual(png_decode(dib_to_png(pure)), (2, 2, rows))


class WatcherLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name

    def test_start_detaches_from_the_callers_systemd_unit(self):
        with mock.patch.object(clipsync, "_pid", side_effect=[None, 4242, 4242, 4242]), \
                mock.patch.object(clipsync, "_unit_installed", return_value=False), \
                mock.patch("jarvis.daemon._under_systemd_unit", return_value=True), \
                mock.patch("jarvis.daemon.shutil.which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(clipsync.subprocess, "Popen") as popen, \
                mock.patch("time.sleep"):
            out = clipsync.start(self.state)
        self.assertTrue(out["started"])
        self.assertEqual(popen.call_args.args[0][:3], ["systemd-run", "--user", "--scope"])

    def test_start_prefers_restarting_the_installed_unit(self):
        with mock.patch.object(clipsync, "_pid", side_effect=[None, 4242, 4242, 4242]), \
                mock.patch.object(clipsync, "_unit_installed", return_value=True), \
                mock.patch.object(clipsync.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(clipsync.subprocess, "Popen") as popen, \
                mock.patch("time.sleep"):
            out = clipsync.start(self.state)
        self.assertTrue(out["started"])
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "restart", "jarvis-clipboard.service"])
        popen.assert_not_called()

    def test_refresh_restarts_the_unit_instead_of_killing_the_watcher(self):
        with open(os.path.join(self.state, clipsync.CODE_FILE), "w") as f:
            f.write("old")
        with mock.patch.object(clipsync, "_pid", return_value=7), \
                mock.patch("jarvis.deploy.code_fingerprint", return_value="new"), \
                mock.patch.object(clipsync, "_unit_installed", return_value=True), \
                mock.patch.object(clipsync, "stop") as stop, \
                mock.patch.object(clipsync, "start", return_value={"started": True}) as start:
            self.assertTrue(clipsync.refresh(self.state, log=lambda _: None))
        stop.assert_not_called()
        start.assert_called_once()

    def test_refresh_survives_a_fingerprint_oserror(self):
        with mock.patch.object(clipsync, "_pid", return_value=7), \
                mock.patch("jarvis.deploy.code_fingerprint", side_effect=OSError("gone")):
            self.assertFalse(clipsync.refresh(self.state, log=lambda _: None))

    def test_pasted_copy_is_still_ours_after_a_long_push(self):
        board = Board(("text", b"x"))
        with mock.patch.object(clipsync, "local_clipboard", return_value=board):
            clipsync.apply(self.state, "text", b"x")
        now = time.time()
        saved = os.path.join(self.state, clipsync.APPLIED)
        # pasted 1 s into a push that ran 20 s and just ended
        busy = (now - 21, now - 0.5)
        with open(saved, "w") as f:
            json.dump({"digest": clipsync.digest("text", b"x"), "at": now - 20}, f)
        self.assertIsNone(clipsync._applied(self.state))
        self.assertEqual(clipsync._applied(self.state, busy), clipsync.digest("text", b"x"))

    def test_connect_does_not_hang_on_non_json_greeting(self):
        order = []
        proc = mock.MagicMock()
        proc.stdout.readline.return_value = b"Welcome banner\n"
        proc.kill.side_effect = lambda: order.append("kill")
        proc.stderr.read.side_effect = lambda n: order.append("read") or b"boom"
        with mock.patch.object(clipsync.shutil, "which", return_value="/usr/bin/ssh"), \
                mock.patch("jarvis.transfer._check_target"), \
                mock.patch("jarvis.transfer._ssh_args", return_value=["ssh"]), \
                mock.patch.object(clipsync, "_pinned_host", return_value=[]), \
                mock.patch.object(clipsync.subprocess, "Popen", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "SSH failed"):
                clipsync.connect({"target": "me@host"}, Board(), lambda _: None)
        self.assertLess(order.index("kill"), order.index("read"))


class AlphaTests(unittest.TestCase):
    @unittest.skipIf(clipsync._pillow() is None, "Pillow not installed")
    def test_opaque_32bpp_dib_gets_full_alpha(self):
        header = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 32, 0, 4, 0, 0, 0, 0)
        png = dib_to_png(header + bytes([10, 20, 30, 0]))
        self.assertEqual(png_decode(png)[2][0][3], 255)


class HandStopTests(unittest.TestCase):
    """Audit cluster-02/05: a stop by hand sticks for the boot; one watcher per state folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = tmp.name

    def test_stop_sticks_for_this_boot_and_start_clears_it(self):
        with mock.patch.object(clipsync, "_boot", return_value=1000.0), \
                mock.patch.object(clipsync, "_pid", return_value=None):
            self.assertFalse(clipsync.stop(self.state)["stopped"])  # already down: still marked
            self.assertTrue(clipsync.stopped_by_hand(self.state))
        with mock.patch.object(clipsync, "_boot", return_value=99999.0):
            self.assertFalse(clipsync.stopped_by_hand(self.state))  # a later boot
        with mock.patch.object(clipsync, "_boot", return_value=None):
            self.assertFalse(clipsync.stopped_by_hand(self.state))  # unknown never keeps it off
        with mock.patch.object(clipsync, "_boot", return_value=1000.0), \
                mock.patch.object(clipsync, "_pid", return_value=4242), \
                mock.patch.object(clipsync, "status", return_value={}):
            clipsync.start(self.state)
            self.assertFalse(clipsync.stopped_by_hand(self.state))

    def test_keep_alive_leaves_a_hand_stopped_watcher_off(self):
        from jarvis import cluster

        class Now:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()

        stop = threading.Event()
        stop.set()  # one keep-alive tick, then return
        with mock.patch.object(clipsync, "_boot", return_value=1000.0), \
                mock.patch.object(clipsync, "_pid", return_value=None):
            clipsync.stop(self.state)
            with mock.patch("jarvis._under_tests", return_value=False), \
                    mock.patch.object(cluster.threading, "Thread", Now), \
                    mock.patch.object(clipsync, "refresh"), \
                    mock.patch.object(clipsync, "start") as start:
                cluster._refresh_clipboard(self.state, {}, stop)
            start.assert_not_called()
            os.remove(os.path.join(self.state, clipsync.STOPPED))
            with mock.patch("jarvis._under_tests", return_value=False), \
                    mock.patch.object(cluster.threading, "Thread", Now), \
                    mock.patch.object(clipsync, "refresh"), \
                    mock.patch("builtins.print"), \
                    mock.patch.object(clipsync, "start", return_value={"started": True}) as start:
                cluster._refresh_clipboard(self.state, {}, stop)
            start.assert_called_once()

    def test_second_watcher_exits_without_watching(self):
        from jarvis.locking import exclusive_lock
        with exclusive_lock(os.path.join(self.state, clipsync.LOCK)), \
                mock.patch.object(clipsync, "_run_watcher") as watch:
            self.assertIsNone(clipsync.run({}, self.state))
        watch.assert_not_called()
        with mock.patch.object(clipsync, "_run_watcher", return_value="ran") as watch:
            self.assertEqual(clipsync.run({}, self.state), "ran")  # free lock: it runs
        watch.assert_called_once()
