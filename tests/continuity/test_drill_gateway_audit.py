"""Luna audit repairs for enrollment atomicity, restore publication, TLS bind, and shutdown.

Run:
  JARVIS_NO_MODELS=1 .venv/bin/python -m unittest tests.continuity.test_drill_gateway_audit
"""

import json
import os
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from jarvis.continuity.authority import AuthorityStore
from jarvis.continuity.gateway import (
    AuthorityClient,
    ContinuityGateway,
    GatewayError,
    backup_gateway,
    load_gateway_config,
    restore_gateway,
    serve_gateway,
)


PC = ["status", "lease", "fence", "candidate", "consume", "reconcile"]
CLIENT = ["status", "client"]
_KEYS = None


def _keys():
    global _KEYS
    if _KEYS is not None:
        return _KEYS
    folder = Path(tempfile.mkdtemp(prefix="jarvis-audit-keys-"))
    made = {}
    for name in ("owner", "pc", "pc2", "phone"):
        path = folder / name
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jarvis-audit", "-f", str(path)],
            check=True, capture_output=True, timeout=10,
        )
        token = (folder / ("%s.pub" % name)).read_text(encoding="ascii").split()
        made[name] = ("%s %s" % (token[0], token[1]), path)
    _KEYS = made
    return made


def _config(folder, state, authority_id, **extra):
    body = {
        "state_path": os.fspath(state),
        "fleet_id": "home",
        "authority_id": authority_id,
        "owner_public_keys": [_keys()["owner"][0]],
    }
    body.update(extra)
    path = Path(folder) / ("gw-%s.json" % authority_id.replace(":", "-"))
    # Two configs in one folder must not share a name.
    if path.exists():
        path = Path(folder) / ("gw-%s-%s.json" % (path.stem, extra.get("bind_port", "b")))
    path.write_text(json.dumps(body), encoding="utf-8")
    return load_gateway_config(os.fspath(path))


