"""Gateway process drill. Real CLI subprocess, loopback ports, real SSH signatures.

Run: env $(env | grep -o '^CODEX_[A-Z_]*' | sed 's/^/-u /') JARVIS_NO_MODELS=1 \
  .venv/bin/python -m unittest tests.continuity.test_drill_gateway
"""

import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from jarvis.continuity.gateway import (
    AuthorityClient,
    GatewayError,
    canonical,
)
import jarvis.continuity.gateway as gateway_mod

from tests.continuity.test_drill_process import (
    PC,
    GatewayProc,
    cli_payload,
    digest_bytes,
    make_keys,
    port_open,
    post_bytes,
    run_cli,
    write_config,
)

FLEET = "drill-fleet"
AUTH = "drill-auth"
AUTH2 = "drill-auth-2"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789+/"


def _flip_signature(raw):
    """Change one payload character. Leave the SSH armor so verification runs."""
    body = json.loads(raw)
    signature = body["device"]["signature"]
    begin = signature.find("BEGIN SSH SIGNATURE")
    end = signature.find("END SSH SIGNATURE")
    header_end = signature.find("\n", begin)
    if begin < 0 or end <= begin or header_end < 0 or header_end >= end:
        raise AssertionError("signature armor missing")
    chars = list(signature)
    flipped = False
    for index in range(header_end + 1, end):
        char = chars[index]
        if char in _ALPHABET:
            chars[index] = "B" if char != "B" else "C"
            flipped = True
            break
    if not flipped:
        raise AssertionError("signature had no flippable character")
    mutated = "".join(chars)
    if "BEGIN SSH SIGNATURE" not in mutated or "END SSH SIGNATURE" not in mutated:
        raise AssertionError("flip broke the armor")
    if mutated == signature:
        raise AssertionError("flip did not change the signature")
    body["device"]["signature"] = mutated
    return canonical(body)


