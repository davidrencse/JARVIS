"""Unified clipboard: image conversions and the sync protocol, with fake clipboards."""

import io
import os
import queue
import struct
import threading
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
        source = open(clipsync.__file__, encoding="utf-8").read()
        scope = {"__name__": "jarvis_clip"}
        exec(compile(source, "remote", "exec"), scope)
        self.assertTrue(callable(scope["serve"]))
        self.assertIn("python3 -c", clipsync.BOOTSTRAP)


if __name__ == "__main__":
    unittest.main()
