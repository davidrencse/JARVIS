"""Regressions for the integrated fleet-gap audit (findings 1–5).

Temp directories only. Signing, sshd and host-key side effects are patched. Nothing here
writes a production config, vault, or live broker, starts a service, or calls a model.
"""

import argparse
import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli, cluster_setup, config_lock, fleet_management as fm, settings
from jarvis.cluster_store import GRACE, Broker, running_ambiguous

from tests.cluster.test_config_writers import HOST_KEY, MAIN, PRIOR, Base

ROOT = Path(__file__).resolve().parents[2]
RUN = "1" * 32
PEND = "2" * 32
DONE = "3" * 32
RUN2 = "4" * 32
T = 1_700_000_000


def _insert(broker, job_id, state, deadline, result=None, node="pixel"):
    with broker.db() as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                   (job_id, node, "owner", "status", "{}", state, 1.0, deadline, result))


def _job_state(broker, job_id):
    with broker.db() as db:
        row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    return None if row is None else row["state"]


def _extract_function(text, name):
    marker = f"function {name}("
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"{name} is not defined")
    brace = text.find("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(f"{name} is unclosed")


class SettingsWriteTests(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: settings._SAVED_CLUSTER.pop(str(self.state), None))

    def test_load_hook_set_main_is_not_written_back(self):
        """The audit hook: json.loads of the config runs set_main, then the stale
        document must not be saved over that main."""
        memory = json.loads(self.path.read_text(encoding="utf-8"))
        real = config_lock.json.loads
        fired = {"n": 0}

        def loads(text, *args, **kwargs):
            if fired["n"] == 0:
                fired["n"] = 1
                cluster_cli.set_main(memory, self.path, "dell", state=self.state)
            return real(text, *args, **kwargs)

        with mock.patch.object(config_lock.json, "loads", side_effect=loads):
            settings.write(memory, self.path, self.state, {"voice.kokoro_speed": 1.1})
        disk = self.disk()
        self.assertEqual(fired["n"], 1)
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["cluster"]["tunnel"], "forward")
        self.assertEqual(disk["voice"]["kokoro_speed"], 1.1)
        self.assertEqual(disk["keep"]["me"], [1, 2, 3])
        self.assertEqual(memory["voice"]["kokoro_speed"], 1.1)
        self.assertEqual(memory["cluster"]["main_device"], "zenbook")

    def test_stale_memory_does_not_restore_main(self):
        memory = json.loads(self.path.read_text(encoding="utf-8"))
        cluster_cli.set_main(memory, self.path, "dell", state=self.state)
        settings.write(memory, self.path, self.state, {"voice.kokoro_speed": 1.1})
        disk = self.disk()
        self.assertEqual(disk["cluster"]["main_device"], "dell")
        self.assertEqual(disk["voice"]["kokoro_speed"], 1.1)
        self.assertEqual(disk["keep"]["me"], [1, 2, 3])
        self.assertEqual(memory["cluster"]["main_device"], "zenbook")
        self.assertEqual(memory["voice"]["kokoro_speed"], 1.1)

    def test_write_is_busy_while_the_fleet_lock_is_held(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with config_lock.config_mutation(self.state):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(2))
        memory = json.loads(self.path.read_text(encoding="utf-8"))
        try:
            with mock.patch.object(config_lock, "DEFAULT_TIMEOUT", 0.2):
                with self.assertRaises(config_lock.ConfigBusy):
                    settings.write(memory, self.path, self.state, {"cluster.tunnel": "reverse"})
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(self.disk()["cluster"]["tunnel"], "forward")
        self.assertEqual(memory["cluster"]["tunnel"], "forward")

    def test_tunnel_is_saved_for_restart_and_main_is_refused(self):
        memory = json.loads(self.path.read_text(encoding="utf-8"))
        saved = settings.write(memory, self.path, self.state, {"cluster.tunnel": "reverse"})
        self.assertEqual(self.disk()["cluster"]["tunnel"], "reverse")
        self.assertEqual(memory["cluster"]["tunnel"], "forward")
        self.assertEqual(settings._SAVED_CLUSTER[str(self.state)]["tunnel"], "reverse")
        self.assertIn("cluster.tunnel", saved["restart"])
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            settings.write(memory, self.path, self.state, {"cluster.main_device": "dell"})
        self.assertIn("Moving MAIN is unavailable", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(memory["cluster"]["main_device"], "zenbook")


class InviteAdmissionTests(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: settings._SAVED_CLUSTER.pop(str(self.state), None))

    def args(self, action, **kw):
        base = dict(action=action, config=str(self.path), state=str(self.state), value=None,
                    main_device=None, local_device=None, url=None, hostname=None, capability=None,
                    output=None, device_id=None, address=[], no_autostart=True, no_start=True,
                    port=7777, rollback=False, autostart=False)
        return argparse.Namespace(**{**base, **kw})

    def test_authorize_tunnel_save_does_not_return_a_code(self):
        enrolled = []

        def authorize(node, public, port, path=None):
            memory = json.loads(json.dumps(MAIN))
            settings.write(memory, self.path, self.state, {"cluster.tunnel": "reverse"})

        def enroll(*args, **kwargs):
            enrolled.append(args)
            raise AssertionError("enroll ran")

        with self.common(), mock.patch.object(cluster_setup, "authorize", side_effect=authorize), \
                mock.patch.object(cluster_setup.Broker, "enroll", side_effect=enroll):
            with self.assertRaises(fm.FleetError) as caught:
                fm.issue_invite(self.disk(), self.path, self.state, {
                    "request_id": "req-tunnel-flip", "node": "oracle1", "profile": "cloud"})
        self.assertEqual(enrolled, [])
        payload = caught.exception.payload
        self.assertEqual(payload["code"], "invite_failed")
        self.assertTrue(payload["retryable"])
        self.assertIn("no longer forward", str(caught.exception))
        self.assertNotIn("jarvis-invite-1:", str(caught.exception))
        self.assertIsNone(cluster_setup._node_snapshot(self.state, "oracle1"))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), PRIOR + "\n")
        self.assertEqual(self.disk()["cluster"]["tunnel"], "reverse")
        self.assertEqual(self.disk()["cluster"]["main_device"], "zenbook")
        self.assertEqual(self.disk()["keep"]["me"], [1, 2, 3])

    def test_uncertain_receipt_blocks_only_that_node_id(self):
        fm._store_receipts(self.state, {
            "req-old-uncertain": {"node": "oracle1", "state": "uncertain", "at": 1.0}})

        def boom(state):
            raise AssertionError("code_key ran")

        body = {"request_id": "req-new-same-node", "node": "oracle1", "profile": "cloud",
                "replace_pending": True}
        with self.common(), mock.patch.object(cluster_setup, "code_key", side_effect=boom), \
                mock.patch.object(cluster_cli, "code_key", side_effect=boom):
            with self.assertRaises(fm.FleetError) as caught:
                fm.issue_invite(self.disk(), self.path, self.state, body)
            with self.assertRaises(fm.AdmissionBlocked):
                cluster_setup.invite(self.disk(), self.path, self.state, "oracle1")
            with self.assertRaises(fm.AdmissionBlocked):
                cluster_cli.run(self.args("enroll", value="oracle1", output=str(self.tmp / "oracle.pair.json")),
                                self.disk(), self.tmp)
        payload = caught.exception.payload
        self.assertEqual((payload["code"], payload["retryable"], payload.get("recover")),
                         ("invite_failed", False, "revoke"))
        self.assertFalse((self.tmp / "oracle.pair.json").exists())
        receipts = fm._load_receipts(self.state)
        self.assertEqual(receipts["req-old-uncertain"]["state"], "uncertain")
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))
        fm.assert_node_admitted(self.state, "pixel9")
        with self.common():
            issued = fm.issue_invite(self.disk(), self.path, self.state, {
                "request_id": "req-other-node", "node": "pixel9", "profile": "cloud"})
        self.assertTrue(str(issued.get("code", "")).startswith("jarvis-invite-1:"))
        ledgers = sorted(p.name for p in self.state.iterdir()
                         if "receipt" in p.name or "ledger" in p.name)
        self.assertEqual(ledgers, ["cluster-invite-receipts.json"])


