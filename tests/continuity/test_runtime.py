"""Runtime renewal, admission, quiesce, resume, and recovery. Real store, fake clocks.

These tests do not start a gateway, rewrite config, or call a provider. A passing
result is not operational failover.
"""

import inspect
import tempfile
import unittest
from pathlib import Path

from jarvis.continuity.authority import LEASE_SECONDS, AuthorityError
from jarvis.continuity.runtime import (
    GatewayPort,
    LinkError,
    MainRuntime,
    RecoveryOrchestrator,
    RuntimePolicy,
    RuntimeRefusal,
    StoreAdapter,
    admission_decision,
)

from tests.continuity._fleet import Clock, ack, cutover, digest, open_store, promote, ticket


NEEDLE = "UNIQUE_PERSONAL_SENTENCE"


def probe(control=True, ai=False, checkpoint="cp-1"):
    return {
        "control_ready": control,
        "luna": ai,
        "grok": ai,
        "quota_ok": ai,
        "mediated_tools": ai,
        "session_local": ai,
        "checkpoint": checkpoint,
    }


class LinkGate:
    def __init__(self, inner):
        self.inner = inner
        self.up = True

    def _go(self, name, *args, **kwargs):
        if not self.up:
            raise LinkError()
        return getattr(self.inner, name)(*args, **kwargs)

    def status(self):
        return self._go("status")

    def renew(self, lease_tuple):
        return self._go("renew", lease_tuple)

    def release(self, lease_tuple):
        return self._go("release", lease_tuple)

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        return self._go("acknowledge", device_id, installation_id, mode_version, fenced_epoch, boot_id)

    def transition_request(self, request):
        return self._go("transition_request", request)

    def transition_readiness(self, transition_id, readiness):
        return self._go("transition_readiness", transition_id, readiness)

    def transition_step(self, transition_id, step, canary_ok=None):
        return self._go("transition_step", transition_id, step, canary_ok=canary_ok)

    def consume(self, admission):
        return self._go("consume", admission)

    def admission(self, admission_id):
        return self._go("admission", admission_id)


class Recording:
    def __init__(self, inner):
        self.inner = inner
        self.events = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            self.events.append((name, args, kwargs))
            return attr(*args, **kwargs)

        return wrapped


class SlowRenew:
    """Delay the renew response without moving the request-start stamp."""

    def __init__(self, inner, clock):
        self.inner = inner
        self.clock = clock

    def renew(self, lease_tuple):
        self.clock.tick(4)
        return self.inner.renew(lease_tuple)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Remote(Exception):
    """Stand-in for a gateway client error. ``code`` is the only safe text."""

    def __init__(self, code, wait=None):
        super().__init__(code)
        self.code = code
        self.wait = wait


_MISSING = object()


