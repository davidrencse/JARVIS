"""PC MAIN lifecycle and owner-driven recovery orchestration.

Implements the ``runtime.py`` facade in
``docs/design/mobile-continuity/OPERATIONAL-CONTRACT.md``. This module does not
activate v2, does not rewrite the saved cluster role, and does not talk to a
provider. A managed hub supplies an ``AuthorityPort`` (in tests,
``StoreAdapter`` over ``AuthorityStore``) and the booleans for local readiness.

Default policy is legacy-until-cutover and manual recovery. Automatic recover
and failback are opt-in. Failback cannot be configured below 30 seconds and
never uses ``make_main_now``. A cached grant never authorizes work while the
authority is unreachable. ``SafeHandoff`` is the manual owner sequence: it does
not create an authority, does not fail over on its own, and does not write a
saved role.

Not operational failover: no gateway is authenticated here, Windows has no
suspend-aware cache, and the store's safety margin is still the unmeasured
prototype value. ``GatewayPort`` speaks the authority client's method names.
It does not sign, does not enroll, and does not change the authority mode.
"""

import hashlib
import importlib
import json
import os
import re
from pathlib import Path

from jarvis.fsio import atomic_write

from jarvis.continuity.authority import (
    ACTIONS,
    LEASE_SECONDS,
    PROFILES,
    MAX_SIGNATURE,
    RENEW_SECONDS,
    TICKET_FIELDS as _TICKET_FIELDS,
    AuthorityError,
)
from jarvis.continuity.clock import CachedAuthority

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BANNED = frozenset({
    "body", "prompt", "content", "transcript", "secret", "cookie", "password",
    "passkey", "filename", "frame", "screenshot",
})
_LEGACY_MODES = frozenset({"LEGACY_ACTIVE", "PREPARING_V2", "QUIESCED", "ROLLBACK_QUIESCED"})
_EFFECTS = frozenset({"control", "ordinary", "ai"})
_LANES = ("luna", "grok", "quota_ok", "mediated_tools", "session_local")
# Renew must not undo these. "suspend" stays until invalidate_after_resume.
_NO_RENEW = frozenset({"suspend", "draining"})
# status() does not return lease_id. The holder must keep the grant response.
_LEASE_FIELDS = ("holder", "lease_id", "incarnation", "epoch")
_STEPS = frozenset({"drain", "fence", "grant", "complete", "cancel"})
_REQUEST_FIELDS = (
    "client_key", "action", "candidate", "expected_incarnation", "expected_epoch",
    "expected_policy", "profile",
)
# Client failures that mean "do not trust this answer", not an authority decision.
_LINK_CODES = frozenset({
    "partition", "unavailable", "no_relay", "redirect", "authority_mismatch",
    "bad_origin", "refused_bind",
})
_CODE = re.compile(r"^[a-z_]{1,40}$")
# Outbound only. A wider set than the readiness probe, matching the gateway's refusal.
_WIRE_BANNED = _BANNED | frozenset({
    "text", "file", "path", "url", "message", "image", "token", "secret",
    "authorization", "host", "upstream", "payload_body",
})


class LinkError(Exception):
    """The authority port could not be reached. Not an authority decision."""

    def __init__(self, code="partition"):
        super().__init__(code)
        self.code = code


class RuntimeRefusal(Exception):
    """Fail-closed runtime decision. ``code`` is safe to show; there is no detail text."""

    def __init__(self, code, wait=None):
        super().__init__(code)
        self.code = code
        self.wait = wait


def _ident_ok(value):
    return isinstance(value, str) and _ID.match(value) is not None


def _banned(mapping):
    return isinstance(mapping, dict) and any(key in mapping for key in _BANNED)


def _reason(value):
    return value if _ident_ok(value) else "stopped"


def _wire_banned(mapping):
    return isinstance(mapping, dict) and any(key in mapping for key in _WIRE_BANNED)


