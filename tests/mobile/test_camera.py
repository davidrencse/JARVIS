"""Camera view (camera.py): consent, picking the RGB camera, cutting ffmpeg's MJPEG stream into
frames, and a session's whole life.

Everything is a fake: ffmpeg is a FakePopen streaming canned JPEGs, sysfs is a temp tree, the
hub is a real remote_view.Sessions answered in-process. No camera is opened, no notification
is shown."""

import base64
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import camera, control, remote_view
from jarvis.camera import PRESETS, Splitter, jpeg_size
from jarvis.remote_view import Sessions

SID = "c" * 32


def jpeg(color=(10, 20, 30), size=(64, 48), comment=None):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", **({"comment": comment} if comment else {}))
    return out.getvalue()


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class FakeProc:
    """ffmpeg as a camera session sees it: back-to-back JPEGs on stdout (each split in two
    reads, so the splitter must join them) until terminated. busy: it quits at once with
    `stderr` lines, as a camera another app holds does. stubborn: terminate does nothing."""

    def __init__(self, argv, frames=None, interval=0.03, stderr=(), quit_at_once=False, stubborn=False):
        self.argv, self.frames, self.interval = argv, frames or [jpeg()], interval
        self.calls, self.returncode, self.stubborn = [], None, stubborn
        self.dead = threading.Event()
        self._lines = [line.encode() + b"\n" for line in stderr]
        self._pending, self._next = [], 0
        if quit_at_once:
            self._die(1)
        self.stdout, self.stderr = self, _Err(self)

    def _die(self, code):
        if self.returncode is None:
            self.returncode = code
        self.dead.set()

    def read1(self, n):
        if self._pending:
            return self._pending.pop(0)
        if self.dead.wait(self.interval):
            return b""
        frame = self.frames[self._next % len(self.frames)]
        self._next += 1
        half = len(frame) // 2
        self._pending.append(frame[half:])
        return frame[:half]

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self.stubborn:
            self._die(-15)

    def kill(self):
        self.calls.append("kill")
        self._die(-9)

    def wait(self, timeout=None):
        if not self.dead.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


class _Err:
    def __init__(self, proc):
        self.proc = proc

    def readline(self):
        if self.proc._lines:
            return self.proc._lines.pop(0)
        self.proc.dead.wait()
        return b""


def fake_popen(**options):
    procs = []

    def popen(argv, **kw):
        proc = FakeProc(argv, **options)
        procs.append(proc)
        return proc
    popen.procs = procs
    return popen