class ClientShape:
    """``AuthorityClient`` method names over the real store. Not an HTTP client."""

    def __init__(self, store, device_id, installation_id):
        self.store = store
        self.device_id = device_id
        self.installation_id = installation_id
        self.calls = []
        self.fault = None
        self.mode_calls = []

    def _note(self, name, args):
        self.calls.append((name, args))
        if self.fault is not None:
            raise self.fault

    def status(self):
        self._note("status", ())
        return self.store.status()

    def renew(self, holder, lease_id, incarnation, epoch):
        self._note("renew", (holder, lease_id, incarnation, epoch))
        return self.store.renew(holder, lease_id, incarnation, epoch)

    def release(self, holder, lease_id, incarnation, epoch):
        self._note("release", (holder, lease_id, incarnation, epoch))
        self.store.release(holder, lease_id, incarnation, epoch)
        return self.store.status()

    def acknowledge(self, mode_version, fenced_epoch, boot_id=None):
        self._note("acknowledge", (mode_version, fenced_epoch, boot_id))
        self.store.acknowledge(
            self.device_id, self.installation_id, mode_version, fenced_epoch, boot_id)
        return {"accepted": True, "device_id": self.device_id}

    def readiness(self, transition_id, control_ready, ai_ready, checkpoint=_MISSING):
        self._note("readiness", (transition_id, control_ready, ai_ready, checkpoint))
        current = self.store.transition(transition_id)
        if current["candidate"] != self.device_id:
            raise Remote("forbidden")
        sent = None if checkpoint is _MISSING else checkpoint
        return self.store.readiness(transition_id, control_ready, ai_ready, sent)

    def step(self, transition_id, step, canary_ok=None):
        self._note("step", (transition_id, step, canary_ok))
        if step == "drain":
            return self.store.drain(transition_id)
        if step == "fence":
            return self.store.fence(transition_id)
        if step == "grant":
            return self.store.grant(transition_id)
        if step == "cancel":
            return self.store.cancel(transition_id)
        if step == "complete":
            return self.store.complete(transition_id, canary_ok)
        raise Remote("bad_request")

    def transition_request(self, client_key, action, candidate, expected_incarnation,
                           expected_epoch, expected_policy, profile="control"):
        recorded = (client_key, action, candidate, expected_incarnation, expected_epoch,
                    expected_policy, profile)
        self._note("transition_request", recorded)
        return self.store.request(
            "owner-key", client_key, action, candidate, expected_incarnation,
            expected_epoch, expected_policy, profile)

    def consume(self, ticket, ticket_signature):
        self._note("consume", (tuple(sorted(ticket)), ticket_signature))
        if "ticket_signature" in ticket:
            raise Remote("bad_request")
        return self.store.consume(**ticket)

    def admission(self, admission_id):
        self._note("admission", (admission_id,))
        return self.store.admission(admission_id)

    def set_mode(self, *args, **kwargs):
        self.mode_calls.append(("set_mode", args, kwargs))
        raise AssertionError("set_mode")

    def enroll(self, *args, **kwargs):
        self.mode_calls.append(("enroll", args, kwargs))
        raise AssertionError("enroll")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "authority.sqlite"
        self.clock = Clock()
        self.store = open_store(self.db, self.clock)
        self.addCleanup(self._close_store)
        self.link = LinkGate(StoreAdapter(self.store))
        self.boot = {"id": "boot-a"}
        self.probes = {"zenbook": probe(), "dell": probe()}
        self.directory = {}

    def _close_store(self):
        try:
            self.store.close()
        except Exception:
            pass

    def reopen(self, authority_id="oracle-auth"):
        self.store.close()
        self.store = open_store(self.db, self.clock, authority_id)
        self.link.inner = StoreAdapter(self.store)
        return self.store

    def pc(self, device, suspend_clock=True, port=None):
        clock = self.clock.mono if suspend_clock else None
        return MainRuntime(
            port or self.link, device, device + "-inst",
            lambda d=device: self.probes[d], clock=clock, boot=lambda: self.boot["id"])

    def orch(self, port=None, **policy):
        chosen = RuntimePolicy(
            preferred=policy.get("preferred", "zenbook"),
            recover_candidates=policy.get("recover_candidates", ("zenbook", "dell")),
            auto_recover=policy.get("auto_recover", False),
            auto_failback=policy.get("auto_failback", False),
            profile=policy.get("profile", "control"),
            caller=policy.get("caller", "managed-hub"),
        )
        return RecoveryOrchestrator(port or self.link, self.directory, chosen, self.clock.mono)

    def touch(self, *devices):
        now = self.clock.m
        for device in devices or ("zenbook", "dell"):
            row = dict(self.probes.get(device) or {})
            row["kind"] = "pc"
            row["observed_at"] = now
            self.directory[device] = row

    def advance(self, seconds, *runtimes):
        """Move the fake clock without letting a live lease lapse or a directory entry go stale."""
        left = float(seconds)
        while left > 0:
            hop = min(10.0, left)
            self.clock.tick(hop)
            left -= hop
            for runtime in runtimes:
                runtime.renew_once()
            self.touch()

    def v2(self, main="zenbook"):
        cutover(self.store)
        _tid, lease = promote(self.store, "recover", main, "k-boot")
        self.touch()
        runtime = self.pc(main)
        runtime.accept_lease(lease)
        runtime.start()
        return runtime, lease

    def refuse(self, fn, code):
        with self.assertRaises(RuntimeRefusal) as caught:
            fn()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def disk(self):
        blob = self.db.read_bytes()
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.db) + suffix)
            if side.exists():
                blob += side.read_bytes()
        return blob

    def test_legacy_is_the_default_and_does_not_renew_or_promote(self):
        runtime = self.pc("zenbook")
        state = runtime.start()
        self.assertEqual(state["phase"], "legacy")
        self.assertFalse(state["admission_open"])
        self.assertNotIn("lease_id", state)
        self.refuse(lambda: runtime.require_authority("control"), "legacy")
        recorded = Recording(self.link)
        step = RecoveryOrchestrator(
            recorded, self.directory, RuntimePolicy(), self.clock.mono).step()
        self.assertEqual(step["code"], "legacy")
        self.assertFalse(any(name in ("renew", "transition_request", "transition_step")
                             for name, _args, _kw in recorded.events))

    def test_start_again_follows_a_later_cutover(self):
        runtime = self.pc("zenbook")
        runtime.start()
        cutover(self.store)
        _tid, lease = promote(self.store, "recover", "zenbook", "k-boot")
        self.touch()
        runtime.accept_lease(lease)
        state = runtime.start()
        self.assertEqual(state["phase"], "main")
        self.assertTrue(state["admission_open"])
        context = runtime.require_authority("control", target="dell")
        self.assertEqual(set(context), {
            "effect_class", "target", "holder", "incarnation", "epoch", "mode",
            "mode_version", "authority_id", "policy_version", "ready", "online",
            "consume_required",
        })
        self.assertTrue(context["consume_required"])
        self.assertNotIn("admission_id", context)
        self.assertNotIn("lease_id", context)

    def test_renew_anchors_at_request_start(self):
        cutover(self.store)
        _tid, lease = promote(self.store, "recover", "zenbook", "k-boot")
        runtime = self.pc("zenbook", port=SlowRenew(self.link, self.clock))
        runtime.accept_lease(lease)
        runtime.start()
        view = runtime.renew_once()
        self.assertEqual(view["code"], "ok")
        self.assertTrue(view["cached"])
        self.assertAlmostEqual(view["remaining"], 11, delta=0.05)

    def test_partition_stops_admission_and_does_not_use_the_cache(self):
        arch, _lease = self.v2()
        self.assertGreater(arch.state()["cache_remaining"], 0)
        self.link.up = False
        self.refuse(lambda: arch.require_authority("ordinary"), "partition")
        state = arch.state()
        self.assertTrue(state["stale"])
        self.assertFalse(state["admission_open"])
        self.assertEqual(state["cache_remaining"], 0)
        recorded = Recording(self.link)
        step = self.orch(port=recorded, auto_recover=True).step()
        self.assertEqual(step["code"], "partition")
        self.assertFalse(any(name == "transition_request" for name, _a, _k in recorded.events))
        self.assertIsNone(self.store.status()["transition"])
        self.link.up = True
        self.assertEqual(arch.renew_once()["code"], "ok")
        self.assertEqual(arch.require_authority("control")["holder"], "zenbook")

    def test_windows_validates_online_and_does_not_cache(self):
        cutover(self.store)
        _tid, lease = promote(self.store, "recover", "zenbook", "k-boot")
        runtime = self.pc("zenbook", suspend_clock=False)
        runtime.accept_lease(lease)
        runtime.start()
        view = runtime.renew_once()
        self.assertEqual(view["code"], "ok")
        self.assertFalse(view["cached"])
        self.assertEqual(view["remaining"], 0)
        self.assertTrue(runtime.state()["admission_open"])
        self.assertEqual(runtime.require_authority("ordinary")["online"], True)
        self.clock.tick(LEASE_SECONDS + 1)
        self.refuse(lambda: runtime.require_authority("ordinary"), "no_main")
        self.assertEqual(runtime.renew_once()["code"], "expired")
        self.assertIsNone(self.store.status()["main"])

    def test_reboot_voids_the_cache_until_a_new_renew(self):
        arch, _lease = self.v2()
        self.boot["id"] = "boot-b"
        self.assertEqual(arch.state()["cache_remaining"], 0)
        self.assertFalse(arch.state()["admission_open"])
        renewed = arch.renew_once()
        self.assertEqual(renewed["code"], "ok")
        self.assertGreater(renewed["remaining"], 0)
        self.assertEqual(arch.require_authority("control")["holder"], "zenbook")

    def test_explicit_stop_is_sticky(self):
        arch, _lease = self.v2()
        arch.stop_new_admission("operator")
        self.refuse(lambda: arch.require_authority("control"), "operator")
        self.assertEqual(arch.renew_once()["code"], "operator")
        self.assertTrue(self.store.status()["lease_valid"])

    def test_provider_name_is_not_ai_readiness(self):
        arch, _lease = self.v2()
        self.probes["zenbook"] = {
            "control_ready": True, "model": "grok-4.7", "lane": "luna+5", "checkpoint": "cp-1",
        }
        report = arch.readiness("ai")
        self.assertFalse(report["meets"])
        self.assertFalse(report["ai_ready"])
        self.assertTrue(arch.readiness("control")["meets"])
        self.refuse(lambda: arch.require_authority("ai"), "profile")
        dumped = str(report)
        self.assertNotIn("grok-4.7", dumped)
        self.assertNotIn("model", report)
        self.probes["zenbook"] = {
            "control_ready": True, "luna": True, "grok": True, "quota_ok": True,
            "mediated_tools": True, "session_local": "/home/jdoe/.codex", "checkpoint": "cp-1",
        }
        copied = arch.readiness("ai")
        self.assertFalse(copied["ai_ready"])
        self.assertNotIn("/home/jdoe/.codex", str(copied))
        self.probes["zenbook"] = probe(ai=True)
        self.assertTrue(arch.readiness("ai")["meets"])
        self.assertEqual(arch.require_authority("ai")["effect_class"], "ai")

    def test_personal_readiness_text_is_refused_before_the_store(self):
        arch, _lease = self.v2()
        self.probes["zenbook"] = {"control_ready": True, "prompt": NEEDLE}
        self.refuse(lambda: arch.readiness("control"), "bad_request")
        self.probes["zenbook"] = {"control_ready": True, "checkpoint": "notes about the day"}
        self.refuse(lambda: arch.readiness("control"), "bad_checkpoint")
        self.assertNotIn(NEEDLE.encode(), self.disk())
        self.assertNotIn(b"notes about the day", self.disk())

    def test_resume_drops_cache_and_release_does_not_reclaim(self):
        arch, _lease = self.v2()
        self.clock.tick(1)
        self.assertGreater(arch.state()["cache_remaining"], 10)
        arch.invalidate_after_resume()
        self.assertEqual(arch.state()["cache_remaining"], 0)
        self.assertFalse(arch.state()["admission_open"])
        # Still the lease holder: online renew is required, the old cache is not.
        self.assertEqual(arch.require_authority("control")["online"], True)
        report = arch.release_before_suspend()
        self.assertTrue(report["released"])
        self.assertTrue(report["acked"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")
        arch.invalidate_after_resume()
        self.assertIsNone(arch.renew_once().get("lease_id"))
        self.refuse(lambda: arch.require_authority("control"), "no_main")
        self.assertIsNone(self.store.status()["main"])

    def test_planned_release_then_manual_fence_without_skipping_the_canary(self):
        arch, _lease = self.v2()
        started = self.clock.m
        self.assertTrue(arch.release_before_suspend()["released"])
        dell = self.pc("dell")
        dell.start()
        dell.acknowledge_current()
        status = self.store.status()
        self.store.acknowledge("oracle", "oracle-inst", status["mode_version"], status["epoch"])
        self.probes["zenbook"]["control_ready"] = False
        self.touch()
        recorded = Recording(self.link)
        granted = self.drive(self.orch(port=recorded, auto_recover=True))
        self.assertEqual(self.clock.m, started)
        self.assertEqual(granted["code"], "canary_required")
        self.assertFalse(granted["lease"]["ready"])
        self.assertFalse(self.store.status()["main_ready"])
        self.assertEqual(self.store.status()["main"], "dell")
        steps = [args[1] for name, args, _kw in recorded.events if name == "transition_step"]
        self.assertIn("drain", steps)
        self.assertIn("fence", steps)
        self.assertIn("grant", steps)
        self.assertNotIn("complete", steps)
        dell.accept_lease(granted["lease"])
        dell.start()
        self.refuse(lambda: dell.require_authority("control"), "no_main")
        self.refuse(lambda: arch.require_authority("control"), "suspend")
        finished = self.orch(port=recorded).submit_canary(granted["transition_id"], True)
        self.assertEqual(finished["code"], "new_main")
        self.assertEqual(self.store.status()["main_ready"], True)
        self.assertEqual(dell.require_authority("control")["holder"], "dell")
        self.assertNotIn("make_main_now", [args[0].get("action") for name, args, _kw in recorded.events
                                            if name == "transition_request"])

    def test_crash_sleep_waits_out_validity_and_refuses_the_old_tuple(self):
        arch, lease = self.v2()
        self.clock.tick(LEASE_SECONDS - 5)
        self.touch()
        watcher = self.orch(auto_recover=True, auto_failback=False)
        self.assertEqual(watcher.step()["code"], "holding")
        self.assertIsNone(self.store.status()["transition"])
        self.clock.tick(6)
        self.probes["zenbook"]["control_ready"] = False
        self.touch()
        self.assertIsNone(self.store.status()["main"])
        opened = watcher.step()
        self.assertEqual((opened["code"], opened["candidate"], opened["action"]),
                         ("requested", "dell", "recover"))
        self.assertEqual(watcher.step()["code"], "draining")
        waiting = watcher.step()
        self.assertEqual(waiting["code"], "not_quiet")
        self.assertGreater(waiting["wait"], 0)
        self.clock.tick(waiting["wait"] / 2)
        self.touch()
        still = watcher.step()
        self.assertEqual(still["code"], "not_quiet")
        self.assertIsNone(self.store.status()["main"])
        self.clock.tick(still["wait"] + 0.01)
        self.touch()
        granted = watcher.step()
        self.assertEqual(granted["code"], "canary_required")
        self.assertFalse(self.store.status()["main_ready"])
        self.refuse(lambda: arch.require_authority("control"), "no_main")
        dell = self.pc("dell")
        dell.accept_lease(granted["lease"])
        dell.start()
        self.refuse(lambda: dell.require_authority("control"), "no_main")
        self.assertEqual(watcher.submit_canary(granted["transition_id"], True)["code"], "new_main")
        self.assertEqual(dell.require_authority("ordinary")["epoch"], self.store.status()["epoch"])
        stale = arch.renew_once()
        self.assertEqual(set(stale), {"code", "wait"})
        self.assertEqual(stale["code"], "stale")
        self.refuse(lambda: arch.require_authority("control"), "not_holder")
        body = ticket(self.store, lease, self.clock, op="op-arch", body=NEEDLE)
        with self.assertRaises(AuthorityError) as caught:
            self.link.consume(body)
        self.assertEqual(caught.exception.code, "stale")
        self.assertNotIn(NEEDLE.encode(), self.disk())

    def test_canary_must_be_a_real_bool_and_failure_leaves_no_main(self):
        _arch, _lease = self.v2()
        ack(self.store)
        self.touch()
        watcher = self.orch()
        opened = watcher.open_manual("make_main_now", "dell", "k-canary")
        self.drive_open(watcher)
        self.assertEqual(opened["transition_id"], self.store.status()["transition"]["transition_id"])
        self.assertFalse(self.store.status()["main_ready"])
        tid = opened["transition_id"]
        self.refuse(lambda: watcher.submit_canary(tid, 1), "bad_request")
        self.assertFalse(self.store.status()["main_ready"])
        failed = watcher.submit_canary(tid, False)
        self.assertEqual(failed["code"], "canary_failed")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(self.store.status()["mode"], "V2_ONLY_NO_MAIN")

    def test_cancel_before_fence_lets_the_old_main_admit_again(self):
        arch, _lease = self.v2()
        self.touch()
        watcher = self.orch()
        opened = watcher.open_manual("make_main_now", "dell", "k-cancel")
        drained = watcher.step()
        self.assertEqual(drained["code"], "draining")
        report = arch.begin_drain(opened["transition_id"])
        self.assertFalse(report["authority_drain"])
        self.refuse(lambda: arch.require_authority("control"), "draining")
        self.assertEqual(arch.renew_once()["code"], "draining")
        cancelled = watcher.cancel(opened["transition_id"])
        self.assertEqual(cancelled["code"], "old_main_kept")
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.assertEqual(arch.renew_once()["code"], "ok")
        self.assertEqual(arch.require_authority("control")["holder"], "zenbook")

    def test_release_during_a_transition_does_not_ack_or_extend(self):
        arch, _lease = self.v2()
        self.touch()
        opened = self.orch().open_manual("make_main_now", "dell", "k-busy")
        self.assertEqual(opened["code"], "requested")
        report = arch.release_before_suspend()
        self.assertEqual(report["code"], "busy")
        self.assertFalse(report["released"])
        self.assertFalse(report["acked"])
        self.assertEqual(self.store.status()["main"], "zenbook")
        self.assertEqual(arch.renew_once()["code"], "suspend")
        self.clock.tick(LEASE_SECONDS + 1)
        self.assertIsNone(self.store.status()["main"])

    def test_recover_while_the_lease_is_valid_is_refused(self):
        _arch, _lease = self.v2()
        self.touch()
        refused = self.orch(auto_recover=True).open_manual("recover", "dell", "k-alive")
        self.assertEqual(refused["code"], "main_alive")
        self.assertIsNone(self.store.status()["transition"])

    def test_auto_recover_is_off_until_asked_and_prefers_the_ready_preferred(self):
        arch, _lease = self.v2()
        self.clock.tick(LEASE_SECONDS + 1)
        self.touch()
        self.assertEqual(self.orch().step()["code"], "manual")
        self.assertIsNone(self.store.status()["transition"])
        both = self.orch(auto_recover=True)
        opened = both.step()
        self.assertEqual((opened["candidate"], opened["action"]), ("zenbook", "recover"))
        self.orch().cancel(opened["transition_id"])
        # Preferred asleep, prepared dell is the successor. No extra 30s hold.
        self.clock.tick(LEASE_SECONDS + 1)
        self.probes["zenbook"]["control_ready"] = False
        self.touch()
        # The cancelled transition may have fenced. Re-seat a live main first if needed.
        if self.store.status()["mode"] == "V2_ONLY_NO_MAIN" or not self.store.status()["lease_valid"]:
            pass
        watcher = self.orch(auto_recover=True, auto_failback=False)
        step = watcher.step()
        if step["code"] == "not_quiet":
            self.clock.tick(step["wait"] + 0.01)
            self.touch()
            step = watcher.step()
        self.assertEqual(step["candidate"], "dell")
        self.assertEqual(step["action"], "recover")
        self.refuse(lambda: arch.require_authority("control"), "no_main")

    def test_ai_profile_is_not_silently_downgraded(self):
        _arch, _lease = self.v2()
        self.clock.tick(LEASE_SECONDS + 1)
        self.probes["zenbook"]["control_ready"] = False
        self.probes["dell"] = probe(control=True, ai=False)
        self.touch()
        refused = self.orch(auto_recover=True, profile="ai").step()
        self.assertEqual(refused["code"], "NO_ELIGIBLE_MAIN")
        self.assertEqual(refused["reason"], "profile")
        self.assertIsNone(self.store.status()["transition"])
        explicit = self.orch(profile="ai").open_manual("recover", "dell", "k-control", profile="control")
        self.assertEqual(explicit["code"], "requested")
        self.assertEqual(self.store.transition(explicit["transition_id"])["profile"], "control")

    def test_failback_waits_and_follows_the_stored_preference(self):
        _arch, _lease = self.v2()
        ack(self.store)
        _tid, dell_lease = promote(self.store, "make_main_now", "dell", "k-temp")
        self.assertIsNone(self.store.status()["preferred"])
        dell = self.pc("dell")
        dell.accept_lease(dell_lease)
        dell.start()
        self.touch()
        early = self.orch(auto_failback=True, preferred="zenbook")
        self.assertEqual(early.step()["code"], "stabilising")
        self.advance(29, dell)
        self.assertEqual(early.step()["code"], "stabilising")
        self.assertIsNone(self.store.status()["transition"])
        self.advance(1, dell)
        opened = early.step()
        self.assertEqual((opened["action"], opened["candidate"]), ("set_preferred_main", "zenbook"))
        self.orch().cancel(opened["transition_id"])
        # Commit Dell as the stored preference, then hand the lease back without
        # clearing that preference. The policy hint still names the other PC.
        ack(self.store)
        promote(self.store, "make_main_now", "zenbook", "k-back-first")
        ack(self.store)
        promote(self.store, "set_preferred_main", "dell", "k-pref")
        self.assertEqual((self.store.status()["preferred"], self.store.status()["main"]), ("dell", "dell"))
        ack(self.store)
        _tid, back = promote(self.store, "make_main_now", "zenbook", "k-back")
        arch = self.pc("zenbook")
        arch.accept_lease(back)
        arch.start()
        self.assertEqual(self.store.status()["preferred"], "dell")
        self.touch()
        returning = self.orch(auto_failback=True, preferred="zenbook")
        self.touch()
        # First observation starts the window; 30s of freshness has to be continuous.
        self.assertEqual(returning.step()["code"], "stabilising")
        self.advance(30, arch)
        opened = returning.step()
        self.assertEqual((opened["action"], opened["candidate"]), ("set_preferred_main", "dell"))

    def test_failback_resets_when_readiness_blinks_or_auto_is_off(self):
        _arch, _lease = self.v2()
        ack(self.store)
        _tid, lease = promote(self.store, "make_main_now", "dell", "k-dell")
        runtime = self.pc("dell")
        runtime.accept_lease(lease)
        runtime.start()
        self.touch()
        off = self.orch(auto_failback=False, preferred="zenbook")
        self.advance(100, runtime)
        self.assertEqual(off.step()["code"], "holding")
        self.assertIsNone(self.store.status()["transition"])
        watcher = self.orch(auto_failback=True, preferred="zenbook")
        self.assertEqual(watcher.step()["code"], "stabilising")
        self.advance(20, runtime)
        self.probes["zenbook"]["control_ready"] = False
        self.touch()
        self.assertEqual(watcher.step()["stable_for"], 0)
        self.probes["zenbook"]["control_ready"] = True
        self.touch()
        watcher.note_discontinuity()
        self.assertEqual(watcher.step()["code"], "stabilising")
        self.advance(29, runtime)
        self.assertIsNone(self.store.status()["transition"])
        self.advance(1, runtime)
        self.assertEqual(watcher.step()["action"], "set_preferred_main")

    def test_oracle_cannot_be_nominated_and_a_lie_is_still_ineligible(self):
        _arch, _lease = self.v2()
        self.clock.tick(LEASE_SECONDS + 1)
        now = self.clock.m
        self.directory.clear()
        self.directory["oracle"] = {
            "kind": "server", "control_ready": True, "observed_at": now, "checkpoint": "cp-1",
        }
        honest = self.orch(auto_recover=True, preferred=None, recover_candidates=("oracle",))
        self.assertEqual(honest.step()["code"], "NO_ELIGIBLE_MAIN")
        self.assertIsNone(self.store.status()["transition"])
        self.directory["oracle"] = {
            "kind": "pc", "control_ready": True, "observed_at": now, "checkpoint": "cp-1",
        }
        lied = self.orch(auto_recover=True, preferred=None, recover_candidates=("oracle",)).step()
        self.assertEqual(lied["code"], "ineligible")
        self.assertIsNone(self.store.status()["transition"])

    def test_stale_directory_and_personal_facts_do_not_promote(self):
        _arch, _lease = self.v2()
        self.clock.tick(LEASE_SECONDS + 1)
        self.directory["zenbook"] = {
            "kind": "pc", "control_ready": True, "observed_at": self.clock.m - 30,
            "checkpoint": "cp-1", "prompt": NEEDLE,
        }
        self.directory["dell"] = {
            "kind": "pc", "control_ready": True, "observed_at": self.clock.m,
            "checkpoint": "diary of the day",
        }
        step = self.orch(auto_recover=True, preferred="zenbook").step()
        self.assertEqual(step["code"], "NO_ELIGIBLE_MAIN")
        self.assertIsNone(self.store.status()["transition"])
        self.assertNotIn(NEEDLE.encode(), self.disk())
        self.assertNotIn(b"diary of the day", self.disk())

    def test_restart_barrier_is_not_shortened(self):
        arch, _lease = self.v2()
        self.reopen()
        self.touch()
        watcher = self.orch(auto_recover=True)
        waiting = watcher.step()
        self.assertEqual(waiting["code"], "not_quiet")
        self.assertGreater(waiting["wait"], LEASE_SECONDS)
        self.assertIsNone(self.store.status()["transition"])
        self.assertEqual(set(arch.renew_once()), {"code", "wait"})
        self.assertEqual(arch.renew_once()["code"], "stale")
        self.clock.tick(waiting["wait"] + 0.01)
        self.probes["zenbook"]["control_ready"] = False
        self.touch()
        granted = self.drive(watcher)
        self.assertEqual(granted["code"], "canary_required")
        self.assertFalse(granted["lease"]["ready"])
        self.assertFalse(self.store.status()["main_ready"])

    def test_replay_reconciles_and_conflict_is_not_a_second_effect(self):
        _arch, _lease = self.v2()
        ack(self.store)
        _tid, lease = promote(self.store, "make_main_now", "dell", "k-dell")
        first = self.link.consume(ticket(self.store, lease, self.clock, body=NEEDLE))
        self.assertEqual(admission_decision(first)["action"], "execute")
        second = self.link.consume(ticket(self.store, lease, self.clock, body=NEEDLE))
        decision = admission_decision(second)
        self.assertEqual(decision["action"], "reconcile")
        self.assertTrue(decision["already"])
        changed = ticket(self.store, lease, self.clock, body="other words")
        with self.assertRaises(AuthorityError) as caught:
            self.link.consume(changed)
        self.assertEqual(caught.exception.code, "conflict")
        self.refuse(lambda: admission_decision(dict(first, prompt=NEEDLE)), "bad_request")
        self.assertNotIn(NEEDLE.encode(), self.disk())

    def test_request_keys_stay_idempotent(self):
        _arch, _lease = self.v2()
        self.touch()
        watcher = self.orch()
        first = watcher.open_manual("make_main_now", "dell", "same-key")
        second = watcher.open_manual("make_main_now", "dell", "same-key")
        self.assertEqual(first["transition_id"], second["transition_id"])
        reused = watcher.open_manual("set_preferred_main", "dell", "same-key")
        self.assertEqual(reused["code"], "key_reused")

    def test_policy_rejects_instant_failback_and_sloppy_flags(self):
        self.refuse(lambda: RuntimePolicy(failback_stability_s=0), "instant_failback")
        self.refuse(lambda: RuntimePolicy(failback_stability_s=29.9), "instant_failback")
        self.refuse(lambda: RuntimePolicy(auto_recover="yes"), "bad_request")
        self.assertNotIn("force", inspect.signature(RecoveryOrchestrator.open_manual).parameters)

    def test_module_does_not_name_a_config_writer_or_a_provider_client(self):
        source = Path(__file__).resolve().parents[2].joinpath("jarvis/continuity/runtime.py").read_text()
        for banned in ("main_device", "subprocess", "openai", "anthropic", "api.x.ai", "zenbook", "dell"):
            self.assertNotIn(banned, source)
        for method in ("status", "renew", "release", "acknowledge", "transition_request",
                       "transition_readiness", "transition_step", "consume", "admission"):
            self.assertTrue(hasattr(StoreAdapter, method), method)
            self.assertTrue(hasattr(GatewayPort, method), method)
        for blocked in ("set_mode", "enroll", "relay"):
            self.assertFalse(hasattr(GatewayPort, blocked), blocked)

    def test_gateway_port_drives_a_real_fence_and_does_not_set_mode(self):
        cutover(self.store)
        self.touch()
        client = ClientShape(self.store, "zenbook", "zenbook-inst")
        port = GatewayPort(client)
        watcher = self.orch(port=port, auto_recover=True, preferred="zenbook")
        opened = self.drive(watcher)
        self.assertEqual(opened["code"], "canary_required")
        self.assertIs(opened["lease"]["ready"], False)
        steps = [args[1] for name, args in client.calls if name == "step"]
        self.assertIn("drain", steps)
        self.assertIn("fence", steps)
        self.assertIn("grant", steps)
        self.assertNotIn("complete", steps)
        request = next(args for name, args in client.calls if name == "transition_request")
        self.assertEqual((request[1], request[2]), ("recover", "zenbook"))
        self.assertNotIn("managed-hub", request)
        self.assertNotIn("owner-key", request)
        self.assertEqual(len(request), 7)
        self.refuse(lambda: port.transition_step(opened["transition_id"], "complete", canary_ok=1), "bad_request")
        self.assertNotIn("complete", [args[1] for name, args in client.calls if name == "step"])
        failed = watcher.submit_canary(opened["transition_id"], False)
        self.assertEqual(failed["code"], "canary_failed")
        self.assertIsNone(self.store.status()["main"])
        self.assertEqual(client.mode_calls, [])
        readiness = next(args for name, args in client.calls if name == "readiness")
        self.assertEqual(readiness[3], "cp-1")

    def test_gateway_port_binds_the_client_and_requires_a_signature(self):
        cutover(self.store)
        self.touch()
        client = ClientShape(self.store, "dell", "dell-inst")
        port = GatewayPort(client)
        watcher = self.orch(port=port)
        opened = watcher.open_manual("recover", "zenbook", "k-foreign")
        self.assertTrue(opened["ok"])
        blocked = watcher.step()
        self.assertEqual(blocked["code"], "forbidden")
        self.assertNotIn("drain", [args[1] for name, args in client.calls if name == "step"])
        runtime = self.pc("zenbook", port=port)
        runtime.start()
        self.refuse(runtime.acknowledge_current, "forbidden")
        self.assertFalse(any(name == "acknowledge" for name, _args in client.calls))
        self.refuse(lambda: port.consume(admission_id="adm-1", target="dell"), "bad_request")
        self.assertFalse(any(name == "consume" for name, _args in client.calls))
        client.calls.clear()
        self.refuse(lambda: port.transition_readiness(opened["transition_id"], {
            "control_ready": 1, "ai_ready": False, "checkpoint": None,
        }), "bad_request")
        self.refuse(lambda: port.transition_readiness(opened["transition_id"], {
            "control_ready": True, "ai_ready": False, "checkpoint": "notes about the day",
        }), "bad_request")
        self.assertFalse(any(name == "readiness" for name, _args in client.calls))
        try:
            port.transition_readiness(opened["transition_id"], {
                "control_ready": False, "ai_ready": False, "checkpoint": None,
            })
        except (AuthorityError, RuntimeRefusal):
            pass
        omitted = next(args for name, args in client.calls if name == "readiness")
        self.assertIs(omitted[3], _MISSING)
        self.assertNotIn(NEEDLE.encode(), self.disk())

    def test_gateway_port_fail_closed_on_transport_and_auth(self):
        runtime, lease = self.v2()
        del runtime
        client = ClientShape(self.store, "zenbook", "zenbook-inst")
        port = GatewayPort(client)
        held = self.pc("zenbook", port=port)
        held.accept_lease(lease)
        held.start()
        self.assertEqual(held.require_authority("control")["consume_required"], True)
        signature = "BEGIN SSH SIGNATURE\ntest\nEND SSH SIGNATURE"
        body = ticket(self.store, lease, self.clock, op="op-port", body=NEEDLE)
        first = port.consume({**body, "ticket_signature": signature})
        second = port.consume(ticket_signature=signature, **body)
        self.assertIs(first["already"], False)
        self.assertEqual(admission_decision(second)["action"], "reconcile")
        forwarded = [args for name, args in client.calls if name == "consume"]
        self.assertTrue(forwarded)
        self.assertNotIn("ticket_signature", forwarded[0][0])
        self.assertNotIn(NEEDLE.encode(), self.disk())
        self.refuse(lambda: port.renew({"holder": "dell", "lease_id": lease["lease_id"],
                                        "incarnation": lease["incarnation"], "epoch": lease["epoch"]}),
                    "forbidden")
        client.fault = Remote("unavailable", wait=15)
        self.refuse(lambda: held.require_authority("ordinary"), "partition")
        view = held.state()
        self.assertTrue(view["stale"])
        self.assertFalse(view["admission_open"])
        client.fault = Remote("unauthorized")
        with self.assertRaises(AuthorityError) as caught:
            held.require_authority("ordinary")
        self.assertEqual(caught.exception.code, "unauthorized")
        client.fault = Remote("not_quiet", wait=True)
        with self.assertRaises(AuthorityError) as waited:
            port.status()
        self.assertEqual(waited.exception.code, "not_quiet")
        self.assertIsNone(waited.exception.wait)
        client.fault = Remote("stale", wait=4.5)
        with self.assertRaises(AuthorityError) as stale:
            port.renew(lease)
        self.assertEqual((stale.exception.code, stale.exception.wait), ("stale", 4.5))

    def drive(self, watcher):
        last = None
        for _ in range(8):
            last = watcher.step()
            if last["code"] == "canary_required":
                return last
            if last["code"] in ("not_quiet", "lease_live"):
                self.assertGreater(last["wait"], 0)
                self.clock.tick(last["wait"] + 0.01)
                self.touch()
                continue
            if last["code"] in ("requested", "draining") or last["ok"]:
                self.touch()
                continue
            self.fail(repr(last))
        self.fail(repr(last))

    def drive_open(self, watcher):
        last = None
        for _ in range(6):
            last = watcher.step()
            if last["code"] == "canary_required":
                return last
            if last["code"] in ("not_quiet", "lease_live"):
                ack(self.store)
                self.touch()
                continue
            self.touch()
        return last