class EnrollOrderTests(Base):
    def args(self, **kw):
        base = dict(action="enroll", config=str(self.path), state=str(self.state), value="pixel",
                    main_device=None, local_device=None, url=None, hostname=None, capability=None,
                    output=str(self.tmp / "pixel.pair.json"), device_id=None, address=[],
                    no_autostart=True, no_start=True, port=7777, rollback=False, autostart=False)
        return argparse.Namespace(**{**base, **kw})

    def prepare(self):
        self.edit_disk(lambda d: d["devices"].update(
            pixel={"hostname": "pixel-host", "label": "Pixel"}))
        old = Broker(self.state).enroll("pixel", ["desktop"])
        _insert(Broker(self.state), RUN, "running", time.time() + 60)
        _insert(Broker(self.state), DONE, "done", 0, result="{}")
        return self.disk(), old

    def test_code_key_failure_does_not_rotate(self):
        config, old = self.prepare()
        calls = []
        real = Broker.enroll

        def enroll(self, *args, **kwargs):
            calls.append(kwargs.get("settle_jobs", True))
            return real(self, *args, **kwargs)

        output = self.tmp / "pixel.pair.json"
        with mock.patch.object(cluster_cli, "code_key", side_effect=RuntimeError("ssh-keygen failed")), \
                mock.patch.object(Broker, "enroll", enroll):
            with self.assertRaises(RuntimeError) as caught:
                cluster_cli.run(self.args(), config, self.tmp)
        self.assertIn("ssh-keygen failed", str(caught.exception))
        self.assertEqual(calls, [])
        self.assertFalse(output.exists())
        self.assertEqual(_job_state(Broker(self.state), RUN), "running")
        self.assertEqual(_job_state(Broker(self.state), DONE), "done")
        self.assertEqual(Broker(self.state).authenticate(old)[0], "pixel")
        self.assertIsNotNone(cluster_setup._node_snapshot(self.state, "pixel"))

    def test_pairing_write_failure_restores_the_token_and_leaves_jobs(self):
        config, old = self.prepare()
        settled = []

        def settle(self, node):
            settled.append(node)
            raise AssertionError("settled")

        output = self.tmp / "pixel.pair.json"
        with mock.patch.object(cluster_cli, "code_key", return_value=HOST_KEY), \
                mock.patch.object(cluster_cli, "save_json", side_effect=OSError("disk full")), \
                mock.patch.object(Broker, "settle_rotated", settle):
            with self.assertRaises(OSError) as caught:
                cluster_cli.run(self.args(), config, self.tmp)
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual(settled, [])
        self.assertFalse(output.exists())
        self.assertEqual(_job_state(Broker(self.state), RUN), "running")
        self.assertEqual(_job_state(Broker(self.state), DONE), "done")
        self.assertEqual(Broker(self.state).authenticate(old)[0], "pixel")
        self.assertFalse(fm.uncertain_for(fm._load_receipts(self.state), "pixel"))

    def test_running_jobs_settle_only_after_the_pairing_file(self):
        config, old = self.prepare()
        output = self.tmp / "pixel.pair.json"
        events = []
        real_enroll = Broker.enroll
        real_settle = Broker.settle_rotated

        def code_key(state):
            events.append(("code_key", output.exists()))
            return HOST_KEY

        def enroll(self, node, capabilities=None, invite_ttl=None, settle_jobs=True):
            events.append(("enroll", settle_jobs, output.exists(), _job_state(self, RUN)))
            return real_enroll(self, node, capabilities, invite_ttl, settle_jobs=settle_jobs)

        def settle(self, node):
            events.append(("settle", output.exists(), _job_state(self, RUN), _job_state(self, DONE)))
            return real_settle(self, node)

        with mock.patch.object(cluster_cli, "code_key", side_effect=code_key), \
                mock.patch.object(Broker, "enroll", enroll), \
                mock.patch.object(Broker, "settle_rotated", settle):
            report = cluster_cli.run(self.args(), config, self.tmp)
        self.assertEqual([item[0] for item in events], ["code_key", "enroll", "settle"])
        self.assertFalse(events[0][1])
        self.assertEqual(events[1][1:], (False, False, "running"))
        self.assertEqual(events[2][1:], (True, "running", "done"))
        self.assertEqual(_job_state(Broker(self.state), RUN), "uncertain")
        self.assertEqual(_job_state(Broker(self.state), DONE), "done")
        new = json.loads(output.read_text(encoding="utf-8"))["token"]
        self.assertEqual(Broker(self.state).authenticate(new)[0], "pixel")
        self.assertIsNone(Broker(self.state).authenticate(old))
        self.assertEqual(report["pairing_file"], str(output))

    def test_receipt_written_during_code_key_does_not_enroll(self):
        """The audit injection: code_key records an uncertain receipt, then returns a key.
        Admission is checked again under the fleet lock before any credential effect."""
        config, old = self.prepare()
        enrolled = []

        def code_key(state):
            fm.note_uncertain_attempt(state, "pixel")
            return HOST_KEY

        def enroll(broker, *args, **kwargs):
            enrolled.append(args)
            raise AssertionError("enroll ran")

        output = self.tmp / "pixel.pair.json"
        with mock.patch.object(cluster_cli, "code_key", side_effect=code_key), \
                mock.patch.object(Broker, "enroll", enroll):
            with self.assertRaises(fm.AdmissionBlocked) as caught:
                cluster_cli.run(self.args(), config, self.tmp)
        self.assertEqual(caught.exception.node, "pixel")
        self.assertEqual(enrolled, [])
        self.assertFalse(output.exists())
        self.assertEqual(_job_state(Broker(self.state), RUN), "running")
        self.assertEqual(_job_state(Broker(self.state), DONE), "done")
        self.assertEqual(Broker(self.state).authenticate(old)[0], "pixel")
        self.assertTrue(fm.uncertain_for(fm._load_receipts(self.state), "pixel"))
        ledgers = sorted(p.name for p in self.state.iterdir()
                         if "receipt" in p.name or "ledger" in p.name)
        self.assertEqual(ledgers, ["cluster-invite-receipts.json"])

    def test_default_enroll_still_settles_running_jobs(self):
        broker = Broker(self.state)
        broker.enroll("pixel", ["desktop"])
        _insert(broker, RUN, "running", time.time() + 60)
        _insert(broker, DONE, "done", 0, result="{}")
        broker.enroll("pixel", ["desktop"])
        self.assertEqual(_job_state(broker, RUN), "uncertain")
        self.assertEqual(_job_state(broker, DONE), "done")


