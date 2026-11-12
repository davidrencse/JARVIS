"""PC MAIN lifecycle and owner-driven recovery orchestration.

Implements the ``runtime.py`` facade in
``docs/design/mobile-continuity/OPERATIONAL-CONTRACT.md``. This module does not
activate v2, does not rewrite the saved cluster role, and does not talk to a
provider. A managed hub supplies an ``AuthorityPort`` (in tests,
``StoreAdapter`` over ``AuthorityStore``) and the booleans for local readiness.

Default policy is legacy-until-cutover and manual recovery. Automatic recover
and failback are opt-in. Failback cannot be configured below 30 seconds and
never uses ``make_main_now``. A cached grant never authorizes work while the
authority is unreachable.

Not operational failover: no gateway is authenticated here, Windows has no
suspend-aware cache, and the store's safety margin is still the unmeasured
prototype value. ``GatewayPort`` speaks the authority client's method names.
It does not sign, does not enroll, and does not change the authority mode.
"""

import re

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
_PORT_METHODS = (
    "status", "renew", "release", "acknowledge", "transition_request",
    "transition_readiness", "transition_step", "consume", "admission",
)
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
