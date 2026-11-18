"""Durable participant revocation on the authority store.

WS5 enroll calls register_participant(device_id, installation_id, kind, True) and does
not pass reenable. That replay must leave a revoked installation disabled.
"""

from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import unittest

from jarvis.continuity import authority as A

from tests.continuity._fleet import ack, promote, ticket, transition
from tests.continuity.test_authority import Base


_META_KEYS = {"device_id", "installation_id", "kind", "eligible", "mode_version",
              "fenced_epoch", "disabled", "already"}


class Membership(Base):
    def _part(self, device):
        row = self.store._db.execute(
            "SELECT device_id, installation_id, kind, eligible, mode_version, fenced_epoch,"
            " boot_id, acked_at FROM participants WHERE device_id=?", (device,)).fetchone()
        return None if row is None else dict(row)

    def _counts(self):
        db = self.store._db
        return tuple(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                     for name in ("participants", "transitions", "admissions"))

    def _on_disk(self, device):
        con = sqlite3.connect(self.db)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT installation_id, kind, eligible, mode_version, fenced_epoch"
                " FROM participants WHERE device_id=?", (device,)).fetchone()
            return None if row is None else dict(row)
        finally:
            con.close()

    def test_disable_persists_across_sqlite_restart(self):
        self.store.register_participant("dell", "dell-inst", "pc", True)
        self.store.acknowledge("dell", "dell-inst", 1, 0)
        first = self.store.disable_participant("dell", "dell-inst")
        self.assertEqual(set(first), _META_KEYS)
        self.assertFalse(first["already"])
        self.assertFalse(first["eligible"])
        self.assertTrue(first["disabled"])
        self.assertEqual((first["device_id"], first["installation_id"], first["kind"]),
                         ("dell", "dell-inst", "pc"))
        self.assertEqual((first["mode_version"], first["fenced_epoch"]), (1, 0))
        again = self.store.disable_participant("dell", "dell-inst")
        self.assertEqual(again, dict(first, already=True))

        self.restart()
        disk = self._on_disk("dell")
        self.assertEqual(disk["eligible"], 0)
        self.assertEqual((disk["installation_id"], disk["mode_version"], disk["fenced_epoch"]),
                         ("dell-inst", 1, 0))
        replay = self.store.disable_participant("dell", "dell-inst")
        self.assertTrue(replay["already"])
        self.assertEqual((replay["mode_version"], replay["fenced_epoch"], replay["eligible"]),
                         (1, 0, False))
        self.assertEqual(self._counts()[0], 1)

    def test_active_holder_is_refused_until_the_lease_row_is_clear(self):
        lease = self.active()
        held = self._part("zenbook")
        self.refuse("active_holder", self.store.disable_participant, "zenbook", "zenbook-inst")
        self.assertEqual(self._part("zenbook"), held)
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.assertEqual(self.store.status()["lease_holder"], "zenbook")

        self.clock.tick(A.LEASE_SECONDS + 1)
        self.assertIsNone(self.store.status()["main"])
        self.refuse("active_holder", self.store.disable_participant, "zenbook", "zenbook-inst")
        self.assertEqual(self._part("zenbook"), held)
        self.assertEqual(self.store.status()["lease_holder"], lease["holder"])

    def test_open_transition_candidate_is_refused(self):
        self.active()
        tid = transition(self.store, "make_main_now", "dell", "k-open")
        before = self._part("dell")
        source = self._part("zenbook")
        self.refuse("transition_member", self.store.disable_participant, "dell", "dell-inst")
        self.assertEqual(self._part("dell"), before)
        self.assertEqual(self._part("dell")["eligible"], 1)
        self.refuse("active_holder", self.store.disable_participant, "zenbook", "zenbook-inst")
        self.assertEqual(self._part("zenbook"), source)
        still = self.store.transition(tid)
        self.assertEqual((still["state"], still["candidate"], still["source"]),
                         ("REQUESTED", "dell", "zenbook"))
        self.assertEqual(self.store.status()["main"], "zenbook")

    def test_installation_mismatch_writes_nothing(self):
        self.store.register_participant("dell", "dell-inst", "pc")
        self.store.acknowledge("dell", "dell-inst", 1, 0)
        before = self._part("dell")
        counts = self._counts()
        self.refuse("installation_mismatch", self.store.disable_participant, "dell", "other-inst")
        self.refuse("unknown_participant", self.store.disable_participant, "ghost", "ghost-inst")
        self.assertEqual(self._part("dell"), before)
        self.assertIsNone(self._part("ghost"))
        self.assertEqual(self._counts(), counts)
        self.assertEqual(self._on_disk("dell")["eligible"], 1)

    def test_revoked_installation_cannot_ack_or_be_admitted(self):
        lease = self.active()
        admitted = self.store.consume(**ticket(self.store, lease, self.clock, target="dell"))
        self.assertFalse(admitted["already"])
        counts = self._counts()
        status = self.store.status()
        fence = self._part("dell")
        meta = self.store.disable_participant("dell", "dell-inst")
        self.assertEqual((meta["mode_version"], meta["fenced_epoch"]),
                         (fence["mode_version"], fence["fenced_epoch"]))
        self.refuse("revoked", self.store.acknowledge, "dell", "dell-inst",
                    status["mode_version"], fence["fenced_epoch"])
        self.refuse("unknown_participant", self.store.acknowledge, "dell", "other-inst",
                    status["mode_version"], 0)
        self.assertEqual(self._part("dell")["fenced_epoch"], fence["fenced_epoch"])
        self.assertEqual(self._part("dell")["mode_version"], fence["mode_version"])
        blocked = ticket(self.store, lease, self.clock, op="op-2", target="dell")
        self.refuse("revoked", self.store.consume, **blocked)
        self.assertIsNone(self.store.admission(blocked["admission_id"]))
        self.assertEqual(self.store.admission(admitted["admission_id"])["target"], "dell")
        self.assertEqual(self._counts(), counts)
        after = self.store.status()
        self.assertEqual((after["main"], after["epoch"], after["incarnation"], after["mode"]),
                         (status["main"], status["epoch"], status["incarnation"], status["mode"]))
        self.assertTrue(after["lease_valid"])
        self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])

    def test_disabled_device_is_not_an_ack_quorum_member(self):
        self.store.register_participant("zenbook", "zenbook-inst", "pc")
        self.store.register_participant("dell", "dell-inst", "pc")
        version = self.store.set_mode("PREPARING_V2", 1)
        version = self.store.set_mode("QUIESCED", version)
        self.store.acknowledge("zenbook", "zenbook-inst", version + 1, 0)
        self.store.disable_participant("dell", "dell-inst")
        self.store.set_mode("V2_ONLY_NO_MAIN", version)

    def test_enroll_replay_does_not_reenable(self):
        self.store.register_participant("dell", "dell-inst", "pc", True)
        self.store.acknowledge("dell", "dell-inst", 1, 0)
        disabled = self.store.disable_participant("dell", "dell-inst")
        # Gateway enroll's exact call. Replay must not clear the revocation.
        self.store.register_participant("dell", "dell-inst", "pc", True)
        self.store.register_participant("dell", "dell-inst", "server", False)
        row = self._part("dell")
        self.assertEqual(row["eligible"], 0)
        self.assertEqual(row["kind"], "server")
        self.assertEqual(row["installation_id"], "dell-inst")
        self.assertEqual((row["mode_version"], row["fenced_epoch"]),
                         (disabled["mode_version"], disabled["fenced_epoch"]))
        self.refuse("bad_request", self.store.register_participant, "dell", "dell-inst", "pc",
                    False, reenable=True)
        self.refuse("bad_request", self.store.register_participant, "dell", "dell-inst", "pc",
                    True, reenable="yes")
        self.assertEqual(self._part("dell")["eligible"], 0)

        self.store.register_participant("dell", "dell-inst", "pc", True, reenable=True)
        restored = self._part("dell")
        self.assertEqual(restored["eligible"], 1)
        self.assertEqual(restored["installation_id"], "dell-inst")
        self.assertEqual((restored["mode_version"], restored["fenced_epoch"]),
                         (disabled["mode_version"], disabled["fenced_epoch"]))
        self.store.acknowledge("dell", "dell-inst", 1, 0)

        self.store.disable_participant("dell", "dell-inst")
        self.store.register_participant("dell", "dell-v2", "pc", True)
        enrolled = self._part("dell")
        self.assertEqual(enrolled["installation_id"], "dell-v2")
        self.assertEqual(enrolled["eligible"], 1)
        self.assertEqual((enrolled["mode_version"], enrolled["fenced_epoch"]), (0, -1))
        self.refuse("unknown_participant", self.store.acknowledge, "dell", "dell-inst", 1, 0)

    def test_live_holder_replay_does_not_swap_installation(self):
        self.active()
        before = self._part("zenbook")
        self.store.register_participant("zenbook", "zenbook-inst", "pc", True)
        self.assertEqual(self._part("zenbook"), before)
        self.refuse("active_holder", self.store.register_participant, "zenbook", "zenbook-inst",
                    "pc", True, reenable=True)
        self.refuse("active_holder", self.store.register_participant, "zenbook", "zenbook-new",
                    "pc", True)
        self.assertEqual(self._part("zenbook"), before)
        self.assertEqual(self.store.status()["main"], "zenbook")

    def test_former_main_cannot_be_used_after_revoke(self):
        self.active()
        ack(self.store)
        _, lease = promote(self.store, "make_main_now", "dell", "k-next")
        before = self._part("zenbook")
        transitions, admissions = self._counts()[1:]
        meta = self.store.disable_participant("zenbook", "zenbook-inst")
        self.assertFalse(meta["already"])
        self.assertEqual((meta["mode_version"], meta["fenced_epoch"]),
                         (before["mode_version"], before["fenced_epoch"]))
        self.assertEqual(self.store.status()["main"], "dell")
        self.assertEqual(self._counts()[1:], (transitions, admissions))
        self.refuse("ineligible", transition, self.store, "make_main_now", "zenbook", "k-back")
        old = ticket(self.store, lease, self.clock, op="op-old-main", target="oracle")
        old["main"] = "zenbook"
        self.refuse("revoked", self.store.consume, **old)
        self.assertIsNone(self.store.admission(old["admission_id"]))
        self.store.renew(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertEqual(self._part("zenbook")["installation_id"], "zenbook-inst")
        self.assertEqual(self._part("zenbook")["eligible"], 0)

    def test_second_process_cannot_disable(self):
        self.store.register_participant("dell", "dell-inst", "pc")
        before = self._part("dell")
        script = (
            "import json, sys\n"
            "from jarvis.continuity.authority import AuthorityError, AuthorityStore\n"
            "try:\n"
            "    store = AuthorityStore(sys.argv[1], 'home', 'oracle-auth')\n"
            "except AuthorityError as exc:\n"
            "    print(json.dumps({'code': exc.code}))\n"
            "else:\n"
            "    try:\n"
            "        store.disable_participant('dell', 'dell-inst')\n"
            "        print(json.dumps({'code': None}))\n"
            "    finally:\n"
            "        store.close()\n"
        )
        env = dict(os.environ)
        env["JARVIS_NO_MODELS"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", script, str(self.db)],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=30, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"code": "writer_busy"})
        self.assertEqual(self._part("dell"), before)
        meta = self.store.disable_participant("dell", "dell-inst")
        self.assertFalse(meta["already"])
        self.assertFalse(meta["eligible"])

    def test_same_install_kind_change_refuses_live_members(self):
        lease = self.active()
        holder = self._part("zenbook")
        self.store.register_participant("zenbook", "zenbook-inst", "pc", True)
        self.assertEqual(self._part("zenbook"), holder)
        self.refuse("active_holder", self.store.register_participant, "zenbook", "zenbook-inst", "server")
        self.assertEqual(self._part("zenbook"), holder)

        tid = transition(self.store, "make_main_now", "dell", "k-kind")
        candidate = self._part("dell")
        self.store.register_participant("dell", "dell-inst", "pc", False)
        self.assertEqual(self._part("dell"), candidate)
        self.refuse("transition_member", self.store.register_participant, "dell", "dell-inst", "server")
        self.assertEqual(self._part("dell"), candidate)
        self.refuse("active_holder", self.store.register_participant, "zenbook", "zenbook-inst", "server")
        self.assertEqual(self._part("zenbook"), holder)

        self.store.readiness(tid, True, True, "cp-1")
        self.store.drain(tid)
        self.store.release(lease["holder"], lease["lease_id"], lease["incarnation"], lease["epoch"])
        self.assertIsNone(self.store.status()["main"])
        self.assertIsNone(self.store.status()["lease_holder"])
        source = self._part("zenbook")
        self.store.register_participant("zenbook", "zenbook-inst", "pc", True)
        self.assertEqual(self._part("zenbook"), source)
        self.refuse("transition_member", self.store.register_participant, "zenbook", "zenbook-inst", "server")
        self.assertEqual(self._part("zenbook"), source)
        self.refuse("transition_member", self.store.register_participant, "dell", "dell-inst", "server")
        self.assertEqual(self.store.transition(tid)["state"], "DRAINING")
        self.assertEqual(self._part("dell")["kind"], "pc")

    def test_replay_conflicts_when_main_incarnation_or_epoch_changes(self):
        lease = self.active()
        original = ticket(self.store, lease, self.clock, op="op-replay", target="oracle")
        first = self.store.consume(**original)
        self.assertFalse(first["already"])
        self.refuse("conflict", self.store.consume, **dict(original, main="dell"))
        self.refuse("conflict", self.store.consume, **dict(original, incarnation=lease["incarnation"] + 1))
        self.refuse("conflict", self.store.consume, **dict(original, epoch=lease["epoch"] + 1))
        again = self.store.consume(**original)
        self.assertTrue(again["already"])
        self.assertEqual(again["consumed_at"], first["consumed_at"])
        self.assertEqual((again["main"], again["incarnation"], again["epoch"]),
                         (lease["holder"], lease["incarnation"], lease["epoch"]))

        ack(self.store)
        _, moved = promote(self.store, "make_main_now", "dell", "k-replay")
        replay = self.store.consume(**original)
        self.assertTrue(replay["already"])
        self.assertEqual((replay["main"], replay["incarnation"], replay["epoch"], replay["consumed_at"]),
                         (lease["holder"], lease["incarnation"], lease["epoch"], first["consumed_at"]))
        shifted = dict(original, main=moved["holder"], incarnation=moved["incarnation"], epoch=moved["epoch"])
        self.refuse("conflict", self.store.consume, **shifted)
        historical = self.store.admission(original["admission_id"])
        self.assertEqual((historical["main"], historical["epoch"]), (lease["holder"], lease["epoch"]))
        self.assertNotIn("already", historical)
        fresh = ticket(self.store, moved, self.clock, op="op-fresh", target="oracle")
        self.refuse("stale", self.store.consume, **dict(
            fresh, main=lease["holder"], incarnation=lease["incarnation"], epoch=lease["epoch"]))

    def test_exact_replay_refuses_revoked_main_or_target(self):
        lease = self.active()
        named = ticket(self.store, lease, self.clock, op="op-named-main", target="oracle")
        first = self.store.consume(**named)
        ack(self.store)
        _, moved = promote(self.store, "make_main_now", "dell", "k-revoke-main")
        replay = self.store.consume(**named)
        self.assertTrue(replay["already"])
        self.store.disable_participant("zenbook", "zenbook-inst")
        self.refuse("revoked", self.store.consume, **named)
        saved = self.store.admission(named["admission_id"])
        self.assertEqual((saved["main"], saved["target"], saved["epoch"], saved["consumed_at"]),
                         ("zenbook", "oracle", lease["epoch"], first["consumed_at"]))
        self.assertNotIn("already", saved)

        current = ticket(self.store, moved, self.clock, op="op-named-target", target="oracle")
        self.store.consume(**current)
        self.store.disable_participant("oracle", "oracle-inst")
        self.refuse("revoked", self.store.consume, **current)
        kept = self.store.admission(current["admission_id"])
        self.assertEqual(kept["target"], "oracle")
        self.assertNotIn("already", kept)


if __name__ == "__main__":
    unittest.main()
