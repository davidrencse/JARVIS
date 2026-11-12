"""WS2 membership: join/leave under the managed lock, autostart cleanup, configure guard.

Temp directories only. What is fake is named in the test that uses it. The loopback test
runs production Hub, Client, and Broker in separate processes: real HTTP and real SQLite.
It does not open SSH, Tailscale, the user's authorized_keys, or a model.
"""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from jarvis import autostart, cluster_cli, cluster_setup, config_lock
from jarvis.cluster import Hub
from jarvis.config_lock import ConfigBusy, ConfigDrift

REPO = Path(__file__).resolve().parents[2]
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
KEY_TEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
# Echoed by the hub process and asserted here, so a pass states the substituted boundaries.
FAKES = [
    "ssh host key is a fixture, not this machine's sshd",
    "sshd port-22 probe is forced true",
    "address discovery returns 203.0.113.10 and does not call tailscale",
    "authorized_keys is a temp file, not the user ~/.ssh file",
    "invite user is the fixture jdoe",
    "tunnel launch is skipped (join start=False); no ssh process",
]


def _note(node="dell", main="zenbook", token=None, port=7780, key=None):
    return {"v": 1, "node_id": node, "main_device": main, "token": token or ("t" * 32),
            "host_key": HOST_KEY, "key": KEY_TEXT if key is None else key, "port": port,
            "user": "jdoe", "addresses": ["203.0.113.10"], "code_key": HOST_KEY,
            "main": {"hostname": "arch", "label": "Zenbook"}}


def _fleet(local, main="zenbook", enabled=True, port=7780):
    return {"cluster": {"enabled": enabled, "main_device": main, "local_device": local,
                        "tunnel": "forward", "listen_host": "127.0.0.1", "listen_port": port,
                        "main_url": f"http://127.0.0.1:{port}"},
            "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"},
                        "dell": {"hostname": "WinLaptop", "label": "Dell"}},
            "keep": {"mine": True}}


def _cli(path, state, action, **kw):
    base = dict(action=action, config=str(path), state=str(state), value=None,
                main_device=None, local_device=None, url=None, hostname=None, capability=None,
                output=None, device_id=None, address=[], no_autostart=True, no_start=True,
                port=7777, rollback=False, autostart=False)
    return argparse.Namespace(**{**base, **kw})


def _say(payload):
    print("WS2 " + json.dumps(payload), flush=True)


def _sqlite_expires(db_path):
    uri = "file:" + Path(db_path).resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        return conn.execute("SELECT invite_expires FROM nodes WHERE id=?", ("dell",)).fetchone()
    finally:
        conn.close()


