"""Shared fake clock and fleet setup for the continuity tests."""

import hashlib
import sys
import types

from jarvis.continuity.authority import AuthorityStore
from jarvis.continuity.runtime import MainRuntime, SafeHandoff

DEVICES = (("zenbook", "pc"), ("dell", "pc"), ("oracle", "server"))


class Clock:
    def __init__(self):
        self.m = 1000.0
        self.w = 1_800_000_000.0

    def mono(self):
        return self.m

    def wall(self):
        return self.w

    def tick(self, seconds):
        self.m += seconds
        self.w += seconds


def open_store(path, clock, authority_id="oracle-auth"):
    return AuthorityStore(path, "home", authority_id, monotonic=clock.mono, wall=clock.wall)


def ack(store, devices=None, epoch=None):
    st = store.status()
    for device, _ in DEVICES:
        if devices is None or device in devices:
            store.acknowledge(device, device + "-inst", st["mode_version"],
                              st["epoch"] if epoch is None else epoch)


def cutover(store):
    for device, kind in DEVICES:
        store.register_participant(device, device + "-inst", kind)
    v = store.status()["mode_version"]
    v = store.set_mode("PREPARING_V2", v)
    v = store.set_mode("QUIESCED", v)
    for device, _ in DEVICES:
        store.acknowledge(device, device + "-inst", v + 1, 0)
    store.set_mode("V2_ONLY_NO_MAIN", v)


def transition(store, action, candidate, key, caller="owner-pc", profile="control"):
    st = store.status()
    t = store.request(caller, key, action, candidate, st["incarnation"], st["epoch"], st["policy_version"], profile)
    return t["transition_id"]


def promote(store, action, candidate, key, canary=True):
    """Run a transition to the end, assuming nothing earlier needs waiting out."""
    tid = transition(store, action, candidate, key)
    store.readiness(tid, True, True, "cp-1")
    store.drain(tid)
    store.fence(tid)
    lease = store.grant(tid)
    store.complete(tid, canary)
    return tid, lease


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def ticket(store, lease, clock, op="op-1", attempt="a-1", target="dell", body="open firefox", admission=None):
    st = store.status()
    return dict(admission_id=admission or f"adm-{op}-{attempt}", target=target, operation_id=op,
                attempt_id=attempt, digest=digest(body), main=lease["holder"], incarnation=lease["incarnation"],
                epoch=lease["epoch"], issued_at=clock.w - 1, admit_until=clock.w + 3600,
                issuer_incarnation=st["incarnation"])


_MISSING = object()


def handoff_facts(candidate):
    return {"control_ready": True, "ai_ready": False, "checkpoint": "cp-1"}


class HandoffFixtures:
    """Mixin (not a base test class) for SafeHandoff tests.

    The test's setUp provides ``self.port``, ``self.store``, ``self.clock`` and
    ``self.lease``; the test class provides ``repoint`` and ``canary`` methods.
    """

    def pc(self, device, cls=MainRuntime):
        return cls(
            self.port, device, device + "-inst",
            lambda: {"control_ready": True, "checkpoint": "cp-1"},
            clock=self.clock.mono, boot=lambda: "boot-a")

    def seat(self, device="zenbook", cls=MainRuntime):
        runtime = self.pc(device, cls)
        if device == "zenbook":
            runtime.accept_lease(self.lease)
        runtime.start()
        return runtime

    def use_brain(self, fn):
        """Install a stub ``jarvis.brainsync.handoff``; the previous module (or its absence)
        is restored by a sentinel check on cleanup."""
        previous = sys.modules.get("jarvis.brainsync", _MISSING)
        module = types.ModuleType("jarvis.brainsync")
        module.handoff = fn
        sys.modules["jarvis.brainsync"] = module

        def restore():
            if previous is _MISSING:
                sys.modules.pop("jarvis.brainsync", None)
            else:
                sys.modules["jarvis.brainsync"] = previous

        self.addCleanup(restore)
        return module

    def handoff(self, source, candidate, port=None, **kwargs):
        """A SafeHandoff over ``port`` (default ``self.port``); kwargs override the hooks."""
        return SafeHandoff(
            port or self.port, source_runtime=source, candidate_runtime=candidate,
            readiness=kwargs.get("readiness", handoff_facts),
            repoint=kwargs.get("repoint", self.repoint),
            canary=kwargs.get("canary", self.canary),
            journal_path=kwargs.get("journal_path"))