def _part(store, device_id):
    row = store._db.execute("SELECT * FROM participants WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _directory(store, device_id):
    row = store._db.execute(
        "SELECT device_id, installation_id, kind, public_key FROM gateway_directory WHERE device_id=?",
        (device_id,)).fetchone()
    if row is None:
        return None
    return tuple(row)


def _break_directory(store):
    store._db.executescript(
        "DROP TRIGGER IF EXISTS audit_fail_dir_ins;"
        "DROP TRIGGER IF EXISTS audit_fail_dir_upd;"
        "CREATE TRIGGER audit_fail_dir_ins BEFORE INSERT ON gateway_directory "
        "BEGIN SELECT RAISE(ABORT, 'audit'); END;"
        "CREATE TRIGGER audit_fail_dir_upd BEFORE UPDATE ON gateway_directory "
        "BEGIN SELECT RAISE(ABORT, 'audit'); END;"
    )


def _count(store, table):
    return store._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


class _GatewayTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def _open(self, name="authority.sqlite"):
        state = self.folder / name
        gw = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        self.addCleanup(gw.close)
        return state, gw

    def _serve(self, gw):
        origin = gw.serve()
        self.assertTrue(origin.startswith("http://127.0.0.1:"))
        return origin

    def _owner(self, origin):
        return AuthorityClient(
            origin, authority_id="oracle-auth", wall=time.time,
            owner_private_key_path=_keys()["owner"][1])

    def _enroll(self, owner, device, kind, caps, installation=None, public=None):
        return owner.enroll({
            "device_id": device,
            "installation_id": installation or (device + "-inst"),
            "kind": kind,
            "public_key": public or _keys()[device][0],
            "capabilities": list(caps),
        })


class EnrollmentAtomicity(_GatewayTemp):
    def test_directory_failure_publishes_neither_membership_nor_directory(self):
        state, gw = self._open()
        origin = self._serve(gw)
        _break_directory(gw.store)
        owner = self._owner(origin)
        with self.assertRaises(GatewayError) as caught:
            self._enroll(owner, "pc", "pc", PC)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIsNone(_part(gw.store, "pc"))
        self.assertIsNone(_directory(gw.store, "pc"))
        self.assertNotIn("pc", gw._devices)
        # The failed transaction must not stick the connection.
        self.assertEqual(gw.store.status()["mode"], "LEGACY_ACTIVE")
        gw.close()
        restarted = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        self.addCleanup(restarted.close)
        self.assertIsNone(_part(restarted.store, "pc"))
        self.assertIsNone(_directory(restarted.store, "pc"))
        self.assertEqual(restarted._devices, {})

    def test_client_directory_failure_does_not_create_a_participant(self):
        state, gw = self._open()
        origin = self._serve(gw)
        _break_directory(gw.store)
        with self.assertRaises(GatewayError) as caught:
            self._enroll(self._owner(origin), "phone", "client", CLIENT)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIsNone(_part(gw.store, "phone"))
        self.assertEqual(_count(gw.store, "participants"), 0)
        self.assertIsNone(_directory(gw.store, "phone"))
        gw.close()
        restarted = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        self.addCleanup(restarted.close)
        self.assertEqual(restarted._devices, {})
        self.assertEqual(_count(restarted.store, "participants"), 0)

    def test_reinstall_and_key_rotation_roll_back_together(self):
        state, gw = self._open()
        origin = self._serve(gw)
        owner = self._owner(origin)
        self._enroll(owner, "pc", "pc", PC)
        gw.store.acknowledge("pc", "pc-inst", 1, 0, "boot-a")
        before_part = _part(gw.store, "pc")
        before_dir = _directory(gw.store, "pc")
        self.assertEqual(before_part["mode_version"], 1)
        self.assertEqual(before_part["fenced_epoch"], 0)
        self.assertEqual(before_part["boot_id"], "boot-a")
        _break_directory(gw.store)
        with self.assertRaises(GatewayError) as caught:
            self._enroll(
                owner, "pc", "pc", PC, installation="pc-reinstall", public=_keys()["pc2"][0])
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(_part(gw.store, "pc"), before_part)
        self.assertEqual(_directory(gw.store, "pc"), before_dir)
        self.assertEqual(gw._devices["pc"].public_key, _keys()["pc"][0])
        self.assertEqual(gw._devices["pc"].installation_id, "pc-inst")
        old = AuthorityClient(
            origin, authority_id="oracle-auth", device_id="pc", installation_id="pc-inst",
            private_key_path=_keys()["pc"][1])
        self.assertEqual(old.status()["authority_id"], "oracle-auth")
        gw.close()
        restarted = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        self.addCleanup(restarted.close)
        self.assertEqual(_part(restarted.store, "pc"), before_part)
        self.assertEqual(_directory(restarted.store, "pc"), before_dir)
        self.assertEqual(restarted._devices["pc"].public_key, _keys()["pc"][0])
        again = restarted.serve()
        still = AuthorityClient(
            again, authority_id="oracle-auth", device_id="pc", installation_id="pc-inst",
            private_key_path=_keys()["pc"][1])
        self.assertEqual(still.status()["mode"], "LEGACY_ACTIVE")
        rotated = AuthorityClient(
            again, authority_id="oracle-auth", device_id="pc", installation_id="pc-reinstall",
            private_key_path=_keys()["pc2"][1])
        with self.assertRaises(GatewayError) as caught:
            rotated.status()
        self.assertEqual(caught.exception.code, "unauthorized")

    def test_same_installation_keeps_acks_and_a_new_installation_resets_them(self):
        _state, gw = self._open()
        origin = self._serve(gw)
        owner = self._owner(origin)
        self._enroll(owner, "pc", "pc", PC)
        gw.store.acknowledge("pc", "pc-inst", 1, 0, "boot-a")
        self._enroll(owner, "pc", "pc", PC, public=_keys()["pc2"][0])
        kept = _part(gw.store, "pc")
        self.assertEqual(kept["installation_id"], "pc-inst")
        self.assertEqual(kept["mode_version"], 1)
        self.assertEqual(kept["fenced_epoch"], 0)
        self.assertEqual(kept["boot_id"], "boot-a")
        self.assertEqual(kept["kind"], "pc")
        self.assertEqual(kept["eligible"], 1)
        self.assertEqual(_directory(gw.store, "pc")[3], _keys()["pc2"][0])
        rotated = AuthorityClient(
            origin, authority_id="oracle-auth", device_id="pc", installation_id="pc-inst",
            private_key_path=_keys()["pc2"][1])
        self.assertEqual(rotated.status()["mode"], "LEGACY_ACTIVE")
        self._enroll(owner, "pc", "pc", PC, installation="pc-inst-2", public=_keys()["pc"][0])
        reset = _part(gw.store, "pc")
        self.assertEqual(reset["installation_id"], "pc-inst-2")
        self.assertEqual(reset["mode_version"], 0)
        self.assertEqual(reset["fenced_epoch"], -1)
        self.assertIsNone(reset["boot_id"])
        self.assertIsNone(reset["acked_at"])
        self.assertEqual(_directory(gw.store, "pc")[1], "pc-inst-2")


class RestorePublication(_GatewayTemp):
    def _snapshot(self):
        state = self.folder / "source.sqlite"
        gw = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        try:
            origin = gw.serve()
            owner = self._owner(origin)
            self._enroll(owner, "pc", "pc", PC)
            self._enroll(owner, "phone", "client", CLIENT)
            gw.store.acknowledge("pc", "pc-inst", 1, 0, "boot-a")
            self.assertEqual(_count(gw.store, "participants"), 1)
            self.assertEqual(_count(gw.store, "gateway_directory"), 2)
        finally:
            gw.close()
        snap = self.folder / "snap.sqlite"
        backup_gateway(_config(self.folder, state, "oracle-auth"), os.fspath(snap))
        db = sqlite3.connect(os.fspath(snap))
        try:
            db.execute(
                "UPDATE authority SET mode='PREPARING_V2', holder='pc', lease_id='ls-1',"
                " lease_ready=1, transition_id='tr-1', max_validity=12 WHERE fleet_id='home'")
            digest = "sha256:" + ("ab" * 32)
            db.execute(
                "INSERT INTO admissions(admission_id, target, operation_id, attempt_id, digest, main,"
                " incarnation, epoch, issued_at, admit_until, issuer_incarnation, consumed_at)"
                " VALUES('adm-1','pc','op-1','at-1',?,'pc',1,0,1,2,1,1)", (digest,))
            db.execute(
                "INSERT INTO transitions(transition_id, caller, client_key, request_digest, action,"
                " profile, source, candidate, expected_incarnation, expected_epoch, expected_policy,"
                " state, created_at, updated_at)"
                " VALUES('tr-1','owner','k1',?,'recover','control',NULL,'pc',1,0,1,'REQUESTED',1,1)",
                (digest,))
            db.commit()
        finally:
            db.close()
        return state, snap

    def _restore(self, snap, dest_name="restored.sqlite"):
        dest = self.folder / dest_name
        result = restore_gateway(
            _config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
        return dest, result

    def test_restore_wipes_membership_grants_and_keeps_a_fresh_identity(self):
        source, snap = self._snapshot()
        dest, result = self._restore(snap)
        self.assertEqual(result["authority_id"], "oracle-auth-2")
        self.assertEqual(result["source_authority_id"], "oracle-auth")
        self.assertEqual(result["mode"], "LEGACY_ACTIVE")
        self.assertEqual(result["participants"], 0)
        self.assertEqual(result["admissions"], 0)
        self.assertEqual(result["transitions"], 0)
        self.assertEqual(result["directory"], 0)
        self.assertTrue(result["serve_requires_new_config"])
        self.assertTrue(dest.is_file())
        self.assertFalse(dest.is_symlink())
        for suffix in ("-wal", "-shm", "-journal", ".writer"):
            self.assertFalse(os.path.lexists(os.fspath(dest) + suffix), suffix)
        self.assertFalse(any("partial" in path.name for path in dest.parent.iterdir()))
        db = sqlite3.connect("file:%s?mode=ro" % dest.resolve().as_uri().removeprefix("file:"), uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM authority").fetchone()
            self.assertEqual(row["authority_id"], "oracle-auth-2")
            self.assertEqual(row["fleet_id"], "home")
            self.assertEqual(row["mode"], "LEGACY_ACTIVE")
            self.assertEqual(row["incarnation"], 1)
            self.assertEqual(row["epoch"], 0)
            self.assertEqual(row["mode_version"], 1)
            self.assertIsNone(row["holder"])
            self.assertIsNone(row["lease_id"])
            self.assertEqual(row["lease_ready"], 0)
            self.assertIsNone(row["transition_id"])
            self.assertEqual(row["max_validity"], 0)
            for table in ("participants", "admissions", "transitions", "gateway_directory", "gateway_nonces"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0, table)
        finally:
            db.close()
        store = AuthorityStore(dest, "home", "oracle-auth-2")
        try:
            status = store.status()
        finally:
            store.close()
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        self.assertEqual(status["authority_id"], "oracle-auth-2")
        self.assertIsNone(status["main"])
        untouched = sqlite3.connect("file:%s?mode=ro" % source.resolve().as_uri().removeprefix("file:"), uri=True)
        try:
            self.assertEqual(
                untouched.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        finally:
            untouched.close()

    def test_preexisting_writer_sidecar_and_symlink_are_kept(self):
        _source, snap = self._snapshot()
        cases = {
            "writer": ".writer",
            "wal": "-wal",
            "symlink": None,
        }
        for label, suffix in cases.items():
            dest = self.folder / ("dest-%s.sqlite" % label)
            if suffix is None:
                other = self.folder / ("other-%s" % label)
                other.write_bytes(b"KEEP-SYMLINK")
                dest.symlink_to(other)
            else:
                Path(os.fspath(dest) + suffix).write_bytes(b"KEEP-" + label.encode())
            with self.assertRaises(GatewayError) as caught:
                restore_gateway(_config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
            self.assertEqual(caught.exception.code, "conflict", label)
            if suffix is None:
                self.assertTrue(dest.is_symlink())
                self.assertEqual(other.read_bytes(), b"KEEP-SYMLINK")
            else:
                self.assertFalse(dest.exists())
                self.assertEqual(Path(os.fspath(dest) + suffix).read_bytes(), b"KEEP-" + label.encode())

    def test_sidecar_raced_into_place_is_not_published_or_unlinked(self):
        _source, snap = self._snapshot()
        dest = self.folder / "raced-wal.sqlite"
        wal = Path(os.fspath(dest) + "-wal")
        planted = []
        real_connect = sqlite3.connect

        def connect(target, *args, **kwargs):
            db = real_connect(target, *args, **kwargs)
            if target == ":memory:" and not planted:
                planted.append(target)
                fd = os.open(os.fspath(wal), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, b"FOREIGN-WAL")
                os.close(fd)
            return db

        with mock.patch("jarvis.continuity.gateway.sqlite3.connect", connect):
            with self.assertRaises(GatewayError) as caught:
                restore_gateway(_config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(planted, [":memory:"])
        self.assertEqual(wal.read_bytes(), b"FOREIGN-WAL")
        self.assertFalse(dest.exists())
        self.assertFalse(any("partial" in path.name for path in dest.parent.iterdir()))

    def test_foreign_writer_is_not_adopted_or_unlinked(self):
        _source, snap = self._snapshot()
        dest = self.folder / "raced-writer.sqlite"
        writer = Path(os.fspath(dest) + ".writer")
        flocked = []
        real_connect = sqlite3.connect

        def connect(target, *args, **kwargs):
            db = real_connect(target, *args, **kwargs)
            if target == ":memory:" and not flocked:
                os.unlink(writer)
                fd = os.open(os.fspath(writer), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                try:
                    os.write(fd, b"FOREIGN-WRITER")
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    flocked.append(True)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            return db

        with mock.patch("jarvis.continuity.gateway.sqlite3.connect", connect):
            with self.assertRaises(GatewayError) as caught:
                restore_gateway(_config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(flocked, [True])
        self.assertEqual(writer.read_bytes(), b"FOREIGN-WRITER")
        self.assertFalse(dest.exists())
        self.assertFalse(stat.S_ISLNK(os.lstat(writer).st_mode))

    def test_destination_race_does_not_unlink_the_other_file(self):
        _source, snap = self._snapshot()
        dest = self.folder / "raced-dest.sqlite"
        real_link = os.link
        seen = {}

        def link(src, dst, *args, **kwargs):
            seen["dir_fd"] = kwargs.get("dst_dir_fd")
            seen["follow"] = kwargs.get("follow_symlinks")
            seen["dst"] = dst
            fd = os.open(
                dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                dir_fd=kwargs["dst_dir_fd"])
            os.write(fd, b"FOREIGN-DEST")
            os.close(fd)
            return real_link(src, dst, *args, **kwargs)

        with mock.patch("jarvis.continuity.gateway.os.link", link):
            with self.assertRaises(GatewayError) as caught:
                restore_gateway(_config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIsInstance(seen["dir_fd"], int)
        self.assertIs(seen["follow"], False)
        self.assertNotIn("/", seen["dst"])
        self.assertEqual(dest.read_bytes(), b"FOREIGN-DEST")

    def test_parent_symlink_replacement_does_not_redirect_the_write(self):
        _source, snap = self._snapshot()
        real = self.folder / "real"
        other = self.folder / "other"
        real.mkdir()
        other.mkdir()
        dest = real / "auth.sqlite"
        swapped = []
        real_connect = sqlite3.connect

        def connect(target, *args, **kwargs):
            db = real_connect(target, *args, **kwargs)
            if target == ":memory:" and not swapped:
                swapped.append(True)
                os.rename(real, self.folder / "moved")
                os.symlink(other, real)
            return db

        try:
            with mock.patch("jarvis.continuity.gateway.sqlite3.connect", connect):
                with self.assertRaises(GatewayError) as caught:
                    restore_gateway(
                        _config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
            self.assertEqual(caught.exception.code, "conflict")
            self.assertEqual(swapped, [True])
            self.assertEqual(list(other.iterdir()), [])
            moved = self.folder / "moved"
            self.assertFalse((moved / "auth.sqlite").exists())
            self.assertFalse(any(path.name.endswith(".writer") or "partial" in path.name
                                 for path in moved.iterdir()))
            self.assertFalse((real / "auth.sqlite").exists())
        finally:
            if real.is_symlink():
                real.unlink()
            moved = self.folder / "moved"
            if moved.exists() and not real.exists():
                moved.rename(real)

    def test_symlink_parent_fails_closed_before_writing_the_target(self):
        _source, snap = self._snapshot()
        target = self.folder / "target"
        target.mkdir()
        link = self.folder / "via-link"
        link.symlink_to(target)
        dest = link / "auth.sqlite"
        with self.assertRaises(GatewayError) as caught:
            restore_gateway(_config(self.folder, dest, "oracle-auth"), os.fspath(snap), "oracle-auth-2")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(list(target.iterdir()), [])


class BindTlsAndShutdown(_GatewayTemp):
    def _free_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _assert_port_free(self, port):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()

    def test_nonloopback_config_is_refused_without_a_state_file(self):
        state = self.folder / "never.sqlite"
        for host in ("192.0.2.10", "0.0.0.0", "example.com"):
            body = {
                "state_path": os.fspath(state),
                "fleet_id": "home",
                "authority_id": "oracle-auth",
                "owner_public_keys": [_keys()["owner"][0]],
                "bind_host": host,
            }
            path = self.folder / ("bind-%s.json" % host.replace(".", "-").replace(":", ""))
            path.write_text(json.dumps(body), encoding="utf-8")
            with self.assertRaises(GatewayError) as caught:
                load_gateway_config(os.fspath(path))
            self.assertEqual(caught.exception.code, "refused_bind", host)
        self.assertFalse(state.exists())
        self.assertFalse(os.path.lexists(os.fspath(state) + ".writer"))
        loaded = _config(self.folder, state, "oracle-auth")
        self.assertEqual(loaded.bind_host, "127.0.0.1")
        self.assertIsNone(loaded.tls_cert)
        self.assertFalse(state.exists())

    def test_tls_failure_binds_no_listener_and_releases_the_writer(self):
        state = self.folder / "tls.sqlite"
        cert = self.folder / "bad.pem"
        key = self.folder / "bad.key"
        cert.write_bytes(b"not a certificate")
        key.write_bytes(b"not a key")
        port = self._free_port()
        cfg = _config(
            self.folder, state, "oracle-auth", bind_port=port,
            tls_cert=os.fspath(cert), tls_key=os.fspath(key))
        with self.assertRaises(GatewayError) as caught:
            serve_gateway(cfg)
        self.assertEqual(caught.exception.code, "bad_config")
        self._assert_port_free(port)
        reopened = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        reopened.close()
        direct = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        try:
            with self.assertRaises(GatewayError) as caught:
                direct.serve("127.0.0.1", port, tls_cert=cert, tls_key=key)
            self.assertEqual(caught.exception.code, "bad_config")
            self.assertIsNone(direct._httpd)
            self.assertIsNone(direct._thread)
            self.assertIsNone(direct.listening)
            self._assert_port_free(port)
            self.assertEqual(direct.store.status()["mode"], "LEGACY_ACTIVE")
        finally:
            direct.close()
        plain = _config(self.folder, self.folder / "plain.sqlite", "oracle-auth")
        gw, origin = serve_gateway(plain)
        try:
            self.assertTrue(origin.startswith("http://"))
            self.assertFalse(gw._tls)
        finally:
            gw.close()

    def test_shutdown_stops_the_server_thread_while_a_body_is_incomplete(self):
        _state, gw = self._open()
        origin = self._serve(gw)
        port = urlsplit(origin).port
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /v1/authority/participants HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: 80\r\nConnection: close\r\n\r\n")
        time.sleep(0.2)
        thread = gw._thread
        self.assertTrue(thread.is_alive())
        started = time.monotonic()
        gw.shutdown()
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(gw.listening)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_sigterm_during_incomplete_post_exits_and_the_authority_reopens(self):
        state = self.folder / "served.sqlite"
        cfg_path = self.folder / "serve.json"
        cfg_path.write_text(json.dumps({
            "state_path": os.fspath(state),
            "fleet_id": "home",
            "authority_id": "oracle-auth",
            "owner_public_keys": [_keys()["owner"][0]],
            "bind_host": "127.0.0.1",
            "bind_port": 0,
        }), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "jarvis.continuity.gateway", "serve", "--config", os.fspath(cfg_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=os.fspath(Path(__file__).resolve().parents[2]),
        )
        self.addCleanup(lambda: _reap(proc))
        line = _read_line(proc, 8)
        ready = json.loads(line.decode())
        port = urlsplit(ready["origin"]).port
        self.assertEqual(ready["authority_id"], "oracle-auth")
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            sock.sendall(
                b"POST /v1/authority/participants HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                b"Content-Length: 80\r\nConnection: close\r\n\r\n")
            time.sleep(0.2)
            proc.send_signal(signal.SIGTERM)
            try:
                code = proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
                err = proc.stderr.read().decode(errors="replace")
                self.fail("serve did not exit after SIGTERM\n" + err)
        finally:
            sock.close()
        err = proc.stderr.read().decode(errors="replace")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        reopened = ContinuityGateway.open(state, "home", "oracle-auth", [_keys()["owner"][0]])
        try:
            self.assertEqual(reopened.store.status()["authority_id"], "oracle-auth")
        finally:
            reopened.close()


def _read_line(proc, timeout):
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        if proc.poll() is not None and not buf:
            err = proc.stderr.read().decode(errors="replace")
            raise AssertionError("serve exited early: %s %s" % (proc.returncode, err))
        remaining = max(0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], min(0.2, remaining))
        if not ready:
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf:
            line, _rest = buf.split(b"\n", 1)
            return line
    proc.kill()
    raise AssertionError("no readiness line: %r" % buf)


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