class _Pipe:
    """Drain a worker's stdout so a chatty hub cannot fill the pipe and stall."""

    def __init__(self, proc):
        self.proc = proc
        self.lines = []
        self.ready = []
        self._lock = threading.Lock()
        self._wait = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line)
                if line.startswith("WS2 "):
                    self.ready.append(json.loads(line[4:]))
            self._wait.set()
        self._wait.set()

    def next(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.ready:
                    return self.ready.pop(0)
                self._wait.clear()
            self._wait.wait(max(0.05, deadline - time.monotonic()))
        raise AssertionError("worker timed out\n" + "".join(self.lines[-40:]))

    def text(self):
        with self._lock:
            return "".join(self.lines)


def hub_worker():
    """Production hub process. SSH discovery and the keys file are the fakes listed above."""
    root = Path(sys.argv[2])
    hub = None
    try:
        port_sock = __import__("socket").socket()
        port_sock.bind(("127.0.0.1", 0))
        port = port_sock.getsockname()[1]
        port_sock.close()
        config = _fleet("auto", port=port)
        path, state = root / "main.json", root / "main-state"
        path.write_text(json.dumps(config), encoding="utf-8")
        keys = root / "authorized_keys"
        keys.write_text("", encoding="utf-8")
        patches = [
            mock.patch.object(cluster_setup, "_sshd_up", return_value=True),
            mock.patch.object(cluster_setup, "host_key", return_value=HOST_KEY),
            mock.patch.object(cluster_setup, "addresses", return_value=["203.0.113.10"]),
            mock.patch.object(cluster_setup, "_authorized_keys", return_value=keys),
            mock.patch.object(cluster_setup, "_hostname", return_value="arch"),
            mock.patch("jarvis.topology.socket.gethostname", return_value="arch"),
            mock.patch.object(cluster_setup.getpass, "getuser", return_value="jdoe"),
        ]
        for item in patches:
            item.start()
        report = cluster_setup.invite(config, path, state, "dell")
        fresh = json.loads(path.read_text(encoding="utf-8"))
        hub = Hub(fresh, root, state).start(host="127.0.0.1", port=port)
        _say({"ready": True, "port": port, "code": report["code"], "fake": FAKES,
              "keys": str(keys)})
        for line in sys.stdin:
            cmd = line.strip()
            if cmd == "quit" or cmd == "":
                if cmd == "quit":
                    break
                continue
            if cmd == "revoke":
                _say({"revoked": cluster_setup.revoke(state, "dell")})
            elif cmd == "invite":
                fresh = json.loads(path.read_text(encoding="utf-8"))
                again = cluster_setup.invite(fresh, path, state, "dell")
                _say({"code": again["code"]})
            else:
                _say({"error": "unknown " + cmd})
        _say({"bye": True})
    except Exception as exc:  # the parent asserts on this line; the process must not hang
        _say({"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if hub is not None:
            hub.close()


def node_worker():
    """Production node process. Joins over HTTP to the hub. Does not launch ssh."""
    root = Path(sys.argv[2])
    path, state = root / "node.json", root / "node-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "notes.txt").write_text("keep-me", encoding="utf-8")
    (state / "logs").mkdir(exist_ok=True)
    (state / "logs" / "voice.log").write_text("voice", encoding="utf-8")
    (state / "known-host-custom").write_text("unrelated\n", encoding="ascii")
    host = mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop")
    name = mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop")
    host.start()
    name.start()
    try:
        _say({"ready": True})
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue
            if cmd == "quit":
                _say({"bye": True})
                return
            if cmd == "leave":
                disk = json.loads(path.read_text(encoding="utf-8"))
                report = cluster_setup.leave(disk, path, state)
                _say({"left": report.get("left"), "enabled": json.loads(
                    path.read_text(encoding="utf-8"))["cluster"]["enabled"],
                    "cleanup_error": report.get("cleanup_error"),
                    "notes": (state / "notes.txt").read_text(encoding="utf-8"),
                    "voice": (state / "logs" / "voice.log").read_text(encoding="utf-8"),
                    "custom_host": (state / "known-host-custom").is_file(),
                    "credential": (state / "cluster-node.json").is_file(),
                    "tunnel_status": (state / "cluster-tunnel.json").is_file()})
                continue
            if cmd == "status":
                from jarvis.cluster import Client, HubRefused
                disk = json.loads(path.read_text(encoding="utf-8"))
                try:
                    Client(disk, state).request("/v1/status", timeout=5)
                    _say({"status": "open"})
                except HubRefused as exc:
                    _say({"status": "refused", "code": exc.status})
                continue
            if cmd.startswith("join "):
                code = cmd[5:].strip()
                disk = json.loads(path.read_text(encoding="utf-8"))
                note = cluster_setup.decode(code)
                joined = cluster_setup.join(disk, path, state, note, autostart=False, start=False)
                saved = json.loads(path.read_text(encoding="utf-8"))
                from jarvis.cluster import Client, redeem_invite
                redeemed = redeem_invite(saved, state)
                status = Client(saved, state).request("/v1/status", timeout=5)
                old = note["token"]
                request = urllib.request.Request(
                    saved["cluster"]["main_url"] + "/v1/status",
                    headers={"Authorization": "Bearer " + old, "X-JARVIS-Main": "zenbook"})
                try:
                    urllib.request.urlopen(request, timeout=5)
                    invite_dead = False
                    http_status = 200
                except urllib.error.HTTPError as exc:
                    invite_dead = exc.code == 403
                    http_status = exc.code
                cred = json.loads((state / "cluster-node.json").read_text(encoding="utf-8"))
                _say({"joined": joined["joined"], "keep": saved.get("keep"),
                      "redeemed": redeemed, "status_main": status.get("main_device"),
                      "invite_http": http_status, "invite_dead": invite_dead,
                      "redeem_flag_gone": "redeem" not in cred,
                      "pending_key": (state / "cluster-node-pending.key").is_file(),
                      "tunnel_status": (state / "cluster-tunnel.json").is_file(),
                      "real": ["loopback HTTP /v1/redeem and /v1/status",
                               "sqlite cluster.sqlite3 on the hub",
                               "local ssh-keygen for the node key and the code-signing key"]})
                continue
            _say({"error": "unknown " + cmd})
    except Exception as exc:
        _say({"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        host.stop()
        name.stop()


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "jarvis.config.json"
        self.state = self.tmp / ".jarvis"
        self.state.mkdir()
        self.procs = []
        self.addCleanup(self._stop)

    def _stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def _write(self, doc):
        self.path.write_text(json.dumps(doc), encoding="utf-8")

    def _disk(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _plant_link(self):
        (self.state / "cluster-node.json").write_text(json.dumps({
            "node_id": "dell", "main_device": "zenbook", "token": "t" * 32,
            "link": {"hostname": "arch", "identity_file": str(self.state / "cluster-node.key")}}),
            encoding="utf-8")
        for name in ("cluster-node.key", "cluster-node-pending.key", "cluster-node-launch.vbs",
                     "cluster-tunnel.json", "cluster-tunnel.stop", "cluster-tunnel.lock",
                     "cluster-tunnel-launch.vbs", "known-host-jarvis-arch"):
            (self.state / name).write_text("link\n", encoding="utf-8")
        (self.state / "logs").mkdir(exist_ok=True)
        (self.state / "logs" / "cluster-tunnel.log").write_text("tunnel\n", encoding="utf-8")
        (self.state / "logs" / "voice.log").write_text("voice\n", encoding="utf-8")
        (self.state / "notes.txt").write_text("keep\n", encoding="utf-8")
        (self.state / "cluster.sqlite3").write_bytes(b"SQLite format 3\0unrelated")
        (self.state / "known-host-custom").write_text("other\n", encoding="utf-8")

    def test_stale_join_refuses_to_demote_a_main(self):
        disk = _fleet("zenbook")
        self._write(disk)
        before = self.path.read_bytes()
        stale = _fleet("dell")  # this copy still thinks the PC is a node
        with self.assertRaisesRegex(ValueError, "jarvis cluster main"):
            cluster_setup.join(stale, self.path, self.state, _note(), autostart=False, start=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())

    def test_stale_leave_refuses_to_demote_a_main(self):
        self._write(_fleet("zenbook"))
        self._plant_link()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "jarvis cluster main"):
            cluster_setup.leave(_fleet("dell"), self.path, self.state)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.state / "cluster-node.json").is_file())
        self.assertTrue((self.state / "notes.txt").is_file())
        self.assertTrue(self._disk()["cluster"]["enabled"])
        self.assertEqual(self._disk()["cluster"]["main_device"], "zenbook")

    def test_join_leave_rejoin_keeps_unrelated_state(self):
        self._write(_fleet("dell"))
        (self.state / "notes.txt").write_text("keep\n", encoding="utf-8")
        (self.state / "cluster.sqlite3").write_bytes(b"SQLite format 3\0node-db")
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"):
            first = cluster_setup.join(_fleet("dell"), self.path, self.state, _note(token="a" * 32),
                                       autostart=False, start=False)
            self.assertEqual(first["joined"], "dell")
            self.assertEqual(self._disk()["keep"], {"mine": True})
            self.assertNotIn("PRIVATE KEY", self.path.read_text(encoding="utf-8"))
            self.assertTrue((self.state / "cluster-node.key").is_file())
            # leave() turns login autostart off through the real unit path. Patch install
            # so this never systemctl-disables or unlinks this machine's jarvis-cluster unit.
            with mock.patch("jarvis.autostart.install",
                            return_value={"autostart": False, "path": ""}):
                left = cluster_setup.leave(self._disk(), self.path, self.state)
        self.assertEqual(left["left"], "dell")
        self.assertFalse(self._disk()["cluster"]["enabled"])
        self.assertEqual(self._disk()["keep"], {"mine": True})
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertTrue((self.state / "cluster.sqlite3").read_bytes().startswith(b"SQLite format 3"))
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"):
            again = cluster_setup.join(self._disk(), self.path, self.state, _note(token="b" * 32),
                                       autostart=False, start=False)
        self.assertEqual(again["joined"], "dell")
        self.assertTrue(self._disk()["cluster"]["enabled"])
        self.assertIn("b" * 32, (self.state / "cluster-node.json").read_text(encoding="utf-8"))
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")

    def test_partial_cleanup_reports_the_failure_and_rejoin_works(self):
        self._write(_fleet("dell"))
        self._plant_link()
        entries = self.tmp / "startup"
        entries.mkdir()
        for name in ("jarvis-cluster-tunnel", "jarvis-cluster"):
            (entries / name).write_text("unit\n", encoding="utf-8")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "supervisor down")

        real_unlink = Path.unlink

        def flaky(self, missing_ok=False):
            if self.name == "cluster-node.key":
                raise OSError("key file busy")
            return real_unlink(self, missing_ok=missing_ok)

        with mock.patch.object(autostart, "system", return_value="linux"), \
                mock.patch.object(autostart, "path", side_effect=lambda name, title: entries / name), \
                mock.patch.object(autostart.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(Path, "unlink", flaky):
            report = cluster_setup.leave(_fleet("dell"), self.path, self.state)
        self.assertIn("key file busy", report["cleanup_error"])
        self.assertIn("supervisor down", report["tunnel_autostart_error"])
        self.assertIn("supervisor down", report["node_autostart_error"])
        self.assertFalse(self._disk()["cluster"]["enabled"])
        self.assertTrue((self.state / "cluster-node.key").is_file())
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "known-host-jarvis-arch").exists())
        self.assertFalse((self.state / "logs" / "cluster-tunnel.log").exists())
        self.assertFalse((self.state / "cluster-node-pending.key").exists())
        self.assertFalse((entries / "jarvis-cluster").exists())
        self.assertFalse((entries / "jarvis-cluster-tunnel").exists())
        self.assertEqual((self.state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertEqual((self.state / "logs" / "voice.log").read_text(encoding="utf-8"), "voice\n")
        self.assertTrue((self.state / "known-host-custom").is_file())
        self.assertTrue((self.state / "cluster.sqlite3").is_file())
        self.assertEqual(self._disk()["keep"], {"mine": True})
        self.assertTrue(calls)
        for argv in calls:
            self.assertFalse(any(word in {"kill", "taskkill", "pkill", "killall"} for word in argv))
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"):
            again = cluster_setup.join(self._disk(), self.path, self.state,
                                       _note(token="c" * 32, key=KEY_TEXT + "second\n"),
                                       autostart=False, start=False)
        self.assertEqual(again["joined"], "dell")
        self.assertTrue(self._disk()["cluster"]["enabled"])
        self.assertIn("second", (self.state / "cluster-node.key").read_text(encoding="utf-8"))

    def test_autostart_cleanup_all_os_uses_a_fake_supervisor_only(self):
        # Windows: delete the Startup launcher. No supervisor and no pid signal.
        target = self.tmp / "windows.vbs"
        target.write_text("launcher", encoding="utf-8")
        called = []
        with mock.patch.object(autostart, "system", return_value="windows"), \
                mock.patch.object(autostart, "path", return_value=target), \
                mock.patch.object(autostart.subprocess, "run",
                                 side_effect=lambda *a, **k: called.append(a)):
            gone = autostart.install("jarvis-cluster", "JARVIS Cluster", ["python", "-m", "jarvis"],
                                     enable=False)
        self.assertFalse(target.exists())
        self.assertEqual(called, [])
        self.assertFalse(gone["autostart"])

        # macOS: launchctl fails, the plist is still removed, the error names launchctl.
        target = self.tmp / "macos.plist"
        target.write_text("plist", encoding="utf-8")
        calls = []

        def macos_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "launchctl refused")

        with mock.patch.object(autostart, "system", return_value="macos"), \
                mock.patch.object(autostart, "path", return_value=target), \
                mock.patch.object(autostart.subprocess, "run", side_effect=macos_run):
            with self.assertRaises(subprocess.SubprocessError) as caught:
                autostart.install("jarvis-cluster", "JARVIS Cluster", ["python"], enable=False)
        self.assertFalse(target.exists())
        self.assertIn("launchctl", str(caught.exception))
        self.assertIn("launchctl refused", str(caught.exception))
        self.assertEqual(calls, [["launchctl", "unload", "-w", str(target)]])

        # Linux: systemctl cannot be started, the unit is still removed, daemon-reload is still tried.
        target = self.tmp / "linux.service"
        target.write_text("unit", encoding="utf-8")
        calls = []

        def linux_run(argv, **kwargs):
            calls.append(list(argv))
            if "disable" in argv:
                raise OSError("systemctl missing")
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(autostart, "system", return_value="linux"), \
                mock.patch.object(autostart, "path", return_value=target), \
                mock.patch.object(autostart.subprocess, "run", side_effect=linux_run):
            with self.assertRaises(subprocess.SubprocessError) as caught:
                autostart.install("jarvis-cluster", "JARVIS Cluster", ["python"], enable=False)
        self.assertFalse(target.exists())
        self.assertIn("systemctl missing", str(caught.exception))
        self.assertEqual(calls[0][:3], ["systemctl", "--user", "disable"])
        self.assertEqual(calls[1][:3], ["systemctl", "--user", "daemon-reload"])
        for argv in calls:
            self.assertNotIn("kill", argv)
            self.assertNotIn("taskkill", argv)

    def test_configure_refuses_an_enabled_main_and_allows_first_setup(self):
        self._write(_fleet("zenbook"))
        before = self.path.read_bytes()
        stale = _fleet("dell")
        with self.assertRaisesRegex(ValueError, "jarvis cluster main dell"):
            cluster_cli.run(_cli(self.path, self.state, "configure", main_device="dell",
                                 local_device="dell"), stale, self.tmp)
        self.assertEqual(self.path.read_bytes(), before)

        disabled = _fleet("dell", enabled=False)
        disabled["cluster"]["main_device"] = "dell"
        self._write(disabled)
        out = cluster_cli.run(_cli(self.path, self.state, "configure", main_device="zenbook",
                                   local_device="zenbook"), {"devices": {}}, self.tmp)
        self.assertEqual((out["role"], out["main_device"]), ("main", "zenbook"))
        self.assertEqual(self._disk()["keep"], {"mine": True})
        self.assertTrue(self._disk()["cluster"]["enabled"])

    def test_join_waits_on_the_managed_lock_then_sees_the_new_main(self):
        self._write(_fleet("dell"))
        script = (
            "import json,sys; sys.path.insert(0, sys.argv[1]);"
            "from jarvis.config_lock import config_mutation;"
            "cm = config_mutation(sys.argv[2]); cm.__enter__(); print('held', flush=True);"
            "line = sys.stdin.readline();\n"
            "doc = json.loads(open(sys.argv[3], encoding='utf-8').read());"
            "doc['cluster'].update(enabled=True, main_device='zenbook', local_device='zenbook');"
            "open(sys.argv[3], 'w', encoding='utf-8').write(json.dumps(doc));"
            "print('promoted', flush=True); cm.__exit__(None, None, None); print('released', flush=True)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(REPO), str(self.state), str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "JARVIS_NO_MODELS": "1"})
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        with mock.patch.object(config_lock, "DEFAULT_TIMEOUT", 0.2):
            with self.assertRaises(ConfigBusy):
                cluster_setup.join(_fleet("dell"), self.path, self.state, _note(),
                                   autostart=False, start=False)
        self.assertFalse((self.state / "cluster-node.json").exists())
        proc.stdin.write("go\n")
        proc.stdin.flush()
        self.assertEqual(proc.stdout.readline().strip(), "promoted")
        self.assertEqual(proc.stdout.readline().strip(), "released")
        proc.wait(10)
        with self.assertRaisesRegex(ValueError, "jarvis cluster main"):
            cluster_setup.join(_fleet("dell"), self.path, self.state, _note(),
                               autostart=False, start=False)
        disk = self._disk()
        self.assertEqual(disk["cluster"]["main_device"], "zenbook")
        self.assertTrue(disk["cluster"]["enabled"])
        self.assertEqual(disk["keep"], {"mine": True})
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())

    def _host(self):
        return mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
            mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"), \
            mock.patch("jarvis.fleet.legacy_topology.socket.gethostname", return_value="WinLaptop")

    def _plant_owned(self):
        self._write(_fleet("dell"))
        (self.state / "cluster-node.key").write_text("ORIGINAL-KEY\n", encoding="utf-8")
        (self.state / "cluster-node.json").write_text(json.dumps({
            "node_id": "dell", "main_device": "zenbook", "token": "t" * 32,
            "link": {"targets": ["jdoe@203.0.113.10"], "hostname": "arch"}}), encoding="utf-8")

    def test_join_refuses_enabled_malformed_identity(self):
        disk = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "nope"},
                "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"}}}
        self._write(disk)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity cannot be read"):
            cluster_setup.join(disk, self.path, self.state, _note(), autostart=False, start=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.state / "cluster-node.json").exists())
        self.assertFalse((self.state / "cluster-node.key").exists())

    def test_leave_refuses_enabled_malformed_identity(self):
        disk = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "nope"},
                "devices": {"zenbook": {"hostname": "arch", "label": "Zenbook"}}}
        self._write(disk)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity cannot be read"):
            cluster_setup.leave(disk, self.path, self.state)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self._disk()["cluster"]["enabled"])

    def test_leave_refuses_the_disk_main_even_when_the_caller_is_main(self):
        disk = _fleet("zenbook")
        self._write(disk)
        self._plant_link()
        before = self.path.read_bytes()
        key = (self.state / "cluster-node.key").read_bytes()
        with self.assertRaisesRegex(ValueError, "jarvis cluster main"):
            cluster_setup.leave(disk, self.path, self.state)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.state / "cluster-node.key").read_bytes(), key)
        self.assertTrue((self.state / "cluster-node.json").is_file())
        self.assertTrue(self._disk()["cluster"]["enabled"])
        self.assertEqual(self._disk()["cluster"]["main_device"], "zenbook")

    def test_join_does_not_follow_or_replace_a_key_symlink(self):
        outside = self.tmp / "outside.key"
        outside.write_text("DO-NOT-READ\n", encoding="utf-8")
        self._write(_fleet("dell"))
        link = self.state / "cluster-node.key"
        link.symlink_to(outside)
        before = self.path.read_bytes()
        patches = self._host()
        with patches[0], patches[1], patches[2]:
            with self.assertRaisesRegex(ValueError, "not a regular file") as caught:
                cluster_setup.join(_fleet("dell"), self.path, self.state, _note(),
                                   autostart=False, start=False)
        self.assertNotIn("DO-NOT-READ", str(caught.exception))
        self.assertEqual(outside.read_text(encoding="utf-8"), "DO-NOT-READ\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.state / "cluster-node.json").exists())

    def test_join_backup_drift_restores_the_original_key_and_note(self):
        self._plant_owned()
        key_path = self.state / "cluster-node.key"
        note_path = self.state / "cluster-node.json"
        key_bytes = key_path.read_bytes()
        note_bytes = note_path.read_bytes()
        seen = {}
        real = config_lock._backup

        def drift(path, raw, tag, mode):
            seen["key"] = key_path.read_bytes()
            seen["note"] = note_path.read_bytes()
            made = real(path, raw, tag, mode)
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            doc["concurrent"] = {"from": "editor"}
            doc["keep"] = {"other": 1}
            Path(path).write_text(json.dumps(doc), encoding="utf-8")
            return made

        patches = self._host()
        with patches[0], patches[1], patches[2], \
                mock.patch("jarvis.config_lock._backup", side_effect=drift):
            with self.assertRaises(ConfigDrift):
                cluster_setup.join(_fleet("dell"), self.path, self.state, _note(),
                                   autostart=False, start=False)
        self.assertIn(b"BEGIN OPENSSH PRIVATE KEY", seen["key"])
        self.assertNotEqual(seen["key"], key_bytes)
        self.assertNotEqual(seen["note"], note_bytes)
        self.assertEqual(key_path.read_bytes(), key_bytes)
        self.assertEqual(note_path.read_bytes(), note_bytes)
        disk = self._disk()
        self.assertEqual(disk["concurrent"], {"from": "editor"})
        self.assertEqual(disk["keep"], {"other": 1})
        self.assertEqual(disk["cluster"]["local_device"], "dell")
        self.assertEqual(disk["cluster"]["main_device"], "zenbook")
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", self.path.read_text(encoding="utf-8"))
        backups = list(self.path.parent.glob(self.path.name + ".pre-cluster-join-*"))
        self.assertEqual(len(backups), 1)
        saved = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertNotIn("concurrent", saved)
        self.assertEqual(saved["keep"], {"mine": True})
        names = [p.name for p in self.state.iterdir()]
        self.assertFalse(any(name.endswith(".writing") or ".tmp" in name or name.startswith(".")
                             for name in names))

    def test_join_backup_drift_does_not_clobber_a_concurrent_redemption(self):
        self._plant_owned()
        key_path = self.state / "cluster-node.key"
        note_path = self.state / "cluster-node.json"
        redeemed_key = b"REDEEMED-BY-OTHER\n"
        redeemed_note = b'{"redeemed":true,"token":"not-this-attempt"}\n'
        seen = {}
        real = config_lock._backup

        def drift(path, raw, tag, mode):
            seen["key"] = key_path.read_bytes()
            made = real(path, raw, tag, mode)
            key_path.write_bytes(redeemed_key)
            note_path.write_bytes(redeemed_note)
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            doc["concurrent"] = {"from": "editor"}
            doc["keep"] = {"other": 1}
            Path(path).write_text(json.dumps(doc), encoding="utf-8")
            return made

        patches = self._host()
        with patches[0], patches[1], patches[2], \
                mock.patch("jarvis.config_lock._backup", side_effect=drift):
            with self.assertRaises(ConfigDrift):
                cluster_setup.join(_fleet("dell"), self.path, self.state, _note(),
                                   autostart=False, start=False)
        self.assertIn(b"BEGIN OPENSSH PRIVATE KEY", seen["key"])
        self.assertEqual(key_path.read_bytes(), redeemed_key)
        self.assertEqual(note_path.read_bytes(), redeemed_note)
        disk = self._disk()
        self.assertEqual(disk["concurrent"], {"from": "editor"})
        self.assertEqual(disk["keep"], {"other": 1})
        self.assertEqual(disk["cluster"]["local_device"], "dell")
        self.assertNotIn(b"ORIGINAL-KEY", key_path.read_bytes())
        self.assertNotIn(b"BEGIN OPENSSH PRIVATE KEY", key_path.read_bytes())


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs local ssh-keygen; no SSH session is opened")
class LoopbackMembershipTests(unittest.TestCase):
    """Separate main and node processes on 127.0.0.1.

    Real: Hub HTTP, Client redeem and status, Broker SQLite enroll/revoke.
    Fake: the FAKES list the hub prints (host key, sshd probe, addresses, keys path,
    invite user, no tunnel). ssh-keygen only writes keys under the temp directory.
    """

    def test_redeem_revoke_and_rejoin_over_loopback(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "xdg").mkdir()
        (root / "home").mkdir()
        (root / "node-state").mkdir()
        (root / "node.json").write_text(json.dumps(_fleet("auto", port=9)), encoding="utf-8")
        env = {**os.environ, "JARVIS_NO_MODELS": "1", "XDG_CONFIG_HOME": str(root / "xdg"),
               "HOME": str(root / "home")}
        script = ("import sys; sys.path.insert(0, sys.argv[3]);"
                  "from tests.cluster.test_ws2_membership import hub_worker, node_worker;"
                  "hub_worker() if sys.argv[1] == 'hub' else node_worker()")
        procs = []

        def spawn(role):
            proc = subprocess.Popen(
                [sys.executable, "-c", script, role, str(root), str(REPO)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env, cwd=str(root))
            procs.append(proc)
            return proc, _Pipe(proc)

        def stop():
            for proc in procs:
                if proc.poll() is None:
                    try:
                        proc.stdin.write("quit\n")
                        proc.stdin.flush()
                    except OSError:
                        pass
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
        self.addCleanup(stop)

        hub, hub_pipe = spawn("hub")
        ready = hub_pipe.next(60)
        if ready.get("error"):
            self.fail(ready["error"] + "\n" + hub_pipe.text())
        self.assertEqual(ready["fake"], FAKES)
        self.assertTrue(str(ready["keys"]).startswith(str(root)))
        self.assertNotEqual(Path(ready["keys"]).resolve(), (Path.home() / ".ssh" / "authorized_keys").resolve())
        db = root / "main-state" / "cluster.sqlite3"
        self.assertTrue(db.read_bytes().startswith(b"SQLite format 3"))
        invited = _sqlite_expires(db)
        self.assertIsNotNone(invited)
        self.assertIsNotNone(invited[0])

        node, node_pipe = spawn("node")
        node_ready = node_pipe.next(30)
        self.assertTrue(node_ready.get("ready"), node_pipe.text())
        node.stdin.write("join " + ready["code"] + "\n")
        node.stdin.flush()
        joined = node_pipe.next(60)
        if joined.get("error"):
            self.fail(joined["error"] + "\n" + node_pipe.text() + "\n" + hub_pipe.text())
        self.assertEqual(joined["joined"], "dell")
        self.assertEqual(joined["status_main"], "zenbook")
        self.assertEqual(joined["keep"], {"mine": True})
        self.assertTrue(joined["invite_dead"])
        self.assertEqual(joined["invite_http"], 403)
        self.assertEqual(joined["redeemed"]["token"], True)
        self.assertEqual(joined["redeemed"]["ssh_key"], True)
        self.assertTrue(joined["redeem_flag_gone"])
        self.assertFalse(joined["pending_key"])
        self.assertFalse(joined["tunnel_status"])
        self.assertIn("loopback HTTP", joined["real"][0])
        self.assertIsNone(_sqlite_expires(db)[0])
        keys = (root / "authorized_keys").read_text(encoding="utf-8")
        self.assertIn("jarvis-node:dell", keys)

        hub.stdin.write("revoke\n")
        hub.stdin.flush()
        revoked = hub_pipe.next(30)
        self.assertEqual(revoked["revoked"]["revoked"], "dell")
        self.assertIsNone(_sqlite_expires(db))
        node.stdin.write("status\n")
        node.stdin.flush()
        refused = node_pipe.next(30)
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["code"], 403)

        # Leave on the node, then a new invite is redeemed again. Unrelated files stay.
        (root / "node-state" / "known-host-jarvis-arch").write_text("pin\n", encoding="ascii")
        (root / "node-state" / "cluster-tunnel.json").write_text("{}\n", encoding="utf-8")
        node.stdin.write("leave\n")
        node.stdin.flush()
        left = node_pipe.next(30)
        if left.get("error"):
            self.fail(left["error"] + "\n" + node_pipe.text())
        self.assertEqual(left["left"], "dell")
        self.assertFalse(left["enabled"])
        self.assertFalse(left["credential"])
        self.assertFalse(left["tunnel_status"])
        self.assertEqual(left["notes"], "keep-me")
        self.assertEqual(left["voice"], "voice")
        self.assertTrue(left["custom_host"])
        self.assertFalse((root / "node-state" / "known-host-jarvis-arch").exists())

        hub.stdin.write("invite\n")
        hub.stdin.flush()
        again = hub_pipe.next(60)
        if again.get("error"):
            self.fail(again["error"] + "\n" + hub_pipe.text())
        self.assertIsNotNone(_sqlite_expires(db)[0])
        node.stdin.write("join " + again["code"] + "\n")
        node.stdin.flush()
        rejoined = node_pipe.next(60)
        if rejoined.get("error"):
            self.fail(rejoined["error"] + "\n" + node_pipe.text() + "\n" + hub_pipe.text())
        self.assertEqual(rejoined["joined"], "dell")
        self.assertEqual(rejoined["status_main"], "zenbook")
        self.assertTrue(rejoined["invite_dead"])
        self.assertEqual(rejoined["redeemed"]["token"], True)
        self.assertIsNone(_sqlite_expires(db)[0])
        self.assertTrue((root / "node-state" / "cluster-node.json").is_file())
        self.assertEqual(json.loads((root / "node.json").read_text(encoding="utf-8"))["keep"], {"mine": True})
        self.assertTrue((root / "node-state" / "known-host-custom").is_file())
        self.assertNotIn(str(REPO / ".jarvis"), str(root))


def production_tunnel_worker():
    """Production ``cluster_tunnel.run``. The ssh on PATH is a sleeper with no stdin."""
    root = Path(sys.argv[2])
    os.environ["PATH"] = str(root / "bin") + os.pathsep + os.environ.get("PATH", "")
    from jarvis import cluster_tunnel
    config = json.loads((root / "node.json").read_text(encoding="utf-8"))
    cluster_tunnel.run(config, root / "state")


def stale_heartbeat_worker():
    """Hold the tunnel lock and publish a heartbeat older than status() allows.

    ``cluster_tunnel.status`` then says the worker is stopped. The lock is the
    proof it has not exited. The stop marker is not touched.
    """
    root = Path(sys.argv[2])
    state = root / "state"
    from jarvis.locking import exclusive_lock
    lock = state / "cluster-tunnel.lock"
    release = state / "cluster-tunnel.release"
    with exclusive_lock(lock):
        while not release.exists():
            note = {"pid": os.getpid(), "ssh_alive": False, "updated": time.time() - 120}
            (state / "cluster-tunnel.json").write_text(json.dumps(note), encoding="utf-8")
            time.sleep(0.2)


def stuck_tunnel_worker():
    """Hold the real tunnel lock and publish status until ``cluster-tunnel.release`` appears.

    The stop marker is observed and left in place. This is a worker that does not
    exit within ``cluster_tunnel.stop``'s wait, so leave/join must time out.
    """
    root = Path(sys.argv[2])
    state = root / "state"
    from jarvis.locking import exclusive_lock
    lock = state / "cluster-tunnel.lock"
    release = state / "cluster-tunnel.release"
    marker = state / "cluster-tunnel.stop"
    with exclusive_lock(lock):
        while not release.exists():
            note = {"pid": os.getpid(), "ssh_alive": True, "updated": time.time(),
                    "saw_stop_marker": marker.exists()}
            (state / "cluster-tunnel.json").write_text(json.dumps(note), encoding="utf-8")
            time.sleep(0.2)


class TunnelLifetimeTests(unittest.TestCase):
    """Real subprocess, real exclusive lock, real stop marker. No SSH session and no login startup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.procs = []
        self.logs = []
        self.addCleanup(self._stop)

    def _stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        proc.kill()
                    proc.wait(5)
        for handle in self.logs:
            if not handle.closed:
                handle.close()

    def _spawn(self, role, root):
        script = (
            "import sys; sys.path.insert(0, sys.argv[3]);"
            "from tests.cluster.test_ws2_membership import ("
            "production_tunnel_worker, stuck_tunnel_worker, stale_heartbeat_worker);"
            "role = sys.argv[1];"
            "production_tunnel_worker() if role == 'run' else "
            "stale_heartbeat_worker() if role == 'stale' else stuck_tunnel_worker()"
        )
        log = open(root / "worker.err", "w", encoding="utf-8")
        self.logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, "-c", script, role, str(root), str(REPO)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True,
            start_new_session=True, cwd=str(root),
            env={**os.environ, "JARVIS_NO_MODELS": "1",
                 "PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", ""),
                 "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "xdg")})
        self.procs.append(proc)
        return proc

    def _running(self, state, proc, timeout=15):
        from jarvis import cluster_tunnel
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                text = (state.parent / "worker.err").read_text(encoding="utf-8", errors="replace")
                self.fail("worker exited early\n" + text[-2000:])
            last = cluster_tunnel.status(state)
            if last.get("running"):
                return last
            time.sleep(0.05)
        text = (state.parent / "worker.err").read_text(encoding="utf-8", errors="replace")
        self.fail("worker did not become live: " + json.dumps(last) + "\n" + text[-2000:])

    def _node_root(self):
        root = self.tmp / "node"
        state = root / "state"
        for name in ("bin", "home", "xdg", "state"):
            (root / name).mkdir(parents=True)
        ssh = root / "bin" / "ssh"
        ssh.write_text(f"#!{sys.executable}\nimport time\nwhile True:\n    time.sleep(0.5)\n",
                       encoding="utf-8")
        ssh.chmod(0o755)
        (root / "node.json").write_text(json.dumps(_fleet("dell")), encoding="utf-8")
        key = state / "cluster-node.key"
        key.write_text("ORIGINAL-KEY\n", encoding="utf-8")
        (state / "cluster-node.json").write_text(json.dumps({
            "node_id": "dell", "main_device": "zenbook", "token": "t" * 32,
            "link": {"targets": ["jdoe@203.0.113.10"], "host_key": HOST_KEY, "hostname": "arch",
                     "identity_file": str(key)}}), encoding="utf-8")
        (state / "notes.txt").write_text("keep\n", encoding="utf-8")
        (state / "cluster-node-launch.vbs").write_text("fixed\n", encoding="utf-8")
        (state / "cluster-node-launch-extra.vbs").write_text("unique\n", encoding="utf-8")
        (state / "keep.vbs").write_text("keep-vbs\n", encoding="utf-8")
        return root, state

    def _quiet_autostart(self):
        entries = self.tmp / "startup-entries"
        entries.mkdir(exist_ok=True)
        return mock.patch.object(autostart, "path", side_effect=lambda name, title: entries / name)

    def test_private_file_keeps_the_old_key_when_permissions_fail(self):
        path = self.tmp / "cluster-node.key"
        path.write_text("OLD-KEY\n", encoding="utf-8")

        def boom(target):
            raise OSError("chmod refused")

        with mock.patch("jarvis.platforms.private_key_permissions", side_effect=boom):
            with self.assertRaises(OSError):
                cluster_setup._private_file(path, "NEW-KEY\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "OLD-KEY\n")
        self.assertFalse(path.with_name("cluster-node.key.writing").exists())

    def test_private_file_cleans_the_temp_when_username_is_missing(self):
        path = self.tmp / "cluster-node.key"
        path.write_text("OLD-KEY\n", encoding="utf-8")

        def boom(target):
            raise ValueError("private key permissions need USERNAME of the account that reads the key.")

        with mock.patch("jarvis.platforms.private_key_permissions", side_effect=boom):
            with self.assertRaises(ValueError):
                cluster_setup._private_file(path, "NEW-KEY\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "OLD-KEY\n")
        self.assertFalse(path.with_name("cluster-node.key.writing").exists())
        self.assertNotIn("NEW-KEY", path.read_text(encoding="utf-8"))

    def test_launch_agent_delegates_to_spawn_detached(self):
        state = self.tmp / "state"
        state.mkdir()
        path = self.tmp / "node.json"
        with mock.patch("jarvis.platforms.spawn_detached") as spawn:
            cluster_setup._launch_agent(path, state)
        argv = spawn.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertNotIsInstance(argv, str)
        self.assertIn("cluster", argv)
        self.assertEqual(argv[-1], "run")
        self.assertEqual(spawn.call_args.kwargs["state"], state)

    def test_leave_ignores_identity_file_and_does_not_follow_symlinks(self):
        state = self.tmp / "state"
        state.mkdir()
        path = self.tmp / "node.json"
        config = _fleet("dell")
        path.write_text(json.dumps(config), encoding="utf-8")
        notes = state / "notes.txt"
        notes.write_text("NOTES\n", encoding="utf-8")
        outside = self.tmp / "outside.txt"
        outside.write_text("OUTSIDE\n", encoding="utf-8")
        notes_ino, outside_ino = notes.lstat().st_ino, outside.lstat().st_ino
        key = state / "cluster-node.key"
        key.symlink_to(notes)
        pending = state / "cluster-node-pending.key"
        pending.symlink_to(outside)
        alias = state / "not-ours"
        alias.symlink_to(outside)
        lock = state / "cluster-tunnel.lock"
        lock.write_bytes(b"\0")
        lock_ino = lock.lstat().st_ino
        (state / "cluster-node.json").write_text(json.dumps({
            "node_id": "dell", "main_device": "zenbook", "token": "t" * 32,
            "link": {"hostname": "arch", "identity_file": str(notes)}}), encoding="utf-8")
        with self._quiet_autostart():
            report = cluster_setup.leave(config, path, state)
        self.assertNotIn("tunnel_stop_error", report)
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["cluster"]["enabled"])
        self.assertEqual(notes.read_text(encoding="utf-8"), "NOTES\n")
        self.assertEqual(notes.lstat().st_ino, notes_ino)
        self.assertEqual(outside.read_text(encoding="utf-8"), "OUTSIDE\n")
        self.assertEqual(outside.lstat().st_ino, outside_ino)
        self.assertFalse(key.exists())
        self.assertFalse(pending.exists())
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.read_text(encoding="utf-8"), "OUTSIDE\n")
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.lstat().st_ino, lock_ino)
        self.assertFalse((state / "cluster-node.json").exists())

    def test_production_tunnel_lock_inode_survives_cooperative_stop(self):
        from jarvis import cluster_tunnel
        from jarvis.locking import LockBusy, exclusive_lock
        root, state = self._node_root()
        proc = self._spawn("run", root)
        self._running(state, proc)
        lock = state / "cluster-tunnel.lock"
        inode = lock.lstat().st_ino
        with self.assertRaises(LockBusy):
            with exclusive_lock(lock, timeout=0.3):
                pass
        self.assertEqual(lock.lstat().st_ino, inode)
        stopped = cluster_tunnel.stop(state)
        self.assertTrue(stopped.get("stopped"), stopped)
        proc.wait(15)
        self.assertIsNotNone(proc.returncode)
        self.assertEqual(lock.lstat().st_ino, inode)
        with exclusive_lock(lock, timeout=2):
            pass
        self.assertEqual(lock.lstat().st_ino, inode)
        path = root / "node.json"
        with self._quiet_autostart():
            report = cluster_setup.leave(json.loads(path.read_text(encoding="utf-8")), path, state)
        self.assertNotIn("tunnel_stop_error", report)
        self.assertFalse((state / "cluster-node.json").exists())
        self.assertFalse((state / "cluster-node.key").exists())
        self.assertFalse((state / "cluster-node-launch.vbs").exists())
        self.assertFalse((state / "cluster-node-launch-extra.vbs").exists())
        self.assertEqual((state / "keep.vbs").read_text(encoding="utf-8"), "keep-vbs\n")
        self.assertEqual((state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((state / "cluster-tunnel.stop").exists())
        self.assertFalse((state / "cluster-tunnel.json").exists())
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.lstat().st_ino, inode)

    def test_leave_timeout_preserves_marker_lock_status_and_credentials(self):
        from jarvis.locking import LockBusy, exclusive_lock
        root, state = self._node_root()
        lock = state / "cluster-tunnel.lock"
        lock.write_bytes(b"\0")
        inode = lock.lstat().st_ino
        key_bytes = (state / "cluster-node.key").read_bytes()
        note_bytes = (state / "cluster-node.json").read_bytes()
        proc = self._spawn("stuck", root)
        self._running(state, proc)
        with self.assertRaises(LockBusy):
            with exclusive_lock(lock, timeout=0.3):
                pass
        path = root / "node.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        with self._quiet_autostart():
            report = cluster_setup.leave(config, path, state)
        self.assertIn("still running", report["tunnel_stop_error"])
        self.assertEqual(report["credentials_retained"], "retained until the tunnel stops")
        self.assertIn("still running", report["next"])
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["cluster"]["enabled"])
        self.assertEqual((state / "cluster-node.key").read_bytes(), key_bytes)
        self.assertEqual((state / "cluster-node.json").read_bytes(), note_bytes)
        self.assertTrue((state / "cluster-tunnel.stop").is_file())
        self.assertTrue((state / "cluster-tunnel.json").is_file())
        self.assertEqual((state / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.lstat().st_ino, inode)
        self.assertTrue((state / "cluster-node-launch-extra.vbs").is_file())
        (state / "cluster-tunnel.release").write_text("go\n", encoding="utf-8")
        proc.wait(10)
        self.assertIsNotNone(proc.returncode)
        # The worker has exited. A second leave may now remove credentials; the lock inode stays.
        stopped = json.loads(path.read_text(encoding="utf-8"))
        with self._quiet_autostart():
            again = cluster_setup.leave(stopped, path, state)
        self.assertNotIn("tunnel_stop_error", again)
        self.assertFalse((state / "cluster-node.json").exists())
        self.assertFalse((state / "cluster-node.key").exists())
        self.assertFalse((state / "cluster-node-launch-extra.vbs").exists())
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.lstat().st_ino, inode)

    def test_join_timeout_does_not_replace_key_note_or_config(self):
        root, state = self._node_root()
        lock = state / "cluster-tunnel.lock"
        lock.write_bytes(b"\0")
        inode = lock.lstat().st_ino
        path = root / "node.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["keep"] = {"mine": True}
        path.write_text(json.dumps(config), encoding="utf-8")
        before = path.read_bytes()
        key_bytes = (state / "cluster-node.key").read_bytes()
        note_bytes = (state / "cluster-node.json").read_bytes()
        proc = self._spawn("stuck", root)
        self._running(state, proc)
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"), \
                mock.patch("jarvis.fleet.legacy_topology.socket.gethostname", return_value="WinLaptop"):
            with self.assertRaises(ValueError) as caught:
                cluster_setup.join(config, path, state, _note(), autostart=False, start=False)
        self.assertIn("still running", str(caught.exception))
        self.assertIn("did not replace", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((state / "cluster-node.key").read_bytes(), key_bytes)
        self.assertEqual((state / "cluster-node.json").read_bytes(), note_bytes)
        self.assertTrue((state / "cluster-tunnel.stop").is_file())
        self.assertTrue((state / "cluster-tunnel.json").is_file())
        self.assertEqual(lock.lstat().st_ino, inode)
        self.assertFalse((state / "cluster-node.key.writing").exists())
        (state / "cluster-tunnel.release").write_text("go\n", encoding="utf-8")
        proc.wait(10)

    def test_stale_heartbeat_does_not_count_as_stopped_while_the_lock_is_held(self):
        from jarvis import cluster_tunnel
        from jarvis.locking import LockBusy, exclusive_lock
        root, state = self._node_root()
        marker = state / "cluster-tunnel.stop"
        marker.write_text("keep-marker\n", encoding="utf-8")
        status_note = state / "cluster-tunnel.json"
        key_bytes = (state / "cluster-node.key").read_bytes()
        note_bytes = (state / "cluster-node.json").read_bytes()
        path = root / "node.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        before = path.read_bytes()
        proc = self._spawn("stale", root)
        lock = state / "cluster-tunnel.lock"
        deadline = time.monotonic() + 10
        inode = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                text = (root / "worker.err").read_text(encoding="utf-8", errors="replace")
                self.fail("stale worker exited early\n" + text[-2000:])
            if lock.is_file() and status_note.is_file():
                body = json.loads(status_note.read_text(encoding="utf-8"))
                reported = cluster_tunnel.status(state)
                busy = False
                try:
                    with exclusive_lock(lock, timeout=0):
                        busy = False
                except LockBusy:
                    busy = True
                if busy and reported.get("running") is False and time.time() - body["updated"] >= 15:
                    inode = lock.lstat().st_ino
                    break
            time.sleep(0.05)
        else:
            text = (root / "worker.err").read_text(encoding="utf-8", errors="replace")
            self.fail("stale heartbeat never hid a held lock\n" + text[-2000:])
        confirmed, detail = cluster_setup._stop_tunnel(state)
        self.assertFalse(confirmed)
        self.assertIn("still running", str(detail.get("note") or detail.get("error") or ""))
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep-marker\n")
        self.assertTrue(status_note.is_file())
        self.assertEqual(lock.lstat().st_ino, inode)
        with mock.patch.object(cluster_setup, "_hostname", return_value="WinLaptop"), \
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"), \
                mock.patch("jarvis.fleet.legacy_topology.socket.gethostname", return_value="WinLaptop"):
            with self.assertRaises(ValueError) as caught:
                cluster_setup.join(config, path, state, _note(), autostart=False, start=False)
        self.assertIn("still running", str(caught.exception))
        self.assertIn("did not replace", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((state / "cluster-node.key").read_bytes(), key_bytes)
        self.assertEqual((state / "cluster-node.json").read_bytes(), note_bytes)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep-marker\n")
        self.assertEqual(lock.lstat().st_ino, inode)
        with self._quiet_autostart():
            report = cluster_setup.leave(config, path, state)
        self.assertIn("still running", report["tunnel_stop_error"])
        self.assertEqual(report["credentials_retained"], "retained until the tunnel stops")
        self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["cluster"]["enabled"])
        self.assertEqual((state / "cluster-node.key").read_bytes(), key_bytes)
        self.assertEqual((state / "cluster-node.json").read_bytes(), note_bytes)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep-marker\n")
        self.assertTrue(status_note.is_file())
        self.assertEqual(lock.lstat().st_ino, inode)
        (state / "cluster-tunnel.release").write_text("go\n", encoding="utf-8")
        proc.wait(10)
        self.assertIsNotNone(proc.returncode)