class DeadlineTests(Base):
    def test_exact_grace_boundary_matches_busy_and_is_not_replayed(self):
        self.assertTrue(running_ambiguous(T - GRACE, T))
        self.assertFalse(running_ambiguous(T - GRACE + 0.001, T))
        self.assertTrue(running_ambiguous(T - GRACE - 0.001, T))
        broker = Broker(self.tmp / "boundary")
        _insert(broker, RUN, "running", T - GRACE)
        _insert(broker, PEND, "pending", T + 60)
        _insert(broker, DONE, "done", 0, result="{}")
        with mock.patch("jarvis.cluster_store.time.time", return_value=T):
            self.assertEqual(broker.running("pixel"), 1)
            self.assertIsNone(broker.busy_job("pixel"))
            claimed = broker.claim("pixel", ["status"])
            self.assertEqual(claimed["id"], PEND)
            self.assertEqual(_job_state(broker, RUN), "uncertain")
            self.assertEqual(_job_state(broker, PEND), "running")
            self.assertEqual(_job_state(broker, DONE), "done")
            self.assertIsNone(broker.claim("pixel", ["status"]))
            self.assertEqual(_job_state(broker, RUN), "uncertain")
        late = Broker(self.tmp / "get-boundary")
        _insert(late, RUN, "running", T - GRACE)
        with mock.patch("jarvis.cluster_store.time.time", return_value=T):
            self.assertEqual(late.get(RUN)["state"], "uncertain")

    def test_inside_the_window_stays_running_and_a_second_claim_still_works(self):
        inside = Broker(self.tmp / "inside")
        _insert(inside, RUN, "running", T - GRACE + 0.001)
        with mock.patch("jarvis.cluster_store.time.time", return_value=T):
            self.assertIsNotNone(inside.busy_job("pixel"))
            self.assertEqual(inside.get(RUN)["state"], "running")
            self.assertIsNone(inside.claim("pixel", ["status"]))
            self.assertEqual(_job_state(inside, RUN), "running")
        twice = Broker(self.tmp / "twice")
        _insert(twice, RUN, "pending", T + 60)
        _insert(twice, RUN2, "pending", T + 60)
        with mock.patch("jarvis.cluster_store.time.time", return_value=T):
            first = twice.claim("pixel", ["status"])
            second = twice.claim("pixel", ["status"])
            self.assertEqual({first["id"], second["id"]}, {RUN, RUN2})
            self.assertEqual(_job_state(twice, RUN), "running")
            self.assertEqual(_job_state(twice, RUN2), "running")
            self.assertIsNotNone(twice.busy_job("pixel"))