class HubClient:
    """The main's hub, in-process: a real Sessions answering what a node would POST."""

    def __init__(self, sessions, node="node"):
        self.s, self.node, self.posts = sessions, node, []

    def request(self, path, body, timeout=None):
        self.posts.append(dict(body))
        sid = body["session"]
        if body.get("end") is not None:
            return self.s.node_end(self.node, sid, body["end"])
        data = base64.b64decode(body["jpeg"]) if body.get("jpeg") else None
        return self.s.seal(sid, "frame", self.s.frame(self.node, sid, data, body.get("width"), body.get("height")))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def write_sysfs(root, nodes, links=()):
    """nodes: {"video1": (name, index, udev caps or None)}; links: {by-path name: target}."""
    root = Path(root)
    for node, (name, index, caps) in nodes.items():
        folder = root / "sys" / "class" / "video4linux" / node
        folder.mkdir(parents=True)
        (folder / "name").write_text(name + "\n")
        (folder / "index").write_text(f"{index}\n")
        minor = int(node[5:])
        (folder / "dev").write_text(f"81:{minor}\n")
        if caps is not None:
            udev = root / "run" / "udev" / "data"
            udev.mkdir(parents=True, exist_ok=True)
            (udev / f"c81:{minor}").write_text(f"S:v4l/by-path/x\nE:ID_V4L_CAPABILITIES={caps}\nG:uaccess\n")
    for name, target in dict(links).items():
        folder = root / "dev" / "v4l" / "by-path"
        folder.mkdir(parents=True, exist_ok=True)
        os.symlink(f"../../{target}", folder / name)


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_off_until_allowed_here_and_read_from_disk_each_time(self):
        self.assertFalse(camera.allowed_here(self.state))
        camera.set_allowed(self.state, True)
        self.assertTrue(camera.allowed_here(self.state))
        self.assertEqual(json.loads((self.state / camera.SETTINGS).read_text())["allow"], True)
        (self.state / camera.SETTINGS).write_text('{"allow": "yes"}')  # only a real true counts
        self.assertFalse(camera.allowed_here(self.state))
        (self.state / camera.SETTINGS).write_text("not json")
        self.assertFalse(camera.allowed_here(self.state))

    def test_turning_it_off_keeps_the_device_choice(self):
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": True, "device": "/dev/video7"}))
        camera.set_allowed(self.state, False)
        self.assertEqual(json.loads((self.state / camera.SETTINGS).read_text()),
                         {"allow": False, "device": "/dev/video7"})


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def test_the_rgb_capture_node_is_picked_by_its_stable_path(self):
        # The Arch main's webcam: one USB device, an RGB and an infrared interface, each with
        # a metadata node. by-id links would collide (shared serial), so by-path it is.
        write_sysfs(self.root, {
            "video1": ("USB2.0 HD UVC WebCam: USB2.0 HD", 0, ":capture:"),
            "video2": ("USB2.0 HD UVC WebCam: USB2.0 HD", 1, ":"),
            "video3": ("USB2.0 HD UVC WebCam: USB2.0 IR", 0, ":capture:"),
            "video4": ("USB2.0 HD UVC WebCam: USB2.0 IR", 1, ":")},
            {"pci-0000:00:14.0-usb-0:9:1.0-video-index0": "video1",
             "pci-0000:00:14.0-usb-0:9:1.2-video-index0": "video3",
             "pci-0000:00:14.0-usb-0:9:1.0-video-index1": "video2"})
        found = camera.linux_devices(self.root)
        self.assertEqual([d["node"] for d in found], ["video1"])
        self.assertEqual(found[0]["path"], "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:9:1.0-video-index0")
        self.assertEqual(camera.pick_device(self.state, "linux", self.root),
                         ("/dev/v4l/by-path/pci-0000:00:14.0-usb-0:9:1.0-video-index0", None))

    def test_without_udev_data_or_links_the_dev_node_is_used(self):
        write_sysfs(self.root, {"video10": ("Integrated Camera", 0, None),
                                "video2": ("Integrated Camera", 0, ":video_output:"),
                                "video0": ("Infrared Camera", 0, None)})
        self.assertEqual([d["path"] for d in camera.linux_devices(self.root)], ["/dev/video10"])

    def test_no_camera_and_an_override(self):
        self.assertEqual(camera.pick_device(self.state, "linux", self.root)[0], None)
        self.assertIn("No camera found", camera.pick_device(self.state, "linux", self.root)[1])
        (self.state / camera.SETTINGS).write_text(json.dumps({"device": "/dev/video5"}))
        self.assertEqual(camera.pick_device(self.state, "linux", self.root), ("/dev/video5", None))
        for bad in ("http://example.com/x.mjpg", "/dev/../etc/passwd", 7, "/dev/video0 -f x"):
            (self.state / camera.SETTINGS).write_text(json.dumps({"device": bad}))
            with self.subTest(bad=bad):
                device, why = camera.pick_device(self.state, "linux", self.root)
                self.assertIsNone(device)
                self.assertIn("isn't usable", why)
        (self.state / camera.SETTINGS).write_text(json.dumps({"device": "video=HD Webcam"}))
        self.assertEqual(camera.pick_device(self.state, "windows", exe="ffmpeg"), ("HD Webcam", None))
        (self.state / camera.SETTINGS).write_text(json.dumps({"device": "Cam:audio=Microphone"}))
        self.assertIsNone(camera.pick_device(self.state, "windows", exe="ffmpeg")[0])

    def test_directshow_listing_old_and_new_layouts(self):
        new = ('[dshow @ 0000] "Integrated Camera" (video)\n'
               '[dshow @ 0000]   Alternative name "@device_pnp_\\\\?\\usb#vid_0c45"\n'
               '[dshow @ 0000] "Integrated IR Camera" (video)\n'
               '[dshow @ 0000] "Microphone Array (Realtek)" (audio)\n'
               '[dshow @ 0000] "OBS Virtual Camera" (none)\n'
               'dummy: Immediate exit requested\n')
        old = ('[dshow @ 01] DirectShow video devices (some may be both video and audio devices)\n'
               '[dshow @ 01]  "HD Webcam"\n'
               '[dshow @ 01]     Alternative name "@device_pnp_x"\n'
               '[dshow @ 01] DirectShow audio devices\n'
               '[dshow @ 01]  "Microphone (HD Webcam)"\n')
        self.assertEqual(camera._parse_dshow(new), ["Integrated Camera"])
        self.assertEqual(camera._parse_dshow(old), ["HD Webcam"])

        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", new)
        self.assertEqual(camera.pick_device(self.state, "windows", exe="C:/ffmpeg.exe", run=run),
                         ("Integrated Camera", None))
        self.assertEqual(calls[0][1:], ["-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"])

    def test_other_systems_say_so(self):
        self.assertIn("isn't built", camera.pick_device(self.state, "macos")[1])