def _finite_wait(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


def _from_client(exc):
    """Map a client refusal onto the port's exceptions. Never a success.

    Transport and mismatched-authority answers become ``LinkError``. A stable
    authority code keeps its wait when the client actually supplied one. This
    does not invent a wait the client dropped.
    """
    if isinstance(exc, (LinkError, AuthorityError, RuntimeRefusal)):
        raise exc
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _CODE.match(code):
        if code in _LINK_CODES:
            raise LinkError("partition") from None
        raise AuthorityError(code, code, _finite_wait(getattr(exc, "wait", None))) from None
    if isinstance(exc, (TypeError, AttributeError)):
        raise RuntimeRefusal("bad_request") from None
    raise LinkError("partition") from None


def admission_decision(receipt):
    """Turn a store consume() record into execute or reconcile. Never a second effect.

    Unknown keys are refused, not stripped. ``already`` true is the original
    attempt coming back after a retry or a later handover.
    """
    if not isinstance(receipt, dict) or _banned(receipt) or "already" not in receipt:
        raise RuntimeRefusal("bad_request")
    if not isinstance(receipt["already"], bool):
        raise RuntimeRefusal("bad_request")
    allowed = {
        "admission_id", "target", "operation_id", "attempt_id", "digest", "main",
        "incarnation", "epoch", "issued_at", "admit_until", "issuer_incarnation",
        "consumed_at", "already",
    }
    if any(key not in allowed for key in receipt):
        raise RuntimeRefusal("bad_request")
    action = "reconcile" if receipt["already"] else "execute"
    return {"action": action, "admission_id": receipt["admission_id"], "already": receipt["already"]}


class RuntimePolicy:
    """Hub-supplied preferences. Nothing here is read from or written to config."""

    def __init__(self, preferred=None, recover_candidates=(), auto_recover=False,
                 auto_failback=False, failback_stability_s=30.0, readiness_ttl_s=10.0,
                 profile="control", caller="managed-hub"):
        if preferred is not None and not _ident_ok(preferred):
            raise RuntimeRefusal("bad_request")
        if not isinstance(recover_candidates, (tuple, list)) or len(recover_candidates) > 8:
            raise RuntimeRefusal("bad_request")
        if any(not _ident_ok(item) for item in recover_candidates):
            raise RuntimeRefusal("bad_request")
        if not isinstance(auto_recover, bool) or not isinstance(auto_failback, bool):
            raise RuntimeRefusal("bad_request")
        if isinstance(failback_stability_s, bool) or not isinstance(failback_stability_s, (int, float)):
            raise RuntimeRefusal("bad_request")
        if failback_stability_s < 30:
            raise RuntimeRefusal("instant_failback")
        if isinstance(readiness_ttl_s, bool) or not isinstance(readiness_ttl_s, (int, float)):
            raise RuntimeRefusal("bad_request")
        if not 0 < float(readiness_ttl_s) <= LEASE_SECONDS:
            raise RuntimeRefusal("bad_request")
        if profile not in PROFILES or not _ident_ok(caller):
            raise RuntimeRefusal("bad_request")
        self.preferred = preferred
        self.recover_candidates = tuple(recover_candidates)
        self.auto_recover = auto_recover
        self.auto_failback = auto_failback
        self.failback_stability_s = float(failback_stability_s)
        self.readiness_ttl_s = float(readiness_ttl_s)
        self.profile = profile
        self.caller = caller


def _ai_ready(raw, control):
    """True only for an explicit local subscription lane. A model name is not a lane."""
    if not control:
        return False
    for key in _LANES:
        if raw.get(key) is not True:
            return False
    return True


def _normalize_facts(raw, now, ttl):
    """Directory/probe entry → metadata, or None when it must not be forwarded."""
    if not isinstance(raw, dict) or _banned(raw):
        return None
    if raw.get("kind") != "pc":
        return None
    observed = raw.get("observed_at")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return None
    if observed > now + 1 or now - float(observed) > ttl:
        return None
    checkpoint = raw.get("checkpoint")
    if checkpoint is not None and not _ident_ok(checkpoint):
        return None
    control = raw.get("control_ready") is True
    return {
        "control_ready": control,
        "ai_ready": _ai_ready(raw, control),
        "checkpoint": checkpoint,
        "kind": "pc",
    }


def _meets(profile, facts):
    if facts is None or not facts["control_ready"]:
        return False
    if profile == "ai":
        return facts["ai_ready"]
    return profile == "control"


class StoreAdapter:
    """In-process port. No authentication: the gateway must check identity before this.

    Maps the gateway's step names onto ``AuthorityStore`` without collapsing
    grant and complete. ``release`` returns a status dict because the store
    method returns None.
    """

    def __init__(self, store):
        self.store = store

    def status(self):
        return self.store.status()

    def renew(self, lease_tuple):
        return self.store.renew(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"])

    def release(self, lease_tuple):
        self.store.release(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"])
        return self.store.status()

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        self.store.acknowledge(device_id, installation_id, mode_version, fenced_epoch, boot_id)
        return {"device_id": device_id, "installation_id": installation_id,
                "mode_version": mode_version, "fenced_epoch": fenced_epoch}

    def transition_request(self, request):
        return self.store.request(
            request["caller"], request["client_key"], request["action"], request["candidate"],
            request["expected_incarnation"], request["expected_epoch"], request["expected_policy"],
            request["profile"])

    def transition_readiness(self, transition_id, readiness):
        return self.store.readiness(
            transition_id, readiness["control_ready"], readiness["ai_ready"],
            readiness.get("checkpoint"))

    def transition_step(self, transition_id, step, canary_ok=None):
        if step == "drain":
            return self.store.drain(transition_id)
        if step == "fence":
            return self.store.fence(transition_id)
        if step == "grant":
            return self.store.grant(transition_id)
        if step == "complete":
            if not isinstance(canary_ok, bool):
                raise RuntimeRefusal("bad_request")
            return self.store.complete(transition_id, canary_ok)
        if step == "cancel":
            return self.store.cancel(transition_id)
        raise RuntimeRefusal("bad_request")

    def consume(self, ticket):
        return self.store.consume(**ticket)

    def admission(self, admission_id):
        return self.store.admission(admission_id)


class GatewayPort:
    """Authority-client port for ``MainRuntime`` and ``RecoveryOrchestrator``.

    The client is the shape in ``gateway.py`` ``AuthorityClient``: ``status()``,
    ``renew(holder, lease_id, incarnation, epoch)``, ``release(...)`` returning
    status, ``acknowledge(mode_version, fenced_epoch, boot_id=None)``,
    ``readiness(...)``, ``step(...)``, ``transition_request(...)`` without a
    caller field, and ``consume(ticket, ticket_signature)``.

    This wrapper does not authenticate. The client must already sign, and the
    gateway must reject a bad signature. Device and installation ids on
    acknowledge and renew are checked against the client before the call.
    A missing ticket signature is refused here. ``set_mode`` and ``enroll``
    are not called. Grant and complete stay separate ``step`` values.
    """

    def __init__(self, client):
        required = ("status", "renew", "release", "acknowledge", "readiness",
                    "step", "transition_request", "consume", "admission")
        if any(not callable(getattr(client, name, None)) for name in required):
            raise RuntimeRefusal("bad_request")
        self._client = client

    def status(self):
        return self._invoke(self._client.status)

    def renew(self, lease_tuple):
        self._bound_holder(lease_tuple)
        return self._invoke(lambda: self._client.renew(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"]))

    def release(self, lease_tuple):
        self._bound_holder(lease_tuple)
        return self._invoke(lambda: self._client.release(
            lease_tuple["holder"], lease_tuple["lease_id"],
            lease_tuple["incarnation"], lease_tuple["epoch"]))

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        if (device_id != getattr(self._client, "device_id", None)
                or installation_id != getattr(self._client, "installation_id", None)):
            raise RuntimeRefusal("forbidden")
        return self._invoke(lambda: self._client.acknowledge(mode_version, fenced_epoch, boot_id))

    def transition_request(self, request):
        """Owner call. ``caller`` stays off the wire; the client key is the caller."""
        if not isinstance(request, dict) or _wire_banned(request):
            raise RuntimeRefusal("bad_request")
        if any(key not in request for key in _REQUEST_FIELDS):
            raise RuntimeRefusal("bad_request")
        if set(request) - set(_REQUEST_FIELDS) - {"caller"}:
            raise RuntimeRefusal("bad_request")
        return self._invoke(lambda: self._client.transition_request(
            request["client_key"], request["action"], request["candidate"],
            request["expected_incarnation"], request["expected_epoch"],
            request["expected_policy"], request["profile"]))

    def transition_readiness(self, transition_id, readiness):
        if not isinstance(readiness, dict) or _wire_banned(readiness):
            raise RuntimeRefusal("bad_request")
        control, ai = readiness.get("control_ready"), readiness.get("ai_ready")
        if not isinstance(control, bool) or not isinstance(ai, bool):
            raise RuntimeRefusal("bad_request")
        checkpoint = readiness.get("checkpoint")
        if checkpoint is not None and not _ident_ok(checkpoint):
            raise RuntimeRefusal("bad_request")
        if set(readiness) - {"control_ready", "ai_ready", "checkpoint"}:
            raise RuntimeRefusal("bad_request")

        def send():
            if checkpoint is None:
                return self._client.readiness(transition_id, control, ai)
            return self._client.readiness(transition_id, control, ai, checkpoint)

        return self._invoke(send)

    def transition_step(self, transition_id, step, canary_ok=None):
        if step not in _STEPS:
            raise RuntimeRefusal("bad_request")
        if step == "complete":
            if not isinstance(canary_ok, bool):
                raise RuntimeRefusal("bad_request")
        elif canary_ok is not None:
            raise RuntimeRefusal("bad_request")
        return self._invoke(lambda: self._client.step(transition_id, step, canary_ok))

    def consume(self, ticket=None, *, ticket_signature=None, **fields):
        """Refuse an unsigned consume. The store-shaped keyword form is not a bypass."""
        if ticket is not None and fields:
            raise RuntimeRefusal("bad_request")
        payload = dict(ticket if ticket is not None else fields)
        if _wire_banned(payload):
            raise RuntimeRefusal("bad_request")
        if ticket_signature is not None:
            if "ticket_signature" in payload and payload["ticket_signature"] != ticket_signature:
                raise RuntimeRefusal("bad_request")
            payload["ticket_signature"] = ticket_signature
        signature = payload.get("ticket_signature")
        if not isinstance(signature, str) or "BEGIN SSH SIGNATURE" not in signature:
            raise RuntimeRefusal("bad_request")
        if "END SSH SIGNATURE" not in signature or len(signature) > MAX_SIGNATURE or not signature.isascii():
            raise RuntimeRefusal("bad_request")
        if any(key not in payload for key in _TICKET_FIELDS):
            raise RuntimeRefusal("bad_request")
        if set(payload) - set(_TICKET_FIELDS) - {"ticket_signature"}:
            raise RuntimeRefusal("bad_request")
        body = {key: payload[key] for key in _TICKET_FIELDS}
        return self._invoke(lambda: self._client.consume(body, signature))

    def admission(self, admission_id):
        if not _ident_ok(admission_id):
            raise RuntimeRefusal("bad_request")
        return self._invoke(lambda: self._client.admission(admission_id))

    def _bound_holder(self, lease_tuple):
        if not isinstance(lease_tuple, dict) or _wire_banned(lease_tuple):
            raise RuntimeRefusal("bad_request")
        if any(key not in lease_tuple for key in _LEASE_FIELDS):
            raise RuntimeRefusal("bad_request")
        if lease_tuple["holder"] != getattr(self._client, "device_id", None):
            raise RuntimeRefusal("forbidden")

    def _invoke(self, fn):
        try:
            return fn()
        except Exception as exc:
            _from_client(exc)


class MainRuntime:
    """One PC's lease, readiness, and admission latch. The hub calls these; nothing is scheduled here."""

    def __init__(self, port, device_id, installation_id, probe, *, clock="auto", boot=None):
        if not _ident_ok(device_id) or not _ident_ok(installation_id):
            raise RuntimeRefusal("bad_request")
        if not callable(probe):
            raise RuntimeRefusal("bad_request")
        self._port = port
        self.device_id = device_id
        self.installation_id = installation_id
        self._probe = probe
        self._boot = boot
        self._cache = CachedAuthority(clock=clock, boot=boot or (lambda: None))
        self._lease = None
        self._started = False
        self._hold = None
        self._block = None
        self._last = None
        self._stale = None
        self._releasing = False

    def start(self):
        self._started = True
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            return self.state()
        self._last = status
        if status["mode"] in _LEGACY_MODES:
            self._block = "legacy"
            return self.state()
        if status["mode"] == "QUARANTINED":
            self._block = "quarantined"
            return self.state()
        if self._block in ("legacy", "quarantined"):
            self._block = None
        if (self._lease and status.get("main") == self.device_id and status.get("lease_valid")
                and self._block not in _NO_RENEW and not self._hold):
            self.renew_once()
        return self.state()

    def state(self):
        """Read-only. After a partition this is the last metadata snapshot, marked stale."""
        if self._block == "partition" and self._stale is not None:
            return dict(self._stale)
        return self._view(self._last, stale=False)

    def readiness(self, profile="control"):
        if profile not in PROFILES:
            raise RuntimeRefusal("bad_request")
        raw = self._probe()
        if not isinstance(raw, dict) or _banned(raw):
            raise RuntimeRefusal("bad_request")
        checkpoint = raw.get("checkpoint")
        if checkpoint is not None and not _ident_ok(checkpoint):
            raise RuntimeRefusal("bad_checkpoint")
        control = raw.get("control_ready") is True
        ai = _ai_ready(raw, control)
        meets = _meets(profile, {"control_ready": control, "ai_ready": ai})
        reason = "ok" if meets else ("profile" if profile == "ai" and control and not ai else "not_ready")
        view = {"profile": profile, "control_ready": control, "ai_ready": ai, "meets": meets,
                "checkpoint": checkpoint, "reason": reason}
        for key in _LANES:
            view[key] = raw.get(key) is True
        return view

    def accept_lease(self, lease):
        """Remember a grant. ``status()`` has no lease id, so a restarted PC cannot renew."""
        if not isinstance(lease, dict) or _banned(lease):
            raise RuntimeRefusal("bad_request")
        if any(key not in lease for key in _LEASE_FIELDS):
            raise RuntimeRefusal("bad_request")
        holder = lease["holder"]
        if holder != self.device_id or not _ident_ok(lease["lease_id"]):
            raise RuntimeRefusal("not_holder")
        for name in ("incarnation", "epoch"):
            if isinstance(lease[name], bool) or not isinstance(lease[name], int):
                raise RuntimeRefusal("bad_request")
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            raise RuntimeRefusal("partition")
        self._last = status
        if status["mode"] in _LEGACY_MODES or status["mode"] == "QUARANTINED":
            raise RuntimeRefusal(self._block or "legacy")
        if status.get("incarnation") != lease["incarnation"] or status.get("epoch") != lease["epoch"]:
            raise RuntimeRefusal("stale")
        if status.get("main") != self.device_id:
            raise RuntimeRefusal("not_holder")
        self._lease = {key: lease[key] for key in _LEASE_FIELDS}
        self._cache.invalidate("accepted")

    def lease_snapshot(self):
        """Copied grant tuple, or None when this PC has no grant in memory.

        Status does not include a lease id. A coordinator that never saw the
        grant cannot rebuild one from this snapshot.
        """
        lease = self._lease
        if not isinstance(lease, dict) or any(key not in lease for key in _LEASE_FIELDS):
            return None
        return {key: lease[key] for key in _LEASE_FIELDS}

    def release_for_handoff(self, transition_id):
        """Release this PC's grant during an open drain. This does not fence.

        A missing, stale, or expired local grant is a report. ``fence`` stays
        false so this PC cannot clear the authority barrier on its own.
        """
        if not _ident_ok(transition_id):
            raise RuntimeRefusal("bad_request")
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            return {"released": False, "code": "partition", "fence": False}
        except AuthorityError as err:
            return {"released": False, "code": err.code, "fence": False}
        except RuntimeRefusal as err:
            return {"released": False, "code": err.code, "fence": False}
        self._last = status
        current = status.get("transition") or {}
        if current.get("transition_id") != transition_id or current.get("state") != "DRAINING":
            return {"released": False, "code": "unknown_transition", "fence": False}
        if current.get("source") not in (None, self.device_id):
            return {"released": False, "code": "not_holder", "fence": False}
        snapshot = self.lease_snapshot()
        if snapshot is None or snapshot.get("holder") != self.device_id:
            return {"released": False, "code": "no_lease", "fence": False}
        if (status.get("incarnation") != snapshot["incarnation"]
                or status.get("epoch") != snapshot["epoch"]
                or status.get("lease_holder") != self.device_id):
            code = "no_lease" if status.get("lease_holder") is None else "stale"
            return {"released": False, "code": code, "fence": False}
        try:
            self._port.release(snapshot)
        except LinkError:
            self._lose_link()
            return {"released": False, "code": "partition", "fence": False}
        except AuthorityError as err:
            return {"released": False, "code": err.code, "fence": False}
        except RuntimeRefusal as err:
            return {"released": False, "code": err.code, "fence": False}
        # Keep the local tuple. A later renew must be refused by the authority.
        return {"released": True, "code": "released", "fence": False}

    def renew_once(self):
        if not self._started:
            raise RuntimeRefusal("not_started")
        if self._hold:
            return {"code": self._hold, "wait": None}
        if self._block == "suspend":
            return {"code": "suspend", "wait": None}
        if self._block == "draining":
            if not self._drain_finished():
                return {"code": self._block, "wait": None}
        if self._lease is None:
            return {"code": "no_lease", "wait": None}
        started = self._cache.now()
        try:
            answer = self._port.renew(self._lease)
        except LinkError:
            self._lose_link()
            return {"code": "partition", "wait": None}
        except AuthorityError as err:
            self._cache.invalidate(err.code)
            self._block = err.code
            return {"code": err.code, "wait": err.wait}
        cached = False
        if started is not None and isinstance(answer, dict):
            cached = self._cache.observe(
                holder=self._lease["holder"], incarnation=self._lease["incarnation"],
                epoch=self._lease["epoch"], valid_for=answer.get("valid_for"),
                ready=answer.get("ready") is True, request_started=started)
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            return {"code": "partition", "wait": None}
        self._last = status
        ready = isinstance(answer, dict) and answer.get("ready") is True
        if (ready and status.get("mode") == "V2_ACTIVE" and not status.get("transition")
                and status.get("main") == self.device_id and status.get("main_ready")):
            self._block = None
        remaining = self._cache.remaining(self.device_id) if self._cache.now() is not None else 0.0
        renew_after = answer.get("renew_after", RENEW_SECONDS) if isinstance(answer, dict) else RENEW_SECONDS
        return {
            "code": "ok" if cached or self._cache.now() is None else self._cache.reason,
            "holder": self._lease["holder"], "lease_id": self._lease["lease_id"],
            "incarnation": self._lease["incarnation"], "epoch": self._lease["epoch"],
            "valid_for": None if not isinstance(answer, dict) else answer.get("valid_for"),
            "ready": ready, "remaining": remaining, "cached": cached,
            "renew_after": renew_after, "wait": None,
        }

    def require_authority(self, effect_class, target=None):
        if effect_class not in _EFFECTS:
            raise RuntimeRefusal("bad_request")
        if target is not None and not _ident_ok(target):
            raise RuntimeRefusal("bad_request")
        if not self._started:
            raise RuntimeRefusal("not_started")
        if self._hold:
            raise RuntimeRefusal(self._hold)
        if self._block == "suspend" or self._block in ("legacy", "quarantined"):
            raise RuntimeRefusal(self._block)
        if self._block == "draining" and not self._drain_finished():
            raise RuntimeRefusal(self._block)
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            raise RuntimeRefusal("partition")
        self._last = status
        if status["mode"] in _LEGACY_MODES:
            self._block = "legacy"
            raise RuntimeRefusal("legacy")
        if status["mode"] == "QUARANTINED":
            self._block = "quarantined"
            raise RuntimeRefusal("quarantined")
        if status.get("mode") != "V2_ACTIVE" or not status.get("lease_valid"):
            self._block = "no_main"
            raise RuntimeRefusal("no_main")
        if status.get("main") != self.device_id:
            self._cache.invalidate("not_holder")
            self._block = "not_holder"
            raise RuntimeRefusal("not_holder")
        if not status.get("main_ready"):
            raise RuntimeRefusal("not_ready")
        report = self.readiness("ai" if effect_class == "ai" else "control")
        if not report["meets"]:
            raise RuntimeRefusal(report["reason"])
        if self._cache.now() is not None:
            if self._cache.remaining(self.device_id) <= 0 or self._lease is None:
                if self._lease is None:
                    raise RuntimeRefusal("no_lease_id")
                renewed = self.renew_once()
                if renewed["code"] != "ok" or self._cache.remaining(self.device_id) <= 0:
                    raise RuntimeRefusal("expired" if renewed["code"] == "ok" else renewed["code"])
                status = self._last
            seen = self._cache.seen
            if seen != (status["incarnation"], status["epoch"]):
                self._cache.invalidate("newer_tuple")
                self._block = "stale"
                raise RuntimeRefusal("stale")
        if self._block == "partition":
            raise RuntimeRefusal("partition")
        return {
            "effect_class": effect_class, "target": target, "holder": self.device_id,
            "incarnation": status["incarnation"], "epoch": status["epoch"],
            "mode": status["mode"], "mode_version": status["mode_version"],
            "authority_id": status["authority_id"], "policy_version": status["policy_version"],
            "ready": True, "online": True, "consume_required": True,
        }

    def begin_drain(self, transition_id):
        """Stop local admission for a transition the authority already has open.

        Does not call store ``drain``. That step is owner-authenticated and belongs
        to ``RecoveryOrchestrator``. Acking the epoch is separate: doing it here
        would make a later cancel destroy this MAIN.
        """
        if not _ident_ok(transition_id):
            raise RuntimeRefusal("bad_request")
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            raise RuntimeRefusal("partition")
        self._last = status
        current = status.get("transition") or {}
        if current.get("transition_id") != transition_id:
            raise RuntimeRefusal("unknown_transition")
        self._block = "draining"
        self._cache.invalidate("draining")
        return {"transition_id": transition_id, "admission_stopped": True,
                "renewals_stopped": True, "authority_drain": False,
                "state": current.get("state"), "code": "draining"}

    def release_before_suspend(self):
        """Best-effort ``release`` plus a local stop. A missed ack does not elect a successor."""
        self._block = "suspend"
        self._cache.invalidate("suspend")
        self._releasing = True
        released = False
        code = "no_lease"
        if self._lease is not None:
            try:
                self._port.release(self._lease)
                released = True
                code = "released"
                self._lease = None
            except LinkError:
                self._lose_link()
                self._block = "suspend"
                code = "partition"
            except AuthorityError as err:
                code = err.code
        acked = False
        # A busy release means a transition is open. Acking then would let cancel
        # destroy the current MAIN. Ack only after the lease is actually gone.
        if released or code == "no_lease":
            try:
                self.acknowledge_current()
                acked = True
            except (RuntimeRefusal, AuthorityError, LinkError):
                acked = False
        self._releasing = False
        self._block = "suspend"
        return {"released": released, "admission_stopped": True, "invalidated": True,
                "acked": acked, "code": code}

    def acknowledge_current(self):
        """Refuse the epoch ``status`` currently names. A live MAIN may do this only while stopping."""
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            raise RuntimeRefusal("partition")
        self._last = status
        if status["mode"] in _LEGACY_MODES or status["mode"] == "QUARANTINED":
            raise RuntimeRefusal("legacy" if status["mode"] != "QUARANTINED" else "quarantined")
        live = status.get("main") == self.device_id and status.get("lease_valid")
        if live and not self._releasing and self._block not in _NO_RENEW and not self._hold:
            raise RuntimeRefusal("main_alive")
        boot = self._boot() if self._boot else None
        if boot is not None and not _ident_ok(boot):
            boot = None
        return self._port.acknowledge(
            self.device_id, self.installation_id, status["mode_version"], status["epoch"], boot)

    def invalidate_after_resume(self):
        """Drop every cached grant. Does not reclaim MAIN and does not renew."""
        self._cache.invalidate("resumed")
        if self._block == "partition":
            self._stale = None
        if self._block != "legacy":
            self._block = "resumed"

    def stop_new_admission(self, reason):
        """Sticky for this process. A later renew cannot clear an explicit hold."""
        self._hold = _reason(reason)
        self._cache.invalidate("stopped")

    def _drain_finished(self):
        """A local drain latch lasts only while the authority transition is still open."""
        try:
            status = self._port.status()
        except LinkError:
            self._lose_link()
            return False
        self._last = status
        if status.get("transition"):
            return False
        self._block = None
        return True

    def _lose_link(self):
        self._stale = self._view(self._last, stale=True)
        self._cache.invalidate("link_loss")
        self._block = "partition"

    def _view(self, status, stale):
        status = status or {}
        remaining = 0.0
        if not stale and self._cache.now() is not None:
            remaining = self._cache.remaining(self.device_id)
        phase = self._phase(status)
        open_admission = (
            not stale and not self._hold and not self._block and phase == "main"
            and (self._cache.now() is None or remaining > 0)
        )
        reason = self._hold or self._block or ("stale" if stale else "ok")
        return {
            "phase": phase, "mode": status.get("mode"), "mode_version": status.get("mode_version"),
            "incarnation": status.get("incarnation"), "epoch": status.get("epoch"),
            "main": status.get("main"), "preferred": status.get("preferred"),
            "device_id": self.device_id, "installation_id": self.installation_id,
            "admission_open": open_admission, "cache_remaining": remaining,
            "stale": stale, "reason": reason, "authority_id": status.get("authority_id"),
            "policy_version": status.get("policy_version"),
            "transition_id": (status.get("transition") or {}).get("transition_id"),
        }

    def _phase(self, status):
        if not self._started:
            return "not_started"
        if self._block == "partition":
            return "partition"
        if self._hold:
            return "stopped"
        if self._block == "suspend":
            return "suspended"
        if self._block == "legacy" or status.get("mode") in _LEGACY_MODES:
            return "legacy"
        if self._block == "quarantined" or status.get("mode") == "QUARANTINED":
            return "quarantined"
        if self._block == "draining" or status.get("mode") == "HANDOVER":
            # The candidate holds a not-ready lease while the old epoch is fenced.
            # That is canary time, not a local drain.
            if (self._block != "draining" and status.get("main") == self.device_id
                    and status.get("lease_valid") and not status.get("main_ready")):
                return "main_not_ready"
            return "draining"
        if status.get("main") == self.device_id and status.get("main_ready") and not self._block:
            return "main"
        if status.get("main") == self.device_id and status.get("lease_valid"):
            return "main_not_ready"
        if status.get("lease_valid"):
            return "standby"
        return "no_main"


def _step(ok, phase, code, wait=None, transition=None, lease=None, reason=None,
          stable_for=0.0, candidate=None):
    transition = transition or {}
    return {
        "ok": ok, "phase": phase, "code": code, "wait": wait, "reason": reason,
        "stable_for": stable_for,
        "transition_id": transition.get("transition_id"),
        "transition_state": transition.get("state"),
        "action": transition.get("action"),
        "candidate": candidate if candidate is not None else transition.get("candidate"),
        "lease": lease,
    }


class RecoveryOrchestrator:
    """Hub-side driver. One authority step at a time. Never grants admission by itself.

    ``step`` will not complete a canary, will not call ``make_main_now``, and will
    not shrink a store wait. The hub passes ``submit_canary(ok)`` only after its
    own check. Candidate facts come from the hub's directory; this object does
    not probe providers or list participants (the store has no such read).
    """

    def __init__(self, port, directory, policy, clock):
        if not isinstance(policy, RuntimePolicy) or not callable(clock):
            raise RuntimeRefusal("bad_request")
        self._port = port
        self._directory = directory
        self.policy = policy
        self._clock = clock
        self._stable_key = None
        self._stable_since = None
        self._lease = None
        self._keys = {}

    def note_discontinuity(self):
        """Resume, link loss, or a directory reset. Stability does not accrue across this."""
        self._stable_key = None
        self._stable_since = None

    def step(self):
        try:
            status = self._port.status()
        except LinkError:
            self.note_discontinuity()
            return _step(False, "partition", "partition")
        if status["mode"] in _LEGACY_MODES:
            return _step(False, "legacy", "legacy")
        if status["mode"] == "QUARANTINED":
            return _step(False, "quarantined", "quarantined")
        if status.get("transition"):
            return self._advance(status, status["transition"])
        barrier = status.get("grant_barrier")
        if barrier:
            return _step(False, "waiting", "not_quiet", wait=barrier)
        if not status.get("lease_valid"):
            return self._recover(status)
        return self._failback(status)

    def open_manual(self, action, candidate, client_key, profile=None):
        """Owner action. No force flag. Readiness is checked before a transition is opened."""
        if action not in ACTIONS or not _ident_ok(candidate) or not _ident_ok(client_key):
            raise RuntimeRefusal("bad_request")
        profile = self.policy.profile if profile is None else profile
        if profile not in PROFILES:
            raise RuntimeRefusal("bad_request")
        try:
            status = self._port.status()
        except LinkError:
            self.note_discontinuity()
            return _step(False, "partition", "partition")
        facts = self._facts(candidate, self._clock())
        if not _meets(profile, facts):
            return _step(False, "checking", "not_ready", reason="profile" if profile == "ai" else "not_ready")
        return self._request(status, action, candidate, profile, client_key)

    def submit_canary(self, transition_id, ok):
        if not isinstance(ok, bool) or not _ident_ok(transition_id):
            raise RuntimeRefusal("bad_request")
        try:
            view = self._port.transition_step(transition_id, "complete", canary_ok=ok)
        except LinkError:
            self.note_discontinuity()
            return _step(False, "partition", "partition")
        except AuthorityError as err:
            return _step(False, "promoting", err.code, wait=err.wait)
        if not ok or view.get("state") != "SUCCEEDED":
            self._lease = None
        return _step(view.get("state") == "SUCCEEDED",
                     "main_ready" if view.get("state") == "SUCCEEDED" else "no_main",
                     view.get("outcome") or view.get("state"), transition=view, lease=None)

    def cancel(self, transition_id):
        if not _ident_ok(transition_id):
            raise RuntimeRefusal("bad_request")
        try:
            view = self._port.transition_step(transition_id, "cancel")
        except (LinkError, AuthorityError) as err:
            code = getattr(err, "code", "partition")
            return _step(False, "cancelled", code, wait=getattr(err, "wait", None))
        self._lease = None
        return _step(True, "cancelled", view.get("outcome") or view.get("state"), transition=view)

    def _recover(self, status):
        if not self.policy.auto_recover:
            return _step(False, "no_main", "manual")
        now = self._clock()
        chosen, reason = self._choose(status, now)
        if chosen is None:
            return _step(False, "no_main", "NO_ELIGIBLE_MAIN", reason=reason)
        key = self._auto_key("recover", chosen, status)
        return self._request(status, "recover", chosen, self.policy.profile, key)

    def _failback(self, status):
        target = status.get("preferred") or self.policy.preferred
        if not self.policy.auto_failback or not target or target == status.get("main"):
            return _step(False, "main", "holding")
        now = self._clock()
        facts = self._facts(target, now)
        stable = self._stability(target, facts, now)
        if not _meets(self.policy.profile, facts) or not facts or not facts.get("checkpoint"):
            return _step(False, "main", "stabilising", stable_for=stable, reason="not_ready")
        if stable < self.policy.failback_stability_s:
            return _step(False, "main", "stabilising", stable_for=stable)
        key = self._auto_key("set_preferred_main", target, status)
        return self._request(status, "set_preferred_main", target, self.policy.profile, key)

    def _choose(self, status, now):
        ordered = []
        for candidate in (status.get("preferred"), self.policy.preferred, *self.policy.recover_candidates):
            if candidate and candidate not in ordered:
                ordered.append(candidate)
        profile_gap = False
        stale = False
        for candidate in ordered:
            raw = self._read(candidate)
            facts = _normalize_facts(raw, now, self.policy.readiness_ttl_s)
            if facts is None:
                if isinstance(raw, dict) and raw.get("kind") == "pc" and not _banned(raw):
                    stale = True
                continue
            if _meets(self.policy.profile, facts):
                return candidate, None
            if facts["control_ready"] and self.policy.profile == "ai" and not facts["ai_ready"]:
                profile_gap = True
        if profile_gap:
            return None, "profile"
        if stale:
            return None, "stale_readiness"
        return None, "not_ready"

    def _facts(self, device, now):
        return _normalize_facts(self._read(device), now, self.policy.readiness_ttl_s)

    def _read(self, device):
        source = self._directory
        if hasattr(source, "read"):
            return source.read(device)
        return source.get(device)

    def _stability(self, device, facts, now):
        if facts is None or not facts["control_ready"] or not facts.get("checkpoint"):
            self._stable_key = None
            self._stable_since = None
            return 0.0
        key = (device, facts["control_ready"], facts["ai_ready"], facts.get("checkpoint"), self.policy.profile)
        if key != self._stable_key:
            self._stable_key = key
            self._stable_since = now
        return now - self._stable_since

    def _auto_key(self, action, candidate, status):
        identity = (action, candidate, status["incarnation"], status["epoch"], self.policy.profile)
        if identity not in self._keys:
            self._keys[identity] = "auto-{0}-{1}-{2}-{3}-{4}".format(*identity)
        return self._keys[identity]

    def _request(self, status, action, candidate, profile, client_key):
        request = {
            "caller": self.policy.caller, "client_key": client_key, "action": action,
            "candidate": candidate, "expected_incarnation": status["incarnation"],
            "expected_epoch": status["epoch"], "expected_policy": status["policy_version"],
            "profile": profile,
        }
        try:
            view = self._port.transition_request(request)
        except LinkError:
            self.note_discontinuity()
            return _step(False, "partition", "partition")
        except AuthorityError as err:
            return _step(False, "no_main" if action == "recover" else "main", err.code, wait=err.wait,
                         candidate=candidate)
        return _step(True, "requested", "requested", transition=view)

    def _advance(self, status, transition):
        state = transition.get("state")
        tid = transition["transition_id"]
        if state in ("REQUESTED", "VALIDATING"):
            facts = self._facts(transition["candidate"], self._clock())
            ready = _meets(transition["profile"], facts)
            # Booleans only. An ai transition stays ai; control-only facts do not drain it.
            if ready and facts is not None:
                body = {"control_ready": True, "ai_ready": facts["ai_ready"],
                        "checkpoint": facts.get("checkpoint")}
            else:
                body = {"control_ready": False, "ai_ready": False, "checkpoint": None}
            try:
                self._port.transition_readiness(tid, body)
                if not ready:
                    return _step(False, "checking", "not_ready", transition=transition,
                                 reason="profile" if transition["profile"] == "ai" else "not_ready")
                view = self._port.transition_step(tid, "drain")
            except AuthorityError as err:
                return _step(False, "checking", err.code, wait=err.wait, transition=transition)
            except LinkError:
                self.note_discontinuity()
                return _step(False, "partition", "partition")
            return _step(True, "draining", "draining", transition=view)
        if state == "DRAINING":
            try:
                self._port.transition_step(tid, "fence")
            except AuthorityError as err:
                if err.code in ("not_quiet", "lease_live"):
                    return _step(False, "waiting", err.code, wait=err.wait, transition=transition)
                return _step(False, "waiting", err.code, wait=err.wait, transition=transition)
            except LinkError:
                self.note_discontinuity()
                return _step(False, "partition", "partition")
            return self._grant(tid, transition)
        if state == "FENCING":
            return self._grant(tid, transition)
        if state == "GRANTED_NOT_READY":
            return _step(False, "promoting", "canary_required", transition=transition, lease=self._lease)
        return _step(False, "checking", "bad_state", transition=transition)

    def _grant(self, transition_id, transition):
        try:
            lease = self._port.transition_step(transition_id, "grant")
        except AuthorityError as err:
            return _step(False, "waiting", err.code, wait=err.wait, transition=transition)
        except LinkError:
            self.note_discontinuity()
            return _step(False, "partition", "partition")
        if not isinstance(lease, dict) or lease.get("ready") is not False:
            self._lease = None
            return _step(False, "promoting", "not_ready", transition=transition)
        self._lease = {key: lease[key] for key in _LEASE_FIELDS if key in lease}
        self._lease["valid_for"] = lease.get("valid_for")
        self._lease["ready"] = False
        return _step(True, "promoting", "canary_required", transition=transition, lease=dict(self._lease))


_HANDOFF_ACTIONS = frozenset({"set_preferred_main", "recover"})
_FENCE_WAIT = frozenset({"not_quiet", "lease_live"})
_PORT_HANDOFF = ("status", "release", "transition_request", "transition_readiness", "transition_step")


def _caller_key(action, old_main, new_main, status):
    body = "{0}|{1}|{2}|{3}|{4}|{5}".format(
        action, old_main, new_main, status["incarnation"], status["epoch"], status["policy_version"])
    return "sh-" + hashlib.sha256(body.encode("ascii")).hexdigest()[:32]


_JOURNAL_STEPS = ("brain", "repoint", "canary", "complete")
_JOURNAL_KEYS = frozenset({
    "schema", "transition_id", "candidate", "action", "incarnation", "epoch", "steps",
})
_STEP_KEYS = frozenset({"phase", "ok"})


def _journal_default(base):
    """``base/.jarvis`` only when ``base`` is an existing directory. A token is not a path."""
    if not isinstance(base, (str, os.PathLike)):
        return None
    path = Path(base)
    try:
        if not path.is_dir() or path.is_symlink():
            return None
    except OSError:
        return None
    return path / ".jarvis" / "handoff-journal.json"


def _journal_banned(value):
    if isinstance(value, dict):
        if any(key not in _JOURNAL_KEYS and key not in _STEP_KEYS for key in value):
            # Nested step maps use phase/ok only. The top document uses _JOURNAL_KEYS.
            if any(key in _WIRE_BANNED or key in {"lease_id", "config", "base", "checkpoint"} for key in value):
                return True
        for key, item in value.items():
            if key in _WIRE_BANNED or key in {"lease_id", "config", "base", "checkpoint", "secret"}:
                return True
            if _journal_banned(item):
                return True
        return False
    if isinstance(value, list):
        return any(_journal_banned(item) for item in value)
    if isinstance(value, str) and (value.startswith("ls-") or "BEGIN " in value):
        return True
    return False


def _journal_load_text(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != 1 or _journal_banned(raw):
        return None
    if set(raw) - _JOURNAL_KEYS:
        return None
    steps = raw.get("steps", {})
    if not isinstance(steps, dict) or set(steps) - set(_JOURNAL_STEPS):
        return None
    for name, step in steps.items():
        if not isinstance(step, dict) or set(step) - _STEP_KEYS:
            return None
        if step.get("phase") not in ("started", "finished"):
            return None
        if step.get("phase") == "finished" and step.get("ok") not in (True, False):
            return None
        if step.get("phase") == "started" and "ok" in step:
            return None
    return raw


def _journal_write(path, doc):
    """Replace the journal. Caller holds the lock. Metadata only."""
    path = Path(path)
    if _journal_banned(doc) or set(doc) - _JOURNAL_KEYS:
        raise RuntimeRefusal("bad_request")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    atomic_write(path, payload, mode=0o600, fsync=True)


class _HandoffJournal:
    """Per-transition step record. Lease ids and personal fields are not stored."""

    def __init__(self, path):
        self.path = None if path is None else Path(path)
        self.corrupt = False
        self.doc = {"schema": 1, "steps": {}}
        if self.path is not None and self.path.exists():
            loaded = _journal_load_text(self.path)
            if loaded is None:
                self.corrupt = True
            else:
                self.doc = loaded

    def step(self, name):
        found = (self.doc.get("steps") or {}).get(name) or {}
        if not isinstance(found, dict):
            return {}
        return {"phase": found.get("phase"), "ok": found.get("ok")}

    def any_recorded(self):
        return any(self.step(name).get("phase") in ("started", "finished") for name in _JOURNAL_STEPS)

    def callback_done(self, name):
        found = self.step(name)
        return found.get("phase") == "finished" and found.get("ok") is True

    def bind(self, transition, incarnation, epoch):
        if self.corrupt:
            return False
        tid = transition.get("transition_id")
        if not _ident_ok(tid):
            self.corrupt = True
            return False

        def merge(doc):
            if doc.get("transition_id") == tid:
                if doc.get("epoch") not in (None, epoch) or doc.get("incarnation") not in (None, incarnation):
                    return None
                if doc.get("candidate") not in (None, transition.get("candidate")):
                    return None
            else:
                doc = {"schema": 1, "steps": {}}
            doc["schema"] = 1
            doc["transition_id"] = tid
            doc["candidate"] = transition.get("candidate")
            doc["action"] = transition.get("action")
            doc["incarnation"] = incarnation
            doc["epoch"] = epoch
            doc.setdefault("steps", {})
            return doc

        return self._update(merge)

    def mark(self, name, phase, ok=None):
        if self.corrupt or name not in _JOURNAL_STEPS or phase not in ("started", "finished"):
            return False
        body = {"phase": phase}
        if phase == "finished":
            body["ok"] = ok is True

        def merge(doc):
            doc.setdefault("steps", {})[name] = body
            return doc

        return self._update(merge)

    def claim(self, name):
        """Compare-and-set ``started`` under the journal lock.

        ``("claimed", None)`` is the only winner. ``("started", None)`` means
        another caller already owns the step. ``("finished", ok)`` is a prior
        result. Anything else is ``("corrupt", None)`` and must not run the hook.
        """
        if self.corrupt or name not in _JOURNAL_STEPS:
            return "corrupt", None
        box = {"phase": "corrupt", "ok": None}

        def merge(doc):
            steps = doc.get("steps")
            if not isinstance(steps, dict):
                box["phase"] = "corrupt"
                return None
            found = steps.get(name)
            if found is None:
                steps[name] = {"phase": "started"}
                box["phase"] = "claimed"
                return doc
            if not isinstance(found, dict):
                box["phase"] = "corrupt"
                return None
            phase = found.get("phase")
            if phase == "started":
                box["phase"] = "started"
                return doc
            if phase == "finished":
                box["phase"] = "finished"
                box["ok"] = found.get("ok") is True
                return doc
            box["phase"] = "corrupt"
            return None

        if not self._update(merge):
            return "corrupt", None
        return box["phase"], box["ok"]

    def _update(self, merge):
        if self.corrupt:
            return False
        if self.path is None:
            updated = merge(self.doc)
            if updated is None:
                self.corrupt = True
                return False
            self.doc = updated
            return True
        from jarvis.platforms import _acquire_os_lock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name("." + self.path.name + ".jarvis-lock")
        handle = None
        try:
            # Closing the handle releases the lock on both OSes. An explicit
            # LK_UNLCK before close would let another process slip in on Windows.
            handle = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600),
                               "r+b", buffering=0)
            _acquire_os_lock(handle)
            current = self.doc
            if self.path.exists():
                loaded = _journal_load_text(self.path)
                if loaded is None:
                    self.corrupt = True
                    return False
                current = loaded
            updated = merge(current)
            if updated is None or _journal_banned(updated):
                self.corrupt = True
                return False
            _journal_write(self.path, updated)
            self.doc = updated
            return True
        except (OSError, RuntimeRefusal):
            return False
        finally:
            if handle is not None:
                handle.close()


def _public_snapshot(runtime):
    """Holder's copied tuple. Never reads a private lease attribute."""
    method = getattr(runtime, "lease_snapshot", None)
    if not callable(method):
        return None
    try:
        snap = method()
    except (RuntimeRefusal, AuthorityError, LinkError, OSError):
        return None
    return _lease_view(snap)


class SafeHandoff:
    """Manual MAIN handoff through an owner-authenticated port.

    ``port`` is a ``StoreAdapter`` or ``GatewayPort``: ``transition_request`` and
    ``transition_step`` are the owner calls. This object does not create an
    authority, enroll a device, or choose a successor by itself.

    ``readiness(candidate)`` returns ``control_ready``, ``ai_ready``, and an
    optional ``checkpoint``. Missing or non-boolean flags are a refusal. Nothing
    here fills those in. ``candidate_readiness(device)`` is that same check for
    the coordinator: the device must be this candidate, and the result is the
    validated trio or ``RuntimeRefusal``. ``repoint(old, new, lease)`` must be
    an authenticated signing-key and endpoint update. ``ok`` alone, or
    ``config_only``, does not confirm it. ``canary(new)`` is a real bool. After
    the old lease is fenced, ``jarvis.brainsync.handoff(old, new, config, base)``
    is imported and must return ``ok`` True. ``config`` and ``base`` are local
    hook arguments, not authority requests. A failure after the fence cancels
    only when authenticated status shows no holder. Fence waits come back as
    ``pending`` with ``retry_after``. The same call resumes one transition for
    the same candidate and refuses a different one. The result never includes a
    lease id.

    There is no force flag and no automatic failover. A ready target with no
    matching journal is ``already_main`` and is not a completed handoff. Callbacks
    are journaled as metadata. The first process to claim a step runs it; a
    started step with no result is not replayed.
    """

    def __init__(self, port, *, source_runtime, candidate_runtime, readiness, repoint, canary,
                 journal_path=None, brain=None):
        if brain is not None and not callable(brain):
            raise RuntimeRefusal("bad_request")
        if any(not callable(getattr(port, name, None)) for name in _PORT_HANDOFF):
            raise RuntimeRefusal("bad_request")
        if candidate_runtime is None or not _ident_ok(getattr(candidate_runtime, "device_id", None)):
            raise RuntimeRefusal("bad_request")
        if source_runtime is not None and not _ident_ok(getattr(source_runtime, "device_id", None)):
            raise RuntimeRefusal("bad_request")
        if not callable(readiness) or not callable(repoint) or not callable(canary):
            raise RuntimeRefusal("bad_request")
        if journal_path is not None and not isinstance(journal_path, (str, os.PathLike)):
            raise RuntimeRefusal("bad_request")
        if isinstance(journal_path, str) and not journal_path:
            raise RuntimeRefusal("bad_request")
        self._port = port
        self._source = source_runtime
        self._candidate = candidate_runtime
        self._readiness = readiness
        self._repoint = repoint
        self._canary = canary
        self._brain = _brainsync_handoff if brain is None else brain
        self._journal_path = journal_path
        self._active_journal = None

    def authority_status(self):
        """Authenticated ``port.status()``. Transport errors are ``RuntimeRefusal('partition')``."""
        try:
            status = self._port.status()
        except RuntimeRefusal as err:
            if err.code in _LINK_CODES or err.code == "partition":
                raise RuntimeRefusal("partition") from None
            raise
        except LinkError:
            raise RuntimeRefusal("partition") from None
        except AuthorityError as err:
            code = err.code if isinstance(err.code, str) and _CODE.match(err.code) else "partition"
            if code in _LINK_CODES:
                code = "partition"
            raise RuntimeRefusal(code) from None
        except Exception:
            raise RuntimeRefusal("partition") from None
        if not isinstance(status, dict) or _wire_banned(status):
            raise RuntimeRefusal("partition")
        return status

    def candidate_readiness(self, device):
        """Validated readiness for this handoff's candidate. No invented flags.

        A device other than ``self._candidate.device_id`` raises
        ``RuntimeRefusal('candidacy_mismatch')`` and does not call readiness.
        ``_facts`` failure raises ``RuntimeRefusal`` with that code. A validated
        report is exactly ``control_ready``, ``ai_ready``, and ``checkpoint``,
        including explicit false flags.
        """
        if device != self._candidate.device_id:
            raise RuntimeRefusal("candidacy_mismatch")
        facts, code = self._facts(device)
        if code is not None:
            raise RuntimeRefusal(code)
        return facts

    def _read_status(self):
        try:
            return self.authority_status(), None
        except RuntimeRefusal as err:
            return None, err

    def __call__(self, old_main, new_main, config, base):
        if not _ident_ok(old_main) or not _ident_ok(new_main) or old_main == new_main:
            return _handoff_result(False, "refused", "bad_request")
        if self._candidate.device_id != new_main:
            return _handoff_result(False, "refused", "candidacy_mismatch")
        if self._source is not None and self._source.device_id != old_main:
            return _handoff_result(False, "refused", "candidacy_mismatch")
        # config and base stay local. Path, url, and host are for the storage
        # hook. Outbound authority bodies and the journal keep their allowlists.
        self._active_journal = None
        self._journal_base = base
        status, err = self._read_status()
        if err is not None:
            return _handoff_from_error(err)
        if status.get("mode") in _LEGACY_MODES:
            return _handoff_result(False, "refused", "legacy", status=status)
        if status.get("mode") == "QUARANTINED":
            return _handoff_result(False, "refused", "quarantined", status=status)
        transition = status.get("transition")
        if transition:
            return self._resume(status, transition, old_main, new_main, config, base)
        if status.get("lease_valid") and status.get("main") == new_main:
            if (status.get("main_ready") is True and status.get("mode") == "V2_ACTIVE"
                    and not status.get("transition")):
                proved = self._journaled_success(status, new_main)
                if proved is not None:
                    return proved
                return _handoff_result(False, "already_main", "already_main", status=status, action=None)
            return _handoff_result(False, "not_ready", "not_ready", status=status)
        action, refusal = self._action(status, old_main)
        if refusal is not None:
            return _handoff_result(False, "refused", refusal, status=status)
        facts, fact_code = self._facts(new_main)
        if fact_code is not None:
            return _handoff_result(False, "not_ready", fact_code, status=status)
        if not facts["control_ready"]:
            return _handoff_result(False, "not_ready", "not_ready", status=status)
        request = {
            "caller": "safe-handoff",
            "client_key": _caller_key(action, old_main, new_main, status),
            "action": action,
            "candidate": new_main,
            "expected_incarnation": status["incarnation"],
            "expected_epoch": status["epoch"],
            "expected_policy": status["policy_version"],
            "profile": "control",
        }
        _opened, err = _invoke(lambda: self._port.transition_request(request))
        if err is not None:
            return _handoff_from_error(err, status=status)
        status, err = self._read_status()
        if err is not None:
            return _handoff_from_error(err)
        transition = status.get("transition")
        if not transition or transition.get("candidate") != new_main:
            return _handoff_result(False, "refused", "bad_state", status=status)
        return self._resume(status, transition, old_main, new_main, config, base)

    def _action(self, status, old_main):
        """``set_preferred_main`` only while the central lease is live. ``recover`` only when it is not."""
        if status.get("lease_valid") and status.get("main"):
            if status.get("main") != old_main:
                return None, "candidacy_mismatch"
            if self._source is None:
                return None, "source_required"
            return "set_preferred_main", None
        if status.get("lease_valid"):
            return None, "main_alive"
        return "recover", None

    def _facts(self, candidate):
        try:
            raw = self._readiness(candidate)
        except Exception:
            return None, "not_ready"
        if not isinstance(raw, dict) or _wire_banned(raw):
            return None, "bad_request"
        if set(raw) - {"control_ready", "ai_ready", "checkpoint"}:
            return None, "bad_request"
        if "control_ready" not in raw or "ai_ready" not in raw:
            return None, "not_ready"
        if type(raw["control_ready"]) is not bool or type(raw["ai_ready"]) is not bool:
            return None, "not_ready"
        checkpoint = raw.get("checkpoint")
        if checkpoint is not None and not _ident_ok(checkpoint):
            return None, "bad_checkpoint"
        return {
            "control_ready": raw["control_ready"],
            "ai_ready": raw["ai_ready"],
            "checkpoint": checkpoint,
        }, None

    def _resume(self, status, transition, old_main, new_main, config, base):
        if transition.get("candidate") != new_main or transition.get("action") not in _HANDOFF_ACTIONS:
            return _handoff_result(False, "conflict", "conflict", status=status, transition=transition)
        source = transition.get("source")
        if source is not None and source != old_main:
            return _handoff_result(False, "conflict", "conflict", status=status, transition=transition)
        if transition.get("action") == "recover" and status.get("lease_valid"):
            return _handoff_result(False, "refused", "main_alive", status=status, transition=transition)
        state = transition.get("state")
        tid = transition.get("transition_id")
        if state in ("REQUESTED", "VALIDATING"):
            facts, fact_code = self._facts(new_main)
            if fact_code is not None:
                return _handoff_result(False, "not_ready", fact_code, status=status, transition=transition)
            _posted, err = _invoke(lambda: self._port.transition_readiness(tid, facts))
            if err is not None:
                return _handoff_from_error(err, status=status, transition=transition)
            if not facts["control_ready"]:
                return _handoff_result(False, "not_ready", "not_ready", status=status, transition=transition)
            view, err = _invoke(lambda: self._port.transition_step(tid, "drain"))
            if err is not None:
                return _handoff_from_error(err, status=status, transition=transition)
            transition = view
            state = view.get("state")
        if state == "DRAINING":
            if self._source is not None and callable(getattr(self._source, "begin_drain", None)):
                try:
                    report = self._source.begin_drain(tid)
                except (RuntimeRefusal, AuthorityError) as err:
                    return _handoff_result(False, "refused", err.code, status=status, transition=transition)
                if not isinstance(report, dict) or report.get("admission_stopped") is not True:
                    return _handoff_result(False, "refused", "drain", status=status, transition=transition)
            released = self._release_source(status, transition)
            if released is not None:
                return _handoff_result(False, "refused", released, status=status, transition=transition)
            fenced, err = _invoke(lambda: self._port.transition_step(tid, "fence"))
            if err is not None:
                if isinstance(err, AuthorityError) and err.code in _FENCE_WAIT:
                    fresh, _ignored = self._read_status()
                    return _handoff_result(
                        False, "pending", err.code, status=fresh or status, transition=transition,
                        retry_after=err.wait)
                if isinstance(err, LinkError):
                    return _handoff_result(False, "partition", "partition", status=status, transition=transition)
                return self._abort(tid, getattr(err, "code", "fence"), transition)
            transition = fenced or transition
            state = "FENCING"
        if state == "FENCING":
            lease, err = _invoke(lambda: self._port.transition_step(tid, "grant"))
            if err is not None:
                if isinstance(err, AuthorityError) and err.code in _FENCE_WAIT:
                    fresh, _ignored = self._read_status()
                    return _handoff_result(
                        False, "pending", err.code, status=fresh or status, transition=transition,
                        retry_after=err.wait)
                if isinstance(err, LinkError) or _is_partition(err):
                    return self._lost_grant(tid, transition, old_main, new_main, config, base)
                return self._abort(tid, getattr(err, "code", "grant"), transition)
            # A not-ready grant is the only grant. ready True would admit before the canary.
            if not isinstance(lease, dict) or lease.get("ready") is not False:
                return self._abort(tid, "epoch", transition)
            if not _grant_ok(lease, new_main, transition.get("expected_epoch")):
                return self._abort(tid, "epoch", transition)
            try:
                self._candidate.accept_lease(lease)
            except (RuntimeRefusal, AuthorityError) as err:
                return self._abort(tid, err.code, transition)
            except Exception:
                return self._abort(tid, "accept", transition)
            state = "GRANTED_NOT_READY"
        if state == "GRANTED_NOT_READY":
            return self._promote(transition, old_main, new_main, config, base)
        return _handoff_result(False, "refused", "bad_state", status=status, transition=transition)

    def _journal(self):
        if self._active_journal is None:
            path = self._journal_path
            if path is None:
                path = _journal_default(getattr(self, "_journal_base", None))
            self._active_journal = _HandoffJournal(path)
        return self._active_journal

    def _grant_current(self, status, transition):
        """Current authority tuple plus the holder's public snapshot, or a refusal code."""
        current = status.get("transition") or {}
        if current.get("transition_id") != transition.get("transition_id"):
            return None, "stale"
        if current.get("state") != "GRANTED_NOT_READY":
            return None, "bad_state"
        if current.get("candidate") != self._candidate.device_id:
            return None, "stale"
        if status.get("lease_valid") is not True or status.get("main") != self._candidate.device_id:
            return None, "expired"
        if status.get("lease_holder") != self._candidate.device_id or status.get("main_ready") is True:
            return None, "stale"
        snap = _public_snapshot(self._candidate)
        if not _grant_ok(snap, self._candidate.device_id, transition.get("expected_epoch")):
            return None, "lease_unavailable"
        if (snap.get("incarnation") != status.get("incarnation")
                or snap.get("epoch") != status.get("epoch")
                or snap.get("holder") != status.get("lease_holder")):
            return None, "stale"
        return snap, None

    def _lost_grant(self, transition_id, transition, old_main, new_main, config, base):
        """The grant answer was lost. Do not invent a lease id."""
        status, err = self._read_status()
        if err is not None:
            return _handoff_from_error(err, transition=transition)
        current = status.get("transition") or {}
        if (current.get("transition_id") != transition_id
                or current.get("state") != "GRANTED_NOT_READY"):
            return _handoff_result(False, "partition", "partition", status=status, transition=transition)
        _snap, problem = self._grant_current(status, current)
        if problem is not None:
            if problem == "lease_unavailable":
                return self._abort(transition_id, "lease_unavailable", current)
            journal = self._journal()
            if journal.corrupt or journal.any_recorded():
                return self._uncertain(current, "reconcile")
            return self._abort(transition_id, problem, current)
        return self._promote(current, old_main, new_main, config, base)

    def _promote(self, transition, old_main, new_main, config, base):
        self._journal_base = base
        status, err = self._read_status()
        if err is not None:
            return _handoff_from_error(err, transition=transition)
        snap, problem = self._grant_current(status, transition)
        journal = self._journal()
        if journal.corrupt:
            return self._uncertain(transition, "reconcile")
        if problem is not None:
            if journal.any_recorded():
                return self._uncertain(transition, "reconcile")
            return self._abort(transition.get("transition_id"), problem, transition)
        if not journal.bind(transition, snap["incarnation"], snap["epoch"]):
            return self._uncertain(transition, "reconcile")
        brain = self._callback(
            transition, "brain", lambda _snap: self._brain(old_main, new_main, config, base),
            lambda result: isinstance(result, dict) and result.get("ok") is True,
            lambda result: "missing_hook" if isinstance(result, dict) and result.get("error") == "missing_hook" else "brain")
        if brain is not None:
            return brain
        repoint = self._callback(
            transition, "repoint", lambda granted: self._repoint(old_main, new_main, dict(granted)),
            _repoint_ok, lambda _result: "repoint")
        if repoint is not None:
            return repoint
        canary = self._callback(
            transition, "canary", lambda _snap: self._canary(new_main),
            lambda result: result is True, lambda _result: "canary")
        if canary is not None:
            return canary
        return self._complete(transition)

    def _callback(self, transition, name, call, accept, refuse_code):
        """One winner claims the step under the journal lock and runs the hook."""
        journal = self._journal()
        status, err = self._read_status()
        if err is not None:
            return _handoff_from_error(err, transition=transition)
        snap, problem = self._grant_current(status, transition)
        phase, ok_flag = journal.claim(name)
        if phase == "started":
            return self._uncertain(transition, "reconcile")
        if phase == "finished":
            if ok_flag is True:
                return None
            return self._abort(transition.get("transition_id"), refuse_code(None), transition)
        if phase != "claimed":
            return self._uncertain(transition, "reconcile")
        if problem is not None:
            return self._abort(transition.get("transition_id"), problem, transition)
        try:
            result = call(snap)
        except Exception:
            return self._uncertain(transition, "reconcile")
        if not accept(result):
            journal.mark(name, "finished", ok=False)
            return self._abort(transition.get("transition_id"), refuse_code(result), transition)
        if not journal.mark(name, "finished", ok=True):
            return self._uncertain(transition, "reconcile")
        return None

    def _complete(self, transition):
        journal = self._journal()
        tid = transition.get("transition_id")
        record = journal.step("complete")
        if record.get("phase") == "finished":
            if record.get("ok") is not True:
                return self._abort(tid, "complete", transition)
            proved = self._prove_ready(transition)
            if proved is None:
                return self._uncertain(transition, "reconcile")
            return proved
        if record.get("phase") == "started":
            proved = self._prove_ready(transition)
            if proved is not None:
                journal.mark("complete", "finished", ok=True)
                return proved
            status, err = self._read_status()
            if err is not None:
                return self._uncertain(transition, "reconcile")
            _snap, problem = self._grant_current(status, transition)
            if problem is not None:
                return self._uncertain(transition, "reconcile")
        else:
            status, err = self._read_status()
            if err is not None:
                return _handoff_from_error(err, transition=transition)
            _snap, problem = self._grant_current(status, transition)
            if problem is not None:
                if journal.any_recorded():
                    return self._uncertain(transition, "reconcile")
                return self._abort(tid, problem, transition)
            if not journal.mark("complete", "started"):
                return self._uncertain(transition, "reconcile")
        view, err = _invoke(lambda: self._port.transition_step(tid, "complete", canary_ok=True))
        if isinstance(err, LinkError) or _is_partition(err):
            proved = self._prove_ready(transition)
            if proved is not None:
                journal.mark("complete", "finished", ok=True)
                return proved
            return self._uncertain(transition, "reconcile")
        if err is not None:
            journal.mark("complete", "finished", ok=False)
            return self._abort(tid, getattr(err, "code", "complete"), transition)
        journal.mark("complete", "finished", ok=True)
        proved = self._prove_ready(transition)
        if proved is None:
            fresh, _ignored = self._read_status()
            if not fresh or view.get("state") != "SUCCEEDED":
                return _handoff_result(False, "no_main", "status", status=fresh, transition=view or transition)
            return _handoff_result(False, "no_main", "not_ready", status=fresh, transition=view)
        return proved

    def _journaled_success(self, status, new_main):
        """Ready state proves a handoff only when this journal bound the callbacks."""
        journal = self._journal()
        if journal.corrupt or journal.doc.get("candidate") != new_main:
            return None
        if (journal.doc.get("incarnation") != status.get("incarnation")
                or journal.doc.get("epoch") != status.get("epoch")):
            return None
        action = journal.doc.get("action")
        transition_id = journal.doc.get("transition_id")
        if action not in _HANDOFF_ACTIONS or not _ident_ok(transition_id):
            return None
        return self._prove_ready({
            "transition_id": transition_id,
            "candidate": new_main,
            "action": action,
            "state": "SUCCEEDED",
        })

    def _prove_ready(self, transition):
        """Success only when authority status matches the journaled grant tuple."""
        journal = self._journal()
        for name in ("brain", "repoint", "canary"):
            if not journal.callback_done(name):
                return None
        status, err = self._read_status()
        if err is not None or not status:
            return None
        if status.get("mode") != "V2_ACTIVE" or status.get("main_ready") is not True:
            return None
        if status.get("lease_valid") is not True or status.get("main") != transition.get("candidate"):
            return None
        if status.get("transition"):
            return None
        if (status.get("incarnation") != journal.doc.get("incarnation")
                or status.get("epoch") != journal.doc.get("epoch")):
            return None
        if isinstance(journal.doc.get("epoch"), bool) or not isinstance(journal.doc.get("epoch"), int):
            return None
        return _handoff_result(
            True, "ready", "new_main", status=status,
            transition={"transition_id": transition.get("transition_id"), "state": "SUCCEEDED",
                        "action": transition.get("action")},
            action=transition.get("action"))

    def _uncertain(self, transition, code):
        """The effect may have happened. Do not cancel, replay, or open another transition."""
        fresh, _err = self._read_status()
        return _handoff_result(False, "uncertain", code, status=fresh, transition=transition)

    def _release_source(self, status, transition):
        """Ask the source to release. A local miss does not clear or skip the authority fence."""
        if transition.get("action") != "set_preferred_main":
            return None
        if self._source is None:
            if status.get("lease_valid"):
                return "source_required"
            return None
        method = getattr(self._source, "release_for_handoff", None)
        if not callable(method):
            if status.get("lease_valid"):
                return "no_lease"
            return None
        try:
            report = method(transition.get("transition_id"))
        except (RuntimeRefusal, AuthorityError) as err:
            report = {"released": False, "code": err.code, "fence": False}
        except Exception:
            report = {"released": False, "code": "no_lease", "fence": False}
        if not isinstance(report, dict):
            report = {"released": False, "code": "no_lease", "fence": False}
        # A source that claims the fence is done is ignored. The authority call is the fence.
        if report.get("released") is True and report.get("fence") is not True:
            return None
        code = report.get("code") or "no_lease"
        if code == "partition":
            return "partition"
        fresh, err = self._read_status()
        if err is not None:
            return "partition"
        if fresh.get("lease_valid"):
            return code
        return None

    def _status_clear(self, status):
        """True only when authenticated status shows no holder and no open transition."""
        if not isinstance(status, dict):
            return False
        if status.get("lease_valid") is True or status.get("main") is not None:
            return False
        if status.get("lease_holder") is not None or status.get("transition"):
            return False
        return status.get("mode") == "V2_ONLY_NO_MAIN"

    def _abort(self, transition_id, code, transition):
        """Cancel, then say ``no_main`` only when status shows the holder is gone.

        A failed cancel, a live candidate, or a status that cannot be read is
        ``uncertain`` or ``partition`` and still carries the transition.
        """
        _view, err = _invoke(lambda: self._port.transition_step(transition_id, "cancel"))
        fresh, status_err = self._read_status()
        bound = transition if isinstance(transition, dict) else {}
        if not bound.get("transition_id"):
            bound = dict(bound)
            bound["transition_id"] = transition_id
        if status_err is not None or not isinstance(fresh, dict):
            if _is_partition(status_err) or isinstance(status_err, LinkError):
                return _handoff_result(False, "partition", "partition", transition=bound)
            return _handoff_result(
                False, "uncertain", "reconcile", status=fresh if isinstance(fresh, dict) else None,
                transition=bound)
        if err is not None:
            if isinstance(err, LinkError) or _is_partition(err):
                return _handoff_result(False, "partition", "partition", status=fresh, transition=bound)
            return _handoff_result(False, "uncertain", "reconcile", status=fresh, transition=bound)
        if not self._status_clear(fresh):
            return _handoff_result(False, "uncertain", "reconcile", status=fresh, transition=bound)
        return _handoff_result(False, "no_main", code, status=fresh, transition=bound)


def _grant_ok(lease, new_main, expected_epoch):
    if not isinstance(lease, dict) or lease.get("holder") != new_main:
        return False
    epoch = lease.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return False
    if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int):
        return False
    if epoch <= expected_epoch:
        return False
    if "ready" in lease and lease.get("ready") is not False and lease.get("ready") is not True:
        return False
    return True


def _lease_view(lease):
    if not isinstance(lease, dict) or any(key not in lease for key in _LEASE_FIELDS):
        return None
    view = {key: lease[key] for key in _LEASE_FIELDS}
    if "valid_for" in lease:
        view["valid_for"] = lease["valid_for"]
    if "ready" in lease:
        view["ready"] = lease["ready"]
    return view


def _repoint_ok(result):
    if not isinstance(result, dict) or result.get("config_only") is True:
        return False
    return (result.get("ok") is True and result.get("authenticated") is True
            and result.get("key_updated") is True and result.get("endpoint_updated") is True)


def _brainsync_handoff(old_main, new_main, config, base):
    """Import the storage hook only after the fence. Never treat a missing hook as success."""
    try:
        module = importlib.import_module("jarvis.brainsync")
    except ImportError:
        return {"ok": False, "status": "missing", "error": "missing_hook"}
    hook = getattr(module, "handoff", None)
    if not callable(hook):
        return {"ok": False, "status": "missing", "error": "missing_hook"}
    try:
        result = hook(old_main, new_main, config, base)
    except Exception:
        return {"ok": False, "status": "error", "error": "hook_failed"}
    if not isinstance(result, dict):
        return {"ok": False, "status": "error", "error": "hook_failed"}
    return result


def _is_partition(err):
    return isinstance(err, (LinkError, RuntimeRefusal)) and getattr(err, "code", None) == "partition"


def _invoke(fn):
    try:
        return fn(), None
    except (LinkError, AuthorityError, RuntimeRefusal) as err:
        return None, err


def _handoff_from_error(err, status=None, transition=None):
    code = getattr(err, "code", None) or "partition"
    if isinstance(err, LinkError) or code in _LINK_CODES:
        code = "partition"
        state = "partition"
    else:
        state = "refused"
    return _handoff_result(False, state, code, status=status, transition=transition)


def _handoff_result(ok, state, code, status=None, transition=None, retry_after=None, action=None):
    status = status or {}
    transition = transition or {}
    if action is None:
        action = transition.get("action")
    body = {
        "ok": bool(ok) and ok is True,
        "status": state,
        "code": code,
        "transition_id": transition.get("transition_id"),
        "main": status.get("main"),
        "epoch": status.get("epoch"),
        "mode": status.get("mode"),
        "action": action,
    }
    if state == "pending":
        body["retry_after"] = retry_after
    return body