class SurfaceWordingTests(Base):
    def test_settings_screen_and_phone_picker_say_busy_and_stay_disabled(self):
        now = time.time()
        config = json.loads(self.path.read_text(encoding="utf-8"))
        config["cluster"]["local_device"] = "zenbook"
        config["devices"]["pixel"] = {"hostname": "pixel-host", "label": "Pixel", "transport": "cluster"}
        broker = Broker(self.state)
        with broker.db() as db:
            db.execute("INSERT INTO nodes(id,token_hash,capabilities,seen,info) VALUES(?,?,?,?,?)",
                       ("pixel", "ab" * 32, '["desktop"]', now - 100, "{}"))
        _insert(broker, RUN, "running", now + 30)
        pixel = next(item for item in settings.machines(config, self.state) if item["id"] == "pixel")
        self.assertEqual((pixel["online"], pixel["presence"], pixel["paired"]), (False, "busy", True))

        app = (ROOT / "jarvis/web/app.js").read_text(encoding="utf-8")
        phone = (ROOT / "jarvis/web/phone/phone.js").read_text(encoding="utf-8")
        settings_js = (ROOT / "jarvis/web/settings.js").read_text(encoding="utf-8")
        self.assertIn('${hold.enabled ? "" : " disabled"}', app)
        self.assertIn('if ((clusterNode(device) || {}).presence === "busy") { paintScreen(); return; }', app)
        self.assertIn("disabled: choice.disabled", phone)
        for rel in ("jarvis/web/settings.js", "jarvis/web/app.js", "jarvis/web/phone/phone.js"):
            checked = subprocess.run(["node", "--check", str(ROOT / rel)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
        script = "\n".join([
            "function ago() { return '1 min ago'; }",
            "function str(v, n) { const s = v == null ? '' : String(v); return s.length > n ? s.slice(0, n) : s; }",
            _extract_function(settings_js, "machineStatus"),
            _extract_function(app, "screenHold"),
            _extract_function(phone, "askTargetChoice"),
            "const fails = [];",
            "function check(cond, msg) { if (!cond) fails.push(msg); }",
            "const live = " + json.dumps({"role": "node", "paired": True, "online": pixel["online"],
                                          "presence": pixel["presence"], "seen": pixel["seen"]}) + ";",
            "const busy = machineStatus(live);",
            "check(busy[0] === 'warn' && busy[1].indexOf('Busy, awaiting contact') === 0 && busy[1].indexOf('Offline') < 0, 'settings ' + busy.join(' | '));",
            "const off = machineStatus({role:'node', paired:true, online:false, presence:'offline', seen:1});",
            "check(off[0] === 'down' && off[1].indexOf('Offline') === 0, 'offline ' + off.join(' | '));",
            "const hold = screenHold({presence:'busy', online:false});",
            "check(hold.enabled === false && hold.text === 'Busy, awaiting contact', JSON.stringify(hold));",
            "const holdOff = screenHold({online:false});",
            "check(holdOff.enabled === false && holdOff.text === 'offline', JSON.stringify(holdOff));",
            "const holdOn = screenHold({online:true, presence:'online'});",
            "check(holdOn.enabled === true && holdOn.text === 'online', JSON.stringify(holdOn));",
            "const askB = askTargetChoice({id:'pixel', label:'Pixel', online:false, presence:'busy'});",
            "check(askB.disabled === true && askB.label === 'Pixel (busy, awaiting contact)' && askB.label.indexOf('(offline)') < 0, askB.label);",
            "const askO = askTargetChoice({id:'pixel', label:'Pixel', online:false, presence:'offline'});",
            "check(askO.disabled === true && askO.label === 'Pixel (offline)', askO.label);",
            "const askN = askTargetChoice({id:'pixel', label:'Pixel', online:true, presence:'online'});",
            "check(askN.disabled === false && askN.label === 'Pixel', JSON.stringify(askN));",
            "if (fails.length) { console.error(fails.join('\\n')); process.exit(1); }",
            "console.log('ok');",
        ])
        proc = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("ok", proc.stdout)


class ReceiptFenceTests(Base):
    """CLI revoke, the enroll re-check, and receipt writers share one locked ledger."""

    def args(self, action, **kw):
        base = dict(action=action, config=str(self.path), state=str(self.state), value=None,
                    main_device=None, local_device=None, url=None, hostname=None, capability=None,
                    output=None, device_id=None, address=[], no_autostart=True, no_start=True,
                    port=7777, rollback=False, autostart=False)
        return argparse.Namespace(**{**base, **kw})

    def plant(self, **rows):
        fm._store_receipts(self.state, {
            key: {"node": node, "state": "uncertain", "at": 1.0} for key, node in rows.items()})

    def test_receipt_during_device_delta_blocks_before_enroll(self):
        enrolled = []
        real_save = config_lock.save_delta

        def save_delta(*args, **kwargs):
            fm.note_uncertain_attempt(self.state, "pixel9")
            return real_save(*args, **kwargs)

        def enroll(broker, *args, **kwargs):
            enrolled.append(args)
            raise AssertionError("enroll ran")

        output = self.tmp / "pixel9.pair.json"
        with mock.patch.object(cluster_cli, "code_key", return_value=HOST_KEY), \
                mock.patch.object(config_lock, "save_delta", side_effect=save_delta), \
                mock.patch.object(Broker, "enroll", enroll):
            with self.assertRaises(fm.AdmissionBlocked):
                cluster_cli.run(self.args("enroll", value="pixel9", hostname="phone",
                                           output=str(output)), self.disk(), self.tmp)
        self.assertEqual(enrolled, [])
        self.assertFalse(output.exists())
        self.assertTrue(fm.uncertain_for(fm._load_receipts(self.state), "pixel9"))

    def test_cli_revoke_clears_that_fence_and_leaves_the_other(self):
        self.plant(**{"pixel-row": "pixel9", "oracle-row": "oracle1"})
        with mock.patch.object(cluster_setup, "unauthorize", return_value=True):
            report = cluster_cli.run(self.args("revoke", value="pixel9"), self.disk(), self.tmp)
        self.assertEqual((report["revoked"], report["ssh_key_removed"]), ("pixel9", True))
        receipts = fm._load_receipts(self.state)
        self.assertFalse(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))
        fm.assert_node_admitted(self.state, "pixel9")
        with self.assertRaises(fm.AdmissionBlocked):
            fm.assert_node_admitted(self.state, "oracle1")
        # The console uses the same revoke. A cli-* row for pixel9 goes; oracle1 stays.
        self.plant(**{"cli-pixel9": "pixel9", "oracle-row": "oracle1"})
        with mock.patch.object(cluster_setup, "unauthorize", return_value=True):
            console = fm.revoke_node(self.disk(), self.state, {"node": "pixel9", "confirm": "pixel9"},
                                     config_path=self.path)
        self.assertTrue(console["ok"])
        self.assertTrue(console["ssh_key_removed"])
        receipts = fm._load_receipts(self.state)
        self.assertFalse(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))
        self.assertEqual(sorted(p.name for p in self.state.iterdir()
                                if "receipt" in p.name or "ledger" in p.name),
                         ["cluster-invite-receipts.json"])

    def test_revoke_failure_does_not_clear_the_fence(self):
        self.plant(**{"pixel-row": "pixel9", "oracle-row": "oracle1"})
        removed = []

        def boom(broker, node):
            raise RuntimeError("db down")

        with mock.patch.object(Broker, "revoke", boom), \
                mock.patch.object(cluster_setup, "unauthorize",
                                  side_effect=lambda node: removed.append(node)):
            with self.assertRaises(RuntimeError) as caught:
                cluster_cli.run(self.args("revoke", value="pixel9"), self.disk(), self.tmp)
        self.assertIn("db down", str(caught.exception))
        self.assertEqual(removed, [])
        receipts = fm._load_receipts(self.state)
        self.assertTrue(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))
        with self.assertRaises(fm.AdmissionBlocked):
            fm.assert_node_admitted(self.state, "pixel9")
        with mock.patch.object(Broker, "revoke", boom), \
                mock.patch.object(cluster_setup, "unauthorize",
                                  side_effect=lambda node: removed.append(node)):
            with self.assertRaises(RuntimeError):
                fm.revoke_node(self.disk(), self.state, {"node": "pixel9", "confirm": "pixel9"},
                               config_path=self.path)
        self.assertEqual(removed, [])
        receipts = fm._load_receipts(self.state)
        self.assertTrue(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))

    def test_concurrent_writers_under_a_barrier_keep_every_fence(self):
        self.plant(**{"dell-row": "dell"})
        inside = threading.Barrier(2)
        at_gate = threading.Barrier(2)
        release = threading.Event()
        done_b = threading.Event()
        errors = []
        real_load = fm._load_receipts
        paused = {"done": False}

        def load(state):
            data = real_load(state)
            if not paused["done"]:
                paused["done"] = True
                inside.wait(5)
                if not release.wait(5):
                    raise AssertionError("receipt writer was not released")
            return data

        def add(node, mark=None):
            try:
                if mark is not None:
                    at_gate.wait(5)
                fm.note_uncertain_attempt(self.state, node)
                if mark is not None:
                    mark.set()
            except Exception as exc:  # noqa: BLE001 - the join asserts this list
                errors.append(exc)

        with mock.patch.object(fm, "_load_receipts", side_effect=load):
            first = threading.Thread(target=add, args=("pixel9",))
            second = threading.Thread(target=add, args=("oracle1", done_b))
            first.start()
            inside.wait(5)
            second.start()
            at_gate.wait(5)
            try:
                self.assertFalse(done_b.wait(0.25))
                midway = real_load(self.state)
                self.assertTrue(fm.uncertain_for(midway, "dell"))
                self.assertFalse(fm.uncertain_for(midway, "pixel9"))
                self.assertFalse(fm.uncertain_for(midway, "oracle1"))
            finally:
                release.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        receipts = fm._load_receipts(self.state)
        self.assertTrue(fm.uncertain_for(receipts, "dell"))
        self.assertTrue(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))

    def test_reentrant_writer_does_not_store_a_stale_snapshot(self):
        self.plant(**{"dell-row": "dell"})
        real_sha = fm.hashlib.sha256
        gate = {"open": True}

        def sha256(data=b"", **kwargs):
            raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
            if gate["open"] and b"pixel9" in raw:
                gate["open"] = False
                fm.note_uncertain_attempt(self.state, "oracle1")
            return real_sha(data, **kwargs)

        with mock.patch.object(fm.hashlib, "sha256", side_effect=sha256):
            fm.note_uncertain_attempt(self.state, "pixel9")
        self.assertFalse(gate["open"])
        receipts = fm._load_receipts(self.state)
        self.assertTrue(fm.uncertain_for(receipts, "dell"))
        self.assertTrue(fm.uncertain_for(receipts, "pixel9"))
        self.assertTrue(fm.uncertain_for(receipts, "oracle1"))
        self.assertEqual(sorted(p.name for p in self.state.iterdir()
                                if "receipt" in p.name or "ledger" in p.name),
                         ["cluster-invite-receipts.json"])


if __name__ == "__main__":
    unittest.main()