class StreamTests(unittest.TestCase):
    def test_frames_cut_from_any_chunking_with_garbage_between(self):
        frames = [jpeg((i * 40, 0, 0), (32 + i * 8, 24)) for i in range(4)]
        stream = b"junk" + frames[0] + frames[1] + b"\x00\xff" + frames[2] + frames[3]
        for size in (1, 7, 100, 4096, len(stream)):
            splitter, got = Splitter(), []
            for i in range(0, len(stream), size):
                got += splitter.feed(stream[i:i + size])
            with self.subTest(size=size):
                self.assertEqual(got, frames)

    def test_an_end_marker_inside_a_header_never_cuts_a_frame(self):
        tricky = jpeg(comment=b"x\xff\xd9y")  # FF D9 in a COM segment
        self.assertIn(b"\xff\xd9y", tricky)
        self.assertEqual(Splitter().feed(tricky + tricky), [tricky, tricky])

    def test_oversized_and_broken_frames_are_dropped(self):
        good = jpeg()
        splitter = Splitter(limit=len(good) + 10)
        huge = b"\xff\xd8\xff\xfe\x00\x10" + b"a" * 14 + b"\xff\xda\x00\x02" + b"b" * (len(good) * 2)
        self.assertEqual(splitter.feed(huge), [])
        self.assertEqual(splitter.feed(good), [good])
        self.assertEqual(Splitter().feed(b"\xff\xd8\x00\x00garbage" + good), [good])

    def test_size_comes_from_the_frame_header(self):
        self.assertEqual(jpeg_size(jpeg(size=(320, 180))), (320, 180))
        self.assertIsNone(jpeg_size(b"\x89PNG"))
        self.assertIsNone(jpeg_size(b"\xff\xd8\xff\xd9"))

    def test_ffmpeg_command_lines(self):
        linux = camera.command("/usr/bin/ffmpeg", "/dev/v4l/by-path/p-video-index0", "low", "linux")
        self.assertEqual(linux[linux.index("-f") + 1], "v4l2")
        self.assertEqual(linux[linux.index("-video_size") + 1], "640x480")
        self.assertIn("fps=5,scale=640:480", linux[linux.index("-vf") + 1])
        self.assertEqual(linux[-5:], ["-f", "mjpeg", "-q:v", "8", "-"])
        windows = camera.command("ffmpeg.exe", 'Cam "x"', "high", "windows")
        self.assertIn('video=Cam "x"', windows)  # one argv item: no shell parses it
        self.assertNotIn("-video_size", windows)
        self.assertIn("fps=10,scale=1280:720", windows[windows.index("-vf") + 1])
        self.assertEqual(set(PRESETS), {"low", "medium", "high"})

    def test_ffmpeg_errors_in_plain_words(self):
        self.assertIn("in use by another app",
                      camera.explain(["[video4linux2] ioctl(VIDIOC_STREAMON): Device or resource busy"], "linux"))
        self.assertIn("Privacy & security > Camera",
                      camera.explain(["Could not run graph (sometimes caused by a device already in use)",
                                      "video=Cam: I/O error"], "windows"))
        self.assertEqual(camera.explain([], "linux"), "The camera stopped (ffmpeg exited).")

    def test_job_validation(self):
        good = {"op": "start", "session": SID, "quality": "low", "key": "d" * 32}
        camera.validate(good)
        camera.validate({"op": "stop", "session": SID})
        for bad in ({**good, "op": "record"}, {**good, "session": "x"}, {**good, "quality": "4k"},
                    {**good, "key": "nothex"}, {**good, "device": "/dev/video0"},
                    {**good, "by": "phone"}, None, "start"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                camera.validate(bad)


class SessionTests(unittest.TestCase):
    """camera.run with fakes: ffmpeg, the hub (a real Sessions), notifications, the clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": True, "device": "/dev/video1"}))
        self.notes = []
        self.sessions = Sessions()
        self.addCleanup(lambda: self.sessions.close_all("test over"))
        self.addCleanup(camera.end_all, "test over", 2)
        self.clock = Clock()

    def open(self, quality="low"):
        view = self.sessions.open("node", quality, kind="camera", opener="phone:p1")
        self.sessions.touch(view["id"])
        return view["id"]

    def start(self, popen=None, which="/usr/bin/ffmpeg", **kw):
        sid = self.open()
        self.client = HubClient(self.sessions)
        self.popen = popen or fake_popen()
        payload = {"op": "start", "session": sid, "quality": "low", "key": self.sessions.key(sid)}
        kw.setdefault("notify", lambda text, title=None: self.notes.append(text))
        result = camera.run(payload, {}, self.state, client=self.client, os_name="linux",
                            which=lambda name: which, popen=self.popen, clock=self.clock, **kw)
        return sid, result

    def test_frames_reach_the_hub_and_disconnect_stops_ffmpeg(self):
        sid, result = self.start()
        self.assertEqual(result, {"ok": True, "text": "Camera on."})
        proc = self.popen.procs[0]
        self.assertEqual(proc.argv[proc.argv.index("-i") + 1], "/dev/video1")
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 2))
        seq, data, width, height = self.sessions.wait_frame(sid, 0, hold=0)
        self.assertEqual((data[:2], width, height), (b"\xff\xd8", 64, 48))
        self.assertEqual(self.sessions.get(sid)["state"], "live")
        self.assertIn("camera on", self.notes[0])
        self.assertTrue((self.state / camera.MARKER).exists())
        self.assertTrue(camera.busy(self.state))
        # Never the screen session's registry or marker: those would lend it keys and Lan Mouse.
        self.assertEqual(remote_view._ACTIVE, {})
        self.assertFalse((self.state / remote_view.MARKER).exists())

        self.sessions.close(sid, "Disconnected.")
        session = camera._ACTIVE.get(sid)
        self.assertTrue(until(lambda: sid not in camera._ACTIVE))
        session.join(2)
        self.assertEqual(proc.calls, ["terminate"])
        self.assertIsNotNone(proc.returncode)
        self.assertIn("camera off", self.notes[-1])
        self.assertFalse((self.state / camera.MARKER).exists())
        self.assertFalse(any(p.get("end") for p in self.client.posts))  # the hub ended it: nothing to tell
        log = (self.state / "logs" / "control.jsonl").read_text()
        self.assertIn("camera_start", log)
        self.assertIn("camera_end", log)

    def test_refused_without_consent_ffmpeg_or_a_camera(self):
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": False, "device": "/dev/video1"}))
        popen = fake_popen()
        _, result = self.start(popen)
        self.assertFalse(result["ok"])
        self.assertIn("jarvis camera allow on", result["text"])
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": True}))
        _, result = self.start(popen, which=None)
        self.assertIn("ffmpeg isn't installed", result["text"])
        _, result = self.start(popen, root=self.tmp.name)  # an empty sysfs
        self.assertIn("No camera found", result["text"])
        self.assertEqual(popen.procs, [])  # nothing was ever opened
        self.assertEqual(self.notes, [])

    def test_a_busy_camera_fails_the_start_with_its_reason(self):
        popen = fake_popen(quit_at_once=True,
                           stderr=["[video4linux2,v4l2 @ 0x5] ioctl(VIDIOC_STREAMON): Device or resource busy"])
        sid, result = self.start(popen)
        self.assertFalse(result["ok"])
        self.assertIn("in use by another app", result["text"])
        self.assertEqual(camera._ACTIVE, {})
        self.assertEqual(self.notes, [])  # it never turned on
        self.assertFalse((self.state / camera.MARKER).exists())

    def test_a_start_that_breaks_midway_still_stops_ffmpeg(self):
        def broken(text, title=None):
            raise RuntimeError("notification service crashed")
        with self.assertRaises(RuntimeError):
            self.start(notify=broken)
        self.assertEqual(self.popen.procs[0].calls, ["terminate"])
        self.assertEqual(camera._ACTIVE, {})
        self.assertFalse((self.state / camera.MARKER).exists())

    def test_control_stop_here_ends_it(self):
        sid, _ = self.start()
        proc = self.popen.procs[0]
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        # What `jarvis control stop` writes (not control.stop(): that also touches the real mouse).
        (self.state / control.STOP_FILE).write_text(str(time.time() + 600))
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assertIn("stopped on this computer", self.sessions.get(sid)["error"])
        self.assertIn("terminate", proc.calls)
        # and no new one starts while control is stopped
        _, result = self.start()
        self.assertIn("control is stopped", result["text"])

    def test_withdrawn_consent_ends_a_running_session(self):
        sid, _ = self.start()
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": False}))
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assertIn("turned off for JARVIS", self.sessions.get(sid)["error"])

    def stalled(self, sid):
        """The link to the main stalls: every post now hangs (until the test ends)."""
        session = camera._ACTIVE[sid]
        gate = threading.Event()
        self.addCleanup(gate.set)  # runs before end_all's cleanup: the stuck post can finish
        calls = []

        class Stalled:
            def request(self, path, body, timeout=None):
                calls.append(path)
                gate.wait(timeout if timeout is not None else 30)
                raise OSError("timed out")
        session.client = Stalled()
        self.assertTrue(until(lambda: calls, 3), "the sender is stuck in a post")
        return session

    def test_control_stop_ends_it_at_once_even_while_a_post_is_stuck(self):
        sid, _ = self.start()
        proc = self.popen.procs[0]
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        session = self.stalled(sid)
        (self.state / control.STOP_FILE).write_text(str(time.time() + 600))
        self.assertTrue(until(lambda: proc.returncode is not None, 1.0), "ffmpeg stopped within a second")
        self.assertIn("stopped on this computer", session.why)
        self.assertTrue(until(lambda: "camera off" in self.notes[-1], 1.0), "and the PC says so at once")
        self.assertEqual(sum("camera off" in n for n in self.notes), 1)

    def test_withdrawn_consent_ends_it_at_once_even_while_a_post_is_stuck(self):
        sid, _ = self.start()
        proc = self.popen.procs[0]
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        session = self.stalled(sid)
        (self.state / camera.SETTINGS).write_text(json.dumps({"allow": False}))
        self.assertTrue(until(lambda: proc.returncode is not None, 1.0), "ffmpeg stopped within a second")
        self.assertIn("turned off for JARVIS", session.why)

    def test_a_stalled_link_ends_it_after_link_loss_not_after_the_retries(self):
        sid, _ = self.start()
        proc = self.popen.procs[0]
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        session = camera._ACTIVE[sid]
        with patch.object(camera.CameraSession, "LINK_LOSS", 0.5):
            self.stalled(sid)
            self.assertTrue(until(lambda: proc.returncode is not None, 2.0))
        self.assertIn("Lost the link", session.why)

    def test_sessions_end_after_fifteen_minutes(self):
        sid, _ = self.start()
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        self.clock.now += camera.MAX_SESSION
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assertIn("15 minutes", self.sessions.get(sid)["error"])
        self.assertTrue(until(lambda: self.popen.procs[0].returncode is not None))

    def test_ffmpeg_that_ignores_terminate_is_killed(self):
        sid, _ = self.start(fake_popen(stubborn=True))
        proc = self.popen.procs[0]
        with patch.object(camera, "TERM_WAIT", 0.1):
            camera.end_all("test", wait=2)
        self.assertEqual(proc.calls, ["terminate", "kill"])
        self.assertEqual(camera._ACTIVE, {})
        self.assertEqual(self.sessions.get(sid)["state"], "ended")  # the hub was told at once
        self.assertIn({"session": sid, "end": "test"}, self.client.posts)

    def test_ffmpeg_dying_mid_stream_ends_it_with_the_reason(self):
        sid, _ = self.start(fake_popen(stderr=["/dev/video1: No such file or directory"]))
        proc = self.popen.procs[0]
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        proc._die(1)  # unplugged
        self.assertTrue(until(lambda: self.sessions.get(sid)["state"] == "ended"))
        self.assertIn("isn't there any more", self.sessions.get(sid)["error"])

    def test_a_reply_without_the_sessions_mac_ends_it(self):
        sid, _ = self.start()
        self.assertTrue(until(lambda: self.sessions.get(sid)["frames"] >= 1))
        session = camera._ACTIVE[sid]
        session.client = type("Squatter", (), {"request": lambda self, *a, **k: {"stop": False}})()
        self.assertTrue(until(lambda: session.done.is_set()))
        self.assertIn("ended for safety", session.why)
        self.assertIn("terminate", self.popen.procs[0].calls)

    def test_a_refusal_from_the_main_ends_it_at_once(self):
        from jarvis.cluster import HubRefused
        sid, _ = self.start()
        session = camera._ACTIVE[sid]

        class Refusing:
            def request(self, path, body, timeout=None):
                raise HubRefused("Main PC refused the request: not enrolled", 403)
        session.client = Refusing()
        self.assertTrue(until(lambda: session.done.is_set(), 3))
        self.assertIn("refused", session.why)

    def test_a_new_session_replaces_the_old_and_stop_jobs_end_it(self):
        first, _ = self.start()
        old = self.popen.procs[0]
        second, _ = self.start(self.popen)
        self.assertIn("terminate", old.calls)
        self.assertEqual(list(camera._ACTIVE), [second])
        self.assertEqual(camera.run({"op": "stop", "session": second}, {}, self.state),
                         {"ok": True, "text": "Stopped."})
        self.assertTrue(until(lambda: not camera._ACTIVE))
        self.assertIn("terminate", self.popen.procs[1].calls)

    def test_status_reports_consent_device_ffmpeg_and_busy(self):
        report = camera.status({}, self.state, os_name="linux", which=lambda name: "/usr/bin/ffmpeg")
        self.assertEqual(report, {"allowed": True, "device": "/dev/video1", "ffmpeg": "/usr/bin/ffmpeg",
                                  "busy": False, "why": None})
        self.assertEqual(camera.ready({}, self.state, os_name="linux", which=lambda name: None),
                         (False, "ffmpeg isn't installed on this computer, so JARVIS can't open the camera."))


if __name__ == "__main__":
    unittest.main()