class RecordingClient(AuthorityClient):
    """Records the exact signed bytes. Optionally posts one flipped copy first."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.flip_once = False
        self.flipped = None

    def _send(self, path, obj):
        raw = canonical(obj)
        if self.flip_once and isinstance(obj, dict) and "device" in obj:
            self.flip_once = False
            self.flipped = post_bytes(self.origin, path, _flip_signature(raw))
        self.sent.append((path, raw))
        return super()._send(path, obj)


def _soon(fn, seconds=1.0):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn()
        except GatewayError as exc:
            if exc.code != "unavailable" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _ro(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        row = db.execute("SELECT * FROM authority").fetchone()
        counts = {}
        for table in ("admissions", "participants", "transitions", "gateway_directory", "gateway_nonces"):
            counts[table] = db.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"]
        return dict(row), counts
    finally:
        db.close()


def _error_body(raw):
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return body.get("error") if isinstance(body, dict) else None


class GatewayDrill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-drill-gw-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = make_keys(self.root / "keys", ("owner", "node-a"))
        self.owner_pub = self.keys["owner"][0]
        self.procs = []
        self.addCleanup(self._stop)

    def _stop(self):
        for proc in reversed(self.procs):
            proc.stop()

    def _track(self, proc):
        self.procs.append(proc)
        return proc

    def _config(self, name, *, state, authority=AUTH, bind_port=0, tls=None):
        return write_config(
            self.root, state=state, fleet=FLEET, authority=authority, owner_public=self.owner_pub,
            bind_port=bind_port, tls=tls, name=name)

    def _device(self, origin, *, authority=AUTH, recording=False):
        cls = RecordingClient if recording else AuthorityClient
        return cls(
            origin, authority_id=authority, device_id="node-a", installation_id="node-a-inst",
            private_key_path=self.keys["node-a"][1], owner_private_key_path=self.keys["owner"][1])

    def _enroll(self, client):
        return _soon(lambda: client.enroll({
            "device_id": "node-a", "installation_id": "node-a-inst", "kind": "pc",
            "public_key": self.keys["node-a"][0], "capabilities": list(PC),
        }))

    def _assert_readiness(self, info, proc, authority=AUTH):
        self.assertEqual(set(info), {"authority_id", "origin", "pid"})
        self.assertEqual(info["pid"], proc.proc.pid)
        self.assertNotEqual(info["pid"], os.getpid())
        self.assertEqual(info["authority_id"], authority)
        self.assertTrue(info["origin"].startswith("http://127.0.0.1:"), info["origin"])
        self.assertNotIn(str(self.root), info["origin"])
        _host, port = info["origin"].rsplit(":", 1)
        self.assertTrue(port_open(int(port)))
        return int(port)

    def test_process_starts_shuts_down_and_reopens_state(self):
        state = self.root / "authority.sqlite"
        cfg = self._config("gateway.json", state=state)
        gateway = self._track(GatewayProc(cfg))
        info = gateway.serve()
        port = self._assert_readiness(info, gateway)
        device = self._device(info["origin"])
        with self.assertRaises(GatewayError) as raised:
            _soon(device.status)
        self.assertEqual(raised.exception.code, "unauthorized")
        self._enroll(device)
        status = device.status()
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        self.assertIsNone(status["main"])
        self.assertFalse(status["lease_valid"])
        self.assertEqual(status["authority_id"], AUTH)
        code = gateway.stop()
        self.assertEqual(code, 0)
        deadline = time.monotonic() + 2
        while port_open(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(port_open(port))
        again = self._track(GatewayProc(cfg))
        restarted = again.serve()
        self._assert_readiness(restarted, again)
        self.assertNotEqual(restarted["origin"], info["origin"])
        lived = self._device(restarted["origin"]).status()
        self.assertEqual(lived["mode"], "LEGACY_ACTIVE")
        self.assertGreater(lived["incarnation"], status["incarnation"])
        self.assertEqual(again.stop(), 0)

    def test_second_process_is_writer_busy_while_the_first_answers(self):
        state = self.root / "authority.sqlite"
        cfg = self._config("gateway.json", state=state)
        gateway = self._track(GatewayProc(cfg))
        info = gateway.serve()
        device = self._device(info["origin"])
        self._enroll(device)
        before = device.status()
        code, out, err = run_cli(["serve", "--config", str(cfg)], timeout=10)
        self.assertEqual(code, 2)
        self.assertNotIn(b"origin", out)
        self.assertEqual(cli_payload(err), {"error": "writer_busy"})
        after = device.status()
        self.assertEqual(after["incarnation"], before["incarnation"])
        self.assertEqual(after["authority_id"], AUTH)
        self.assertIsNone(gateway.proc.poll())

    def test_enrollment_survives_restart_and_old_nonce_is_refused(self):
        state = self.root / "authority.sqlite"
        cfg = self._config("gateway.json", state=state)
        gateway = self._track(GatewayProc(cfg))
        info = gateway.serve()
        device = self._device(info["origin"], recording=True)
        self._enroll(device)
        first = device.status()
        self.assertEqual(first["mode"], "LEGACY_ACTIVE")
        status_posts = [item for item in device.sent if item[0] == "/v1/authority/status"]
        self.assertEqual(len(status_posts), 1)
        path, raw = status_posts[0]
        replay_status, _headers, replay_body = post_bytes(info["origin"], path, raw)
        self.assertNotEqual(replay_status, 200)
        self.assertEqual(_error_body(replay_body), "unauthorized")
        device.flip_once = True
        second = device.status()
        self.assertEqual(second["incarnation"], first["incarnation"])
        self.assertIsNotNone(device.flipped)
        self.assertNotEqual(device.flipped[0], 200)
        self.assertEqual(_error_body(device.flipped[2]), "unauthorized")
        self.assertEqual(gateway.stop(), 0)
        again = self._track(GatewayProc(cfg))
        restarted = again.serve()
        fresh = self._device(restarted["origin"])
        lived = fresh.status()
        self.assertGreater(lived["incarnation"], first["incarnation"])
        self.assertEqual(lived["mode"], "LEGACY_ACTIVE")
        self.assertEqual(lived["authority_id"], AUTH)
        stale_status, _headers, stale_body = post_bytes(restarted["origin"], path, raw)
        self.assertNotEqual(stale_status, 200)
        self.assertEqual(_error_body(stale_body), "unauthorized")

    def test_backup_refuses_a_live_writer_and_restore_mints_a_new_pin(self):
        state = self.root / "authority.sqlite"
        cfg = self._config("gateway.json", state=state)
        gateway = self._track(GatewayProc(cfg))
        info = gateway.serve()
        device = self._device(info["origin"])
        self._enroll(device)
        status = device.status()
        version = device.set_mode("PREPARING_V2", status["mode_version"])["mode_version"]
        version = device.set_mode("QUIESCED", version)["mode_version"]
        device.acknowledge(version + 1, 0)
        device.set_mode("V2_ONLY_NO_MAIN", version)
        current = device.status()
        transition = device.transition_request(
            "k-backup", "recover", "node-a", current["incarnation"], current["epoch"], current["policy_version"])
        tid = transition["transition_id"]
        device.readiness(tid, True, False, "cp-1")
        device.step(tid, "drain")
        device.step(tid, "fence")
        lease = device.step(tid, "grant")
        device.step(tid, "complete", True)
        device.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        now = time.time()
        ticket = {
            "admission_id": "adm-backup-1", "target": "node-a", "operation_id": "op-backup-1",
            "attempt_id": "try-1", "digest": digest_bytes(b"drill-metadata"), "main": "node-a",
            "incarnation": lease["incarnation"], "epoch": lease["epoch"],
            "issued_at": now - 1, "admit_until": now + 30, "issuer_incarnation": lease["incarnation"],
        }
        consumed = device.consume(ticket, device.sign_ticket(ticket))
        self.assertFalse(consumed["already"])
        pinned = device.status()
        live_out = self.root / "live.sqlite"
        code, out, err = run_cli(["backup", "--config", str(cfg), "--output", str(live_out)], timeout=10)
        self.assertEqual(code, 2)
        self.assertEqual(cli_payload(err), {"error": "writer_busy"})
        self.assertFalse(live_out.exists())
        self.assertNotIn(b"origin", out)
        self.assertEqual(device.status()["incarnation"], pinned["incarnation"])
        self.assertEqual(gateway.stop(), 0)
        row, counts = _ro(state)
        self.assertEqual(row["incarnation"], pinned["incarnation"])
        self.assertEqual(counts["admissions"], 1)
        backup = self.root / "backup.sqlite"
        code, out, err = run_cli(["backup", "--config", str(cfg), "--output", str(backup)])
        self.assertEqual(code, 0, err)
        saved = cli_payload(out)
        self.assertEqual(saved["authority_id"], AUTH)
        self.assertTrue(backup.is_file())
        copied, copied_counts = _ro(backup)
        self.assertEqual(copied["incarnation"], pinned["incarnation"])
        self.assertEqual(copied_counts["admissions"], 1)
        blob = backup.read_bytes()
        self.assertNotIn(b"OPENSSH PRIVATE KEY", blob)
        self.assertIn(self.keys["node-a"][0].encode("ascii"), blob)
        same = self.root / "same.sqlite"
        same_cfg = self._config("same.json", state=same, authority=AUTH)
        code, _out, err = run_cli(
            ["restore", "--config", str(same_cfg), "--input", str(backup), "--new-authority-id", AUTH])
        self.assertEqual(code, 2)
        self.assertEqual(cli_payload(err), {"error": "conflict"})
        self.assertFalse(same.exists())
        existing = self.root / "existing.sqlite"
        existing.write_bytes(b"keep-me")
        existing_cfg = self._config("existing.json", state=existing, authority=AUTH)
        before = existing.read_bytes()
        code, _out, err = run_cli(
            ["restore", "--config", str(existing_cfg), "--input", str(backup), "--new-authority-id", AUTH2])
        self.assertEqual(code, 2)
        self.assertEqual(cli_payload(err), {"error": "conflict"})
        self.assertEqual(existing.read_bytes(), before)
        sibling_state = self.root / "sibling.sqlite"
        (self.root / "sibling.sqlite.writer").write_bytes(b"lock")
        sibling_cfg = self._config("sibling.json", state=sibling_state, authority=AUTH)
        code, _out, err = run_cli(
            ["restore", "--config", str(sibling_cfg), "--input", str(backup), "--new-authority-id", AUTH2])
        self.assertEqual(code, 2)
        self.assertEqual(cli_payload(err), {"error": "conflict"})
        self.assertFalse(sibling_state.exists())
        dest = self.root / "restored.sqlite"
        restore_cfg = self._config("restore.json", state=dest, authority=AUTH)
        config_bytes = restore_cfg.read_bytes()
        code, out, err = run_cli(
            ["restore", "--config", str(restore_cfg), "--input", str(backup), "--new-authority-id", AUTH2])
        self.assertEqual(code, 0, err)
        self.assertEqual(restore_cfg.read_bytes(), config_bytes)
        restored = cli_payload(out)
        self.assertEqual(restored["authority_id"], AUTH2)
        self.assertEqual(restored["source_authority_id"], AUTH)
        self.assertTrue(restored["serve_requires_new_config"])
        row, counts = _ro(dest)
        self.assertEqual(row["authority_id"], AUTH2)
        self.assertEqual(row["mode"], "LEGACY_ACTIVE")
        self.assertEqual(row["incarnation"], 1)
        self.assertEqual(row["epoch"], 0)
        self.assertIsNone(row["holder"])
        for table in ("admissions", "participants", "transitions", "gateway_directory", "gateway_nonces"):
            self.assertEqual(counts[table], 0, table)
        quarantine = self.root / "quarantine.sqlite"
        shutil.copy(dest, quarantine)
        old_cfg = self._config("old.json", state=quarantine, authority=AUTH)
        old = self._track(GatewayProc(old_cfg))
        old_info = old.serve()
        old_client = self._device(old_info["origin"], authority=AUTH)
        with self.assertRaises(GatewayError) as raised:
            old_client.enroll({
                "device_id": "node-a", "installation_id": "node-a-inst", "kind": "pc",
                "public_key": self.keys["node-a"][0], "capabilities": list(PC),
            })
        self.assertEqual(raised.exception.code, "quarantined")
        self.assertEqual(old.stop(), 0)
        new_cfg = self._config("new.json", state=dest, authority=AUTH2)
        served = self._track(GatewayProc(new_cfg))
        new_info = served.serve()
        self._assert_readiness(new_info, served, AUTH2)
        retired = self._device(new_info["origin"], authority=AUTH)
        with self.assertRaises(GatewayError) as raised:
            retired.status()
        self.assertEqual(raised.exception.code, "unauthorized")
        current_client = self._device(new_info["origin"], authority=AUTH2)
        with self.assertRaises(GatewayError) as raised:
            current_client.status()
        self.assertEqual(raised.exception.code, "unauthorized")
        self._enroll(current_client)
        lived = current_client.status()
        self.assertEqual(lived["authority_id"], AUTH2)
        self.assertEqual(lived["mode"], "LEGACY_ACTIVE")
        self.assertIsNone(lived["main"])
        self.assertFalse(lived["lease_valid"])
        with self.assertRaises(GatewayError) as raised:
            retired.status()
        self.assertEqual(raised.exception.code, "authority_mismatch")
        with self.assertRaises(GatewayError) as raised:
            current_client.admission("adm-backup-1")
        self.assertEqual(raised.exception.code, "not_found")

    def test_tls_serves_a_verified_certificate(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is not available")
        cert, key = _cert(self.root, "good")
        state = self.root / "authority.sqlite"
        cfg = self._config("tls.json", state=state, tls=(cert, key))
        gateway = self._track(GatewayProc(cfg))
        info = gateway.serve()
        self.assertTrue(info["origin"].startswith("https://127.0.0.1:"), info["origin"])
        self.assertEqual(info["pid"], gateway.proc.pid)
        _host, port = info["origin"].rsplit(":", 1)
        verify = _openssl_verify(int(port), cert)
        self.assertIn("Verify return code: 0", verify)
        original = gateway_mod._opener
        gateway_mod._opener = lambda: _tls_opener(cert)
        try:
            device = self._device(info["origin"])
            self._enroll(device)
            status = device.status()
        finally:
            gateway_mod._opener = original
        self.assertEqual(status["mode"], "LEGACY_ACTIVE")
        self.assertEqual(status["authority_id"], AUTH)

    def test_tls_errors_bind_no_listener(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is not available")
        cert, key = _cert(self.root, "one")
        _other_cert, other_key = _cert(self.root, "two")
        garbage_cert = self.root / "garbage-cert.pem"
        garbage_key = self.root / "garbage-key.pem"
        garbage_cert.write_text("not a certificate\n", encoding="ascii")
        garbage_key.write_text("not a key\n", encoding="ascii")
        cases = (
            ("missing-key", {"tls_cert": str(cert)}, "bad_config"),
            ("mismatch", {"tls_cert": str(cert), "tls_key": str(other_key)}, "bad_config"),
            ("garbage", {"tls_cert": str(garbage_cert), "tls_key": str(garbage_key)}, "bad_config"),
        )
        for name, extra, code in cases:
            with self.subTest(name=name):
                port = _free_port()
                state = self.root / ("%s.sqlite" % name)
                body = {
                    "state_path": str(state), "fleet_id": FLEET, "authority_id": AUTH,
                    "owner_public_keys": [self.owner_pub], "bind_host": "127.0.0.1",
                    "bind_port": port, "lease_seconds": 6, "margin": 0.5,
                }
                body.update(extra)
                path = self.root / ("%s.json" % name)
                path.write_text(json.dumps(body), encoding="utf-8")
                status, out, err = run_cli(["serve", "--config", str(path)], timeout=10)
                self.assertEqual(status, 2)
                self.assertEqual(cli_payload(err), {"error": code})
                self.assertNotIn(b"origin", out)
                self.assertFalse(port_open(port))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _cert(folder, name):
    cert = Path(folder) / ("%s-cert.pem" % name)
    key = Path(folder) / ("%s-key.pem" % name)
    out = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=DNS:127.0.0.1,IP:127.0.0.1",
         "-keyout", str(key), "-out", str(cert)],
        check=False, capture_output=True, timeout=20,
    )
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode("utf-8", "replace")[:400])
    return cert, key


def _openssl_verify(port, cert):
    command = [
        "openssl", "s_client", "-connect", "127.0.0.1:%d" % port, "-servername", "127.0.0.1",
        "-CAfile", str(cert), "-verify_hostname", "127.0.0.1",
    ]
    out = subprocess.run(command, input=b"", capture_output=True, timeout=15)
    text = out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")
    if "Verify return code: 0" in text:
        return text
    fallback = [
        "openssl", "s_client", "-connect", "127.0.0.1:%d" % port, "-servername", "127.0.0.1",
        "-CAfile", str(cert),
    ]
    out = subprocess.run(fallback, input=b"", capture_output=True, timeout=15)
    return out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")


def _tls_opener(cert):
    context = ssl.create_default_context(cafile=str(cert))
    https = urllib.request.HTTPSHandler(context=context)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        https,
        gateway_mod._no_redirect(urllib.request.HTTPRedirectHandler),
    )


if __name__ == "__main__":
    unittest.main()
